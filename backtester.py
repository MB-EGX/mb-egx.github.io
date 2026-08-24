"""
backtester.py
=============
Walk-forward backtest engine (N5).

WHY THIS EXISTS
----------------
Every score/action the live app shows in the Action Matrix (decision_matrix.
analyze_market) is a claim about the future: "this classification tends to
be followed by a gain." Nothing in the app before this file ever checked
that claim against history. This module runs the SAME buy-signal rules the
live matrix uses - same thresholds, straight from config.ACTION_THRESHOLDS /
config.SCORE_WEIGHTS, so a config re-tune re-tunes both identically - against
years of historical bars, strictly point-in-time, and reports whether acting
on those signals would actually have made money.

POINT-IN-TIME DISCIPLINE (the whole point of "walk-forward")
--------------------------------------------------------------
At simulated bar t, the classifier only ever sees df.iloc[:t+1] - bar t's
own close and everything before it. It never sees bar t+1 or later. A
qualifying signal detected from bar t's CLOSE is filled at bar t+1's OPEN
(never bar t's own close - that price was already fixed by the time the
signal could have been acted on). This is what separates a real backtest
from curve-fit hindsight.

Indicators (sma/ema/rsi/adx/atr/...) are computed ONCE per ticker over its
full history rather than re-computed from scratch at every simulated bar.
This is safe - not a lookahead shortcut - because every one of
QuantitativeEngine.compute_indicators' transforms (rolling, ewm, diff,
resample+shift) is strictly causal: row t's value is a function of rows
<= t only, never of anything after t. Recomputing per-bar would give
bit-for-bit identical numbers at ~1000x the cost for a multi-year, multi-
ticker run. See analytics.compute_indicators if you ever need to verify
this claim after changing that function.

SCOPE / KNOWN SIMPLIFICATIONS
------------------------------
* Long-only, one open position per ticker at a time (mirrors how a single
  retail account actually trades this app's signals - no pyramiding).
* Take-profit uses the ATR-floor formula only (no chart_patterns.
  PatternDetector match) - running geometric pattern detection at every
  historical bar of every ticker would make a multi-year backtest
  prohibitively slow for a marginal accuracy gain on the exit target only
  (entry logic is completely unaffected). The live matrix's pattern-based
  take-profit is still exactly what a real position gets a live TP off of;
  this is a backtest-only approximation of that one number.
* "SELL / AVOID" and unconfirmed low-ADX/low-volume signals are treated as
  no-trade, matching what a disciplined trader following this app's own
  labels would actually do (the live UI marks the latter
  "(Unconfirmed: low ADX/volume)" and halves its score for the same
  reason).
* Folds are counted per-ticker (ticker A's fold 3 and ticker B's fold 3
  are each that ticker's 3rd out-of-sample test window, not necessarily
  the same calendar dates) - see _aggregate_results docstring.
* SURVIVORSHIP BIAS: the universe is "every ticker that still has market
  data today" - names that were delisted / went bust / stopped trading
  years ago are absent, so historical win rates are likely overstated
  versus what a trader could actually have done at the time. This is a
  known, unavoidable limitation of backtesting on a surviving universe
  and is why every number here is reported with its sample size and a
  "reliable" flag rather than presented as a validated edge.

USAGE
-----
    from backtester import run_walk_forward_backtest
    result = run_walk_forward_backtest(progress_callback=lambda pct, msg: ...)

``result`` is a JSON-serializable dict - see _aggregate_results for the
exact shape. Gate access with config.SUBSCRIPTION_TIERS[tier]
["include_walkforward"] at the call site (GUI menu / web API); this module
itself is tier-agnostic pure compute.
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np
import pandas as pd

from config import (
    ACTION_THRESHOLDS,
    MIN_AVG_VOLUME,
    ROUND_TRIP_FEE_PCT,
    WALK_FORWARD_BACKTEST_DEFAULTS,
    get_logger,
)
from analytics import QuantitativeEngine
from market_regime import (
    normalized_benchmark_set,
    load_benchmark_indicators,
    build_regime_map,
    build_close_by_date,
    pct_change_between,
)

logger = get_logger("backtester")

# Substring-matched against raw_action, same convention decision_matrix.py
# already uses (e.g. "⚡ BREAKOUT BUY" matches all 3 breakout sub-labels).
QUALIFYING_ACTIONS = ("STRONG BUY", "BREAKOUT BUY", "BUY ON DIP", "ACCUMULATE")


def _point_in_time_signal(df_upto: pd.DataFrame) -> dict | None:
    """Classify the LAST row of ``df_upto`` exactly like decision_matrix.
    analyze_market()'s buy-scoring block, using only rows visible up to
    and including that one. Returns None if there isn't enough history
    yet, the ticker is illiquid, the signal is SELL/AVOID, or an
    unconfirmed signal fails the same ADX/volume confirmation gate the
    live matrix applies.

    Mirrors decision_matrix.py 1:1 in the RULES (branch order/conditions)
    and is driven by the same config.ACTION_THRESHOLDS, so threshold
    tuning stays in sync automatically. If the branch LOGIC in
    decision_matrix.py ever changes (not just a threshold), mirror the
    change here too - there is no single shared function (see module
    docstring for why: analyze_market's block is entangled with
    portfolio/pattern state this pure point-in-time classifier
    deliberately doesn't depend on).
    """
    n_bars = len(df_upto)
    if n_bars < 15:
        return None

    latest = df_upto.iloc[-1]
    prev = df_upto.iloc[-2] if n_bars > 1 else latest

    curr_price = float(latest.get("close", 0.0) or 0.0)
    if curr_price <= 0:
        return None
    prev_close = float(prev.get("close", curr_price) or curr_price)
    sma50 = float(latest.get("sma_50", curr_price) or curr_price)
    prev_sma50 = float(prev.get("sma_50", prev_close) or prev_close)
    ema20 = float(latest.get("ema_20", curr_price) or curr_price)
    rsi = float(latest.get("rsi_14", 50.0) or 50.0)
    adx = float(latest.get("adx_14", 0.0) or 0.0)
    vol_ratio = float(latest.get("volume_ratio", 1.0) or 1.0)
    vol_z = float(latest.get("vol_z_score", 0.0) or 0.0)
    avg_volume_20 = float(latest.get("volume_avg", 0.0) or 0.0)
    atr = QuantitativeEngine.estimate_atr(latest, curr_price)
    vwap = float(latest.get("vwap_20", curr_price) or curr_price)
    cmf = float(latest.get("cmf_20", 0.0) or 0.0)
    is_squeezed = bool(latest.get("bb_kc_squeeze", False))
    w_sma50 = float(latest.get("w_sma_50", curr_price) or curr_price)
    w_rsi = float(latest.get("w_rsi", 50.0) or 50.0)
    weekly_aligned = (curr_price > w_sma50) and (w_rsi >= 50.0)

    is_liquid = avg_volume_20 >= MIN_AVG_VOLUME
    gap_pct = ((curr_price - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0

    lookback = min(250, n_bars)
    range_high = float(df_upto["high"].iloc[-lookback:].max())
    range_low = float(df_upto["low"].iloc[-lookback:].min())
    range_pos_pct = (
        ((curr_price - range_low) / (range_high - range_low) * 100.0)
        if (range_high - range_low) > 0
        else 50.0
    )

    ma_crossover = (prev_close <= prev_sma50) and (curr_price > sma50)
    momentum_signal = (curr_price > ema20) and (rsi >= ACTION_THRESHOLDS["breakout_momentum_rsi_min"])

    if (
        curr_price <= sma50 * ACTION_THRESHOLDS["sell_avoid_price_ratio"]
        and rsi <= ACTION_THRESHOLDS["sell_avoid_rsi_max"]
    ):
        raw_action = "SELL / AVOID"
        needs_confirmation = False
    elif (
        range_pos_pct >= ACTION_THRESHOLDS["strong_buy_range_pos_min"]
        and ACTION_THRESHOLDS["strong_buy_rsi_min"] <= rsi <= ACTION_THRESHOLDS["strong_buy_rsi_max"]
        and gap_pct >= ACTION_THRESHOLDS["strong_buy_gap_min"]
    ):
        raw_action = "STRONG BUY"
        needs_confirmation = True
    elif ma_crossover and momentum_signal and gap_pct >= ACTION_THRESHOLDS["breakout_gap_min"]:
        raw_action = "BREAKOUT BUY (X-OVER + MOMENTUM)"
        needs_confirmation = True
    elif ma_crossover and gap_pct >= ACTION_THRESHOLDS["breakout_gap_min"]:
        raw_action = "BREAKOUT BUY (X-OVER)"
        needs_confirmation = True
    elif momentum_signal and gap_pct >= ACTION_THRESHOLDS["breakout_gap_min"]:
        raw_action = "BREAKOUT BUY (MOMENTUM)"
        needs_confirmation = True
    elif (
        curr_price < sma50 * ACTION_THRESHOLDS["sell_trend_price_ratio"]
        and rsi <= ACTION_THRESHOLDS["sell_trend_rsi_max"]
        and cmf <= ACTION_THRESHOLDS["sell_trend_cmf_max"]
    ):
        raw_action = "SELL / AVOID"
        needs_confirmation = False
    elif (
        range_pos_pct <= ACTION_THRESHOLDS["buy_on_dip_range_pos_max"]
        or rsi <= ACTION_THRESHOLDS["buy_on_dip_rsi_max"]
    ):
        raw_action = "BUY ON DIP"
        needs_confirmation = False
    elif (
        range_pos_pct >= ACTION_THRESHOLDS["accumulate_range_pos_min"]
        and rsi >= ACTION_THRESHOLDS["accumulate_rsi_min"]
        and cmf >= ACTION_THRESHOLDS["accumulate_cmf_min"]
        and (curr_price >= ema20 or curr_price >= sma50)
    ):
        raw_action = "ACCUMULATE"
        needs_confirmation = False
    else:
        raw_action = "HOLD / NEUTRAL"
        needs_confirmation = False

    if not is_liquid or "SELL" in raw_action or "HOLD" in raw_action:
        return None  # backtester only ever takes the long/buy side

    strong_trend = adx >= ACTION_THRESHOLDS["strong_trend_adx_min"]
    vol_confirmed = (
        vol_ratio >= ACTION_THRESHOLDS["volume_ratio_threshold"]
        or vol_z >= ACTION_THRESHOLDS["volume_z_score_threshold"]
    )
    vwap_ok = curr_price >= vwap * ACTION_THRESHOLDS["vwap_acceptance_ratio"]
    squeeze_ok = is_squeezed if "BREAKOUT BUY" in raw_action else True
    confirmed = strong_trend and vol_confirmed and vwap_ok and squeeze_ok
    if needs_confirmation and not confirmed:
        return None

    if raw_action == "BUY ON DIP":
        dip_confirmed = weekly_aligned and cmf >= ACTION_THRESHOLDS["medium_term_cmf_min"]
        if not dip_confirmed:
            return None

    if not any(a in raw_action for a in QUALIFYING_ACTIONS):
        return None

    atr_mult = ACTION_THRESHOLDS["atr_trailing_multiplier"]
    stop_loss = max(curr_price - (atr_mult * atr), 0.0001)

    atr_floor_mult = ACTION_THRESHOLDS["take_profit_atr_floor_multiplier"]
    expected_gain = (atr * atr_floor_mult) / curr_price if curr_price > 0 else 0.03
    expected_gain = max(expected_gain, ACTION_THRESHOLDS["take_profit_pattern_floor_pct"] / 100.0)
    take_profit = curr_price * (1 + expected_gain + ROUND_TRIP_FEE_PCT)

    return {
        "raw_action": raw_action,
        "stop_loss": round(stop_loss, 4),
        "take_profit": round(take_profit, 4),
        # Snapshot of the factors that fed this classification, carried
        # through into the closed trade record by _simulate_ticker so
        # factor_analysis.py can bucket outcomes by them (ADX/RSI/gap/
        # volume-confirmation strength) - see that module's _bucket_stats.
        "adx": round(adx, 2),
        "rsi": round(rsi, 2),
        "gap_pct": round(gap_pct, 3),
        "vol_ratio": round(vol_ratio, 3),
        "vol_z": round(vol_z, 3),
    }


def _bucket_adx(adx: float) -> str:
    if adx < 20:
        return "adx<20 (weak trend)"
    if adx < 25:
        return "adx 20-25"
    if adx < 35:
        return "adx 25-35"
    return "adx>=35 (strong trend)"


def _bucket_rsi(rsi: float) -> str:
    if rsi < 40:
        return "rsi<40"
    if rsi < 55:
        return "rsi 40-55"
    if rsi < 65:
        return "rsi 55-65"
    if rsi < 75:
        return "rsi 65-75"
    return "rsi>=75"


def _bucket_gap(gap_pct: float) -> str:
    if gap_pct < -1.0:
        return "gap<-1%"
    if gap_pct < 0.0:
        return "gap -1%..0%"
    if gap_pct < 1.0:
        return "gap 0%..1%"
    if gap_pct < 3.0:
        return "gap 1%..3%"
    return "gap>=3%"


def _volume_confirmation_label(vol_ratio: float, vol_z: float) -> str:
    """How strongly volume confirmed the signal at entry. vol_ratio/vol_z
    are the same two readings ACTION_THRESHOLDS['volume_ratio_threshold']
    / ['volume_z_score_threshold'] gate confirmation on (see
    _point_in_time_signal's 'confirmed' check) - bucketed here into a
    human-readable strength label rather than a pass/fail flag, so the
    factor report can show whether MORE volume confirmation actually
    correlates with a better outcome, not just whether the minimum bar
    was cleared. BUY ON DIP / ACCUMULATE don't require confirmation to
    fire, so "unconfirmed" is a normal, valid bucket for those - not an
    error."""
    if vol_ratio >= 2.0 or vol_z >= 3.0:
        return "very strong volume"
    if vol_ratio >= 1.5 or vol_z >= 2.0:
        return "strong volume"
    if vol_ratio >= ACTION_THRESHOLDS["volume_ratio_threshold"] or vol_z >= ACTION_THRESHOLDS["volume_z_score_threshold"]:
        return "confirmed (minimum bar)"
    return "unconfirmed"


def _simulate_ticker(
    ticker: str,
    df_ind: pd.DataFrame,
    min_train_bars: int,
    test_bars: int,
    step_bars: int,
    max_hold_bars: int,
    sector: str | None = None,
    regime_map: dict | None = None,
    bench_close_by_date: dict | None = None,
) -> list[dict]:
    """Expanding walk-forward simulation for one ticker's full,
    already-indicator-enriched history. Returns every trade the strategy
    would have closed, each tagged with its (ticker-local) fold number
    and, when available, a benchmark-relative regime/alpha snapshot -
    see ``regime_map``/``bench_close_by_date`` below.

    ``sector`` : this ticker's sector name (db_manager.get_sector_map()),
    tagged onto every trade for factor_analysis.py's by-sector breakdown.
    ``regime_map`` / ``bench_close_by_date`` : precomputed once per
    backtest run by market_regime.build_regime_map / build_close_by_date
    off config.PRIMARY_BENCHMARK_TICKER's own indicator frame. Both are
    keyed by date string so they line up with THIS ticker's own trading
    calendar even when it doesn't perfectly match the benchmark's (a
    stock can have a data gap the index doesn't, or vice versa). Left
    None (both default to {}), every trade's regime/alpha fields come
    back as "unknown"/None instead of raising - a missing benchmark is a
    silently-disabled feature, never a hard failure of the backtest.
    """
    regime_map = regime_map or {}
    bench_close_by_date = bench_close_by_date or {}
    n = len(df_ind)
    trades: list[dict] = []
    fold_id = 0
    start = min_train_bars

    while start + test_bars <= n:
        fold_id += 1
        test_end = start + test_bars  # exclusive
        open_trade = None
        t = start
        while t < test_end:
            if open_trade is None:
                if t + 1 < n:  # need a next bar to fill the entry on
                    sig = _point_in_time_signal(df_ind.iloc[: t + 1])
                    if sig is not None:
                        entry_bar = df_ind.iloc[t + 1]
                        entry_price = float(entry_bar.get("open", entry_bar.get("close")) or 0.0)
                        if entry_price > 0:
                            entry_date_str = str(df_ind.index[t + 1])[:10]
                            open_trade = {
                                "ticker": ticker,
                                "fold": fold_id,
                                "action": sig["raw_action"],
                                "entry_idx": t + 1,
                                "entry_date": entry_date_str,
                                "entry_price": round(entry_price, 4),
                                "stop_loss": sig["stop_loss"],
                                "take_profit": sig["take_profit"],
                                # Initial per-share risk = distance from entry
                                # to stop - the denominator every R-multiple
                                # below is measured against (see exit branch).
                                "initial_risk": max(entry_price - sig["stop_loss"], 1e-9),
                                "min_low": entry_price,
                                "max_high": entry_price,
                                "max_exit_idx": min(t + 1 + max_hold_bars, test_end - 1, n - 1),
                                # Factor snapshot at the bar the signal
                                # fired (t, not t+1's fill bar - the
                                # classification itself was made off bar
                                # t's close) - carried into the closed
                                # trade record below for factor_analysis.py.
                                "sector": sector or "General / Diversified",
                                "adx_bucket": _bucket_adx(sig["adx"]),
                                "rsi_bucket": _bucket_rsi(sig["rsi"]),
                                "gap_bucket": _bucket_gap(sig["gap_pct"]),
                                "volume_confirmation": _volume_confirmation_label(sig["vol_ratio"], sig["vol_z"]),
                                "entry_regime": regime_map.get(entry_date_str, "unknown"),
                            }
            else:
                bar = df_ind.iloc[t]
                low = float(bar.get("low", bar.get("close")) or 0.0)
                high = float(bar.get("high", bar.get("close")) or 0.0)
                # Track the excursion envelope while the trade is open so the
                # closed record can report MAE/MFE in R (see exit branch).
                open_trade["min_low"] = min(open_trade["min_low"], low)
                open_trade["max_high"] = max(open_trade["max_high"], high)
                exit_price, exit_reason = None, None
                if low <= open_trade["stop_loss"]:
                    exit_price, exit_reason = open_trade["stop_loss"], "stop_loss"
                elif high >= open_trade["take_profit"]:
                    exit_price, exit_reason = open_trade["take_profit"], "take_profit"
                elif t >= open_trade["max_exit_idx"]:
                    exit_price = float(bar.get("close", open_trade["entry_price"]) or open_trade["entry_price"])
                    exit_reason = "max_hold" if t < test_end - 1 else "fold_end"

                if exit_price is not None:
                    gross_pct = (exit_price / open_trade["entry_price"] - 1.0) * 100.0
                    net_pct = gross_pct - (ROUND_TRIP_FEE_PCT * 100.0)
                    # R-multiple / MAE / MFE - standard "real trading"
                    # bookkeeping: every result is a multiple of the initial
                    # per-share risk (entry-to-stop distance).
                    risk = open_trade["initial_risk"]
                    r_multiple = (exit_price - open_trade["entry_price"]) / risk
                    mae_r = (open_trade["entry_price"] - open_trade["min_low"]) / risk
                    mfe_r = (open_trade["max_high"] - open_trade["entry_price"]) / risk
                    exit_date_str = str(df_ind.index[t])[:10]
                    bench_ret = pct_change_between(bench_close_by_date, open_trade["entry_date"], exit_date_str)
                    excess_return_pct = round(net_pct - bench_ret, 3) if bench_ret is not None else None
                    trades.append({
                        "ticker": open_trade["ticker"],
                        "fold": open_trade["fold"],
                        "action": open_trade["action"],
                        "entry_date": open_trade["entry_date"],
                        "entry_price": open_trade["entry_price"],
                        "exit_date": exit_date_str,
                        "exit_price": round(exit_price, 4),
                        "exit_reason": exit_reason,
                        "return_pct": round(net_pct, 3),
                        "r_multiple": round(r_multiple, 3),
                        "mae_r": round(mae_r, 3),
                        "mfe_r": round(mfe_r, 3),
                        "holding_bars": t - open_trade["entry_idx"],
                        # Factor tags (see market_regime.py / config.
                        # BENCHMARK_TICKERS) - all additive fields, safe
                        # for any existing caller of run_walk_forward_
                        # backtest() that only reads the fields above.
                        "sector": open_trade["sector"],
                        "adx_bucket": open_trade["adx_bucket"],
                        "rsi_bucket": open_trade["rsi_bucket"],
                        "gap_bucket": open_trade["gap_bucket"],
                        "volume_confirmation": open_trade["volume_confirmation"],
                        "entry_regime": open_trade["entry_regime"],
                        "benchmark_return_pct": bench_ret,
                        "excess_return_pct": excess_return_pct,
                    })
                    open_trade = None
            t += 1
        start += step_bars

    return trades


def _aggregate_results(trades: list[dict], cfg: dict, tickers_run: int) -> dict:
    """Rolls per-trade results up into per-fold and overall statistics.

    NOTE on fold alignment: fold numbers are per-ticker (ticker A's fold 3
    and ticker B's fold 3 are each that ticker's own 3rd out-of-sample
    window, counted from wherever ITS history starts) - they are NOT
    guaranteed to cover the same calendar dates across tickers with
    different history lengths. Treat the per-fold breakdown as "how did
    the Nth out-of-sample window perform, averaged across whichever
    tickers had reached their Nth window", not as a single shared
    calendar period. The overall/aggregate numbers below are calendar-
    agnostic and unaffected by this.
    """
    by_fold = defaultdict(list)
    for tr in trades:
        by_fold[tr["fold"]].append(tr)

    fold_summaries = []
    for fold_id in sorted(by_fold.keys()):
        fold_trades = by_fold[fold_id]
        returns = [t["return_pct"] / 100.0 for t in fold_trades]
        metrics = QuantitativeEngine.compute_perf_metrics(returns)
        wins = [r for r in returns if r > 0]
        fold_summaries.append({
            "fold": fold_id,
            "trade_count": len(fold_trades),
            "win_rate_pct": round(len(wins) / len(returns) * 100.0, 1) if returns else 0.0,
            "avg_return_pct": round(float(np.mean(returns)) * 100.0, 3) if returns else 0.0,
            **metrics,
        })

    n_folds = len(fold_summaries)
    all_returns = [t["return_pct"] / 100.0 for t in trades]
    overall_metrics = QuantitativeEngine.compute_perf_metrics(all_returns)
    # R-multiple bookkeeping (see _simulate_ticker): expectancy = average R
    # per trade, plus how deep trades went against us (MAE) / for us (MFE).
    r_multiples = [t["r_multiple"] for t in trades if t.get("r_multiple") is not None]
    holding_bars = [t["holding_bars"] for t in trades if t.get("holding_bars") is not None]
    avg_r = (sum(r_multiples) / len(r_multiples)) if r_multiples else None
    avg_mae = (sum(t.get("mae_r", 0.0) for t in trades) / len(trades)) if trades else None
    avg_mfe = (sum(t.get("mfe_r", 0.0) for t in trades) / len(trades)) if trades else None
    wins_overall = [r for r in all_returns if r > 0]
    gross_profit = sum(r for r in all_returns if r > 0)
    gross_loss = abs(sum(r for r in all_returns if r <= 0))
    profit_factor = (
        round(gross_profit / gross_loss, 3) if gross_loss > 1e-9
        else (None if gross_profit <= 1e-9 else float("inf"))
    )
    if profit_factor == float("inf"):
        profit_factor = None  # not JSON-serializable; None reads as "no losing trades yet"

    is_reliable = n_folds >= cfg["min_folds"]

    return {
        "config": dict(cfg),
        "tickers_backtested": tickers_run,
        "fold_count": n_folds,
        "is_reliable": is_reliable,
        "reliability_note": (
            None if is_reliable else
            f"Only {n_folds} out-of-sample fold(s) produced trades - below "
            f"min_folds={cfg['min_folds']}. Treat this result as "
            f"illustrative, not a validated edge: a handful of folds can "
            f"look good or bad by chance alone."
        ),
        "trade_count": len(trades),
        "win_rate_pct": round(len(wins_overall) / len(all_returns) * 100.0, 1) if all_returns else 0.0,
        "avg_return_pct": round(float(np.mean(all_returns)) * 100.0, 3) if all_returns else 0.0,
        "avg_r_multiple": round(avg_r, 3) if avg_r is not None else None,
        "expectancy_r": round(avg_r, 3) if avg_r is not None else None,
        "avg_mae_r": round(avg_mae, 3) if avg_mae is not None else None,
        "avg_mfe_r": round(avg_mfe, 3) if avg_mfe is not None else None,
        "avg_holding_bars": round(sum(holding_bars) / len(holding_bars), 2) if holding_bars else None,
        "profit_factor": profit_factor,
        "overall": overall_metrics,
        "folds": fold_summaries,
        "trades": trades,
    }


def run_walk_forward_backtest(tickers: list[str] | None = None, progress_callback=None) -> dict:
    """Runs the walk-forward backtest across ``tickers`` (normalized or
    raw symbols; None = every ticker with market data) and returns the
    aggregated, JSON-serializable result dict from ``_aggregate_results``.
    """
    cfg = WALK_FORWARD_BACKTEST_DEFAULTS
    qe = QuantitativeEngine()
    bulk = qe.get_all_market_data_bulk(days=None)

    # Load the benchmark (config.PRIMARY_BENCHMARK_TICKER, e.g. EGX30)
    # BEFORE excluding benchmark rows from ``bulk`` below - it has to
    # come from the same bulk fetch. A missing/unconfigured benchmark
    # degrades gracefully: regime_map/bench_close_by_date come back {},
    # every trade's entry_regime reads "unknown" and its benchmark_
    # return_pct/excess_return_pct read None - the walk-forward result
    # itself (win rate, avg return, ...) is completely unaffected.
    bench_df_ind = load_benchmark_indicators(qe, market_data_bulk=bulk)
    regime_map = build_regime_map(bench_df_ind)
    bench_close_by_date = build_close_by_date(bench_df_ind)

    # Never backtest an index/benchmark feed as if it were a tradeable
    # stock (see config.BENCHMARK_TICKERS) - it would generate
    # meaningless "trades" on a number nobody can actually buy or sell.
    benchmark_norms = normalized_benchmark_set(qe.dbm)
    bulk = {t: df for t, df in bulk.items() if qe.dbm.normalize_symbol(t) not in benchmark_norms}

    if tickers:
        wanted = {qe.dbm.normalize_symbol(t) for t in tickers} | set(tickers)
        bulk = {t: df for t, df in bulk.items() if t in wanted or qe.dbm.normalize_symbol(t) in wanted}

    sector_map = qe.dbm.get_sector_map()

    min_bars_needed = cfg["min_train_bars"] + cfg["test_bars"]
    all_trades: list[dict] = []
    tickers_run = 0
    items = list(bulk.items())
    total = len(items)

    for idx, (ticker, df) in enumerate(items):
        if progress_callback and idx % 5 == 0:
            progress_callback(int(idx / max(total, 1) * 95), f"Walk-forward: {ticker} ({idx}/{total})...")
        if df is None or df.empty or len(df) < min_bars_needed:
            continue
        try:
            df_ind = qe.compute_indicators(df)
        except Exception as e:
            logger.warning(f"Backtest indicator computation failed for {ticker}: {e}")
            continue
        if df_ind.empty:
            continue
        norm_ticker = qe.dbm.normalize_symbol(ticker)
        sector = sector_map.get(norm_ticker, sector_map.get(ticker, "General / Diversified"))
        trades = _simulate_ticker(
            ticker, df_ind,
            cfg["min_train_bars"], cfg["test_bars"], cfg["step_bars"], cfg["max_hold_bars"],
            sector=sector, regime_map=regime_map, bench_close_by_date=bench_close_by_date,
        )
        all_trades.extend(trades)
        tickers_run += 1

    if progress_callback:
        progress_callback(97, "Aggregating fold statistics...")

    result = _aggregate_results(all_trades, cfg, tickers_run)

    if progress_callback:
        progress_callback(100, "Walk-forward backtest complete.")

    return result
