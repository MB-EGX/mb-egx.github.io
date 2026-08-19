"""
reconstruct_from_git_history.py
================================
fix_session_pick_dates.py reverse-engineers pick_date/achieved_date by
matching the stamped price against market_data - but that heuristic
turned out to be fooled by real price coincidences (EGX stocks re-trade
near old levels all the time): for TWSA/EAST/ABUK, the "achieved" price
matched a market_data close from well BEFORE the ticker's real (August)
pick_date, meaning the achievement itself is a false artifact of the
MM/DD-vs-DD/MM corruption window, not just a mis-dated real event.

No amount of price-matching tolerance fixes that - it can't distinguish
"a real price path that legitimately hit target" from "a coincidental
match near a corrupted date." What CAN settle it: your git history.
publish.py commits web_public/data/market_data.json on every run, so
the commit sequence itself is untouched by the DuckDB corruption -
regardless of what pick_date/achieved_date/status said internally that
day, the ORDER and TIMING of commits is real, external, tamper-proof
ground truth.

METHOD: walk every commit that touched the target file (oldest first).
For each ticker+horizon, the first commit where it appears (in the
short/medium/long active lists) is treated as the true pick date; the
first commit where it appears with status=achieved (in achieved_today /
achieved_history) is treated as the true achieved date. Every
(ticker, horizon) is then sanity-checked:

  - achieved_date before pick_date            -> impossible; almost
    certainly a false achievement from the corruption window.
  - achieved_date/pick_date AFTER the commit  -> impossible; the field
    that first reported it                      says something from the
                                                  future relative to when
                                                  it was actually published.

REPORT-ONLY: this script never writes to the database. It only prints
findings, grouped into "flagged" (something is logically inconsistent -
review these before touching anything) and "clean" (self-consistent -
no reason to suspect a false achievement). A separate --fix pass (not
this script) should be written afterward, once you've confirmed which
flagged picks really never hit target.

USAGE (run from the repo root, where .git/ and web_public/ live):
    python reconstruct_from_git_history.py
    python reconstruct_from_git_history.py --ticker TWSA EAST ABUK
    python reconstruct_from_git_history.py --file web_public/data/session_picks.json

NOTE: if you also have web_public/data/session_picks.json committed
(publish.py's export list includes it) and it carries a fuller per-pick
history than the "session_picks" block inside market_data.json, try
--file against that too - more fields per commit means a more precise
reconstruction. This script tolerates either shape.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import date


def run_git(*args) -> str:
    # encoding="utf-8" is required on Windows: subprocess with text=True
    # otherwise decodes using the console's default codepage (cp1252),
    # which raises UnicodeDecodeError the moment git's output contains a
    # UTF-8 multi-byte sequence - e.g. the emoji embedded in alerts.py's
    # own alert text, or any non-ASCII company name/note elsewhere in the
    # repo. The repo's actual content is UTF-8 (git's own default), so
    # decoding as UTF-8 here is correct regardless of the OS default.
    result = subprocess.run(["git", *args], capture_output=True, text=True,
                             encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def list_commits(path: str) -> list[tuple[str, str]]:
    """[(commit_hash, iso_commit_date), ...] oldest-first, for every
    commit that touched `path` on the current branch. --follow so
    history survives any past rename of the file."""
    out = run_git("log", "--follow", "--format=%H|%aI", "--", path)
    commits = []
    for line in out.strip().splitlines():
        if not line.strip():
            continue
        h, iso = line.split("|", 1)
        commits.append((h, iso))
    commits.reverse()
    return commits


def show_file_at_commit(commit_hash: str, path: str) -> dict | None:
    # Same UTF-8 fix as run_git() - see its comment. errors="replace"
    # means a genuinely corrupt byte becomes U+FFFD instead of crashing
    # the whole run; that's fine here since we only read known JSON
    # fields out of the result, we don't re-emit the raw text anywhere.
    result = subprocess.run(["git", "show", f"{commit_hash}:{path}"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace")
    if result.returncode != 0:
        # Most commonly: this path didn't exist yet at this commit
        # (the file was added partway through history) - not an error,
        # just "no snapshot to read here".
        return None
    if not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def extract_picks(data: dict) -> dict:
    """Normalizes either the full market_data.json shape (a top-level
    "session_picks" block) or a standalone session_picks.json (the same
    shape at the top level) into:

        {(ticker, horizon): {"status", "pick_date", "ref_price",
                              "achieved_date", "achieved_price",
                              "achieved_pct"}}

    Presence in short/medium/long implies status="active" as of this
    snapshot; presence in achieved_today/achieved_history overrides it
    to "achieved" - mirrors how export_json.py itself derives these
    lists from session_picks.status, so a ticker is never double-counted
    as both in the same snapshot.
    """
    sp = data.get("session_picks", data)
    out = {}

    for horizon in ("short", "medium", "long"):
        for row in sp.get(horizon, []) or []:
            ticker = row.get("ticker")
            if not ticker:
                continue
            out[(ticker, horizon)] = {
                "status": "active",
                "pick_date": row.get("pick_date"),
                "ref_price": row.get("ref_price"),
                "achieved_date": None,
                "achieved_price": None,
                "achieved_pct": None,
            }

    for row in (sp.get("achieved_today") or []) + (sp.get("achieved_history") or []):
        ticker = row.get("ticker")
        horizon = row.get("horizon", "?")
        if not ticker:
            continue
        entry = out.setdefault((ticker, horizon), {})
        entry["status"] = "achieved"
        entry["pick_date"] = row.get("pick_date", entry.get("pick_date"))
        entry["ref_price"] = row.get("ref_price", entry.get("ref_price"))
        entry["achieved_date"] = row.get("achieved_date")
        entry["achieved_price"] = row.get("achieved_price")
        entry["achieved_pct"] = row.get("achieved_pct")

    return out


def _as_date(s) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return None


def main():
    ap = argparse.ArgumentParser(
        description="Reconstruct true pick_date/achieved_date/status per ticker+horizon "
                     "from git commit history, and flag anything logically impossible - "
                     "the signature of a false achievement from the date-corruption bug."
    )
    ap.add_argument("--file", default="web_public/data/market_data.json",
                     help="Path (relative to repo root) of the committed JSON file to walk history for.")
    ap.add_argument("--ticker", nargs="*", default=None, help="Optional: restrict output to these ticker(s).")
    args = ap.parse_args()

    print(f"Walking git history of {args.file} ...")
    commits = list_commits(args.file)
    if not commits:
        raise SystemExit(f"No commits found touching {args.file} - wrong path, or not run from the repo root?")
    print(f"Found {len(commits)} commit(s) touching this file.\n")

    # timeline[(ticker, horizon)] = [(commit_date_iso, snapshot_dict), ...] in commit order
    timeline: dict[tuple[str, str], list[tuple[str, dict]]] = {}

    for commit_hash, commit_date in commits:
        data = show_file_at_commit(commit_hash, args.file)
        if data is None:
            continue
        for key, snap in extract_picks(data).items():
            if args.ticker and key[0] not in args.ticker:
                continue
            timeline.setdefault(key, []).append((commit_date, snap))

    print(f"Tracking {len(timeline)} distinct (ticker, horizon) pick(s) across history.\n")

    flagged, clean = [], []

    for (ticker, horizon), events in timeline.items():
        first_commit_date, first_snap = events[0]
        reconstructed_pick_date = _as_date(first_snap.get("pick_date")) or _as_date(first_commit_date)

        achieved_event = next((e for e in events if e[1].get("status") == "achieved"), None)

        record = {
            "ticker": ticker, "horizon": horizon,
            "pick_date": reconstructed_pick_date,
            "achieved_date": None,
            "achieved_price": None,
            "ref_price": first_snap.get("ref_price"),
            "first_seen_commit": first_commit_date,
            "achieved_seen_commit": None,
            "issues": [],
        }

        if achieved_event:
            ac_commit_date, ac_snap = achieved_event
            reconstructed_achieved_date = _as_date(ac_snap.get("achieved_date")) or _as_date(ac_commit_date)
            commit_ceiling = _as_date(ac_commit_date)

            record["achieved_date"] = reconstructed_achieved_date
            record["achieved_price"] = ac_snap.get("achieved_price")
            record["achieved_seen_commit"] = ac_commit_date

            if reconstructed_pick_date and reconstructed_achieved_date and reconstructed_achieved_date < reconstructed_pick_date:
                record["issues"].append(
                    f"achieved_date {reconstructed_achieved_date} is BEFORE pick_date {reconstructed_pick_date} "
                    f"- impossible. Almost certainly a FALSE achievement from the date-corruption window, "
                    f"not just a wrong date."
                )
            if reconstructed_achieved_date and commit_ceiling and reconstructed_achieved_date > commit_ceiling:
                record["issues"].append(
                    f"achieved_date {reconstructed_achieved_date} is AFTER the commit "
                    f"({commit_ceiling}) that first reported it - data from the future relative to publish time."
                )
            if reconstructed_pick_date and commit_ceiling and reconstructed_pick_date > commit_ceiling:
                record["issues"].append(
                    f"pick_date {reconstructed_pick_date} is AFTER the commit "
                    f"({commit_ceiling}) that first reported this pick - data from the future."
                )

        (flagged if record["issues"] else clean).append(record)

    print("=" * 90)
    print(f" ⚠️  Flagged (inconsistent / likely FALSE achievement): {len(flagged)}")
    print(f" ✅ Clean (self-consistent history)                    : {len(clean)}")
    print("=" * 90)

    if flagged:
        print("\nFLAGGED - review before touching the database:\n")
        for r in sorted(flagged, key=lambda r: r["ticker"]):
            print(f"  {r['ticker']:<10} {r['horizon']:<7} "
                  f"pick={r['pick_date']}  achieved={r['achieved_date']}  "
                  f"(achieved_price={r['achieved_price']}, ref_price={r['ref_price']})")
            for issue in r["issues"]:
                print(f"      - {issue}")
            print(f"      first seen: {r['first_seen_commit']}   achieved seen: {r['achieved_seen_commit']}")
            print()
    else:
        print("\nNo flagged picks - no other ticker shows this pattern right now.")

    print("This was a REPORT-ONLY run - nothing in the database was touched.")
    if flagged:
        print("Once you've confirmed which of the flagged picks above never really achieved")
        print("their target, tell me and I'll write the --fix pass: revert those rows to")
        print("status='active' and clear achieved_date/achieved_price/achieved_pct (and, where")
        print("this history gives a confident answer, correct pick_date too).")


if __name__ == "__main__":
    main()
