"""
purge_future_date_duplicates.py
================================
Companion to `data_repair_tools.py future-dates --fix`.

That command fixes a future-dated row by swapping its day/month back to
the real date - but SKIPS ("would collide") any row whose swapped date
already has a real row on file for that ticker, and tells you to fix
it "by hand." This script IS that manual fix, applied safely and in
bulk instead of one ticker at a time.

WHY DELETE, NOT SWAP, FOR THESE ROWS:
    A collision means the ticker already has a legitimate row for the
    corrected date - the future-dated row isn't a mis-dated NEW session,
    it's a DUPLICATE of a session that's already correctly on file
    (this is exactly what happened here: the corruption incident wrote
    every ticker's already-ingested 2026-05-12 session a second time,
    mis-parsed as 2026-12-05 - see ingestion.py's dayfirst fix).
    Swapping would try to overwrite the good row's date onto itself/
    collide; the only correct fix is to remove the corrupted duplicate
    and keep the real row that was already there.

SAFETY:
    - Dry run by default - prints every row it WOULD delete, plus both
      rows' close price side-by-side so you can eyeball that the
      "existing" row really does look like real data (not itself junk).
    - Only touches a ticker/date pair if TWO conditions both hold:
        1. date > --today (still an impossible future date)
        2. swap_day_month(date) already has an existing row for the
           same ticker (a genuine collision, not a normal fixable case -
           normal fixable cases were already handled by
           `future-dates --fix` and are left alone here)
    - Nothing is deleted until you pass --fix.

USAGE
-----
    # 1. See what would be deleted (safe, read-only):
    python purge_future_date_duplicates.py --db quant_master.duckdb --today 2026-08-19

    # 2. Once you've eyeballed the list, actually delete:
    python purge_future_date_duplicates.py --db quant_master.duckdb --today 2026-08-19 --fix

Then re-run publish.py to regenerate market_data.json.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime

import duckdb


def swap_day_month(d: date) -> date | None:
    try:
        return date(d.year, d.day, d.month)
    except ValueError:
        return None


def _as_date(v) -> date:
    return v if isinstance(v, date) else datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


def main():
    ap = argparse.ArgumentParser(description="Delete future-dated rows that are duplicates of an already-correct row.")
    ap.add_argument("--db", default="quant_master.duckdb")
    ap.add_argument("--today", default=None, help="YYYY-MM-DD; defaults to the system's today.")
    ap.add_argument("--fix", action="store_true", help="Actually delete. Without this flag, dry-run only.")
    ap.add_argument("--verbose", action="store_true", help="Print every single ticker/date row instead of a summary. With thousands of collisions this is a LOT of output - the default summary is almost always enough to sanity-check the fix.")
    args = ap.parse_args()

    today = date.fromisoformat(args.today) if args.today else date.today()
    print(f"Treating any market_data row after {today.isoformat()} as impossible/corrupted.\n")

    con = duckdb.connect(args.db, read_only=not args.fix)
    bad_rows = con.execute(
        "SELECT ticker, date, close FROM market_data WHERE date > ? ORDER BY ticker;",
        [today.isoformat()],
    ).fetchall()

    if not bad_rows:
        print("✅ No future-dated rows found. Nothing to do.")
        con.close()
        return

    collisions = []
    for ticker, bad_date, bad_close in bad_rows:
        bad_date = _as_date(bad_date)
        swapped = swap_day_month(bad_date)
        if not swapped:
            continue
        existing = con.execute(
            "SELECT close FROM market_data WHERE ticker = ? AND date = ?;",
            [ticker, swapped.isoformat()],
        ).fetchone()
        if existing:
            collisions.append((ticker, bad_date, bad_close, swapped, existing[0]))

    if not collisions:
        print("✅ No collision rows found - nothing left for this script to do.")
        print("   (If future-dates --fix reported none either, your data is clean.)")
        con.close()
        return

    print(f"Found {len(collisions)} duplicate future-dated row(s) that collide with an existing correct row.\n")

    # With thousands of rows (this is one bulk duplication event, not
    # thousands of unrelated problems - see the pattern below), a
    # per-row table is 4,000+ lines of noise. Summarize by the
    # (bad_date -> real_date) PAIR instead: if this really is one bulk
    # duplicate-day event, you should see just ONE pair covering nearly
    # every row, which is itself the confirmation that this is safe to
    # bulk-delete.
    from collections import Counter
    pair_counts = Counter((bad_date.isoformat(), swapped.isoformat()) for _, bad_date, _, swapped, _ in collisions)
    print("Grouped by (corrupted date -> real date that already exists):")
    for (bad_iso, real_iso), count in sorted(pair_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {bad_iso}  ->  {real_iso}   ({count} ticker(s))")
    if len(pair_counts) == 1:
        print("\n  -> Exactly one (bad, real) date pair across ALL collisions - this is a single")
        print("     bulk duplication event (the same real session got re-inserted a second time")
        print("     under a mis-parsed date), not thousands of separate issues. Safe to bulk-delete.")
    else:
        print(f"\n  -> {len(pair_counts)} distinct (bad, real) date pairs - review these before deleting;")
        print("     more than one pair means more than one incident is mixed in here.")

    sample_n = 15
    print(f"\nSample of {min(sample_n, len(collisions))} of {len(collisions)} row(s) (ticker, bad close vs. real close):")
    print(f"  {'Ticker':<14}{'Bad close':<14}{'Real close':<14}")
    for ticker, bad_date, bad_close, swapped, real_close in collisions[:sample_n]:
        print(f"  {ticker:<14}{str(bad_close):<14}{str(real_close):<14}")
    if len(collisions) > sample_n and not args.verbose:
        print(f"  ... and {len(collisions) - sample_n} more (re-run with --verbose to see all of them)")
    elif args.verbose:
        for ticker, bad_date, bad_close, swapped, real_close in collisions[sample_n:]:
            print(f"  {ticker:<14}{str(bad_close):<14}{str(real_close):<14}")

    if not args.fix:
        print("\nThis was a DRY RUN - nothing was deleted.")
        print("  Check the sample above: 'Bad close' and 'Real close' should be close/identical")
        print("  (same session, duplicated) - that's the sign this is safe to delete.")
        print("  If it looks right, re-run with --fix to delete all the duplicate rows.")
        con.close()
        return

    print(f"\nDeleting {len(collisions)} duplicate row(s)...")
    for ticker, bad_date, _bad_close, _swapped, _real_close in collisions:
        con.execute(
            "DELETE FROM market_data WHERE ticker = ? AND date = ?;",
            [ticker, bad_date.isoformat()],
        )
    con.close()
    print("✅ Done. Now re-run publish.py to refresh market_data.json.")


if __name__ == "__main__":
    main()
