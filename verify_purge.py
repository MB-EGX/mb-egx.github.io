"""
verify_purge.py
===============
Tiny diagnostic that confirms the TWSA/ABUK/EAST cleanup actually
landed in BOTH the DuckDB side AND the public web_public/data/
market_data.json export. Use this after running:

    python fix_future_dates.py --fix
    python revert_false_achievements.py --fix
    python purge_false_leaderboard_hits.py --fix
    python publish.py
    git add -A && git commit -m "..." && git push

Prints the actual leaderboard arrays pulled from BOTH sources and
flags any of TWSA/ABUK/EAST that are still there. Exits 0 on clean,
1 on residual issue. Encoding-safe on Windows cp1252 / Arabic strings.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

FALSE_TICKERS = {"TWSA.CA", "ABUK.CA", "EAST.CA"}

REPO_ROOT = Path(__file__).resolve().parent
DB_PATH = REPO_ROOT / "quant_master.duckdb"
JSON_PATH = REPO_ROOT / "web_public" / "data" / "market_data.json"


def check_db() -> list[str]:
    try:
        import duckdb
    except ImportError:
        print("  ⚠️  duckdb not installed - skipping DB check (pip install duckdb)")
        return []
    if not DB_PATH.exists():
        print(f"  ⚠️  {DB_PATH} not found - skipping DB check")
        return []
    con = duckdb.connect(str(DB_PATH), read_only=True)
    rows = con.execute(
        "SELECT ticker FROM leaderboard "
        "ORDER BY hits DESC, total_return_pct DESC"
    ).fetchall()
    con.close()
    return [r[0] for r in rows]


def check_json() -> list[str]:
    if not JSON_PATH.exists():
        print(f"  ⚠️  {JSON_PATH} not found - skipping JSON check")
        return []
    with open(JSON_PATH, encoding="utf-8") as f:
        payload = json.load(f)
    return [r["ticker"] for r in payload.get("leaderboard", [])]


def main() -> int:
    print(f"Repo root : {REPO_ROOT}")
    print(f"DB        : {DB_PATH}")
    print(f"JSON      : {JSON_PATH}")
    print()

    db_tickers = check_db()
    json_tickers = check_json()

    print("=== Leaderboard in DuckDB (source of truth) ===")
    if db_tickers:
        for t in db_tickers:
            flag = "  <- FALSE (should be gone)" if t in FALSE_TICKERS else ""
            print(f"  - {t}{flag}")
    else:
        print("  (empty)")
    print()

    print("=== Leaderboard in web_public/data/market_data.json (what the website actually reads) ===")
    if json_tickers:
        for t in json_tickers:
            flag = "  <- FALSE (should be gone)" if t in FALSE_TICKERS else ""
            print(f"  - {t}{flag}")
    else:
        print("  (empty)")
    print()

    db_residual = [t for t in db_tickers if t in FALSE_TICKERS]
    json_residual = [t for t in json_tickers if t in FALSE_TICKERS]

    if db_residual or json_residual:
        print("❌ RESIDUAL FALSE HITS FOUND")
        if db_residual:
            print(f"   DuckDB still has: {db_residual}")
            print(f"   -> python purge_false_leaderboard_hits.py --fix")
        if json_residual:
            print(f"   market_data.json still has: {json_residual}")
            print(f"   -> python publish.py            # regenerate the export")
        return 1

    if db_tickers != json_tickers:
        print("⚠️  DuckDB and JSON disagree - re-run publish.py so the export catches up")
        print(f"   DB   : {db_tickers}")
        print(f"   JSON : {json_tickers}")
        return 1

    print("✅ Clean. Leaderboard contains:")
    for t in (db_tickers or json_tickers):
        print(f"   - {t}")
    print()
    print("If you already pushed to git and the LIVE site still shows the false rows:")
    print("  - Hard-refresh with Ctrl+Shift+R (bypasses cache).")
    print("  - Wait 1-2 minutes for GitHub Pages CDN to rotate.")
    print("  - Open an incognito window to be 100% sure.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
