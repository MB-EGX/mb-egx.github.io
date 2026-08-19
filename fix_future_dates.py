"""
fix_future_dates.py
====================
Finds every row in market_data whose date is AFTER today (impossible for
historical data) and fixes it - both in the database and in the source
CSV in market_data_feeds/ - so the corruption doesn't come back on a
future re-ingestion.

ROOT CAUSE: normalize_historical_csvs.py assumed every raw CSV used US
MM/DD/YYYY date order. At least one of your 288 files was actually
DD/MM/YYYY, so a real date like "12/05/2026" (12 May) got written out
as "2026-12-05" (5 December) - a date in the future. Your app derives
"today's session" as MAX(date) across market_data (see freshness.py),
so that one bad row hijacked the whole app's notion of "now."

THE FIX: for each future-dated row, swap its day and month back
(2026-12-05 -> 2026-05-12). If that lands on-or-before today, it's
almost certainly the correct date, and this script:
  1. Prints every affected (ticker, bad_date, likely_correct_date).
  2. With --fix: updates the row's date directly in market_data, AND
     finds + corrects the same row in its source CSV under
     market_data_feeds/ (matched by ticker + the bad date), so a future
     full re-ingest can't reintroduce the same corrupted date.
  3. Anything that DOESN'T have a valid swapped date (still in the
     future, or invalid as a calendar date) is left alone and printed
     separately - open the corresponding CSV and fix it by hand.

USAGE:
    python fix_future_dates.py                     # dry run - just diagnose
    python fix_future_dates.py --fix                # apply the fixes
    python fix_future_dates.py --fix --today 2026-08-19   # override "today" if needed
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
from datetime import date, datetime

import duckdb


def swap_day_month(d: date) -> date | None:
    """Returns a date with day/month swapped, or None if that's not a
    valid calendar date (e.g. day > 12, so swapping would need a month
    > 12)."""
    try:
        return date(d.year, d.day, d.month)
    except ValueError:
        return None


def patch_csv_file(feeds_dir: str, ticker: str, bad_iso: str, fixed_iso: str) -> bool:
    """Finds the row for `ticker` at date `bad_iso` inside market_data_feeds/
    and rewrites its Date to `fixed_iso`. Tries the expected filename first
    (TICKER_CA.csv, matching normalize_historical_csvs.py's naming), then
    falls back to scanning every CSV in the folder for a matching
    Ticker+Date pair (covers files ingested some other way). Returns True
    if a row was found and patched."""
    candidates = [os.path.join(feeds_dir, f"{ticker.replace('.', '_')}.csv")]
    candidates += [p for p in glob.glob(os.path.join(feeds_dir, "**", "*.csv"), recursive=True) if p not in candidates]

    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            with open(path, newline="", encoding="utf-8-sig") as fh:
                rows = list(csv.reader(fh))
        except Exception:
            continue
        if not rows:
            continue
        header = [h.strip().lower() for h in rows[0]]
        if "date" not in header or "ticker" not in header:
            continue
        d_idx, t_idx = header.index("date"), header.index("ticker")

        changed = False
        for row in rows[1:]:
            if len(row) > max(d_idx, t_idx) and row[t_idx].strip().upper() == ticker and row[d_idx].strip() == bad_iso:
                row[d_idx] = fixed_iso
                changed = True

        if changed:
            with open(path, "w", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerows(rows)
            print(f"      patched source file: {path}")
            return True
    return False


def main():
    ap = argparse.ArgumentParser(description="Find and fix impossible future dates in market_data.")
    ap.add_argument("--db", default="quant_master.duckdb")
    ap.add_argument("--feeds-dir", default="market_data_feeds")
    ap.add_argument("--today", default=None, help="Override today's date (YYYY-MM-DD). Default: real system date.")
    ap.add_argument("--fix", action="store_true", help="Apply fixes. Without this flag, only diagnoses.")
    args = ap.parse_args()

    today = date.fromisoformat(args.today) if args.today else date.today()
    print(f"Treating any market_data row after {today.isoformat()} as impossible/corrupted.\n")

    con = duckdb.connect(args.db, read_only=not args.fix)
    rows = con.execute(
        "SELECT ticker, date FROM market_data WHERE date > ? ORDER BY date DESC, ticker;",
        [today.isoformat()],
    ).fetchall()

    if not rows:
        print("✅ No future-dated rows found. Nothing to fix.")
        return

    print(f"Found {len(rows)} future-dated row(s):\n")
    fixable, unfixable = [], []
    for ticker, bad_date in rows:
        bad_date = bad_date if isinstance(bad_date, date) else datetime.strptime(str(bad_date), "%Y-%m-%d").date()
        swapped = swap_day_month(bad_date)
        if swapped and swapped <= today:
            fixable.append((ticker, bad_date, swapped))
            print(f"  {ticker:<12} {bad_date.isoformat()}  ->  likely correct: {swapped.isoformat()}")
        else:
            unfixable.append((ticker, bad_date))
            print(f"  {ticker:<12} {bad_date.isoformat()}  ->  NO obvious fix (still future/invalid after day/month swap) - needs manual review")

    if unfixable:
        print(f"\n⚠️  {len(unfixable)} row(s) need manual review - open their source CSV in "
              f"{args.feeds_dir} and check the Date column by hand.")

    if not args.fix:
        print("\nThis was a DRY RUN - nothing was changed. Re-run with --fix to apply the fixes above.")
        return

    print(f"\nApplying {len(fixable)} fix(es)...")
    for ticker, bad_date, swapped in fixable:
        exists = con.execute(
            "SELECT 1 FROM market_data WHERE ticker = ? AND date = ?;",
            [ticker, swapped.isoformat()],
        ).fetchone()
        if exists:
            print(f"  ⚠️  {ticker} already has a row on {swapped.isoformat()} - skipping DB update "
                  f"(would collide). Fix this one by hand.")
            continue
        con.execute(
            "UPDATE market_data SET date = ? WHERE ticker = ? AND date = ?;",
            [swapped.isoformat(), ticker, bad_date.isoformat()],
        )
        print(f"  ✅ {ticker}: {bad_date.isoformat()} -> {swapped.isoformat()} (database)")
        if not patch_csv_file(args.feeds_dir, ticker, bad_date.isoformat(), swapped.isoformat()):
            print(f"      ⚠️  couldn't find the source row in {args.feeds_dir} to patch - "
                  f"the database is fixed, but check the CSV by hand so a future full "
                  f"re-ingest doesn't bring the bad date back.")

    con.close()
    print("\nDone. Now re-run publish.py (or just Run Ingestion in the desktop app) to refresh the exported JSON.")


if __name__ == "__main__":
    main()
