"""
trade_performance.py
=====================
Shared analytics engine for the "Performance" feature: turns a list of
closed trades (db_manager.get_all_closed_trades()'s own shape - app-tracked
AND manual, indistinguishable here except for the "Source" field) into
every number the Performance view needs: an equity curve, drawdown, win
rate / profit factor / expectancy, win/loss streaks, breakdowns by which
Action Matrix signal triggered the trade / which sector / which tag, and -
the "remarkable" part - per-trade alpha vs. the EGX30 (or a trade's own
sector index), reusing market_regime.py's benchmark plumbing so "did I
actually beat the market" is answered from the same regime/benchmark data
every other feature in this app already trusts.

WHY THIS IS ITS OWN MODULE, NOT INLINED INTO app_gui.py:
    Every number here is a pure function of (trades, optional benchmark
    close series) - no PyQt6, no DuckDB connection, no I/O. That keeps it
    trivially unit-testable (see the bottom of this file) and reusable
    from anywhere that can hand it a list of trade dicts: the desktop
    Performance tab (db_manager.get_all_closed_trades()), a future CLI
    report, or a batch export for the user's own records. The web app's
    equivalent client-side trade log intentionally mirrors the SAME field
    names (Ticker, Buy Price, Sell Price, Realized P&L (EGP), Realized
    P&L (%), Purchase Date, Sell Date, Entry Action, Sector, Tags, Notes,
    Source) precisely so a JSON export from here is a drop-in journal
    import there, and so the two surfaces' analytics never quietly
    diverge in what "win rate" or "alpha" means.

SCOPE: this reads/derives from portfolio_closed rows only - CLOSED,
REALIZED trades. Open positions (portfolio_owned) have no realized P&L
yet and are deliberately out of scope here (unrealized performance
already has its own home in the Action Matrix / Exit Strategy tabs).

TRUST BOUNDARY: everything this module returns is real personal account
performance data (same category as closed_trades / cash_balance - see
export_json.py's own PRIVACY FIX comments). Never publish this module's
output to web_public/data/ - it is for the authenticated owner only
(desktop app locally, or a future per-user-authenticated web view).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime


# ---------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------

def _as_date(v) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def normalize_trades(raw_trades: list[dict]) -> list[dict]:
    """Sorts by Sell Date (oldest first - the order an equity curve needs)
    and adds a few derived fields every downstream function reuses rather
    than recomputing: ``_sell_date``/``_purchase_date`` (parsed date
    objects), ``_pnl`` (float EGP), ``_pnl_pct`` (float %), ``_holding_days``.
    Rows with an unparsable Sell Date are dropped (silently - a trade
    logged with a broken date can't be placed on a chronological equity
    curve, and there is no safe date to guess)."""
    out = []
    for t in raw_trades or []:
        sell_d = _as_date(t.get("Sell Date"))
        if sell_d is None:
            continue
        buy_d = _as_date(t.get("Purchase Date"))
        row = dict(t)
        row["_sell_date"] = sell_d
        row["_purchase_date"] = buy_d
        row["_pnl"] = float(t.get("Realized P&L (EGP)") or 0.0)
        row["_pnl_pct"] = float(t.get("Realized P&L (%)") or 0.0)
        row["_holding_days"] = (sell_d - buy_d).days if buy_d else None
        out.append(row)
    out.sort(key=lambda r: r["_sell_date"])
    return out


# ---------------------------------------------------------------------
# Summary stats
# ---------------------------------------------------------------------

def compute_summary_stats(trades: list[dict]) -> dict:
    """Headline numbers for the top of the Performance view. A trade with
    ``_pnl`` exactly 0 counts as neither a win nor a loss (a true
    breakeven, net of fees) - it's excluded from win_rate's denominator
    the same way a "push" is excluded from a poker win-rate, but IS
    included in total_trades/total_realized_pnl so nothing about it is
    silently dropped."""
    n = len(trades)
    if n == 0:
        return {
            "total_trades": 0, "win_rate_pct": None, "profit_factor": None,
            "expectancy_egp": None, "avg_win_egp": None, "avg_loss_egp": None,
            "avg_win_pct": None, "avg_loss_pct": None, "best_trade": None,
            "worst_trade": None, "total_realized_pnl_egp": 0.0,
            "avg_holding_days": None, "manual_trade_count": 0,
        }

    wins = [t for t in trades if t["_pnl"] > 0]
    losses = [t for t in trades if t["_pnl"] < 0]
    decisive = len(wins) + len(losses)

    gross_win = sum(t["_pnl"] for t in wins)
    gross_loss = abs(sum(t["_pnl"] for t in losses))
    total_pnl = sum(t["_pnl"] for t in trades)

    holding_days = [t["_holding_days"] for t in trades if t["_holding_days"] is not None]

    best = max(trades, key=lambda t: t["_pnl"])
    worst = min(trades, key=lambda t: t["_pnl"])

    return {
        "total_trades": n,
        "win_rate_pct": round(len(wins) / decisive * 100.0, 1) if decisive else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else (None if gross_win == 0 else float("inf")),
        "expectancy_egp": round(total_pnl / n, 2),
        "avg_win_egp": round(gross_win / len(wins), 2) if wins else None,
        "avg_loss_egp": round(-abs(gross_loss) / len(losses), 2) if losses else None,
        "avg_win_pct": round(sum(t["_pnl_pct"] for t in wins) / len(wins), 2) if wins else None,
        "avg_loss_pct": round(sum(t["_pnl_pct"] for t in losses) / len(losses), 2) if losses else None,
        "best_trade": {"Ticker": best.get("Ticker"), "pnl_egp": round(best["_pnl"], 2), "pnl_pct": round(best["_pnl_pct"], 2), "sell_date": str(best["_sell_date"])},
        "worst_trade": {"Ticker": worst.get("Ticker"), "pnl_egp": round(worst["_pnl"], 2), "pnl_pct": round(worst["_pnl_pct"], 2), "sell_date": str(worst["_sell_date"])},
        "total_realized_pnl_egp": round(total_pnl, 2),
        "avg_holding_days": round(sum(holding_days) / len(holding_days), 1) if holding_days else None,
        "manual_trade_count": sum(1 for t in trades if (t.get("Source") or "app") == "manual"),
    }


# ---------------------------------------------------------------------
# Streaks
# ---------------------------------------------------------------------

def compute_streaks(trades: list[dict]) -> dict:
    """Win/loss streaks in chronological (sell-date) order. Breakeven
    trades (_pnl == 0) break a streak without starting a new one of
    either color - same "neither win nor loss" treatment as
    compute_summary_stats' win_rate."""
    if not trades:
        return {"current_streak_type": None, "current_streak_len": 0, "max_win_streak": 0, "max_loss_streak": 0}

    max_win = max_loss = cur_len = 0
    cur_type = None
    for t in trades:
        outcome = "win" if t["_pnl"] > 0 else "loss" if t["_pnl"] < 0 else None
        if outcome is None:
            cur_type, cur_len = None, 0
            continue
        if outcome == cur_type:
            cur_len += 1
        else:
            cur_type, cur_len = outcome, 1
        if cur_type == "win":
            max_win = max(max_win, cur_len)
        else:
            max_loss = max(max_loss, cur_len)

    return {"current_streak_type": cur_type, "current_streak_len": cur_len, "max_win_streak": max_win, "max_loss_streak": max_loss}


