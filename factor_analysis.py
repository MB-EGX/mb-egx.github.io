"""
factor_analysis.py
===================
The factor-validation harness. Answers "which of the factors this app's
signals/scores rely on are actually worth trusting" with real numbers -
trade counts, win rates, confidence intervals - instead of a hunch.
Two independent halves:

1. run_factor_backtest() - runs backtester.run_walk_forward_backtest()
   (which now tags every closed trade with sector / ADX bucket / RSI
   bucket / gap bucket / volume-confirmation strength / market regime
   at entry / excess return vs. config.PRIMARY_BENCHMARK_TICKER - see
   backtester.py and market_regime.py) and rolls those tags up into a
   win-rate/avg-return/95%-CI table per factor. This covers every FIRED
   signal action - STRONG BUY, all three BREAKOUT BUY variants, BUY ON
   DIP, ACCUMULATE - the same pool that feeds Session Picks' primary
   "signal" bucket (see session_picks._candidate_pool), not just the
   Pre-Breakout Watchlist.

2. evaluate_pre_breakout_history() - separately validates the Pre-
   Breakout Watchlist's own Breakout Score (session_picks' supplementary
   "pre_breakout" bucket) against what actually happened afterwards,
   using real logged snapshots (db_manager.log_breakout_watchlist_
   snapshot, written by decision_matrix.analyze_market on every run) -
   never a re-implementation of that scoring formula. See that
   function's own docstring for why re-deriving the score bar-by-bar
   historically (the way backtester.py mirrors the main buy signal)
   is NOT done here: that score is genuinely cross-sectional (needs the
   whole market's same-day data - see decision_matrix.py's sector_avg_5d
   pre-pass), so re-deriving it at every past date would mean re-running
   something close to the full analyze_market() scan at every historical
   date, and any drift from the live scorer would validate a DIFFERENT
   formula than the one actually in production. Measuring the real,
   already-computed score going forward avoids both problems, at the
   honest cost of needing snapshots to accumulate before it has anything
   to say - see the "reliable"/"sample_count" fields it returns.

RELIABILITY, EVERYWHERE: every bucket/result in this module is reported
alongside its raw sample size and a "reliable" flag gated on
config.FACTOR_BACKTEST_MIN_TRADES_RELIABLE. A false flag never means
"this factor is bad" - it means there is not yet enough closed-trade (or
snapshot) history to say either way. Nothing here is ever hidden for
having a small sample; it's labeled, not suppressed - hiding it would
be exactly the kind of guessing this harness exists to replace.

USAGE
-----
    from factor_analysis import run_factor_backtest, evaluate_pre_breakout_history
    report = run_factor_backtest()
    pre_breakout = evaluate_pre_breakout_history()

See run_factor_backtest.py for the CLI wrapper.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone

from config import (
    FACTOR_BACKTEST_MIN_TRADES_RELIABLE,
    PRIMARY_BENCHMARK_TICKER,
    SESSION_PICKS_EXPECTED_PCT,
    SESSION_PICKS_EXPECTED_DAYS,
    get_logger,
)
from backtester import run_walk_forward_backtest
from market_regime import build_close_by_date

logger = get_logger("factor_analysis")


# =============================================================================
# Part 1: factor breakdown of the walk-forward backtest's closed trades
# =============================================================================

def _wilson_ci(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score confidence interval for a win rate. Used instead
    of a plain (wins/n) point estimate because a naive normal-
    approximation CI can go outside [0, 1] or badly understate
    uncertainty below ~30 samples - exactly the range most factor
    slices here fall into. This is the actual math behind the
    "reliable" flag's spirit: two buckets with the same win rate can
    have very different confidence depending on n, and the CI width
    shows that directly instead of hiding it behind one number.
    """
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + (z ** 2) / n
    center = p + (z ** 2) / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + (z ** 2) / (4 * n)) / n)
    lo = (center - margin) / denom
    hi = (center + margin) / denom
    return (max(0.0, lo), min(1.0, hi))


def _bucket_stats(trades: list[dict], key_fn) -> list[dict]:
    """Groups ``trades`` by key_fn(trade) and computes win-rate/avg-
    return/avg-excess-return with a 95% CI and a reliability flag for
    each group, sorted by trade count descending. This is the one place
    "is this factor worth trusting" gets an actual number attached -
    see module docstring's RELIABILITY note.
    """
    groups: dict = defaultdict(list)
    for tr in trades:
        groups[key_fn(tr)].append(tr)

    rows = []
    for key, group in groups.items():
        n = len(group)
        returns = [t["return_pct"] for t in group]
        wins = sum(1 for r in returns if r > 0)
        win_rate = (wins / n) if n else 0.0
        ci_lo, ci_hi = _wilson_ci(wins, n)
        excess = [t["excess_return_pct"] for t in group if t.get("excess_return_pct") is not None]
        rows.append({
            "bucket": key,
            "trade_count": n,
            "win_rate_pct": round(win_rate * 100.0, 1),
            "win_rate_95pct_ci": [round(ci_lo * 100.0, 1), round(ci_hi * 100.0, 1)],
            "avg_return_pct": round(sum(returns) / n, 3) if n else 0.0,
            "avg_excess_return_vs_benchmark_pct": round(sum(excess) / len(excess), 3) if excess else None,
            "benchmark_coverage": f"{len(excess)}/{n}",
            "reliable": n >= FACTOR_BACKTEST_MIN_TRADES_RELIABLE,
        })
    rows.sort(key=lambda r: r["trade_count"], reverse=True)
    return rows


