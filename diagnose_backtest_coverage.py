"""
diagnose_backtest_coverage.py
==============================
run_backtest.py / run_factor_backtest.py both came back with
"Tickers backtested: 0, Folds: 0, Trades: 0" against your real
quant_master.duckdb. That number (tickers_run in backtester.
run_walk_forward_backtest) only increments for a ticker when:

    1. df is not None/empty, AND
    2. len(df) >= WALK_FORWARD_BACKTEST_DEFAULTS["min_train_bars"]
       + WALK_FORWARD_BACKTEST_DEFAULTS["test_bars"]   (250 + 60 = 310
       trading bars by default - roughly 15 months of daily data), AND
    3. QuantitativeEngine.compute_indicators(df) doesn't raise.

A ticker that fails any of those is silently skipped (backtester.py's
own `continue` statements) - there is currently no visibility into
WHICH of the 3 is actually happening, or for how many tickers. This
script reproduces backtester.py's exact same filter, ticker by ticker,
and prints why each one was excluded - so "0 tickers backtested" turns
into an actual, actionable number instead of a silent zero.

USAGE
-----
    python diagnose_backtest_coverage.py
    python diagnose_backtest_coverage.py --tickers COMI HRHO
"""
from __future__ import annotations

import argparse
import traceback

import config  # noqa: F401  (thread-cap env vars; see config.py docstring)
from config import WALK_FORWARD_BACKTEST_DEFAULTS
from analytics import QuantitativeEngine
from market_regime import normalized_benchmark_set


def main():
    parser = argparse.ArgumentParser(description="Diagnose why the walk-forward backtest sees 0 tickers/folds.")
    parser.add_argument("--tickers", nargs="*", default=None, help="Optional ticker subset (default: all).")
    args = parser.parse_args()

    cfg = WALK_FORWARD_BACKTEST_DEFAULTS
    min_bars_needed = cfg["min_train_bars"] + cfg["test_bars"]
    print(f"min_train_bars={cfg['min_train_bars']}  test_bars={cfg['test_bars']}  "
          f"=> a ticker needs >= {min_bars_needed} stored daily bars to backtest at all.\n")

    qe = QuantitativeEngine()
    bulk = qe.get_all_market_data_bulk(days=None)
    print(f"get_all_market_data_bulk(days=None) returned {len(bulk)} ticker(s) total (before any filtering).\n")

    benchmark_norms = normalized_benchmark_set(qe.dbm)
    tradeable = {t: df for t, df in bulk.items() if qe.dbm.normalize_symbol(t) not in benchmark_norms}
    print(f"After excluding {len(bulk) - len(tradeable)} benchmark/index ticker(s): {len(tradeable)} tradeable ticker(s).\n")

    if args.tickers:
        wanted = {qe.dbm.normalize_symbol(t) for t in args.tickers} | set(args.tickers)
        tradeable = {t: df for t, df in tradeable.items() if t in wanted or qe.dbm.normalize_symbol(t) in wanted}
        print(f"Restricted to --tickers filter: {len(tradeable)} ticker(s).\n")

    too_short, indicator_failed, empty_after_indicators, ok = [], [], [], []

    for ticker, df in tradeable.items():
        n = 0 if df is None else len(df)
        if df is None or df.empty or n < min_bars_needed:
            too_short.append((ticker, n))
            continue
        try:
            df_ind = qe.compute_indicators(df)
        except Exception as e:
            indicator_failed.append((ticker, n, f"{type(e).__name__}: {e}"))
            continue
        if df_ind is None or df_ind.empty:
            empty_after_indicators.append((ticker, n))
            continue
        ok.append((ticker, n))

    print("=" * 78)
    print(f" ✅ Would actually be backtested : {len(ok)}")
    print(f" ⛔ Too few bars (< {min_bars_needed})       : {len(too_short)}")
    print(f" ⛔ compute_indicators() raised   : {len(indicator_failed)}")
    print(f" ⛔ Empty after compute_indicators: {len(empty_after_indicators)}")
    print("=" * 78)

    if too_short:
        bar_counts = sorted(n for _, n in too_short)
        median = bar_counts[len(bar_counts) // 2]
        print(f"\nBars-too-short tickers: median {median} bars stored, max {max(bar_counts)}, min {min(bar_counts)}.")
        print("Sample (ticker: bars_stored):")
        for t, n in too_short[:15]:
            print(f"   {t:<14} {n}")
        if len(too_short) > 15:
            print(f"   ... and {len(too_short) - 15} more")
        print(
            f"\n-> If most/all of these are well under {min_bars_needed}, your ingested "
            "history simply doesn't go back far enough yet for a walk-forward "
            "backtest (needs ~15 months of daily bars per ticker minimum). Feed "
            "more historical CSVs (further back in time) into market_data_feeds/ "
            "and re-run publish.py / the ingestion pipeline - there is no config "
            "knob that substitutes for actually having the history."
        )

    if indicator_failed:
        print("\ncompute_indicators() raised for these tickers (real bug, not a data-depth issue):")
        for t, n, err in indicator_failed[:10]:
            print(f"   {t:<14} ({n} bars) -> {err}")
        if len(indicator_failed) > 10:
            print(f"   ... and {len(indicator_failed) - 10} more")

    if ok:
        print(f"\n{len(ok)} ticker(s) SHOULD have produced folds/trades. If run_backtest.py still "
              "reported 0 for these, the issue is inside _simulate_ticker's fold-splitting or "
              "signal logic itself, not data coverage - worth a follow-up look with --tickers "
              "set to one of the names below.")
        for t, n in ok[:10]:
            print(f"   {t:<14} {n} bars")


if __name__ == "__main__":
    main()
