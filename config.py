"""
config.py
=========
Runtime configuration for the Institutional Quant Engine.

Hard-coded environment knobs (OpenBLAS thread caps, paths) and tunable
thresholds (scoring weights, action classification boundaries, risk
parameters, chart-export window) live here so a single file change can
re-tune the whole engine without hunting through business logic.

CRITICAL: the OpenBLAS/MKL/OMP/NUMEXPR thread caps below MUST be set
before any numpy/pandas import inside a `ProcessPoolExecutor` worker on
Windows. Otherwise each worker allocates redundant BLAS thread buffers
and the pool eventually fails with "Memory allocation failed /
BrokenProcessPool".
"""
import os
import logging
from pathlib import Path

# =============================================================================
# OPENBLAS / MULTIPROCESSING MEMORY FIX
# Must be set BEFORE NumPy/Pandas are imported in spawned Windows worker
# processes to prevent OpenBLAS from allocating redundant thread buffers
# and causing "Memory allocation failed / BrokenProcessPool" crashes.
# =============================================================================
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["NUMEXPR_NUM_THREADS"] = "1"

BASE_DIR = Path(__file__).parent.resolve()
DB_PATH = BASE_DIR / "quant_master.duckdb"
WATCH_DIR = BASE_DIR / "market_data_feeds"
LOG_PATH = BASE_DIR / "quant_app.log"

# Cap maximum workers to prevent RAM/Paging file exhaustion on Windows.
# Each worker process independently reloads the full scipy/numpy/BLAS
# stack on startup (unavoidable with ProcessPoolExecutor's 'spawn' start
# method on Windows) — that's a real, sizeable chunk of virtual memory
# per worker, separate from the OPENBLAS/MKL/OMP thread caps above (those
# limit threads WITHIN a process, not how many processes pile up loading
# DLLs at the same time). Default lowered from 4 to 2: 4 concurrent
# scipy imports is enough to exhaust a default-sized Windows page file
# even on otherwise-capable machines ("DLL load failed while importing
# _fblas: The paging file is too small for this operation to complete").
# Override with the MBEGX_MAX_WORKERS env var if your machine has more
# headroom (more RAM/larger page file) and you want the extra
# parallelism back, without editing this file again.
_env_max_workers = os.environ.get("MBEGX_MAX_WORKERS", "").strip()
if _env_max_workers.isdigit() and int(_env_max_workers) > 0:
    MAX_WORKERS = int(_env_max_workers)
else:
    MAX_WORKERS = min(os.cpu_count() or 2, 2)
CHUNK_SIZE = 1000  # Rows per flush when writing to DuckDB.

# --- Risk & signal-quality controls ---
MIN_AVG_VOLUME = 50_000
MIN_BARS_FOR_PATTERN_TRUST = 25
RISK_PER_TRADE_PCT = 0.01

# ATR is the app's universal volatility unit - it sizes stops, targets,
# and trailing exits everywhere (decision_matrix.py, backtester.py,
# analytics.get_latest_price_and_atr). compute_indicators() always
# produces a real Wilder atr_14 for any ticker with >=3 bars, so this
# fallback is NOT a general substitute for real ATR - it exists only
# for the genuine edge case of an exactly-zero true range (e.g. a
# thinly-traded stock that printed no price movement at all over its
# ATR window, so every TR bar is 0 and Wilder ATR legitimately comes
# out to 0/NaN). A flat 0/NaN ATR would make the stop distance and
# take-profit target both collapse to the entry price, which is not a
# usable trade plan - so a small non-zero volatility floor is applied
# instead. It is intentionally conservative (roughly typical EGX
# small/mid-cap daily range) rather than tuned to any one name, since
# by definition there is no real per-ticker signal to use in this case.
DEFAULT_ATR_PCT_FALLBACK = 0.02  # 2% of price, zero-true-range edge case only

# --- Per-ticker regime classification (analytics.classify_regime) ---
# Distinct from BENCHMARK_REGIME_* below (bull/bear/neutral on the
# index itself, from SMA slope). This instead labels an individual
# ticker's CURRENT trading character - trend strength (ADX) crossed
# with realized volatility (ATR as % of price) - so downstream code
# (pattern-match confidence discounting, exit logic, session-picks
# categorization) can tell "this is a strong clean trend" apart from
# "this is trending but whipsawing" or "this is just choppy/range-bound".
# ADX>=25 is Wilder's own published convention for "trend established"
# (Wilder, "New Concepts in Technical Trading Systems", 1978) - not
# invented here. The ATR% bands are this app's own volatility cut and
# are deliberately explicit/tunable (rather than hardcoded in
# analytics.py) so they can be re-checked against realized EGX
# small/mid-cap ATR% distributions as more history accumulates.
TICKER_REGIME_THRESHOLDS = {
    "adx_trending_min": 25.0,       # Wilder's own "trend established" convention
    "atr_pct_volatile_min": 2.5,    # ATR% at/above this alongside a trend = "Trending / Volatile"
    "atr_pct_range_volatile_min": 3.5,  # ATR% at/above this with NO trend = "Volatile Range"
}

TRANSACTION_FEE_PCT = 0.0035  # 0.35% per side (EGX brokerage)
ROUND_TRIP_FEE_PCT = TRANSACTION_FEE_PCT * 2

# --- Performance-metric conventions (analytics.compute_perf_metrics) ---
TRADING_DAYS_PER_YEAR = 252          # EGX trades Sun-Thu, ~252 sessions/year
ANNUALIZED_RISK_FREE_RATE = 0.0      # EGP risk-free proxy; 0 = ignore (no clean
                                     # risk-free series is maintained for EGP)