def run_factor_backtest(tickers: list[str] | None = None, progress_callback=None) -> dict:
    """Runs the walk-forward backtest, then breaks the closed trades down
    by every factor backtester.py now tags them with. Returns a single
    JSON-serializable report dict - see the shape below.
    """
    result = run_walk_forward_backtest(tickers=tickers, progress_callback=progress_callback)
    trades = result.get("trades", [])

    factors = {
        # Covers STRONG BUY / all 3 BREAKOUT BUY variants / BUY ON DIP /
        # ACCUMULATE - every fired action that can feed a Session Pick's
        # primary "signal" bucket, not only the Pre-Breakout Watchlist
        # (that's evaluated separately below, in evaluate_pre_breakout_
        # history - see module docstring).
        "by_action": _bucket_stats(trades, lambda t: t["action"]),
        "by_market_regime_at_entry": _bucket_stats(trades, lambda t: t.get("entry_regime", "unknown")),
        "by_adx_bucket": _bucket_stats(trades, lambda t: t.get("adx_bucket", "unknown")),
        "by_rsi_bucket": _bucket_stats(trades, lambda t: t.get("rsi_bucket", "unknown")),
        "by_gap_bucket": _bucket_stats(trades, lambda t: t.get("gap_bucket", "unknown")),
        "by_volume_confirmation": _bucket_stats(trades, lambda t: t.get("volume_confirmation", "unknown")),
        "by_sector": _bucket_stats(trades, lambda t: t.get("sector", "General / Diversified")),
        "by_exit_reason": _bucket_stats(trades, lambda t: t.get("exit_reason", "unknown")),
    }

    with_alpha = [t for t in trades if t.get("excess_return_pct") is not None]
    benchmark_summary = None
    if with_alpha:
        avg_excess = sum(t["excess_return_pct"] for t in with_alpha) / len(with_alpha)
        beat_benchmark = sum(1 for t in with_alpha if t["excess_return_pct"] > 0)
        benchmark_summary = {
            "primary_benchmark": PRIMARY_BENCHMARK_TICKER,
            "trades_with_benchmark_data": len(with_alpha),
            "trades_total": len(trades),
            "avg_excess_return_vs_benchmark_pct": round(avg_excess, 3),
            "pct_of_trades_that_beat_benchmark": round(beat_benchmark / len(with_alpha) * 100.0, 1),
            "reliable": len(with_alpha) >= FACTOR_BACKTEST_MIN_TRADES_RELIABLE,
        }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "walk_forward": {
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
        },
        "factors": factors,
        "benchmark": benchmark_summary,
        "min_trades_for_reliable": FACTOR_BACKTEST_MIN_TRADES_RELIABLE,
        "note": (
            "Every bucket above is reported with its raw trade count and a "
            "95% confidence interval on win rate, and flagged reliable=false "
            "below min_trades_for_reliable closed trades. A false flag does "
            "not mean the factor is bad - it means there is not yet enough "
            "closed-trade history to say either way. Treat unreliable "
            "buckets as hypotheses to keep watching, not signals to trade "
            "on; only lean on buckets flagged reliable=true when sizing or "
            "filtering real positions."
        ),
    }


# =============================================================================
# Part 2: Pre-Breakout Watchlist score validation (real logged history)
# =============================================================================

