"""
market_regime.py
=================
Shared benchmark-index (EGX30/EGX70/...) helpers: point-in-time market
REGIME classification and per-trade EXCESS RETURN (alpha) vs. the
benchmark. Used by backtester.py (tags every backtested trade) and
factor_analysis.py (breaks results down by regime and reports the
benchmark-alpha summary), so both always agree on what "bull/bear
market" and "beat the market" mean. decision_matrix.py also uses
``normalized_benchmark_set`` alone (via db_manager.DatabaseManager.
is_benchmark_ticker) to exclude index rows from the tradeable/scored
universe, without needing anything else in this module.

See config.BENCHMARK_TICKERS / PRIMARY_BENCHMARK_TICKER /
BENCHMARK_REGIME_SMA_PERIOD / BENCHMARK_REGIME_SLOPE_LOOKBACK for the
tunables this module reads.

POINT-IN-TIME DISCIPLINE: ``build_regime_map`` is the vectorized,
whole-history version of "what was the regime at bar t" - and it is
still strictly causal. Bar t's regime depends only on sma_50 up to and
including bar t (a plain rolling mean - causal by construction, see
analytics.compute_indicators) and that same series' value
BENCHMARK_REGIME_SLOPE_LOOKBACK bars earlier, never on anything after
bar t. Safe to precompute once for a whole backtest run and index into
by date, exactly like backtester.py already does with its own
indicator frames.
"""
from __future__ import annotations

import pandas as pd

from config import (
    BENCHMARK_TICKERS,
    PRIMARY_BENCHMARK_TICKER,
    BENCHMARK_REGIME_SLOPE_LOOKBACK,
    get_logger,
)

logger = get_logger("market_regime")


def normalized_benchmark_set(dbm) -> set:
    """{normalized ticker, ...} for every symbol in config.
    BENCHMARK_TICKERS - the set decision_matrix.py / backtester.py
    filter OUT of the tradeable/backtestable universe. ``dbm`` is any
    object exposing ``normalize_symbol`` (a DatabaseManager, or
    QuantitativeEngine.dbm)."""
    return {dbm.normalize_symbol(t) for t in BENCHMARK_TICKERS}


def load_benchmark_indicators(qe, market_data_bulk: dict | None = None, ticker: str | None = None) -> pd.DataFrame:
    """Indicator-enriched DataFrame for ``ticker`` (default: config.
    PRIMARY_BENCHMARK_TICKER), or an empty DataFrame if that benchmark
    isn't present in this database yet (e.g. nobody has fed an EGX30
    CSV into market_data_feeds/ yet) - regime/alpha features are then
    silently skipped everywhere that reads this, never a hard failure.

    Pass an already-fetched ``market_data_bulk`` (e.g. backtester.py's
    own ``qe.get_all_market_data_bulk(days=None)`` call) to avoid a
    second DB round-trip; otherwise this fetches it itself.
    """
    ticker = ticker or PRIMARY_BENCHMARK_TICKER
    norm = qe.dbm.normalize_symbol(ticker)
    bulk = market_data_bulk
    if bulk is None:
        bulk = qe.get_all_market_data_bulk(days=None)
    raw = None
    for t, candidate in bulk.items():
        if qe.dbm.normalize_symbol(t) == norm:
            raw = candidate
            break
    if raw is None or raw.empty:
        logger.info(f"Benchmark '{ticker}' not found in market_data - regime/alpha features disabled for this run.")
        return pd.DataFrame()
    try:
        return qe.compute_indicators(raw.copy())
    except Exception as e:
        logger.warning(f"Benchmark indicator computation failed for '{ticker}': {e}")
        return pd.DataFrame()


def build_regime_map(df_bench_ind: pd.DataFrame) -> dict:
    """{date_str: 'bull' | 'bear' | 'neutral' | 'unknown'} for every bar
    in an indicator-enriched benchmark frame. See module docstring for
    the point-in-time guarantee and config.BENCHMARK_REGIME_* for the
    thresholds. Returns {} if ``df_bench_ind`` is empty/unusable (no
    benchmark configured or loaded) - callers should treat a missing
    key the same as an explicit 'unknown', never as 'neutral'.
    """
    if df_bench_ind is None or df_bench_ind.empty or "sma_50" not in df_bench_ind.columns:
        return {}
    lookback = BENCHMARK_REGIME_SLOPE_LOOKBACK
    close = df_bench_ind["close"]
    sma = df_bench_ind["sma_50"]
    sma_prior = sma.shift(lookback)

    regime = pd.Series("neutral", index=df_bench_ind.index)
    regime[(close > sma) & (sma > sma_prior)] = "bull"
    regime[(close < sma) & (sma < sma_prior)] = "bear"
    regime[sma_prior.isna()] = "unknown"

    # Date-only string key, robust regardless of whether pandas renders
    # the timestamp with a "T" separator, a space, or (for a clean
    # midnight timestamp) no time component at all - str(...)[:10] is
    # "YYYY-MM-DD" in every one of those cases, which a .split("T")/
    # .split(" ") approach is NOT (it silently leaves the time-of-day
    # attached whenever the separator it expects isn't the one present -
    # this bit backtester.py's own entry_date/exit_date construction
    # before it was fixed to use this same helper; see build_close_by_date).
    dates = df_bench_ind.index.astype(str).str[:10]
    return dict(zip(dates, regime))


def build_close_by_date(df_ind: pd.DataFrame) -> dict:
    """{date_str: close_price} for an indicator-enriched (or plain OHLCV)
    frame - used both for benchmark excess-return lookups here and by
    factor_analysis.evaluate_pre_breakout_history() for a single
    ticker's own forward price path. Date keys are "YYYY-MM-DD" via
    str(...)[:10] - see build_regime_map's comment for why that's the
    robust extraction (works regardless of separator/time-of-day),
    unlike a naive .split("T")/.split(" ")."""
    if df_ind is None or df_ind.empty or "close" not in df_ind.columns:
        return {}
    dates = df_ind.index.astype(str).str[:10]
    return dict(zip(dates, df_ind["close"].astype(float)))


def pct_change_between(close_by_date: dict, entry_date: str, exit_date: str) -> float | None:
    """% change between two dates in a {date_str: close} map (see
    build_close_by_date), or None if either date is missing - a missing
    benchmark return is reported as missing (no alpha computed for that
    trade), never guessed at or silently zeroed."""
    entry_px = close_by_date.get(entry_date)
    exit_px = close_by_date.get(exit_date)
    if not entry_px or entry_px <= 0 or exit_px is None:
        return None
    return round((exit_px / entry_px - 1.0) * 100.0, 3)
