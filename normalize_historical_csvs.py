"""
normalize_historical_csvs.py
=============================
Converts your raw historical CSVs (Date, Price, Open, High, Low, Vol.,
Change % - no Ticker column, US-order MM/DD/YYYY dates, "263.53K"/
"5.20M" style volume) into files your existing ingestion.py will parse
correctly and without silent corruption.

Fixes the two silent bugs a raw file would otherwise hit:
  1. No Ticker column -> file rejected outright.
     Fixed by adding one, using the filename->ticker map from
     build_ticker_map.py.
  2. ingestion._parse_excel_date() calls pd.to_datetime(val,
     dayfirst=True). Your CSVs are MM/DD/YYYY (US order), so any date
     where both day and month are <=12 gets silently misread. Fixed by
     rewriting every date here to unambiguous ISO YYYY-MM-DD *before*
     ingestion.py ever sees it - dayfirst=True is a no-op on an ISO
     string, so this neutralizes the bug without touching ingestion.py.
  3. ingestion._clean_volume() does a plain float(val) - "263.53K"
     raises and silently becomes 0.0. Fixed by expanding K/M/B suffixes
     to real numbers here first.

USAGE:
    python normalize_historical_csvs.py \
        --raw-dir "path/to/your/260 csv files" \
        --ticker-map ticker_map.csv \
        --out-dir cleaned_for_ingestion

Then copy/symlink cleaned_for_ingestion/*.csv into market_data_feeds/
and run publish.py (or the desktop app's ingest) as normal.
"""
from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

_SUFFIX_RE = re.compile(r'\s*stock\s*price\s*history\s*$', re.IGNORECASE)
_VOL_RE = re.compile(r'^([\d,.]+)\s*([KkMmBb])?$')
_VOL_MULT = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}


def filename_key(stem: str) -> str:
    """Same normalization used on both sides so a filename always finds
    its row in ticker_map.csv regardless of how it's spelled there."""
    s = stem.replace("_", " ").strip().lower()
    s = _SUFFIX_RE.sub("", s)
    return re.sub(r'\s+', ' ', s).strip()


def load_ticker_map(path: str) -> dict:
    m = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            filename = row.get("filename", "")
            ticker = (row.get("ticker") or "").strip().upper()
            if filename and ticker:
                m[filename_key(Path(filename).stem)] = ticker
    return m


def parse_us_date(raw: str) -> str | None:
    """Strict MM/DD/YYYY (or M/D/YYYY) parse - no dayfirst ambiguity,
    since we know these CSVs are always US-ordered. Returns ISO
    YYYY-MM-DD, or None if the cell doesn't match."""
    raw = raw.strip()
    m = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{4})$', raw)
    if not m:
        return None
    mo, da, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= mo <= 12 and 1 <= da <= 31):
        return None
    return f"{yr:04d}-{mo:02d}-{da:02d}"


def parse_volume(raw: str) -> str:
    """'263.53K' -> '263530'; '5.20M' -> '5200000'; '' / '-' -> '0'."""
    raw = raw.strip().replace(",", "")
    if raw in ("", "-", "--"):
        return "0"
    m = _VOL_RE.match(raw)
    if not m:
        return "0"
    num_str, suffix = m.group(1), m.group(2)
    num = float(num_str)
    if suffix:
        num *= _VOL_MULT[suffix.upper()]
    return str(int(round(num)))


def clean_price(raw: str) -> str:
    return raw.strip().replace(",", "")