# --- Position sizing & Kelly (decision_matrix) ---
# Shares = risk_budget / (entry - stop + round-trip fees). The fee term in
# the denominator makes the 1% risk figure a NET risk (what you actually
# lose if stopped out after paying both commissions), not a gross estimate.
POSITION_SIZE_FEE_ADJUST = True
# Fraction of the full Kelly bet actually used. Half-Kelly is the standard
# risk-reducing choice - it keeps ~75% of the theoretical growth rate at
# roughly half the variance (see the Kelly-criterion literature).
KELLY_FRACTION = 0.5
KELLY_CAP_FRACTION = 0.25            # never bet more than 25% of equity on one idea
# Neutral win-rate prior used when no realized backtest win rate exists for
# an action yet (see decision_matrix._kelly_fraction) - an honest 50/50,
# not an invented edge.
DEFAULT_WIN_RATE_PRIOR = 0.5

# --- Multi-factor confirmation matrix scoring weights ---
SCORE_WEIGHTS = {
    "sell_avoid": -45.0,
    "strong_buy": 48.0,
    "breakout_crossover": 32.0,  # tighter than before: reduce false-positive urgency
    "breakout_momentum": 24.0,
    "buy_on_dip": 30.0,
    "accumulate": 10.0,
    "hold_neutral": 0.0,
    "unconfirmed_scale": 0.25,
    "cmf_bonus": 12.0,
    "cmf_bonus_threshold": 0.15,
    "squeeze_bonus": 8.0,
    "weekly_aligned_bonus": 15.0,
    "illiquid_penalty": -40.0,
    "range_position_weight": 0.05,
    "pattern_confidence_weight": 0.3,
    "pattern_projected_gain_weight": 1.5,
}

