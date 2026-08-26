"""
top_movers.py
=============
Best-5 Gainers / Worst-5 Losers of the latest session — the pure-compute
source for the new "Daily Movers" tab (desktop app) and the
"daily_movers" key in market_data.json (web dashboard).

Two callers, one formula:
  * export_json.py  -> compute_daily_movers(chart_history, matrix_rows,
                        as_of=...) from the same chart closes the web
                        chart already renders;
  * app_gui.py      -> its own small DB pull over 3 days of bars, shaped
                        identically (see _compute_daily_movers there).

Change is per-session close-to-close % (last bar vs previous bar). Ties
on the % change are broken by Rank Score (more conviction wins) so a
quiet day still produces a stable, sensible ranking. No account data —
ticker + price levels + score only, same public trust boundary as the
rest of market_data.json.
"""
from __future__ import annotations


def compute_daily_movers(chart_history=None, matrix_rows=None, top_n: int = 5,
                         as_of: str | None = None) -> dict:
    """Rank every ticker in ``chart_history`` by its latest close-to-close
    % change; return the top ``top_n`` gainers and top ``top_n`` losers.

    ``chart_history`` is the {"stocks": {ticker: {"close": [...]}}} shape
    export_json.build_chart_history produces. ``matrix_rows`` (optional)
    provides Rank Score lookups for tie-breaking. Returned rows keep the
    keys: ticker, close, prev_close, change_pct, rank_score.
    """
    stocks = (chart_history or {}).get("stocks", {})
    score_map = {r.get("Ticker"): r.get("Rank Score") for r in (matrix_rows or []) if r.get("Ticker")}

    rows = []
    for ticker, h in stocks.items():
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
        "as_of": as_of,
        "gainers": rows[:top_n],
        "losers": (list(reversed(rows[-top_n:])) if rows else []),
    }
