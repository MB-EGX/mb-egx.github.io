"""
sector_rotation.py
===================
Relative-strength ROTATION signal between the two EGX sector sub-indices
in config.SECTOR_ROTATION_PAIR: EGX IMCS (".EGIMCS" - tech/telecom/
fintech: Telecom Egypt, Fawry, Raya, ...) and EGX Text Double (".EGTEDU"
- textiles/spinning/export manufacturing). These aren't an arbitrary
pair - they track a real, distinct macro divergence in this market:
domestic digital demand (IMCS) vs EGP-devaluation-driven export
competitiveness (Text Double). See config.SECTOR_ROTATION_* for the
tunables this module reads.

THE RATIO, DEFINED CAREFULLY:
    RS_t = Close(IMCS, t) / Close(Text Double, t)

    RS rising  -> IMCS (tech) outperforming Text Double -> favor tech.
    RS falling -> Text Double (export manufacturing) outperforming IMCS
                  -> favor export manufacturers.

    (Numerator is IMCS, not Text Double - defined this way specifically
    so the ratio's direction matches its own plain-English description
    above with no sign flip needed anywhere else in this module. A
    Text-Double-over-IMCS ratio would describe the opposite rotation
    while reading identically at a glance - this ordering removes that
    footgun.)

SIGNAL RULE - fast/slow SMA crossover on the RS line itself (same
"smooth the ratio, cross the ratio" approach used for VWAP/SMA
crossovers elsewhere in this app - see analytics.compute_indicators):
    fast_sma(RS, SECTOR_ROTATION_FAST_SMA) > slow_sma(RS, SECTOR_ROTATION_SLOW_SMA)
        -> signal = "imcs"   (rotate toward tech/telecom)
    fast_sma < slow_sma
        -> signal = "text_double"   (rotate toward export manufacturing)

POINT-IN-TIME DISCIPLINE: the signal for bar t is shifted forward one
bar before being read (same causal contract as market_regime.
build_regime_map - "don't trade on today's own close") - the live
snapshot below reads the signal computed as of the PRIOR bar, applied
to today.

Both legs are aligned on their shared trading dates (inner join) before
any of this runs - a session where only one of the two indices printed
a bar contributes nothing to the ratio, rather than silently
forward-filling a stale close for the missing leg.
"""
from __future__ import annotations

import pandas as pd

from config import (
    SECTOR_ROTATION_PAIR,
    SECTOR_ROTATION_FAST_SMA,
    SECTOR_ROTATION_SLOW_SMA,
    SECTOR_ROTATION_MIN_BARS,
    get_logger,
)
from market_regime import benchmark_label

logger = get_logger("sector_rotation")

_IMCS_TICKER, _TEXT_DOUBLE_TICKER = SECTOR_ROTATION_PAIR


def _aligned_closes(frame_imcs: pd.DataFrame, frame_text: pd.DataFrame) -> pd.DataFrame | None:
    """Inner-joins the two legs' 'close' columns on shared dates - RS is
    only meaningful on sessions where BOTH indices actually printed a
    bar. Returns None if either leg is missing/empty (not yet ingested
    - see module docstring's "missing means unavailable" contract,
    same as market_regime.py)."""
    if frame_imcs is None or frame_imcs.empty or frame_text is None or frame_text.empty:
        return None
    if "close" not in frame_imcs.columns or "close" not in frame_text.columns:
        return None
    a = frame_imcs["close"].rename("imcs_close")
    b = frame_text["close"].rename("text_close")
    joined = pd.concat([a, b], axis=1, join="inner").dropna()
    return joined if not joined.empty else None


