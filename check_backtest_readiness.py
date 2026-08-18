"""
check_backtest_readiness.py
============================
Fast (single SQL query, no per-ticker indicator computation) readiness
gate for the daily walk-forward backtest refresh in launch_and_publish.bat.

WHY THIS EXISTS: diagnose_backtest_coverage.py confirmed the real cause
of "Tickers backtested: 0" - the ingested history is currently only ~35
daily bars per ticker, nowhere near the ~310 (min_train_bars + test_bars)
a single ticker needs to produce even one out-of-sample fold. That isn't
a config problem - no threshold tune substitutes for missing history -
so running the full run_backtest.py / run_factor_backtest.py cycle every
day (263 tickers, walk-forward, genuinely slow) is currently pure wasted
time: it is GUARANTEED to come back empty until enough calendar days of
ingestion have passed (or you backfill deeper historical CSVs).

This script answers "is it worth running the slow backtests today" in
under a second via one SQL aggregate query, instead of paying the full
walk-forward cost just to find out the answer is still no.

EXIT CODE: 0 if at least one non-benchmark ticker has enough bars to
produce a fold, 1 otherwise. Always prints a one-line status either way.

USAGE (called from launch_and_publish.bat, but safe to run by hand):
    python check_backtest_readiness.py
"""
from __future__ import annotations

import sys

import config  # noqa: F401  (thread-cap env vars; see config.py docstring)
from config import WALK_FORWARD_BACKTEST_DEFAULTS, BENCHMARK_TICKERS
from db_manager import DatabaseManager


def main():
    cfg = WALK_FORWARD_BACKTEST_DEFAULTS
    min_bars_needed = cfg["min_train_bars"] + cfg["test_bars"]

    dbm = DatabaseManager()
    benchmark_norms = {dbm.normalize_symbol(t) for t in BENCHMARK_TICKERS}

    with dbm.get_connection() as conn:
        rows = conn.execute(
            "SELECT ticker, COUNT(*) AS n FROM market_data GROUP BY ticker;"
        ).fetchall()

    # Exclude benchmark/index rows the same way backtester.py does - they
    # were never eligible to backtest as a stock, so their (usually much
    # longer) history shouldn't make the tradeable universe look more
    # ready than it actually is.
    counts = [n for ticker, n in rows if dbm.normalize_symbol(ticker) not in benchmark_norms]

    if not counts:
        print("NOT_READY: no tradeable market data ingested yet.")
        sys.exit(1)

    counts.sort()
    median_bars = counts[len(counts) // 2]
    max_bars = counts[-1]
    ready_count = sum(1 for n in counts if n >= min_bars_needed)

    if ready_count > 0:
        print(f"READY: {ready_count}/{len(counts)} ticker(s) have >= {min_bars_needed} bars "
              f"(median across all tickers: {median_bars}).")
        sys.exit(0)

    bars_short = min_bars_needed - max_bars
    print(
        f"NOT_READY: no ticker has reached {min_bars_needed} bars yet "
        f"(median {median_bars}, best-covered ticker {max_bars}). "
        f"Needs ~{bars_short} more trading session(s) of ingestion for the "
        f"first ticker to qualify - or feed deeper historical CSVs into "
        f"market_data_feeds/ to backfill instead of waiting."
    )
    sys.exit(1)


if __name__ == "__main__":
    main()