def convert_file(src: Path, ticker: str, dst_dir: Path) -> tuple[int, int]:
    """Returns (rows_written, rows_skipped)."""
    with open(src, newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        header = [h.strip().lower() for h in next(reader)]
        idx = {name: i for i, name in enumerate(header)}
        required = {"date", "price", "open", "high", "low", "vol."}
        if not required.issubset(idx.keys()):
            raise ValueError(f"Unexpected header in {src.name}: {header}")

        out_rows = []
        skipped = 0
        for row in reader:
            iso_date = parse_us_date(row[idx["date"]])
            if not iso_date:
                skipped += 1
                continue
            out_rows.append({
                "Date": iso_date,
                "Ticker": ticker,
                "Open": clean_price(row[idx["open"]]),
                "High": clean_price(row[idx["high"]]),
                "Low": clean_price(row[idx["low"]]),
                "Close": clean_price(row[idx["price"]]),
                "Volume": parse_volume(row[idx["vol."]]),
            })

    # Ascending (oldest-first) - avoids any _file_mod_time dedup ambiguity
    # if this ticker's file is ever re-dropped later.
    out_rows.sort(key=lambda r: r["Date"])

    dst_dir.mkdir(parents=True, exist_ok=True)
    dst_path = dst_dir / f"{ticker.replace('.', '_')}.csv"
    with open(dst_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["Date", "Ticker", "Open", "High", "Low", "Close", "Volume"])
        w.writeheader()
        w.writerows(out_rows)

    return len(out_rows), skipped


def main():
    ap = argparse.ArgumentParser(description="Normalize raw historical CSVs for ingestion.py.")
    ap.add_argument("--raw-dir", required=True)
    ap.add_argument("--ticker-map", default="ticker_map.csv")
    ap.add_argument("--out-dir", default="cleaned_for_ingestion")
    args = ap.parse_args()

    raw_dir = Path(args.raw_dir)
    out_dir = Path(args.out_dir)
    ticker_map = load_ticker_map(args.ticker_map)
    print(f"Loaded {len(ticker_map)} filename->ticker mapping(s) from {args.ticker_map}")

    csv_files = sorted(raw_dir.glob("*.csv"))

    # Safety net: if two different raw files were mapped to the SAME
    # ticker (e.g. a bad auto-match collision in ticker_map.csv), this
    # script would otherwise write both to the same <ticker>.csv and the
    # second one silently overwrites the first - quietly losing an
    # entire company's history with no error. Detect that up front and
    # skip the whole group instead of guessing which one is right.
    ticker_to_files: dict[str, list[str]] = {}
    for f in csv_files:
        ticker = ticker_map.get(filename_key(f.stem))
        if ticker:
            ticker_to_files.setdefault(ticker, []).append(f.name)
    conflicts = {t: names for t, names in ticker_to_files.items() if len(names) > 1}
    if conflicts:
        print(f"⚠️  {len(conflicts)} ticker(s) are mapped to MORE THAN ONE file in {args.ticker_map} - "
              f"these would silently overwrite each other, so they're SKIPPED until you fix the mapping:")
        for ticker, names in conflicts.items():
            print(f"   {ticker}:")
            for name in names:
                print(f"     - {name}")
        print()

    converted, unmapped, failed = 0, [], []

    for f in csv_files:
        key = filename_key(f.stem)
        ticker = ticker_map.get(key)
        if not ticker:
            unmapped.append(f.name)
            continue
        if ticker in conflicts:
            continue
        try:
            written, skipped = convert_file(f, ticker, out_dir)
            print(f"  {f.name:<55} -> {ticker:<12} {written} bars written, {skipped} row(s) skipped")
            converted += 1
        except Exception as e:
            failed.append((f.name, str(e)))

    print(f"\n✅ Converted {converted}/{len(csv_files)} file(s) -> {out_dir}/")
    if unmapped:
        print(f"⚠️  {len(unmapped)} file(s) had no ticker in {args.ticker_map} - skipped:")
        for name in unmapped[:20]:
            print(f"     {name}")
        if len(unmapped) > 20:
            print(f"     ... and {len(unmapped) - 20} more")
    if failed:
        print(f"❌ {len(failed)} file(s) failed to parse (unexpected header/shape):")
        for name, err in failed:
            print(f"     {name}: {err}")

    print(f"\nNext: copy {out_dir}/*.csv into market_data_feeds/ and run publish.py "
          f"(or the desktop app's ingest).")


if __name__ == "__main__":
    main()
