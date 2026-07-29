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
"""
from __future__ import annotations

import json
import math
import os

import pandas as pd

from decision_matrix import DecisionMatrix
from db_manager import DatabaseManager
from config import CHART_HISTORY_DAYS

# Any of these keys, if present on a market_matrix/top_10 row, are derived
# from the real cash balance and must never reach the public JSON.
_PRIVATE_ROW_KEYS = ("Suggested Shares (1% Risk)",)


def _strip_private_row_fields(rows):
    """Remove cash-balance-derived fields from a list of signal-row dicts."""
    for row in rows:
        for key in _PRIVATE_ROW_KEYS:
            row.pop(key, None)
    return rows


def sanitize_for_json(obj):
    """Recursively convert NaN, Infinity, and -Infinity to None (null in JSON)."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
    elif isinstance(obj, dict):
        return {k: sanitize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [sanitize_for_json(v) for v in obj]
    return obj


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
            closes = [
                round(float(c), 4) if pd.notna(c) else None for c in df_ind["close"]
            ]
            vwaps = [
                round(float(v), 4)
                if "vwap_20" in df_ind.columns and pd.notna(v)
                else None
                for v in df_ind.get("vwap_20", df_ind["close"])
            ]
            chart_history["stocks"][norm_sym] = {
                "dates": dates,
                "close": closes,
                "vwap": vwaps,
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

    buys, exits, top10, closed_trades, fin_stmt, sectors, breakout_watchlist = matrix.analyze_market()
    last_data_date = dbm.get_latest_market_date()

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
        "chart_history": chart_history,
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
