"""
decision_matrix.py
==================
Multi-factor confirmation matrix: rank every stock, decide buy/sell/hold,
compute exit signals for owned positions.

The action classification thresholds are no longer hard-coded — they are
imported from ``config.ACTION_THRESHOLDS`` so the whole engine can be
re-tuned from a single file.

CHANGELOG vs the original:
  * BREAKOUT BUY is now split into three distinct labels:
        - ⚡ BREAKOUT BUY (X-OVER + MOMENTUM)  — both signals fire (strongest)
        - ⚡ BREAKOUT BUY (X-OVER)            — SMA-50 golden cross only
        - ⚡ BREAKOUT BUY (MOMENTUM)          — EMA20 + RSI momentum only
    The previous code OR'd the two conditions into one bucket, which masked
    whether a signal was a confirmed crossover or just an RSI overheat.
  * All thresholds pulled from ``config.ACTION_THRESHOLDS``.
  * Scoring weights for the new labels live in ``config.SCORE_WEIGHTS``
    under ``breakout_crossover`` (35.0) and ``breakout_momentum`` (28.0).
"""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed

# MUST be imported before pandas and before `analytics` (below) - config.py
# sets OPENBLAS/MKL/OMP/NUMEXPR thread caps as a module-level side effect,
# which only takes effect if set before numpy/pandas load anywhere in this
# process.
from config import (
    MIN_AVG_VOLUME,
    MIN_BARS_FOR_PATTERN_TRUST,
    RISK_PER_TRADE_PCT,
    TRANSACTION_FEE_PCT,
    ROUND_TRIP_FEE_PCT,
    SCORE_WEIGHTS,
    ACTION_THRESHOLDS,
    CONFIDENCE_FLOOR_WEIGHT,
    CONFIDENCE_FULL_TRUST_BARS,
    MAX_WORKERS,
    MATRIX_LOOKBACK_DAYS,
    get_logger,
)

import pandas as pd

from analytics import QuantitativeEngine
from db_manager import DatabaseManager

logger = get_logger("decision_matrix")


def _confidence_weight(n_bars: int) -> float:
    if n_bars <= MIN_BARS_FOR_PATTERN_TRUST:
        return CONFIDENCE_FLOOR_WEIGHT
    if n_bars >= CONFIDENCE_FULL_TRUST_BARS:
        return 1.0
    span = CONFIDENCE_FULL_TRUST_BARS - MIN_BARS_FOR_PATTERN_TRUST
    progress = (n_bars - MIN_BARS_FOR_PATTERN_TRUST) / span
    return CONFIDENCE_FLOOR_WEIGHT + progress * (1.0 - CONFIDENCE_FLOOR_WEIGHT)


# Pass connect_db=False so background workers never try to open DuckDB!
def _worker_compute_chunk(chunk_data):
    qe = QuantitativeEngine(connect_db=False)
    results = {}
    for ticker, df in chunk_data:
        try:
            df_ind = qe.compute_indicators(df)
            pattern_data = None
            if not df_ind.empty and len(df_ind) >= 15:
                pattern_data = qe.match_historical_patterns(df_ind, min_sim=0.60)
            results[ticker] = (df_ind, pattern_data, None)
        except Exception as e:
            results[ticker] = (pd.DataFrame(), None, str(e))
    return results


