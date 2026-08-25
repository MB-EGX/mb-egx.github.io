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
from datetime import date

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
    CASH_DRAG_LOW_PCT,
    PRIMARY_BENCHMARK_TICKER,
    SECTOR_BENCHMARK_MAP,
    BREAKOUT_WATCH_BULL_REGIME_BONUS,
    BREAKOUT_WATCH_BEAR_REGIME_PENALTY,
    PATTERN_DETECTION,
    LONG_TERM_SETUP,
    MIN_AVG_VOLUME_LONG_TERM,
    POSITION_SIZE_FEE_ADJUST,
    KELLY_FRACTION,
    KELLY_CAP_FRACTION,
    DEFAULT_WIN_RATE_PRIOR,
    MIN_BACKTEST_TRADES_FOR_LIVE_WIN_RATE,
    get_logger,
)

import pandas as pd

from analytics import QuantitativeEngine
from chart_patterns import PatternDetector
from db_manager import DatabaseManager
from session_picks import refresh_session_picks, emit_alert
from backtester import load_win_rate_cache, _action_family
from market_regime import (
    normalized_benchmark_set,
    load_all_benchmark_indicators,
    live_regime_snapshot,
    build_close_by_date,
    benchmark_label,
    get_sector_benchmark_ticker,
)
from sector_rotation import live_rotation_snapshot
from usd_divergence import live_divergence_snapshot

logger = get_logger("decision_matrix")

# Module-level cache for the backtested per-action win rates (see
# backtester.save_win_rate_cache / refresh_win_rate_cache.py). Loaded once
# per process (a fresh analyze_market() call within the same process reuses
# it - the cache file itself only changes when refresh_win_rate_cache.py is
# re-run, not every session) rather than re-reading the JSON file once per
# ticker in the scoring loop.
_WIN_RATE_CACHE: dict | None = None


def _get_backtested_win_rate(raw_action: str) -> tuple[float | None, int | None, bool]:
    """(win_rate_fraction, trade_count, is_reliable) for the action family
    ``raw_action`` belongs to, from the REAL out-of-sample backtest cache -
    or (None, None, False) if the cache hasn't been generated yet
    (refresh_win_rate_cache.py has never run) or this family doesn't have
    enough trades yet (config.MIN_BACKTEST_TRADES_FOR_LIVE_WIN_RATE).
    Callers must fall back to DEFAULT_WIN_RATE_PRIOR in either case - never
    fabricate a win rate that wasn't actually measured."""
    global _WIN_RATE_CACHE
    if _WIN_RATE_CACHE is None:
        _WIN_RATE_CACHE = load_win_rate_cache()
    by_action = _WIN_RATE_CACHE.get("by_action", {})
    family = _action_family(raw_action)
    stats = by_action.get(family)
    if not stats or not stats.get("is_reliable"):
        return None, (stats.get("trade_count") if stats else None), False
    return stats["win_rate_pct"] / 100.0, stats["trade_count"], True


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



def _check_long_term_setup(df_ind: pd.DataFrame) -> dict:
    """Long-term (2-6 month) Session Picks quality gate: real geometric
    structure, not just a favorable indicator snapshot. Two independent
    checks, BOTH required:

      1. Ascending-lows swing structure - the last two swing troughs (T1,
         the older; T2, the more recent) directly from PatternDetector's
         own swing points must show T2 >= T1 * (1 + LONG_TERM_SETUP[
         "swing_ascending_low_min_pct"] / 100) - a strictly higher low,
         not merely "not lower".
      2. An active bullish geometric pattern match (Cup & Handle, Ascending
         Triangle, Double Bottom, Inverse H&S, Bull Flag, ...) at/above
         PATTERN_DETECTION["min_quality"] - reuses chart_patterns.py's own
         "direction" field (see LONG_TERM_SETUP's docstring in config.py)
         rather than a hardcoded pattern-name whitelist.

    Returns {"confirmed": bool, "reasons": [...]} - reasons is always
    populated (why it passed/failed) so the row's Signal Reason can show
    it, and never raises: any detection failure is treated as "gate not
    cleared", same fail-closed contract as this app's other feature gates
    (e.g. sector_rotation.py / usd_divergence.py's "available: False").
    """
    reasons = []
    try:
        min_bars = PATTERN_DETECTION["min_bars_required"]
        if df_ind is None or df_ind.empty or len(df_ind) < min_bars:
            return {"confirmed": False, "reasons": ["Not enough history for a long-term setup check"]}

        detector = PatternDetector(
            df_ind, epsilon=PATTERN_DETECTION["epsilon"], order=PATTERN_DETECTION["order"],
        )

        troughs = [s for s in detector.swings if s.kind == "T"]
        ascending_ok = False
        if len(troughs) >= 2:
            t1, t2 = troughs[-2], troughs[-1]
            min_pct = LONG_TERM_SETUP["swing_ascending_low_min_pct"]
            ascending_ok = t2.price >= t1.price * (1 + min_pct / 100.0)
        reasons.append("Higher-low swing structure confirmed" if ascending_ok
                        else "No confirmed higher-low swing structure yet")

        min_quality = PATTERN_DETECTION["min_quality"]
        want_direction = LONG_TERM_SETUP["required_pattern_direction"]
        patterns = detector.detect_all(dedupe=True)
        best_bullish = max(
            (p for p in patterns if p.get("direction") == want_direction),
            key=lambda p: p.get("quality", 0.0),
            default=None,
        )
        pattern_ok = bool(best_bullish) and best_bullish.get("quality", 0.0) >= min_quality
        reasons.append(
            f"{best_bullish['pattern']} match (quality {best_bullish.get('quality', 0):.2f})"
            if pattern_ok else "No bullish pattern match at required quality"
        )

        return {"confirmed": ascending_ok and pattern_ok, "reasons": reasons}
    except Exception as e:
        logger.warning(f"Long-term setup check failed: {e}")
        return {"confirmed": False, "reasons": ["Setup check failed"]}


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