# ---------------------------------------------------------------------
# Equity curve + drawdown
# ---------------------------------------------------------------------

def compute_equity_curve(trades: list[dict], starting_equity: float = 0.0) -> list[dict]:
    """One point per closed trade (chronological), not one per calendar
    day - a quiet week with no trades shouldn't add flat filler points,
    and a day with 3 closes should show all 3 steps, not just the last.
    ``equity`` is ``starting_equity`` plus the running sum of realized
    P&L - pass your actual cash_balance-at-first-trade if you want a real
    equity level; 0.0 gives a pure cumulative-P&L curve, which is exactly
    as useful for judging trading skill and needs no extra input."""
    curve = []
    running = float(starting_equity)
    for t in trades:
        running += t["_pnl"]
        curve.append({
            "date": str(t["_sell_date"]),
            "ticker": t.get("Ticker"),
            "trade_pnl_egp": round(t["_pnl"], 2),
            "cumulative_pnl_egp": round(running - starting_equity, 2),
            "equity_egp": round(running, 2),
        })
    return curve


def compute_max_drawdown(equity_curve: list[dict]) -> dict:
    """Classic peak-to-trough drawdown on the equity curve's ``equity_egp``
    series. ``current_dd_pct`` is how far below the ALL-TIME peak the
    curve sits right now (0 if currently at a new high) - the number a
    trader actually watches day to day, distinct from max_dd_pct which is
    the worst historical drawdown regardless of whether it's been
    recovered from since."""
    if not equity_curve:
        return {"max_dd_pct": None, "max_dd_egp": None, "peak_date": None, "trough_date": None, "current_dd_pct": None}

    peak = equity_curve[0]["equity_egp"]
    peak_date = equity_curve[0]["date"]
    max_dd_pct = 0.0
    max_dd_egp = 0.0
    max_dd_peak_date = peak_date
    max_dd_trough_date = peak_date

    for pt in equity_curve:
        eq = pt["equity_egp"]
        if eq > peak:
            peak = eq
            peak_date = pt["date"]
        dd_egp = peak - eq
        dd_pct = (dd_egp / peak * 100.0) if peak > 0 else 0.0
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct
            max_dd_egp = dd_egp
            max_dd_peak_date = peak_date
            max_dd_trough_date = pt["date"]

    last_eq = equity_curve[-1]["equity_egp"]
    all_time_peak = max(pt["equity_egp"] for pt in equity_curve)
    current_dd_pct = ((all_time_peak - last_eq) / all_time_peak * 100.0) if all_time_peak > 0 else 0.0

    return {
        "max_dd_pct": round(max_dd_pct, 2),
        "max_dd_egp": round(max_dd_egp, 2),
        "peak_date": max_dd_peak_date,
        "trough_date": max_dd_trough_date,
        "current_dd_pct": round(current_dd_pct, 2),
    }