class DecisionMatrix:
    def __init__(self):
        self.dbm = DatabaseManager()
        self.qe = QuantitativeEngine()

    def analyze_market(self, progress_callback=None):
        # Was an unbounded pull of EVERY bar ever ingested, for every ticker,
        # on every single analysis run. Nothing in the scoring below looks
        # back more than ~250 trading days, so capping this cuts the data
        # volume (and the indicator recompute cost) substantially without
        # changing any signal.
        market_data_bulk = self.qe.get_all_market_data_bulk(days=MATRIX_LOOKBACK_DAYS)
        tickers = list(market_data_bulk.keys())

        owned_dict = self.dbm.get_all_owned_stocks()
        closed_trades = self.dbm.get_all_closed_trades()
        cash_balance = self.dbm.get_cash_balance()
        sector_map = self.dbm.get_sector_map()

        buy_recommendations = []
        exit_strategies = []
        breakout_watchlist = []
        processed_tickers_dict: dict = {}

        total_invested = 0.0
        total_market_value = 0.0
        processed_owned_tickers = set()

        total = len(tickers)
        if total == 0 and not owned_dict:
            if progress_callback:
                progress_callback(100, "No tickers found in database.")
            empty_stmt = {
                "Cash Balance (EGP)": round(cash_balance, 2),
                "Stock Portfolio Cost Basis (EGP)": 0.0,
                "Stock Portfolio Market Value (EGP)": 0.0,
                "Unrealized Stock P&L (EGP)": 0.0,
                "Unrealized Stock P&L (%)": 0.0,
                "Realized P&L from Closed Trades (EGP)": 0.0,
                "Total Account Equity / Net Worth (EGP)": round(cash_balance, 2),
            }
            return (
                buy_recommendations,
                exit_strategies,
                {},
                closed_trades,
                empty_stmt,
                [],
                breakout_watchlist,
            )

        eligible = []
        for ticker in tickers:
            norm_ticker = self.dbm.normalize_symbol(ticker)
            df = market_data_bulk[ticker]
            is_owned = norm_ticker in owned_dict
            n_bars = len(df)
            if not is_owned and (df.empty or n_bars < 15):
                continue
            eligible.append((ticker, norm_ticker, df, is_owned, n_bars))

        precomputed: dict = {}
        if eligible:
            chunk_size = 30
            chunks = [eligible[i : i + chunk_size] for i in range(0, len(eligible), chunk_size)]
            with ProcessPoolExecutor(max_workers=MAX_WORKERS) as executor:
                future_to_chunk = {
                    executor.submit(_worker_compute_chunk, [(t[0], t[2]) for t in chunk]): chunk
                    for chunk in chunks
                }
                done_tickers = 0
                for future in as_completed(future_to_chunk):
                    res_dict = future.result()
                    for ticker, (df_ind, pattern_data, err) in res_dict.items():
                        if err:
                            logger.warning(f"Skipping {ticker} - computation failed: {err}")
                        precomputed[ticker] = (df_ind, pattern_data)
                    done_tickers += len(res_dict)
                    if progress_callback:
                        progress_callback(
                            int((done_tickers / len(eligible)) * 70),
                            f"Computing indicators & pattern matches: {done_tickers}/{len(eligible)}...",
                        )

        for idx, (ticker, norm_ticker, df, is_owned, n_bars) in enumerate(eligible):
            if progress_callback and idx % 5 == 0:
                progress_callback(
                    70 + int((idx / max(len(eligible), 1)) * 30),
                    f"Scanning multi-factor confirmation matrix: {ticker}...",
                )

            df_ind, pattern_data = precomputed.get(ticker, (pd.DataFrame(), None))
            if df_ind.empty and not is_owned:
                continue
            processed_tickers_dict[norm_ticker] = df_ind

            try:
                if is_owned:
                    pos = owned_dict[norm_ticker]
                    buy_price = pos["buy_price"]
                    shares = pos["shares"]
                    p_date = pos["purchase_date"]

                    invested_val = buy_price * shares
                    curr_price = (
                        df_ind.iloc[-1].get("close", buy_price)
                        if not df_ind.empty
                        else buy_price
                    )
                    curr_val = curr_price * shares

                    total_invested += invested_val
                    total_market_value += curr_val
                    processed_owned_tickers.add(norm_ticker)

                    pnl_egp = curr_val - invested_val
                    pnl_pct = (
                        (((curr_price - buy_price) / buy_price) * 100)
                        if buy_price > 0
                        else 0.0
                    )

                    if n_bars >= 15 and not df_ind.empty:
                        latest = df_ind.iloc[-1]
                        data_conf_tier = self.qe.data_confidence_tier(n_bars)
                        if pattern_data is None:
                            pattern_data = self.qe.match_historical_patterns(
                                df_ind, min_sim=0.60
                            )

                        rsi = latest.get("rsi_14", 50.0)
                        adx = latest.get("adx_14", 0.0)
                        vwap = latest.get("vwap_20", curr_price)
                        trend_latest = latest.get("trend_class", "Consolidation / Neutral")
                        atr = latest.get("atr_14", curr_price * 0.02)
                        if pd.isna(atr) or atr == 0:
                            atr = curr_price * 0.02

                        atr_mult = ACTION_THRESHOLDS["atr_trailing_multiplier"]
                        trailing_stop = curr_price - (atr_mult * atr)

                        if pattern_data["match_found"]:
                            projected = pattern_data["projected_change_pct"]
                            floor_pct = ACTION_THRESHOLDS["take_profit_pattern_floor_pct"]
                            expected_gain = max(projected, floor_pct) / 100.0
                        else:
                            atr_floor_mult = ACTION_THRESHOLDS["take_profit_atr_floor_multiplier"]
                            expected_gain = (
                                (atr * atr_floor_mult) / curr_price
                                if curr_price > 0
                                else 0.03
                            )

                        take_profit = curr_price * (1 + expected_gain + ROUND_TRIP_FEE_PCT)

                        action_cmd = "🛡️ HOLD / TRAIL STOP"
                        if (
                            pnl_pct <= ACTION_THRESHOLDS["cut_loss_pnl_pct"]
                            or (
                                curr_price < vwap * ACTION_THRESHOLDS["vwap_cut_loss_ratio"]
                                and pnl_pct < 0
                            )
                        ):
                            action_cmd = "⚠️ CUT LOSS / REVIEW (Below VWAP)"
                        elif curr_price >= take_profit:
                            action_cmd = "💰 TAKE PROFIT ZONE"
                    else:
                        data_conf_tier = "Very Low (New/Short History)"
                        rsi, adx = 50.0, 0.0
                        trend_latest = "Insufficient Data"
                        trailing_stop = curr_price * 0.95
                        default_tp = ACTION_THRESHOLDS["default_take_profit_pct"] / 100.0
                        take_profit = curr_price * (1 + default_tp + ROUND_TRIP_FEE_PCT)
                        action_cmd = "🛡️ HOLD / TRAIL STOP (Low Data)"

                    exit_strategies.append(
                        {
                            "Ticker": norm_ticker,
                            "Shares": round(shares, 4),
                            "Buy Price": round(buy_price, 4),
                            "Current Price": round(curr_price, 4),
                            "P&L (EGP)": round(pnl_egp, 2),
                            "P&L (%)": round(pnl_pct, 2),
                            "Action Command": action_cmd,
                            "Take-Profit Target": round(take_profit, 4),
                            "Trailing Stop-Loss": round(trailing_stop, 4),
                            "Trend Class": trend_latest,
                            "RSI-14": round(rsi, 1),
                            "ADX-14": round(adx, 1),
                            "Data Confidence": data_conf_tier,
                            "Purchase Date": p_date,
                        }
                    )
                    continue

                latest = df_ind.iloc[-1]
                prev = df_ind.iloc[-2] if len(df_ind) > 1 else latest

                data_conf_tier = self.qe.data_confidence_tier(n_bars)
                conf_weight = _confidence_weight(n_bars)
                if pattern_data is None:
                    pattern_data = self.qe.match_historical_patterns(
                        df_ind, min_sim=0.60
                    )

                curr_price = latest.get("close", 0.0)
                prev_close = prev.get("close", curr_price)
                sma50 = latest.get("sma_50", curr_price)
                prev_sma50 = prev.get("sma_50", prev_close)
                ema20 = latest.get("ema_20", curr_price)
                rsi = latest.get("rsi_14", 50.0)
                adx = latest.get("adx_14", 0.0)
                vol_ratio = latest.get("volume_ratio", 1.0)
                vol_z = latest.get("vol_z_score", 0.0)
                vwap = latest.get("vwap_20", curr_price)
                cmf = latest.get("cmf_20", 0.0)
                is_squeezed = latest.get("bb_kc_squeeze", False)
                avg_volume_20 = latest.get("volume_avg", 0.0)
                trend_latest = latest.get("trend_class", "Consolidation / Neutral")
                atr = latest.get("atr_14", curr_price * 0.02)
                if pd.isna(atr) or atr == 0:
                    atr = curr_price * 0.02

                w_sma50 = latest.get("w_sma_50", curr_price)
                w_rsi = latest.get("w_rsi", 50.0)
                weekly_aligned = (curr_price > w_sma50) and (w_rsi >= 50.0)

                is_liquid = avg_volume_20 >= MIN_AVG_VOLUME
                gap_pct = (
                    (((curr_price - prev_close) / prev_close) * 100) if prev_close > 0 else 0.0
                )
                lookback = min(250, n_bars)
                range_high = df_ind["high"].iloc[-lookback:].max()
                range_low = df_ind["low"].iloc[-lookback:].min()
                range_pos_pct = (
                    (((curr_price - range_low) / (range_high - range_low)) * 100)
                    if (range_high - range_low) > 0
                    else 50.0
                )

                # -------------------------------------------------------------------------
                # Action classification (now split into 3 BREAKOUT BUY labels)
                # -------------------------------------------------------------------------
                ma_crossover = (prev_close <= prev_sma50) and (curr_price > sma50)
                momentum_signal = (curr_price > ema20) and (
                    rsi >= ACTION_THRESHOLDS["breakout_momentum_rsi_min"]
                )

                if (
                    curr_price <= sma50 * ACTION_THRESHOLDS["sell_avoid_price_ratio"]
                    and rsi <= ACTION_THRESHOLDS["sell_avoid_rsi_max"]
                ):
                    raw_action = "🛑 SELL / AVOID"
                    trend_bonus = SCORE_WEIGHTS["sell_avoid"]
                    needs_confirmation = False
                elif (
                    range_pos_pct >= ACTION_THRESHOLDS["strong_buy_range_pos_min"]
                    and ACTION_THRESHOLDS["strong_buy_rsi_min"] <= rsi <= ACTION_THRESHOLDS["strong_buy_rsi_max"]
                    and gap_pct >= ACTION_THRESHOLDS["strong_buy_gap_min"]
                ):
                    raw_action = "🔥 STRONG BUY"
                    trend_bonus = SCORE_WEIGHTS["strong_buy"]
                    needs_confirmation = True
                elif (
                    ma_crossover
                    and momentum_signal
                    and gap_pct >= ACTION_THRESHOLDS["breakout_gap_min"]
                ):
                    # Both crossover AND momentum fire — the strongest breakout
                    raw_action = "⚡ BREAKOUT BUY (X-OVER + MOMENTUM)"
                    trend_bonus = (
                        SCORE_WEIGHTS["breakout_crossover"]
                        + SCORE_WEIGHTS["breakout_momentum"]
                    )
                    needs_confirmation = True
                elif ma_crossover and gap_pct >= ACTION_THRESHOLDS["breakout_gap_min"]:
                    raw_action = "⚡ BREAKOUT BUY (X-OVER)"
                    trend_bonus = SCORE_WEIGHTS["breakout_crossover"]
                    needs_confirmation = True
                elif momentum_signal and gap_pct >= ACTION_THRESHOLDS["breakout_gap_min"]:
                    raw_action = "⚡ BREAKOUT BUY (MOMENTUM)"
                    trend_bonus = SCORE_WEIGHTS["breakout_momentum"]
                    needs_confirmation = True
                elif (
                    range_pos_pct <= ACTION_THRESHOLDS["buy_on_dip_range_pos_max"]
                    or rsi <= ACTION_THRESHOLDS["buy_on_dip_rsi_max"]
                ):
                    raw_action = "⏳ BUY ON DIP"
                    trend_bonus = SCORE_WEIGHTS["buy_on_dip"]
                    needs_confirmation = False
                else:
                    raw_action = "📈 ACCUMULATE"
                    trend_bonus = SCORE_WEIGHTS["accumulate"]
                    needs_confirmation = False

                # Confirmation gates
                strong_trend = adx >= ACTION_THRESHOLDS["strong_trend_adx_min"]
                vol_confirmed = (
                    vol_ratio >= ACTION_THRESHOLDS["volume_ratio_threshold"]
                    or vol_z >= ACTION_THRESHOLDS["volume_z_score_threshold"]
                )

                confirmed = strong_trend and vol_confirmed
                if needs_confirmation and not confirmed:
                    action_cmd = f"{raw_action} (Unconfirmed: low ADX/volume)"
                    trend_bonus *= SCORE_WEIGHTS["unconfirmed_scale"]
                else:
                    action_cmd = raw_action

                if cmf >= SCORE_WEIGHTS["cmf_bonus_threshold"] and "SELL" not in action_cmd:
                    trend_bonus += SCORE_WEIGHTS["cmf_bonus"]
                if is_squeezed and "SELL" not in action_cmd:
                    action_cmd = f"{action_cmd} [💥 SQUEEZE]"
                    trend_bonus += SCORE_WEIGHTS["squeeze_bonus"]

                if weekly_aligned and (
                    "STRONG BUY" in action_cmd or "BREAKOUT BUY" in action_cmd
                ):
                    action_cmd = f"{action_cmd} [👑 WEEKLY ALIGNED]"
                    trend_bonus += SCORE_WEIGHTS["weekly_aligned_bonus"]

                if not is_liquid:
                    action_cmd = f"🚫 ILLIQUID - {action_cmd}"
                    trend_bonus += SCORE_WEIGHTS["illiquid_penalty"]

                # -------------------------------------------------------------
                # Pre-breakout screening: "what might break out NEXT session/
                # week", separate from the reactive BREAKOUT BUY labels above
                # (which confirm a move already in progress). A stock only
                # qualifies here if it's still coiling - not already fired.
                # -------------------------------------------------------------
                if is_liquid and n_bars >= 20:
                    bw_score = 0.0
                    bw_reasons = []

                    if is_squeezed:
                        bw_score += 25.0
                        bw_reasons.append("Volatility squeeze")

                    adx_series = df_ind["adx_14"]
                    adx_prior = adx_series.iloc[-6] if len(adx_series) > 6 else adx
                    adx_rising = pd.notna(adx_prior) and adx > adx_prior
                    if (
                        ACTION_THRESHOLDS["breakout_watch_adx_min"] <= adx < ACTION_THRESHOLDS["breakout_watch_adx_max"]
                        and adx_rising
                    ):
                        bw_score += 20.0
                        bw_reasons.append("ADX trend just building")

                    if ACTION_THRESHOLDS["breakout_watch_rsi_min"] <= rsi <= ACTION_THRESHOLDS["breakout_watch_rsi_max"]:
                        bw_score += 15.0
                        bw_reasons.append("RSI bullish with room to run")

                    vol_recent = df_ind["volume"].iloc[-5:].mean() if n_bars >= 10 else avg_volume_20
                    vol_prior = df_ind["volume"].iloc[-10:-5].mean() if n_bars >= 10 else avg_volume_20
                    volume_building = (
                        pd.notna(vol_recent) and pd.notna(vol_prior) and vol_prior > 0
                        and vol_recent > vol_prior * ACTION_THRESHOLDS["breakout_watch_volume_build_ratio"]
                    )
                    if volume_building:
                        bw_score += 15.0
                        bw_reasons.append("Volume trending up")

                    if range_pos_pct >= ACTION_THRESHOLDS["breakout_watch_range_pos_min"]:
                        bw_score += 15.0
                        bw_reasons.append("Near recent high (resistance test)")

                    if cmf > 0:
                        bw_score += 10.0
                        bw_reasons.append("Positive money flow")

                    if weekly_aligned:
                        bw_score += 10.0
                        bw_reasons.append("Weekly trend aligned")

                    already_fired = (
                        "STRONG BUY" in raw_action or "BREAKOUT BUY" in raw_action or "SELL" in raw_action
                    )
                    if bw_score >= ACTION_THRESHOLDS["breakout_watch_min_score"] and not already_fired:
                        dist_to_resistance = (
                            round(max(0.0, ((range_high - curr_price) / curr_price) * 100), 2)
                            if curr_price > 0 else None
                        )
                        breakout_watchlist.append({
                            "Ticker": norm_ticker,
                            "Breakout Score": round(bw_score, 1),
                            "Current Price": round(curr_price, 4),
                            "Dist. to Resistance (%)": dist_to_resistance,
                            "RSI-14": round(rsi, 1),
                            "ADX-14": round(adx, 1),
                            "Squeeze Active": bool(is_squeezed),
                            "Volume Trend": "Rising" if volume_building else "Flat/Falling",
                            "Trend Class": trend_latest,
                            "Signals": ", ".join(bw_reasons),
                            "Data Confidence": data_conf_tier,
                        })

                pattern_component = (
                    pattern_data["confidence"] * SCORE_WEIGHTS["pattern_confidence_weight"]
                    if pattern_data["match_found"]
                    else 0.0
                )
                projected_component = (
                    pattern_data["projected_change_pct"] * SCORE_WEIGHTS["pattern_projected_gain_weight"]
                    if pattern_data["match_found"]
                    else 0.0
                )

                raw_score = (
                    pattern_component
                    + projected_component
                    + (range_pos_pct * SCORE_WEIGHTS["range_position_weight"])
                    + trend_bonus
                )
                score = raw_score * conf_weight

                entry_target = (
                    min(curr_price, vwap) if pd.notna(vwap) and vwap > 0 else curr_price
                )
                atr_mult = ACTION_THRESHOLDS["atr_trailing_multiplier"]
                stop_distance = atr_mult * atr
                suggested_stop = round(max(curr_price - stop_distance, 0.0001), 4)
                risk_budget = cash_balance * RISK_PER_TRADE_PCT

                effective_entry_cost = curr_price * (1.0 + TRANSACTION_FEE_PCT)
                max_affordable_shares = (
                    int(cash_balance / effective_entry_cost) if effective_entry_cost > 0 else 0
                )
                raw_shares = int(risk_budget / stop_distance) if stop_distance > 0 else 0
                suggested_shares = min(raw_shares, max_affordable_shares)

                buy_recommendations.append(
                    {
                        "Ticker": norm_ticker,
                        "Action": action_cmd,
                        "Rank Score": round(score, 1),
                        "Current Price": round(curr_price, 4),
                        "Target Entry (VWAP)": round(entry_target, 4),
                        "Suggested Stop-Loss": suggested_stop,
                        "Suggested Shares (1% Risk)": suggested_shares,
                        "Projected Gain (%)": (
                            pattern_data["projected_change_pct"]
                            if pattern_data["match_found"]
                            else "N/A"
                        ),
                        "Pattern Conf (%)": (
                            pattern_data["confidence"] if pattern_data["match_found"] else "N/A"
                        ),
                        "Trend Class": trend_latest,
                        "RSI-14": round(rsi, 1),
                        "ADX-14": round(adx, 1),
                        "Vol Z-Score": round(vol_z, 2),
                        "Avg Volume (20D)": int(avg_volume_20),
                        "Data Confidence": data_conf_tier,
                    }
                )
            except Exception as e:
                logger.error(f"Failed to score {ticker}: {e}")
                continue

        for ticker, pos in owned_dict.items():
            if ticker not in processed_owned_tickers:
                buy_price = pos["buy_price"]
                shares = pos["shares"]
                invested_val = buy_price * shares
                total_invested += invested_val
                total_market_value += invested_val
                default_tp = ACTION_THRESHOLDS["default_take_profit_pct"] / 100.0
                exit_strategies.append(
                    {
                        "Ticker": ticker,
                        "Shares": round(shares, 4),
                        "Buy Price": round(buy_price, 4),
                        "Current Price": round(buy_price, 4),
                        "P&L (EGP)": 0.0,
                        "P&L (%)": 0.0,
                        "Action Command": "⚠️ NO MARKET DATA FOUND",
                        "Take-Profit Target": round(
                            buy_price * (1 + default_tp + ROUND_TRIP_FEE_PCT), 4
                        ),
                        "Trailing Stop-Loss": round(buy_price * 0.95, 4),
                        "Trend Class": "Unknown",
                        "RSI-14": 50.0,
                        "ADX-14": 0.0,
                        "Data Confidence": "None",
                        "Purchase Date": pos["purchase_date"],
                    }
                )

        buy_recommendations.sort(key=lambda x: x["Rank Score"], reverse=True)

        # Top 10 by category — substring "⚡ BREAKOUT BUY" still matches all 3
        # new breakout labels, so this filter keeps working as-is.
        categories = ["🔥 STRONG BUY", "⚡ BREAKOUT BUY", "📈 ACCUMULATE", "⏳ BUY ON DIP"]
        top_10_by_category = {}
        for cat in categories:
            filtered = [
                r
                for r in buy_recommendations
                if cat in r["Action"] and "ILLIQUID" not in r["Action"]
            ]
            top_10_by_category[cat] = filtered[:10]

        unrealized_pnl = total_market_value - total_invested
        unrealized_pct = (
            ((unrealized_pnl / total_invested) * 100) if total_invested > 0 else 0.0
        )
        realized_pnl_total = sum(t["Realized P&L (EGP)"] for t in closed_trades)
        total_equity = cash_balance + total_market_value

        financial_statement = {
            "Cash Balance (EGP)": round(cash_balance, 2),
            "Stock Portfolio Cost Basis (EGP)": round(total_invested, 2),
            "Stock Portfolio Market Value (EGP)": round(total_market_value, 2),
            "Unrealized Stock P&L (EGP)": round(unrealized_pnl, 2),
            "Unrealized Stock P&L (%)": round(unrealized_pct, 2),
            "Realized P&L from Closed Trades (EGP)": round(realized_pnl_total, 2),
            "Total Account Equity / Net Worth (EGP)": round(total_equity, 2),
        }

        sector_summary = self.qe.compute_sector_analytics(processed_tickers_dict, sector_map)

        breakout_watchlist.sort(key=lambda x: x["Breakout Score"], reverse=True)
        breakout_watchlist = breakout_watchlist[: ACTION_THRESHOLDS["breakout_watch_max_results"]]

        if progress_callback:
            progress_callback(100, "Multi-factor confirmation matrix scan complete.")
        return (
            buy_recommendations,
            exit_strategies,
            top_10_by_category,
            closed_trades,
            financial_statement,
            sector_summary,
            breakout_watchlist,
        )
