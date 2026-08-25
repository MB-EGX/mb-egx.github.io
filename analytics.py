"""
analytics.py
============
The analytical brain of the engine. Pure compute, no GUI imports.

Public surface:
    QuantitativeEngine.get_ticker_data / get_all_market_data_bulk
    QuantitativeEngine.get_latest_price_and_atr
    QuantitativeEngine.data_confidence_tier
    QuantitativeEngine.compute_indicators
    QuantitativeEngine.compute_sector_analytics
    QuantitativeEngine.compute_trendline
    QuantitativeEngine.get_sector_historical_index
    QuantitativeEngine.match_historical_patterns
    QuantitativeEngine.estimate_days_to_target
"""
from __future__ import annotations

import math

# MUST be imported before numpy/pandas (below) - sets OPENBLAS/MKL/OMP/
# NUMEXPR thread caps as a module-level side effect; those only take
# effect if set before numpy/pandas load anywhere in this process. See
# config.py's module docstring.
from config import (
    MIN_BARS_FOR_PATTERN_TRUST,
    SR_LOOKBACK_BARS,
    SR_SWING_ORDER,
    SR_CLUSTER_TOLERANCE_PCT,
    SR_MIN_TOUCHES,
    SR_RECENCY_HALF_LIFE_DAYS,
    SR_MAX_LEVELS,
    TRADING_DAYS_PER_YEAR,
    ANNUALIZED_RISK_FREE_RATE,
    DEFAULT_ATR_PCT_FALLBACK,
    TICKER_REGIME_THRESHOLDS,
)

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view
from scipy.signal import argrelextrema
from scipy.stats import ttest_1samp

from db_manager import DatabaseManager, clean_sector_name


