"""
top_movers.py
=============
"Daily Movers" compute layer for the desktop app + web dashboard:

  * Best-5 Gainers / Worst-5 Losers      (compute_gainers_losers)
  * Most Active (by volume)              (compute_most_active)
  * Most Undervalued / Most Overvalued   (compute_valuation_extremes)
  * 52-Week High / 52-Week Low           (compute_week52_extremes)

``compute_market_movers`` bundles all five into the one dict both callers
publish (export_json.py's "daily_movers" key / movers.json shard, and
app_gui.py's Daily Movers tab). ``compute_daily_movers`` is kept as a
thin backward-compatible alias — same name/signature callers already
use — that now returns the FULL bundle (gainers/losers plus the four
new lists) rather than just gainers/losers, so no call site breaks.

Two callers, one formula:
  * export_json.py  -> compute_market_movers(chart_history, matrix_rows,
                        sector_map, as_of=...) from the same chart closes
                        the web chart already renders, plus the same
                        Action Matrix rows (market cap, P/E, 52-week
                        range, day range, avg volume) already computed
                        by decision_matrix.py for this run;
  * app_gui.py      -> its own small DB pull over a few days of bars for
                        gainers/losers, PLUS the matrix rows it already
                        holds in memory for the other four lists (see
                        MainWindow._compute_daily_movers).

Gainers/Losers change is per-session close-to-close % (last bar vs
previous bar). Ties on % change are broken by Rank Score (more
conviction wins) so a quiet day still produces a stable ranking.

Most Active ranks by the latest session's raw volume (shares traded) -
the plain, standard definition ("today's busiest names"), not dollar
turnover, so it answers "what did the most people trade today", not
"where did the most money move" (that would double-count the biggest
names by price alone).

Most Undervalued/Overvalued ranks by P/E RELATIVE TO THE TICKER'S OWN
SECTOR AVERAGE (never in isolation - a bank and a real-estate developer
have structurally different "normal" P/E bands). A ticker with no P/E,
or whose sector has no other priced peer this run, is simply excluded
from these two lists (never guessed at) - see config.HEALTH_SCORE_
WEIGHTS's docstring for the same "P/E vs sector" logic already used for
the "Relative Value" health score in decision_matrix.py.

52-Week High/Low lists tickers trading AT (or within config.
WEEK52_NEAR_PCT of) their own 52-week high/low, ranked by how close they
are to that extreme - not just the single closest name, since several
names can genuinely be making a new high/low on the same session.

No account data anywhere in this module — ticker + price/volume/
valuation levels + score only, same public trust boundary as the rest
of market_data.json.
"""
from __future__ import annotations

from config import (
    MOST_ACTIVE_TOP_N,
    VALUATION_EXTREMES_TOP_N,
    WEEK52_EXTREMES_TOP_N,
    WEEK52_NEAR_PCT,
    STALE_EXCLUSION_DAYS,
    ARCHIVED_TICKERS,
)


def _excluded_tickers(matrix_rows) -> set:
    """Tickers that must never appear in "today's movers": archived/delisted
    names (config.ARCHIVED_TICKERS) plus any ticker whose last bar is stale
    (Days Stale > 0) - a stale ticker's close-to-close % change describes an
    earlier session, not today, so showing it as a "gainer" is misleading
    (e.g. ARVA.CA +56% from 3-week-old data)."""
    excluded = set(ARCHIVED_TICKERS or set())
    for r in (matrix_rows or []):
        if (r.get("Days Stale") or 0) > 0:
            excluded.add(r.get("Ticker"))
    return excluded


