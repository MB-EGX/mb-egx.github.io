"""

session_picks.py

=================

"Session Picks" is the app's forward-looking watchlist: at any time it

holds up to SESSION_PICKS_QUOTA active tickers per horizon (5 short-term,

3 medium-term, 3 long-term by default), each stamped with the price it

was picked at. On every matrix run:



  1. Every currently-active pick's stamped price is compared against

     today's price. Once a pick is up >= its horizon's own target %

     gain (config.SESSION_PICKS_EXPECTED_PCT — short/medium/long each

     have a different bar) from where it was picked, it's marked

     'achieved' — its slot frees up.

  2. Each horizon is refilled back up to quota from the same signal pools

     the app already ranks tickers into (STRONG BUY/BREAKOUT for

     short-term, ACCUMULATE/BUY ON DIP for medium-term, strong-inflow

     sector leaders for long-term), skipping any ticker already active

     in ANY bucket so the same name is never double-counted.



This runs from DecisionMatrix.analyze_market() itself (not export_json.py

or app_gui.py separately) so the desktop app's "Execute Matrix" button and

the unattended nightly export drive the exact same picking/achievement

logic — one code path, no drift between what the app shows and what gets

posted to Instagram/Facebook.



Nothing here is account data. A "pick" is just a ticker + a public,

score-derived reason to watch it — same trust boundary as the existing

market_matrix / top_10 payload already published to market_data.json.

"""

from __future__ import annotations



from datetime import date, timedelta



from config import (

    SESSION_PICKS_QUOTA, SESSION_PICKS_EXPECTED_PCT, SESSION_PICKS_EXPECTED_DAYS,

    MAX_ACHIEVED_HISTORY, SESSION_PICKS_PRE_BREAKOUT_MAX_SLOTS, SESSION_PICKS_PRE_BREAKOUT_MIN_SCORE,

    SESSION_PICKS_STOP_LOSS_PCT,

)

from market_regime import pct_change_between

# W-future-date-guard: today_cairo() is the ONLY trustworthy "now" - the
# real-world Cairo wall-clock date, untouched by any corruption in
# market_data. session_date (passed in below) instead comes from
# DatabaseManager.get_latest_market_date() - i.e. MAX(date) in
# market_data - which is exactly what the MM/DD-vs-DD/MM CSV bug
# corrupted into a future date, and that corrupted value is what got
# stamped straight into pick_date/achieved_date the moment this module
# ran against it (see fix_future_dates.py / fix_session_pick_dates.py /
# revert_false_achievements.py for the cleanup this caused). Comparing
# session_date against today_cairo() below is what stops it from ever
# happening silently again, regardless of what causes a future date to
# show up next time.
from freshness import today_cairo
from config import get_logger

logger = get_logger("session_picks")



HORIZONS = ("short", "medium", "long")



ALERT_CHANNELS = []





def register_alert_channel(callback):

    if callable(callback) and callback not in ALERT_CHANNELS:

        ALERT_CHANNELS.append(callback)





# Auto-registers the Telegram channel (I5/N2) if config.SESSION_PICKS_

# TELEGRAM_WEBHOOK is set — see alerts.py's own docstring for why this is

# a plain side-effect import rather than a call from every place that

# might run a matrix (desktop app_gui.py / publish.py / run_backtest.py

# would each otherwise need their own wiring). A no-op if the webhook

# isn't configured. Placed after register_alert_channel() above since

# alerts.register_default_channels() calls back into this module.

import alerts  # noqa: E402,F401





def _emit_alert(event_type: str, payload: dict):

    for cb in list(ALERT_CHANNELS):

        try:

            cb(event_type, payload)

        except Exception:

            continue





def emit_alert(event_type: str, payload: dict, dbm=None, dedup_key: str | None = None,

                session_date: str | None = None, dedup_days: int | None = None):

    """Public entry point for OTHER modules (decision_matrix.py's

    concentration-breach check and pre-breakout-watchlist alert, etc.) to

    reuse the exact same ALERT_CHANNELS fan-out that refresh_session_picks()

    uses for 'pick_achieved', instead of every caller re-implementing its

    own dispatch. If ``dbm``/``dedup_key``/``session_date`` are all

    provided, gates the push through DatabaseManager.should_fire_alert()

    first so a still-unresolved condition doesn't re-push on every single

    analyze_market() run. Without those three, fires unconditionally -

    matches the existing 'pick_achieved' behavior, which is inherently a

    one-time event and needs no dedup.



    ``dedup_days`` lets each caller pick its own re-notify cadence (e.g.

    config.CONCENTRATION_ALERT_DEDUP_DAYS vs. config.

    PRE_BREAKOUT_ALERT_DEDUP_DAYS) instead of every event type being forced

    onto the same window; defaults to CONCENTRATION_ALERT_DEDUP_DAYS to

    keep existing callers' behavior unchanged.

    """

    if dbm is not None and dedup_key and session_date:

        try:

            if dedup_days is None:

                from config import CONCENTRATION_ALERT_DEDUP_DAYS

                dedup_days = CONCENTRATION_ALERT_DEDUP_DAYS

            if not dbm.should_fire_alert(dedup_key, session_date, dedup_days):

                return

        except Exception:

            pass

    _emit_alert(event_type, payload)





