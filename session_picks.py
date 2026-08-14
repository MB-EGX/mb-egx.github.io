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

from config import SESSION_PICKS_QUOTA, SESSION_PICKS_EXPECTED_PCT, SESSION_PICKS_EXPECTED_DAYS

HORIZONS = ("short", "medium", "long")

# Human-readable labels shared by the GUI tab and the social-post captions,
# so the two never describe the same horizon differently.
HORIZON_LABELS = {
    "short": "Next Session",
    "medium": "Medium-Term",
    "long": "Long-Term",
}


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
    return (d + timedelta(days=lo)).isoformat(), (d + timedelta(days=hi)).isoformat()


def _with_expected_window(picks: list, horizon: str) -> list:
    """Attaches display-only expected metadata to each active pick:
    the expected_from/expected_by date window (see _expected_window) AND
    expected_pct — this horizon's target % gain from config, so the
    GUI/web tab and the social captions can all show "target +X%"
    without duplicating the config lookup themselves."""
    expected_pct = SESSION_PICKS_EXPECTED_PCT.get(horizon)
    for p in picks:
        p["expected_from"], p["expected_by"] = _expected_window(p["pick_date"], horizon)
        p["expected_pct"] = expected_pct
    return picks


def _price_map(buys: list) -> dict:
    return {r["Ticker"]: r["Current Price"] for r in buys if r.get("Current Price") is not None}


def _candidate_pool(horizon: str, top10: dict, buys: list, sectors: list, by_ticker: dict) -> list:
    """Ranked candidate list for one horizon. Mirrors the exact category
    logic social_poster.py's pick_daily_highlights() used for its old
    single next/medium/long pick, generalized from 'take the #1 row' to
    'take rows in ranked order until a bucket is full'."""
    if horizon == "short":
        pool = list(top10.get("🔥 STRONG BUY", []))
        for cat, rows in top10.items():
            if "BREAKOUT BUY" in cat:
                pool.extend(rows)
        pool.sort(key=lambda r: r.get("Rank Score", 0), reverse=True)
        return pool

    if horizon == "medium":
        pool = list(top10.get("📈 ACCUMULATE", [])) + list(top10.get("⏳ BUY ON DIP", []))
        pool.sort(key=lambda r: r.get("Rank Score", 0), reverse=True)
        return pool

    # long-term: strongest-inflow sectors' leaders first (breadth-backed,
    # not a single-bar signal), then fall back to the next-highest overall
    # score so the bucket still fills even with no qualifying sector.
    pool = []
    strong_sectors = [s for s in sectors if "STRONG INFLOW" in s.get("Sector Status", "")]
    strong_sectors.sort(key=lambda s: s.get("Bullish Breadth (%)", 0), reverse=True)
    for s in strong_sectors:
        leader = s.get("Sector Leader")
        if leader in by_ticker:
            pool.append(by_ticker[leader])
    fallback = sorted(buys, key=lambda r: r.get("Rank Score", 0), reverse=True)
    pool.extend(fallback)
    return pool


def refresh_session_picks(dbm, buys: list, top10: dict, sectors: list, session_date: str) -> dict:
    """Checks active picks for achievement, refills each bucket back up to
    quota, persists everything via ``dbm``, and returns the resulting
    state for the caller to display/export/post.

    ``session_date`` must be the market DATA's own session date (e.g.
    DatabaseManager.get_latest_market_date()), not wall-clock "today" —
    matches the "freshness judged by the data's own date" rule the rest
    of this pipeline already follows (see post_state.py).

    Returns:
        {
          "short": [...active picks...], "medium": [...], "long": [...],
          "achieved_today": [...picks newly marked achieved this run...],
          "session_date": session_date,
        }
    """
    if not session_date or session_date == "N/A":
        # No ingested data yet — nothing to check or refill against; just
        # hand back whatever's already stored so the GUI still has something
        # to show on a first run.
        state = {h: _with_expected_window(dbm.get_active_picks(h), h) for h in HORIZONS}
        state["achieved_today"] = []
        state["session_date"] = session_date
        return state

    prices = _price_map(buys)
    by_ticker = {r["Ticker"]: r for r in buys}

    # 1. Achievement check — every currently-active pick vs today's price,
    #    against THAT PICK'S OWN horizon target (short/medium/long each
    #    have a different bar — see config.SESSION_PICKS_EXPECTED_PCT).
    achieved_today = []
    for horizon in HORIZONS:
        threshold = SESSION_PICKS_EXPECTED_PCT.get(horizon, 3.0)
        for pick in dbm.get_active_picks(horizon):
            current_price = prices.get(pick["ticker"])
            if current_price is None or not pick["ref_price"]:
                continue
            pct = (current_price / pick["ref_price"] - 1.0) * 100.0
            if pct >= threshold:
                dbm.mark_pick_achieved(pick["id"], session_date, current_price, round(pct, 2))
                achieved_today.append({
                    **pick,
                    "achieved_date": session_date,
                    "achieved_price": current_price,
                    "achieved_pct": round(pct, 2),
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
        for row in _candidate_pool(horizon, top10, buys, sectors, by_ticker):
            if needed <= 0:
                break
            ticker = row.get("Ticker")
            price = row.get("Current Price")
            if not ticker or price is None or ticker in active_tickers or ticker in excluded_today:
                continue
            dbm.add_pick(ticker, horizon, session_date, price)
            active_tickers.add(ticker)
            needed -= 1

    state = {h: _with_expected_window(dbm.get_active_picks(h), h) for h in HORIZONS}
    state["achieved_today"] = achieved_today
    state["session_date"] = session_date
    return state