def compute_gainers_losers(chart_history=None, matrix_rows=None, top_n: int = 5) -> dict:
    """Rank every ticker in ``chart_history`` by its latest close-to-close
    % change; return the top ``top_n`` gainers and top ``top_n`` losers.

    ``chart_history`` is the {"stocks": {ticker: {"close": [...]}}} shape
    export_json.build_chart_history produces. ``matrix_rows`` (optional)
    provides Rank Score lookups for tie-breaking. Returned rows keep the
    keys: ticker, close, prev_close, change_pct, rank_score.
    """
    stocks = (chart_history or {}).get("stocks", {})
    score_map = {r.get("Ticker"): r.get("Rank Score") for r in (matrix_rows or []) if r.get("Ticker")}

    excluded = _excluded_tickers(matrix_rows)

    rows = []
    for ticker, h in stocks.items():
        if ticker in excluded:
            continue
        try:
            closes = h.get("close") or []
            if len(closes) < 2:
                continue
            prev, last = closes[-2], closes[-1]
            if prev is None or last is None or not float(prev):
                continue
            prev_f, last_f = float(prev), float(last)
            rows.append({
                "ticker": ticker,
                "close": round(last_f, 4),
                "prev_close": round(prev_f, 4),
                "change_pct": round((last_f / prev_f - 1.0) * 100.0, 2),
                "rank_score": score_map.get(ticker),
            })
        except (TypeError, ValueError, IndexError):
            continue

    rows.sort(
        key=lambda r: (r["change_pct"], r["rank_score"] if r["rank_score"] is not None else -1e9),
        reverse=True,
    )

    return {
        "gainers": rows[:top_n],
        "losers": (list(reversed(rows[-top_n:])) if rows else []),
    }


def compute_most_active(chart_history=None, matrix_rows=None, top_n: int = MOST_ACTIVE_TOP_N) -> list:
    """Top ``top_n`` tickers by the latest session's raw traded volume
    (shares). Reads the latest bar straight out of chart_history (which
    already carries a "volume" series per ticker - see export_json.
    build_chart_history) - the same source of truth the web chart itself
    renders, no extra DB round trip.

    Returned rows: ticker, volume, close, change_pct, rank_score.
    """
    stocks = (chart_history or {}).get("stocks", {})
    score_map = {r.get("Ticker"): r.get("Rank Score") for r in (matrix_rows or []) if r.get("Ticker")}

    excluded = _excluded_tickers(matrix_rows)

    rows = []
    for ticker, h in stocks.items():
        if ticker in excluded:
            continue
        try:
            volumes = h.get("volume") or []
            closes = h.get("close") or []
            if not volumes or volumes[-1] is None:
                continue
            vol = float(volumes[-1])
            if vol <= 0:
                continue
            last = closes[-1] if closes else None
            prev = closes[-2] if len(closes) >= 2 else None
            change_pct = (
                round((float(last) / float(prev) - 1.0) * 100.0, 2)
                if last is not None and prev not in (None, 0)
                else None
            )
            rows.append({
                "ticker": ticker,
                "volume": int(vol),
                "close": round(float(last), 4) if last is not None else None,
                "change_pct": change_pct,
                "rank_score": score_map.get(ticker),
            })
        except (TypeError, ValueError, IndexError):
            continue

    rows.sort(key=lambda r: r["volume"], reverse=True)
    return rows[:top_n]


def compute_valuation_extremes(matrix_rows=None, sector_map=None, top_n: int = VALUATION_EXTREMES_TOP_N) -> dict:
    """Most Undervalued / Most Overvalued, ranked by P/E Ratio relative
    to the ticker's own sector average (see module docstring). Requires
    ``matrix_rows`` to already carry "P/E Ratio" and "Sector" (both are
    standard Action Matrix fields - see decision_matrix.py's
    _build_enrichment_fields and per-row "Sector" key).

    Returned rows: ticker, pe_ratio, sector_avg_pe, pe_vs_sector_pct
    (negative = cheaper than sector average), close, rank_score.
    """
    rows_in = [r for r in (matrix_rows or []) if r.get("Ticker") not in _excluded_tickers(matrix_rows)]
    sector_map = sector_map or {}

    sector_pe_sum: dict = {}
    sector_pe_count: dict = {}
    for r in rows_in:
        pe = r.get("P/E Ratio")
        sec = r.get("Sector") or sector_map.get(r.get("Ticker"))
        if pe is None or pe <= 0 or not sec:
            continue
        sector_pe_sum[sec] = sector_pe_sum.get(sec, 0.0) + pe
        sector_pe_count[sec] = sector_pe_count.get(sec, 0) + 1
    sector_avg_pe = {
        sec: sector_pe_sum[sec] / sector_pe_count[sec]
        for sec in sector_pe_sum if sector_pe_count.get(sec, 0) > 0
    }

    scored = []
    for r in rows_in:
        pe = r.get("P/E Ratio")
        sec = r.get("Sector") or sector_map.get(r.get("Ticker"))
        avg_pe = sector_avg_pe.get(sec)
        if pe is None or pe <= 0 or not avg_pe:
            continue
        pe_vs_sector_pct = round(((pe / avg_pe) - 1.0) * 100.0, 2)
        scored.append({
            "ticker": r.get("Ticker"),
            "pe_ratio": round(float(pe), 2),
            "sector": sec,
            "sector_avg_pe": round(float(avg_pe), 2),
            "pe_vs_sector_pct": pe_vs_sector_pct,
            "close": r.get("Current Price"),
            "rank_score": r.get("Rank Score"),
        })

    scored.sort(key=lambda r: r["pe_vs_sector_pct"])
    undervalued = scored[:top_n]                              # most negative first (cheapest vs sector)
    overvalued = list(reversed(scored[-top_n:])) if scored else []  # most positive first (priciest vs sector)
    return {"most_undervalued": undervalued, "most_overvalued": overvalued}