# Human-readable labels shared by the GUI tab and the social-post captions,

# so the two never describe the same horizon differently.

HORIZON_LABELS = {

    "short": "Next Session",

    "medium": "Medium-Term",

    "long": "Long-Term",

}



def _parse_iso_date(value: str | None) -> date | None:

    try:

        return date.fromisoformat(str(value)[:10]) if value else None

    except (ValueError, TypeError):

        return None



def _add_egx_trading_days(start_date: date, sessions: int) -> date:

    """Adds EGX trading sessions, skipping Friday/Saturday weekends."""

    if sessions <= 0:

        return start_date

    d = start_date

    added = 0

    while added < sessions:

        d += timedelta(days=1)

        if d.weekday() not in (4, 5):  # Fri/Sat weekend, Sun-Thu trading

            added += 1

    return d



def _retirement_reason(pick: dict, horizon: str, session_date: str, current_pct: float | None) -> str | None:

    reasons = []

    stop_pct = float(SESSION_PICKS_STOP_LOSS_PCT.get(horizon, 0.0) or 0.0)

    if current_pct is not None and stop_pct > 0 and current_pct <= -abs(stop_pct):

        reasons.append(f"stop_loss_{stop_pct:g}pct")

    _, expected_by = _expected_window(pick.get("pick_date"), horizon)

    session_d = _parse_iso_date(session_date)

    expected_by_d = _parse_iso_date(expected_by)

    if session_d and expected_by_d and session_d > expected_by_d:

        reasons.append("expired_expected_window")

    return "+".join(reasons) if reasons else None






def _expected_window(pick_date: str, horizon: str) -> tuple[str | None, str | None]:

    """(expected_from, expected_by) calendar dates for when a pick's

    target % move (config.SESSION_PICKS_EXPECTED_PCT) might realistically play out, per

    config.SESSION_PICKS_EXPECTED_DAYS. Display-only estimate, not a

    guarantee — computed fresh from the current config every time this

    is called rather than stamped into the DB, so a config tweak applies

    to already-active picks immediately instead of only new ones."""

    lo, hi = SESSION_PICKS_EXPECTED_DAYS.get(horizon, (0, 0))

    try:

        d = date.fromisoformat(str(pick_date))

    except (ValueError, TypeError):

        return None, None

    return _add_egx_trading_days(d, lo).isoformat(), _add_egx_trading_days(d, hi).isoformat()





def _with_expected_window(picks: list, horizon: str, session_date: str | None = None,

                           bench_close_by_date: dict | None = None) -> list:

    """Attaches display-only expected metadata to each active pick:

    the expected_from/expected_by date window (see _expected_window) AND

    expected_pct — this horizon's target % gain from config, so the

    GUI/web tab and the social captions can all show "target +X%"

    without duplicating the config lookup themselves.



    NEW: when ``bench_close_by_date`` (a {date_str: close} map for the

    live primary benchmark — see market_regime.build_close_by_date and

    decision_matrix's market-regime block) and ``session_date`` are both

    given, also attaches ``benchmark_pct`` — the benchmark's own % move

    from this pick's pick_date to today — so the caller can show

    "beating/lagging the index" without a separate lookup. Left as

    ``None`` (never a fabricated 0) whenever benchmark data isn't

    available for the relevant dates — same missing-means-missing

    contract as market_regime.pct_change_between itself."""

    expected_pct = SESSION_PICKS_EXPECTED_PCT.get(horizon)

    session_d = _parse_iso_date(session_date)

    for p in picks:

        p["expected_from"], p["expected_by"] = _expected_window(p["pick_date"], horizon)

        p["expected_pct"] = expected_pct

        p["stop_loss_pct"] = SESSION_PICKS_STOP_LOSS_PCT.get(horizon)

        pick_d = _parse_iso_date(p.get("pick_date"))

        expected_by_d = _parse_iso_date(p.get("expected_by"))

        p["days_active"] = (session_d - pick_d).days if session_d and pick_d else None

        p["days_overdue"] = (session_d - expected_by_d).days if session_d and expected_by_d and session_d > expected_by_d else 0

        p["is_overdue"] = bool(p.get("days_overdue"))

        p["benchmark_pct"] = (

            pct_change_between(bench_close_by_date, p["pick_date"], session_date)

            if bench_close_by_date and session_date else None

        )

    return picks





