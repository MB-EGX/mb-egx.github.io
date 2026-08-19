"""
purge_false_leaderboard_hits.py
================================
removes the three false leaderboard entries that revert_false_achievements.py
uncovered but couldn't touch (it only edits 'session_picks' — the leaderboard
table is a SEPARATE write path called from session_picks.py:674 via
db_manager.record_leaderboard_hit()).

THE OFFENDING ROWS (verified against the live market_data.json export):
    TWSA.CA  hits=1  avg_return_pct=29.01  last_achieved_date=2026-08-18
    ABUK.CA  hits=1  avg_return_pct=11.93  last_achieved_date=2026-12-05
    EAST.CA  hits=1  avg_return_pct=11.69  last_achieved_date=2026-12-05

These exact percentages (29.01 / 11.93 / 11.69) are what was visible in the
web app's Leaderboard tab. They correspond to the same false artifacts the
MM/DD-vs-DD/MM date-corruption window fabricated:

    TWSA - entire row fabricated by the corruption (pick_date=2026-12-05
          window). Was never a real pick. revert_false_achievements.py
          DELETED the session_picks row; this script removes the matching
          leaderboard hit.
    ABUK - real pick with a false achievement. revert_false_achievements.py
          REVERTED session_picks back to active; this removes the matching
          leaderboard hit.
    EAST - same as ABUK.

WHY THIS NEEDS ITS OWN SCRIPT INSTEAD OF ROLLED INTO
revert_false_achievements.py:
    That script's CLI is already wired around session_picks columns
    (achieved_date / achieved_price / achieved_pct / pick_date). Adding
    a second table that uses totally different keys (hits / total_return_pct
    / last_achieved_date) would muddy its semantics - it's "fix the false
    achievements" not "fix every side effect of the false achievements."
    Keeping the two scripts separate means: when a future debugger finds a
    session_picks discrepancy, reverting via revert_false_achievements.py
    is still a one-flag operation. The leaderboard cleanup is opt-in and
    obvious by name.

FUTURE PROTECTION: the future-session_date guard already in
session_picks.py (lines 593-621) returns early and skips BOTH
mark_pick_achieved AND record_leaderboard_hit when session_date > today,
so these rows cannot regenerate from a new run. This script just clears
the historical residue.

USAGE:
    python purge_false_leaderboard_hits.py                    # dry run
    python purge_false_leaderboard_hits.py --fix              # apply
    python purge_false_leaderboard_hits.py --fix \\
        --ticker TWSA.CA --ticker ABUK.CA --ticker EAST.CA    # custom set
"""
from __future__ import annotations

import argparse
from datetime import date

import duckdb

# Default set = the 3 known false hits from the MM/DD-vs-DD/MM window.
# last_achieved_date is included as a SAFETY check (paranoid double-key)
# so a future genuine achievement on the same ticker won't be wiped by
# a re-run of this script.
DEFAULT_PURGE = [
    ("TWSA.CA", "2026-08-18"),
    ("ABUK.CA", "2026-12-05"),
    ("EAST.CA", "2026-12-05"),
]


def main():
    ap = argparse.ArgumentParser(
        description="Purge confirmed-false leaderboard rows (companion to revert_false_achievements.py)."
    )
    ap.add_argument("--db", default="quant_master.duckdb",
                    help="Path to the DuckDB file (default: quant_master.duckdb).")
    ap.add_argument("--ticker", action="append", default=None,
                    help="Ticker to purge (repeatable). If omitted, uses the default set "
                         "and matches by both ticker AND last_achieved_date.")
    ap.add_argument("--last-achieved-date", default=None,
                    help="If set, only purge rows whose last_achieved_date equals this YYYY-MM-DD. "
                         "Safer for a ticker that may have a real future hit you don't want to wipe.")
    ap.add_argument("--fix", action="store_true",
                    help="Apply the deletes. Without this flag, prints what WOULD be deleted and exits.")
    args = ap.parse_args()

    # If user passed --ticker without --last-achieved-date, key on ticker alone
    # (explicit user intent: "remove ALL leaderboard rows for X"). If they
    # passed neither, key on (ticker, last_achieved_date) for the defaults —
    # exactly the safe mode documented above.
    if args.ticker:
        wanted = [(t.strip().upper(), args.last_achieved_date) for t in args.ticker]
    else:
        wanted = DEFAULT_PURGE
        if args.last_achieved_date:
            wanted = [(t, args.last_achieved_date) for t, _ in wanted]

    print(f"Target DuckDB: {args.db}")
    print(f"Mode: {'APPLY (--fix)' if args.fix else 'DRY RUN (no writes)'}")
    print()

    con = duckdb.connect(args.db, read_only=not args.fix)

    # Build the WHERE clause safely - we already validated arg shape above,
    # but the per-row check inside the loop uses parameterized queries so
    # there's no SQL injection surface from --ticker either.
    found_rows = []
    for ticker, achieved_date in wanted:
        if achieved_date:
            row = con.execute(
                "SELECT ticker, hits, total_return_pct, last_achieved_date "
                "FROM leaderboard WHERE upper(ticker) = ? AND last_achieved_date = ?;",
                [ticker, achieved_date],
            ).fetchone()
        else:
            rows = con.execute(
                "SELECT ticker, hits, total_return_pct, last_achieved_date "
                "FROM leaderboard WHERE upper(ticker) = ?;",
                [ticker],
            ).fetchall()
            row = rows[0] if rows else None
            extra = rows[1:] if rows and len(rows) > 1 else []

        if not row:
            print(f"  -- {ticker:<10} (target_date={achieved_date}): NOT FOUND - nothing to remove.")
            continue

        avg_pct = round(float(row[2]) / max(int(row[1]), 1), 2)
        print(f"  -- {row[0]:<10} hits={row[1]}  total_return_pct={row[2]}  "
              f"avg_return_pct={avg_pct}  last_achieved_date={row[3]}")
        print(f"       -> DELETE  (key={ticker}, target_date={achieved_date})")
        found_rows.append((ticker, achieved_date, row[3]))

    if not found_rows:
        print("\nNothing to do - no matching leaderboard rows found.")
        con.close()
        return

    if not args.fix:
        print("\nThis was a DRY RUN - no rows were deleted.")
        print("Re-run with --fix to apply the deletes above.")
        con.close()
        return

    print("\nApplying deletes...")
    for ticker, target_date, _ in found_rows:
        if target_date:
            deleted = con.execute(
                "DELETE FROM leaderboard WHERE upper(ticker) = ? AND last_achieved_date = ? "
                "RETURNING ticker;",
                [ticker, target_date],
            ).fetchall()
        else:
            deleted = con.execute(
                "DELETE FROM leaderboard WHERE upper(ticker) = ? RETURNING ticker;",
                [ticker],
            ).fetchall()
        for d in deleted:
            print(f"  ✅ {d[0]} removed from leaderboard")

    con.close()
    print("\nDone. Re-run publish.py to regenerate web_public/data/market_data.json "
          "(or, if you only need the leaderboard section, run export_json.py alone - "
          "it reads the leaderboard via dbm.get_leaderboard()).")
    print("Then push, and the web app's Leaderboard tab will no longer show those 3 "
          "false entries. Hard-refresh (Ctrl+Shift+R) to bypass the browser cache.")


if __name__ == "__main__":
    main()
