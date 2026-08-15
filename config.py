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
# If your physical RAM is strictly limited (e.g. 8GB), lower this to 2.
MAX_WORKERS = min(os.cpu_count() or 2, 4)
CHUNK_SIZE = 1000  # Rows per flush when writing to DuckDB.

# --- Risk & signal-quality controls ---
MIN_AVG_VOLUME = 50_000
MIN_BARS_FOR_PATTERN_TRUST = 25
RISK_PER_TRADE_PCT = 0.01

TRANSACTION_FEE_PCT = 0.0035  # 0.35% per side (EGX brokerage)
ROUND_TRIP_FEE_PCT = TRANSACTION_FEE_PCT * 2

# --- Multi-factor confirmation matrix scoring weights ---
SCORE_WEIGHTS = {
    "sell_avoid": -35.0,
    "strong_buy": 50.0,
    "breakout_crossover": 35.0,  # NEW: SMA-50 golden-cross component
    "breakout_momentum": 28.0,   # NEW: price > EMA20 + RSI >= 52 component
    "buy_on_dip": 45.0,
    "accumulate": 15.0,
    "unconfirmed_scale": 0.35,
    "cmf_bonus": 15.0,
    "cmf_bonus_threshold": 0.15,
    "squeeze_bonus": 10.0,
    "weekly_aligned_bonus": 20.0,
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
    # STRONG BUY: near 1Y high, RSI mid-bull, gap not down
    "strong_buy_range_pos_min": 85.0,
    "strong_buy_rsi_min": 55.0,
    "strong_buy_rsi_max": 75.0,
    "strong_buy_gap_min": 0.0,
    # BREAKOUT BUY components (see decision_matrix for how they combine)
    "breakout_momentum_rsi_min": 52.0,
    "breakout_gap_min": -1.0,
    # BUY ON DIP
    "buy_on_dip_range_pos_max": 25.0,
    "buy_on_dip_rsi_max": 38.0,
    # Confirmation gates
    "strong_trend_adx_min": 20.0,
    "volume_ratio_threshold": 1.3,
    "volume_z_score_threshold": 1.5,
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
    "breakout_watch_adx_min": 15.0,   # ADX floor: some trend forming, not flat
    "breakout_watch_adx_max": 25.0,   # ADX ceiling: below the "already trending hard" zone
    "breakout_watch_rsi_min": 50.0,   # RSI floor: bullish bias
    "breakout_watch_rsi_max": 65.0,   # RSI ceiling: room to run before overbought
    "breakout_watch_range_pos_min": 80.0,  # % of 250-day range: near resistance
    "breakout_watch_volume_build_ratio": 1.1,  # last 5D avg vol vs prior 5D avg vol
    "breakout_watch_min_score": 45.0,  # minimum composite score to qualify
    "breakout_watch_max_results": 25,
}

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
    "min_quality": 0.5,
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
}

PAPER_TRADING_DEFAULTS = {
    "starting_cash_egp": 100000.0,
    "max_open_positions": 10,
    "default_fee_pct": TRANSACTION_FEE_PCT,
}

CACHE_CONTROL_HEADER = "public, max-age=60, stale-while-revalidate=600"
CDN_PURGE_URL = os.environ.get("MBEGX_CDN_PURGE_URL", "").strip()
EMAIL_DIGEST_RECIPIENTS = os.environ.get("MBEGX_EMAIL_DIGEST_TO", "").strip()
SESSION_PICKS_TELEGRAM_WEBHOOK = os.environ.get("MBEGX_TELEGRAM_WEBHOOK", "").strip()

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
