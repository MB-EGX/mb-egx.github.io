"""
market_regime.py
=================
Shared benchmark-index (EGX30, EGX70 EWI, EGX100 EWI, EGX30 Capped,
EGX33 Shariah, and the per-sector EGX indices - Banks, Real Estate,
Building Materials, Basic Resources, Food & Beverages, Education
Services, Trade & Distributors, Shipping & Transportation, Non-Bank
Financial Services, Consulting Engineers, Travel & Leisure, Health
Care, Industrial Goods & Services - see config.BENCHMARK_TICKERS)
helpers: point-in-time market REGIME classification and per-trade
EXCESS RETURN (alpha) vs. a benchmark. Used by backtester.py (tags
every backtested trade) and factor_analysis.py (breaks results down by
regime and reports the benchmark-alpha summary), so both always agree
on what "bull/bear market" and "beat the market" mean.

decision_matrix.py's LIVE run (analyze_market) also consults this
module directly now - both for the single PRIMARY_BENCHMARK_TICKER
market-wide regime (see ``live_regime_snapshot``) and for whichever
per-sector index (config.SECTOR_BENCHMARK_MAP) applies to a given
ticker's sector, for the Pre-Breakout Watchlist's "Sector Index RS"
factor. It also still uses ``normalized_benchmark_set`` alone (via
db_manager.DatabaseManager.is_benchmark_ticker) to exclude every index
row above from the tradeable/scored universe.

See config.BENCHMARK_TICKERS / BENCHMARK_LABELS / SECTOR_BENCHMARK_MAP /
PRIMARY_BENCHMARK_TICKER / BENCHMARK_REGIME_SMA_PERIOD /
BENCHMARK_REGIME_SLOPE_LOOKBACK for the tunables this module reads.

POINT-IN-TIME DISCIPLINE: ``build_regime_map`` is the vectorized,
whole-history version of "what was the regime at bar t" - and it is
still strictly causal. Bar t's regime depends only on sma_50 up to and
including bar t (a plain rolling mean - causal by construction, see
analytics.compute_indicators) and that same series' value
BENCHMARK_REGIME_SLOPE_LOOKBACK bars earlier, never on anything after
bar t. Safe to precompute once for a whole backtest run and index into
by date, exactly like backtester.py already does with its own
indicator frames. ``live_regime_snapshot`` below reuses the exact same
function for the live, single-latest-bar case - it does not duplicate
the regime logic, just reads the last entry of the same map.
"""
from __future__ import annotations

import pandas as pd

from config import (
    BENCHMARK_TICKERS,
    BENCHMARK_LABELS,
    SECTOR_BENCHMARK_MAP,
    PRIMARY_BENCHMARK_TICKER,
    BENCHMARK_REGIME_SLOPE_LOOKBACK,
    get_logger,
)

logger = get_logger("market_regime")


def benchmark_label(ticker: str) -> str:
    """Human-readable name for a raw benchmark ticker (config.
    BENCHMARK_LABELS), e.g. "EGBANK" -> "EGX Banks". Falls back to the
    raw ticker itself if it's ever added to config.BENCHMARK_TICKERS
    without a matching label - never a hard failure over a display
    string."""
    return BENCHMARK_LABELS.get(str(ticker).upper(), str(ticker))


def get_sector_benchmark_ticker(sector_name: str) -> str | None:
    """The EGX sub-index ticker (raw, as in config.BENCHMARK_TICKERS)
    that tracks ``sector_name`` (this app's own sector classification -
    see db_manager.get_sector_map), or None if that sector has no
    dedicated index in config.SECTOR_BENCHMARK_MAP. Callers should treat
    None as "fall back to PRIMARY_BENCHMARK_TICKER or skip", never as an
    error."""
    return SECTOR_BENCHMARK_MAP.get(sector_name)


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


def load_all_benchmark_indicators(qe, market_data_bulk: dict | None = None,
                                   tickers: list[str] | None = None) -> dict:
    """Indicator-enriched DataFrame for EVERY ticker in ``tickers``
    (default: config.BENCHMARK_TICKERS - i.e. the full EGX30/70 EWI/100
    EWI/30 Capped/33 Shariah + per-sector index list), keyed by the
    NORMALIZED ticker. One bulk fetch shared across every benchmark
    (same ``market_data_bulk`` pass-through as ``load_benchmark_
    indicators``) instead of a separate DB round-trip per index.

    A benchmark whose data hasn't been ingested yet is simply absent
    from the returned dict (see ``load_benchmark_indicators``'s own
    graceful-empty behavior) - callers should treat a missing key as
    "not available for this run", never as an error. This is the
    live-run counterpart to what backtester.py/factor_analysis.py do
    for the single PRIMARY_BENCHMARK_TICKER; decision_matrix.py uses
    this to compute the market-wide regime AND every configured
    per-sector index's regime/relative-strength in one pass.
    """
    tickers = tickers or BENCHMARK_TICKERS
    bulk = market_data_bulk
    if bulk is None:
        bulk = qe.get_all_market_data_bulk(days=None)
    out = {}
    for ticker in tickers:
        norm = qe.dbm.normalize_symbol(ticker)
        df_ind = load_benchmark_indicators(qe, market_data_bulk=bulk, ticker=ticker)
        if df_ind is not None and not df_ind.empty:
            out[norm] = df_ind
    return out


def live_regime_snapshot(benchmark_indicator_frames: dict) -> dict:
    """For each {normalized_ticker: indicator_frame} pair (see
    ``load_all_benchmark_indicators``), returns the CURRENT (most
    recent bar) regime + close + as-of date - the live-run equivalent
    of what backtester.py does per-historical-bar via ``build_regime_
    map``, reusing that exact same function rather than re-deriving the
    bull/bear/neutral logic:

        {
          "EGX30.CA": {"regime": "bull", "close": 30412.5, "as_of": "2026-08-16", "label": "EGX 30"},
          "EGBANK.CA": {"regime": "neutral", "close": 4488.2, "as_of": "2026-08-16", "label": "EGX Banks"},
          ...
        }

    A benchmark with no usable regime yet (too little history for the
    SMA/slope window) is simply absent - same "missing means unknown,
    never a fabricated 'neutral'" contract as ``build_regime_map``.
    """
    snapshot = {}
    for norm_ticker, df_ind in benchmark_indicator_frames.items():
        regime_map = build_regime_map(df_ind)
        if not regime_map:
            continue
        last_date = max(regime_map.keys())
        try:
            last_close = float(df_ind["close"].iloc[-1])
        except (IndexError, KeyError, ValueError, TypeError):
            last_close = None
        snapshot[norm_ticker] = {
            "regime": regime_map[last_date],
            "close": last_close,
            "as_of": last_date,
            "label": benchmark_label(norm_ticker.replace(".CA", "")),
        }
    return snapshot
