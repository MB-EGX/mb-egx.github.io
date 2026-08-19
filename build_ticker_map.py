"""
build_ticker_map.py
====================
Auto-matches your ~260 raw historical CSV filenames (e.g. "A Capital
Holding Stock Price History.csv") to real EGX tickers - WITHOUT you
typing 260 tickers by hand.

HOW: reuses the exact same normalize-and-compare-word-overlap approach
your own db_manager.get_sector_map() already uses to match a company
name to a ticker. It reads your real `ticker_names` table (ticker,
name) out of your local quant_master.duckdb - built up over time from
every file you've ever ingested that carried a company-name column -
and matches each filename's company name against it.

You are NOT typing 260 tickers. You're running this once, and only
filling in the small number of filenames it couldn't confidently
match (printed to needs_review.csv).

USAGE (run from your MB-EGX repo folder, where quant_master.duckdb lives):
    python build_ticker_map.py --raw-dir "path/to/your/260 csv files"

Outputs, next to this script:
    ticker_map.csv     filename_stem,ticker,matched_name,confidence
    needs_review.csv   filename_stem   (fill in a "ticker" column by hand)

Then feed ticker_map.csv into normalize_historical_csvs.py.
"""
from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

import duckdb

# Same normalization + stopword-stripping regex as db_manager.get_sector_map(),
# reused verbatim so filename matching agrees with how your app already
# matches company names to tickers everywhere else.
_STOPWORDS = r'\b(s\.?a\.?e\.?|co\.?|company|egypt|egyptian|for|and|of|the|in|-|–|&|holding|group)\b'
_SUFFIX_RE = re.compile(r'\s*stock\s*price\s*history\s*$', re.IGNORECASE)


def normalize_tokens(text: str) -> set:
    s = text.lower().replace("_", " ").replace("go green", "gogreen")
    s = re.sub(_STOPWORDS, ' ', s)
    s = re.sub(r'[^\w\s]', ' ', s)
    return set(s.split())


def filename_to_company_name(stem: str) -> str:
    """'A_Capital_Holding_Stock_Price_History' -> 'A Capital Holding'"""
    s = stem.replace("_", " ").strip()
    s = _SUFFIX_RE.sub("", s)
    return s.strip()


def load_ticker_names(db_path: str) -> list[tuple[str, str]]:
    con = duckdb.connect(db_path, read_only=True)
    try:
        return con.execute("SELECT ticker, name FROM ticker_names WHERE name IS NOT NULL AND name != '';").fetchall()
    finally:
        con.close()


def best_match(name_tokens: set, candidates: list[tuple[str, set]], min_ratio: float):
    """Same overlap-ratio scoring as get_sector_map(): overlap / min(len(a), len(b))."""
    best_ticker, best_ratio = None, 0.0
    for ticker, cand_tokens in candidates:
        if not cand_tokens or not name_tokens:
            continue
        overlap = len(name_tokens & cand_tokens)
        if overlap == 0:
            continue
        ratio = overlap / min(len(name_tokens), len(cand_tokens))
        if ratio > best_ratio:
            best_ratio, best_ticker = ratio, ticker
    if best_ratio >= min_ratio:
        return best_ticker, round(best_ratio, 2)
    return None, round(best_ratio, 2)


def main():
    ap = argparse.ArgumentParser(description="Auto-match raw historical CSV filenames to EGX tickers.")
    ap.add_argument("--raw-dir", required=True, help="Folder containing your ~260 raw CSV files.")
    ap.add_argument("--db", default="quant_master.duckdb", help="Path to your local DuckDB file (default: quant_master.duckdb in the current folder).")
    ap.add_argument("--min-ratio", type=float, default=0.5, help="Match confidence threshold (default 0.5, same as get_sector_map()).")
    ap.add_argument("--out", default="ticker_map.csv")
    ap.add_argument("--review-out", default="needs_review.csv")
    args = ap.parse_args()

    raw_dir = Path(args.raw_dir)
    csv_files = sorted(raw_dir.glob("*.csv"))
    if not csv_files:
        raise SystemExit(f"No .csv files found in {raw_dir}")

    print(f"Found {len(csv_files)} raw CSV file(s) in {raw_dir}")

    rows = load_ticker_names(args.db)
    print(f"Loaded {len(rows)} (ticker, name) pair(s) from ticker_names in {args.db}")
    candidates = [(ticker, normalize_tokens(name)) for ticker, name in rows]

    matched, unmatched = [], []
    for f in csv_files:
        company_name = filename_to_company_name(f.stem)
        tokens = normalize_tokens(company_name)
        ticker, ratio = best_match(tokens, candidates, args.min_ratio)
        if ticker:
            matched.append((f.name, ticker, company_name, ratio))
        else:
            unmatched.append((f.name, company_name, ratio))

    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["filename", "ticker", "matched_company_name", "confidence"])
        w.writerows(matched)

    with open(args.review_out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["filename", "guessed_company_name", "best_ratio_found", "ticker"])
        for name, guess, ratio in unmatched:
            w.writerow([name, guess, ratio, ""])  # "ticker" column left blank for you to fill in

    print(f"\n✅ Auto-matched: {len(matched)}/{len(csv_files)} -> {args.out}")
    print(f"⚠️  Needs manual review: {len(unmatched)}/{len(csv_files)} -> {args.review_out}")
    if unmatched:
        print("   Open needs_review.csv, fill in the 'ticker' column for each row,")
        print("   then append those rows into ticker_map.csv (same 4 columns; leave")
        print("   matched_company_name/confidence blank if you like) before running")
        print("   normalize_historical_csvs.py.")


if __name__ == "__main__":
    main()
