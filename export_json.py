"""
export_json.py
==============
CLI utility that runs the full decision matrix and dumps everything
(buy recs, top-10 by category, closed trades, sector heatmap, financial
statement, plus 1 year of OHLC + VWAP history per ticker) into a single
``web_public/data/market_data.json`` file for a browser dashboard.

CHANGELOG vs the original:
  * Output path is anchored to the script's own directory (was CWD-relative).
  * The 365-day chart slice is now pushed into DuckDB via
    ``qe.get_all_market_data_bulk(days=CHART_HISTORY_DAYS)`` instead of
    pulling every bar of every ticker just to tail-trim in pandas.
  * PRIVACY FIX: cash_balance, financial_statement, and closed_trades are
    no longer written to the public JSON at all. These are real personal
    account figures (actual cash, actual realized/unrealized P&L, actual
    trade history) and the web dashboard's own JS never reads any of
    these three fields anyway — the site computes each visitor's own
    portfolio view from their private per-user Firestore/localStorage
    data instead. Publishing them here served no purpose except exposing
    real account data on a public, unauthenticated static file.
  * PRIVACY FIX: "Suggested Shares (1% Risk)" is stripped from every row
    of market_matrix / top_10 before export. That figure is computed as
    cash_balance * RISK_PER_TRADE_PCT / entry_price, so publishing it
    lets anyone back-calculate the exact real cash_balance from a single
    row even with the cash_balance field itself removed.
  * Each stock's chart_history entry now also carries a "patterns" list
    (chart_patterns.PatternDetector, quality-filtered via
    config.PATTERN_DETECTION) - the same geometric pattern overlay the
    desktop app's chart "Patterns" toggle draws, so the web dashboard can
    render identical overlays straight from data already in this payload.
"""
from __future__ import annotations

import json
import math
import os

import numpy as np

# MUST be imported before pandas and before decision_matrix/db_manager
# (below) - config.py sets OPENBLAS/MKL/OMP/NUMEXPR thread caps as a
# module-level side effect, which only takes effect if set before
# numpy/pandas load anywhere in this process.
from config import CHART_HISTORY_DAYS, PATTERN_DETECTION

import pandas as pd

from decision_matrix import DecisionMatrix
from db_manager import DatabaseManager
from chart_patterns import PatternDetector

# Any of these keys, if present on a market_matrix/top_10 row, are derived
# from real private account data and must never reach the public JSON.
#   - "Suggested Shares (1% Risk)": derived from the real cash balance.
#   - "Position": reveals which tickers are actually owned (added so owned
#     positions can also be re-scored as scale-in candidates in the Action
#     Matrix) - that's account holdings info, same trust boundary as
#     cash_balance/closed_trades/portfolio_risk above.
_PRIVATE_ROW_KEYS = ("Suggested Shares (1% Risk)", "Position")


def _strip_private_row_fields(rows):
    """Remove cash-balance-derived fields from a list of signal-row dicts."""
    for row in rows:
        for key in _PRIVATE_ROW_KEYS:
            row.pop(key, None)
    return rows


def sanitize_for_json(obj):
    """Recursively convert NaN/Infinity to None and numpy scalars to native
    Python types.

    BUGFIX: pandas aggregations (.sum(), .count(), comparisons, etc.) very
    commonly hand back numpy.int64 / numpy.float64 / numpy.bool_ instead of
    plain Python types. json.dump() does not know how to serialize those
    ("Object of type int64 is not JSON serializable") and the previous
    version here only special-cased `float`, so any such value reaching
    the payload silently killed the entire nightly export.
    """
    if isinstance(obj, (np.floating,)):
        f = float(obj)
        return None if (math.isnan(f) or math.isinf(f)) else f
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        return sanitize_for_json(obj.tolist())
    if isinstance(obj, dict):
        return {k: sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize_for_json(v) for v in obj]
    return obj


def _detect_chart_patterns(df_ind: pd.DataFrame) -> list:
    """Geometric chart patterns (chart_patterns.PatternDetector) for one
    stock's indicator-enriched history, filtered to PATTERN_DETECTION's
    quality bar before they ever reach the public JSON. Mirrors the same
    config-driven behavior as the desktop chart's "Patterns" toggle so
    the web dashboard and the desktop app never disagree about what
    counts as a valid match.
    """
    if len(df_ind) < PATTERN_DETECTION["min_bars_required"]:
        return []
    try:
        found = PatternDetector(
            df_ind,
            epsilon=PATTERN_DETECTION["epsilon"],
            order=PATTERN_DETECTION["order"],
        ).detect_all()
    except Exception:
        # One ticker's bad fit should never take down the whole nightly
        # export - same "skip and continue" posture used everywhere else
        # in this file's per-ticker loop.
        return []
    min_quality = PATTERN_DETECTION["min_quality"]
    return [p for p in found if p.get("quality", 1.0) >= min_quality]