def _compute_exit_analytics_fields(df_ind, p_date, buy_price, curr_price, trailing_stop,
                                    pnl_egp, pnl_pct, invested_val, curr_val):
    """Extra per-position analytics for the Exits tab, layered on top of
    the existing P&L / trailing-stop / take-profit fields:

      - Days Held / Annualized Return (%) — separates a slow multi-week
        grind from a fast one-week pop that would otherwise show the same
        raw P&L (%).
      - Distance to Stop (%) — how close the CURRENT price already is to
        crossing the trailing-stop line: a leading indicator, unlike the
        Action column's CUT LOSS flag which only fires once it's already
        crossed.
      - Peak Price Since Purchase / Drawdown from Peak (%) — P&L (%) is
        only ever measured against the buy price, so a position that ran
        hard and has since given most of it back reads identically to one
        that climbed steadily to the same level.
      - Net P&L (EGP) / Net P&L (%) — P&L (EGP)/(%) above ignore trading
        fees entirely. This is what's actually left after the buy-side fee
        already paid AND the sell-side fee that would be paid to close the
        position today (config.TRANSACTION_FEE_PCT each way).

    Every field degrades to None (rendered as "-" by both UIs) rather than
    raising when the inputs needed for it are missing — this must never be
    the reason an Exits row fails to render.
    """
    today = date.today()
    days_held = None
    try:
        p_d = date.fromisoformat(str(p_date)[:10])
        days_held = (today - p_d).days
    except (ValueError, TypeError):
        p_d = None

    annualized_return_pct = None
    if days_held and days_held > 0:
        growth = 1 + (pnl_pct / 100.0)
        if growth > 0:
            annualized_return_pct = round(((growth ** (365.0 / days_held)) - 1) * 100.0, 2)

    distance_to_stop_pct = None
    if curr_price and curr_price > 0 and trailing_stop is not None:
        distance_to_stop_pct = round(((curr_price - trailing_stop) / curr_price) * 100.0, 2)

    peak_price = None
    try:
        if p_d is not None and df_ind is not None and not df_ind.empty and "close" in df_ind.columns:
            since_purchase = df_ind.loc[df_ind.index >= pd.Timestamp(p_d), "close"]
            if not since_purchase.empty:
                peak_price = float(since_purchase.max())
    except (ValueError, TypeError, KeyError):
        peak_price = None
    if not peak_price or peak_price <= 0:
        # No price history since purchase (very new position / low data) -
        # fall back to the higher of buy price and current price so the
        # drawdown figure still degrades sensibly instead of disappearing.
        peak_price = max(buy_price or 0.0, curr_price or 0.0) or None

    drawdown_from_peak_pct = None
    if peak_price and peak_price > 0 and curr_price is not None:
        drawdown_from_peak_pct = round(((curr_price / peak_price) - 1) * 100.0, 2)

    buy_fee = invested_val * TRANSACTION_FEE_PCT
    sell_fee = curr_val * TRANSACTION_FEE_PCT
    net_pnl_egp = round(pnl_egp - buy_fee - sell_fee, 2)
    net_pnl_pct = round((net_pnl_egp / invested_val) * 100.0, 2) if invested_val > 0 else 0.0

    return {
        "Days Held": days_held,
        "Annualized Return (%)": annualized_return_pct,
        "Distance to Stop (%)": distance_to_stop_pct,
        "Peak Price Since Purchase": round(peak_price, 4) if peak_price else None,
        "Drawdown from Peak (%)": drawdown_from_peak_pct,
        "Net P&L (EGP)": net_pnl_egp,
        "Net P&L (%)": net_pnl_pct,
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
    """Fractional-Kelly position fraction, standard form:
    f* = (p*b - q) / b  where p=win_rate, q=1-p, b=payoff_ratio
    (equivalently p - q/b), then scaled by KELLY_FRACTION (half-Kelly by
    default) and capped at KELLY_CAP_FRACTION. The 0.5/0.25 scaling was
    previously hardcoded inline; it now lives in config so the risk
    posture is tunable in one place. Full Kelly is the growth-optimal
    bet but is far too aggressive for real trading (tiny errors in the
    win-rate estimate are magnified), which is why the standard practice
    is to bet a fraction of it."""
    win_rate = max(0.0, min(1.0, float(win_rate)))
    payoff_ratio = max(float(payoff_ratio), 1e-6)
    raw = win_rate - ((1.0 - win_rate) / payoff_ratio)
    return max(0.0, min(raw * KELLY_FRACTION, KELLY_CAP_FRACTION))


def _graduated_score(value: float, soft_lo: float, lo: float, hi: float, soft_hi: float, max_pts: float) -> float:
    """Smooth, cliff-free scoring for the Pre-Breakout Watchlist (v2).

    Full ``max_pts`` credit for any value inside [lo, hi]. Credit tapers
    LINEARLY to zero between soft_lo->lo and hi->soft_hi, and is exactly
    zero outside [soft_lo, soft_hi]. This replaces the old binary
    in-band/out-of-band checks, whose hard edges meant a stock at, say,
    ADX 25.3 (vs. a 15-25 band) scored a flat zero on that factor despite
    being economically indistinguishable from one at 24.7. A real
    pre-breakout candidate can very plausibly sit just outside any single
    hand-picked band on any single factor - the graduated taper means one
    borderline reading no longer silently zeroes out the whole factor,
    while values already well past strict criteria still earn full marks.
    """
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return 0.0
    if lo <= value <= hi:
        return max_pts
    if soft_lo < value < lo and (lo - soft_lo) > 0:
        return max_pts * (value - soft_lo) / (lo - soft_lo)
    if hi < value < soft_hi and (soft_hi - hi) > 0:
        return max_pts * (soft_hi - value) / (soft_hi - hi)
    return 0.0


def sector_benchmark_label(sec_name: str) -> str:
    """Display string for the Pre-Breakout Watchlist's "Sector Index RS"
    reason (e.g. "its sector index (EGX Banks)"), or a generic fallback
    if this sector has no dedicated EGX sub-index in config.
    SECTOR_BENCHMARK_MAP - keeps the reason string informative without
    the caller needing to know whether a mapped index exists."""
    bench_ticker = get_sector_benchmark_ticker(sec_name)
    if not bench_ticker:
        return "its sector index"
    return f"its sector index ({benchmark_label(bench_ticker)})"


def _recent_failed_resistance_test(df_ind, resistance_level: float, curr_price: float,
                                     lookback: int, near_pct: float, reject_pct: float) -> bool:
    """True if price tested (came within ``near_pct``% of) ``resistance_
    level`` within the last ``lookback`` bars but has since pulled back at
    least ``reject_pct``% from that level without closing above it - i.e.
    a recent failed breakout attempt. Chasing a resistance level that just
    rejected the stock is a materially worse setup than a fresh approach,
    even though both can otherwise look identical on RSI/ADX/volume,
    which is why the base score doesn't already capture this.

    ``resistance_level`` should be the ticker's nearest genuine (touch-
    confirmed) resistance zone when one exists (see analytics.
    compute_support_resistance), falling back to the plain 250-day range
    high only when no qualifying zone has formed yet - testing the real
    nearest level a stock is fighting is what actually matters here, not
    necessarily the all-time high of the lookback window, which a name
    can be nowhere near while still being genuinely capped by a much
    closer level."""
    try:
        highs = df_ind["high"].iloc[-lookback:]
    except Exception:
        return False
    if highs.empty or resistance_level <= 0:
        return False
    tested = bool((highs >= resistance_level * (1 - near_pct / 100.0)).any())
    if not tested:
        return False
    pulled_back = curr_price <= resistance_level * (1 - reject_pct / 100.0)
    return bool(tested and pulled_back)


def _build_signal_reason(action_cmd: str, trend_latest: str, confirmed: bool, weekly_aligned: bool, is_squeezed: bool, cmf: float, vol_ratio: float) -> str:
    reasons = [action_cmd, f"trend={trend_latest}"]
    if "HOLD / NEUTRAL" in action_cmd:
        reasons.append("no decisive edge")
    else:
        reasons.append("confirmed" if confirmed else "awaiting confirmation")
    if weekly_aligned:
        reasons.append("weekly aligned")
    if is_squeezed and "HOLD / NEUTRAL" not in action_cmd:
        reasons.append("volatility squeeze")
    if cmf > 0 and "HOLD / NEUTRAL" not in action_cmd:
        reasons.append("positive money flow")
    if vol_ratio >= 1.0:
        reasons.append(f"vol x{vol_ratio:.2f}")
    return " | ".join(reasons)


def _build_enrichment_fields(action_cmd: str, enrichment: dict | None) -> dict:
    """Fundamentals/external-rating/period-return fields (see db_manager.
    get_latest_enrichment) for one ticker's row, plus a conservative,
    INFORMATIONAL-ONLY divergence note when this app's own action strongly
    disagrees with the external multi-timeframe rating consensus.

    Deliberately never gates or rescales anything - a third-party rating
    consensus is a different methodology from this app's own indicator
    stack, and this app's own thresholds were just tightened specifically
    to be trustworthy on their own terms (see config.ACTION_THRESHOLDS'
    VWAP/squeeze/CMF gates). Silently overriding or scoring against an
    unrelated external opinion would undo that. This only ever ADDS a
    visible note for a human to weigh - never changes Rank Score or Action.

    Returns a dict of all enrichment fields (None for any ticker with no
    enrichment row at all - see get_latest_enrichment's "missing means
    not available" contract) plus "External Rating Note".
    """
    fields = {
        "P/E Ratio": None, "EPS": None, "Beta": None, "Dividend Yield %": None,
        "Market Cap": None, "1W Return %": None, "1M Return %": None,
        "YTD Return %": None, "1Y Return %": None, "3Y Return %": None,
        "External Rating Consensus": None, "External Rating Note": None,
    }
    if not enrichment:
        return fields

    fields["P/E Ratio"] = enrichment.get("pe_ratio")
    fields["EPS"] = enrichment.get("eps")
    fields["Beta"] = enrichment.get("beta")
    fields["Dividend Yield %"] = enrichment.get("yield_pct")
    fields["Market Cap"] = enrichment.get("market_cap")
    fields["1W Return %"] = enrichment.get("return_1w_pct")
    fields["1M Return %"] = enrichment.get("return_1m_pct")
    fields["YTD Return %"] = enrichment.get("return_ytd_pct")
    fields["1Y Return %"] = enrichment.get("return_1y_pct")
    fields["3Y Return %"] = enrichment.get("return_3y_pct")

    consensus = enrichment.get("rating_consensus_score")
    fields["External Rating Consensus"] = consensus
    if consensus is not None:
        our_side = (
            1 if ("BUY" in action_cmd or "ACCUMULATE" in action_cmd)
            else -1 if "SELL" in action_cmd
            else 0
        )
        # Only flag a MEANINGFUL disagreement (our own bullish/bearish call
        # vs a clearly opposite external consensus, not just "slightly less
        # enthusiastic") - a consensus near 0 (mixed/neutral across
        # timeframes) is not a disagreement worth surfacing.
        if our_side == 1 and consensus <= -1:
            fields["External Rating Note"] = f"Diverges from external consensus ({consensus:+.1f}/2, bearish)"
        elif our_side == -1 and consensus >= 1:
            fields["External Rating Note"] = f"Diverges from external consensus ({consensus:+.1f}/2, bullish)"
    return fields


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
    def _compute_portfolio_risk(total_equity, sector_market_value, position_market_value, cash_balance=0.0):
        """Flag concentration risk: too much of the account in one sector or
        one ticker. Per-stock metrics (ATR stops, Sortino-weighted patterns,
        etc.) all look fine in isolation even when the account as a whole is
        one bad sector day away from a large loss - this is the check that
        catches that blind spot.

        Also carries two related, cheaper-to-compute portfolio-level notes
        that belong next to it rather than as their own separate return
        values (keeps analyze_market()'s return signature unchanged):
          - cash_drag_pct / low_cash_drag — % of total equity sitting in
            cash right now, and whether it's below config.CASH_DRAG_LOW_PCT
            ("fully invested" / no dry powder for new signals).
          - each position_allocations entry's "risk_multiple" — that
            position's % of equity divided by config.RISK_PER_TRADE_PCT (as
            a %), i.e. "how many multiples of a normal 1%-risk position
            size this position actually is". This is what the Exits tab's
            per-row risk flag (see analyze_market) reads from.
        """
        n_positions = len(position_market_value)
        cash_drag_pct = round((cash_balance / total_equity) * 100, 1) if total_equity > 0 else 0.0
        result = {
            "total_equity": round(total_equity, 2),
            "sector_allocations": [],
            "position_allocations": [],
            "warnings": [],
            "cash_drag_pct": cash_drag_pct,
            "low_cash_drag": cash_drag_pct < CASH_DRAG_LOW_PCT,
            "warning_subjects": [],  # dedup keys parallel to "warnings", e.g. "sector:Banks"
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
        risk_unit_pct = RISK_PER_TRADE_PCT * 100  # e.g. 1.0 for a 1%-risk normal position
        position_alloc = sorted(
            (
                {
                    "ticker": tkr,
                    "value": round(val, 2),
                    "pct_of_equity": round((val / total_equity) * 100, 1),
                    "risk_multiple": round((val / total_equity) * 100 / risk_unit_pct, 1) if risk_unit_pct > 0 else None,
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
                result["warning_subjects"].append(f"sector:{top['sector']}")
            if position_alloc and position_alloc[0]["pct_of_equity"] >= pos_warn_pct:
                top = position_alloc[0]
                result["warnings"].append(
                    f"⚠️ {top['pct_of_equity']}% of your account equity is in "
                    f"{top['ticker']} alone ({top['risk_multiple']}x your normal "
                    f"1%-risk sizing) — a single-stock stop-out would hurt more "
                    f"than your normal 1%-per-trade risk sizing implies."
                )
                result["warning_subjects"].append(f"position:{top['ticker']}")
        return result

    @staticmethod
    def _compute_rotation_flags(buy_recommendations, processed_owned_tickers, top_n_candidates=1, max_flags=10):
        """"You're holding X but the matrix now prefers Y more": for each
        currently-held ticker, checks whether an UNHELD ticker in the SAME
        signal category (STRONG BUY / BREAKOUT BUY / ACCUMULATE / BUY ON
        DIP) currently scores higher. Compares apples to apples - a held
        HOLD-ON-DIP position is only ever measured against other dip
        candidates, never against an unrelated STRONG BUY - since the two
        categories represent different setups, not a strict ranking of one
        universe. Held tickers with no recognized category (e.g. already in
        a SELL/AVOID state) are skipped rather than guessed at.
        """
        categories = ["🔥 STRONG BUY", "⚡ BREAKOUT BUY", "📈 ACCUMULATE", "⏳ BUY ON DIP"]
        by_ticker = {r["Ticker"]: r for r in buy_recommendations}
        unheld_by_category = {
            cat: sorted(
                (r for r in buy_recommendations if cat in r["Action"] and "ILLIQUID" not in r["Action"] and r["Position"] == "New Candidate"),
                key=lambda r: r["Rank Score"], reverse=True,
            )
            for cat in categories
        }
        flags = []
        for ticker in processed_owned_tickers:
            held = by_ticker.get(ticker)
            if not held:
                continue
            held_cat = next((c for c in categories if c in held["Action"]), None)
            if not held_cat:
                continue
            candidates = [c for c in unheld_by_category[held_cat] if c["Ticker"] != ticker][:top_n_candidates]
            for cand in candidates:
                if cand["Rank Score"] > held["Rank Score"]:
                    flags.append({
                        "held_ticker": ticker,
                        "held_score": held["Rank Score"],
                        "candidate_ticker": cand["Ticker"],
                        "candidate_score": cand["Rank Score"],
                        "category": held_cat,
                    })
        flags.sort(key=lambda f: f["candidate_score"] - f["held_score"], reverse=True)
        return flags[:max_flags]

    def analyze_market(self, progress_callback=None):
        # Was an unbounded pull of EVERY bar ever ingested, for every ticker,
        # on every single analysis run. Nothing in the scoring below looks
        # back more than ~250 trading days, so capping this cuts the data
        # volume (and the indicator recompute cost) substantially without
        # changing any signal.
        market_data_bulk = self.qe.get_all_market_data_bulk(days=MATRIX_LOOKBACK_DAYS)
        # Exclude EGX30/EGX70/... benchmark-index feeds from the
        # tradeable/scored universe (see config.BENCHMARK_TICKERS) - an
        # index LEVEL is not something you place a buy/sell order on the
        # way you do COMI or HRHO, so it must never be classified,
        # ranked, or turned into a Session Pick like an ordinary stock.
        # market_regime.py reads these same rows separately for
        # regime/relative-strength calculations.
        _benchmark_norms = normalized_benchmark_set(self.dbm)
        tickers = [
            t for t in market_data_bulk.keys()
            if self.dbm.normalize_symbol(t) not in _benchmark_norms
        ]

        # -------------------------------------------------------------------
        # LIVE market regime + per-sector benchmark data (config.
        # BENCHMARK_TICKERS / SECTOR_BENCHMARK_MAP). Previously this data
        # was only ever computed inside the offline backtester/factor
        # harness (run_backtest.py --save, run_factor_backtest.py) - this
        # live run never consulted it. Computed once per run, up front,
        # from the SAME market_data_bulk pull above (no extra DB round
        # trip), and reused below for: (1) the Pre-Breakout Watchlist's
        # regime-based bw_score nudge and "Sector Index RS" factor, (2)
        # the "market_regime" block returned to callers (app_gui.py /
        # export_json.py) for a header badge, and (3) Session Picks' live
        # alpha-vs-benchmark (see refresh_session_picks call below).
        # Every consumer here treats a missing/not-yet-ingested benchmark
        # as "feature unavailable this run", never a hard failure - see
        # market_regime.load_all_benchmark_indicators's own graceful-empty
        # behavior per ticker.
        # -------------------------------------------------------------------
        benchmark_frames = load_all_benchmark_indicators(self.qe, market_data_bulk=market_data_bulk)
        benchmark_regimes = live_regime_snapshot(benchmark_frames)
        primary_norm = self.dbm.normalize_symbol(PRIMARY_BENCHMARK_TICKER)
        primary_regime_info = benchmark_regimes.get(primary_norm, {})
        primary_market_regime = primary_regime_info.get("regime", "unknown")
        primary_bench_close_by_date = build_close_by_date(benchmark_frames.get(primary_norm, pd.DataFrame()))

        # Sector-name -> that sector's own EGX sub-index indicator frame,
        # for whichever sectors have a mapped benchmark (config.
        # SECTOR_BENCHMARK_MAP) AND that benchmark's data has actually
        # been ingested. Built once here (not per-ticker) since multiple
        # tickers usually share a sector.
        sector_bench_frames = {}
        for sec_name, bench_ticker in SECTOR_BENCHMARK_MAP.items():
            bnorm = self.dbm.normalize_symbol(bench_ticker)
            if bnorm in benchmark_frames:
                sector_bench_frames[sec_name] = benchmark_frames[bnorm]

        # Small, additive nudge to the Pre-Breakout Watchlist's composite
        # score based on the broad-market regime (see config.
        # BREAKOUT_WATCH_BULL_REGIME_BONUS / BEAR_REGIME_PENALTY's own
        # docstring for why this is additive rather than a hard gate).
        regime_score_adj = {
            "bull": BREAKOUT_WATCH_BULL_REGIME_BONUS,
            "bear": BREAKOUT_WATCH_BEAR_REGIME_PENALTY,
        }.get(primary_market_regime, 0.0)

        # Public, non-sensitive - benchmark index LEVELS, same trust
        # boundary as the rest of market_matrix/sectors (no account data).
        # Returned from every analyze_market() code path (including the
        # early "no tickers" return below) so a caller can always show a
        # regime badge even before any stock has been ingested.
        # "benchmarks" is keyed by normalized ticker (e.g. "EGBANK.CA")
        # and includes every configured benchmark whose data is currently
        # available - not just the primary one - so a UI can show
        # per-sector-index regimes too (Sectors tab), not only the single
        # broad-market badge.
        # Sector-rotation (EGX IMCS vs EGX Text Double) and EGX30 EGP-vs-
        # USD divergence - computed from the SAME benchmark_frames pull
        # above, no extra DB round trip. Both are "available: False with
        # a reason" rather than a hard failure whenever either leg isn't
        # ingested yet or there isn't enough shared history - see each
        # module's own docstring for the exact rule and config.
        # SECTOR_ROTATION_* / USD_DIVERGENCE_* for the tunables.
        sector_rotation_snapshot = live_rotation_snapshot(benchmark_frames, self.dbm)
        usd_divergence_snapshot = live_divergence_snapshot(benchmark_frames, self.dbm)

        market_regime_summary = {
            "primary": {
                "ticker": PRIMARY_BENCHMARK_TICKER,
                "label": benchmark_label(PRIMARY_BENCHMARK_TICKER),
                "regime": primary_market_regime,
                "close": primary_regime_info.get("close"),
                "as_of": primary_regime_info.get("as_of"),
            },
            "benchmarks": dict(benchmark_regimes),
            "sector_rotation": sector_rotation_snapshot,
            "usd_divergence": usd_divergence_snapshot,
        }

        owned_dict = self.dbm.get_all_owned_stocks()
        position_targets = self.dbm.get_all_position_targets()
        closed_trades = self.dbm.get_all_closed_trades()
        cash_balance = self.dbm.get_cash_balance()
        sector_map = self.dbm.get_sector_map()
        # Watchlist enrichment (fundamentals, external rating consensus,
        # provider period returns) - see db_manager.get_latest_enrichment
        # and ingestion.py's ENRICHMENT_FIELD_EXACT. {} for any ticker never
        # fed via an enrichment-bearing CSV - handled per-ticker below as
        # "not available", never invented.
        enrichment_map = self.dbm.get_latest_enrichment()

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
                self.dbm, buy_recommendations, {}, [], self.dbm.get_latest_market_date(),
                bench_close_by_date=primary_bench_close_by_date,
                benchmark_label=benchmark_label(PRIMARY_BENCHMARK_TICKER),
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
                market_regime_summary,
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

        # -------------------------------------------------------------------
        # Lightweight pre-pass: per-sector average 5-day return, used by the
        # Pre-Breakout Watchlist's relative-strength factor below. Needs to
        # happen BEFORE the main per-ticker loop (which is where each
        # ticker's own bw_score gets computed) since a ticker's relative
        # strength is only meaningful once every other ticker's 5D return in
        # its sector is known. Cheap: just two closes per ticker, no new
        # indicator computation, reuses the already-parallelized precomputed
        # indicator frames.
        # -------------------------------------------------------------------
        sector_5d_returns: dict = {}
        for ticker, norm_ticker, df, is_owned, n_bars in eligible:
            df_ind_pre, _ = precomputed.get(ticker, (pd.DataFrame(), None))
            if df_ind_pre.empty or len(df_ind_pre) < 6:
                continue
            try:
                c_now = df_ind_pre["close"].iloc[-1]
                c_5d_ago = df_ind_pre["close"].iloc[-6]
                if c_5d_ago > 0:
                    sec = sector_map.get(norm_ticker, sector_map.get(ticker, "General / Diversified"))
                    sector_5d_returns.setdefault(sec, []).append(((c_now - c_5d_ago) / c_5d_ago) * 100.0)
            except Exception:
                continue
        sector_avg_5d = {
            sec: (sum(vals) / len(vals)) for sec, vals in sector_5d_returns.items() if vals
        }

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
                        atr = self.qe.estimate_atr(latest, curr_price)

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
                        atr = None
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
                    exit_analytics_fields = _compute_exit_analytics_fields(
                        df_ind, p_date, buy_price, curr_price, trailing_stop,
                        pnl_egp, pnl_pct, invested_val, curr_val,
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
                            "ATR-14": round(atr, 4) if atr is not None else None,
                            "Data Confidence": data_conf_tier,
                            "Purchase Date": p_date,
                            "Sector": sec,
                            **target_fields,
                            **breakeven_fields,
                            **exit_analytics_fields,
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
                atr = self.qe.estimate_atr(latest, curr_price)

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
                try:
                    pivots = self.qe.compute_pivot_points(df_ind)
                except Exception as e:
                    logger.warning(f"{norm_ticker}: pivot points failed ({e}) - continuing without them")
                    pivots = None
                try:
                    sr = self.qe.compute_support_resistance(df_ind)
                except Exception as e:
                    logger.warning(f"{norm_ticker}: support/resistance clustering failed ({e}) - continuing without it")
                    sr = None
                # Nearest genuine (touch-confirmed) resistance, falling back to
                # the plain range extreme when no qualifying cluster exists yet
                # (thin history, or price simply hasn't reversed near-term) -
                # see analytics.compute_support_resistance's own docstring for
                # why this is a different, more accurate concept than range_high.
                nearest_resistance = (sr["resistance"]["level"] if sr and sr.get("resistance") else range_high)
                nearest_support = (sr["support"]["level"] if sr and sr.get("support") else range_low)
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
                    curr_price < sma50 * ACTION_THRESHOLDS["sell_trend_price_ratio"]
                    and rsi <= ACTION_THRESHOLDS["sell_trend_rsi_max"]
                    and cmf <= ACTION_THRESHOLDS["sell_trend_cmf_max"]
                ):
                    raw_action = "🛑 SELL / AVOID"
                    trend_bonus = SCORE_WEIGHTS["sell_avoid"] * 0.6
                    needs_confirmation = False
                elif (
                    range_pos_pct <= ACTION_THRESHOLDS["buy_on_dip_range_pos_max"]
                    or rsi <= ACTION_THRESHOLDS["buy_on_dip_rsi_max"]
                ):
                    raw_action = "⏳ BUY ON DIP"
                    trend_bonus = SCORE_WEIGHTS["buy_on_dip"]
                    needs_confirmation = False
                elif (
                    range_pos_pct >= ACTION_THRESHOLDS["accumulate_range_pos_min"]
                    and rsi >= ACTION_THRESHOLDS["accumulate_rsi_min"]
                    and cmf >= ACTION_THRESHOLDS["accumulate_cmf_min"]
                    and (curr_price >= ema20 or curr_price >= sma50)
                ):
                    raw_action = "📈 ACCUMULATE"
                    trend_bonus = SCORE_WEIGHTS["accumulate"]
                    needs_confirmation = False
                else:
                    raw_action = "🟡 HOLD / NEUTRAL"
                    trend_bonus = SCORE_WEIGHTS["hold_neutral"]
                    needs_confirmation = False

                # Confirmation gates
                strong_trend = adx >= ACTION_THRESHOLDS["strong_trend_adx_min"]
                vol_confirmed = (
                    vol_ratio >= ACTION_THRESHOLDS["volume_ratio_threshold"]
                    or vol_z >= ACTION_THRESHOLDS["volume_z_score_threshold"]
                )
                # VWAP acceptance gate: a close below its own 20D VWAP still
                # carries intraday selling pressure that frequently stalls a
                # breakout/strong-buy attempt the next session - require
                # real VWAP acceptance, not just a raw price/RSI/ADX/volume
                # match, before a STRONG BUY / BREAKOUT BUY confirms.
                vwap_ok = curr_price >= vwap * ACTION_THRESHOLDS["vwap_acceptance_ratio"]
                # Squeeze-release gate: ONLY applied to the reactive BREAKOUT
                # BUY labels (not STRONG BUY, which is a different, already-
                # extended setup). A breakout that follows a genuine BB/KC
                # volatility squeeze has materially better follow-through
                # than one that doesn't - make it mandatory for a BREAKOUT
                # BUY to confirm, not just a bonus tacked on afterward.
                is_breakout_signal = "BREAKOUT BUY" in raw_action
                squeeze_ok = is_squeezed if is_breakout_signal else True

                confirmed = strong_trend and vol_confirmed and vwap_ok and squeeze_ok
                if needs_confirmation and not confirmed:
                    unmet = []
                    if not (strong_trend and vol_confirmed):
                        unmet.append("low ADX/volume")
                    if not vwap_ok:
                        unmet.append("below VWAP")
                    if not squeeze_ok:
                        unmet.append("no squeeze release")
                    # BUGFIX: an unconfirmed STRONG BUY / BREAKOUT BUY must NOT
                    # be recommended. It used to stay in the matrix with the
                    # label "...(Unconfirmed: ...)" and a quartered score, but the
                    # Top-10 and Session Picks filters match on the action
                    # substring ("STRONG BUY" / "BREAKOUT BUY"), so these
                    # failed signals still leaked into the actionable
                    # recommendations and got picked as Session Picks - the
                    # exact "recommendation that loses money" failure. The
                    # backtester (backtester._point_in_time_signal) already
                    # treats an unconfirmed signal as NO TRADE; the live matrix
                    # must match it. Reclassify to HOLD/NEUTRAL so it can't
                    # appear in Top-10 / Session Picks, while the reason stays
                    # visible in Signal Reason.
                    raw_action = "🟡 HOLD / NEUTRAL"
                    action_cmd = f"{raw_action} (Signal unconfirmed: {', '.join(unmet)})"
                    trend_bonus = SCORE_WEIGHTS["hold_neutral"]
                else:
                    action_cmd = raw_action

                if (
                    cmf >= SCORE_WEIGHTS["cmf_bonus_threshold"]
                    and any(tag in action_cmd for tag in ("STRONG BUY", "BREAKOUT BUY", "ACCUMULATE", "BUY ON DIP"))
                ):
                    trend_bonus += SCORE_WEIGHTS["cmf_bonus"]
                if is_squeezed and any(tag in action_cmd for tag in ("STRONG BUY", "BREAKOUT BUY", "ACCUMULATE", "BUY ON DIP")):
                    action_cmd = f"{action_cmd} [💥 SQUEEZE]"
                    trend_bonus += SCORE_WEIGHTS["squeeze_bonus"]

                # Medium-term (BUY ON DIP) confirmation: previously this
                # label carried NO confirmation gate at all (needs_confirmation
                # was False) and never even checked weekly trend. A "dip" with
                # no weekly uptrend underneath it and no real accumulation
                # (CMF) is not a low-risk pullback in an established trend -
                # it's just a falling stock. Gate it the same way STRONG BUY/
                # BREAKOUT BUY are gated above, instead of waving every dip
                # through unconfirmed.
                dip_confirmed = True
                if raw_action == "⏳ BUY ON DIP":
                    dip_confirmed = weekly_aligned and cmf >= ACTION_THRESHOLDS["medium_term_cmf_min"]
                    if not dip_confirmed:
                        unmet = []
                        if not weekly_aligned:
                            unmet.append("weekly trend not aligned")
                        if cmf < ACTION_THRESHOLDS["medium_term_cmf_min"]:
                            unmet.append("CMF below accumulation floor")
                        # BUGFIX: an unconfirmed "dip" is a falling stock, not a
                        # dip - a name below its weekly trend with no accumulation
                        # (CMF) is exactly what the backtester refuses to trade
                        # (it returns None for an unconfirmed BUY ON DIP). The
                        # live matrix used to keep it as "⏳ BUY ON DIP
                        # (Unconfirmed: ...)", and because Top-10 / Session Picks
                        # match on the "BUY ON DIP" substring, these falling
                        # stocks were picked as medium-term Session Picks and
                        # recommended to buy. Reclassify to HOLD/NEUTRAL so it
                        # can't leak into Top-10 / Session Picks.
                        raw_action = "🟡 HOLD / NEUTRAL"
                        action_cmd = f"{raw_action} (Dip unconfirmed: {', '.join(unmet)})"
                        trend_bonus = SCORE_WEIGHTS["hold_neutral"]

                if weekly_aligned and (
                    "STRONG BUY" in action_cmd or "BREAKOUT BUY" in action_cmd
                    or (raw_action == "⏳ BUY ON DIP" and dip_confirmed)
                ):
                    action_cmd = f"{action_cmd} [👑 WEEKLY ALIGNED]"
                    trend_bonus += SCORE_WEIGHTS["weekly_aligned_bonus"]

                if not is_liquid:
                    action_cmd = f"🚫 ILLIQUID - {action_cmd}"
                    trend_bonus += SCORE_WEIGHTS["illiquid_penalty"]

                # -------------------------------------------------------------
                # Pre-breakout screening (v2): "what might break out NEXT
                # session/week", separate from the reactive BREAKOUT BUY
                # labels above (which confirm a move already in progress). A
                # stock only qualifies here if it's still coiling - not
                # already fired.
                #
                # Isolated in its own try/except deliberately: this used to
                # sit inside the same try block as pattern-matching, pivots,
                # Kelly sizing, etc. further down, so ANY exception anywhere
                # in a ticker's scoring (even something unrelated, e.g. a
                # pivot-point edge case) silently dropped that ticker from
                # EVERY output - the Action Matrix, Top 10, the Watchlist,
                # and therefore Session Picks - with only a log line nobody
                # was watching. A confirmed real-world case: GTWL.CA broke
                # out ~20% while showing up nowhere, despite having valid
                # price data (it was even computed as a Sector Leader
                # elsewhere in this same run, which uses the same indicator
                # frame). Running this block first and independently means a
                # failure anywhere else in this ticker's row can no longer
                # take the watchlist entry down with it.
                # -------------------------------------------------------------
                try:
                    if is_liquid and n_bars >= 20:
                        bw_score = 0.0
                        bw_reasons = []
                        at = ACTION_THRESHOLDS

                        if is_squeezed:
                            bw_score += 25.0
                            bw_reasons.append("Volatility squeeze")

                        adx_series = df_ind.get("adx_14")
                        adx_prior = (
                            adx_series.iloc[-6] if adx_series is not None and len(adx_series) > 6 else adx
                        )
                        adx_rising = pd.notna(adx_prior) and adx > adx_prior
                        adx_pts = _graduated_score(
                            adx, at["breakout_watch_adx_soft_min"], at["breakout_watch_adx_min"],
                            at["breakout_watch_adx_max"], at["breakout_watch_adx_soft_max"], 20.0,
                        )
                        if adx_rising:
                            bw_score += adx_pts
                            if adx_pts > 0:
                                label = "ADX trend just building" if adx_pts >= 20.0 else "ADX trend building (borderline)"
                                bw_reasons.append(label)
                        elif adx_pts > 0:
                            # Rising trend strength matters more than the raw
                            # level - a stalled ADX inside the "sweet spot"
                            # still gets partial credit, just less than one
                            # that's actively climbing.
                            bw_score += adx_pts * 0.4
                            bw_reasons.append("ADX in range (not yet rising)")

                        rsi_pts = _graduated_score(
                            rsi, at["breakout_watch_rsi_soft_min"], at["breakout_watch_rsi_min"],
                            at["breakout_watch_rsi_max"], at["breakout_watch_rsi_soft_max"], 15.0,
                        )
                        if rsi_pts > 0:
                            bw_score += rsi_pts
                            bw_reasons.append("RSI bullish with room to run")

                        vol_recent = df_ind["volume"].iloc[-5:].mean() if n_bars >= 10 else avg_volume_20
                        vol_prior = df_ind["volume"].iloc[-10:-5].mean() if n_bars >= 10 else avg_volume_20
                        vol_build_ratio = (
                            (vol_recent / vol_prior) if pd.notna(vol_recent) and pd.notna(vol_prior) and vol_prior > 0 else 0.0
                        )
                        volume_building = vol_build_ratio >= at["breakout_watch_volume_build_ratio"]
                        vol_pts = _graduated_score(
                            vol_build_ratio, at["breakout_watch_volume_build_soft_ratio"], at["breakout_watch_volume_build_ratio"],
                            10.0, 10.0, 15.0,  # no meaningful "too much" ceiling for volume build
                        )
                        if vol_pts > 0:
                            bw_score += vol_pts
                            bw_reasons.append("Volume trending up" if volume_building else "Volume starting to build")

                        range_pts = _graduated_score(
                            range_pos_pct, at["breakout_watch_range_pos_soft_min"], at["breakout_watch_range_pos_min"],
                            100.0, 100.0, 15.0,
                        )
                        if range_pts > 0:
                            bw_score += range_pts
                            bw_reasons.append("Near recent high (resistance test)")

                        if cmf > 0:
                            bw_score += 10.0
                            bw_reasons.append("Positive money flow")

                        if weekly_aligned:
                            bw_score += 10.0
                            bw_reasons.append("Weekly trend aligned")

                        # NEW: relative strength vs. this ticker's own sector.
                        # A stock coiling WHILE outperforming its peers is a
                        # meaningfully stronger setup than one coiling in
                        # lockstep with (or lagging) a sector that isn't
                        # moving - see sector_avg_5d pre-pass above.
                        sec_name = sector_map.get(norm_ticker, sector_map.get(ticker, "General / Diversified"))
                        ticker_5d = 0.0
                        if n_bars >= 6:
                            c_5d_ago = df_ind["close"].iloc[-6]
                            if c_5d_ago > 0:
                                ticker_5d = ((curr_price - c_5d_ago) / c_5d_ago) * 100.0
                        sector_rs = ticker_5d - sector_avg_5d.get(sec_name, 0.0)
                        rs_pts = _graduated_score(
                            sector_rs, 0.0, at["breakout_watch_sector_rs_span_pct"], 999.0, 999.0,
                            at["breakout_watch_sector_rs_bonus_max"],
                        )
                        if rs_pts > 0:
                            bw_score += rs_pts
                            bw_reasons.append("Outperforming its sector")

                        # NEW: relative strength vs. the REAL EGX sector
                        # sub-index (config.SECTOR_BENCHMARK_MAP), where one
                        # exists and its data has been ingested - distinct
                        # from "Outperforming its sector" above, which only
                        # compares against the average of whatever other
                        # tickers this app happens to classify into the same
                        # sector. This compares against the actual published
                        # index (e.g. EGX Banks, EGX Real Estate), the same
                        # kind of benchmark EGX30 is for the whole market,
                        # just scoped to this ticker's own sector. Skipped
                        # silently (0 pts) if this sector has no mapped
                        # index or that index's data isn't loaded - see
                        # sector_bench_frames built once above the main loop.
                        sector_index_rs = None
                        bench_frame_for_sector = sector_bench_frames.get(sec_name)
                        if bench_frame_for_sector is not None and len(bench_frame_for_sector) >= 6:
                            b_close = bench_frame_for_sector["close"]
                            b_now, b_5d_ago = b_close.iloc[-1], b_close.iloc[-6]
                            if pd.notna(b_now) and pd.notna(b_5d_ago) and b_5d_ago > 0:
                                sector_index_5d = ((b_now - b_5d_ago) / b_5d_ago) * 100.0
                                sector_index_rs = ticker_5d - sector_index_5d
                        if sector_index_rs is not None:
                            idx_rs_pts = _graduated_score(
                                sector_index_rs, 0.0, at["breakout_watch_sector_index_rs_span_pct"], 999.0, 999.0,
                                at["breakout_watch_sector_index_rs_bonus_max"],
                            )
                            if idx_rs_pts > 0:
                                bw_score += idx_rs_pts
                                bw_reasons.append(f"Outperforming {sector_benchmark_label(sec_name)}")

                        # NEW: bullish chart-pattern confirmation, reusing the
                        # same pattern match already computed for the main
                        # matrix instead of re-running pattern detection.
                        if pattern_data and pattern_data.get("match_found") and pattern_data.get("projected_change_pct", 0) > 0:
                            pat_conf = pattern_data.get("confidence", 0.0) or 0.0
                            pat_pts = min(at["breakout_watch_pattern_bonus_max"], pat_conf * at["breakout_watch_pattern_bonus_max"] / 100.0)
                            if pat_pts > 0:
                                bw_score += pat_pts
                                bw_reasons.append("Bullish historical-analog pattern match")

                        # NEW (v2b): recent failed test of the same
                        # resistance level - chasing a level that already
                        # rejected the stock once is a worse setup than a
                        # fresh approach, even with identical RSI/ADX/volume.
                        failed_test = _recent_failed_resistance_test(
                            df_ind, nearest_resistance, curr_price,
                            int(at["breakout_watch_failed_test_lookback"]),
                            at["breakout_watch_failed_test_near_pct"],
                            at["breakout_watch_failed_test_reject_pct"],
                        )
                        if failed_test:
                            bw_score += at["breakout_watch_failed_breakout_penalty"]
                            bw_reasons.append("⚠️ Recently rejected at this level")

                        # NEW (v2b): "quiet before the storm" — volume
                        # drying up during the base. This is the mirror
                        # image of "Volume trending up" above: that factor
                        # is closer to a COINCIDENT tell (fires near the
                        # actual breakout day, once buying has already
                        # picked up); a genuine dry-up shows sellers have
                        # exhausted themselves BEFORE any of that starts,
                        # which is why it's scored as its own independent
                        # factor rather than folded into volume_building.
                        dry_recent_n = int(at["breakout_watch_dryup_lookback_recent"])
                        dry_base_n = int(at["breakout_watch_dryup_lookback_base"])
                        dry_up_ratio = None
                        if n_bars >= dry_base_n:
                            vol_recent_dry = df_ind["volume"].iloc[-dry_recent_n:].mean()
                            vol_base = df_ind["volume"].iloc[-dry_base_n:].mean()
                            if pd.notna(vol_recent_dry) and pd.notna(vol_base) and vol_base > 0:
                                dry_up_ratio = vol_recent_dry / vol_base
                        dryup_pts = 0.0
                        if dry_up_ratio is not None:
                            dryup_pts = _graduated_score(
                                dry_up_ratio, -1.0, -1.0,
                                at["breakout_watch_dryup_volume_ratio_max"],
                                at["breakout_watch_dryup_volume_ratio_soft_max"],
                                at["breakout_watch_dryup_bonus_max"],
                            )
                            # dry_up_ratio has no meaningful lower bound (an
                            # ultra-thin recent window is still "dried up",
                            # not a reason for LESS credit), so the soft/lo
                            # floor is set below any plausible ratio - only
                            # the upper taper (base -> soft_max) matters.
                        if dryup_pts > 0:
                            bw_score += dryup_pts
                            bw_reasons.append("Volume dried up during base (supply exhausted)")

                        # NEW (v2b): volatility contraction rank. Extends the
                        # existing boolean bb_kc_squeeze flag with a
                        # continuous read: how tight is today's ATR% against
                        # its OWN recent history, not just "is it inside the
                        # Keltner Channel right now". Catches names that are
                        # coiling tightly but haven't (yet) tripped the
                        # strict squeeze flag.
                        atr_lookback = int(at["breakout_watch_atr_contraction_lookback"])
                        atr_pts = 0.0
                        atr_percentile = None
                        if n_bars >= 20 and atr and curr_price > 0:
                            atr_pct_now = (atr / curr_price) * 100.0
                            atr_series_raw = df_ind.get("atr_14")
                            close_series = df_ind.get("close")
                            if atr_series_raw is not None and close_series is not None:
                                hist_n = min(atr_lookback, n_bars)
                                atr_pct_hist = (atr_series_raw.iloc[-hist_n:] / close_series.iloc[-hist_n:] * 100.0).dropna()
                                if len(atr_pct_hist) >= 10:
                                    atr_percentile = float((atr_pct_hist <= atr_pct_now).mean() * 100.0)
                                    atr_pts = _graduated_score(
                                        atr_percentile, -1.0, -1.0,
                                        at["breakout_watch_atr_contraction_percentile_max"],
                                        at["breakout_watch_atr_contraction_percentile_soft_max"],
                                        at["breakout_watch_atr_contraction_bonus_max"],
                                    )
                        if atr_pts > 0:
                            bw_score += atr_pts
                            bw_reasons.append("Volatility contraction (tightening range)")

                        # NEW (v2b): up-day vs down-day volume split - a
                        # cruder, more direct read on accumulation than CMF
                        # alone: are the heavier-volume days the UP days or
                        # the DOWN days over the recent base? Buyers quietly
                        # absorbing supply on strength (and sellers thin on
                        # weakness) tends to precede a breakout even before
                        # price itself has moved much.
                        ud_n = int(at["breakout_watch_updown_vol_lookback"])
                        updown_pts = 0.0
                        updown_ratio = None
                        if n_bars >= ud_n + 1:
                            recent_closes = df_ind["close"].iloc[-ud_n:]
                            recent_vols = df_ind["volume"].iloc[-ud_n:]
                            prev_closes = df_ind["close"].iloc[-ud_n - 1:-1].reset_index(drop=True)
                            up_mask = recent_closes.reset_index(drop=True) >= prev_closes
                            up_vol = recent_vols.reset_index(drop=True)[up_mask].sum()
                            down_vol = recent_vols.reset_index(drop=True)[~up_mask].sum()
                            if down_vol > 0:
                                updown_ratio = up_vol / down_vol
                            elif up_vol > 0:
                                updown_ratio = at["breakout_watch_updown_vol_ratio_min"]  # all up-volume, no down-volume to divide by
                            if updown_ratio is not None:
                                updown_pts = _graduated_score(
                                    updown_ratio, at["breakout_watch_updown_vol_ratio_soft_min"],
                                    at["breakout_watch_updown_vol_ratio_min"], 999.0, 999.0,
                                    at["breakout_watch_updown_vol_bonus_max"],
                                )
                        if updown_pts > 0:
                            bw_score += updown_pts
                            bw_reasons.append("Buyers absorbing supply (up-volume > down-volume)")

                        # NEW: small additive nudge from the LIVE broad-market
                        # regime (EGX30 - see market_regime_summary computed
                        # once above the main loop). Not a hard gate - see
                        # config.BREAKOUT_WATCH_BULL_REGIME_BONUS / BEAR_
                        # REGIME_PENALTY's own docstring for why.
                        if regime_score_adj:
                            bw_score += regime_score_adj
                            if regime_score_adj > 0:
                                bw_reasons.append(f"Broad market in confirmed uptrend ({benchmark_label(PRIMARY_BENCHMARK_TICKER)})")
                            else:
                                bw_reasons.append(f"Broad market in confirmed downtrend ({benchmark_label(PRIMARY_BENCHMARK_TICKER)})")
                        bw_score = max(0.0, bw_score)

                        already_fired = (
                            "STRONG BUY" in raw_action or "BREAKOUT BUY" in raw_action or "SELL" in raw_action
                        )
                        if not already_fired:
                            dist_to_resistance = (
                                round(max(0.0, ((nearest_resistance - curr_price) / curr_price) * 100), 2)
                                if curr_price > 0 else None
                            )
                            tier = (
                                "Watching"
                                if bw_score < at["breakout_watch_min_score"]
                                else "High Confidence" if bw_score >= at["breakout_watch_alert_score"]
                                else "Confirmed"
                            )
                            # Confirmed/High-Confidence entries always qualify.
                            # Sub-threshold "Watching" entries are collected
                            # too (min_score gated only at fallback_min_score)
                            # so a genuinely strong-but-borderline setup is
                            # never simply invisible - it's ranked and capped
                            # to the top N instead (see below, after the loop).
                            if bw_score >= at["breakout_watch_fallback_min_score"]:
                                breakout_watchlist.append({
                                    "Ticker": norm_ticker,
                                    "Breakout Score": round(bw_score, 1),
                                    "Tier": tier,
                                    "Current Price": round(curr_price, 4),
                                    "Dist. to Resistance (%)": dist_to_resistance,
                                    "RSI-14": round(rsi, 1),
                                    "ADX-14": round(adx, 1),
                                    "Squeeze Active": bool(is_squeezed),
                                    "Volume Trend": "Rising" if volume_building else "Flat/Falling",
                                    "Dry-Up Ratio (10D/50D Vol)": round(dry_up_ratio, 2) if dry_up_ratio is not None else None,
                                    "ATR% Contraction Percentile": round(atr_percentile, 1) if atr_percentile is not None else None,
                                    "Up/Down Volume Ratio": round(updown_ratio, 2) if updown_ratio is not None else None,
                                    "Sector RS (5D, pts)": round(sector_rs, 2),
                                    "Sector Index RS (5D, pts)": round(sector_index_rs, 2) if sector_index_rs is not None else None,
                                    "Sector Index": benchmark_label(get_sector_benchmark_ticker(sec_name)) if get_sector_benchmark_ticker(sec_name) else None,
                                    "Recently Rejected": bool(failed_test),
                                    "Trend Class": trend_latest,
                                    "Signals": ", ".join(bw_reasons) if bw_reasons else "—",
                                    "Data Confidence": data_conf_tier,
                                    "Market Regime (EGX30)": primary_market_regime,
                                })
                except Exception as e:
                    logger.warning(f"{norm_ticker}: pre-breakout screening failed ({e}) - skipping watchlist for this ticker only")

                # --- Stop / take-profit / reward:risk, computed BEFORE the
                # score so a poor payoff can (a) gate the action itself and
                # (b) factor into Rank Score. This used to run AFTER score
                # was finalized, which meant reward:risk only ever reached
                # Kelly position-sizing - never Rank Score, and never had a
                # chance to veto the action - so a row with a great pattern
                # match but a terrible payoff (risking several times what it
                # could gain) could still rank #1 in Top-10/Session Picks.
                entry_target = (
                    min(curr_price, vwap) if pd.notna(vwap) and vwap > 0 else curr_price
                )
                atr_mult = ACTION_THRESHOLDS["atr_trailing_multiplier"]
                # The displayed stop level is the pure ATR stop (unchanged).
                stop_distance = atr_mult * atr
                suggested_stop = round(max(curr_price - stop_distance, 0.0001), 4)
                risk_budget = cash_balance * RISK_PER_TRADE_PCT
                # Real position sizing: shares = risk budget / (stop distance
                # + round-trip fees). Adding the fee drag to the denominator
                # makes the 1%-risk figure a NET risk (what is actually lost
                # if stopped out after paying both commissions), not a gross
                # estimate. The stop LEVEL itself is untouched.
                if POSITION_SIZE_FEE_ADJUST:
                    sizing_stop_distance = stop_distance + (
                        entry_target * ROUND_TRIP_FEE_PCT
                    )
                else:
                    sizing_stop_distance = stop_distance

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
                raw_shares = (
                    int(risk_budget / sizing_stop_distance)
                    if sizing_stop_distance > 0 else 0
                )
                suggested_shares = min(raw_shares, max_affordable_shares)
                reward_risk = (
                    (max(take_profit_target - curr_price, 0.0) / sizing_stop_distance)
                    if sizing_stop_distance > 0 else 0.0
                )

                # BUGFIX: gate out buy-type actions whose reward:risk falls
                # below ACTION_THRESHOLDS["min_reward_risk"] - same treatment
                # as an unconfirmed signal (reclassify to HOLD/NEUTRAL so it
                # can't reach Top-10/Session Picks, reason stays visible in
                # Signal Reason). Real, non-fabricated data from this app
                # showed a median buy-side RR of ~0.27 before this existed -
                # i.e. the typical "buy" risked ~3.7x what it stood to gain,
                # which is a losing proposition even at a good win rate.
                is_buy_type_action = any(
                    tag in raw_action for tag in ("STRONG BUY", "BREAKOUT BUY", "ACCUMULATE", "BUY ON DIP")
                )
                min_rr = ACTION_THRESHOLDS["min_reward_risk"]
                if is_buy_type_action and reward_risk < min_rr:
                    raw_action = "🟡 HOLD / NEUTRAL"
                    action_cmd = f"{raw_action} (Poor reward:risk {reward_risk:.2f}x < {min_rr:.2f}x floor)"
                    trend_bonus = SCORE_WEIGHTS["hold_neutral"]

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
                # BUGFIX: reward:risk now contributes to the score itself
                # (not just the gate above), so that among rows that DO clear
                # the floor, better asymmetry is rewarded rather than ranking
                # purely on pattern/trend. Capped so one outlier RR (e.g. a
                # near-zero stop distance) can't dominate the rest of the score.
                reward_risk_component = (
                    min(reward_risk, ACTION_THRESHOLDS.get("reward_risk_score_cap", 3.0))
                    * SCORE_WEIGHTS["reward_risk_weight"]
                )

                raw_score = (
                    pattern_component
                    + projected_component
                    + (range_pos_pct * SCORE_WEIGHTS["range_position_weight"])
                    + trend_bonus
                    + reward_risk_component
                )
                score = raw_score * conf_weight
                # Win-rate estimate for Kelly - priority order:
                #   1. A historical-analog pattern match's ACTUAL win rate
                #      (pattern_data["win_rate"]) - not "confidence", which
                #      is a similarity x significance x downside-risk TRUST
                #      weight for the composite score, a different number
                #      from win probability (see analytics.
                #      match_historical_patterns' own docstring).
                #   2. BUGFIX (was a "future step"): the backtester's REAL,
                #      out-of-sample walk-forward win rate for this action
                #      family (backtester.save_win_rate_cache /
                #      refresh_win_rate_cache.py), when the cache has enough
                #      trades behind it (config.
                #      MIN_BACKTEST_TRADES_FOR_LIVE_WIN_RATE) to trust.
                #   3. The honest 50/50 prior (config.DEFAULT_WIN_RATE_PRIOR)
                #      when neither of the above is available - never
                #      fabricate an edge that isn't measured.
                bt_win_rate, bt_trade_count, bt_reliable = _get_backtested_win_rate(raw_action)
                if pattern_data.get("match_found"):
                    win_rate_est = pattern_data.get("win_rate", DEFAULT_WIN_RATE_PRIOR)
                    win_rate_source = "pattern_match"
                elif bt_reliable:
                    win_rate_est = bt_win_rate
                    win_rate_source = "backtest"
                else:
                    win_rate_est = DEFAULT_WIN_RATE_PRIOR
                    win_rate_source = "prior_50_50"
                kelly_pct = round(_kelly_fraction(win_rate_est, reward_risk) * 100.0, 2)
                projected_band = (
                    f"{pattern_data.get('lower_95_pct', 'N/A')}% to {pattern_data.get('upper_95_pct', 'N/A')}%"
                    if pattern_data.get("match_found") else "N/A"
                )
                signal_reason = _build_signal_reason(action_cmd, trend_latest, confirmed, weekly_aligned, is_squeezed, cmf, vol_ratio)

                # Long-term Session Picks quality gate (see config.
                # LONG_TERM_SETUP / MIN_AVG_VOLUME_LONG_TERM): computed for
                # every ticker (cheap - reuses the geometric detector already
                # available via chart_patterns.PatternDetector, no extra data
                # pull) so session_picks.py's "long" bucket can filter on it
                # without needing its own indicator-frame access.
                long_term_setup = _check_long_term_setup(df_ind)
                long_term_liquid = avg_volume_20 >= MIN_AVG_VOLUME_LONG_TERM

                # Enrichment (fundamentals / external rating / period
                # returns) - see db_manager.get_latest_enrichment. Looked up
                # by both the normalized and raw ticker since watchlist CSV
                # tickers may be stored in either form.
                enrichment = enrichment_map.get(norm_ticker) or enrichment_map.get(ticker)
                enrichment_fields = _build_enrichment_fields(action_cmd, enrichment)

                # While this app's OWN ingested history is still too short
                # for chart_patterns' swing/pattern detection to ever
                # confirm (see _check_long_term_setup's min_bars_required
                # gate), the provider's own 1-Year % return - computed from
                # THEIR deeper history, not this app's 35 stored bars - is a
                # real, honest substitute momentum check. This is additive
                # ONLY: it can help a ticker that already passed the
                # liquidity floor but hasn't yet accumulated enough local
                # history to run pattern detection at all; it can never
                # override a setup that pattern detection actively rejected
                # (both legs must fail below `min_bars_required` bars, i.e.
                # long_term_setup["confirmed"] is False specifically because
                # there wasn't enough data to check, not because the checks
                # ran and failed).
                long_term_data_insufficient = (
                    df_ind is None or df_ind.empty
                    or len(df_ind) < PATTERN_DETECTION["min_bars_required"]
                )
                provider_1y_momentum_ok = (
                    enrichment_fields["1Y Return %"] is not None
                    and enrichment_fields["1Y Return %"] > 0
                )
                long_term_confirmed_final = bool(long_term_setup["confirmed"] and long_term_liquid) or (
                    long_term_data_insufficient and long_term_liquid and provider_1y_momentum_ok
                )
                long_term_reasons_final = long_term_setup["reasons"] + (
                    [] if long_term_liquid else [f"Avg volume below {MIN_AVG_VOLUME_LONG_TERM:,} long-term floor"]
                )
                if long_term_data_insufficient and long_term_liquid:
                    long_term_reasons_final.append(
                        f"Substituted provider 1Y return ({enrichment_fields['1Y Return %']}%) "
                        f"for pattern/swing check - insufficient local history"
                        if provider_1y_momentum_ok else
                        "Insufficient local history AND no positive provider 1Y return to substitute"
                    )

                buy_recommendations.append(
                    {
                        "Ticker": norm_ticker,
                        "Sector": sector_map.get(norm_ticker, sector_map.get(ticker, "General / Diversified")),
                        "Long-Term Setup Confirmed": long_term_confirmed_final,
                        "Long-Term Setup Reasons": long_term_reasons_final,
                        **enrichment_fields,
                        "Position": "🔁 OWNED - Scale-In Candidate" if is_owned else "New Candidate",
                        "Action": action_cmd,
                        "Rank Score": round(score, 1),
                        "Reward:Risk": round(reward_risk, 2),
                        "Win Rate Estimate (%)": round(win_rate_est * 100.0, 1),
                        "Win Rate Source": win_rate_source,
                        "Backtested Sample Size": bt_trade_count,
                        "Current Price": round(curr_price, 4),
                        "Target Entry (VWAP)": round(entry_target, 4),
                        "Suggested Stop-Loss": suggested_stop,
                        "Take-Profit Target": take_profit_target,
                        "Resistance (52W High)": round(float(range_high), 4),
                        "Support (52W Low)": round(float(range_low), 4),
                        # Genuine, touch-confirmed levels (see analytics.
                        # compute_support_resistance) - the nearest price the
                        # stock has actually reversed at more than once,
                        # which is often well inside the 52-week range and a
                        # more useful "what happens next" reference than the
                        # single highest/lowest print above. None when no
                        # qualifying (>= SR_MIN_TOUCHES) zone has formed yet
                        # on that side within the lookback window.
                        "Nearest Resistance": sr["resistance"]["level"] if sr and sr.get("resistance") else None,
                        "Resistance Touches": sr["resistance"]["touches"] if sr and sr.get("resistance") else None,
                        "Nearest Support": sr["support"]["level"] if sr and sr.get("support") else None,
                        "Support Touches": sr["support"]["touches"] if sr and sr.get("support") else None,
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
                        "Sector": sector_map.get(ticker, "General / Diversified"),
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
            total_equity, sector_market_value, position_market_value, cash_balance
        )

        # Push each position's risk_multiple (see _compute_portfolio_risk)
        # back onto its own Exits-tab row, so the flag sits directly on the
        # row instead of only in the top-of-window banner.
        _risk_multiple_by_ticker = {
            p["ticker"]: p["risk_multiple"] for p in portfolio_risk["position_allocations"]
        }
        pos_warn_pct = PORTFOLIO_RISK_THRESHOLDS["position_concentration_warn_pct"]
        _pct_by_ticker = {
            p["ticker"]: p["pct_of_equity"] for p in portfolio_risk["position_allocations"]
        }
        for row in exit_strategies:
            tkr = row["Ticker"]
            row["Risk Multiple"] = _risk_multiple_by_ticker.get(tkr)
            row["Position % of Equity"] = _pct_by_ticker.get(tkr)
            row["Oversized Position"] = bool(_pct_by_ticker.get(tkr) and _pct_by_ticker[tkr] >= pos_warn_pct)

        financial_statement = {
            "Cash Balance (EGP)": round(cash_balance, 2),
            "Stock Portfolio Cost Basis (EGP)": round(total_invested, 2),
            "Stock Portfolio Market Value (EGP)": round(total_market_value, 2),
            "Unrealized Stock P&L (EGP)": round(unrealized_pnl, 2),
            "Unrealized Stock P&L (%)": round(unrealized_pct, 2),
            "Realized P&L from Closed Trades (EGP)": round(realized_pnl_total, 2),
            "Total Account Equity / Net Worth (EGP)": round(total_equity, 2),
            "Cash Drag (%)": portfolio_risk["cash_drag_pct"],
        }

        sector_summary = self.qe.compute_sector_analytics(processed_tickers_dict, sector_map)

        # Confirmed/High-Confidence entries (score >= breakout_watch_min_score)
        # are the primary list, capped at breakout_watch_max_results as
        # before. "Watching" entries (below min_score but above the looser
        # fallback_min_score gate applied in the loop) are appended after,
        # capped separately at breakout_watch_fallback_top_n - so a
        # borderline setup is always visible SOMEWHERE, just clearly labeled
        # lower-confidence, instead of a single fixed cutoff making it
        # invisible outright.
        confirmed_bw = sorted(
            [r for r in breakout_watchlist if r["Breakout Score"] >= ACTION_THRESHOLDS["breakout_watch_min_score"]],
            key=lambda x: x["Breakout Score"], reverse=True,
        )[: ACTION_THRESHOLDS["breakout_watch_max_results"]]
        watching_bw = sorted(
            [r for r in breakout_watchlist if r["Breakout Score"] < ACTION_THRESHOLDS["breakout_watch_min_score"]],
            key=lambda x: x["Breakout Score"], reverse=True,
        )[: ACTION_THRESHOLDS["breakout_watch_fallback_top_n"]]
        breakout_watchlist = confirmed_bw + watching_bw

        session_date = self.dbm.get_latest_market_date()

        # Persist today's Breakout Score for every watchlist entry so its
        # actual predictive power can eventually be checked against real
        # subsequent price action - see db_manager.log_breakout_watchlist_
        # snapshot / factor_analysis.evaluate_pre_breakout_history(). This
        # was never being recorded anywhere before, so the score's
        # reliability could never be measured, only assumed. Wrapped in
        # try/except like the alert calls below: a logging failure here
        # must never be able to take down analyze_market()'s own return.
        try:
            self.dbm.log_breakout_watchlist_snapshot(breakout_watchlist, session_date)
        except Exception as e:
            logger.warning(f"log_breakout_watchlist_snapshot failed: {e}")

        # Rotation flags: for each held position, does the matrix currently
        # rate an UNHELD candidate higher within the same signal category
        # (STRONG BUY / BREAKOUT BUY / ACCUMULATE / BUY ON DIP)? Surfaces
        # "you're holding X but the matrix now prefers Y more" instead of
        # requiring a manual side-by-side read of Exits vs Top 10.
        portfolio_risk["rotation_flags"] = self._compute_rotation_flags(
            buy_recommendations, processed_owned_tickers
        )

        # Concentration-breach push alert - reuses session_picks.py's
        # existing ALERT_CHANNELS fan-out (Telegram today; any future
        # channel registered there gets this for free). Deduped to once
        # per config.CONCENTRATION_ALERT_DEDUP_DAYS per distinct sector/
        # ticker so a still-unresolved breach doesn't re-push every run.
        for w, subject in zip(portfolio_risk["warnings"], portfolio_risk["warning_subjects"]):
            emit_alert(
                "concentration_breach", {"message": w},
                dbm=self.dbm, dedup_key=f"concentration:{subject}", session_date=session_date,
            )

        # Proactive push alert for NEW "High Confidence" Pre-Breakout
        # Watchlist entrants - this is the early-warning half of fixing the
        # GTWL-type miss: even a name that's coiling correctly and shows up
        # in the watchlist table is easy to miss if nobody's looking at the
        # app that session. Deduped per ticker over
        # config.PRE_BREAKOUT_ALERT_DEDUP_DAYS so a name sitting near the
        # top of the list for a week doesn't re-push every single run.
        from config import PRE_BREAKOUT_ALERT_DEDUP_DAYS
        for row in confirmed_bw:
            if row.get("Tier") != "High Confidence":
                continue
            emit_alert(
                "pre_breakout_high_confidence", dict(row),
                dbm=self.dbm, dedup_key=f"prebreakout:{row['Ticker']}", session_date=session_date,
                dedup_days=PRE_BREAKOUT_ALERT_DEDUP_DAYS,
            )

        # EGX30 EGP-vs-USD divergence push alert - same ALERT_CHANNELS
        # fan-out as the two alerts above (see usd_divergence.py).
        # Deduped per divergence DIRECTION (not just "any divergence") so
        # a bearish divergence that's still unresolved doesn't re-push
        # every run, but a flip from bearish to bullish (or vice versa)
        # fires again as a genuinely new event. "none" never alerts.
        if usd_divergence_snapshot.get("available") and usd_divergence_snapshot.get("divergence") in ("bearish", "bullish"):
            emit_alert(
                "usd_divergence_detected", dict(usd_divergence_snapshot),
                dbm=self.dbm,
                dedup_key=f"usd_divergence:{usd_divergence_snapshot['divergence']}",
                session_date=session_date,
                dedup_days=PRE_BREAKOUT_ALERT_DEDUP_DAYS,
            )

        # Session Picks: check active picks for achievement + refill each
        # bucket back up to quota. Runs here (not in export_json.py / the
        # GUI separately) so "Execute Matrix" in the desktop app and the
        # unattended nightly export always agree on picks/achievements.
        # breakout_watchlist is now passed through too (see
        # session_picks._candidate_pool) so a still-coiling, not-yet-fired
        # pre-breakout name can fill a bounded number of "short" horizon
        # slots once the already-fired STRONG BUY/BREAKOUT BUY pool runs
        # dry, instead of never being eligible until after it's already run.
        # bench_close_by_date/benchmark_label (see market_regime_summary
        # computed above) let refresh_session_picks attach a live
        # "beating/lagging EGX30" alpha figure to every active pick - see
        # that function's own docstring.
        session_picks = refresh_session_picks(
            self.dbm, buy_recommendations, top_10_by_category, sector_summary,
            session_date, breakout_watchlist=breakout_watchlist,
            bench_close_by_date=primary_bench_close_by_date,
            benchmark_label=benchmark_label(PRIMARY_BENCHMARK_TICKER),
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
            market_regime_summary,
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