def compute_rotation_series(frame_imcs: pd.DataFrame, frame_text: pd.DataFrame,
                             fast: int = SECTOR_ROTATION_FAST_SMA,
                             slow: int = SECTOR_ROTATION_SLOW_SMA) -> pd.DataFrame | None:
    """Whole-history RS ratio + fast/slow SMA + causal (t-1 shifted)
    signal column, for anyone who wants the full series (e.g. a future
    chart) rather than just today's snapshot. Returns None if there
    isn't enough shared history yet (config.SECTOR_ROTATION_MIN_BARS).

    Columns: imcs_close, text_close, rs_ratio, rs_fast_sma, rs_slow_sma,
    signal ("imcs" | "text_double" | None while warming up).
    """
    joined = _aligned_closes(frame_imcs, frame_text)
    if joined is None or len(joined) < SECTOR_ROTATION_MIN_BARS:
        return None

    df = joined.copy()
    df["rs_ratio"] = df["imcs_close"] / df["text_close"]
    df["rs_fast_sma"] = df["rs_ratio"].rolling(window=fast).mean()
    df["rs_slow_sma"] = df["rs_ratio"].rolling(window=slow).mean()

    raw_signal = pd.Series(None, index=df.index, dtype=object)
    raw_signal[df["rs_fast_sma"] > df["rs_slow_sma"]] = "imcs"
    raw_signal[df["rs_fast_sma"] < df["rs_slow_sma"]] = "text_double"
    # Causal: today's ACTIONABLE signal is yesterday's crossover state,
    # never today's own (same-session) close - mirrors market_regime.py's
    # point-in-time discipline.
    df["signal"] = raw_signal.shift(1)
    return df


def live_rotation_snapshot(benchmark_indicator_frames: dict, dbm) -> dict:
    """Live, single-latest-bar rotation snapshot for decision_matrix.
    analyze_market() to fold into market_regime_summary, exactly the way
    market_regime.live_regime_snapshot() does for bull/bear/neutral
    regime. ``benchmark_indicator_frames`` is the same
    {normalized_ticker: indicator_frame} dict decision_matrix already
    builds via market_regime.load_all_benchmark_indicators() - no extra
    DB round trip.

        {
          "available": true,
          "signal": "imcs",              # or "text_double"
          "favored_ticker": ".EGIMCS",
          "favored_label": "EGX IMCS",
          "rs_ratio": 1.0842,
          "rs_fast_sma": 1.079,
          "rs_slow_sma": 1.061,
          "as_of": "2026-08-17",
        }

    Returns {"available": False, "reason": "..."} if either leg isn't
    ingested yet or there isn't enough shared history - never a
    fabricated signal (same contract as every other benchmark feature
    in this app).
    """
    imcs_norm = dbm.normalize_symbol(_IMCS_TICKER)
    text_norm = dbm.normalize_symbol(_TEXT_DOUBLE_TICKER)
    frame_imcs = benchmark_indicator_frames.get(imcs_norm)
    frame_text = benchmark_indicator_frames.get(text_norm)

    if frame_imcs is None or frame_text is None:
        missing = []
        if frame_imcs is None:
            missing.append(benchmark_label(_IMCS_TICKER))
        if frame_text is None:
            missing.append(benchmark_label(_TEXT_DOUBLE_TICKER))
        return {"available": False, "reason": f"Not yet ingested: {', '.join(missing)}"}

    series = compute_rotation_series(frame_imcs, frame_text)
    if series is None:
        return {"available": False, "reason": "Insufficient shared history for both legs yet."}

    last_valid = series.dropna(subset=["signal"])
    if last_valid.empty:
        return {"available": False, "reason": "Warming up - not enough bars past the slow SMA window yet."}

    last = last_valid.iloc[-1]
    as_of = str(last_valid.index[-1])[:10]
    signal = last["signal"]
    favored_ticker = _IMCS_TICKER if signal == "imcs" else _TEXT_DOUBLE_TICKER
    return {
        "available": True,
        "signal": signal,
        "favored_ticker": favored_ticker,
        "favored_label": benchmark_label(favored_ticker),
        "rs_ratio": round(float(last["rs_ratio"]), 4),
        "rs_fast_sma": round(float(last["rs_fast_sma"]), 4),
        "rs_slow_sma": round(float(last["rs_slow_sma"]), 4),
        "as_of": as_of,
    }
