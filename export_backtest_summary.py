"""
export_backtest_summary.py
===========================
N6: "What would have happened if I followed this?" calculator (web).

Runs the same walk-forward backtest engine as run_backtest.py, then
condenses the result into a small, PUBLIC-safe JSON shard —
``web_public/data/strategy_performance.json`` — that the web dashboard's
Strategy Calculator tab reads to answer "if I'd followed every qualifying
BUY signal this app produced, what would that have looked like?".

WHY THIS IS A SEPARATE SCRIPT, NOT PART OF publish.py's NIGHTLY RUN:
    backtester.py's own module docstring already explains why
    run_backtest.py is deliberately NOT wired into the nightly
    ingest -> export -> publish pipeline: a multi-year, multi-ticker
    walk-forward run is far slower than the nightly export and has no
    reason to block or slow down the daily data publish. This script is
    the same trade-off applied to the web-facing summary: run it by hand
    (or on a slow weekly/monthly cron of your own) whenever you want the
    public "strategy performance" numbers refreshed, then let the next
    ordinary `python publish.py` run pick up and push the resulting
    strategy_performance.json file exactly like any other changed file
    (publish.py's `git add -A` doesn't care which script wrote what).

WHAT'S IN THE FILE (all of it is already public-safe — see below):
    * Aggregate stats identical in spirit to run_backtest.py's console
      summary (trade_count, win_rate_pct, avg_return_pct, profit_factor,
      sharpe/sortino/max_drawdown, is_reliable/reliability_note) — these
      describe the STRATEGY's historical behavior, not any user's
      account, so none of export_json.py's privacy-stripping logic
      applies here.
    * A breakdown BY ACTION LABEL (STRONG BUY vs BREAKOUT BUY vs BUY ON
      DIP vs ACCUMULATE) so the calculator can show "signals labelled X
      won Y% of the time", not just one blended number.
    * An illustrative sequential equity curve: every trade sorted by
      exit_date and compounded ONE AFTER ANOTHER as if a single account
      took every qualifying signal in sequence. This is explicitly NOT a
      faithful portfolio simulation (real trades from different tickers
      overlap in time and a real account can hold >1 position at once —
      see backtester.py's "SCOPE / KNOWN SIMPLIFICATIONS"), so the
      calculator UI must label it "illustrative" and show
      reliability_note/is_reliable alongside it, never as a promise.
    * Per-trade rows are NOT included (backtester.py's own "trades" list
      can be tens of thousands of rows — pure bandwidth waste for a
      summary calculator) — only the compounded curve's checkpoints.

USAGE
-----
    python export_backtest_summary.py                  # every ticker
    python export_backtest_summary.py --tickers COMI HRHO
    python export_backtest_summary.py --equity-points 200
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import config  # noqa: F401  (thread-cap env vars; see config.py docstring)
from config import CACHE_CONTROL_HEADER, get_logger
from backtester import run_walk_forward_backtest

logger = get_logger("export_backtest_summary")


def _build_action_breakdown(trades: list[dict]) -> list[dict]:
    """Win rate / avg return per action label (STRONG BUY, BREAKOUT BUY
    (X-OVER + MOMENTUM), BUY ON DIP, ACCUMULATE, ...) so the calculator
    can show which signal TYPE actually drove the aggregate number,
    instead of implying every label performs identically."""
    by_action: dict[str, list[float]] = {}
    for tr in trades:
        by_action.setdefault(tr["action"], []).append(tr["return_pct"])

    rows = []
    for action, returns in by_action.items():
        wins = [r for r in returns if r > 0]
        rows.append({
            "action": action,
            "trade_count": len(returns),
            "win_rate_pct": round(len(wins) / len(returns) * 100.0, 1) if returns else 0.0,
            "avg_return_pct": round(sum(returns) / len(returns), 3) if returns else 0.0,
        })
    rows.sort(key=lambda r: r["trade_count"], reverse=True)
    return rows


def _build_equity_curve(trades: list[dict], max_points: int = 200) -> list[dict]:
    """Illustrative "growth of 100 EGP" curve: every trade sorted by
    exit_date and compounded in sequence (net_pct already includes
    round-trip fees — see backtester._simulate_ticker). NOT a real
    multi-position portfolio simulation; see module docstring.

    Downsampled to ``max_points`` checkpoints (even stride through the
    sorted trade list) so a multi-thousand-trade backtest doesn't ship a
    multi-thousand-point array to the browser for a chart that's only
    ever shown at a few hundred pixels wide anyway.
    """
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


def build_summary(result: dict, equity_points: int) -> dict:
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
        # "growth of 100 EGP if every qualifying signal was taken in
        # sequence" — illustrative only, see module docstring.
        "equity_curve": _build_equity_curve(trades, max_points=equity_points),
    }


def export_backtest_summary(tickers: list[str] | None, equity_points: int) -> str:
    def _progress(pct, msg):
        print(f"[{pct:>3}%] {msg}", file=sys.stderr)

    print("🧪 Running walk-forward backtest for the public Strategy Calculator...")
    result = run_walk_forward_backtest(tickers=tickers, progress_callback=_progress)
    summary = build_summary(result, equity_points)

    output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_public", "data")
    os.makedirs(output_dir, exist_ok=True)
    file_path = os.path.join(output_dir, "strategy_performance.json")
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    # Same shard-caching convention export_json.py already uses, so a
    # future cache_manifest.json regeneration (or a manual CDN rule)
    # treats this file identically to the other public shards.
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
    print(f"   trades={summary['trade_count']}  win_rate={summary['win_rate_pct']}%  "
          f"avg_return={summary['avg_return_pct']}%  reliable={summary['is_reliable']}")
    return file_path


def main():
    parser = argparse.ArgumentParser(description="Export the public Strategy Calculator (N6) backtest summary.")
    parser.add_argument("--tickers", nargs="*", default=None, help="Optional ticker subset (default: all).")
    parser.add_argument("--equity-points", type=int, default=200, help="Max points in the downsampled equity curve.")
    args = parser.parse_args()
    export_backtest_summary(args.tickers, args.equity_points)


if __name__ == "__main__":
    main()