# --- Action classification thresholds (formerly hard-coded in decision_matrix) ---
# All percentages are in raw % (e.g. 85 means 85%, not 0.85).
ACTION_THRESHOLDS = {
    # SELL / AVOID: catastrophic drawdown + deeply oversold
    "sell_avoid_price_ratio": 0.75,      # close < sma50 * this
    "sell_avoid_rsi_max": 28.0,
    # STRONG BUY: only when the stock is genuinely pressing highs with
    # healthy-but-not-overcooked momentum.
    "strong_buy_range_pos_min": 88.0,
    "strong_buy_rsi_min": 58.0,
    "strong_buy_rsi_max": 72.0,
    "strong_buy_gap_min": 0.2,
    # BREAKOUT BUY components (see decision_matrix for how they combine)
    "breakout_momentum_rsi_min": 55.0,
    "breakout_gap_min": 0.0,
    # Secondary SELL / AVOID lane: catches structurally weak names that are
    # no longer just "not bullish" but actively deteriorating.
    "sell_trend_price_ratio": 0.97,
    "sell_trend_rsi_max": 42.0,
    "sell_trend_cmf_max": -0.05,
    # BUY ON DIP: only for genuinely stretched pullbacks, not every mildly
    # weak name drifting sideways under resistance.
    "buy_on_dip_range_pos_max": 20.0,
    "buy_on_dip_rsi_max": 35.0,
    # ACCUMULATE: constructive trend, but not strong enough for breakout/
    # strong-buy urgency.
    "accumulate_range_pos_min": 45.0,
    "accumulate_rsi_min": 45.0,
    "accumulate_cmf_min": 0.0,
    # HOLD / NEUTRAL: default bucket for mixed/no-edge conditions.
    "hold_neutral_range_pos_min": 35.0,
    "hold_neutral_range_pos_max": 70.0,
    "hold_neutral_rsi_min": 42.0,
    "hold_neutral_rsi_max": 58.0,
    # Confirmation gates
    "strong_trend_adx_min": 22.0,
    "volume_ratio_threshold": 1.6,   # tightened from 1.3 - demand real institutional-size RVOL before confirming a short-term signal
    "volume_z_score_threshold": 1.5,
    # VWAP acceptance gate (short-term confirmation): close must sit at least
    # this multiple above the 20-day VWAP for STRONG BUY / BREAKOUT BUY to
    # confirm. A close below its own VWAP still carries intraday selling
    # pressure that frequently stalls a breakout attempt the next session.
    "vwap_acceptance_ratio": 1.01,
    # Medium-term (BUY ON DIP) confirmation floor - same CMF value already
    # used for the additive score bonus (SCORE_WEIGHTS["cmf_bonus_threshold"]),
    # but enforced here as a hard gate for medium-term entries specifically:
    # a dip with net distribution (CMF below this) is not a low-risk pullback
    # in an established trend, it's a stock still being sold.
    "medium_term_cmf_min": 0.15,
    # Trailing stop and take-profit
    "atr_trailing_multiplier": 2.0,
    "cut_loss_pnl_pct": -8.0,
    "vwap_cut_loss_ratio": 0.92,
    "take_profit_pattern_floor_pct": 2.0,        # 2%
    "take_profit_atr_floor_multiplier": 2.5,     # (ATR*2.5) / price floor
    "default_take_profit_pct": 5.0,              # 5% if no pattern + no ATR
    # Sector rotation (compute_sector_analytics)
    "sector_strong_inflow_cmf": 0.12,
    "sector_strong_inflow_1d": 1.0,
    "sector_strong_inflow_breadth": 60.0,
    "sector_breakout_1d": 1.8,
    "sector_accumulate_5d": 2.0,
    "sector_accumulate_breadth": 50.0,
    "sector_heavy_dist_1d": -1.5,
    # Pre-breakout screening (decision_matrix.scan for "🎯 Breakout Watchlist")
    # This is deliberately separate from the reactive BREAKOUT BUY labels
    # above: those confirm a move that's already happening (crossover fired,
    # momentum fired). This instead flags stocks that look like they're
    # COILING - i.e. still setting up, not yet fired - so it answers a
    # different question: "what might break out next session/week", not
    # "what broke out today".
    "breakout_watch_adx_min": 15.0,   # ADX floor: full credit zone starts here
    "breakout_watch_adx_max": 25.0,   # ADX ceiling: full credit zone ends here
    "breakout_watch_adx_soft_min": 8.0,   # below this, zero ADX credit (was a hard floor before)
    "breakout_watch_adx_soft_max": 34.0,  # above this, zero ADX credit (was a hard ceiling before)
    "breakout_watch_rsi_min": 50.0,   # RSI floor: full credit zone starts here
    "breakout_watch_rsi_max": 65.0,   # RSI ceiling: full credit zone ends here
    "breakout_watch_rsi_soft_min": 42.0,  # graduated taper floor
    "breakout_watch_rsi_soft_max": 73.0,  # graduated taper ceiling
    "breakout_watch_range_pos_min": 87.0,  # % of 250-day range: full credit from here (tightened from 80 - knock directly on resistance, not mid-range)
    "breakout_watch_range_pos_soft_min": 62.0,  # graduated taper floor
    "breakout_watch_volume_build_ratio": 1.25,  # last 5D avg vol vs prior 5D avg vol: full credit from here (tightened from 1.1)
    "breakout_watch_volume_build_soft_ratio": 0.9,  # graduated taper floor
    # NEW factors (v2 scoring — see decision_matrix._score_breakout_watch)
    "breakout_watch_sector_rs_bonus_max": 12.0,   # ticker's 5D return vs its own sector's 5D avg
    "breakout_watch_sector_rs_span_pct": 6.0,     # outperformance (pct pts) needed for full bonus
    "breakout_watch_pattern_bonus_max": 12.0,     # bullish chart-pattern confirmation bonus
    "breakout_watch_failed_breakout_penalty": -15.0,  # recent failed test of resistance
    "breakout_watch_failed_test_lookback": 10,        # bars checked for a failed resistance test
    "breakout_watch_failed_test_near_pct": 2.0,       # "tested" = high within this % of range high
    "breakout_watch_failed_test_reject_pct": 5.0,     # "failed" = pulled back at least this % since
    # NEW (v2b): "quiet before the storm" volume/volatility signature. All
    # three are LEADING signals - they describe the base BEING BUILT, as
    # opposed to breakout_watch_volume_build_ratio above, which is closer
    # to a COINCIDENT tell that fires nearer the actual breakout day. A
    # name can score well here well before it ever shows rising volume.
    "breakout_watch_dryup_lookback_recent": 10,   # recent window (bars) for the dry-up ratio
    "breakout_watch_dryup_lookback_base": 50,     # baseline window (bars) the recent one is compared to
    "breakout_watch_dryup_volume_ratio_max": 0.75,  # recent/base avg volume <= this = "dried up"
    "breakout_watch_dryup_volume_ratio_soft_max": 0.95,  # graduated taper ceiling
    "breakout_watch_dryup_bonus_max": 12.0,
    "breakout_watch_atr_contraction_lookback": 60,   # bars of ATR% history used for the percentile rank
    "breakout_watch_atr_contraction_percentile_max": 40.0,  # full credit at/under this percentile
    "breakout_watch_atr_contraction_percentile_soft_max": 65.0,  # graduated taper ceiling
    "breakout_watch_atr_contraction_bonus_max": 10.0,
    "breakout_watch_updown_vol_lookback": 15,       # bars used for the up-day vs down-day volume split
    "breakout_watch_updown_vol_ratio_min": 1.3,     # up-day vol / down-day vol >= this = full credit
    "breakout_watch_updown_vol_ratio_soft_min": 0.9,  # graduated taper floor
    "breakout_watch_updown_vol_bonus_max": 10.0,
    "breakout_watch_min_score": 65.0,  # minimum composite score for the confirmed list (tightened from 60 -> 45 originally - raised again so "Confirmed" means genuinely tight, not just past a low bar)
    "breakout_watch_fallback_min_score": 35.0,  # secondary "Watching" tier (tightened from 30 - keeps the "always visible" fallback list from filling with noise)
    "breakout_watch_fallback_top_n": 10,   # always surface the top N by score even under min_score,
                                            # tagged as lower-confidence, so near-miss setups are
                                            # never simply invisible (see decision_matrix.py notes)
                                            # (tightened from 15 - the fallback tier is a safety net,
                                            # not a second list to trawl through)
    "breakout_watch_alert_score": 78.0,  # score bar for a proactive "new pre-breakout" push alert
                                          # (raised from 70 alongside the new v2b bonus factors below,
                                          # so "High Confidence" stays selective now that there are
                                          # more ways to accumulate points)
    "breakout_watch_max_results": 25,
    # NEW: relative strength vs. the REAL EGX sector sub-index (config.
    # SECTOR_BENCHMARK_MAP), not just the peer-average "breakout_watch_
    # sector_rs_*" factor above. See decision_matrix's "Sector Index RS"
    # block. Same convention as every other breakout_watch_* knob above -
    # read via the `at` dict at the call site.
    "breakout_watch_sector_index_rs_bonus_max": 10.0,
    "breakout_watch_sector_index_rs_span_pct": 5.0,  # outperformance (pct pts) vs. the real sector index needed for full bonus
}

