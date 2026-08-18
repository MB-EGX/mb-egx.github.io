"""
usd_divergence.py
==================
Structural-peak divergence detector between EGX30 in local currency
(config.PRIMARY_BENCHMARK_TICKER, ".EGX30") and its dollar-denominated
twin (".EGX30USD"). Both track the same 30 companies - the only thing
that differs between them is the currency the index is expressed in -
so any divergence between their price STRUCTURE (not just their level)
isolates the EGP/USD devaluation effect from real equity performance.

WHY THIS MATTERS FOR AN EGP-ONLY TRADER: a stock (or the index) can
print a strong uptrend in EGP purely because the pound is losing value,
not because the underlying business or market is actually stronger. The
EGP tape alone can't tell those two apart. The USD twin can, because
currency devaluation cancels out of a dollar-denominated series by
construction.

CLASSIC BEARISH DIVERGENCE: EGX30 (EGP) makes a higher high while
EGX30 (USD) makes a LOWER high over the same two peaks -> the EGP rally
is devaluation-driven, not real appreciation; foreign (USD-measured)
capital is not confirming the move.

CLASSIC BULLISH DIVERGENCE (rarer): EGX30 (EGP) makes a lower high
while EGX30 (USD) makes a HIGHER high -> local-currency weakness is
masking real underlying strength.

METHOD: scipy.signal.argrelextrema on each series' close (same tool
chart_patterns.py already uses elsewhere in this codebase - not a new
dependency), comparing the slope between each series' own last two
local maxima. ``config.USD_DIVERGENCE_PEAK_ORDER`` sets how many bars on
each side must be lower for a bar to count as a peak (larger = fewer,
more structurally significant peaks; smaller = more, noisier peaks).

Both legs are aligned on shared trading dates first (inner join) - a
peak is only compared when both series actually have a bar on that
date, exactly like sector_rotation.py's RS alignment.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.signal import argrelextrema

from config import (
    PRIMARY_BENCHMARK_TICKER,
    USD_DIVERGENCE_PEAK_ORDER,
    USD_DIVERGENCE_MIN_BARS,
    get_logger,
)
from market_regime import benchmark_label

logger = get_logger("usd_divergence")

_USD_TICKER = ".EGX30USD"


def _aligned_closes(frame_egp: pd.DataFrame, frame_usd: pd.DataFrame) -> pd.DataFrame | None:
    if frame_egp is None or frame_egp.empty or frame_usd is None or frame_usd.empty:
        return None
    if "close" not in frame_egp.columns or "close" not in frame_usd.columns:
        return None
    a = frame_egp["close"].rename("egp_close")
    b = frame_usd["close"].rename("usd_close")
    joined = pd.concat([a, b], axis=1, join="inner").dropna()
    return joined if not joined.empty else None


def detect_divergence(frame_egp: pd.DataFrame, frame_usd: pd.DataFrame,
                       order: int = USD_DIVERGENCE_PEAK_ORDER) -> dict:
    """Returns a dict describing the current divergence state between
    the two legs' last two local peaks:

        {
          "available": true,
          "divergence": "bearish" | "bullish" | "none",
          "egp_peaks": [{"date": "...", "close": 30412.5}, {"date": "...", "close": 31800.0}],
          "usd_peaks": [{"date": "...", "close": 985.2}, {"date": "...", "close": 970.1}],
          "note": "..."
        }

    "available": False (with a "reason") if either leg isn't ingested
    yet, there isn't enough shared history (config.
    USD_DIVERGENCE_MIN_BARS), or either leg doesn't yet have 2 local
    peaks to compare - never a guessed/fabricated verdict.
    """
    joined = _aligned_closes(frame_egp, frame_usd)
    if joined is None:
        return {"available": False, "reason": "EGX30 (EGP) and/or EGX30 (USD) not yet ingested."}
    if len(joined) < USD_DIVERGENCE_MIN_BARS:
        return {"available": False, "reason": f"Only {len(joined)} shared session(s) - need at least {USD_DIVERGENCE_MIN_BARS}."}

    egp = joined["egp_close"].values
    usd = joined["usd_close"].values
    dates = joined.index.astype(str).str[:10].values

    peaks_egp = argrelextrema(egp, np.greater_equal, order=order)[0]
    peaks_usd = argrelextrema(usd, np.greater_equal, order=order)[0]

    if len(peaks_egp) < 2 or len(peaks_usd) < 2:
        return {"available": False, "reason": "Not enough structural peaks yet in one or both legs."}

    last2_egp = peaks_egp[-2:]
    last2_usd = peaks_usd[-2:]

    egp_slope = float(egp[last2_egp[1]] - egp[last2_egp[0]])
    usd_slope = float(usd[last2_usd[1]] - usd[last2_usd[0]])

    if egp_slope > 0 and usd_slope < 0:
        divergence = "bearish"
        note = "EGX30 (EGP) is making higher highs while EGX30 (USD) is making lower highs - the EGP rally looks devaluation-driven, not confirmed by dollar-measured performance."
    elif egp_slope < 0 and usd_slope > 0:
        divergence = "bullish"
        note = "EGX30 (EGP) is making lower highs while EGX30 (USD) is making higher highs - local-currency weakness may be masking real underlying strength."
    else:
        divergence = "none"
        note = "EGP and USD structural highs are moving in the same direction - no currency-driven divergence detected right now."

    return {
        "available": True,
        "divergence": divergence,
        "egp_peaks": [
            {"date": str(dates[i]), "close": round(float(egp[i]), 4)} for i in last2_egp
        ],
        "usd_peaks": [
            {"date": str(dates[i]), "close": round(float(usd[i]), 4)} for i in last2_usd
        ],
        "note": note,
    }


def live_divergence_snapshot(benchmark_indicator_frames: dict, dbm) -> dict:
    """Live wrapper for decision_matrix.analyze_market() - same call
    shape as sector_rotation.live_rotation_snapshot() and market_regime.
    live_regime_snapshot(), reading from the same already-fetched
    {normalized_ticker: indicator_frame} dict (no extra DB round trip).
    """
    egp_norm = dbm.normalize_symbol(PRIMARY_BENCHMARK_TICKER)
    usd_norm = dbm.normalize_symbol(_USD_TICKER)
    frame_egp = benchmark_indicator_frames.get(egp_norm)
    frame_usd = benchmark_indicator_frames.get(usd_norm)

    if frame_egp is None or frame_usd is None:
        missing = []
        if frame_egp is None:
            missing.append(benchmark_label(PRIMARY_BENCHMARK_TICKER))
        if frame_usd is None:
            missing.append(benchmark_label(_USD_TICKER))
        return {"available": False, "reason": f"Not yet ingested: {', '.join(missing)}"}

    return detect_divergence(frame_egp, frame_usd)
