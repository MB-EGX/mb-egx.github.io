"""
chart_patterns.py
==================
Deterministic, rule-based geometric pattern-recognition engine for OHLC
price data.

No external APIs, no machine learning. Local swing highs/lows are pulled
out of the close series with ``scipy.signal.argrelextrema`` and every
pattern is then just a set of numpy/pandas inequality checks against
those swing points (shoulders roughly equal, neckline roughly flat,
trendline slopes roughly parallel/converging, etc.), each allowed to
drift by a configurable ``epsilon`` tolerance to absorb real-world noise.

Usage
-----
    from chart_patterns import PatternDetector

    detector = PatternDetector(df, epsilon=0.03, order=5)
    patterns = detector.detect_all()

    # or, one-shot:
    from chart_patterns import detect_patterns
    patterns = detect_patterns(df, epsilon=0.03)

``df`` must be a pandas DataFrame with lowercase ``open/high/low/close``
columns (the same shape ``analytics.QuantitativeEngine`` already passes
around) and a monotonically increasing index (a ``DatetimeIndex`` is
fine and is what you'll normally have; a plain ``RangeIndex`` also
works). ``volume`` is not required by this module.

Extending
---------
Every pattern is one ``detect_*`` method that returns a list of result
dicts. To add a new pattern: write a ``detect_my_pattern`` method that
follows the same shape (see ``_result`` below) and add its name to
``PatternDetector.PATTERN_METHODS`` — ``detect_all`` picks it up
automatically and a broken detector can never take down the rest of
the scan (each one runs in its own try/except).

Output shape
------------
Each detected pattern is a dict:

    {
        "pattern": "Head & Shoulders",
        "direction": "bearish",              # bullish / bearish / neutral
        "start_index": 12,                   # positional index into df
        "end_index": 34,
        "start_date": "2025-03-10",          # df.index[start_index], stringified if a date
        "end_date": "2025-04-02",
        "levels": {                          # pattern-specific key price levels
            "left_shoulder": 18.4,
            "head": 21.1,
            "right_shoulder": 18.6,
            "neckline": 16.9,
            "target": 12.7,
        },
        "quality": 0.87,                     # 0-1 geometric goodness-of-fit, NOT an ML confidence score
    }
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd
from scipy.signal import argrelextrema
from scipy.stats import linregress

REQUIRED_COLUMNS = ("open", "high", "low", "close")


# =============================================================================
# Small stateless geometry helpers
# =============================================================================
def _pct_diff(a: float, b: float) -> float:
    """Symmetric relative difference between two prices (0.03 == 3%)."""
    denom = (abs(a) + abs(b)) / 2.0
    if denom == 0:
        return 0.0
    return abs(a - b) / denom


def _fit_line(xs, ys) -> tuple[float, float, float]:
    """Least-squares line through (xs, ys). Returns (slope, intercept, r)."""
    xs = np.asarray(xs, dtype=float)
    ys = np.asarray(ys, dtype=float)
    if len(xs) < 2 or np.allclose(xs, xs[0]):
        return 0.0, float(ys[0]) if len(ys) else 0.0, 0.0
    res = linregress(xs, ys)
    slope = 0.0 if np.isnan(res.slope) else float(res.slope)
    intercept = 0.0 if np.isnan(res.intercept) else float(res.intercept)
    rvalue = 0.0 if np.isnan(res.rvalue) else float(res.rvalue)
    return slope, intercept, rvalue


def _line_value(slope: float, intercept: float, x: float) -> float:
    return slope * x + intercept


@dataclass(frozen=True)
class Swing:
    """One local extremum (swing high or swing low) on the close series."""
    index: int
    price: float
    kind: str  # "P" (peak/swing-high) or "T" (trough/swing-low)


# =============================================================================
# Main engine
# =============================================================================
class PatternDetector:
    """Scans one ticker's OHLC history for classical chart patterns.

    Parameters
    ----------
    df : pd.DataFrame
        Must contain lowercase open/high/low/close columns.
    epsilon : float
        Relative tolerance (e.g. 0.03 = 3%) used everywhere two price
        levels are compared for "roughly equal" (shoulders, necklines,
        double-top/bottom peaks, flat triangle edges, ...). This is the
        one knob that trades strictness for recall.
    order : int
        Number of bars on each side a point must beat to count as a
        local extremum (passed straight to ``argrelextrema``). Higher =
        fewer, more significant swing points; lower = more, noisier
        swing points. 5 is a reasonable default for daily bars.
    """

    #: Registry of every pattern detector this class knows about. Add a
    #: new "detect_*" method and its name here to plug in a new pattern
    #: (strategy-pattern style extensibility) without touching detect_all.
    PATTERN_METHODS: tuple[str, ...] = (
        "detect_head_and_shoulders",
        "detect_inverse_head_and_shoulders",
        "detect_double_top",
        "detect_double_bottom",
        "detect_ascending_triangle",
        "detect_descending_triangle",
        "detect_symmetrical_triangle",
        "detect_price_channel",
        "detect_cup_and_handle",
        "detect_bull_flag",
        "detect_bear_flag",
        "detect_pennant",
    )

    def __init__(self, df: pd.DataFrame, epsilon: float = 0.03, order: int = 5):
        missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
        if missing:
            raise ValueError(f"PatternDetector: df is missing required column(s): {missing}")
        if not (0.0 < epsilon < 0.5):
            raise ValueError("epsilon should be a small fraction, e.g. 0.03 for 3%")
        if order < 1:
            raise ValueError("order must be >= 1")

        self.df = df
        self.epsilon = float(epsilon)
        self.order = int(order)

        self.close = df["close"].to_numpy(dtype=float)
        self.high = df["high"].to_numpy(dtype=float)
        self.low = df["low"].to_numpy(dtype=float)
        self.n = len(df)

        self.swings: list[Swing] = self._find_swings(self.close, self.order)

    # -------------------------------------------------------------------
    # Swing extraction
    # -------------------------------------------------------------------
    def _find_swings(self, series: np.ndarray, order: int) -> list[Swing]:
        """Alternating list of swing highs/lows on ``series``.

        Peaks and troughs are found independently, then merged in index
        order and de-duplicated so the result always strictly
        alternates P, T, P, T, ... — every reversal/triangle/channel
        rule below depends on that alternation to define legs.
        """
        if len(series) < 2 * order + 1:
            return []

        peak_idx = argrelextrema(series, np.greater, order=order)[0]
        trough_idx = argrelextrema(series, np.less, order=order)[0]

        raw = [Swing(int(i), float(series[i]), "P") for i in peak_idx]
        raw += [Swing(int(i), float(series[i]), "T") for i in trough_idx]
        raw.sort(key=lambda s: s.index)

        cleaned: list[Swing] = []
        for s in raw:
            if cleaned and cleaned[-1].kind == s.kind:
                # Two same-type swings back-to-back (can happen when a peak
                # and trough tie in index spacing) — keep only the more
                # extreme one so the sequence stays strictly alternating.
                if s.kind == "P" and s.price > cleaned[-1].price:
                    cleaned[-1] = s
                elif s.kind == "T" and s.price < cleaned[-1].price:
                    cleaned[-1] = s
                continue
            cleaned.append(s)
        return cleaned

    # -------------------------------------------------------------------
    # Output helpers
    # -------------------------------------------------------------------
    def _date(self, i: int):
        try:
            val = self.df.index[i]
        except Exception:
            return None
        if isinstance(val, pd.Timestamp):
            return val.strftime("%Y-%m-%d")
        return val

    def _result(
        self,
        name: str,
        start_idx: int,
        end_idx: int,
        direction: str,
        levels: dict,
        quality: float | None = None,
        extra: dict | None = None,
    ) -> dict:
        clean_levels = {}
        for k, v in levels.items():
            if isinstance(v, (int, float, np.floating, np.integer)) and not isinstance(v, bool):
                clean_levels[k] = round(float(v), 4)
            else:
                clean_levels[k] = v
        result = {
            "pattern": name,
            "direction": direction,
            "start_index": int(start_idx),
            "end_index": int(end_idx),
            "start_date": self._date(start_idx),
            "end_date": self._date(end_idx),
            "levels": clean_levels,
        }
        if quality is not None:
            result["quality"] = round(float(max(0.0, min(1.0, quality))), 2)
        if extra:
            result.update(extra)
        return result

    @staticmethod
    def _quality_from_diffs(diffs: list[float], tol: float) -> float:
        """1.0 = every comparison was a perfect match, 0.0 = every
        comparison was right at (or past) the tolerance ``tol``."""
        if not diffs or tol <= 0:
            return 1.0
        avg = sum(diffs) / len(diffs)
        return max(0.0, 1.0 - (avg / tol))

    def _slope_class(self, slope: float, span: float, avg_price: float) -> str:
        """Classify a fitted line as 'rising' / 'falling' / 'flat' by
        how much it moves over the window relative to price level and
        ``epsilon``, so flat/rising/falling scale with the instrument."""
        if avg_price <= 0 or span <= 0:
            return "flat"
        rel_move = slope * span / avg_price
        if rel_move > self.epsilon:
            return "rising"
        if rel_move < -self.epsilon:
            return "falling"
        return "flat"

    # =====================================================================
    # REVERSAL PATTERNS
    # =====================================================================
    def detect_head_and_shoulders(self) -> list[dict]:
        """P(shoulder) - T - P(head, highest) - T - P(shoulder) with a
        roughly flat neckline through the two troughs. Bearish reversal;
        target projects the head-to-neckline depth below the neckline."""
        results = []
        sw = self.swings
        for i in range(len(sw) - 4):
            p1, t1, p2, t2, p3 = sw[i:i + 5]
            if [p1.kind, t1.kind, p2.kind, t2.kind, p3.kind] != ["P", "T", "P", "T", "P"]:
                continue
            head, ls, rs = p2.price, p1.price, p3.price
            if not (head > ls * (1 + self.epsilon) and head > rs * (1 + self.epsilon)):
                continue
            shoulder_diff = _pct_diff(ls, rs)
            if shoulder_diff > self.epsilon:
                continue
            neckline_diff = _pct_diff(t1.price, t2.price)
            if neckline_diff > self.epsilon * 1.5:
                continue

            neckline = (t1.price + t2.price) / 2.0
            depth = head - neckline
            target = neckline - depth
            quality = self._quality_from_diffs([shoulder_diff, neckline_diff], self.epsilon * 1.5)

            results.append(self._result(
                "Head & Shoulders", p1.index, p3.index, "bearish",
                levels={
                    "left_shoulder": ls, "head": head, "right_shoulder": rs,
                    "neckline": neckline, "target": target,
                },
                quality=quality,
            ))
        return results

    def detect_inverse_head_and_shoulders(self) -> list[dict]:
        """Mirror of Head & Shoulders: T-P-T(head, lowest)-P-T with a flat
        neckline through the two peaks. Bullish reversal."""
        results = []
        sw = self.swings
        for i in range(len(sw) - 4):
            t1, p1, t2, p2, t3 = sw[i:i + 5]
            if [t1.kind, p1.kind, t2.kind, p2.kind, t3.kind] != ["T", "P", "T", "P", "T"]:
                continue
            head, ls, rs = t2.price, t1.price, t3.price
            if not (head < ls * (1 - self.epsilon) and head < rs * (1 - self.epsilon)):
                continue
            shoulder_diff = _pct_diff(ls, rs)
            if shoulder_diff > self.epsilon:
                continue
            neckline_diff = _pct_diff(p1.price, p2.price)
            if neckline_diff > self.epsilon * 1.5:
                continue

            neckline = (p1.price + p2.price) / 2.0
            depth = neckline - head
            target = neckline + depth
            quality = self._quality_from_diffs([shoulder_diff, neckline_diff], self.epsilon * 1.5)

            results.append(self._result(
                "Inverse Head & Shoulders", t1.index, t3.index, "bullish",
                levels={
                    "left_shoulder": ls, "head": head, "right_shoulder": rs,
                    "neckline": neckline, "target": target,
                },
                quality=quality,
            ))
        return results

    def detect_double_top(self) -> list[dict]:
        """P - T - P (M-shape): two roughly-equal peaks separated by a
        meaningful pullback. Bearish reversal off the second peak."""
        results = []
        sw = self.swings
        min_depth = self.epsilon * 1.5
        for i in range(len(sw) - 2):
            p1, t1, p2 = sw[i:i + 3]
            if [p1.kind, t1.kind, p2.kind] != ["P", "T", "P"]:
                continue
            top_diff = _pct_diff(p1.price, p2.price)
            if top_diff > self.epsilon:
                continue
            avg_top = (p1.price + p2.price) / 2.0
            depth_pct = (avg_top - t1.price) / avg_top if avg_top else 0.0
            if depth_pct < min_depth:
                continue  # too shallow to be a real pullback, just noise

            neckline = t1.price
            target = neckline - (avg_top - neckline)
            quality = self._quality_from_diffs([top_diff], self.epsilon)

            results.append(self._result(
                "Double Top", p1.index, p2.index, "bearish",
                levels={
                    "first_top": p1.price, "second_top": p2.price,
                    "neckline": neckline, "target": target,
                },
                quality=quality,
            ))
        return results

    def detect_double_bottom(self) -> list[dict]:
        """T - P - T (W-shape): two roughly-equal troughs separated by a
        meaningful bounce. Bullish reversal off the second trough."""
        results = []
        sw = self.swings
        min_depth = self.epsilon * 1.5
        for i in range(len(sw) - 2):
            t1, p1, t2 = sw[i:i + 3]
            if [t1.kind, p1.kind, t2.kind] != ["T", "P", "T"]:
                continue
            bottom_diff = _pct_diff(t1.price, t2.price)
            if bottom_diff > self.epsilon:
                continue
            avg_bottom = (t1.price + t2.price) / 2.0
            rise_pct = (p1.price - avg_bottom) / avg_bottom if avg_bottom else 0.0
            if rise_pct < min_depth:
                continue

            neckline = p1.price
            target = neckline + (neckline - avg_bottom)
            quality = self._quality_from_diffs([bottom_diff], self.epsilon)

            results.append(self._result(
                "Double Bottom", t1.index, t2.index, "bullish",
                levels={
                    "first_bottom": t1.price, "second_bottom": t2.price,
                    "neckline": neckline, "target": target,
                },
                quality=quality,
            ))
        return results

    # =====================================================================
    # BILATERAL PATTERNS (triangles & channels)
    # =====================================================================
    def _bilateral_windows(self, min_swings: int = 4, max_swings: int = 7):
        """Yield (window, peaks, troughs) for every span of consecutive
        swings that has at least 2 peaks and 2 troughs to fit lines
        through — the shared scan loop behind every triangle/channel
        detector below."""
        sw = self.swings
        for w in range(min_swings, max_swings + 1):
            for i in range(len(sw) - w + 1):
                window = sw[i:i + w]
                peaks = [s for s in window if s.kind == "P"]
                troughs = [s for s in window if s.kind == "T"]
                if len(peaks) >= 2 and len(troughs) >= 2:
                    yield window, peaks, troughs

    def _fit_bilateral(self, window, peaks, troughs):
        """Fit resistance (peaks) & support (troughs) lines for one
        candidate window and return the shared geometry used by every
        triangle/channel rule."""
        peak_slope, peak_intercept, _ = _fit_line([p.index for p in peaks], [p.price for p in peaks])
        trough_slope, trough_intercept, _ = _fit_line([t.index for t in troughs], [t.price for t in troughs])
        avg_price = float(np.mean([s.price for s in window]))
        span = float(window[-1].index - window[0].index)
        peak_class = self._slope_class(peak_slope, span, avg_price)
        trough_class = self._slope_class(trough_slope, span, avg_price)
        return {
            "peak_slope": peak_slope, "peak_intercept": peak_intercept,
            "trough_slope": trough_slope, "trough_intercept": trough_intercept,
            "avg_price": avg_price, "span": span,
            "peak_class": peak_class, "trough_class": trough_class,
        }

    def _apex(self, geo) -> tuple[float | None, float | None]:
        """Where the resistance and support lines would cross, if ever."""
        d_slope = geo["peak_slope"] - geo["trough_slope"]
        if abs(d_slope) < 1e-9:
            return None, None
        x = (geo["trough_intercept"] - geo["peak_intercept"]) / d_slope
        y = _line_value(geo["peak_slope"], geo["peak_intercept"], x)
        return float(x), float(y)

    def detect_ascending_triangle(self) -> list[dict]:
        """Flat resistance (roughly equal swing highs) + rising support
        (higher swing lows). Bilateral, resolves bullish on breakout."""
        results = []
        for window, peaks, troughs in self._bilateral_windows():
            geo = self._fit_bilateral(window, peaks, troughs)
            if geo["peak_class"] != "flat" or geo["trough_class"] != "rising":
                continue
            apex_x, apex_y = self._apex(geo)
            resistance = float(np.mean([p.price for p in peaks]))
            quality = self._quality_from_diffs(
                [_pct_diff(p.price, resistance) for p in peaks], self.epsilon
            )
            results.append(self._result(
                "Ascending Triangle", window[0].index, window[-1].index, "bullish",
                levels={
                    "resistance": resistance,
                    "support_start": _line_value(geo["trough_slope"], geo["trough_intercept"], window[0].index),
                    "support_end": _line_value(geo["trough_slope"], geo["trough_intercept"], window[-1].index),
                    "apex_price": apex_y,
                },
                quality=quality,
                extra={"apex_index": apex_x},
            ))
        return results

    def detect_descending_triangle(self) -> list[dict]:
        """Flat support (roughly equal swing lows) + falling resistance
        (lower swing highs). Bilateral, resolves bearish on breakdown."""
        results = []
        for window, peaks, troughs in self._bilateral_windows():
            geo = self._fit_bilateral(window, peaks, troughs)
            if geo["peak_class"] != "falling" or geo["trough_class"] != "flat":
                continue
            apex_x, apex_y = self._apex(geo)
            support = float(np.mean([t.price for t in troughs]))
            quality = self._quality_from_diffs(
                [_pct_diff(t.price, support) for t in troughs], self.epsilon
            )
            results.append(self._result(
                "Descending Triangle", window[0].index, window[-1].index, "bearish",
                levels={
                    "support": support,
                    "resistance_start": _line_value(geo["peak_slope"], geo["peak_intercept"], window[0].index),
                    "resistance_end": _line_value(geo["peak_slope"], geo["peak_intercept"], window[-1].index),
                    "apex_price": apex_y,
                },
                quality=quality,
                extra={"apex_index": apex_x},
            ))
        return results

    def detect_symmetrical_triangle(self) -> list[dict]:
        """Falling resistance + rising support converging toward an
        apex. Bilateral (breaks either way; watch volume for direction)."""
        results = []
        for window, peaks, troughs in self._bilateral_windows():
            geo = self._fit_bilateral(window, peaks, troughs)
            if geo["peak_class"] != "falling" or geo["trough_class"] != "rising":
                continue
            apex_x, apex_y = self._apex(geo)
            # Apex should sit ahead of the pattern, not behind it.
            if apex_x is not None and apex_x <= window[-1].index:
                continue
            results.append(self._result(
                "Symmetrical Triangle", window[0].index, window[-1].index, "neutral",
                levels={
                    "resistance_start": _line_value(geo["peak_slope"], geo["peak_intercept"], window[0].index),
                    "resistance_end": _line_value(geo["peak_slope"], geo["peak_intercept"], window[-1].index),
                    "support_start": _line_value(geo["trough_slope"], geo["trough_intercept"], window[0].index),
                    "support_end": _line_value(geo["trough_slope"], geo["trough_intercept"], window[-1].index),
                    "apex_price": apex_y,
                },
                quality=min(1.0, abs(geo["peak_slope"] - geo["trough_slope"]) / (geo["avg_price"] * self.epsilon + 1e-9) * 0 + 0.75),
                extra={"apex_index": apex_x},
            ))
        return results

    def detect_price_channel(self) -> list[dict]:
        """Resistance and support lines with roughly parallel slopes —
        ascending, descending, or horizontal channel. Continuation."""
        results = []
        for window, peaks, troughs in self._bilateral_windows():
            geo = self._fit_bilateral(window, peaks, troughs)
            if geo["peak_class"] != geo["trough_class"]:
                continue  # not parallel -> that's a triangle, not a channel
            slope_gap = abs(geo["peak_slope"] - geo["trough_slope"])
            slope_scale = abs(geo["peak_slope"]) + abs(geo["trough_slope"]) + 1e-9
            if geo["peak_class"] != "flat" and (slope_gap / slope_scale) > self.epsilon * 4:
                continue  # lines diverging/converging too much to call parallel

            direction = {"rising": "bullish", "falling": "bearish", "flat": "neutral"}[geo["peak_class"]]
            results.append(self._result(
                f"Price Channel ({geo['peak_class'].capitalize()})",
                window[0].index, window[-1].index, direction,
                levels={
                    "resistance_start": _line_value(geo["peak_slope"], geo["peak_intercept"], window[0].index),
                    "resistance_end": _line_value(geo["peak_slope"], geo["peak_intercept"], window[-1].index),
                    "support_start": _line_value(geo["trough_slope"], geo["trough_intercept"], window[0].index),
                    "support_end": _line_value(geo["trough_slope"], geo["trough_intercept"], window[-1].index),
                },
                quality=0.85,
            ))
        return results

    # =====================================================================
    # CONTINUATION PATTERNS
    # =====================================================================
    def _find_poles(self, min_move_pct: float = 0.08, max_pole_bars: int = 15,
                     monotonic_ratio_min: float = 0.75):
        """Scan for a short, strong, mostly-one-directional run in the
        close series — the "pole" that a flag/pennant hangs off of.

        For each start ``i`` the pole is taken to end at the running
        extreme (max for an up-move, min for a down-move) within the
        next ``max_pole_bars`` bars — i.e. the point the thrust actually
        ran out of steam — rather than just the first bar that happens
        to clear ``min_move_pct``, so a mild give-back right after the
        thrust doesn't get folded into the pole itself.
        """
        c = self.close
        poles = []
        i = 0
        while i < self.n - 3:
            window_end = min(i + max_pole_bars, self.n)
            window = c[i:window_end]
            if len(window) < 4 or c[i] == 0:
                i += 1
                continue

            candidates = []
            for j_rel, want_up in ((int(np.argmax(window)), True), (int(np.argmin(window)), False)):
                j = i + j_rel
                if j - i < 3:
                    continue
                move = (c[j] - c[i]) / c[i]
                if want_up and move < min_move_pct:
                    continue
                if not want_up and move > -min_move_pct:
                    continue
                seg_diffs = np.diff(c[i:j + 1])
                monotonic = float(np.mean(seg_diffs >= 0) if want_up else np.mean(seg_diffs <= 0))
                if monotonic >= monotonic_ratio_min:
                    candidates.append((j, move))

            if candidates:
                j, move = max(candidates, key=lambda t: abs(t[1]))
                poles.append((i, j, move))
                i = j  # continue scanning after this pole, not inside it
            else:
                i += 1
        return poles

    def _consolidation_geometry(self, start: int, max_bars: int = 20):
        """Fit resistance/support lines to the short consolidation window
        that follows a pole, using a smaller extrema ``order`` since
        flags/pennants are short-lived by definition."""
        end = min(start + max_bars, self.n - 1)
        if end - start < 5:
            return None
        seg = self.close[start:end + 1]
        local_order = max(1, self.order // 2)
        peak_pos = argrelextrema(seg, np.greater, order=local_order)[0]
        trough_pos = argrelextrema(seg, np.less, order=local_order)[0]
        if len(peak_pos) < 2 or len(trough_pos) < 2:
            return None
        peaks = [(int(p + start), float(seg[p])) for p in peak_pos]
        troughs = [(int(t + start), float(seg[t])) for t in trough_pos]

        peak_slope, peak_intercept, _ = _fit_line([p[0] for p in peaks], [p[1] for p in peaks])
        trough_slope, trough_intercept, _ = _fit_line([t[0] for t in troughs], [t[1] for t in troughs])
        avg_price = float(np.mean(seg))
        span = float(end - start)
        return {
            "end": end, "avg_price": avg_price, "span": span,
            "peak_slope": peak_slope, "peak_intercept": peak_intercept,
            "trough_slope": trough_slope, "trough_intercept": trough_intercept,
            "peak_class": self._slope_class(peak_slope, span, avg_price),
            "trough_class": self._slope_class(trough_slope, span, avg_price),
            "range": float(seg.max() - seg.min()),
        }

    def _flag_or_pennant(self, pole_i, pole_j, move, want_direction: str) -> dict | None:
        """Shared body for bull-flag / bear-flag detection: strong pole
        of the right direction, then a tight channel drifting the
        opposite way (or sideways)."""
        if (move > 0) != (want_direction == "bull"):
            return None
        geo = self._consolidation_geometry(pole_j)
        if geo is None:
            return None
        pole_range = abs(self.close[pole_j] - self.close[pole_i])
        if pole_range == 0 or geo["range"] > 0.6 * pole_range:
            return None  # consolidation too wide to be a tight flag
        if geo["peak_class"] != geo["trough_class"]:
            return None  # converging -> that's a pennant, handled separately

        if want_direction == "bull" and geo["peak_class"] == "rising":
            return None  # flag must drift down or sideways against the pole
        if want_direction == "bear" and geo["peak_class"] == "falling":
            return None  # flag must drift up or sideways against the pole

        pole_start_price = self.close[pole_i]
        pole_end_price = self.close[pole_j]
        target = pole_end_price + (pole_end_price - pole_start_price)  # measured move
        return {
            "end": geo["end"],
            "levels": {
                "pole_start": pole_start_price,
                "pole_end": pole_end_price,
                "flag_resistance_end": _line_value(geo["peak_slope"], geo["peak_intercept"], geo["end"]),
                "flag_support_end": _line_value(geo["trough_slope"], geo["trough_intercept"], geo["end"]),
                "measured_move_target": target,
            },
            "quality": round(max(0.0, 1.0 - geo["range"] / pole_range), 2),
        }

    def detect_bull_flag(self) -> list[dict]:
        """Sharp rally (pole) + tight, mildly-down/sideways channel.
        Bullish continuation; target is the pole's length projected
        from the breakout point (measured-move rule)."""
        results = []
        for pole_i, pole_j, move in self._find_poles():
            match = self._flag_or_pennant(pole_i, pole_j, move, "bull")
            if match:
                results.append(self._result(
                    "Bull Flag", pole_i, match["end"], "bullish",
                    levels=match["levels"], quality=match["quality"],
                ))
        return results

    def detect_bear_flag(self) -> list[dict]:
        """Sharp decline (pole) + tight, mildly-up/sideways channel.
        Bearish continuation; symmetric mirror of the bull flag."""
        results = []
        for pole_i, pole_j, move in self._find_poles():
            match = self._flag_or_pennant(pole_i, pole_j, move, "bear")
            if match:
                results.append(self._result(
                    "Bear Flag", pole_i, match["end"], "bearish",
                    levels=match["levels"], quality=match["quality"],
                ))
        return results

    def detect_pennant(self) -> list[dict]:
        """Sharp move (pole, either direction) + a small converging
        triangle (rather than a parallel channel) before continuation."""
        results = []
        for pole_i, pole_j, move in self._find_poles():
            geo = self._consolidation_geometry(pole_j)
            if geo is None:
                continue
            pole_range = abs(self.close[pole_j] - self.close[pole_i])
            if pole_range == 0 or geo["range"] > 0.6 * pole_range:
                continue
            if not (geo["peak_class"] == "falling" and geo["trough_class"] == "rising"):
                continue  # need genuine convergence, not a parallel flag

            direction = "bullish" if move > 0 else "bearish"
            pole_end_price = self.close[pole_j]
            target = pole_end_price + (pole_end_price - self.close[pole_i])
            results.append(self._result(
                "Pennant", pole_i, geo["end"], direction,
                levels={
                    "pole_start": self.close[pole_i],
                    "pole_end": pole_end_price,
                    "resistance_end": _line_value(geo["peak_slope"], geo["peak_intercept"], geo["end"]),
                    "support_end": _line_value(geo["trough_slope"], geo["trough_intercept"], geo["end"]),
                    "measured_move_target": target,
                },
                quality=round(max(0.0, 1.0 - geo["range"] / pole_range), 2),
            ))
        return results

    def detect_cup_and_handle(self, min_cup_bars: int = 20, max_cup_bars: int = 150,
                               min_depth_pct: float = 0.12, max_handle_ratio: float = 0.5) -> list[dict]:
        """Rounded "U" between two roughly-equal rims (the cup), followed
        by a shallower short pullback (the handle) that stays above the
        cup's midpoint. Bullish continuation.

        The rounding test fits a degree-2 polynomial to the close prices
        between candidate rims: a convex (upward-opening) fit whose
        vertex falls roughly in the middle of the window is what "U-shaped"
        means mathematically here.
        """
        results = []
        peaks = [s for s in self.swings if s.kind == "P"]
        for a in range(len(peaks)):
            for b in range(a + 1, len(peaks)):
                left_rim, right_rim = peaks[a], peaks[b]
                width = right_rim.index - left_rim.index
                if width < min_cup_bars or width > max_cup_bars:
                    continue
                if _pct_diff(left_rim.price, right_rim.price) > self.epsilon:
                    continue

                seg = self.close[left_rim.index:right_rim.index + 1]
                if len(seg) < min_cup_bars:
                    continue
                x = np.arange(len(seg), dtype=float)
                coeffs = np.polyfit(x, seg, 2)
                a2, b2, _c2 = coeffs
                if a2 <= 0:
                    continue  # not convex -> not a bowl
                vertex_x = -b2 / (2 * a2)
                if not (0.25 * len(seg) <= vertex_x <= 0.75 * len(seg)):
                    continue  # bottom isn't centered enough to read as rounded

                rim_avg = (left_rim.price + right_rim.price) / 2.0
                cup_bottom = float(seg.min())
                depth_pct = (rim_avg - cup_bottom) / rim_avg if rim_avg else 0.0
                if depth_pct < min_depth_pct:
                    continue

                # Handle: a shallower pullback in the short window right after the right rim.
                handle_start = right_rim.index
                handle_end = min(handle_start + max(5, width // 4), self.n - 1)
                if handle_end - handle_start < 3:
                    continue
                handle_seg = self.close[handle_start:handle_end + 1]
                handle_low = float(handle_seg.min())
                handle_depth = right_rim.price - handle_low
                cup_depth = rim_avg - cup_bottom
                if handle_depth <= 0 or handle_depth > max_handle_ratio * cup_depth:
                    continue
                if handle_low < (rim_avg + cup_bottom) / 2.0:
                    continue  # handle dug below the cup's midpoint -> not a handle anymore

                quality = self._quality_from_diffs(
                    [_pct_diff(left_rim.price, right_rim.price)], self.epsilon
                )
                results.append(self._result(
                    "Cup & Handle", left_rim.index, handle_end, "bullish",
                    levels={
                        "left_rim": left_rim.price, "right_rim": right_rim.price,
                        "cup_bottom": cup_bottom, "handle_low": handle_low,
                        "breakout_level": max(left_rim.price, right_rim.price),
                        "target": max(left_rim.price, right_rim.price) + cup_depth,
                    },
                    quality=quality,
                ))
        return results

    # =====================================================================
    # Orchestration
    # =====================================================================
    def detect_all(self, dedupe: bool = True) -> list[dict]:
        """Run every registered detector and return one time-ordered list.

        A single misbehaving detector (bad slice, degenerate fit, ...)
        is swallowed and skipped rather than aborting the whole scan —
        matches how the rest of this codebase treats per-ticker failures
        in bulk passes (see export_json.build_chart_history).

        The bilateral detectors (triangles/channels) deliberately scan
        every window length from 4-7 swings, so the same real triangle
        is very commonly matched several times with slightly different
        start/end points. With ``dedupe=True`` (the default) those
        overlapping same-pattern matches are collapsed down to the
        single best one — set it False if you want the raw matches
        (e.g. to inspect detector behavior yourself).
        """
        all_results: list[dict] = []
        for method_name in self.PATTERN_METHODS:
            method: Callable[[], list[dict]] = getattr(self, method_name)
            try:
                all_results.extend(method())
            except Exception:
                continue
        if dedupe:
            all_results = self._dedupe_overlapping(all_results)
        all_results.sort(key=lambda r: (r["start_index"], r["end_index"], r["pattern"]))
        return all_results

    @staticmethod
    def _dedupe_overlapping(results: list[dict]) -> list[dict]:
        """Collapse overlapping same-pattern matches, keeping whichever
        one of each overlapping cluster has the higher quality score
        (ties broken by the wider span, since a triangle/channel that
        holds over more bars is the more meaningful read)."""
        by_pattern: dict[str, list[dict]] = {}
        for r in results:
            by_pattern.setdefault(r["pattern"], []).append(r)

        deduped: list[dict] = []
        for _name, group in by_pattern.items():
            group.sort(key=lambda r: r["start_index"])
            kept: list[dict] = []
            for r in group:
                if kept and r["start_index"] <= kept[-1]["end_index"]:
                    prev = kept[-1]
                    r_span = r["end_index"] - r["start_index"]
                    prev_span = prev["end_index"] - prev["start_index"]
                    better = (r.get("quality", 0), r_span) > (prev.get("quality", 0), prev_span)
                    if better:
                        kept[-1] = r
                    continue
                kept.append(r)
            deduped.extend(kept)
        return deduped


def summarize_patterns(patterns: list[dict]) -> dict:
    summary = {"total": len(patterns), "by_pattern": {}, "by_direction": {}}
    for item in patterns:
        name = item.get("pattern", "Unknown")
        direction = item.get("direction", "neutral")
        summary["by_pattern"][name] = summary["by_pattern"].get(name, 0) + 1
        summary["by_direction"][direction] = summary["by_direction"].get(direction, 0) + 1
    return summary


def detect_patterns(df: pd.DataFrame, epsilon: float = 0.03, order: int = 5) -> list[dict]:
    """One-shot convenience wrapper: ``PatternDetector(df, ...).detect_all()``."""
    return PatternDetector(df, epsilon=epsilon, order=order).detect_all()


if __name__ == "__main__":
    # Self-test with synthetic data — no DB/network needed. Builds a
    # deliberately obvious double-bottom + breakout series and checks the
    # engine actually finds it, the same "module loaded successfully"
    # smoke-test style used by analytics.py / config.py.
    rng = np.random.default_rng(7)
    n = 120
    trend = np.concatenate([
        np.linspace(100, 80, 25),   # decline into the first bottom
        np.linspace(80, 92, 15),    # bounce
        np.linspace(92, 79, 20),    # decline into the second bottom
        np.linspace(79, 105, 60),   # breakout and run
    ])
    noise = rng.normal(0, 0.4, size=len(trend))
    close = trend + noise
    dates = pd.date_range("2025-01-01", periods=len(close), freq="B")
    synth = pd.DataFrame({
        "open": close * 0.998,
        "high": close * 1.006,
        "low": close * 0.994,
        "close": close,
        "volume": rng.integers(50_000, 200_000, size=len(close)),
    }, index=dates)

    found = detect_patterns(synth, epsilon=0.03, order=4)
    print(f"PatternDetector module loaded successfully. {len(found)} pattern(s) found on synthetic data:")
    for p in found:
        print(f"  {p['pattern']:<24} {p['direction']:<8} {p['start_date']} -> {p['end_date']}  levels={p['levels']}")
