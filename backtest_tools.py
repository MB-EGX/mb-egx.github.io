"""
backtest_tools.py
=================
Consolidated CLI + helper module for MB-EGX backtest operations.

This replaces these root-level one-purpose scripts:
    - check_backtest_readiness.py
    - diagnose_backtest_coverage.py
    - run_backtest.py
    - run_factor_backtest.py
    - export_backtest_summary.py

It also keeps ``build_summary()`` / ``export_backtest_summary()`` as
importable helpers so the desktop GUI can continue to generate the same
Strategy Calculator JSON shape without depending on a standalone script.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Iterable

import config  # noqa: F401  (thread-cap env vars; see config.py docstring)
from analytics import QuantitativeEngine
from backtester import run_walk_forward_backtest
from config import (
    BENCHMARK_TICKERS,
    CACHE_CONTROL_HEADER,
    PRIMARY_BENCHMARK_TICKER,
    WALK_FORWARD_BACKTEST_DEFAULTS,
    get_logger,
)
from factor_analysis import evaluate_pre_breakout_history, run_factor_backtest
from market_regime import normalized_benchmark_set

logger = get_logger("backtest_tools")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _progress(pct, msg):
    print(f"[{pct:>3}%] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# readiness  (from check_backtest_readiness.py)
# ---------------------------------------------------------------------------

def check_backtest_readiness() -> int:
    from db_manager import DatabaseManager

    cfg = WALK_FORWARD_BACKTEST_DEFAULTS
    min_bars_needed = cfg["min_train_bars"] + cfg["test_bars"]

    dbm = DatabaseManager()
    benchmark_norms = {dbm.normalize_symbol(t) for t in BENCHMARK_TICKERS}

    with dbm.get_connection() as conn:
        rows = conn.execute(
            "SELECT ticker, COUNT(*) AS n FROM market_data GROUP BY ticker;"
        ).fetchall()

    counts = [n for ticker, n in rows if dbm.normalize_symbol(ticker) not in benchmark_norms]

    if not counts:
        print("NOT_READY: no tradeable market data ingested yet.")
        return 1

    counts.sort()
    median_bars = counts[len(counts) // 2]
    max_bars = counts[-1]
    ready_count = sum(1 for n in counts if n >= min_bars_needed)

    if ready_count > 0:
        print(
            f"READY: {ready_count}/{len(counts)} ticker(s) have >= {min_bars_needed} bars "
            f"(median across all tickers: {median_bars})."
        )
        return 0

    bars_short = min_bars_needed - max_bars
    print(
        f"NOT_READY: no ticker has reached {min_bars_needed} bars yet "
        f"(median {median_bars}, best-covered ticker {max_bars}). "
        f"Needs ~{bars_short} more trading session(s) of ingestion for the "
        f"first ticker to qualify - or feed deeper historical CSVs into "
        f"market_data_feeds/ to backfill instead of waiting."
    )
    return 1


# ---------------------------------------------------------------------------
# diagnose  (from diagnose_backtest_coverage.py)
# ---------------------------------------------------------------------------

def diagnose_backtest_coverage(tickers: list[str] | None = None) -> int:
    cfg = WALK_FORWARD_BACKTEST_DEFAULTS
    min_bars_needed = cfg["min_train_bars"] + cfg["test_bars"]
    print(
        f"min_train_bars={cfg['min_train_bars']}  test_bars={cfg['test_bars']}  "
        f"=> a ticker needs >= {min_bars_needed} stored daily bars to backtest at all.\n"
    )

    qe = QuantitativeEngine()
    bulk = qe.get_all_market_data_bulk(days=None)
    print(f"get_all_market_data_bulk(days=None) returned {len(bulk)} ticker(s) total (before any filtering).\n")

    benchmark_norms = normalized_benchmark_set(qe.dbm)
    tradeable = {t: df for t, df in bulk.items() if qe.dbm.normalize_symbol(t) not in benchmark_norms}
    print(f"After excluding {len(bulk) - len(tradeable)} benchmark/index ticker(s): {len(tradeable)} tradeable ticker(s).\n")

    if tickers:
        wanted = {qe.dbm.normalize_symbol(t) for t in tickers} | set(tickers)
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
        print(
            f"\n{len(ok)} ticker(s) SHOULD have produced folds/trades. If the backtest still "
            "reported 0 for these, the issue is inside _simulate_ticker's fold-splitting or "
            "signal logic itself, not data coverage - worth a follow-up look with --tickers "
            "set to one of the names below."
        )
        for t, n in ok[:10]:
            print(f"   {t:<14} {n} bars")
    return 0


# ---------------------------------------------------------------------------
# run / factors  (from run_backtest.py + run_factor_backtest.py)
# ---------------------------------------------------------------------------

def run_backtest_cli(tickers: list[str] | None = None, save: bool = False, out: str | None = None) -> int:
    result = run_walk_forward_backtest(tickers=tickers, progress_callback=_progress)

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

    if save:
        from db_manager import DatabaseManager
        run_id = DatabaseManager().save_walkforward_run(result, tickers_filter=tickers)
        print(f"Saved as walk_forward_runs.id={run_id}")

    if out:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        print(f"Full result written to {out}")
    return 0


def _print_bucket_table(title: str, rows: list[dict]):
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


def run_factor_backtest_cli(
    tickers: list[str] | None = None,
    out: str | None = None,
    skip_pre_breakout: bool = False,
) -> int:
    print("Running walk-forward backtest with factor tagging...")
    report = run_factor_backtest(tickers=tickers, progress_callback=_progress)

    wf = report["walk_forward"]
    print("\n" + "=" * 90)
    print(
        f" Trades: {wf['trade_count']}   Win rate: {wf['win_rate_pct']}%   "
        f"Avg return/trade: {wf['avg_return_pct']}%   "
        f"Profit factor: {wf['profit_factor'] if wf['profit_factor'] is not None else '∞'}"
    )
    if not wf["is_reliable"]:
        print(f" ⚠  {wf['reliability_note']}")

    if report["benchmark"]:
        b = report["benchmark"]
        print(
            f"\n Benchmark: {b['primary_benchmark']}  "
            f"({b['trades_with_benchmark_data']}/{b['trades_total']} trades have benchmark data)"
        )
        print(
            f" Avg excess return vs benchmark: {b['avg_excess_return_vs_benchmark_pct']}%   "
            f"Beat benchmark: {b['pct_of_trades_that_beat_benchmark']}% of trades"
            + ("" if b["reliable"] else "  ⚠ small sample")
        )
    else:
        print(
            f"\n ⚠  No benchmark data found for '{PRIMARY_BENCHMARK_TICKER}' - "
            "feed an EGX30 (or your chosen config.PRIMARY_BENCHMARK_TICKER) CSV into "
            "market_data_feeds/ and re-ingest to unlock alpha/regime breakdowns."
        )

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

    if not skip_pre_breakout:
        print("\nValidating Pre-Breakout Watchlist score against logged history...")
        pb = evaluate_pre_breakout_history()
        report["pre_breakout_watchlist"] = pb
        print(f" Sample: {pb['sample_count']} scored snapshot(s)")
        if pb.get("score_quintiles_low_to_high"):
            print(
                f" Overall hit rate (reached +{pb['target_pct']}% within "
                f"{pb['window_sessions'][1]} sessions): {pb['overall_hit_rate_pct']}%"
            )
            print(" Score quintile (low -> high) -> hit rate:")
            for q in pb["score_quintiles_low_to_high"]:
                print(
                    f"   score {q['score_range']}: n={q['sample_count']:<4} "
                    f"hit_rate={q['hit_rate_pct']:>5}%   avg_best_gain={q['avg_best_gain_pct']}%"
                )
        print(f" {pb['note']}")

    if out:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"\nFull result written to {out}")
    return 0


# ---------------------------------------------------------------------------
# export-summary  (from export_backtest_summary.py)
# ---------------------------------------------------------------------------

def _build_action_breakdown(trades: list[dict]) -> list[dict]:
    by_action: dict[str, list[float]] = {}
    for tr in trades:
        by_action.setdefault(tr["action"], []).append(tr["return_pct"])

    rows = []
    for action, returns in by_action.items():
        wins = [r for r in returns if r > 0]
        rows.append(
            {
                "action": action,
                "trade_count": len(returns),
                "win_rate_pct": round(len(wins) / len(returns) * 100.0, 1) if returns else 0.0,
                "avg_return_pct": round(sum(returns) / len(returns), 3) if returns else 0.0,
            }
        )
    rows.sort(key=lambda r: r["trade_count"], reverse=True)
    return rows


def _build_equity_curve(trades: list[dict], max_points: int = 200) -> list[dict]:
    ordered = sorted(trades, key=lambda t: t["exit_date"])
    equity = 100.0
    curve = [{"date": None, "equity": round(equity, 2), "trade_count": 0}]
    for i, tr in enumerate(ordered, start=1):
        equity *= (1.0 + tr["return_pct"] / 100.0)
        curve.append({"date": tr["exit_date"], "equity": round(equity, 2), "trade_count": i})

    if len(curve) <= max_points:
        return curve
    stride = len(curve) / max_points
    sampled = [curve[int(i * stride)] for i in range(max_points)]
    if sampled[-1] != curve[-1]:
        sampled.append(curve[-1])
    return sampled


def build_summary(result: dict, equity_points: int = 200) -> dict:
    trades = result.get("trades", [])
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "config": result["config"],
        "tickers_backtested": result["tickers_backtested"],
        "fold_count": result["fold_count"],
        "is_reliable": result["is_reliable"],
        "reliability_note": result["reliability_note"],
        "trade_count": result["trade_count"],
        "win_rate_pct": result["win_rate_pct"],
        "avg_return_pct": result["avg_return_pct"],
        "profit_factor": result["profit_factor"],
        "overall": result["overall"],
        "by_action": _build_action_breakdown(trades),
        "equity_curve": _build_equity_curve(trades, max_points=equity_points),
    }


def export_backtest_summary(tickers: list[str] | None = None, equity_points: int = 200) -> str:
    print("🧪 Running walk-forward backtest for the public Strategy Calculator...")
    result = run_walk_forward_backtest(tickers=tickers, progress_callback=_progress)
    summary = build_summary(result, equity_points)

    output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_public", "data")
    os.makedirs(output_dir, exist_ok=True)
    file_path = os.path.join(output_dir, "strategy_performance.json")
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    manifest_path = os.path.join(output_dir, "cache_manifest.json")
    manifest = {}
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except (json.JSONDecodeError, OSError):
            manifest = {}
    manifest["strategy_performance.json"] = {"cache_control": CACHE_CONTROL_HEADER}
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(f"✅ Wrote {file_path}")
    print(
        f"   trades={summary['trade_count']}  win_rate={summary['win_rate_pct']}%  "
        f"avg_return={summary['avg_return_pct']}%  reliable={summary['is_reliable']}"
    )
    return file_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Consolidated MB-EGX backtest tools.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("readiness", help="Fast readiness gate for walk-forward backtests.")

    p_diag = sub.add_parser("diagnose", help="Explain why backtesting returns 0 tickers/folds.")
    p_diag.add_argument("--tickers", nargs="*", default=None, help="Optional ticker subset.")

    p_run = sub.add_parser("run", help="Run the walk-forward backtest engine.")
    p_run.add_argument("--tickers", nargs="*", default=None, help="Optional ticker subset.")
    p_run.add_argument("--save", action="store_true", help="Persist the result to the local DB.")
    p_run.add_argument("--out", default=None, help="Optional path to also write the full result as JSON.")

    p_fact = sub.add_parser("factors", help="Run factor-validation backtest harness.")
    p_fact.add_argument("--tickers", nargs="*", default=None, help="Optional ticker subset.")
    p_fact.add_argument("--out", default=None, help="Optional path to write the full result as JSON.")
    p_fact.add_argument("--skip-pre-breakout", action="store_true", help="Skip logged Pre-Breakout validation.")

    p_sum = sub.add_parser("export-summary", help="Export public strategy_performance.json summary.")
    p_sum.add_argument("--tickers", nargs="*", default=None, help="Optional ticker subset.")
    p_sum.add_argument("--equity-points", type=int, default=200, help="Max points in downsampled equity curve.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "readiness":
        return check_backtest_readiness()
    if args.command == "diagnose":
        return diagnose_backtest_coverage(args.tickers)
    if args.command == "run":
        return run_backtest_cli(args.tickers, args.save, args.out)
    if args.command == "factors":
        return run_factor_backtest_cli(args.tickers, args.out, args.skip_pre_breakout)
    if args.command == "export-summary":
        export_backtest_summary(args.tickers, args.equity_points)
        return 0

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