# ---------------------------------------------------------------------
# Breakdowns (signal quality, sector, tags, manual vs app)
# ---------------------------------------------------------------------

def _breakdown_by(trades: list[dict], group_fn, unknown_label: str) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        keys = group_fn(t) or [unknown_label]
        for k in keys:
            groups[k].append(t)
    rows = []
    for label, group in groups.items():
        wins = [t for t in group if t["_pnl"] > 0]
        losses = [t for t in group if t["_pnl"] < 0]
        decisive = len(wins) + len(losses)
        rows.append({
            "label": label,
            "trade_count": len(group),
            "win_rate_pct": round(len(wins) / decisive * 100.0, 1) if decisive else None,
            "total_pnl_egp": round(sum(t["_pnl"] for t in group), 2),
            "avg_pnl_pct": round(sum(t["_pnl_pct"] for t in group) / len(group), 2),
        })
    rows.sort(key=lambda r: r["total_pnl_egp"], reverse=True)
    return rows


def breakdown_by_entry_action(trades: list[dict]) -> list[dict]:
    """Answers "which Action Matrix signal actually pays off for me" -
    the single most actionable breakdown here, since it closes the loop
    between the matrix's recommendation and real, realized outcomes."""
    return _breakdown_by(trades, lambda t: [t.get("Entry Action")] if t.get("Entry Action") else None, "(no signal recorded / manual)")


def breakdown_by_sector(trades: list[dict]) -> list[dict]:
    return _breakdown_by(trades, lambda t: [t.get("Sector")] if t.get("Sector") else None, "(sector unknown)")


def breakdown_by_tag(trades: list[dict]) -> list[dict]:
    """Tags are free-form, comma-separated (see db_manager.update_trade_journal) -
    a trade tagged "earnings,gap-down" counts toward BOTH groups, since
    that's how a trader would actually want to slice "how do my
    earnings-play trades do" vs "how do my gap-down trades do"."""
    def tags_of(t):
        raw = (t.get("Tags") or "").strip()
        if not raw:
            return None
        return [tag.strip() for tag in raw.split(",") if tag.strip()]
    return _breakdown_by(trades, tags_of, "(untagged)")


def breakdown_by_source(trades: list[dict]) -> list[dict]:
    """App-tracked vs manually-logged trades - lets a user sanity-check
    whether their off-app trading (source='manual') is actually doing
    better or worse than the trades they placed off the matrix."""
    return _breakdown_by(trades, lambda t: [t.get("Source") or "app"], "app")


# ---------------------------------------------------------------------
# Benchmark alpha (the "did I beat the market" answer)
# ---------------------------------------------------------------------

def compute_benchmark_alpha(trades: list[dict], close_by_date, label: str = "EGX 30") -> dict:
    """Per-trade + aggregate alpha vs a benchmark close-by-date map (see
    market_regime.build_close_by_date - {date_str: close}). For each
    trade, the benchmark's own buy-and-hold return over that SAME entry->
    exit window is what "the market" would have returned holding nothing
    but the index for the same days - alpha is the trade's own % return
    minus that. Trades whose entry AND exit dates aren't both present in
    the benchmark series (weekend/holiday mismatches, or the benchmark
    not covering that far back) are skipped for alpha specifically -
    reported as ``trades_with_alpha`` vs ``total_trades`` so a partial
    benchmark history is visible, never silently averaged over as if it
    covered everything."""
    if not trades or not close_by_date:
        return {"available": False, "reason": "No trades or no benchmark data.", "label": label}

    per_trade = []
    for t in trades:
        entry_d = t["_purchase_date"]
        exit_d = t["_sell_date"]
        if entry_d is None:
            continue
        entry_close = close_by_date.get(str(entry_d))
        exit_close = close_by_date.get(str(exit_d))
        if not entry_close or not exit_close:
            continue
        bench_return_pct = (exit_close / entry_close - 1.0) * 100.0
        alpha_pct = t["_pnl_pct"] - bench_return_pct
        per_trade.append({
            "Ticker": t.get("Ticker"), "sell_date": str(exit_d),
            "trade_return_pct": round(t["_pnl_pct"], 2),
            "benchmark_return_pct": round(bench_return_pct, 2),
            "alpha_pct": round(alpha_pct, 2),
        })

    if not per_trade:
        return {"available": False, "reason": f"No trades overlap with available {label} history.", "label": label}

    beat = sum(1 for r in per_trade if r["alpha_pct"] > 0)
    avg_alpha = sum(r["alpha_pct"] for r in per_trade) / len(per_trade)
    return {
        "available": True,
        "label": label,
        "trades_with_alpha": len(per_trade),
        "total_trades": len(trades),
        "win_rate_vs_benchmark_pct": round(beat / len(per_trade) * 100.0, 1),
        "avg_alpha_pct": round(avg_alpha, 2),
        "per_trade": per_trade,
    }


