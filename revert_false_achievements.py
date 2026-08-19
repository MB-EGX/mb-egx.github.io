"""
revert_false_achievements.py
=============================
reconstruct_from_git_history.py confirmed 3 session_picks rows whose
"achieved" status is a false artifact of the MM/DD-vs-DD/MM date-
corruption window, not a real target-crossing event:

    ABUK.CA (short)   pick_date 2026-08-13 (sane) / achieved_date 2026-12-05 (impossible)
    EAST.CA (medium)  pick_date 2026-08-13 (sane) / achieved_date 2026-12-05 (impossible)
    TWSA.CA (medium)  pick_date 2026-12-05 (itself corrupted) / achieved_date 2026-08-18

ABUK/EAST were REAL picks - only their achieved_date is bogus, so they
get REVERTED back to status='active' with achieved_* cleared.

TWSA is different: the user confirms it was never actually picked at
all - the corruption window fabricated the entire row (pick_date AND
the "achievement" both), not just mis-dated an event that really
happened. Reverting it to 'active' would be wrong too - it would show
up as a real live pick you never made. TWSA is DELETED outright.

Anything already pushed as a Telegram "pick achieved" alert for these
can't be unsent - alerts.py has no delete/edit hook - so treat those
as known false positives if anyone asks.

USAGE:
    python revert_false_achievements.py                          # dry run, default set below
    python revert_false_achievements.py --fix                    # apply
    python revert_false_achievements.py --fix --revert ABUK.CA EAST.CA --delete TWSA.CA
    python revert_false_achievements.py --fix --revert ABUK.CA EAST.CA --delete TWSA.CA \\
        --set-pick-date ABUK.CA=2026-08-13
"""
from __future__ import annotations

import argparse
from datetime import date

import duckdb

DEFAULT_REVERT_TICKERS = ["ABUK.CA", "EAST.CA"]
DEFAULT_DELETE_TICKERS = ["TWSA.CA"]


def parse_pick_date_overrides(raw: list[str] | None) -> dict[str, date]:
    """--set-pick-date TICKER=YYYY-MM-DD, one or more. Ticker is matched
    case-insensitively against session_picks.ticker."""
    overrides = {}
    for item in raw or []:
        if "=" not in item:
            raise SystemExit(f"--set-pick-date expects TICKER=YYYY-MM-DD, got: {item}")
        ticker, iso = item.split("=", 1)
        overrides[ticker.strip().upper()] = date.fromisoformat(iso.strip())
    return overrides


def main():
    ap = argparse.ArgumentParser(description="Revert or delete confirmed false session_picks rows.")
    ap.add_argument("--db", default="quant_master.duckdb")
    ap.add_argument("--revert", nargs="*", default=DEFAULT_REVERT_TICKERS,
                     help=f"Ticker(s) that were REAL picks with a false achievement - revert to active "
                          f"(default: {' '.join(DEFAULT_REVERT_TICKERS)}).")
    ap.add_argument("--delete", nargs="*", default=DEFAULT_DELETE_TICKERS,
                     help=f"Ticker(s) that were NEVER real picks - delete the row entirely "
                          f"(default: {' '.join(DEFAULT_DELETE_TICKERS)}).")
    ap.add_argument("--set-pick-date", nargs="*", default=None,
                     help="Optional TICKER=YYYY-MM-DD override(s) for pick_date on --revert tickers, "
                          "e.g. ABUK.CA=2026-08-13. Ignored for --delete tickers.")
    ap.add_argument("--fix", action="store_true", help="Apply changes. Without this flag, only reports what would change.")
    args = ap.parse_args()

    pick_date_overrides = parse_pick_date_overrides(args.set_pick_date)
    revert_wanted = {t.strip().upper() for t in args.revert}
    delete_wanted = {t.strip().upper() for t in args.delete}
    overlap = revert_wanted & delete_wanted
    if overlap:
        raise SystemExit(f"Ticker(s) can't be in both --revert and --delete: {', '.join(sorted(overlap))}")
    all_wanted = revert_wanted | delete_wanted
    if not all_wanted:
        raise SystemExit("Nothing to do - both --revert and --delete are empty.")

    con = duckdb.connect(args.db, read_only=not args.fix)
    rows = con.execute(
        "SELECT id, ticker, horizon, pick_date, ref_price, status, "
        "       achieved_date, achieved_price, achieved_pct "
        "FROM session_picks WHERE upper(ticker) IN ({}) ORDER BY ticker, horizon;".format(
            ",".join("?" for _ in all_wanted)
        ),
        list(all_wanted),
    ).fetchall()

    if not rows:
        print(f"No session_picks rows found for: {', '.join(sorted(all_wanted))}")
        return

    print(f"Found {len(rows)} row(s) matching the requested ticker(s):\n")

    to_revert = []
    to_delete = []
    for (pick_id, ticker, horizon, pick_date, ref_price, status,
         achieved_date, achieved_price, achieved_pct) in rows:

        norm_ticker = str(ticker).strip().upper()
        print(f"  id={pick_id:<6} {ticker:<10} {horizon:<7} status={status}")

        if norm_ticker in delete_wanted:
            print(f"      -> DELETE (never a real pick - entire row removed)")
            print(f"         was: pick_date={pick_date}  achieved_date={achieved_date}  "
                  f"achieved_price={achieved_price}")
            to_delete.append(pick_id)
            print()
            continue

        was_achieved = status == "achieved" or achieved_date is not None
        override = pick_date_overrides.get(norm_ticker)
        new_pick_date = override if override else pick_date

        print(f"      pick_date      {pick_date}" + (f"  -> {new_pick_date}  (override)" if override else "  (unchanged)"))
        if was_achieved:
            print(f"      status         {status}  -> active")
            print(f"      achieved_date  {achieved_date}  -> NULL")
            print(f"      achieved_price {achieved_price}  -> NULL")
            print(f"      achieved_pct   {achieved_pct}  -> NULL")
        else:
            print(f"      status         {status}  (already active - nothing to revert here)")
        print()

        to_revert.append((pick_id, new_pick_date, was_achieved))

    if not args.fix:
        print("This was a DRY RUN - nothing was changed. Re-run with --fix to apply the changes above.")
        con.close()
        return

    print("Applying...")
    for pick_id in to_delete:
        con.execute("DELETE FROM session_picks WHERE id = ?;", [int(pick_id)])
        print(f"  ✅ id={pick_id} deleted")

    for pick_id, new_pick_date, was_achieved in to_revert:
        if was_achieved:
            con.execute(
                "UPDATE session_picks SET status = 'active', achieved_date = NULL, "
                "achieved_price = NULL, achieved_pct = NULL, pick_date = ? WHERE id = ?;",
                [new_pick_date.isoformat() if isinstance(new_pick_date, date) else new_pick_date, int(pick_id)],
            )
        else:
            con.execute(
                "UPDATE session_picks SET pick_date = ? WHERE id = ?;",
                [new_pick_date.isoformat() if isinstance(new_pick_date, date) else new_pick_date, int(pick_id)],
            )
        print(f"  ✅ id={pick_id} updated")

    con.close()
    print("\nDone. Restart the desktop app (or switch tabs / re-open Session Picks) to see the change.")
    print("Next publish.py run will push the corrected session_picks state to the public site -")
    print("the false achievement disappears from Track Record, and the deleted TWSA row disappears")
    print("from the active list entirely.")


if __name__ == "__main__":
    main()
