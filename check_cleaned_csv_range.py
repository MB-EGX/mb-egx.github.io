"""
check_cleaned_csv_range.py
===========================
Reads every file in cleaned_for_ingestion/ DIRECTLY (no database
involved) and reports each file's own row count and date range. This
settles one specific question: do the "historical" CSVs actually
contain dates before your existing live feed's start (2026-01-02), or
do they just cover roughly the same window you already had?

If most files here show first_date well before 2026, but
check_bar_coverage.py still shows ~158 bars/ticker after ingestion,
the problem is in ingestion/dedup. If most files here ALSO start
around 2026-01, there's nothing to fix downstream - the raw source
files themselves just don't go back further, and the small bump you
saw (158 vs 143) is the real, honest ceiling of what you currently have.

USAGE (run from the repo folder):
    python check_cleaned_csv_range.py --dir cleaned_for_ingestion
"""
import argparse
import csv
import glob
import os
import statistics


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="cleaned_for_ingestion")
    ap.add_argument("--top-n", type=int, default=20)
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.dir, "*.csv")))
    if not files:
        print(f"No CSV files found in {args.dir}/ - did normalize_historical_csvs.py actually write here?")
        return

    rows_summary = []
    empty_files = []
    for path in files:
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            dates = [r["Date"] for r in reader if r.get("Date")]
        if not dates:
            empty_files.append(os.path.basename(path))
            continue
        dates.sort()
        rows_summary.append({
            "file": os.path.basename(path),
            "rows": len(dates),
            "first": dates[0],
            "last": dates[-1],
        })

    if not rows_summary:
        print("Every file was empty (0 parsed rows). normalize_historical_csvs.py likely failed silently on all of them.")
        return

    rows_summary.sort(key=lambda r: r["first"])  # earliest-starting files first

    row_counts = [r["rows"] for r in rows_summary]
    first_dates = [r["first"] for r in rows_summary]

    print(f"Files with parsed rows: {len(rows_summary)}/{len(files)}")
    if empty_files:
        print(f"⚠️  {len(empty_files)} file(s) parsed to ZERO rows: {empty_files[:10]}{' ...' if len(empty_files) > 10 else ''}")
    print(f"Row count per file — min: {min(row_counts)}  median: {statistics.median(row_counts):.0f}  max: {max(row_counts)}")
    print(f"Earliest 'first date' across all files: {min(first_dates)}")
    print(f"Latest 'first date' across all files:    {max(first_dates)}")

    print(f"\n{min(args.top_n, len(rows_summary))} file(s) with the EARLIEST first date (your real deepest history):")
    print(f"  {'File':<28}{'Rows':<8}{'First date':<14}{'Last date':<14}")
    for r in rows_summary[: args.top_n]:
        print(f"  {r['file']:<28}{r['rows']:<8}{r['first']:<14}{r['last']:<14}")

    print(f"\n{min(args.top_n, len(rows_summary))} file(s) with the LATEST first date (your shallowest history):")
    for r in rows_summary[-args.top_n:][::-1]:
        print(f"  {r['file']:<28}{r['rows']:<8}{r['first']:<14}{r['last']:<14}")

    n_before_2026 = sum(1 for r in rows_summary if r["first"] < "2026-01-01")
    print(f"\nFiles whose history starts before 2026-01-01: {n_before_2026}/{len(rows_summary)}")
    if n_before_2026 == 0:
        print("⚠️  NONE of your cleaned historical files go back before 2026 — they cover essentially")
        print("   the same window your live daily feed already has. There's nothing wrong with your")
        print("   copy/ingest steps; the raw 'Stock Price History' CSVs you downloaded simply don't")
        print("   contain deeper history than that (likely a short date-range export, e.g. '6 months'")
        print("   or '1Y' instead of 'Max' from whatever site you pulled them from).")
        print("   Next step: re-download those files with a longer date range (5Y/Max) if the source")
        print("   site offers it, then re-run build_ticker_map.py / normalize_historical_csvs.py.")
    else:
        print(f"✅ {n_before_2026} file(s) genuinely extend before 2026 - if check_bar_coverage.py still")
        print("   shows small bar counts after ingesting these, the issue is downstream in ingestion.py,")
        print("   not the source files. Worth checking market_data.file_tracker / for parse errors.")


if __name__ == "__main__":
    main()