# --- Support/Resistance (swing-cluster method) ---
# Real S/R levels are prices the market has repeatedly reversed at, not
# simply the single highest/lowest print in the lookback window - that
# naive "range extreme" is still computed separately (decision_matrix.py's
# own range_high/range_low) for range_pos_pct and Rank Score, which are a
# different, already-tuned concept (every ACTION_THRESHOLDS[..._range_
# pos_...] knob was calibrated against that definition) and is
# deliberately left alone here. This block only feeds the "Nearest
# Support"/"Nearest Resistance" columns, the failed-resistance-test gate,
# the Pre-Breakout Watchlist's "Dist. to Resistance (%)", and the price
# chart's reference lines - see analytics.QuantitativeEngine.
# compute_support_resistance for the full method.
SR_LOOKBACK_BARS = 250          # same window as the existing range extreme, for a like-for-like comparison
SR_SWING_ORDER = 5              # bars on each side for a swing high/low (same convention as chart_patterns.py)
SR_CLUSTER_TOLERANCE_PCT = 1.5  # swings within this % of each other collapse into one zone
SR_MIN_TOUCHES = 2              # a single untested swing isn't a "level" yet - needs at least one repeat
SR_RECENCY_HALF_LIFE_DAYS = 120 # a touch's weight in the strength score halves every this-many days
SR_MAX_LEVELS = 3               # runner-up levels kept per side (nearest-first), beyond the primary one

# --- Data-confidence weighting ---
CONFIDENCE_FLOOR_WEIGHT = 0.5
CONFIDENCE_FULL_TRUST_BARS = 250

# --- Chart history export (used by export_json.py) ---
CHART_HISTORY_DAYS = 365

# --- Geometric pattern detection (chart_patterns.PatternDetector) ---
# epsilon: relative tolerance used everywhere two price levels are compared
# as "roughly equal" (shoulders, necklines, flat triangle edges, ...).
# order: how many bars on each side a point must beat to count as a swing
# high/low - higher = fewer, more significant swings; lower = noisier ones.
# min_quality: patterns below this geometric goodness-of-fit are dropped
# before they ever reach the chart, so a barely-qualifying match doesn't
# clutter the view next to a clean one.
PATTERN_DETECTION = {
    "epsilon": 0.03,
    "order": 5,
    "min_bars_required": 40,   # don't bother scanning very short histories
    "min_quality": 0.75,   # tightened from 0.5 - also now the gate value long-term Session Picks require (see LONG_TERM_SETUP below)
}

# --- Long-term Session Picks setup gate (decision_matrix's long-horizon
# quality check, feeding session_picks.py's "long" bucket) ---
# A long-term pick must clear a HIGHER liquidity bar than the base
# MIN_AVG_VOLUME above (exit liquidity matters more over a 2-6 month hold,
# during which a larger drawdown is more likely to need an orderly exit),
# AND show real geometric structure: an active PATTERN_DETECTION match
# (Cup & Handle / Ascending Triangle / Double Bottom - the bullish,
# continuation/reversal patterns) at/above PATTERN_DETECTION["min_quality"],
# AND a validated higher-low swing structure (the last two swing troughs
# must show a strictly ascending low, not just "not falling").
MIN_AVG_VOLUME_LONG_TERM = 100_000
LONG_TERM_SETUP = {
    "swing_ascending_low_min_pct": 3.0,   # T2 (more recent trough) must be >= T1 * (1 + this/100)
    # Gate on chart_patterns.PatternDetector's own "direction" field
    # (bullish/bearish/neutral - see that module's _result()) rather than a
    # hardcoded pattern-name whitelist, so any bullish geometric match
    # (Cup & Handle, Ascending Triangle, Double Bottom, Inverse H&S,
    # Bull Flag, a bullish Pennant/Symmetrical Triangle breakout, ...)
    # qualifies uniformly at/above PATTERN_DETECTION["min_quality"].
    "required_pattern_direction": "bullish",
}

# --- Decision matrix data pull (used by decision_matrix.analyze_market) ---
# Nothing in the scoring logic looks back further than ~250 trading days
# (the 52-week range lookback, and the weekly SMA-50/RSI resample). 400
# calendar days gives a comfortable buffer over that for holidays/gaps,
# while avoiding pulling and recomputing indicators over a ticker's ENTIRE
# multi-year history on every single "Execute Matrix" run.
MATRIX_LOOKBACK_DAYS = 400

# --- Session Picks (forward-looking watchlist tab + achievement posts) ---
# Every matrix run keeps up to this many ACTIVE picks per horizon, each
# stamped with the price it was picked at. When an active pick's current
# price is >= that horizon's SESSION_PICKS_EXPECTED_PCT above its stamped
# price, it's marked "achieved" (see session_picks.py) — highlighted in
# the desktop app and announced on Instagram/Facebook.
SESSION_PICKS_QUOTA = {
    "short": 5,   # next-session candidates (STRONG BUY / BREAKOUT BUY pool)
    "medium": 3,  # medium-term candidates (ACCUMULATE / BUY ON DIP pool)
    "long": 3,    # long-term candidates (strong-inflow sector leaders)
}

# Historically, the "short" pool above only ever drew from tickers that had
# ALREADY fired STRONG BUY / BREAKOUT BUY - i.e. the move was already
# underway. The Pre-Breakout Watchlist (decision_matrix's coiling-stock
# screen, config.ACTION_THRESHOLDS["breakout_watch_*"]) never fed Session
# Picks at all, so a stock quietly coiling before a breakout could never
# become a pick until AFTER it had already moved - defeating the "get in
# before the move" point of a forward-looking watchlist. These two knobs
# let a bounded number of the "short" horizon's slots be filled by the
# highest-scoring still-coiling names once the already-fired pool is
# exhausted, instead of leaving genuine setups completely unflagged (see
# session_picks._candidate_pool).
SESSION_PICKS_PRE_BREAKOUT_MAX_SLOTS = 1      # cap: never more than this many of the 5 "short" slots
                                               # (tightened from 2 - a Session Pick is a stronger claim
                                               # than a watchlist entry; at most one speculative slot)
