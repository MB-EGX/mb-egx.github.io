"""
data_repair_tools.py
====================
Consolidated maintenance/repair CLI for MB-EGX historical-data cleanup.

This replaces these root-level one-off repair scripts:
    - fix_future_dates.py
    - fix_session_pick_dates.py
    - reconstruct_from_git_history.py
    - revert_false_achievements.py
    - purge_false_leaderboard_hits.py
    - verify_purge.py
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import duckdb

DEFAULT_REVERT_TICKERS = ["ABUK.CA", "EAST.CA"]
DEFAULT_DELETE_TICKERS = ["TWSA.CA"]
DEFAULT_PURGE = [
    ("TWSA.CA", "2026-08-18"),
    ("ABUK.CA", "2026-12-05"),
    ("EAST.CA", "2026-12-05"),
]
FALSE_TICKERS = {"TWSA.CA", "ABUK.CA", "EAST.CA"}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _as_date(v) -> date | None:
    if v is None:
        return None
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def normalize_symbol(ticker: str) -> str:
    t = str(ticker).strip().upper()
    if not t.endswith(".CA") and len(t) <= 6 and "." not in t:
        return t + ".CA"
    return t


# ---------------------------------------------------------------------------
# future-dates  (from fix_future_dates.py)
# ---------------------------------------------------------------------------

def swap_day_month(d: date) -> date | None:
    try:
        return date(d.year, d.day, d.month)
    except ValueError:
        return None


def patch_csv_file(feeds_dir: str, ticker: str, bad_iso: str, fixed_iso: str) -> bool:
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


def cmd_future_dates(args) -> int:
    today = date.fromisoformat(args.today) if args.today else date.today()
    print(f"Treating any market_data row after {today.isoformat()} as impossible/corrupted.\n")

    con = duckdb.connect(args.db, read_only=not args.fix)
    rows = con.execute(
        "SELECT ticker, date FROM market_data WHERE date > ? ORDER BY date DESC, ticker;",
        [today.isoformat()],
    ).fetchall()

    if not rows:
        print("✅ No future-dated rows found. Nothing to fix.")
        con.close()
        return 0

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
        print(f"\n⚠️  {len(unfixable)} row(s) need manual review - open their source CSV in {args.feeds_dir} and check the Date column by hand.")

    if not args.fix:
        print("\nThis was a DRY RUN - nothing was changed. Re-run with --fix to apply the fixes above.")
        con.close()
        return 0

    print(f"\nApplying {len(fixable)} fix(es)...")
    for ticker, bad_date, swapped in fixable:
        exists = con.execute(
            "SELECT 1 FROM market_data WHERE ticker = ? AND date = ?;",
            [ticker, swapped.isoformat()],
        ).fetchone()
        if exists:
            print(f"  ⚠️  {ticker} already has a row on {swapped.isoformat()} - skipping DB update (would collide). Fix this one by hand.")
            continue
        con.execute(
            "UPDATE market_data SET date = ? WHERE ticker = ? AND date = ?;",
            [swapped.isoformat(), ticker, bad_date.isoformat()],
        )
        print(f"  ✅ {ticker}: {bad_date.isoformat()} -> {swapped.isoformat()} (database)")
        if not patch_csv_file(args.feeds_dir, ticker, bad_date.isoformat(), swapped.isoformat()):
            print(f"      ⚠️  couldn't find the source row in {args.feeds_dir} to patch - the database is fixed, but check the CSV by hand so a future full re-ingest doesn't bring the bad date back.")

    con.close()
    print("\nDone. Now re-run publish.py (or just Run Ingestion in the desktop app) to refresh the exported JSON.")
    return 0


# ---------------------------------------------------------------------------
# session-pick-dates  (from fix_session_pick_dates.py)
# ---------------------------------------------------------------------------

def find_price_matches(con, ticker: str, price: float, today: date, tolerance_pct: float) -> list[tuple[date, float]]:
    if price is None:
        return []
    rows = con.execute(
        "SELECT date, close FROM market_data WHERE ticker = ? AND date <= ? AND close IS NOT NULL;",
        [ticker, today.isoformat()],
    ).fetchall()
    tol = abs(price) * (tolerance_pct / 100.0)
    matches = []
    for d, close in rows:
        d = _as_date(d)
        if d is None or close is None:
            continue
        diff = abs(float(close) - float(price))
        if diff <= max(tol, 1e-9):
            matches.append((d, float(close), diff))
    matches.sort(key=lambda m: m[2])
    return [(d, c) for d, c, _ in matches]


def choose_candidate(matches: list[tuple[date, float]], anchor: date | None, anchor_is_after: bool) -> date | None:
    distinct_dates = sorted({d for d, _c in matches})
    if not distinct_dates:
        return None
    if anchor is None:
        return distinct_dates[0] if len(distinct_dates) == 1 else None
    if anchor_is_after:
        valid = [d for d in distinct_dates if d <= anchor]
    else:
        valid = [d for d in distinct_dates if d >= anchor]
    if not valid:
        return None
    valid.sort(key=lambda d: abs((anchor - d).days))
    return valid[0]


def cmd_session_pick_dates(args) -> int:
    today = date.fromisoformat(args.today) if args.today else date.today()
    print(f"Treating any session_picks pick_date/achieved_date after {today.isoformat()} as an impossible/corrupted stamp.\n")

    con = duckdb.connect(args.db, read_only=not args.fix)
    rows = con.execute(
        "SELECT id, ticker, horizon, pick_date, ref_price, status, achieved_date, achieved_price, achieved_pct "
        "FROM session_picks WHERE pick_date > ? OR achieved_date > ? ORDER BY id;",
        [today.isoformat(), today.isoformat()],
    ).fetchall()

    if not rows:
        print("✅ No corrupted pick_date/achieved_date stamps found. Nothing to fix.")
        con.close()
        return 0

    print(f"Found {len(rows)} row(s) with a future-dated stamp:\n")
    fixes = []
    needs_review = []

    for (pick_id, ticker, horizon, pick_date, ref_price, status, achieved_date, achieved_price, achieved_pct) in rows:
        pick_date = _as_date(pick_date)
        achieved_date = _as_date(achieved_date)
        norm_ticker = normalize_symbol(ticker)

        pick_bad = pick_date is not None and pick_date > today
        achieved_bad = achieved_date is not None and achieved_date > today

        print(f"  id={pick_id:<6} {ticker:<12} {horizon:<7} status={status}")
        new_pick_date = pick_date

        if pick_bad:
            matches = find_price_matches(con, norm_ticker, ref_price, today, args.tolerance_pct)
            anchor = achieved_date if (achieved_date is not None and not achieved_bad) else None
            candidate = choose_candidate(matches, anchor, anchor_is_after=True)
            if candidate:
                print(f"      pick_date     {pick_date}  ->  {candidate}  (matched ref_price {ref_price} in market_data)")
                new_pick_date = candidate
                fixes.append((pick_id, "pick_date", pick_date, candidate, ticker))
            else:
                distinct = sorted({d for d, _c in matches})
                print(f"      pick_date     {pick_date}  ->  NO confident match ({len(distinct)} candidate date(s): {[d.isoformat() for d in distinct] or 'none'}) - needs manual review")
                needs_review.append((pick_id, ticker, "pick_date", pick_date, ref_price, distinct))

        if achieved_bad:
            matches = find_price_matches(con, norm_ticker, achieved_price, today, args.tolerance_pct)
            anchor = new_pick_date if (new_pick_date is not None and new_pick_date <= today) else None
            candidate = choose_candidate(matches, anchor, anchor_is_after=False)
            if candidate:
                print(f"      achieved_date {achieved_date}  ->  {candidate}  (matched achieved_price {achieved_price} in market_data)")
                fixes.append((pick_id, "achieved_date", achieved_date, candidate, ticker))
            else:
                distinct = sorted({d for d, _c in matches})
                print(f"      achieved_date {achieved_date}  ->  NO confident match ({len(distinct)} candidate date(s): {[d.isoformat() for d in distinct] or 'none'}) - needs manual review")
                needs_review.append((pick_id, ticker, "achieved_date", achieved_date, achieved_price, distinct))
        print()

    print("=" * 78)
    print(f" ✅ Auto-resolvable fixes : {len(fixes)}")
    print(f" ⚠️  Needs manual review   : {len(needs_review)}")
    print("=" * 78)

    if needs_review:
        print("\nRows needing manual review (open the desktop app / query the DB directly):")
        for pick_id, ticker, col, old_val, price, candidates in needs_review:
            cand_str = ", ".join(d.isoformat() for d in candidates) if candidates else "(no price match at all in market_data - ticker may be missing history for that period)"
            print(f"   id={pick_id:<6} {ticker:<12} {col:<14} was {old_val}  price={price}  candidates: {cand_str}")

    if not args.fix:
        print("\nThis was a DRY RUN - nothing was changed. Re-run with --fix to apply the resolvable fixes above.")
        con.close()
        return 0

    print(f"\nApplying {len(fixes)} fix(es)...")
    for pick_id, column, old_val, new_val, ticker in fixes:
        con.execute(f"UPDATE session_picks SET {column} = ? WHERE id = ?;", [new_val.isoformat(), int(pick_id)])
        print(f"  ✅ id={pick_id} {ticker}: {column} {old_val} -> {new_val}")

    con.close()
    print("\nDone. Restart the desktop app (or just switch tabs / re-open Session Picks) to see the corrected dates.")
    if needs_review:
        print(f"\n⚠️  {len(needs_review)} row(s) above still need manual review - those were left untouched.")
    return 0


# ---------------------------------------------------------------------------
# reconstruct-git  (from reconstruct_from_git_history.py)
# ---------------------------------------------------------------------------

def run_git(*args) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def list_commits(path: str) -> list[tuple[str, str]]:
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
    result = subprocess.run(["git", "show", f"{commit_hash}:{path}"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def extract_picks(data: dict) -> dict:
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


def summarize_history(commits: list[tuple[str, str]], path: str, tickers: set[str] | None = None) -> tuple[list[dict], list[dict]]:
    seen = {}
    for commit_hash, commit_iso in commits:
        data = show_file_at_commit(commit_hash, path)
        if not data:
            continue
        commit_date = date.fromisoformat(commit_iso[:10])
        picks = extract_picks(data)
        for (ticker, horizon), row in picks.items():
            if tickers and ticker.upper() not in tickers:
                continue
            key = (ticker, horizon)
            entry = seen.setdefault(
                key,
                {
                    "ticker": ticker,
                    "horizon": horizon,
                    "first_seen_pick_commit": None,
                    "first_seen_pick_commit_date": None,
                    "first_seen_pick_date_field": None,
                    "first_seen_achieved_commit": None,
                    "first_seen_achieved_commit_date": None,
                    "first_seen_achieved_date_field": None,
                    "status_seen": set(),
                },
            )
            entry["status_seen"].add(row.get("status"))
            if row.get("pick_date") and entry["first_seen_pick_commit"] is None:
                entry["first_seen_pick_commit"] = commit_hash
                entry["first_seen_pick_commit_date"] = commit_date
                entry["first_seen_pick_date_field"] = row.get("pick_date")
            if row.get("status") == "achieved" and entry["first_seen_achieved_commit"] is None:
                entry["first_seen_achieved_commit"] = commit_hash
                entry["first_seen_achieved_commit_date"] = commit_date
                entry["first_seen_achieved_date_field"] = row.get("achieved_date")

    flagged, clean = [], []
    for entry in seen.values():
        pick_field = _as_date(entry["first_seen_pick_date_field"])
        achieved_field = _as_date(entry["first_seen_achieved_date_field"])
        pick_commit_date = entry["first_seen_pick_commit_date"]
        achieved_commit_date = entry["first_seen_achieved_commit_date"]
        issues = []
        if pick_field and pick_commit_date and pick_field > pick_commit_date:
            issues.append(f"pick_date field {pick_field} is AFTER the first commit that reported it ({pick_commit_date})")
        if achieved_field and achieved_commit_date and achieved_field > achieved_commit_date:
            issues.append(f"achieved_date field {achieved_field} is AFTER the first commit that reported it ({achieved_commit_date})")
        if pick_field and achieved_field and achieved_field < pick_field:
            issues.append(f"achieved_date field {achieved_field} is BEFORE pick_date field {pick_field}")

        item = {
            **entry,
            "status_seen": sorted(s for s in entry["status_seen"] if s),
            "issues": issues,
        }
        if issues:
            flagged.append(item)
        else:
            clean.append(item)
    flagged.sort(key=lambda x: (x["ticker"], x["horizon"]))
    clean.sort(key=lambda x: (x["ticker"], x["horizon"]))
    return flagged, clean


def cmd_reconstruct_git(args) -> int:
    tickers = {t.upper() for t in (args.ticker or [])} or None
    commits = list_commits(args.file)
    print(f"Scanned {len(commits)} commit(s) touching {args.file}.\n")
    flagged, clean = summarize_history(commits, args.file, tickers=tickers)

    print("=== FLAGGED (logically inconsistent / likely corruption artifacts) ===")
    if not flagged:
        print("  none")
    for item in flagged:
        print(f"\n- {item['ticker']} ({item['horizon']})")
        print(f"    first pick commit     : {item['first_seen_pick_commit_date']}  {item['first_seen_pick_commit']}")
        print(f"    pick_date field       : {item['first_seen_pick_date_field']}")
        print(f"    first achieved commit : {item['first_seen_achieved_commit_date']}  {item['first_seen_achieved_commit']}")
        print(f"    achieved_date field   : {item['first_seen_achieved_date_field']}")
        for issue in item["issues"]:
            print(f"    ⚠ {issue}")

    print("\n=== CLEAN (history is self-consistent) ===")
    if not clean:
        print("  none")
    for item in clean:
        print(f"  - {item['ticker']} ({item['horizon']}): pick={item['first_seen_pick_date_field']} achieved={item['first_seen_achieved_date_field']}")

    print("\nThis was a REPORT-ONLY run - nothing in the database was touched.")
    if flagged:
        print("Once you've confirmed which flagged picks never really achieved target,")
        print("use the revert-achievements / purge-leaderboard subcommands here.")
    return 0


# ---------------------------------------------------------------------------
# revert-achievements  (from revert_false_achievements.py)
# ---------------------------------------------------------------------------

def parse_pick_date_overrides(raw: list[str] | None) -> dict[str, date]:
    overrides = {}
    for item in raw or []:
        if "=" not in item:
            raise SystemExit(f"--set-pick-date expects TICKER=YYYY-MM-DD, got: {item}")
        ticker, iso = item.split("=", 1)
        overrides[ticker.strip().upper()] = date.fromisoformat(iso.strip())
    return overrides


def cmd_revert_achievements(args) -> int:
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
        "SELECT id, ticker, horizon, pick_date, ref_price, status, achieved_date, achieved_price, achieved_pct "
        "FROM session_picks WHERE upper(ticker) IN ({}) ORDER BY ticker, horizon;".format(
            ",".join("?" for _ in all_wanted)
        ),
        list(all_wanted),
    ).fetchall()

    if not rows:
        print(f"No session_picks rows found for: {', '.join(sorted(all_wanted))}")
        con.close()
        return 0

    print(f"Found {len(rows)} row(s) matching the requested ticker(s):\n")
    to_revert = []
    to_delete = []
    for (pick_id, ticker, horizon, pick_date, ref_price, status, achieved_date, achieved_price, achieved_pct) in rows:
        norm_ticker = str(ticker).strip().upper()
        print(f"  id={pick_id:<6} {ticker:<10} {horizon:<7} status={status}")

        if norm_ticker in delete_wanted:
            print("      -> DELETE (never a real pick - entire row removed)")
            print(f"         was: pick_date={pick_date}  achieved_date={achieved_date}  achieved_price={achieved_price}")
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
        return 0

    print("Applying...")
    for pick_id in to_delete:
        con.execute("DELETE FROM session_picks WHERE id = ?;", [int(pick_id)])
        print(f"  ✅ id={pick_id} deleted")

    for pick_id, new_pick_date, was_achieved in to_revert:
        if was_achieved:
            con.execute(
                "UPDATE session_picks SET status = 'active', achieved_date = NULL, achieved_price = NULL, achieved_pct = NULL, pick_date = ? WHERE id = ?;",
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
    print("Next publish.py run will push the corrected session_picks state to the public site.")
    return 0


# ---------------------------------------------------------------------------
# purge-leaderboard  (from purge_false_leaderboard_hits.py)
# ---------------------------------------------------------------------------

def cmd_purge_leaderboard(args) -> int:
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
    found_rows = []
    for ticker, achieved_date in wanted:
        if achieved_date:
            row = con.execute(
                "SELECT ticker, hits, total_return_pct, last_achieved_date FROM leaderboard WHERE upper(ticker) = ? AND last_achieved_date = ?;",
                [ticker, achieved_date],
            ).fetchone()
        else:
            rows = con.execute(
                "SELECT ticker, hits, total_return_pct, last_achieved_date FROM leaderboard WHERE upper(ticker) = ?;",
                [ticker],
            ).fetchall()
            row = rows[0] if rows else None

        if not row:
            print(f"  -- {ticker:<10} (target_date={achieved_date}): NOT FOUND - nothing to remove.")
            continue

        avg_pct = round(float(row[2]) / max(int(row[1]), 1), 2)
        print(f"  -- {row[0]:<10} hits={row[1]}  total_return_pct={row[2]}  avg_return_pct={avg_pct}  last_achieved_date={row[3]}")
        print(f"       -> DELETE  (key={ticker}, target_date={achieved_date})")
        found_rows.append((ticker, achieved_date, row[3]))

    if not found_rows:
        print("\nNothing to do - no matching leaderboard rows found.")
        con.close()
        return 0

    if not args.fix:
        print("\nThis was a DRY RUN - no rows were deleted. Re-run with --fix to apply the deletes above.")
        con.close()
        return 0

    print("\nApplying deletes...")
    for ticker, target_date, _ in found_rows:
        if target_date:
            deleted = con.execute(
                "DELETE FROM leaderboard WHERE upper(ticker) = ? AND last_achieved_date = ? RETURNING ticker;",
                [ticker, target_date],
            ).fetchall()
        else:
            deleted = con.execute("DELETE FROM leaderboard WHERE upper(ticker) = ? RETURNING ticker;", [ticker]).fetchall()
        for d in deleted:
            print(f"  ✅ {d[0]} removed from leaderboard")

    con.close()
    print("\nDone. Re-run publish.py to regenerate web_public/data/market_data.json.")
    return 0


# ---------------------------------------------------------------------------
# verify-purge  (from verify_purge.py)
# ---------------------------------------------------------------------------

def check_db(db_path: Path) -> list[str]:
    if not db_path.exists():
        print(f"  ⚠️  {db_path} not found - skipping DB check")
        return []
    con = duckdb.connect(str(db_path), read_only=True)
    rows = con.execute("SELECT ticker FROM leaderboard ORDER BY hits DESC, total_return_pct DESC").fetchall()
    con.close()
    return [r[0] for r in rows]


def check_json(json_path: Path) -> list[str]:
    if not json_path.exists():
        print(f"  ⚠️  {json_path} not found - skipping JSON check")
        return []
    with open(json_path, encoding="utf-8") as f:
        payload = json.load(f)
    return [r["ticker"] for r in payload.get("leaderboard", [])]


def cmd_verify_purge(args) -> int:
    repo_root = Path(args.repo_root).resolve()
    db_path = repo_root / args.db
    json_path = repo_root / args.json

    print(f"Repo root : {repo_root}")
    print(f"DB        : {db_path}")
    print(f"JSON      : {json_path}\n")

    db_tickers = check_db(db_path)
    json_tickers = check_json(json_path)

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
            print("   -> python data_repair_tools.py purge-leaderboard --fix")
        if json_residual:
            print(f"   market_data.json still has: {json_residual}")
            print("   -> python publish.py")
        return 1

    if db_tickers != json_tickers:
        print("⚠️  DuckDB and JSON disagree - re-run publish.py so the export catches up")
        print(f"   DB   : {db_tickers}")
        print(f"   JSON : {json_tickers}")
        return 1

    print("✅ Clean.")
    for t in (db_tickers or json_tickers):
        print(f"   - {t}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Consolidated MB-EGX maintenance/repair tools.")
    sub = parser.add_subparsers(dest="command", required=True)

    p1 = sub.add_parser("future-dates", help="Find/fix impossible future dates in market_data.")
    p1.add_argument("--db", default="quant_master.duckdb")
    p1.add_argument("--feeds-dir", default="market_data_feeds")
    p1.add_argument("--today", default=None)
    p1.add_argument("--fix", action="store_true")

    p2 = sub.add_parser("session-pick-dates", help="Fix corrupted session_picks dates.")
    p2.add_argument("--db", default="quant_master.duckdb")
    p2.add_argument("--today", default=None)
    p2.add_argument("--tolerance-pct", type=float, default=0.05)
    p2.add_argument("--fix", action="store_true")

    p3 = sub.add_parser("reconstruct-git", help="Reconstruct pick/achievement timing from git history.")
    p3.add_argument("--ticker", action="append", default=None)
    p3.add_argument("--file", default="web_public/data/market_data.json")

    p4 = sub.add_parser("revert-achievements", help="Revert/delete confirmed false session_picks rows.")
    p4.add_argument("--db", default="quant_master.duckdb")
    p4.add_argument("--revert", nargs="*", default=DEFAULT_REVERT_TICKERS)
    p4.add_argument("--delete", nargs="*", default=DEFAULT_DELETE_TICKERS)
    p4.add_argument("--set-pick-date", nargs="*", default=None)
    p4.add_argument("--fix", action="store_true")

    p5 = sub.add_parser("purge-leaderboard", help="Purge confirmed-false leaderboard rows.")
    p5.add_argument("--db", default="quant_master.duckdb")
    p5.add_argument("--ticker", action="append", default=None)
    p5.add_argument("--last-achieved-date", default=None)
    p5.add_argument("--fix", action="store_true")

    p6 = sub.add_parser("verify-purge", help="Verify cleanup in both DB and exported JSON.")
    p6.add_argument("--repo-root", default=".")
    p6.add_argument("--db", default="quant_master.duckdb")
    p6.add_argument("--json", default="web_public/data/market_data.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "future-dates":
        return cmd_future_dates(args)
    if args.command == "session-pick-dates":
        return cmd_session_pick_dates(args)
    if args.command == "reconstruct-git":
        return cmd_reconstruct_git(args)
    if args.command == "revert-achievements":
        return cmd_revert_achievements(args)
    if args.command == "purge-leaderboard":
        return cmd_purge_leaderboard(args)
    if args.command == "verify-purge":
        return cmd_verify_purge(args)
    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