# ---------------------------------------------------------------------
# Top-level report
# ---------------------------------------------------------------------

def build_performance_report(raw_trades: list[dict], starting_equity: float = 0.0,
                              benchmark_close_by_date: dict | None = None,
                              benchmark_label: str = "EGX 30") -> dict:
    """The single dict shape both the desktop Performance tab and any
    future export/report consume - one function call, everything computed
    off the same normalized trade list so every number is internally
    consistent (e.g. summary.total_trades always equals len(equity_curve))."""
    trades = normalize_trades(raw_trades)
    equity_curve = compute_equity_curve(trades, starting_equity=starting_equity)
    report = {
        "summary": compute_summary_stats(trades),
        "streaks": compute_streaks(trades),
        "equity_curve": equity_curve,
        "drawdown": compute_max_drawdown(equity_curve),
        "breakdown_by_signal": breakdown_by_entry_action(trades),
        "breakdown_by_sector": breakdown_by_sector(trades),
        "breakdown_by_tag": breakdown_by_tag(trades),
        "breakdown_by_source": breakdown_by_source(trades),
        "journal": [
            {
                "id": t.get("id"), "Ticker": t.get("Ticker"), "Buy Price": t.get("Buy Price"),
                "Sell Price": t.get("Sell Price"), "Shares Sold": t.get("Shares Sold"),
                "Purchase Date": t.get("Purchase Date"), "Sell Date": t.get("Sell Date"),
                "Realized P&L (EGP)": t.get("Realized P&L (EGP)"), "Realized P&L (%)": t.get("Realized P&L (%)"),
                "Entry Action": t.get("Entry Action"), "Sector": t.get("Sector"),
                "Tags": t.get("Tags"), "Notes": t.get("Notes"), "Source": t.get("Source"),
            }
            for t in reversed(trades)  # most recent first for a journal view
        ],
    }
    if benchmark_close_by_date:
        report["benchmark_alpha"] = compute_benchmark_alpha(trades, benchmark_close_by_date, label=benchmark_label)
    else:
        report["benchmark_alpha"] = {"available": False, "reason": "No benchmark data supplied.", "label": benchmark_label}
    return report


if __name__ == "__main__":
    # Tiny smoke test with synthetic trades - run directly
    # (`python trade_performance.py`) any time this module changes.
    sample = [
        {"Ticker": "COMI.CA", "Buy Price": 50, "Sell Price": 60, "Shares Sold": 100,
         "Purchase Date": "2026-01-01", "Sell Date": "2026-01-20",
         "Realized P&L (EGP)": 961.5, "Realized P&L (%)": 19.16,
         "Entry Action": "🔥 STRONG BUY", "Sector": "Banks", "Tags": None, "Notes": None, "Source": "app", "id": 1},
        {"Ticker": "SWDY.CA", "Buy Price": 20, "Sell Price": 18, "Shares Sold": 50,
         "Purchase Date": "2026-01-05", "Sell Date": "2026-01-15",
         "Realized P&L (EGP)": -106.65, "Realized P&L (%)": -10.63,
         "Entry Action": None, "Sector": "Industrial Goods", "Tags": "earnings,gap-down", "Notes": "Missed the stop", "Source": "manual", "id": 2},
        {"Ticker": "EAST.CA", "Buy Price": 15, "Sell Price": 17.5, "Shares Sold": 200,
         "Purchase Date": "2026-01-10", "Sell Date": "2026-02-01",
         "Realized P&L (EGP)": 480.2, "Realized P&L (%)": 16.5,
         "Entry Action": "🔥 STRONG BUY", "Sector": "Consumer Goods", "Tags": "breakout", "Notes": None, "Source": "app", "id": 3},
    ]
    bench = {"2026-01-01": 30000, "2026-01-20": 30300, "2026-01-05": 30100, "2026-01-15": 30050,
             "2026-01-10": 30150, "2026-02-01": 30600}
    report = build_performance_report(sample, benchmark_close_by_date=bench)
    import json
    print(json.dumps(report, indent=2, default=str))