SESSION_PICKS_PRE_BREAKOUT_MIN_SCORE = 75.0   # quality bar. MUST stay >= breakout_watch_min_score (65) -
                                               # it used to be 55, i.e. BELOW the watchlist's own 60-point
                                               # "Confirmed" bar at the time, which meant a merely-"Watching"
                                               # (sub-Confirmed) name could still leak into a Session Pick.
                                               # Set here to breakout_watch_alert_score (78) minus a small
                                               # margin, so in practice only "High Confidence" tier names -
                                               # not just barely-Confirmed ones - ever fill this slot.

# Per-horizon expected % gain target. A LONGER horizon gets a bigger
# target, not just a bigger window — a 3% move over 2-6 months would be a
# weak long-term call, so "achieved" means something different (and
# bigger) at each horizon. This is what's shown in the app/web "Expected
# Gain" column and in the Instagram/Facebook captions, and it's also the
# threshold session_picks.py checks a pick's current price against to
# decide it has been achieved.
#   short  -> 3%  (matches the quick 1-3 session window)
#   medium -> 8%  (matches the 2-6 week window)
#   long   -> 15% (matches the 2-6 month window)
# Tune per horizon if you want a tighter/wider target.
SESSION_PICKS_EXPECTED_PCT = {
    "short": 3.0,
    "medium": 8.0,
    "long": 15.0,
}

# Display-only estimate of how long a pick might realistically take to
# reach its horizon's SESSION_PICKS_EXPECTED_PCT, per horizon - shown in
# the Session Picks tab as an "expected by" date range (pick_date +
# margin). This is NOT a guarantee or a trading signal, purely a
# horizon-appropriate expectation-setting window (see session_picks.py's
# _expected_window):
#   short  -> next 1-3 trading sessions (matches "Next Session" framing)
#   medium -> roughly 2-6 weeks out
#   long   -> roughly 2-6 months out
# Tune these two numbers per horizon if you want a tighter/wider margin.
SESSION_PICKS_EXPECTED_DAYS = {
    "short": (1, 3),
    "medium": (14, 45),
    "long": (60, 180),
}

# Session Picks used to stay active FOREVER unless they eventually hit their
# upside target. That meant a short-term idea could still sit in the tab two
# weeks later even after its own expected window had already passed and/or the
# price had rolled over materially. These per-horizon downside invalidation
# bars fix that: on each matrix run, session_picks.py now retires a pick once
# it is down at least this much from its stamped ref_price, freeing the slot
# for a fresher candidate instead of letting stale losers clog the list.
#
# Values are absolute downside percentages (positive numbers here; the code
# compares current return <= -stop_pct).
SESSION_PICKS_STOP_LOSS_PCT = {
    "short": 3.0,
    "medium": 6.0,
    "long": 10.0,
}

# --- Hosted/web scale configuration ---
SUBSCRIPTION_TIERS = {
    "free": {
        "max_history_days": 365,
        "include_walkforward": False,
        "include_paper_trading": False,
        "include_leaderboard": False,
    },
    "pro": {
        "max_history_days": 365 * 5,
        "include_walkforward": True,
        "include_paper_trading": True,
        "include_leaderboard": True,
    },
}

WALK_FORWARD_BACKTEST_DEFAULTS = {
    "min_train_bars": 250,
    "test_bars": 60,
    "step_bars": 60,
    "min_folds": 4,
    # Force-close any trade still open after this many bars inside its
    # fold's test window, even if neither the stop-loss nor the take-
    # profit target has been hit yet. Without a cap, one illiquid/range-
    # bound ticker could hold a position "open" for the entire test
    # window and understate turnover; this keeps every trade's horizon
    # roughly comparable to how the live app actually manages exits
    # (ATR trailing stop / take-profit, not "hold forever").
    "max_hold_bars": 60,
}