class QuantitativeEngine:
    def __init__(self, connect_db: bool = True):
        # REMEDIATION: when launched inside a ProcessPoolExecutor worker we
        # must NOT open a DuckDB connection in the child (each worker already
        # has its own process and would otherwise contend on the file lock).
        self.dbm = DatabaseManager() if connect_db else None

    # -------------------------------------------------------------------------
    # Data hygiene
    # -------------------------------------------------------------------------
    @staticmethod
    def _clean_price_series(df: pd.DataFrame) -> pd.DataFrame:
        """Trim history to the most recent "clean" window.

        Either compares the supplied prior-close column to the recorded shift
        of close (split/dividend detection) or, if no such column exists,
        looks for a >25-day gap. Requires at least 15 bars in the trimmed
        window; otherwise returns the input unchanged.
        """
        if df.empty or len(df) <= 1:
            return df

        prev_col = next(
            (col for col in ["prev", "prev_close", "prev.", "previous_close"] if col in df.columns),
            None,
        )
        if prev_col:
            recorded_prior_close = df["close"].shift(1)
            price_mismatch = ~np.isclose(df[prev_col], recorded_prior_close, atol=0.05)
            price_mismatch.iloc[0] = False
            if price_mismatch.any():
                last_break_date = df[price_mismatch].index[-1]
                temp_df = df.loc[last_break_date:]
                if len(temp_df) >= 15:
                    df = temp_df
        else:
            gap_days = df.index.to_series().diff().dt.days
            if (gap_days > 25).any():
                last_major_gap_date = gap_days[gap_days > 25].index[-1]
                temp_df = df.loc[last_major_gap_date:]
                if len(temp_df) >= 15:
                    df = temp_df

        return df

    # -------------------------------------------------------------------------
    # Bulk fetchers
    # -------------------------------------------------------------------------
    def get_ticker_data(self, ticker: str) -> pd.DataFrame:
        if not self.dbm:
            self.dbm = DatabaseManager()
        with self.dbm.get_connection() as conn:
            query = "SELECT * FROM market_data WHERE ticker = ? ORDER BY date ASC;"
            df = conn.cursor().execute(query, [ticker]).fetchdf()

        if df.empty:
            return df

        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
        return self._clean_price_series(df)

    def get_all_market_data_bulk(self, days: int | None = None) -> dict:
        """Return ``{ticker: cleaned_df}`` for the whole market.

        Parameters
        ----------
        days : int, optional
            If provided (and positive), only fetch rows from the last ``days``
            days of the most recent market date. The date filter is pushed
            down into DuckDB so the worker does not pay RAM/transfer cost for
            ancient bars. Used by ``export_json.build_chart_history`` to avoid
            recomputing indicators on 5-year-old data.
        """
        if not self.dbm:
            self.dbm = DatabaseManager()
        with self.dbm.get_connection() as conn:
            try:
                if days and isinstance(days, int) and days > 0:
                    latest_row = conn.cursor().execute(
                        "SELECT MAX(date) FROM market_data;"
                    ).fetchone()
                    if not latest_row or not latest_row[0]:
                        return {}
                    cutoff = (
                        pd.Timestamp(latest_row[0]) - pd.Timedelta(days=days)
                    ).strftime("%Y-%m-%d")
                    query = (
                        "SELECT * FROM market_data "
                        "WHERE date >= ? "
                        "ORDER BY ticker ASC, date ASC;"
                    )
                    df = conn.cursor().execute(query, [cutoff]).fetchdf()
                else:
                    query = (
                        "SELECT * FROM market_data ORDER BY ticker ASC, date ASC;"
                    )
                    df = conn.cursor().execute(query).fetchdf()
            except Exception:
                return {}
        if df.empty:
            return {}
        df["date"] = pd.to_datetime(df["date"])
        grouped: dict = {}
        for ticker, group in df.groupby("ticker"):
            g_df = group.set_index("date")
            cleaned_df = self._clean_price_series(g_df)
            if not cleaned_df.empty:
                grouped[ticker] = cleaned_df
        return grouped

    def get_latest_price_and_atr(self, ticker: str) -> tuple:
        df = self.get_ticker_data(ticker)
        if df.empty or len(df) < 5:
            return 0.0, 0.0
        df = self.compute_indicators(df)
        latest = df.iloc[-1]
        price = float(latest.get("close", 0.0))
        atr = self.estimate_atr(latest, price)
        return price, atr

    @staticmethod
    def estimate_atr(latest_row, price: float) -> float:
        """The single source of truth for the "real ATR, or a documented
        fallback" decision used everywhere in the app that needs a stop/
        target distance (decision_matrix.py, backtester.py). See
        config.DEFAULT_ATR_PCT_FALLBACK's docstring: compute_indicators()
        always produces a real Wilder atr_14 for any ticker with 3+ bars,
        so this only fires on the genuine zero-true-range edge case
        (an illiquid name with no observed price movement in its ATR
        window) - never as a routine substitute for real ATR."""
        atr = latest_row.get("atr_14", None) if latest_row is not None else None
        if atr is None or pd.isna(atr) or atr <= 0:
            return price * DEFAULT_ATR_PCT_FALLBACK
        return float(atr)

    # -------------------------------------------------------------------------
    # Confidence labelling
    # -------------------------------------------------------------------------
    @staticmethod
    def data_confidence_tier(n_bars: int) -> str:
        if n_bars < 20:
            return "Very Low (New/Short History)"
        elif n_bars < 60:
            return "Low (<3 Months)"
        elif n_bars < 150:
            return "Medium (<1 Year)"
        else:
            return "High (1Y+)"

    @staticmethod
    def classify_regime(df: pd.DataFrame) -> str:
        if df is None or df.empty or len(df) < 20:
            return "Insufficient Data"
        latest = df.iloc[-1]
        adx = float(latest.get("adx_14", 0.0) or 0.0)
        atr = float(latest.get("atr_14", 0.0) or 0.0)
        close = float(latest.get("close", 0.0) or 0.0)
        atr_pct = (atr / close) * 100 if close > 0 else 0.0
        adx_min = TICKER_REGIME_THRESHOLDS["adx_trending_min"]
        atr_volatile_min = TICKER_REGIME_THRESHOLDS["atr_pct_volatile_min"]
        atr_range_volatile_min = TICKER_REGIME_THRESHOLDS["atr_pct_range_volatile_min"]
        if adx >= adx_min and atr_pct >= atr_volatile_min:
            return "Trending / Volatile"
        if adx >= adx_min:
            return "Trending"
        if atr_pct >= atr_range_volatile_min:
            return "Volatile Range"
        return "Range / Consolidation"

    @staticmethod
    def compute_perf_metrics(returns: list[float]) -> dict:
        arr = np.array([float(r) for r in returns if pd.notna(r)], dtype=float)
        if arr.size == 0:
            return {"mean": 0.0, "vol": 0.0, "sharpe": 0.0, "sortino": 0.0, "sharpe_daily": 0.0, "sortino_daily": 0.0, "max_drawdown": 0.0}
        mean = float(arr.mean())
        vol = float(arr.std())
        downside = arr[arr < 0]
        downside_dev = float(downside.std()) if downside.size else 0.0
        equity = np.cumprod(1.0 + arr)
        peaks = np.maximum.accumulate(equity)
        drawdown = np.where(peaks > 0, (equity - peaks) / peaks, 0.0)
        # Annualized Sharpe/Sortino (standard convention): scale the raw
        # per-bar ratio by sqrt(TRADING_DAYS_PER_YEAR) and subtract the
        # per-bar risk-free rate. Raw per-bar ratios are kept as *_daily.
        scale = math.sqrt(TRADING_DAYS_PER_YEAR)
        rf_daily = (1.0 + ANNUALIZED_RISK_FREE_RATE) ** (1.0 / TRADING_DAYS_PER_YEAR) - 1.0
        excess = mean - rf_daily
        sharpe_daily = (excess / vol) if vol > 1e-9 else 0.0
        sortino_daily = (excess / downside_dev) if downside_dev > 1e-9 else 0.0
        return {
            "mean": round(mean, 6),
            "vol": round(vol, 6),
            "sharpe_daily": round(sharpe_daily, 4),
            "sortino_daily": round(sortino_daily, 4),
            "sharpe": round(sharpe_daily * scale, 4),
            "sortino": round(sortino_daily * scale, 4),
            "max_drawdown": round(float(drawdown.min()) if drawdown.size else 0.0, 6),
        }

    @staticmethod
    def validate_indicator_outputs(df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty:
            return df
        for col in ("rsi_14", "adx_14", "atr_14", "macd", "macd_signal", "macd_histogram"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        if "rsi_14" in df.columns:
            df["rsi_14"] = df["rsi_14"].clip(lower=0, upper=100).fillna(50.0)
        if "adx_14" in df.columns:
            df["adx_14"] = df["adx_14"].clip(lower=0, upper=100).fillna(0.0)
        if "atr_14" in df.columns:
            df["atr_14"] = df["atr_14"].clip(lower=0).fillna(0.0)
        if {"macd", "macd_signal", "macd_histogram"}.issubset(df.columns):
            df["macd_histogram"] = (df["macd"] - df["macd_signal"]).fillna(0.0)
        return df

    # -------------------------------------------------------------------------
    # ADX with proper Wilder smoothing
    # -------------------------------------------------------------------------
    @staticmethod
    def _compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
        """Wilder-smoothed ADX (the textbook implementation).

        Wilder's smoothing is equivalent to ``ewm(alpha=1/period, adjust=False)``.
        The previous implementation used a plain rolling mean for the ADX
        itself, which under-weights recent DX values and produces a lagged,
        over-smoothed trend-strength reading. This version matches TradingView,
        TA-Lib, and most institutional platforms.
        """
        period = max(2, min(period, len(df) - 1)) if len(df) > 2 else 1
        high, low, close = df["high"], df["low"], df["close"]

        up_move = high.diff()
        down_move = -low.diff()

        plus_dm = pd.Series(
            np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
            index=df.index,
        )
        minus_dm = pd.Series(
            np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
            index=df.index,
        )

        tr1 = high - low
        tr2 = (high - close.shift()).abs()
        tr3 = (low - close.shift()).abs()
        tr = pd.Series(np.maximum(np.maximum(tr1, tr2), tr3), index=df.index)

        # Wilder's recursive smoothing
        wilder_alpha = 1.0 / period
        sm_tr = tr.ewm(alpha=wilder_alpha, adjust=False, min_periods=1).mean()
        sm_plus_dm = plus_dm.ewm(alpha=wilder_alpha, adjust=False, min_periods=1).mean()
        sm_minus_dm = minus_dm.ewm(alpha=wilder_alpha, adjust=False, min_periods=1).mean()

        plus_di = 100 * sm_plus_dm / sm_tr.replace(0, np.nan)
        minus_di = 100 * sm_minus_dm / sm_tr.replace(0, np.nan)

        dx = (
            100
            * (plus_di - minus_di).abs()
            / (plus_di + minus_di).replace(0, np.nan)
        )
        # Returns (adx, plus_di, minus_di) so downstream code can use the
        # actual Wilder direction (which side is moving the market) rather
        # than inferring it from price-vs-SMA. +DI > -DI is, by Wilder's
        # own definition, the bullish crossover; -DI > +DI the bearish.
        adx = dx.ewm(alpha=wilder_alpha, adjust=False, min_periods=1).mean().fillna(0)
        return adx, plus_di.fillna(0), minus_di.fillna(0)

    # -------------------------------------------------------------------------
    # Master indicator pipeline
    # -------------------------------------------------------------------------
    @staticmethod
    def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty or len(df) < 3:
            return df

        n = len(df)

        df["sma_50"] = df["close"].rolling(window=min(50, n), min_periods=1).mean()
        df["sma_200"] = df["close"].rolling(window=min(200, n), min_periods=1).mean()
        df["ema_20"] = df["close"].ewm(span=min(20, n), adjust=False).mean()

        delta = df["close"].diff()
        gain = delta.where(delta > 0, 0.0)
        loss = -delta.where(delta < 0, 0.0)

        avg_gain = gain.ewm(alpha=1 / min(14, n), min_periods=1, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / min(14, n), min_periods=1, adjust=False).mean()

        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi_raw = 100 - (100 / (1 + rs))
        df["rsi_14"] = np.where((avg_loss == 0) & (avg_gain > 0), 100.0, rsi_raw)
        df["rsi_14"] = pd.Series(df["rsi_14"], index=df.index).fillna(50.0)

        ema_12 = df["close"].ewm(span=min(12, n), adjust=False).mean()
        ema_26 = df["close"].ewm(span=min(26, n), adjust=False).mean()
        df["macd"] = ema_12 - ema_26
        df["macd_signal"] = df["macd"].ewm(span=min(9, n), adjust=False).mean()
        df["macd_histogram"] = df["macd"] - df["macd_signal"]

        high_low = df["high"] - df["low"]
        high_close = (df["high"] - df["close"].shift()).abs()
        low_close = (df["low"] - df["close"].shift()).abs()
        true_range = pd.Series(
            np.maximum(np.maximum(high_low, high_close), low_close), index=df.index
        )
        # First true range is NaN (close.shift() at index 0); the textbook
        # convention sets TR[0] = high - low so the Wilder ATR seed (the SMA
        # of the first `period` true ranges) is exact.
        if pd.isna(true_range.iloc[0]):
            true_range.iloc[0] = float(high_low.iloc[0])
        # Wilder-smoothed ATR (the textbook implementation, matching
        # TradingView / TA-Lib / StockCharts): the first ATR is the simple
        # mean of the first `period` true ranges, then the recursion
        # ATR_t = (ATR_{t-1}*(period-1) + TR_t)/period. A plain rolling
        # mean (the old code) under-weights recent true ranges and produces
        # a lagged, over-smoothed volatility reading. Implemented as an
        # ewm(alpha=1/period) with the seed corrected to that SMA - exact
        # Wilder, vectorized.
        atr_period = min(14, n)
        if atr_period >= 2 and len(true_range) >= atr_period:
            alpha = 1.0 / atr_period
            atr = true_range.ewm(alpha=alpha, adjust=False, min_periods=1).mean()
            seed = float(true_range.iloc[:atr_period].mean())
            start = atr_period - 1
            decay = (1.0 - alpha) ** np.arange(len(true_range) - start)
            atr.iloc[start:] = atr.iloc[start:].values + (seed - atr.iloc[start]) * decay
            df["atr_14"] = atr.fillna(0)
        else:
            df["atr_14"] = true_range.rolling(window=atr_period, min_periods=1).mean().fillna(0)

        bb_mean = df["close"].rolling(window=min(20, n), min_periods=1).mean()
        bb_std = df["close"].rolling(window=min(20, n), min_periods=1).std().fillna(0)
        df["bb_upper"] = bb_mean + (2 * bb_std)
        df["bb_lower"] = bb_mean - (2 * bb_std)
        bb_range = (df["bb_upper"] - df["bb_lower"]).replace(0, np.nan)
        df["bb_percent_b"] = ((df["close"] - df["bb_lower"]) / bb_range).fillna(0.5)

        kc_mean = df["ema_20"]
        df["kc_upper"] = kc_mean + (1.5 * df["atr_14"])
        df["kc_lower"] = kc_mean - (1.5 * df["atr_14"])
        df["bb_kc_squeeze"] = (df["bb_upper"] <= df["kc_upper"]) & (
            df["bb_lower"] >= df["kc_lower"]
        )

        df["volume_avg"] = df["volume"].rolling(window=min(20, n), min_periods=1).mean()
        df["volume_ratio"] = df["volume"] / df["volume_avg"].replace(0, np.nan)
        df["volume_ratio"] = df["volume_ratio"].fillna(1.0)

        vol_std = df["volume"].rolling(window=min(20, n), min_periods=1).std()
        vol_std = vol_std.where(vol_std > 1e-8, np.nan)
        df["vol_z_score"] = ((df["volume"] - df["volume_avg"]) / vol_std).fillna(0.0)

        typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
        tp_vol = typical_price * df["volume"]
        df["vwap_20"] = (
            tp_vol.rolling(window=min(20, n), min_periods=1).sum()
            / df["volume"].rolling(window=min(20, n), min_periods=1).sum().replace(
                0, np.nan
            )
        )
        df["vwap_20"] = df["vwap_20"].fillna(df["close"])

        hl_range = (df["high"] - df["low"]).replace(0, np.nan)
        mf_multiplier = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / hl_range
        mf_volume = mf_multiplier.fillna(0.0) * df["volume"]
        df["cmf_20"] = (
            mf_volume.rolling(window=min(20, n), min_periods=1).sum()
            / df["volume"].rolling(window=min(20, n), min_periods=1).sum().replace(
                0, np.nan
            )
        )
        df["cmf_20"] = df["cmf_20"].fillna(0.0)

        # Weekly alignment
        try:
            weekly_df = (
                df.resample("W-FRI")
                .agg(
                    {
                        "open": "first",
                        "high": "max",
                        "low": "min",
                        "close": "last",
                        "volume": "sum",
                    }
                )
                .dropna()
            )
            if len(weekly_df) >= 10:
                weekly_df["w_sma_50"] = (
                    weekly_df["close"]
                    .rolling(window=min(50, len(weekly_df)), min_periods=1)
                    .mean()
                )
                delta_w = weekly_df["close"].diff()
                gain_w = delta_w.where(delta_w > 0, 0.0)
                loss_w = -delta_w.where(delta_w < 0, 0.0)

                avg_gain_w = gain_w.ewm(
                    alpha=1 / min(14, len(weekly_df)), min_periods=1, adjust=False
                ).mean()
                avg_loss_w = loss_w.ewm(
                    alpha=1 / min(14, len(weekly_df)), min_periods=1, adjust=False
                ).mean()

                rs_w = avg_gain_w / avg_loss_w.replace(0, np.nan)
                weekly_df["w_rsi"] = np.where(
                    (avg_loss_w == 0) & (avg_gain_w > 0),
                    100.0,
                    100 - (100 / (1 + rs_w)),
                )
                weekly_df["w_rsi"] = pd.Series(
                    weekly_df["w_rsi"], index=weekly_df.index
                ).fillna(50.0)

                # Shift(1): only use LAST COMPLETED weekly bar
                weekly_df[["w_sma_50", "w_rsi"]] = weekly_df[["w_sma_50", "w_rsi"]].shift(1)

                df["w_sma_50"] = (
                    weekly_df["w_sma_50"]
                    .reindex(df.index, method="ffill")
                    .fillna(df["sma_50"])
                )
                df["w_rsi"] = (
                    weekly_df["w_rsi"]
                    .reindex(df.index, method="ffill")
                    .fillna(df["rsi_14"])
                )
            else:
                df["w_sma_50"] = df["sma_50"]
                df["w_rsi"] = df["rsi_14"]
        except Exception:
            df["w_sma_50"] = df["sma_50"]
            df["w_rsi"] = df["rsi_14"]

        # _compute_adx now returns (adx, plus_di, minus_di) — standard
        # per Wilder (1978) / Investopedia / TradingView: +DI > -DI is the
        # bullish crossover. Persist both so the matrix and downstream
        # breakers can use real Wilder direction instead of guessing via
        # price-vs-SMA (which the previous code used).
        adx, plus_di, minus_di = QuantitativeEngine._compute_adx(df, period=min(14, n))
        df["adx_14"] = adx
        df["plus_di"] = plus_di
        df["minus_di"] = minus_di
        trending = adx >= 20
        rsi = df["rsi_14"]

        # Standard ADX-14 interpretation bands (Wilder 1978, Investopedia,
        # CMC Markets, TradingView). The previous code collapsed adx 20-25
        # (Forming) into the "Strong" bucket, and infered direction from
        # price-vs-SMA200 instead of +DI/-DI — sub-25 names got mislabeled
        # "Strong Bullish" (e.g. GRCA.CA ADX 21.2, EGAL.CA ADX 24.5).
        # Direction is now taken from the actual DI cross with a 0.5pp
        # tolerance band to absorb rounding noise; ties default to
        # consolidation. Updated band labels are documented in
        # glossary_content.py separately.
        strong = adx >= 25
        di_diff = plus_di - minus_di
        bullish = di_diff >= 0.5
        bearish = di_diff <= -0.5

        conditions = [
            (adx >= 75) & bullish,
            (adx >= 75) & bearish,
            (adx >= 50) & bullish,
            (adx >= 50) & bearish,
            (adx >= 25) & bullish,
            (adx >= 25) & bearish,
            (adx >= 20) & bullish,
            (adx >= 20) & bearish,
            (rsi >= 40) & (rsi <= 60),
        ]
        choices = [
            "Extremely Strong Bullish Trend",
            "Extremely Strong Bearish Trend",
            "Very Strong Bullish Trend",
            "Very Strong Bearish Trend",
            "Strong Bullish Trend",
            "Strong Bearish Trend",
            "Weak Bullish (Forming)",
            "Weak Bearish (Forming)",
            "Consolidation / Neutral",
        ]
        # NB: the seven legacy string choices ("Strong Bullish", "Weak
        # Bullish (Low Trend Strength)", "Weak Bullish", "Consolidation /
        # Neutral", "Strong Bearish", "Weak Bearish (Low Trend Strength)",
        # "Weak Bearish") are subsumed by the new band-x-direction labels;
        # downstream callers only use these as display strings (decision_
        # matrix.reads trend_class as-is, app_gui/index.html render it
        # raw) so no consumer break. Glossary entries for the three new
        # strength-graduated labels ("Extremely/ Very Strong ... Trend")
        # are a separate cosmetic follow-up.
        df["trend_class"] = np.select(conditions, choices, default="Range / Consolidation")
        return QuantitativeEngine.validate_indicator_outputs(df)

    # -------------------------------------------------------------------------
    # Sector roll-up
    # -------------------------------------------------------------------------
    def compute_sector_analytics(
        self, processed_tickers_data: dict, sector_map: dict, session_date_str: str | None = None
    ) -> list:
        from config import ACTION_THRESHOLDS  # local import to avoid cycles at module load

        sector_groups: dict = {}

        for norm_ticker, df in processed_tickers_data.items():
            if df.empty or len(df) < 5:
                continue

            # ROOT-CAUSE FIX: a ticker whose feed stopped updating days/
            # weeks ago still sits in processed_tickers_data with old bars.
            # df.iloc[-1]/iloc[-2] below don't know that - without this
            # check, a stale ticker's real (multi-session) move gets
            # counted as this sector's "1D Return", can crown it "Sector
            # Leader", and drags the sector's breadth/CMF average off a
            # number that has nothing to do with this session. Skipped
            # entirely from the sector's stats (not just zeroed) so it
            # can't quietly pull the average toward itself either.
            # session_date_str is optional (None = old caller, no
            # filtering) so this stays backward compatible.
            if session_date_str and session_date_str != "N/A" and str(df.index[-1])[:10] != session_date_str:
                continue

            raw_ticker = norm_ticker.replace(".CA", "").strip().upper()
            raw_sec = sector_map.get(
                norm_ticker, sector_map.get(raw_ticker, "General / Diversified")
            )

            sector_name = clean_sector_name(raw_sec)
            if sector_name not in sector_groups:
                sector_groups[sector_name] = []

            latest = df.iloc[-1]
            prev = df.iloc[-2] if len(df) > 1 else latest

            curr_c = latest.get("close", 0.0)
            prev_c = prev.get("close", curr_c)
            chg_1d = (((curr_c - prev_c) / prev_c) * 100) if prev_c > 0 else 0.0

            lookback_5d = min(6, len(df))
            c_5d_ago = df["close"].iloc[-lookback_5d]
            chg_5d = (((curr_c - c_5d_ago) / c_5d_ago) * 100) if c_5d_ago > 0 else 0.0

            vol = latest.get("volume", 0.0)
            traded_val = curr_c * vol
            cmf = latest.get("cmf_20", 0.0)
            sma50 = latest.get("sma_50", curr_c)
            is_bullish = curr_c >= sma50

            sector_groups[sector_name].append(
                {
                    "ticker": norm_ticker,
                    "chg_1d": chg_1d,
                    "chg_5d": chg_5d,
                    "value_egp": traded_val,
                    "cmf": cmf,
                    "is_bullish": is_bullish,
                }
            )

        sector_summary = []
        for s_name, stocks in sector_groups.items():
            if not stocks:
                continue
            n_stocks = len(stocks)
            avg_1d = np.mean([s["chg_1d"] for s in stocks])
            avg_5d = np.mean([s["chg_5d"] for s in stocks])
            total_val = sum([s["value_egp"] for s in stocks])
            avg_cmf = np.mean([s["cmf"] for s in stocks])
            bullish_count = sum([1 for s in stocks if s["is_bullish"]])
            breadth_pct = (bullish_count / n_stocks) * 100

            top_stock = max(stocks, key=lambda x: x["chg_1d"])["ticker"]

            # Status assignment using config thresholds
            if (
                avg_cmf >= ACTION_THRESHOLDS["sector_strong_inflow_cmf"]
                and avg_1d >= ACTION_THRESHOLDS["sector_strong_inflow_1d"]
                and breadth_pct >= ACTION_THRESHOLDS["sector_strong_inflow_breadth"]
            ):
                status = "🟢 STRONG INFLOW"
            elif avg_1d >= ACTION_THRESHOLDS["sector_breakout_1d"]:
                status = "⚡ BREAKOUT"
            elif (
                avg_5d >= ACTION_THRESHOLDS["sector_accumulate_5d"]
                and breadth_pct >= ACTION_THRESHOLDS["sector_accumulate_breadth"]
            ):
                status = "📈 ACCUMULATE"
            elif (
                avg_1d <= ACTION_THRESHOLDS["sector_heavy_dist_1d"] and avg_cmf < 0
            ):
                status = "🔴 HEAVY DISTRIBUTION"
            else:
                status = "⚪ CONSOLIDATION"

            sector_summary.append(
                {
                    "Sector": s_name,
                    "Stocks": n_stocks,
                    "1D Return (%)": round(avg_1d, 2),
                    "5D Return (%)": round(avg_5d, 2),
                    "Money Flow (CMF)": round(avg_cmf, 2),
                    "Bullish Breadth (%)": round(breadth_pct, 1),
                    "Traded Value (EGP)": round(total_val, 2),
                    "Sector Leader": top_stock,
                    "Sector Status": status,
                }
            )

        sector_summary.sort(key=lambda x: x["1D Return (%)"], reverse=True)
        return sector_summary

    # -------------------------------------------------------------------------
    # Target-price ETA (used for the "profit target" scale-in/exit planner)
    # -------------------------------------------------------------------------
    @staticmethod
    def estimate_days_to_target(
        df: pd.DataFrame, current_price: float, target_price: float, lookback: int = 20
    ) -> dict:
        """Rough estimate of how many trading days it might take to reach
        ``target_price``, extrapolating the stock's own recent compound
        average daily return over the last ``lookback`` bars.

        This is a pace extrapolation, not a forecast or a promise - it
        answers "if the stock kept moving the way it has recently, when
        would it get there", which can change completely tomorrow. Returns
        eta_days=None (with a plain-language reason) whenever that
        extrapolation isn't meaningful: no history, flat/negative recent
        trend, or the target is already at/below the current price.
        """
        if current_price <= 0 or target_price <= 0:
            return {"eta_days": None, "daily_rate_pct": 0.0, "reason": "Invalid price."}
        if target_price <= current_price:
            return {
                "eta_days": 0,
                "daily_rate_pct": 0.0,
                "reason": "Target is already at or below the current price.",
            }
        if df is None or df.empty or "close" not in df.columns:
            return {"eta_days": None, "daily_rate_pct": 0.0, "reason": "No price history available."}

        n = min(lookback, len(df) - 1)
        if n < 3:
            return {
                "eta_days": None,
                "daily_rate_pct": 0.0,
                "reason": "Not enough history yet to gauge a pace.",
            }

        start_price = float(df["close"].iloc[-n - 1])
        end_price = float(df["close"].iloc[-1])
        if start_price <= 0 or end_price <= 0:
            return {"eta_days": None, "daily_rate_pct": 0.0, "reason": "Invalid price history."}

        daily_rate = (end_price / start_price) ** (1.0 / n) - 1.0
        if daily_rate <= 0:
            return {
                "eta_days": None,
                "daily_rate_pct": round(daily_rate * 100, 3),
                "reason": f"Recent {n}-day trend is flat or negative - can't extrapolate a path to this target.",
            }

        days_needed = math.log(target_price / current_price) / math.log(1.0 + daily_rate)
        return {
            "eta_days": max(1, round(days_needed)),
            "daily_rate_pct": round(daily_rate * 100, 3),
            "reason": None,
        }

    # -------------------------------------------------------------------------
    # Linear-regression trendline
    # -------------------------------------------------------------------------
    @staticmethod
    def compute_trendline(prices: pd.Series) -> tuple:
        n = len(prices)
        if n < 2:
            return prices.values, 0.0
        x = np.arange(n)
        y = prices.values
        slope, intercept = np.polyfit(x, y, 1)
        trend_vals = slope * x + intercept
        start_val = trend_vals[0]
        total_change_pct = (
            ((trend_vals[-1] - start_val) / start_val * 100.0) if start_val != 0 else 0.0
        )
        return trend_vals, round(float(total_change_pct), 2)

    @staticmethod
    def compute_pivot_points(df: pd.DataFrame) -> dict | None:
        """Classic floor-trader pivot points (PP, R1-R3, S1-S3).

        Uses the most recently *completed* calendar week's High/Low/Close
        as the basis - the standard convention for daily-chart pivots - so
        the levels don't shift every single session the way a pure 250-day
        range high/low does. Returns None if there isn't at least one full
        prior week of data yet (e.g. a very recent listing).
        """
        if df.empty or not {"high", "low", "close"}.issubset(df.columns):
            return None

        weekly = df.resample("W").agg({"high": "max", "low": "min", "close": "last"}).dropna()
        if len(weekly) < 2:
            return None

        prior_week = weekly.iloc[-2]  # last *completed* week, not the in-progress one
        h, l, c = float(prior_week["high"]), float(prior_week["low"]), float(prior_week["close"])
        if h <= 0 or l <= 0 or h < l:
            return None

        pp = (h + l + c) / 3.0
        r1, s1 = 2 * pp - l, 2 * pp - h
        r2, s2 = pp + (h - l), pp - (h - l)
        r3, s3 = h + 2 * (pp - l), l - 2 * (h - pp)

        return {
            "pp": round(pp, 4),
            "r1": round(r1, 4), "r2": round(r2, 4), "r3": round(r3, 4),
            "s1": round(s1, 4), "s2": round(s2, 4), "s3": round(s3, 4),
        }

    @staticmethod
    def compute_support_resistance(
        df: pd.DataFrame,
        lookback: int = SR_LOOKBACK_BARS,
        swing_order: int = SR_SWING_ORDER,
        cluster_tol_pct: float = SR_CLUSTER_TOLERANCE_PCT,
        min_touches: int = SR_MIN_TOUCHES,
        half_life_days: int = SR_RECENCY_HALF_LIFE_DAYS,
        max_levels: int = SR_MAX_LEVELS,
    ) -> dict | None:
        """Real support/resistance: prices the market has actually
        reversed at more than once, not just the single highest/lowest
        print in the window. That raw extreme still exists elsewhere in
        this app (decision_matrix.py's own range_high/range_low, which
        feeds range_pos_pct and Rank Score) - it's a different, already-
        tuned concept (every ACTION_THRESHOLDS[..._range_pos_...] knob
        was calibrated against it) and is deliberately left untouched.
        This function is for anything actually LABELED "support" /
        "resistance" to a user, or used to judge whether a level has
        genuinely held/failed: the matrix table's "Nearest Support" /
        "Nearest Resistance" columns, the Pre-Breakout Watchlist's
        "Dist. to Resistance (%)", the failed-breakout-test gate (see
        decision_matrix._recent_failed_resistance_test), and the price
        chart's reference lines (see export_json.py).

        METHOD
        ------
        1. Local swing highs/lows on the CLOSE series over the trailing
           `lookback` bars via scipy.signal.argrelextrema(order=
           swing_order) - the same tool and window convention chart_
           patterns.py and usd_divergence.py already use elsewhere in
           this app, not a new dependency or a different definition of
           "swing" to reconcile.
        2. Swings on each side (highs together, lows together) are
           sorted by price and merged into one cluster whenever
           consecutive swings are within `cluster_tol_pct`% of each
           other - repeated tests of "the same" level collapse into one
           zone even though the exact print differs bar to bar, instead
           of being counted as unrelated one-off levels.
        3. A cluster only counts as a real level once it has at least
           `min_touches` swings in it - a single untested swing is just
           a print, not support or resistance yet.
        4. Each qualifying cluster's strength = sum over its touches of
           0.5 ** (days_since_that_touch / half_life_days) - a level
           tested 3 times in the last month outranks one tested 5 times
           a year ago and never since; an old, long-abandoned level may
           no longer be "in play" even if it was touched often at the
           time.
        5. Clusters are split by whether their level sits below price
           (support candidates) or above it (resistance candidates).
           The NEAREST qualifying cluster on each side is returned as
           the primary level - proximity is what makes a level the one
           price is actually about to test, not raw strength - with up
           to `max_levels` further levels on that side for context,
           nearest-first.

        Returns None if there's too little history to find swings at
        all (same "missing means unavailable" contract as market_
        regime.py / sector_rotation.py) - callers should fall back to
        the plain range extreme in that case, never fabricate a level.
        Otherwise:
            {
              "support":    {"level": .., "touches": N, "strength": .., "last_touch": "YYYY-MM-DD"} | None,
              "resistance": {...} | None,
              "support_levels": [...],     # up to max_levels, nearest first
              "resistance_levels": [...],
            }
        "support"/"resistance" being None means no qualifying (>=
        min_touches) zone exists on that side yet, not that price has
        no support/resistance at all - it just hasn't been established
        within this lookback window.
        """
        if df is None or df.empty or "close" not in df.columns:
            return None
        window = df.iloc[-lookback:]
        closes = window["close"].astype(float).values
        idx = window.index
        n = len(closes)
        if n < swing_order * 4:
            return None

        peak_pos = argrelextrema(closes, np.greater, order=swing_order)[0]
        trough_pos = argrelextrema(closes, np.less, order=swing_order)[0]
        if len(peak_pos) == 0 and len(trough_pos) == 0:
            return None

        last_date = idx[-1]
        curr_price = float(closes[-1])

        def _cluster(positions):
            pts = sorted(((float(closes[p]), idx[p]) for p in positions), key=lambda t: t[0])
            clusters = []
            for price, dt in pts:
                if clusters and price > 0 and abs(price - clusters[-1]["prices"][-1]) / clusters[-1]["prices"][-1] * 100.0 <= cluster_tol_pct:
                    clusters[-1]["prices"].append(price)
                    clusters[-1]["dates"].append(dt)
                else:
                    clusters.append({"prices": [price], "dates": [dt]})
            out = []
            for c in clusters:
                touches = len(c["prices"])
                if touches < min_touches:
                    continue
                level = float(np.mean(c["prices"]))
                last_touch = max(c["dates"])
                strength = sum(
                    0.5 ** (max(0.0, (last_date - dt).days) / half_life_days) for dt in c["dates"]
                )
                out.append({
                    "level": round(level, 4),
                    "touches": touches,
                    "strength": round(strength, 3),
                    "last_touch": str(last_touch)[:10],
                })
            return out

        resistance_zones = sorted(
            (z for z in _cluster(peak_pos) if z["level"] > curr_price),
            key=lambda z: z["level"],  # ascending -> nearest above price first
        )
        support_zones = sorted(
            (z for z in _cluster(trough_pos) if z["level"] < curr_price),
            key=lambda z: z["level"], reverse=True,  # descending -> nearest below price first
        )

        return {
            "resistance": resistance_zones[0] if resistance_zones else None,
            "support": support_zones[0] if support_zones else None,
            "resistance_levels": resistance_zones[:max_levels],
            "support_levels": support_zones[:max_levels],
        }

    # -------------------------------------------------------------------------
    # Sector index (equal-weight, base 100)
    # -------------------------------------------------------------------------
    def get_sector_historical_index(
        self, sector_name: str, sector_map: dict, bulk_data: dict | None = None
    ) -> pd.DataFrame:
        if bulk_data is None:
            bulk_data = self.get_all_market_data_bulk()
        matching_series = []
        for norm_ticker, df in bulk_data.items():
            if df.empty or len(df) < 5:
                continue
            raw_ticker = norm_ticker.replace(".CA", "").strip().upper()
            sec = clean_sector_name(
                sector_map.get(
                    norm_ticker, sector_map.get(raw_ticker, "General / Diversified")
                )
            )
            if sec == sector_name:
                s_close = (df["close"] / df["close"].iloc[0]) * 100.0
                matching_series.append(s_close.rename(norm_ticker))
        if not matching_series:
            return pd.DataFrame()
        sector_df = pd.concat(matching_series, axis=1).ffill().bfill()
        sector_index = sector_df.mean(axis=1).to_frame(name="sector_index")
        return sector_index

    # -------------------------------------------------------------------------
    # Historical-analog pattern matcher
    # -------------------------------------------------------------------------
    def match_historical_patterns(
        self,
        df: pd.DataFrame,
        window_size: int = 15,
        forecast_horizon: int = 5,
        min_sim: float = 0.70,
    ) -> dict:
        min_required = max(window_size + forecast_horizon, MIN_BARS_FOR_PATTERN_TRUST)
        if len(df) < min_required:
            return {
                "match_found": False,
                "confidence": 0.0,
                "projected_change_pct": 0.0,
                "sample_size": 0,
                "sortino_penalty": 1.0,
                "reason": f"Insufficient history: {len(df)} bars available, {min_required} needed.",
            }

        recent_series = df["close"].iloc[-window_size:].values
        recent_norm = (recent_series - np.mean(recent_series)) / (
            np.std(recent_series) + 1e-8
        )

        historical_close = df["close"].iloc[:-window_size].values
        n_windows = len(historical_close) - window_size - forecast_horizon + 1

        if n_windows <= 0:
            return {
                "match_found": False,
                "confidence": 0.0,
                "projected_change_pct": 0.0,
                "sample_size": 0,
                "sortino_penalty": 1.0,
                "reason": "Not enough prior windows to search for an analog.",
            }

        all_windows = sliding_window_view(historical_close, window_shape=window_size)[
            :n_windows
        ]
        w_mean = np.mean(all_windows, axis=1, keepdims=True)
        w_std = np.std(all_windows, axis=1, keepdims=True) + 1e-8
        windows_norm = (all_windows - w_mean) / w_std

        sims_array = np.dot(windows_norm, recent_norm) / window_size

        if len(sims_array) >= 5:
            top_5_idx = np.argpartition(sims_array, -5)[-5:]
            top_indices = top_5_idx[np.argsort(sims_array[top_5_idx])[::-1]]
        else:
            top_indices = np.argsort(sims_array)[::-1]

        top_k = [
            (float(sims_array[idx]), int(idx))
            for idx in top_indices
            if sims_array[idx] >= min_sim
        ]

        if not top_k:
            best_sim = (
                float(sims_array[top_indices[0]]) if len(sims_array) > 0 else 0.0
            )
            return {
                "match_found": False,
                "confidence": 0.0,
                "projected_change_pct": 0.0,
                "sample_size": n_windows,
                "best_similarity": round(best_sim * 100, 1),
                "sortino_penalty": 1.0,
                "reason": f"No historical analog cleared the {min_sim:.0%} similarity bar.",
            }

        returns = []
        for sim, idx in top_k:
            match_end_price = historical_close[idx + window_size - 1]
            future_price = historical_close[idx + window_size + forecast_horizon - 1]
            if match_end_price != 0:
                returns.append((future_price - match_end_price) / match_end_price)

        if not returns:
            return {
                "match_found": False,
                "confidence": 0.0,
                "projected_change_pct": 0.0,
                "sample_size": n_windows,
                "sortino_penalty": 1.0,
                "reason": "Matches found but could not compute forward returns.",
            }

        avg_sim = np.mean([s for s, _ in top_k])
        avg_return = np.mean(returns)
        return_std = np.std(returns)
        lower_95 = float(np.percentile(returns, 5)) if returns else 0.0
        upper_95 = float(np.percentile(returns, 95)) if returns else 0.0
        perf = QuantitativeEngine.compute_perf_metrics(returns)
        regime = QuantitativeEngine.classify_regime(df)

        # Actual win rate among the matched analogs - the fraction that
        # were profitable over the forecast horizon. This is a DIFFERENT
        # quantity from `confidence` below and must stay that way:
        # `confidence` answers "how much should the composite Rank Score
        # trust this pattern" (similarity x statistical significance x
        # downside-risk discount - a signal-QUALITY weight), while
        # `win_rate` answers "what fraction of the time did this setup
        # actually pay off" - the specific probability Kelly sizing (p in
        # f* = p - q/b) needs. A tight, statistically-significant match
        # can still have a modest win rate (a few big winners, several
        # small losers) and vice versa; feeding `confidence` into Kelly
        # instead of this would size positions off the wrong number.
        win_rate = float(np.mean([1.0 if r > 0 else 0.0 for r in returns]))

        # Statistical-significance factor, replacing the old ad hoc
        # "1 - std*5" agreement penalty. The question that actually
        # matters for "confidence" is: given how few, how dispersed, and
        # how small these historical analog returns are, how likely is
        # it that this average forward return is real signal rather than
        # noise around zero? A one-sample t-test against a population
        # mean of 0 answers exactly that (H0: the analog windows carry
        # no real directional edge) - both sample size AND dispersion
        # feed into it automatically via the standard error term, so a
        # single freak analog (n=1) or a highly dispersed sample can no
        # longer produce artificially high confidence just because
        # return_std happened to look small in the old linear formula.
        # p-value needs n>=2; with fewer matches there's no way to test
        # significance at all, so confidence is capped low rather than
        # guessed.
        n_matches = len(returns)
        if n_matches >= 2 and return_std > 1e-9:
            t_stat, p_value = ttest_1samp(returns, popmean=0.0)
            significance_factor = float(np.clip(1.0 - p_value, 0.0, 1.0))
        else:
            p_value = 1.0
            significance_factor = 0.15  # single-match analog: can't test, so heavily discounted

        downside_sq = [min(0.0, r) ** 2 for r in returns]
        downside_dev = np.sqrt(np.mean(downside_sq)) if len(returns) > 0 else 0.0
        sortino_penalty = max(0.5, 1.0 - (downside_dev * 5.0))

        confidence = float(avg_sim * 100 * significance_factor * sortino_penalty)

        return {
            "match_found": True,
            "confidence": round(confidence, 2),
            "win_rate": round(win_rate, 4),
            "projected_change_pct": round(float(avg_return) * 100, 2),
            "sample_size": len(top_k),
            "windows_searched": n_windows,
            "return_dispersion_pct": round(float(return_std) * 100, 2),
            "lower_95_pct": round(lower_95 * 100, 2),
            "upper_95_pct": round(upper_95 * 100, 2),
            "regime": regime,
            "perf": perf,
            "p_value": round(float(p_value), 4),
            "significance_factor": round(significance_factor, 2),
            "sortino_penalty": round(sortino_penalty, 2),
        }


if __name__ == "__main__":
    # Sanity-check the engine without touching the database.
    print("QuantitativeEngine module loaded successfully.")
    print(f"  _compute_adx is a static method: "
          f"{callable(getattr(QuantitativeEngine, '_compute_adx', None))}")
    print(f"  match_historical_patterns signature OK: "
          f"{'forecast_horizon' in QuantitativeEngine.match_historical_patterns.__code__.co_varnames}")
