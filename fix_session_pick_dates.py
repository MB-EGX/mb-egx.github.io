"""
fix_session_pick_dates.py
==========================
fix_future_dates.py fixed the corrupted rows in `market_data` (the
MM/DD vs DD/MM date-order bug). But that corruption had already
poisoned `session_picks` BEFORE it was caught: every refresh_session_
picks() run that happened while MAX(date) in market_data was reporting
a future date (e.g. 2026-12-05) stamped THAT bad date into any row it
touched during that window - either as `pick_date` (a brand new pick)
or `achieved_date` (a pick that crossed its target that run). Fixing
market_data does not retroactively fix those already-written stamps -
same reason a single ticker (RTVC.CA) needed a manual Remove in the
GUI, except this time it's baked into `session_picks` rows you can't
just click a button to clear (achieved history isn't removable from
the GUI, and a still-active pick with a bad pick_date but a correct/
different current price isn't necessarily wrong to KEEP - just wrongly
dated).

THE FIX: for every row whose pick_date and/or achieved_date is still a
future date (impossible for a value that's supposed to be "the real
calendar day this happened"), reverse-engineer the correct date by
matching the stamped price (ref_price for pick_date, achieved_price
for achieved_date) against that ticker's real market_data history -
the row's own price is a fingerprint of which real session it actually
happened on, independent of the corrupted timestamp.

Match rule: candidate dates are real (<= --today) market_data rows for
that ticker whose close is within --tolerance-pct of the stamped
price. If more than one date matches:
    - and the row has a still-good anchor date in the OTHER column
      (e.g. achieved_date is fine but pick_date is bad), the candidate
      closest to - and no later than - that anchor is chosen;
    - otherwise the row is left for manual review rather than guessed.
If NO date matches within tolerance, the row is also left for manual
review (printed separately) - never silently left un-flagged.

USAGE:
    python fix_session_pick_dates.py                        # dry run
    python fix_session_pick_dates.py --fix                  # apply
    python fix_session_pick_dates.py --fix --today 2026-08-19
    python fix_session_pick_dates.py --tolerance-pct 0.15    # default 0.05
"""
from __future__ import annotations

import argparse
from datetime import date, datetime

import duckdb


def normalize_symbol(ticker: str) -> str:
    """Same rule as db_manager.DatabaseManager.normalize_symbol - kept
    duplicated here (no import) so this script has zero dependency on
    the app's own modules and can run standalone against just the
    .duckdb file."""
    t = str(ticker).strip().upper()
    if not t.endswith(".CA") and len(t) <= 6 and "." not in t:
        return t + ".CA"
    return t


def _as_date(v) -> date | None:
    if v is None:
        return None
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def find_price_matches(con, ticker: str, price: float, today: date, tolerance_pct: float) -> list[tuple[date, float]]:
    """Every real (<= today) market_data (date, close) row for `ticker`
    whose close is within `tolerance_pct`% of `price`, closest match
    first."""
    if price is None:
        return []
    rows = con.execute(
        "SELECT date, close FROM market_data WHERE ticker = ? AND date <= ? AND close IS NOT NULL;",
        [ticker, today.isoformat()],
    ).fetchall()
    tol = abs(price) * (tolerance_pct / 100.0)
    matches = []
    for d, close in rows:
        d = _as_date(d)
        if d is None or close is None:
            continue
        diff = abs(float(close) - float(price))
        if diff <= max(tol, 1e-9):
            matches.append((d, float(close), diff))
    matches.sort(key=lambda m: m[2])
    return [(d, c) for d, c, _diff in matches]


def choose_candidate(matches: list[tuple[date, float]], anchor: date | None, anchor_is_after: bool) -> date | None:
    """Picks the single best real date out of `matches` (already sorted
    closest-price-match first).

    - No anchor: only auto-picks when there's exactly ONE distinct
      candidate date (ambiguous otherwise -> manual review).
    - anchor_is_after=True means the anchor (e.g. achieved_date) must
      come ON OR AFTER the corrected date (pick_date <= achieved_date).
      anchor_is_after=False is the reverse (achieved_date >= pick_date,
      solving for achieved_date so it must be ON OR AFTER pick_date).
    """
    distinct_dates = sorted({d for d, _c in matches})
    if not distinct_dates:
        return None
    if anchor is None:
        return distinct_dates[0] if len(distinct_dates) == 1 else None
    if anchor_is_after:
        valid = [d for d in distinct_dates if d <= anchor]
    else:
        valid = [d for d in distinct_dates if d >= anchor]
    if not valid:
        return None
    # Closest to the anchor among chronologically-valid candidates.
    valid.sort(key=lambda d: abs((anchor - d).days))
    return valid[0]