# --- Benchmark / index data (EGX30, EGX70 EWI, EGX sector indices, ...) ---
# These tickers represent EGX INDEX LEVELS - not individual tradeable
# stocks. They must NEVER be scored, ranked, or backtested as if they
# were a stock (see decision_matrix.analyze_market's ticker-universe
# filter and backtester.run_walk_forward_backtest's own filter, both of
# which exclude these before touching any signal/scoring/simulation
# code) - you can't place a buy order on "the index" the way you can on
# COMI or HRHO. Instead they feed market_regime.py's regime/relative-
# strength (alpha) calculations, both the offline backtester/factor
# harness AND (as of this list) the LIVE decision_matrix run.
#
# NOTE: this is deliberately NOT the same list as db_manager.get_sector_
# map's ALIASES entry "EGX3" -> "EGX30ETF" - EGX30ETF is a real,
# tradeable exchange-traded fund with its own sector mapping and price
# history (ingested as "EGX30ETF.CA" - the normal no-dot-prefix stock
# convention, confirmed against your own watchlist CSV's Symbol column);
# it belongs in the normal tradeable universe, so it is intentionally
# left OUT of this list even though it's an index product. If you'd
# rather it be treated as a pure benchmark instead (excluded from
# Buy/Sell scoring, Top 10, Session Picks, etc.), add "EGX30ETF.CA"
# here - nothing else needs to change, every consumer of this list
# (decision_matrix.py, backtester.py, market_regime.py) already derives
# its behavior from this one list.
#
# BUGFIX (round 2): every raw ticker below was missing a leading "." -
# this app's CSV feed (investing.com-style watchlist exports - see your
# own Excel_*_Watchlist_*.csv files) writes EVERY index-level symbol
# with a leading dot (".EGX30", ".EGBANK", ".EGX70EWI", ...) to
# distinguish an index from a tradeable stock ticker - EGX30ETF.CA is
# the one exception, since it's a real tradeable fund and follows the
# normal no-dot + ".CA" stock convention instead. DatabaseManager.
# normalize_symbol() does NOT strip a leading dot, and does NOT append
# ".CA" to a string that already contains a "." anywhere - so a
# dotless "EGX30" here normalized to "EGX30.CA" while the ingested row
# was stored as ".EGX30" (unchanged, dot survives sanitization - see
# ingestion.py's _sanitize_text_field / _FORMULA_INJECTION_PREFIXES,
# which does not treat "." as dangerous). Two different strings -
# never matched, so every regime/relative-strength feature below was
# silently inert even once the CSVs were fed in. Confirmed directly
# against the "Symbol" column of your own daily watchlist exports.
BENCHMARK_TICKERS = [
    ".EGX30",       # EGX 30 - flagship blue-chip index
    ".EGX70EWI",    # EGX 70 EWI - equal-weighted broad index
    ".EGX100EWI",   # EGX 100 EWI - equal-weighted broad index
    ".EGX30CAP",    # EGX 30 Capped
    ".SHARIAH",     # EGX 33 Shariah Compliant Index
    ".EGBANK",      # EGX Banks
    ".EGREAL",      # EGX Real Estate
    ".EGBULM",      # EGX Building Materials
    ".EGBASC",      # EGX Basic Resources
    ".EGFOBT",      # EGX Food & Beverages
    ".EGEDUS",      # EGX Education Services
    ".EGTRDB",      # EGX Trading & Distributors
    ".EGSHTS",      # EGX Shipping & Transportation Services
    ".EGNBFC",      # EGX Non-Bank Financial Services
    ".EGCOCE",      # EGX Consulting Engineers
    ".EGTRVL",      # EGX Travel & Leisure
    ".EGHLTH",      # EGX Health Care / Health Index
    ".EGIGSA",      # EGX Industrial Goods & Services
    # --- Added: 5 more instruments confirmed present in the daily feed
    # under the same dotted convention (grep'd directly against ingested
    # market_data.json Symbol values, not assumed - see SECTOR_ROTATION_*
    # and USD_DIVERGENCE_* blocks below for what EGIMCS/EGTEDU/EGX30USD
    # are actually used for; EGXTBONDS and EGX35LV are registered so
    # they're excluded from the tradeable universe and get a regime
    # badge like any other benchmark, but nothing here computes a
    # T-Bond yield/risk-free-rate or an EGX35LV-based strategy yet):
    ".EGIMCS",      # EGX IMCS - Information/Media/Communication Services (tech-sector index)
    ".EGTEDU",      # EGX Text Double - Textiles, Spinning & Clothing sector index
    ".EGX35LV",     # EGX 35 LV - 35-stock low-volatility index (13 sectors)
    ".EGXTBONDS",   # EGX Treasury Bond Index
    ".EGX30USD",    # EGX 30 USD - dollar-denominated EGX30 (see USD_DIVERGENCE_* below)
]

# Human-readable labels for the tickers above - used anywhere a benchmark
# needs to be shown to a person (market-regime badge, breakout-watchlist
# "Sector Index RS" tooltip, Telegram alert text, glossary) instead of a
# raw ticker code. Falls back to the raw ticker itself if a code is ever
# added to BENCHMARK_TICKERS without a matching label here (see
# market_regime.benchmark_label()).
BENCHMARK_LABELS = {
    ".EGX30": "EGX 30",
    ".EGX70EWI": "EGX 70 EWI",
    ".EGX100EWI": "EGX 100 EWI",
    ".EGX30CAP": "EGX 30 Capped",
    ".SHARIAH": "EGX 33 Shariah",
    ".EGBANK": "EGX Banks",
    ".EGREAL": "EGX Real Estate",
    ".EGBULM": "EGX Building Materials",
    ".EGBASC": "EGX Basic Resources",
    ".EGFOBT": "EGX Food & Beverages",
    ".EGEDUS": "EGX Education Services",
    ".EGTRDB": "EGX Trading & Distributors",
    ".EGSHTS": "EGX Shipping & Transportation",
    ".EGNBFC": "EGX Non-Bank Financial Services",
    ".EGCOCE": "EGX Consulting Engineers",
    ".EGTRVL": "EGX Travel & Leisure",
    ".EGHLTH": "EGX Health Care",
    ".EGIGSA": "EGX Industrial Goods & Services",
    "EGX30ETF.CA": "EGX 30 Index ETF",
    ".EGIMCS": "EGX IMCS",
    ".EGTEDU": "EGX Text Double",
    ".EGX35LV": "EGX 35 LV",
    ".EGXTBONDS": "EGX Treasury Bonds",
    ".EGX30USD": "EGX 30 (USD)",
}

# Which of BENCHMARK_TICKERS is used as the single default/broad-market
# benchmark for market-regime classification and excess-return (alpha)
# calculations when a caller doesn't pick one explicitly (market_regime.py,
# backtester.py, factor_analysis.py, and now the LIVE decision_matrix
# run - see decision_matrix.analyze_market's market-regime block).
# EGX30 is Egypt's flagship blue-chip index and the most widely-quoted
# "is the market up or down today" reference.
PRIMARY_BENCHMARK_TICKER = ".EGX30"

