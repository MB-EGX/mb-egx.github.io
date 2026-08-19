"""
check_remaining_dec5.py
========================
One-off diagnostic: lists every ticker that STILL has a 2026-12-05 row
in market_data, so you can confirm the repair (data_repair_tools.py
future-dates --fix + purge_future_date_duplicates.py --fix) actually
got everything.

USAGE:
    python check_remaining_dec5.py --db quant_master.duckdb
"""
import argparse
import duckdb


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="quant_master.duckdb")
    ap.add_argument("--date", default="2026-12-05")
    args = ap.parse_args()

    con = duckdb.connect(args.db, read_only=True)
    rows = con.execute(
        "SELECT ticker, close FROM market_data WHERE date = ? ORDER BY ticker;",
        [args.date],
    ).fetchall()
    con.close()

    if not rows:
        print(f"✅ No rows found for {args.date} - fully clean.")
        return

    print(f"⚠️  {len(rows)} ticker(s) STILL have a row on {args.date}:\n")
    for ticker, close in rows:
        print(f"  {ticker:<14} close={close}")
    print(f"\nRun data_repair_tools.py future-dates --db {args.db} --today <today> --fix again,")
    print("then purge_future_date_duplicates.py --fix if any of these still collide with a real row.")


if __name__ == "__main__":
    main()
