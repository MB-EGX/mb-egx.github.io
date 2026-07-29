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
"""
from __future__ import annotations

# MUST be imported before numpy/pandas (below) - sets OPENBLAS/MKL/OMP/
# NUMEXPR thread caps as a module-level side effect; those only take
# effect if set before numpy/pandas load anywhere in this process. See
# config.py's module docstring.
from config import MIN_BARS_FOR_PATTERN_TRUST

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

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
        atr = float(latest.get("atr_14", price * 0.02))
        return price, atr

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
        adx = dx.ewm(alpha=wilder_alpha, adjust=False, min_periods=1).mean()
        return adx.fillna(0)

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

        high_low = df["high"] - df["low"]
        high_close = (df["high"] - df["close"].shift()).abs()
        low_close = (df["low"] - df["close"].shift()).abs()
        true_range = pd.Series(
            np.maximum(np.maximum(high_low, high_close), low_close), index=df.index
        )
        df["atr_14"] = (
            true_range.rolling(window=min(14, n), min_periods=1).mean().fillna(0)
        )

        bb_mean = df["close"].rolling(window=min(20, n), min_periods=1).mean()
        bb_std = df["close"].rolling(window=min(20, n), min_periods=1).std().fillna(0)
        df["bb_upper"] = bb_mean + (2 * bb_std)
        df["bb_lower"] = bb_mean - (2 * bb_std)

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

        df["adx_14"] = QuantitativeEngine._compute_adx(df, period=min(14, n))

        c = df["close"]
        s50 = df["sma_50"]
        rsi = df["rsi_14"]
        adx = df["adx_14"]
        trending = adx >= 20

        conditions = [
            (c >= s50) & (rsi >= 45) & trending,
            (c >= s50) & (rsi >= 45) & ~trending,
            c >= s50 * 0.95,
            (rsi >= 40) & (rsi <= 60),
            (c < s50) & (rsi < 40) & trending,
            (c < s50) & (rsi < 40) & ~trending,
        ]
        choices = [
            "Strong Bullish",
            "Weak Bullish (Low Trend Strength)",
            "Weak Bullish",
            "Consolidation / Neutral",
            "Strong Bearish",
            "Weak Bearish (Low Trend Strength)",
        ]
        df["trend_class"] = np.select(conditions, choices, default="Weak Bearish")
        return df

    # -------------------------------------------------------------------------
    # Sector roll-up
    # -------------------------------------------------------------------------
    def compute_sector_analytics(
        self, processed_tickers_data: dict, sector_map: dict
    ) -> list:
        from config import ACTION_THRESHOLDS  # local import to avoid cycles at module load

        sector_groups: dict = {}

        for norm_ticker, df in processed_tickers_data.items():
            if df.empty or len(df) < 5:
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

    # -------------------------------------------------------------------------
    # Sector index (equal-weight, base 100)
    # -------------------------------------------------------------------------
    def get_sector_historical_index(
        self, sector_name: str, sector_map: dict
    ) -> pd.DataFrame:
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

        all_windows = sliding_window_view(historical_close, window_length=window_size)[
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
        agreement_penalty = max(0.0, 1.0 - min(return_std * 5, 0.5))

        downside_sq = [min(0.0, r) ** 2 for r in returns]
        downside_dev = np.sqrt(np.mean(downside_sq)) if len(returns) > 0 else 0.0
        sortino_penalty = max(0.5, 1.0 - (downside_dev * 5.0))

        confidence = float(avg_sim * 100 * agreement_penalty * sortino_penalty)

        return {
            "match_found": True,
            "confidence": round(confidence, 2),
            "projected_change_pct": round(float(avg_return) * 100, 2),
            "sample_size": len(top_k),
            "windows_searched": n_windows,
            "return_dispersion_pct": round(float(return_std) * 100, 2),
            "sortino_penalty": round(sortino_penalty, 2),
        }


if __name__ == "__main__":
    # Sanity-check the engine without touching the database.
    print("QuantitativeEngine module loaded successfully.")
    print(f"  _compute_adx is a static method: "
          f"{callable(getattr(QuantitativeEngine, '_compute_adx', None))}")
    print(f"  match_historical_patterns signature OK: "
          f"{'forecast_horizon' in QuantitativeEngine.match_historical_patterns.__code__.co_varnames}")
