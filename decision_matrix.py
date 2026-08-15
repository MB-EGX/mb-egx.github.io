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

import math
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
    PORTFOLIO_RISK_THRESHOLDS,
    get_logger,
)

import pandas as pd

from analytics import QuantitativeEngine
from db_manager import DatabaseManager
from session_picks import refresh_session_picks

logger = get_logger("decision_matrix")

# =============================================================================
# Optional display-language for the small set of scan-progress messages
# below ONLY (e.g. "Scanning multi-factor confirmation matrix..."). This is
# completely separate from - and never touches - the Action/Trend Class/
# Sector Status strings this module generates as actual data (those must
# stay English unconditionally: they're shipped into market_data.json for
# the website and matched against fixed value sets there and in Firestore
# rules). Self-contained here (no import from app_gui.py) so there's no
# circular-import risk, and the CLI pipeline (publish.py/export_json.py)
# never calls set_language(), so it always sees English and is unaffected.
# =============================================================================
_LANG = "EN"


def set_language(lang):
    global _LANG
    _LANG = lang if lang == "AR" else "EN"


def _t(en, ar):
    return ar if _LANG == "AR" else en



def _compute_target_fields(qe, df_ind, target_rec, buy_price, shares, curr_price):
    """Turn a stored profit target (percent-gain or EGP-amount) into the
    concrete numbers a user actually wants to see: what price to sell at,
    what % and EGP profit that represents, and a rough ETA based on the
    stock's own recent pace. Returns placeholder values if no target has
    been set for this position yet.
    """
    if not target_rec:
        return {
            "Target Price": None,
            "Target Profit %": None,
            "Target Profit (EGP)": None,
            "Est. Days to Target": _t("Not set", "غير محدد"),
        }

    mode = target_rec["target_mode"]
    value = target_rec["target_value"]
    if mode == "AMOUNT":
        target_profit_egp = value
        target_price = buy_price + (value / shares) if shares > 0 else buy_price
        target_pct = ((target_price - buy_price) / buy_price * 100) if buy_price > 0 else 0.0
    else:  # 'PCT'
        target_pct = value
        target_price = buy_price * (1 + value / 100.0)
        target_profit_egp = (target_price - buy_price) * shares

    eta = qe.estimate_days_to_target(df_ind, curr_price, target_price)
    if eta["eta_days"] is None:
        eta_display = _t(eta["reason"], eta["reason"])
    elif eta["eta_days"] == 0:
        eta_display = _t("Target already reached", "تم بلوغ الهدف بالفعل")
    else:
        eta_display = _t(f"~{eta['eta_days']} trading days", f"~{eta['eta_days']} يوم تداول")

    return {
        "Target Price": round(target_price, 4),
        "Target Profit %": round(target_pct, 2),
        "Target Profit (EGP)": round(target_profit_egp, 2),
        "Est. Days to Target": eta_display,
    }