def evaluate_pre_breakout_history(min_session_date: str | None = None, dbm=None, qe=None) -> dict:
    """Validates the Pre-Breakout Watchlist's Breakout Score against
    what ACTUALLY happened afterwards, using real snapshots logged by
    decision_matrix.analyze_market() (see db_manager.
    log_breakout_watchlist_snapshot) joined against real subsequent
    price data. See module docstring for why this is a forward-
    measurement approach rather than a historical re-derivation of the
    (genuinely cross-sectional) scoring formula.

    For each snapshot, looks forward up to SESSION_PICKS_EXPECTED_DAYS
    ["short"][1] trading sessions (the same window Session Picks itself
    uses to judge a "short"-horizon pick - Pre-Breakout picks only ever
    fill "short" slots, see session_picks._candidate_pool) and checks
    whether the ticker's best close in that window reached
    SESSION_PICKS_EXPECTED_PCT["short"] above the snapshot's price - the
    same bar a real Session Pick has to clear to be marked achieved.
    Snapshots without that many subsequent trading days yet are simply
    skipped (not scored as a miss) - there's no verdict to give yet.

    Buckets scored snapshots into 5 roughly-equal groups by Breakout
    Score (lowest to highest) and reports each group's hit rate. If the
    score is doing real predictive work, hit rate should rise fairly
    steadily from the lowest to the highest quintile; if it's flat or
    noisy, the score isn't earning its keep yet, regardless of how
    reasonable any single factor inside it "sounds".
    """
    if dbm is None:
        from db_manager import DatabaseManager
        dbm = DatabaseManager()
    if qe is None:
        from analytics import QuantitativeEngine
        qe = QuantitativeEngine()

    snapshots = dbm.get_breakout_watchlist_snapshots(min_session_date)
    if not snapshots:
        return {
            "sample_count": 0,
            "reliable": False,
            "note": (
                "No Pre-Breakout Watchlist snapshots logged yet. "
                "decision_matrix.analyze_market() now logs one snapshot per "
                "run (see db_manager.log_breakout_watchlist_snapshot) - run "
                "the matrix for a while (desktop 'Execute Matrix' or the "
                "nightly publish.py) and re-run this to get real numbers "
                "instead of a guess."
            ),
        }

    horizon = "short"  # Pre-Breakout picks only ever fill "short" slots.
    target_pct = SESSION_PICKS_EXPECTED_PCT[horizon]
    lo_days, hi_days = SESSION_PICKS_EXPECTED_DAYS[horizon]

    price_cache: dict[str, dict] = {}

    def _closes_for(ticker: str) -> dict:
        if ticker not in price_cache:
            try:
                df = qe.get_ticker_data(ticker)
                price_cache[ticker] = build_close_by_date(df) if df is not None and not df.empty else {}
            except Exception as e:
                logger.warning(f"Price lookup failed for {ticker}: {e}")
                price_cache[ticker] = {}
        return price_cache[ticker]

    scored = []
    for snap in snapshots:
        closes = _closes_for(snap["ticker"])
        if not closes or snap["session_date"] not in closes:
            continue
        dates_sorted = sorted(closes.keys())
        try:
            start_i = dates_sorted.index(snap["session_date"])
        except ValueError:
            continue
        window = dates_sorted[start_i + 1: start_i + 1 + hi_days]
        if len(window) < lo_days:
            continue  # not enough subsequent trading history yet to judge this snapshot
        entry_px = closes[snap["session_date"]]
        if not entry_px or entry_px <= 0:
            continue
        window_prices = [closes[d] for d in window]
        best_gain_pct = (max(window_prices) / entry_px - 1.0) * 100.0
        scored.append({
            "ticker": snap["ticker"],
            "session_date": snap["session_date"],
            "breakout_score": snap["breakout_score"],
            "tier": snap["tier"],
            "best_gain_pct": round(best_gain_pct, 2),
            "hit_target": best_gain_pct >= target_pct,
        })

    if not scored:
        return {
            "sample_count": 0,
            "reliable": False,
            "note": (
                f"{len(snapshots)} snapshot(s) logged, but none have "
                f"{hi_days} subsequent trading sessions yet to score against. "
                "Check back once the earliest snapshots have aged past that "
                "window."
            ),
        }

    scored.sort(key=lambda r: r["breakout_score"])
    n = len(scored)
    n_quintiles = 5
    chunk_size = max(1, -(-n // n_quintiles))  # ceil division: no leftover tiny straggler chunk
    quintiles = []
    for i in range(0, n, chunk_size):
        chunk = scored[i: i + chunk_size]
        if not chunk:
            continue
        hits = sum(1 for r in chunk if r["hit_target"])
        quintiles.append({
            "score_range": [chunk[0]["breakout_score"], chunk[-1]["breakout_score"]],
            "sample_count": len(chunk),
            "hit_rate_pct": round(hits / len(chunk) * 100.0, 1),
            "avg_best_gain_pct": round(sum(r["best_gain_pct"] for r in chunk) / len(chunk), 2),
        })

    overall_hits = sum(1 for r in scored if r["hit_target"])
    return {
        "sample_count": n,
        "target_pct": target_pct,
        "window_sessions": [lo_days, hi_days],
        "overall_hit_rate_pct": round(overall_hits / n * 100.0, 1),
        "score_quintiles_low_to_high": quintiles,
        "reliable": n >= FACTOR_BACKTEST_MIN_TRADES_RELIABLE,
        "note": (
            f"Based on {n} scored snapshot(s). If hit rate rises fairly "
            "steadily from the lowest to the highest score quintile, the "
            "Breakout Score is doing real predictive work; if it's flat or "
            "noisy, treat the score as unproven regardless of how any single "
            "factor inside it 'feels'. Needs at least "
            f"{FACTOR_BACKTEST_MIN_TRADES_RELIABLE} scored snapshots before "
            "the quintile breakdown is anything more than noise - "
            f"{'reached' if n >= FACTOR_BACKTEST_MIN_TRADES_RELIABLE else 'not yet reached'}."
        ),
    }