def _price_map(buys: list) -> dict:

    return {r["Ticker"]: r["Current Price"] for r in buys if r.get("Current Price") is not None}





def _candidate_pool(horizon: str, top10: dict, buys: list, sectors: list, by_ticker: dict,

                     breakout_watchlist: list | None = None) -> list:

    """Ranked candidate list for one horizon. Mirrors the exact category

    logic social_poster.py's pick_daily_highlights() used for its old

    single next/medium/long pick, generalized from 'take the #1 row' to

    'take rows in ranked order until a bucket is full'.



    For "short", the already-fired STRONG BUY / BREAKOUT BUY pool is

    always tried first (unchanged). Pre-Breakout Watchlist candidates

    (still-coiling, not-yet-fired names — see decision_matrix.py's

    breakout-watch scoring) are appended AFTER that pool, at lower

    priority, marked with "Pre-Breakout Pick": True. Previously the

    watchlist never fed Session Picks at all, so a name could sit at the

    top of the watchlist for weeks and never become a pick until it had

    ALREADY broken out - by which point the early entry was gone. The

    caller (refresh_session_picks) still caps how many of these

    lower-confidence, speculative picks can actually fill a slot via

    SESSION_PICKS_PRE_BREAKOUT_MAX_SLOTS, so they can only ever

    supplement the fired-signal pool, never crowd it out.

    """

    if horizon == "short":

        pool = list(top10.get("🔥 STRONG BUY", []))

        for cat, rows in top10.items():

            if "BREAKOUT BUY" in cat:

                pool.extend(rows)

        pool.sort(key=lambda r: r.get("Rank Score", 0), reverse=True)

        if breakout_watchlist:

            pb_candidates = sorted(

                (r for r in breakout_watchlist if r.get("Breakout Score", 0) >= SESSION_PICKS_PRE_BREAKOUT_MIN_SCORE),

                key=lambda r: r.get("Breakout Score", 0), reverse=True,

            )

            for r in pb_candidates:

                pool.append({

                    "Ticker": r.get("Ticker"),

                    "Current Price": r.get("Current Price"),

                    "Rank Score": r.get("Breakout Score", 0),

                    "Pre-Breakout Pick": True,

                })

        return pool



    if horizon == "medium":

        # Sector distribution veto: never fill a medium-term (2-6 week

        # pullback-entry) slot with a ticker whose own sector is under

        # HEAVY DISTRIBUTION (see analytics.compute_sector_analytics /

        # config.ACTION_THRESHOLDS["sector_heavy_dist_1d"]) - a rising tide

        # lifts individual stocks significantly faster than a stock fights

        # its own sector's outflow. Rows missing a "Sector" (shouldn't

        # happen post-decision_matrix, but never treat unknown as unsafe by

        # excluding it) are kept.

        distressed_sectors = {

            s.get("Sector") for s in sectors if "HEAVY DISTRIBUTION" in s.get("Sector Status", "")

        }

        pool = [

            r for r in (list(top10.get("📈 ACCUMULATE", [])) + list(top10.get("⏳ BUY ON DIP", [])))

            if r.get("Sector") not in distressed_sectors

        ]

        pool.sort(key=lambda r: r.get("Rank Score", 0), reverse=True)

        return pool



    # long-term: strongest-inflow sectors' leaders first (breadth-backed,

    # not a single-bar signal), THEN the rest of the overall-score fallback

    # - but within each tier, a ticker that cleared decision_matrix.py's

    # long-term setup gate (higher-low swing structure + a bullish

    # geometric pattern at required quality + the higher long-term

    # liquidity floor - see config.LONG_TERM_SETUP / MIN_AVG_VOLUME_

    # LONG_TERM) is placed ahead of one that didn't, rather than either

    # hard-excluding unconfirmed setups (which could starve the bucket

    # entirely on a quiet day) or ignoring the gate outright.

    def _setup_confirmed(row: dict) -> bool:

        return bool(row.get("Long-Term Setup Confirmed"))



    pool = []

    strong_sectors = [s for s in sectors if "STRONG INFLOW" in s.get("Sector Status", "")]

    strong_sectors.sort(key=lambda s: s.get("Bullish Breadth (%)", 0), reverse=True)

    sector_leaders = []

    for s in strong_sectors:

        leader = s.get("Sector Leader")

        if leader in by_ticker:

            sector_leaders.append(by_ticker[leader])

    fallback = sorted(buys, key=lambda r: r.get("Rank Score", 0), reverse=True)



    for candidates in (sector_leaders, fallback):

        confirmed = [r for r in candidates if _setup_confirmed(r)]

        unconfirmed = [r for r in candidates if not _setup_confirmed(r)]

        pool.extend(confirmed)

        pool.extend(unconfirmed)

    return pool