def _compute_breakeven_fields(buy_price, shares, curr_price, cash_balance):
    """For a losing position, work out how many extra shares bought right
    now at the current (lower) price would blend the average cost down to
    breakeven — defined as the average cost at which selling the WHOLE
    (enlarged) position at today's price would net ~0 after round-trip
    fees. Once averaged down to that point, the stock no longer needs to
    climb all the way back to the original buy price before any further
    move up becomes real profit.

    Returns explanatory placeholder values whenever averaging down isn't
    applicable: the position isn't actually at a loss, or the loss is
    already smaller than the round-trip fee itself (nothing meaningful to
    average into).
    """
    if buy_price <= 0 or shares <= 0 or curr_price <= 0:
        return {
            "Breakeven Shares Needed": None,
            "Breakeven New Avg Cost": None,
            "Breakeven Cost (EGP)": None,
            "Breakeven Note": _t("Not applicable", "غير قابل للتطبيق"),
        }

    if curr_price >= buy_price:
        return {
            "Breakeven Shares Needed": 0,
            "Breakeven New Avg Cost": round(buy_price, 4),
            "Breakeven Cost (EGP)": 0.0,
            "Breakeven Note": _t("Position isn't at a loss", "المركز ليس في خسارة"),
        }

    # Breakeven target: the average cost at which selling everything right
    # now at curr_price is a wash after round-trip fees.
    target = curr_price * (1 + ROUND_TRIP_FEE_PCT)
    if target >= buy_price:
        return {
            "Breakeven Shares Needed": 0,
            "Breakeven New Avg Cost": round(buy_price, 4),
            "Breakeven Cost (EGP)": 0.0,
            "Breakeven Note": _t(
                "Already near breakeven (loss is inside round-trip fee cost)",
                "قريب من التعادل بالفعل (الخسارة داخل تكلفة الرسوم)",
            ),
        }

    raw_n = shares * (buy_price - target) / (target - curr_price)
    n_shares = max(0, math.ceil(raw_n))
    cost_egp = n_shares * curr_price * (1 + TRANSACTION_FEE_PCT)
    new_total_shares = shares + n_shares
    new_avg_cost = (
        (shares * buy_price + n_shares * curr_price) / new_total_shares
        if new_total_shares > 0
        else buy_price
    )

    afford_note = ""
    if cash_balance is not None and cost_egp > cash_balance:
        afford_note = _t(
            f" — needs ~{cost_egp:,.2f} EGP, more than your {cash_balance:,.2f} EGP cash balance",
            f" — يتطلب ~{cost_egp:,.2f} جنيه، أكثر من رصيدك النقدي البالغ {cash_balance:,.2f} جنيه",
        )

    return {
        "Breakeven Shares Needed": n_shares,
        "Breakeven New Avg Cost": round(new_avg_cost, 4),
        "Breakeven Cost (EGP)": round(cost_egp, 2),
        "Breakeven Note": _t(
            f"Buy {n_shares:,} more shares at {curr_price:,.4f} to reach breakeven{afford_note}",
            f"اشترِ {n_shares:,} سهمًا إضافيًا بسعر {curr_price:,.4f} للوصول لنقطة التعادل{afford_note}",
        ),
    }


def _confidence_weight(n_bars: int) -> float:
    if n_bars <= MIN_BARS_FOR_PATTERN_TRUST:
        return CONFIDENCE_FLOOR_WEIGHT
    if n_bars >= CONFIDENCE_FULL_TRUST_BARS:
        return 1.0
    span = CONFIDENCE_FULL_TRUST_BARS - MIN_BARS_FOR_PATTERN_TRUST
    progress = (n_bars - MIN_BARS_FOR_PATTERN_TRUST) / span
    return CONFIDENCE_FLOOR_WEIGHT + progress * (1.0 - CONFIDENCE_FLOOR_WEIGHT)


# Pass connect_db=False so background workers never try to open DuckDB!
def _kelly_fraction(win_rate: float, payoff_ratio: float) -> float:
    win_rate = max(0.0, min(1.0, float(win_rate)))
    payoff_ratio = max(float(payoff_ratio), 1e-6)
    raw = win_rate - ((1.0 - win_rate) / payoff_ratio)
    return max(0.0, min(raw * 0.5, 0.25))


