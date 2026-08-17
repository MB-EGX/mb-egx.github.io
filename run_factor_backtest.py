"""
run_factor_backtest.py
=======================
CLI for the factor-validation harness (factor_analysis.py): runs the
same walk-forward backtest run_backtest.py runs, then breaks the result
down by action type, market regime, ADX/RSI/gap bucket, volume-
confirmation strength, and sector - so you can see, with real trade
counts and 95% confidence intervals, which of these factors the data
actually backs up, instead of trusting a factor because it "feels"
right. Also validates the Pre-Breakout Watchlist's own Breakout Score
against real logged history (see factor_analysis.
evaluate_pre_breakout_history) - covering the OTHER half of Session
Picks, on top of the fired-signal actions the factor backtest above
already covers.

Deliberately a separate script from run_backtest.py (same reasoning as
run_backtest.py's own separation from publish.py - see its docstring):
this is slower (adds benchmark loading + full-history factor tagging)
and is a deliberate, occasional "which factors can I trust" check, not
part of any automated pipeline.

Usage:
    python run_factor_backtest.py                       # every ticker
    python run_factor_backtest.py --tickers COMI HRHO
    python run_factor_backtest.py --out factors.json
    python run_factor_backtest.py --skip-pre-breakout    # walk-forward factors only
"""
import argparse
import json
import sys

import config  # noqa: F401  (thread-cap env vars; see config.py docstring)
from factor_analysis import run_factor_backtest, evaluate_pre_breakout_history


def _print_bucket_table(title: str, rows: list):
    print(f"\n {title}")
    print(" " + "-" * 84)
    print(f" {'bucket':<34}{'n':>5}{'win%':>8}{'95% CI':>16}{'avg %':>9}{'excess %':>11}{'reliable':>10}")
    for r in rows:
        ci = f"[{r['win_rate_95pct_ci'][0]},{r['win_rate_95pct_ci'][1]}]"
        excess = r["avg_excess_return_vs_benchmark_pct"]
        excess_str = f"{excess}" if excess is not None else "n/a"
        print(
            f" {str(r['bucket'])[:34]:<34}{r['trade_count']:>5}{r['win_rate_pct']:>8}"
            f"{ci:>16}{r['avg_return_pct']:>9}{excess_str:>11}{str(r['reliable']):>10}"
        )


def main():
    parser = argparse.ArgumentParser(description="Run the factor-validation backtest harness.")
    parser.add_argument("--tickers", nargs="*", default=None, help="Optional ticker subset (default: all).")
    parser.add_argument("--out", default=None, help="Optional path to write the full result as JSON.")
    parser.add_argument(
        "--skip-pre-breakout", action="store_true",
        help="Skip the logged Pre-Breakout Watchlist score validation (walk-forward factors only).",
    )
    args = parser.parse_args()

    def _progress(pct, msg):
        print(f"[{pct:>3}%] {msg}", file=sys.stderr)

    print("Running walk-forward backtest with factor tagging...")
    report = run_factor_backtest(tickers=args.tickers, progress_callback=_progress)

    wf = report["walk_forward"]
    print("\n" + "=" * 90)
    print(f" Trades: {wf['trade_count']}   Win rate: {wf['win_rate_pct']}%   "
          f"Avg return/trade: {wf['avg_return_pct']}%   "
          f"Profit factor: {wf['profit_factor'] if wf['profit_factor'] is not None else '∞'}")
    if not wf["is_reliable"]:
        print(f" ⚠  {wf['reliability_note']}")

    if report["benchmark"]:
        b = report["benchmark"]
        print(f"\n Benchmark: {b['primary_benchmark']}  "
              f"({b['trades_with_benchmark_data']}/{b['trades_total']} trades have benchmark data)")
        print(f" Avg excess return vs benchmark: {b['avg_excess_return_vs_benchmark_pct']}%   "
              f"Beat benchmark: {b['pct_of_trades_that_beat_benchmark']}% of trades"
              + ("" if b["reliable"] else "  ⚠ small sample"))
    else:
        print(f"\n ⚠  No benchmark data found for '{config.PRIMARY_BENCHMARK_TICKER}' - "
              "feed an EGX30 (or your chosen config.PRIMARY_BENCHMARK_TICKER) CSV into "
              "market_data_feeds/ and re-ingest to unlock alpha/regime breakdowns.")

    for title, key in [
        ("By action (fired signals - STRONG BUY / BREAKOUT BUY / BUY ON DIP / ACCUMULATE)", "by_action"),
        ("By market regime at entry", "by_market_regime_at_entry"),
        ("By ADX bucket", "by_adx_bucket"),
        ("By RSI bucket", "by_rsi_bucket"),
        ("By gap bucket", "by_gap_bucket"),
        ("By volume confirmation strength", "by_volume_confirmation"),
        ("By sector", "by_sector"),
        ("By exit reason", "by_exit_reason"),
    ]:
        _print_bucket_table(title, report["factors"][key])

    print(f"\n {report['note']}")
    print("=" * 90)

    if not args.skip_pre_breakout:
        print("\nValidating Pre-Breakout Watchlist score against logged history...")
        pb = evaluate_pre_breakout_history()
        report["pre_breakout_watchlist"] = pb
        print(f" Sample: {pb['sample_count']} scored snapshot(s)")
        if pb.get("score_quintiles_low_to_high"):
            print(f" Overall hit rate (reached +{pb['target_pct']}% within "
                  f"{pb['window_sessions'][1]} sessions): {pb['overall_hit_rate_pct']}%")
            print(" Score quintile (low -> high) -> hit rate:")
            for q in pb["score_quintiles_low_to_high"]:
                print(f"   score {q['score_range']}: n={q['sample_count']:<4} "
                      f"hit_rate={q['hit_rate_pct']:>5}%   avg_best_gain={q['avg_best_gain_pct']}%")
        print(f" {pb['note']}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"\nFull result written to {args.out}")


if __name__ == "__main__":
    main()
