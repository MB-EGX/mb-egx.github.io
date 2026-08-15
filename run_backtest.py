"""
run_backtest.py
================
CLI wrapper around backtester.run_walk_forward_backtest() for running a
walk-forward backtest without opening the desktop app (CI, cron, a quick
terminal check after tuning config.ACTION_THRESHOLDS/SCORE_WEIGHTS).

Deliberately NOT wired into publish.py's nightly pipeline: a multi-year,
multi-ticker walk-forward run is much slower than the nightly export and
has no reason to block or slow down the daily data publish. Run this by
hand whenever you want a fresh read, e.g. right after tuning config.py.

Usage:
    python run_backtest.py                        # every ticker
    python run_backtest.py --tickers COMI HRHO     # just these
    python run_backtest.py --save                  # also persist to the
                                                     # local DB (visible in
                                                     # the desktop app's
                                                     # Walk-Forward dialog
                                                     # history dropdown)
"""
import argparse
import json
import sys

import config  # noqa: F401  (thread-cap env vars; see config.py docstring)
from backtester import run_walk_forward_backtest


def main():
    parser = argparse.ArgumentParser(description="Run the walk-forward backtest engine.")
    parser.add_argument("--tickers", nargs="*", default=None, help="Optional ticker subset (default: all).")
    parser.add_argument("--save", action="store_true", help="Persist the result to the local DB for the desktop app's history dropdown.")
    parser.add_argument("--out", default=None, help="Optional path to also write the full result as JSON.")
    args = parser.parse_args()

    def _progress(pct, msg):
        print(f"[{pct:>3}%] {msg}", file=sys.stderr)

    result = run_walk_forward_backtest(tickers=args.tickers, progress_callback=_progress)

    print("\n" + "=" * 60)
    print(f" Tickers backtested : {result['tickers_backtested']}")
    print(f" Folds              : {result['fold_count']}")
    print(f" Trades             : {result['trade_count']}")
    print(f" Win rate           : {result['win_rate_pct']}%")
    print(f" Avg return/trade   : {result['avg_return_pct']}%")
    pf = result.get("profit_factor")
    print(f" Profit factor      : {pf if pf is not None else '∞ (no losing trades)'}")
    print(f" Sharpe (overall)   : {result['overall'].get('sharpe', 0)}")
    print(f" Max drawdown       : {result['overall'].get('max_drawdown', 0) * 100:.2f}%")
    if not result.get("is_reliable", True):
        print(f"\n ⚠  {result.get('reliability_note')}")
    print("=" * 60)

    if args.save:
        from db_manager import DatabaseManager
        run_id = DatabaseManager().save_walkforward_run(result, tickers_filter=args.tickers)
        print(f"Saved as walk_forward_runs.id={run_id}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        print(f"Full result written to {args.out}")


if __name__ == "__main__":
    main()