def _build_signal_reason(action_cmd: str, trend_latest: str, confirmed: bool, weekly_aligned: bool, is_squeezed: bool, cmf: float, vol_ratio: float) -> str:
    reasons = [action_cmd, f"trend={trend_latest}"]
    reasons.append("confirmed" if confirmed else "awaiting confirmation")
    if weekly_aligned:
        reasons.append("weekly aligned")
    if is_squeezed:
        reasons.append("volatility squeeze")
    if cmf > 0:
        reasons.append("positive money flow")
    if vol_ratio >= 1.0:
        reasons.append(f"vol x{vol_ratio:.2f}")
    return " | ".join(reasons)


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

    @staticmethod
    def _compute_portfolio_risk(total_equity, sector_market_value, position_market_value):
        """Flag concentration risk: too much of the account in one sector or
        one ticker. Per-stock metrics (ATR stops, Sortino-weighted patterns,
        etc.) all look fine in isolation even when the account as a whole is
        one bad sector day away from a large loss - this is the check that
        catches that blind spot.
        """
        n_positions = len(position_market_value)
        result = {
            "total_equity": round(total_equity, 2),
            "sector_allocations": [],
            "position_allocations": [],
            "warnings": [],
        }
        if total_equity <= 0 or n_positions == 0:
            return result

        sector_alloc = sorted(
            (
                {
                    "sector": sec,
                    "value": round(val, 2),
                    "pct_of_equity": round((val / total_equity) * 100, 1),
                }
                for sec, val in sector_market_value.items()
            ),
            key=lambda r: r["pct_of_equity"],
            reverse=True,
        )
        position_alloc = sorted(
            (
                {
                    "ticker": tkr,
                    "value": round(val, 2),
                    "pct_of_equity": round((val / total_equity) * 100, 1),
                }
                for tkr, val in position_market_value.items()
            ),
            key=lambda r: r["pct_of_equity"],
            reverse=True,
        )
        result["sector_allocations"] = sector_alloc
        result["position_allocations"] = position_alloc

        if n_positions >= PORTFOLIO_RISK_THRESHOLDS["min_positions_for_warning"]:
            sec_warn_pct = PORTFOLIO_RISK_THRESHOLDS["sector_concentration_warn_pct"]
            pos_warn_pct = PORTFOLIO_RISK_THRESHOLDS["position_concentration_warn_pct"]
            if sector_alloc and sector_alloc[0]["pct_of_equity"] >= sec_warn_pct:
                top = sector_alloc[0]
                result["warnings"].append(
                    f"⚠️ {top['pct_of_equity']}% of your account equity is in "
                    f"{top['sector']} alone — a sector-wide move would hit "
                    f"most of your portfolio at once."
                )
            if position_alloc and position_alloc[0]["pct_of_equity"] >= pos_warn_pct:
                top = position_alloc[0]
                result["warnings"].append(
                    f"⚠️ {top['pct_of_equity']}% of your account equity is in "
                    f"{top['ticker']} alone — a single-stock stop-out would "
                    f"hurt more than your normal 1%-per-trade risk sizing implies."
                )
        return result

    def analyze_market(self, progress_callback=None):
        # Was an unbounded pull of EVERY bar ever ingested, for every ticker,
        # on every single analysis run. Nothing in the scoring below looks
        # back more than ~250 trading days, so capping this cuts the data
        # volume (and the indicator recompute cost) substantially without
        # changing any signal.
        market_data_bulk = self.qe.get_all_market_data_bulk(days=MATRIX_LOOKBACK_DAYS)
        tickers = list(market_data_bulk.keys())

        owned_dict = self.dbm.get_all_owned_stocks()
        position_targets = self.dbm.get_all_position_targets()
        closed_trades = self.dbm.get_all_closed_trades()
        cash_balance = self.dbm.get_cash_balance()
        sector_map = self.dbm.get_sector_map()

        buy_recommendations = []
        exit_strategies = []
        breakout_watchlist = []
        processed_tickers_dict: dict = {}

        total_invested = 0.0
        total_market_value = 0.0
        sector_market_value: dict = {}
        position_market_value: dict = {}
        processed_owned_tickers = set()

        total = len(tickers)
        if total == 0 and not owned_dict:
            if progress_callback:
                progress_callback(100, _t("No tickers found in database.", "لم يتم العثور على رموز أسهم في قاعدة البيانات."))
            empty_stmt = {
                "Cash Balance (EGP)": round(cash_balance, 2),
                "Stock Portfolio Cost Basis (EGP)": 0.0,
                "Stock Portfolio Market Value (EGP)": 0.0,
                "Unrealized Stock P&L (EGP)": 0.0,
                "Unrealized Stock P&L (%)": 0.0,
                "Realized P&L from Closed Trades (EGP)": 0.0,
                "Total Account Equity / Net Worth (EGP)": round(cash_balance, 2),
            }
            empty_risk = {
                "total_equity": round(cash_balance, 2),
                "sector_allocations": [],
                "position_allocations": [],
                "warnings": [],
            }
            session_picks = refresh_session_picks(
                self.dbm, buy_recommendations, {}, [], self.dbm.get_latest_market_date()
            )
            return (
                buy_recommendations,
                exit_strategies,
                {},
                closed_trades,
                empty_stmt,
                [],
                breakout_watchlist,
                empty_risk,
                session_picks,
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
                            _t(
                                f"Computing indicators & pattern matches: {done_tickers}/{len(eligible)}...",
                                f"جاري حساب المؤشرات ومطابقة الأنماط: {done_tickers}/{len(eligible)}...",
                            ),
                        )

        for idx, (ticker, norm_ticker, df, is_owned, n_bars) in enumerate(eligible):
            if progress_callback and idx % 5 == 0:
                progress_callback(
                    70 + int((idx / max(len(eligible), 1)) * 30),
                    _t(
                        f"Scanning multi-factor confirmation matrix: {ticker}...",
                        f"جاري فحص مصفوفة التأكيد متعددة العوامل: {ticker}...",
                    ),
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

                    sec = sector_map.get(norm_ticker, sector_map.get(ticker, "General / Diversified"))
                    sector_market_value[sec] = sector_market_value.get(sec, 0.0) + curr_val
                    position_market_value[norm_ticker] = position_market_value.get(norm_ticker, 0.0) + curr_val

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

                    target_fields = _compute_target_fields(
                        self.qe, df_ind, position_targets.get(norm_ticker), buy_price, shares, curr_price
                    )
                    breakeven_fields = _compute_breakeven_fields(
                        buy_price, shares, curr_price, cash_balance
                    )
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
                            **target_fields,
                            **breakeven_fields,
                        }
                    )
                    # The exit-strategy row above always gets computed for an
                    # owned position - that doesn't change. But unless there's
                    # no usable market data at all, don't stop here: fall
                    # through into the same buy-side scoring every other
                    # ticker gets, so a position that's down can still show
                    # up as a legitimate "average down / scale in" candidate
                    # in the Action Matrix, instead of only ever appearing
                    # in the Exits tab. n_bars < 15 or an empty df_ind means
                    # there's nothing reliable to score, so those still stop.
                    if df_ind.empty or n_bars < 15:
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
                macd_hist = latest.get("macd_histogram", 0.0)
                prev_macd_hist = prev.get("macd_histogram", macd_hist)
                bb_pct_b = latest.get("bb_percent_b", 0.5)
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
                pivots = self.qe.compute_pivot_points(df_ind)
                if macd_hist > 0 and prev_macd_hist <= 0:
                    macd_state = "🟢 Bullish Cross"
                elif macd_hist < 0 and prev_macd_hist >= 0:
                    macd_state = "🔴 Bearish Cross"
                elif macd_hist > 0:
                    macd_state = "Bullish"
                elif macd_hist < 0:
                    macd_state = "Bearish"
                else:
                    macd_state = "Neutral"

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

                # Buy-side take-profit target: same pattern-match / ATR-floor
                # blend already used for owned-position exits above, just
                # evaluated from the proposed entry instead of an existing
                # buy price. Gives every "buy" row an explicit sell/target
                # level to go with its stop-loss, instead of only the exit
                # tab having one.
                if pattern_data["match_found"]:
                    tp_floor_pct = ACTION_THRESHOLDS["take_profit_pattern_floor_pct"]
                    tp_expected_gain = max(pattern_data["projected_change_pct"], tp_floor_pct) / 100.0
                else:
                    tp_atr_floor_mult = ACTION_THRESHOLDS["take_profit_atr_floor_multiplier"]
                    tp_expected_gain = (
                        (atr * tp_atr_floor_mult) / curr_price if curr_price > 0 else 0.03
                    )
                take_profit_target = round(
                    curr_price * (1 + tp_expected_gain + ROUND_TRIP_FEE_PCT), 4
                )

                effective_entry_cost = curr_price * (1.0 + TRANSACTION_FEE_PCT)
                max_affordable_shares = (
                    int(cash_balance / effective_entry_cost) if effective_entry_cost > 0 else 0
                )
                raw_shares = int(risk_budget / stop_distance) if stop_distance > 0 else 0
                suggested_shares = min(raw_shares, max_affordable_shares)
                reward_risk = (max(take_profit_target - curr_price, 0.0) / stop_distance) if stop_distance > 0 else 0.0
                win_rate_est = (pattern_data.get("confidence", 45.0) / 100.0) if pattern_data.get("match_found") else 0.45
                kelly_pct = round(_kelly_fraction(win_rate_est, reward_risk) * 100.0, 2)
                projected_band = (
                    f"{pattern_data.get('lower_95_pct', 'N/A')}% to {pattern_data.get('upper_95_pct', 'N/A')}%"
                    if pattern_data.get("match_found") else "N/A"
                )
                signal_reason = _build_signal_reason(action_cmd, trend_latest, confirmed, weekly_aligned, is_squeezed, cmf, vol_ratio)

                buy_recommendations.append(
                    {
                        "Ticker": norm_ticker,
                        "Position": "🔁 OWNED - Scale-In Candidate" if is_owned else "New Candidate",
                        "Action": action_cmd,
                        "Rank Score": round(score, 1),
                        "Current Price": round(curr_price, 4),
                        "Target Entry (VWAP)": round(entry_target, 4),
                        "Suggested Stop-Loss": suggested_stop,
                        "Take-Profit Target": take_profit_target,
                        "Resistance (52W High)": round(float(range_high), 4),
                        "Support (52W Low)": round(float(range_low), 4),
                        "Pivot Point": pivots["pp"] if pivots else None,
                        "R1": pivots["r1"] if pivots else None,
                        "R2": pivots["r2"] if pivots else None,
                        "R3": pivots["r3"] if pivots else None,
                        "S1": pivots["s1"] if pivots else None,
                        "S2": pivots["s2"] if pivots else None,
                        "S3": pivots["s3"] if pivots else None,
                        "Suggested Shares (1% Risk)": suggested_shares,
                        "Projected Gain (%)": (
                            pattern_data["projected_change_pct"]
                            if pattern_data["match_found"]
                            else "N/A"
                        ),
                        "Pattern Conf (%)": (
                            pattern_data["confidence"] if pattern_data["match_found"] else "N/A"
                        ),
                        "Projected Range 95%": projected_band,
                        "Kelly %": kelly_pct,
                        "Signal Reason": signal_reason,
                        "Score Breakdown": {"pattern": round(pattern_component, 2), "projected": round(projected_component, 2), "range": round(range_pos_pct * SCORE_WEIGHTS["range_position_weight"], 2), "trend_bonus": round(trend_bonus, 2), "confidence_weight": round(conf_weight, 2)},
                        "Regime": pattern_data.get("regime", "N/A") if pattern_data else "N/A",
                        "Trend Class": trend_latest,
                        "RSI-14": round(rsi, 1),
                        "ADX-14": round(adx, 1),
                        "Vol Z-Score": round(vol_z, 2),
                        "MACD Signal": macd_state,
                        "MACD Histogram": round(float(macd_hist), 4),
                        "Bollinger %B": round(float(bb_pct_b), 3),
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
                target_fields = _compute_target_fields(
                    self.qe, pd.DataFrame(), position_targets.get(ticker), buy_price, shares, buy_price
                )
                breakeven_fields = _compute_breakeven_fields(
                    buy_price, shares, buy_price, cash_balance
                )
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
                        **target_fields,
                        **breakeven_fields,
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
                if cat in r["Action"]
                and "ILLIQUID" not in r["Action"]
                and r["Position"] == "New Candidate"
            ]
            top_10_by_category[cat] = filtered[:10]

        unrealized_pnl = total_market_value - total_invested
        unrealized_pct = (
            ((unrealized_pnl / total_invested) * 100) if total_invested > 0 else 0.0
        )
        realized_pnl_total = sum(t["Realized P&L (EGP)"] for t in closed_trades)
        total_equity = cash_balance + total_market_value

        portfolio_risk = self._compute_portfolio_risk(
            total_equity, sector_market_value, position_market_value
        )

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

        # Session Picks: check active picks for achievement + refill each
        # bucket back up to quota. Runs here (not in export_json.py / the
        # GUI separately) so "Execute Matrix" in the desktop app and the
        # unattended nightly export always agree on picks/achievements.
        session_picks = refresh_session_picks(
            self.dbm, buy_recommendations, top_10_by_category, sector_summary,
            self.dbm.get_latest_market_date(),
        )

        if progress_callback:
            progress_callback(100, _t("Multi-factor confirmation matrix scan complete.", "اكتمل فحص مصفوفة التأكيد متعددة العوامل."))
        return (
            buy_recommendations,
            exit_strategies,
            top_10_by_category,
            closed_trades,
            financial_statement,
            sector_summary,
            breakout_watchlist,
            portfolio_risk,
            session_picks,
        )



def filter_recommendations(rows: list[dict], action_in=None, sector_in=None, min_score=None) -> list[dict]:
    out = []
    action_in = set(action_in or [])
    sector_in = set(sector_in or [])
    for row in rows or []:
        if action_in and row.get('Action') not in action_in:
            continue
        if sector_in and row.get('Sector') not in sector_in and row.get('Sector Name') not in sector_in:
            continue
        if min_score is not None:
            try:
                if float(row.get('Rank Score', 0)) < float(min_score):
                    continue
            except Exception:
                continue
        out.append(row)
    return out