def _achieved_this_week(dbm, as_of_date: str, days: int = 7) -> list:

    """Every Session Pick marked achieved within the trailing ``days``-day

    window ending on ``as_of_date`` (inclusive), newest first. Reuses

    dbm.get_recent_achieved_picks() rather than a new SQL query, so this

    stays correct against however achieved history is actually pruned/

    stored - no assumptions made about the DB schema beyond that method's

    existing contract.

    """

    try:

        as_of = date.fromisoformat(str(as_of_date)[:10])

    except (ValueError, TypeError):

        return []

    cutoff = as_of - timedelta(days=days - 1)

    recent = dbm.get_recent_achieved_picks(limit=MAX_ACHIEVED_HISTORY)

    out = []

    for pick in recent:

        try:

            achieved_on = date.fromisoformat(str(pick["achieved_date"])[:10])

        except (ValueError, TypeError):

            continue

        if cutoff <= achieved_on <= as_of:

            out.append(pick)

    return out



def refresh_session_picks(dbm, buys: list, top10: dict, sectors: list, session_date: str,

                           breakout_watchlist: list | None = None,

                           bench_close_by_date: dict | None = None,

                           benchmark_label: str | None = None) -> dict:

    """Checks active picks for achievement, refills each bucket back up to

    quota, persists everything via ``dbm``, and returns the resulting

    state for the caller to display/export/post.



    ``session_date`` must be the market DATA's own session date (e.g.

    DatabaseManager.get_latest_market_date()), not wall-clock "today" —

    matches the "freshness judged by the data's own date" rule the rest

    of this pipeline already follows (see post_state.py).



    ``breakout_watchlist``, if provided, lets a bounded number of the

    "short" horizon's slots be filled by still-coiling Pre-Breakout

    Watchlist names once the already-fired STRONG BUY/BREAKOUT BUY pool

    is exhausted — see _candidate_pool and

    config.SESSION_PICKS_PRE_BREAKOUT_MAX_SLOTS.



    ``bench_close_by_date``/``benchmark_label`` (see market_regime.

    build_close_by_date and decision_matrix's market-regime block), if

    provided, attach a LIVE "beating/lagging the index" figure to every

    active pick and to every pick achieved this run: ``benchmark_pct``

    (the benchmark's own % move since pick_date) and

    ``alpha_vs_benchmark_pct`` (the pick's own % move minus that). Left

    as ``None`` on a pick when benchmark data isn't available for the

    relevant date — never a fabricated 0, same contract as

    market_regime.pct_change_between. This was previously only ever

    computed after the fact by the offline backtester's benchmark-alpha

    summary; this is the same idea applied live, per active pick.



    Returns:

        {

          "short": [...active picks...], "medium": [...], "long": [...],

          "achieved_today": [...picks newly marked achieved this run...],

          "session_date": session_date,

          "benchmark_label": benchmark_label,

        }

    """

    # Guard against a corrupted "now": refuse to stamp today's
    # picks/achievements against a session_date that's ahead of the
    # real Cairo date - the exact failure mode that fabricated a false
    # TWSA.CA pick and corrupted ABUK.CA/EAST.CA's achieved_date (see
    # this module's docstring and the today_cairo import above). Every
    # OTHER part of analyze_market()'s return (rankings, exits, sector
    # summary, breakout watchlist, ...) doesn't depend on session_date
    # being correct, so only Session Picks is held back - not the whole
    # matrix run - and the already-stored state is handed back
    # unchanged, same shape as the "no data yet" branch below, so
    # callers never have to special-case this.
    future_session_date = bool(session_date) and session_date != "N/A" and str(session_date)[:10] > today_cairo()
    if future_session_date:
        logger.warning(
            f"REFUSING to refresh session_picks: market_data's latest date "
            f"({session_date}) is AFTER today (Africa/Cairo, {today_cairo()}) - "
            f"this is impossible and is the same corruption fix_future_dates.py "
            f"exists to fix. Run fix_future_dates.py against market_data, then "
            f"re-run. Session Picks state is left untouched this run - no new "
            f"picks or achievements were stamped."
        )

    if not session_date or session_date == "N/A" or future_session_date:

        # No ingested data yet (or session_date is corrupted/future - see
        # the guard above) — nothing to check or refill against; just

        # hand back whatever's already stored so the GUI still has something

        # to show on a first run.

        state = {h: _with_expected_window(dbm.get_active_picks(h), h) for h in HORIZONS}

        state["achieved_today"] = []

        state["achieved_this_week"] = _achieved_this_week(dbm, today_cairo())

        state["session_date"] = session_date

        state["benchmark_label"] = benchmark_label

        return state



    prices = _price_map(buys)
    # Active picks may not appear in the current buys list (an owned
    # position, or a name that dropped below matrix inclusion) - their
    # latest price must still be known or they can never be marked
    # achieved even after hitting their target. Fall back to the DB.
    for h in HORIZONS:
        for p in dbm.get_active_picks(h):
            if p["ticker"] not in prices:
                try:
                    c = dbm.get_latest_close(p["ticker"])
                    if c is not None:
                        prices[p["ticker"]] = c
                except Exception:
                    continue

    by_ticker = {r["Ticker"]: r for r in buys}

    # BUGFIX: rank_score is NULL for any active pick made before the
    # rank_score column existed (see db_manager.backfill_pick_rank_score's
    # docstring) - permanently showing "-" for Score in the Session Picks
    # tab for those rows even though newer picks show a real number.
    # Opportunistically backfill: if a still-active pick's ticker is
    # currently scored in this run's buys/breakout pool, write that score
    # in now. Never fabricates a score for a ticker that isn't currently
    # scored (falls through and stays NULL until it is).
    bw_by_ticker = {r["Ticker"]: r for r in (breakout_watchlist or [])}
    for h in HORIZONS:
        for p in dbm.get_active_picks(h):
            if p["rank_score"] is not None:
                continue
            src_row = by_ticker.get(p["ticker"])
            score = src_row.get("Rank Score") if src_row else None
            origin = src_row.get("Action") if src_row else None
            if score is None:
                bw_row = bw_by_ticker.get(p["ticker"])
                if bw_row:
                    score = bw_row.get("Breakout Score")
                    origin = "Pre-Breakout Watchlist"
            if score is not None:
                try:
                    dbm.backfill_pick_rank_score(p["id"], score, origin)
                except Exception:
                    pass

    # 1. Achievement check — every currently-active pick vs today's price,

    #    against THAT PICK'S OWN horizon target (short/medium/long each

    #    have a different bar — see config.SESSION_PICKS_EXPECTED_PCT).

    achieved_today = []

    retired_today = []

    for horizon in HORIZONS:

        threshold = SESSION_PICKS_EXPECTED_PCT.get(horizon, 3.0)

        for pick in dbm.get_active_picks(horizon):

            current_price = prices.get(pick["ticker"])

            if current_price is None or not pick["ref_price"]:

                continue

            pct = (current_price / pick["ref_price"] - 1.0) * 100.0

            if pct >= threshold:

                achieved_pct = round(pct, 2)

                dbm.mark_pick_achieved(pick["id"], session_date, current_price, achieved_pct)

                try:

                    dbm.record_leaderboard_hit(pick["ticker"], achieved_pct, session_date)

                except Exception:

                    pass

                benchmark_pct = (

                    pct_change_between(bench_close_by_date, pick["pick_date"], session_date)

                    if bench_close_by_date else None

                )

                event_payload = {

                    **pick,

                    "achieved_date": session_date,

                    "achieved_price": current_price,

                    "achieved_pct": achieved_pct,

                    "benchmark_pct": benchmark_pct,

                    "alpha_vs_benchmark_pct": (

                        round(achieved_pct - benchmark_pct, 2) if benchmark_pct is not None else None

                    ),

                }

                achieved_today.append(event_payload)

                _emit_alert("pick_achieved", event_payload)

                continue

            retirement_reason = _retirement_reason(pick, horizon, session_date, pct)

            if retirement_reason:

                retired_pct = round(pct, 2)

                dbm.retire_pick(pick["id"], session_date, current_price, retired_pct, retirement_reason)

                retired_today.append({

                    **pick,

                    "retired_date": session_date,

                    "retired_price": current_price,

                    "retired_pct": retired_pct,

                    "retired_reason": retirement_reason,

                })



    # 2. Refill each bucket back up to quota. A ticker is only ever one

    #    live pick at a time, so exclude anything currently active in ANY

    #    bucket (not just the one being refilled) before picking more.

    active_tickers = set()

    for horizon in HORIZONS:

        active_tickers.update(p["ticker"] for p in dbm.get_active_picks(horizon))



    for horizon in HORIZONS:

        quota = SESSION_PICKS_QUOTA[horizon]

        needed = quota - len(dbm.get_active_picks(horizon))

        if needed <= 0:

            continue

        # Tickers manually removed from this horizon TODAY (see

        # db_manager.remove_pick) must not be handed their own freed slot

        # straight back just because they're still the top-ranked

        # candidate in this same run's pool.

        excluded_today = dbm.get_excluded_tickers(horizon, session_date)

        pre_breakout_fills = 0

        for row in _candidate_pool(horizon, top10, buys, sectors, by_ticker, breakout_watchlist):

            if needed <= 0:

                break

            ticker = row.get("Ticker")

            price = row.get("Current Price")

            if not ticker or price is None or ticker in active_tickers or ticker in excluded_today:

                continue

            if row.get("Pre-Breakout Pick"):

                if pre_breakout_fills >= SESSION_PICKS_PRE_BREAKOUT_MAX_SLOTS:

                    continue

                pre_breakout_fills += 1

            rank_score = row.get("Rank Score")
            if rank_score is None:
                rank_score = row.get("Breakout Score")
            rank_origin = row.get("Action") or (
                "Pre-Breakout Watchlist" if row.get("Pre-Breakout Pick") else "Signal pool"
            )
            dbm.add_pick(
                ticker, horizon, session_date, price,
                source="pre_breakout" if row.get("Pre-Breakout Pick") else "signal",
                rank_score=rank_score,
                rank_origin=rank_origin,
            )

            active_tickers.add(ticker)

            needed -= 1



    try:

        dbm.prune_achieved_picks(MAX_ACHIEVED_HISTORY)

    except Exception:

        pass

    state = {

        h: _with_expected_window(dbm.get_active_picks(h), h, session_date, bench_close_by_date)

        for h in HORIZONS

    }

    # Live alpha for the currently-active picks: benchmark_pct was already

    # attached by _with_expected_window above; add the pick's own move

    # (from `prices`, the same current-price map the achievement check

    # used) and derive alpha_vs_benchmark_pct from the two.

    for horizon in HORIZONS:

        for p in state[horizon]:

            current_price = prices.get(p["ticker"])

            ref_price = p.get("ref_price")

            stock_pct = (

                round((current_price / ref_price - 1.0) * 100.0, 2)

                if current_price is not None and ref_price else None

            )

            benchmark_pct = p.get("benchmark_pct")

            p["current_pct"] = stock_pct

            p["alpha_vs_benchmark_pct"] = (

                round(stock_pct - benchmark_pct, 2)

                if stock_pct is not None and benchmark_pct is not None else None

            )

    state["achieved_today"] = achieved_today

    state["retired_today"] = retired_today

    state["achieved_this_week"] = _achieved_this_week(dbm, session_date)

    state["session_date"] = session_date

    state["benchmark_label"] = benchmark_label

    return state





def build_digest_payload(state: dict) -> dict:

    short = state.get("short", [])[:3]

    medium = state.get("medium", [])[:2]

    long_ = state.get("long", [])[:2]

    achieved = state.get("achieved_today", [])[:5]

    lines = [f"Session date: {state.get('session_date')}"]

    if short:

        lines.append("Short-term: " + ", ".join(f"{p['ticker']} (+{p.get('expected_pct', 0)}%)" for p in short))

    if medium:

        lines.append("Medium-term: " + ", ".join(f"{p['ticker']} (+{p.get('expected_pct', 0)}%)" for p in medium))

    if long_:

        lines.append("Long-term: " + ", ".join(f"{p['ticker']} (+{p.get('expected_pct', 0)}%)" for p in long_))

    if achieved:

        lines.append("Achieved today: " + ", ".join(f"{p['ticker']} ({p['achieved_pct']}%)" for p in achieved))

    return {

        "subject": f"MB-EGX Session Picks — {state.get('session_date')}",

        "text": "\n".join(lines),

        "lines": lines,

    }