def compute_week52_extremes(matrix_rows=None, top_n: int = WEEK52_EXTREMES_TOP_N,
                             near_pct: float = WEEK52_NEAR_PCT) -> dict:
    """Tickers trading AT (within ``near_pct``%) of their own 52-week
    high/low, ranked closest-first. Requires ``matrix_rows`` to carry
    "Current Price", "Resistance (52W High)", "Support (52W Low)" (all
    standard Action Matrix fields).

    Returned rows: ticker, close, week52_high or week52_low,
    dist_pct (0 = exactly at the extreme), rank_score.
    """
    rows_in = [r for r in (matrix_rows or []) if r.get("Ticker") not in _excluded_tickers(matrix_rows)]
    highs, lows = [], []
    for r in rows_in:
        price = r.get("Current Price")
        hi = r.get("Resistance (52W High)")
        lo = r.get("Support (52W Low)")
        if price is None or not price:
            continue
        if hi:
            dist_pct = round(((hi - price) / hi) * 100.0, 2)
            if dist_pct <= near_pct:
                highs.append({
                    "ticker": r.get("Ticker"), "close": price, "week52_high": hi,
                    "dist_pct": max(dist_pct, 0.0), "rank_score": r.get("Rank Score"),
                })
        if lo:
            dist_pct = round(((price - lo) / lo) * 100.0, 2)
            if dist_pct <= near_pct:
                lows.append({
                    "ticker": r.get("Ticker"), "close": price, "week52_low": lo,
                    "dist_pct": max(dist_pct, 0.0), "rank_score": r.get("Rank Score"),
                })
    highs.sort(key=lambda r: r["dist_pct"])
    lows.sort(key=lambda r: r["dist_pct"])
    return {"week52_high": highs[:top_n], "week52_low": lows[:top_n]}


def compute_market_movers(chart_history=None, matrix_rows=None, sector_map=None,
                          top_n: int = 5, as_of: str | None = None) -> dict:
    """The full Daily Movers bundle: gainers, losers, most_active,
    most_undervalued, most_overvalued, week52_high, week52_low."""
    bundle = {"as_of": as_of}
    bundle.update(compute_gainers_losers(chart_history, matrix_rows, top_n=top_n))
    bundle["most_active"] = compute_most_active(chart_history, matrix_rows, top_n=MOST_ACTIVE_TOP_N)
    bundle.update(compute_valuation_extremes(matrix_rows, sector_map, top_n=VALUATION_EXTREMES_TOP_N))
    bundle.update(compute_week52_extremes(matrix_rows, top_n=WEEK52_EXTREMES_TOP_N))
    return bundle


def compute_daily_movers(chart_history=None, matrix_rows=None, top_n: int = 5,
                         as_of: str | None = None, sector_map=None) -> dict:
    """Backward-compatible name/signature - now returns the FULL bundle
    (see compute_market_movers) instead of just gainers/losers. Existing
    callers that only read ["gainers"]/["losers"] are unaffected; callers
    that want the new lists can read ["most_active"] / ["most_undervalued"]
    / ["most_overvalued"] / ["week52_high"] / ["week52_low"] off the same
    dict without any other code changes.
    """
    return compute_market_movers(chart_history, matrix_rows, sector_map, top_n=top_n, as_of=as_of)