# Maps this app's own sector names (db_manager.get_sector_map()'s output
# / the "Sector" column in sectors.json - e.g. "Banks", "Real Estate")
# to the EGX sector sub-index in BENCHMARK_TICKERS that actually tracks
# that sector, so a stock's relative strength can be measured against
# the REAL index for its sector instead of only ever against the broad
# EGX30 (see decision_matrix's "Sector Index RS" pre-breakout factor and
# market_regime.get_sector_benchmark). A sector with no dedicated EGX
# sub-index (e.g. "Textiles & Durables", "Chemicals" - EGX doesn't
# currently publish one) is simply absent here; callers fall back to
# PRIMARY_BENCHMARK_TICKER, never a hard failure.
SECTOR_BENCHMARK_MAP = {
    "Banks": ".EGBANK",
    "Real Estate": ".EGREAL",
    "Building Materials": ".EGBULM",
    "Basic Resources": ".EGBASC",
    "Food, Beverages & Tobacco": ".EGFOBT",
    "Education Services": ".EGEDUS",
    "Trade & Distributors": ".EGTRDB",
    "Shipping & Transportation Services": ".EGSHTS",
    "Non-Bank Financial Services": ".EGNBFC",
    "Construction & Engineering": ".EGCOCE",
    "Travel & Leisure": ".EGTRVL",
    "Health Care & Pharmaceuticals": ".EGHLTH",
    "Industrial Goods, Services & Automobiles": ".EGIGSA",
    # Added alongside the 5 new BENCHMARK_TICKERS above - these two EGX
    # sub-indices track sectors db_manager.get_sector_map() already
    # produces (see db_manager.py's own name-normalization for "Textiles
    # & Durables" / "IT, Media & Communication Services"), so Sector
    # Index RS on individual stocks in those sectors now compares
    # against the REAL sector index instead of silently falling back to
    # PRIMARY_BENCHMARK_TICKER (EGX30) the way it did before.
    "Textiles & Durables": ".EGTEDU",
    "IT, Media & Communication Services": ".EGIMCS",
}

# --- Market regime classification (market_regime.py) ---
# A benchmark session is "bull" when its close is above its own SMA of
# this length AND that SMA has been rising over the last
# BENCHMARK_REGIME_SLOPE_LOOKBACK bars (trend confirmed, not just a
# single bar poking above the average); "bear" is the symmetric
# opposite; anything else is "neutral". Same spirit as
# analytics.QuantitativeEngine.classify_regime, but applied to the
# INDEX as a whole rather than one stock - a single name's own regime
# says nothing about whether the broad tape favors long entries.
BENCHMARK_REGIME_SMA_PERIOD = 50
BENCHMARK_REGIME_SLOPE_LOOKBACK = 10

# --- Live market-regime feed into the Pre-Breakout Watchlist ---
# Previously, EGX30/70/100 regime data was computed ONLY inside the
# offline backtester/factor-validation tools (run_backtest.py --save,
# run_factor_backtest.py) - the live decision_matrix.analyze_market()
# run (the one that drives the desktop "Execute Matrix" button, the
# nightly publish.py export, and therefore both the desktop app and the
# website) never consulted it at all. These two knobs let the ALREADY-
# COMPUTED live EGX30 regime (see decision_matrix's market-regime block)
# nudge the Pre-Breakout Watchlist's composite bw_score - small and
# additive, not a hard gate, so a genuinely strong individual setup is
# never made invisible just because the broad tape is weak, but a
# borderline one needs to work a little harder in a confirmed downtrend,
# and gets a little extra credit in a confirmed uptrend.
BREAKOUT_WATCH_BULL_REGIME_BONUS = 3.0
BREAKOUT_WATCH_BEAR_REGIME_PENALTY = -6.0

# Sector-index relative strength (decision_matrix's Pre-Breakout Watchlist)
# lives inside ACTION_THRESHOLDS above as "breakout_watch_sector_index_rs_
# bonus_max" / "breakout_watch_sector_index_rs_span_pct" - same convention
# as every other breakout_watch_* knob (read via the `at` dict at the call
# site) rather than a separate top-level pair here.

# --- Sector rotation engine (sector_rotation.py) ---
# Relative-strength rotation between two EGX sector sub-indices already
# in BENCHMARK_TICKERS: EGX IMCS (tech/telecom/fintech - ".EGIMCS") vs
# EGX Text Double (textiles/export manufacturing - ".EGTEDU"). These two
# were picked (over any other sub-index pair) because they track a real,
# distinct macro divergence in this market - domestic digital demand vs
# EGP-devaluation-driven export competitiveness - not an arbitrary pair.
# RS_t = Price(IMCS, t) / Price(Text Double, t): RS rising -> IMCS
# outperforming (rotate toward tech); RS falling -> Text Double
# outperforming (rotate toward export manufacturers). See
# sector_rotation.compute_rotation_series's docstring for the exact
# fast/slow SMA crossover rule this drives.
SECTOR_ROTATION_PAIR = (".EGIMCS", ".EGTEDU")
SECTOR_ROTATION_FAST_SMA = 20
SECTOR_ROTATION_SLOW_SMA = 50
# Minimum bars of history required (both legs) before a rotation signal
# is trusted at all - below this, sector_rotation returns "insufficient
# history" rather than a guessed/noisy signal (same "missing means
# unknown" contract as market_regime.py's BENCHMARK_REGIME_* gates).
SECTOR_ROTATION_MIN_BARS = SECTOR_ROTATION_SLOW_SMA + 5

# --- EGX 30 EGP-vs-USD divergence detector (usd_divergence.py) ---
# Compares structural peaks in PRIMARY_BENCHMARK_TICKER (".EGX30", EGP)
# against ".EGX30USD" (the dollar-denominated version of the same
# index). A rising EGP index whose USD twin is NOT confirming (lower
# high while EGP prints a higher high) means the local-currency gain is
# devaluation-driven, not real equity appreciation - see
# usd_divergence.detect_divergence's docstring for the exact peak-slope
# comparison rule. ``order`` mirrors chart_patterns.py's own use of
# scipy.signal.argrelextrema (already a project dependency) - how many
# bars on each side must be lower for a bar to count as a local peak.
USD_DIVERGENCE_PEAK_ORDER = 10
# Minimum bars of history required (both legs) before divergence
# detection is attempted at all - too short a window means "peaks" are
# just noise, not real structural highs.
USD_DIVERGENCE_MIN_BARS = 60

