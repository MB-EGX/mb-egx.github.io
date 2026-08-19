"""
check_remaining_future_dates.py
================================
General-purpose version of check_remaining_dec5.py: lists EVERY row in
market_data still dated after --today, grouped by date, regardless of
which specific date it is. Use this after purge_future_date_duplicates.py
--fix (and/or data_repair_tools.py future-dates --fix) to confirm the
database is fully clean - not just clean of one specific date.

USAGE:
    python check_remaining_future_dates.py --db quant_master.duckdb --today 2026-08-19
"""
import argparse
from collections import Counter
from datetime import date, datetime

import duckdb


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="quant_master.duckdb")
    ap.add_argument("--today", default=None, help="YYYY-MM-DD; defaults to the system's today.")
    args = ap.parse_args()

    today = date.fromisoformat(args.today) if args.today else date.today()

    con = duckdb.connect(args.db, read_only=True)
    rows = con.execute(
        "SELECT ticker, date FROM market_data WHERE date > ? ORDER BY date, ticker;",
        [today.isoformat()],
    ).fetchall()
    con.close()

    if not rows:
        print(f"✅ No rows found after {today.isoformat()} - fully clean.")
        return

    by_date = Counter(str(d)[:10] for _, d in rows)
    print(f"⚠️  {len(rows)} row(s) still dated after {today.isoformat()}, across {len(by_date)} distinct date(s):\n")
    for d, count in sorted(by_date.items()):
        print(f"  {d}   ({count} ticker(s))")

    print(f"\nSample tickers for the first date ({sorted(by_date.keys())[0]}):")
    first_date = sorted(by_date.keys())[0]
    sample = [t for t, d in rows if str(d)[:10] == first_date][:15]
    for t in sample:
        print(f"    {t}")

    print("\nNext step: re-run")
    print(f"  python data_repair_tools.py future-dates --db {args.db} --today {today.isoformat()} --fix")
    print("then, if any of those report collisions again:")
    print(f"  python purge_future_date_duplicates.py --db {args.db} --today {today.isoformat()} --fix")


if __name__ == "__main__":
    main()
