"""
refresh_win_rate_cache.py
==========================
Runs the full walk-forward backtest (backtester.run_walk_forward_backtest,
ALL tickers) and saves REAL, out-of-sample-tested per-action win rates to
config.BACKTEST_WIN_RATE_CACHE_PATH, which decision_matrix.py's live run
reads to size positions off measured edge instead of the 50/50
DEFAULT_WIN_RATE_PRIOR fallback.

WHY THIS IS ITS OWN SCRIPT, NOT PART OF publish.py: a full walk-forward
backtest across ~260 tickers takes real time (minutes), and publish.py is
meant to be fast enough to run after every session. Win rates by action
type also don't meaningfully change session-to-session - re-running this
weekly (or after any threshold/strategy-rule change in config.py) is
sufficient. If nothing has changed in your rules, there's no need to run
this more than that.

USAGE (run from your MB-EGX repo folder, same folder as publish.py):
    python refresh_win_rate_cache.py

Then commit + push web_public/data/backtest_win_rates.json the same way
publish.py pushes everything else (or just run `python publish.py`
afterward - it stages the whole repo).
"""
from __future__ import annotations

import sys

import config  # noqa: F401  (see publish.py's own comment: must import first for its thread-count env vars)
from backtester import run_walk_forward_backtest, save_win_rate_cache
from db_manager import DatabaseLockedError


def main():
    print("Running full walk-forward backtest across all tickers...")
    print("(this can take a few minutes - it's meant to be run periodically, not on every publish)")

    result = run_walk_forward_backtest(
        progress_callback=lambda pct, msg: print(f"  [{pct:>3}%] {msg}")
    )

    by_action = result.get("by_action", {})
    if not by_action:
        print("\n[!] No trades were produced by the backtest - nothing to cache.")
        print("    Win-rate cache left unchanged; live scoring will keep using the 50/50 prior.")
        return

    print("\nPer-action results (out-of-sample):")
    for family, stats in sorted(by_action.items()):
        reliability = "reliable" if stats["is_reliable"] else "TOO FEW TRADES - will fall back to prior live"
        avg_r = stats["avg_r_multiple"]
        avg_r_str = f"{avg_r:+.2f}R" if avg_r is not None else "N/A"
        print(f"  {family:<14} win_rate={stats['win_rate_pct']:>5.1f}%  "
              f"trades={stats['trade_count']:>4}  avg_r={avg_r_str:>7}  ({reliability})")

    path = save_win_rate_cache(by_action)
    print(f"\n✅ Saved win-rate cache -> {path}")
    print("   Run publish.py (or push manually) to make it live.")


if __name__ == "__main__":
    try:
        main()
    except DatabaseLockedError as e:
        print("\n" + "=" * 60)
        print(" [!] Can't read the database - it's already open in")
        print("     another program (usually the MB-EGX desktop app).")
        print("     Close that app, then run this script again.")
        print("=" * 60)
        print(f"\n     Details: {e}")
        sys.exit(1)