# --- Factor-backtest reliability gate (factor_analysis.py) ---
# A factor bucket (an action type, an ADX band, a market regime, ...)
# needs at least this many closed backtested trades before its win-
# rate/avg-return numbers are labeled "reliable". Separate from
# WALK_FORWARD_BACKTEST_DEFAULTS["min_folds"] above: that gates the
# AGGREGATE backtest result on having enough independent out-of-sample
# windows; this gates each individual FACTOR SLICE (which typically has
# far fewer trades than the aggregate) on having enough raw sample size
# for its win-rate to mean anything statistically. Since this app
# drives real trading decisions, a slice below this bar is always
# reported alongside its numbers, never hidden - just labeled
# unreliable rather than presented as if it were a validated edge.
FACTOR_BACKTEST_MIN_TRADES_RELIABLE = 30

PAPER_TRADING_DEFAULTS = {
    "starting_cash_egp": 100000.0,
    "max_open_positions": 10,
    "default_fee_pct": TRANSACTION_FEE_PCT,
}

CACHE_CONTROL_HEADER = "public, max-age=60, stale-while-revalidate=600"
CDN_PURGE_URL = os.environ.get("MBEGX_CDN_PURGE_URL", "").strip()
EMAIL_DIGEST_RECIPIENTS = os.environ.get("MBEGX_EMAIL_DIGEST_TO", "").strip()
SESSION_PICKS_TELEGRAM_WEBHOOK = os.environ.get("MBEGX_TELEGRAM_WEBHOOK", "").strip()

# Separate from the webhook above on purpose. SESSION_PICKS_TELEGRAM_WEBHOOK
# (alerts.py) is a one-way PUSH: a plain POST to a fixed
# ".../sendMessage?chat_id=<CHAT_ID>" URL, so it never needs the raw bot
# token in this app at all. TELEGRAM_BOT_TOKEN (telegram_bot.py) is for
# the INTERACTIVE bot instead - replying to whoever messages it (/strongbuy,
# /ticker COMI, ...) means calling getUpdates/sendMessage against arbitrary
# chat_ids, which genuinely needs the raw token, not a single baked-in URL.
# If you already made a bot for the webhook above, this is the same bot -
# the token is the "<TOKEN>" segment of that webhook URL - just also add it
# here under its own secret so the interactive side can use it too.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

# Broadcast channel for the daily social posts (N21 - "everyone who joined
# the Telegram channel gets the same daily market/sectors/tickers/
# achievement/track_record cards Instagram and Facebook already get").
# Distinct from SESSION_PICKS_TELEGRAM_WEBHOOK above on purpose: that one is
# a single fixed personal chat for real-time achievement pings; this is the
# numeric chat_id of a public/private CHANNEL (e.g. -1003976650817) that
# TELEGRAM_BOT_TOKEN's bot has been added to as an admin with post rights.
# One sendPhoto call here reaches every channel member - no per-user loop.
TELEGRAM_CHANNEL_CHAT_ID = os.environ.get("MBEGX_TELEGRAM_CHANNEL_ID", "").strip()

# Optional live-update endpoint. The web surface can subscribe to this
# if you later add an SSE publisher, but leaving it empty keeps the app
# fully static-host compatible.
SSE_LIVE_FEED_URL = os.environ.get("MBEGX_SSE_LIVE_FEED_URL", "").strip()

# Keep recent achieved picks bounded so the public JSON and social track-
# record payload do not grow forever.
MAX_ACHIEVED_HISTORY = 500

# --- Portfolio concentration risk (analyze_market's portfolio_risk output) ---
# Flags when too much of the account sits in one sector or one ticker -
# a common blind spot that per-stock risk metrics (ATR stops, Sortino, etc.)
# don't catch on their own, since each stock can look individually fine
# while the account as a whole is one bad sector-day away from a large loss.
PORTFOLIO_RISK_THRESHOLDS = {
    "sector_concentration_warn_pct": 35.0,   # single sector > this % of total equity
    "position_concentration_warn_pct": 25.0,  # single ticker > this % of total equity
    "min_positions_for_warning": 2,          # don't warn a 1-stock starter portfolio for being "concentrated"
}

# --- Cash-drag (dry powder) note (Exits tab / Financials tab / portfolio_risk) ---
# Below this % of total equity sitting in cash, the app surfaces a small
# "low dry powder" note - it doesn't block anything, it's purely informational
# context for why new BUY/ACCUMULATE signals may not be actionable right now.
CASH_DRAG_LOW_PCT = 5.0

# --- Concentration-breach Telegram alert dedup (alerts.py / session_picks.py) ---
# A sector/position concentration warning re-evaluates on EVERY analyze_market()
# run (nightly publish AND every desktop "Execute Matrix" click). Without a
# dedup window, an already-known, still-unresolved breach would re-fire a
# Telegram push every single time - this caps it to once per calendar day
# per distinct warning subject (a sector name or a ticker), same cadence as
# the nightly pipeline, so re-opening the desktop app repeatedly in one day
# doesn't spam the channel while the breach is still live.
CONCENTRATION_ALERT_DEDUP_DAYS = 1

# Same should_fire_alert() dedup mechanism, applied to new "High Confidence"
# Pre-Breakout Watchlist entrants (see decision_matrix.analyze_market /
# alerts.send_telegram_pre_breakout_high_confidence). A coiling setup can sit
# in the watchlist for many sessions in a row - this stops the same ticker
# re-pushing a Telegram alert every single run while it's still qualifying,
# while still re-notifying every few sessions in case it was missed/dismissed.
PRE_BREAKOUT_ALERT_DEDUP_DAYS = 3


def get_logger(name: str) -> logging.Logger:
    """Return a process-wide logger writing UTF-8 to ``quant_app.log``."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        logger.addHandler(handler)
    return logger