def main():
    ap = argparse.ArgumentParser(description="Fix corrupted pick_date/achieved_date stamps in session_picks left over from the market_data future-date bug.")
    ap.add_argument("--db", default="quant_master.duckdb")
    ap.add_argument("--today", default=None, help="Override today's real date (YYYY-MM-DD). Default: system date.")
    ap.add_argument("--tolerance-pct", type=float, default=0.05, help="Max %% difference between the stamped price and a market_data close to count as a match (default 0.05%%).")
    ap.add_argument("--fix", action="store_true", help="Apply the fixes. Without this flag, only diagnoses.")
    args = ap.parse_args()

    today = date.fromisoformat(args.today) if args.today else date.today()
    print(f"Treating any session_picks pick_date/achieved_date after {today.isoformat()} as an impossible/corrupted stamp.\n")

    con = duckdb.connect(args.db, read_only=not args.fix)

    rows = con.execute(
        "SELECT id, ticker, horizon, pick_date, ref_price, status, achieved_date, achieved_price, achieved_pct "
        "FROM session_picks WHERE pick_date > ? OR achieved_date > ? ORDER BY id;",
        [today.isoformat(), today.isoformat()],
    ).fetchall()

    if not rows:
        print("✅ No corrupted pick_date/achieved_date stamps found. Nothing to fix.")
        return

    print(f"Found {len(rows)} row(s) with a future-dated stamp:\n")

    fixes = []       # (id, column, old_value, new_value, ticker)
    needs_review = []  # (id, ticker, column, old_value, price, candidates)

    for (pick_id, ticker, horizon, pick_date, ref_price, status,
         achieved_date, achieved_price, achieved_pct) in rows:

        pick_date = _as_date(pick_date)
        achieved_date = _as_date(achieved_date)
        norm_ticker = normalize_symbol(ticker)

        pick_bad = pick_date is not None and pick_date > today
        achieved_bad = achieved_date is not None and achieved_date > today

        print(f"  id={pick_id:<6} {ticker:<12} {horizon:<7} status={status}")

        new_pick_date = pick_date
        new_achieved_date = achieved_date

        if pick_bad:
            matches = find_price_matches(con, norm_ticker, ref_price, today, args.tolerance_pct)
            # achieved_date is a valid anchor only if it isn't ALSO corrupted.
            anchor = achieved_date if (achieved_date is not None and not achieved_bad) else None
            candidate = choose_candidate(matches, anchor, anchor_is_after=True)
            if candidate:
                print(f"      pick_date     {pick_date}  ->  {candidate}  (matched ref_price {ref_price} in market_data)")
                new_pick_date = candidate
                fixes.append((pick_id, "pick_date", pick_date, candidate, ticker))
            else:
                distinct = sorted({d for d, _c in matches})
                print(f"      pick_date     {pick_date}  ->  NO confident match ({len(distinct)} candidate date(s): "
                      f"{[d.isoformat() for d in distinct] or 'none'}) - needs manual review")
                needs_review.append((pick_id, ticker, "pick_date", pick_date, ref_price, distinct))

        if achieved_bad:
            matches = find_price_matches(con, norm_ticker, achieved_price, today, args.tolerance_pct)
            anchor = new_pick_date if (new_pick_date is not None and new_pick_date <= today) else None
            candidate = choose_candidate(matches, anchor, anchor_is_after=False)
            if candidate:
                print(f"      achieved_date {achieved_date}  ->  {candidate}  (matched achieved_price {achieved_price} in market_data)")
                new_achieved_date = candidate
                fixes.append((pick_id, "achieved_date", achieved_date, candidate, ticker))
            else:
                distinct = sorted({d for d, _c in matches})
                print(f"      achieved_date {achieved_date}  ->  NO confident match ({len(distinct)} candidate date(s): "
                      f"{[d.isoformat() for d in distinct] or 'none'}) - needs manual review")
                needs_review.append((pick_id, ticker, "achieved_date", achieved_date, achieved_price, distinct))

        print()

    print("=" * 78)
    print(f" ✅ Auto-resolvable fixes : {len(fixes)}")
    print(f" ⚠️  Needs manual review   : {len(needs_review)}")
    print("=" * 78)

    if needs_review:
        print("\nRows needing manual review (open the desktop app / query the DB directly):")
        for pick_id, ticker, col, old_val, price, candidates in needs_review:
            cand_str = ", ".join(d.isoformat() for d in candidates) if candidates else "(no price match at all in market_data - ticker may be missing history for that period)"
            print(f"   id={pick_id:<6} {ticker:<12} {col:<14} was {old_val}  price={price}  candidates: {cand_str}")

    if not args.fix:
        print("\nThis was a DRY RUN - nothing was changed. Re-run with --fix to apply the resolvable fixes above.")
        con.close()
        return

    print(f"\nApplying {len(fixes)} fix(es)...")
    for pick_id, column, old_val, new_val, ticker in fixes:
        con.execute(
            f"UPDATE session_picks SET {column} = ? WHERE id = ?;",
            [new_val.isoformat(), int(pick_id)],
        )
        print(f"  ✅ id={pick_id} {ticker}: {column} {old_val} -> {new_val}")

    con.close()
    print("\nDone. Restart the desktop app (or just switch tabs / re-open Session Picks) to see the corrected dates.")
    if needs_review:
        print(f"\n⚠️  {len(needs_review)} row(s) above still need manual review - those were left untouched.")


if __name__ == "__main__":
    main()