def build_chart_history(qe, dbm, sector_map):
    """Up to ``CHART_HISTORY_DAYS`` of close + VWAP per stock & sector index."""
    chart_history = {"stocks": {}, "sectors": {}}

    # 1. Stock history (date filter pushed to SQL — see analytics.get_all_market_data_bulk)
    bulk_data = qe.get_all_market_data_bulk(days=CHART_HISTORY_DAYS)
    for ticker, df in bulk_data.items():
        if df.empty or len(df) < 2:
            continue
        norm_sym = dbm.normalize_symbol(ticker)
        try:
            df_ind = qe.compute_indicators(df).tail(CHART_HISTORY_DAYS)
            dates = [str(d).split("T")[0] for d in df_ind.index]

            def _round_col(col_name, fallback_col="close"):
                series = df_ind[col_name] if col_name in df_ind.columns else df_ind[fallback_col]
                return [round(float(v), 4) if pd.notna(v) else None for v in series]

            closes = _round_col("close")
            opens = _round_col("open")
            highs = _round_col("high")
            lows = _round_col("low")
            vwaps = [
                round(float(v), 4)
                if "vwap_20" in df_ind.columns and pd.notna(v)
                else None
                for v in df_ind.get("vwap_20", df_ind["close"])
            ]
            # Resistance/support reference lines for the chart, matching the
            # same 250-day range the matrix table's "Resistance (52W High)" /
            # "Support (52W Low)" columns use, so the chart and the table
            # never disagree about where those levels sit.
            lookback = min(250, len(df_ind))
            resistance = round(float(df_ind["high"].iloc[-lookback:].max()), 4)
            support = round(float(df_ind["low"].iloc[-lookback:].min()), 4)
            pivots = qe.compute_pivot_points(df_ind)
            chart_history["stocks"][norm_sym] = {
                "dates": dates,
                "close": closes,
                "open": opens,
                "high": highs,
                "low": lows,
                "vwap": vwaps,
                "resistance": resistance,
                "support": support,
                "pivots": pivots,
                "patterns": _detect_chart_patterns(df_ind),
            }
        except Exception:
            continue

    # 2. Sector macro indices
    unique_sectors = sorted(list(set(sector_map.values())))
    for sec_name in unique_sectors:
        try:
            df_sec = qe.get_sector_historical_index(sec_name, sector_map)
            if not df_sec.empty:
                df_sec = df_sec.tail(CHART_HISTORY_DAYS)
                dates = [str(d).split("T")[0] for d in df_sec.index]
                idx_vals = [
                    round(float(val), 2) if pd.notna(val) else None
                    for val in df_sec["sector_index"]
                ]
                chart_history["sectors"][sec_name] = {
                    "dates": dates,
                    "close": idx_vals,
                }
        except Exception:
            continue

    return chart_history


def export_market_matrix():
    print("🧠 Running core decision matrix & sector analytics...")
    matrix = DecisionMatrix()
    dbm = DatabaseManager()
    sector_map = dbm.get_sector_map()

    # PRIVACY: portfolio_risk (position sizes, sector/ticker concentration %)
    # is derived from real account holdings, same trust boundary as
    # cash_balance/financial_statement/closed_trades above — intentionally
    # not unpacked into a used variable so it can't accidentally end up in
    # the public payload below. The desktop app (app_gui.py), which reads
    # its own private local DB, is the correct place to display this.
    buys, exits, top10, closed_trades, fin_stmt, sectors, breakout_watchlist, _portfolio_risk, session_picks = matrix.analyze_market()
    last_data_date = dbm.get_latest_market_date()

    # Overwrite with the FULL set of picks achieved on this session date,
    # not just whatever this one analyze_market() call happened to detect.
    # If publish.py runs more than once on the same trading day, each run
    # only re-checks currently-ACTIVE picks (an already-achieved pick is
    # skipped), so a later run's own "achieved_today" would only contain
    # that run's newly-crossed picks. post_state.py/social_poster.py only
    # ever read this field from market_data.json (they're deliberately
    # decoupled from the local DuckDB — see social_poster.py's docstring),
    # so it needs to be the complete day's list every time or an earlier
    # same-day achievement could be missed by the achievement post.
    session_picks["achieved_today"] = dbm.get_achievements_for_date(last_data_date)

    # PRIVACY: strip the cash-derived "Suggested Shares (1% Risk)" column
    # from every row before it can reach the public JSON.
    _strip_private_row_fields(buys)
    for rows in top10.values():
        _strip_private_row_fields(rows)

    print(
        f"📊 Extracting up to {CHART_HISTORY_DAYS} days of historical chart data "
        "from DuckDB..."
    )
    chart_history = build_chart_history(matrix.qe, dbm, sector_map)

    payload = {
        "last_data_date": last_data_date,
        "market_matrix": buys,
        "sectors": sectors,
        "top_10": top10,
        "breakout_watchlist": breakout_watchlist,
        # Public, non-sensitive (see session_picks.py) - the forward-looking
        # watchlist tab's current state, including any picks that crossed
        # +3% on THIS run ("achieved_today"). post_state.py/social_poster.py
        # read achieved_today off this same field to decide whether an
        # "achievement" post is due.
        "session_picks": session_picks,
        "chart_history": chart_history,
        # Public, non-sensitive (sector classification, not account data) -
        # lets the web client compute its OWN portfolio concentration risk
        # from its own privately-stored positions, without the server ever
        # seeing position sizes. See portfolio_risk privacy note above.
        "ticker_sectors": sector_map,
    }

    print("🧹 Sanitizing data payload (removing NaN / Infinity)...")
    clean_payload = sanitize_for_json(payload)

    # Anchor the output to the script's own directory, not CWD
    output_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "web_public", "data"
    )
    os.makedirs(output_dir, exist_ok=True)

    file_path = os.path.join(output_dir, "market_data.json")
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(clean_payload, f, ensure_ascii=False, indent=2)

    print(
        f"✅ Successfully exported {CHART_HISTORY_DAYS}-day market matrix & "
        f"charts to {file_path}"
    )


if __name__ == "__main__":
    export_market_matrix()
