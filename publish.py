"""
publish.py — one-command refresh for the whole pipeline.

Run this after ANY change: new market data, an engine/calculation
fix, or a web_public/index.html layout tweak. It always does the
same 3 things, so you never have to think about which command to use:

    python publish.py

1. Ingest any new/changed files in market_data_feeds/.
2. Recompute the decision matrix and regenerate
   web_public/data/market_data.json.
3. Commit + push EVERYTHING that changed in the repo (data, engine
   code, index.html, whatever) so the live site + your GitHub
   history always reflect exactly what's on this machine.

Run this from the repo root (same folder as export_json.py / config.py).
"""
import json
import os
import shutil
import subprocess
import sys
from urllib import request as _urlrequest

# MUST be imported before ingestion/export_json (and, transitively,
# analytics/decision_matrix) - config.py sets the OPENBLAS_NUM_THREADS /
# MKL_NUM_THREADS / OMP_NUM_THREADS / NUMEXPR_NUM_THREADS env vars as a
# module-level side effect, and those only take effect if set BEFORE
# numpy/pandas are imported anywhere in this process. Importing it here
# first also means any ProcessPoolExecutor spawned later (Windows uses
# 'spawn') inherits the already-capped environment for its child
# processes, regardless of each module's own internal import order.
import config  # noqa: F401
from config import CDN_PURGE_URL

from ingestion import IngestionPipeline
from export_json import export_market_matrix
from db_manager import DatabaseLockedError


def run_git(*args):
    result = subprocess.run(["git", *args], capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout, result.stderr, file=sys.stderr)
        raise SystemExit(f"git {' '.join(args)} failed")
    return result.stdout.strip()


def sync_with_remote_before_push():
    """The daily-instagram-post.yml GitHub Action commits rendered social
    card PNGs straight to main every time it runs. If that happened since
    the last time this script ran, a plain `git push` gets rejected
    (non-fast-forward) even though nothing is actually wrong — Git is just
    protecting those bot commits from being overwritten.

    Fix: rebase our just-made local commit on top of whatever's on the
    remote before pushing, so both sets of changes end up in history
    cleanly with no manual `git pull` ever required. --autostash is a
    no-op safety net here (our tree is already clean from the commit
    above) but costs nothing to include.
    """
    result = subprocess.run(
        ["git", "pull", "--rebase", "--autostash"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(result.stdout, result.stderr, file=sys.stderr)
        # Leave the repo in a clean state rather than a half-finished
        # rebase — safer to abort and surface the problem than to guess.
        subprocess.run(["git", "rebase", "--abort"], capture_output=True, text=True)
        raise SystemExit(
            "git pull --rebase failed — this looks like a REAL conflict "
            "with remote changes (not just the usual bot commits, which "
            "sync automatically). The rebase was aborted so your local "
            "repo is back to a clean state. Open a terminal in this "
            "folder and run 'git pull --rebase' yourself to see and "
            "resolve the conflict, then re-run publish.py."
        )


def validate_export_files():
    base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_public", "data")
    required = [
        "market_data.json",
        "matrix.json",
        "sectors.json",
        "top_10.json",
        "session_picks.json",
        "chart_history.json",
        "ticker_sectors.json",
        "cache_manifest.json",
    ]
    missing = [name for name in required if not os.path.exists(os.path.join(base_dir, name))]
    if missing:
        raise SystemExit(f"export validation failed; missing shards: {missing}")
    for name in required:
        with open(os.path.join(base_dir, name), "r", encoding="utf-8") as fh:
            json.load(fh)


def backup_export_dir():
    """W5: snapshot web_public/data BEFORE export_market_matrix() overwrites
    it, so a bad export (crash mid-write, invalid JSON, a schema change
    that breaks validate_export_files()) can be rolled back instead of
    leaving the live site on half-written/broken data until someone
    notices and fixes it by hand. Returns the backup dir, or None if
    there was nothing to back up yet (first-ever run)."""
    base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_public", "data")
    if not os.path.isdir(base_dir):
        return None
    backup_dir = base_dir + ".backup"
    if os.path.isdir(backup_dir):
        shutil.rmtree(backup_dir)
    shutil.copytree(base_dir, backup_dir)
    return backup_dir


def restore_export_dir(backup_dir):
    """Counterpart to backup_export_dir() — used when export_market_matrix()
    or validate_export_files() fails, so the working tree (and therefore
    `git add -A` below) never sees the broken output at all."""
    base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_public", "data")
    if not backup_dir or not os.path.isdir(backup_dir):
        return
    if os.path.isdir(base_dir):
        shutil.rmtree(base_dir)
    shutil.move(backup_dir, base_dir)


def purge_cdn_if_configured():
    if not CDN_PURGE_URL:
        return
    req = _urlrequest.Request(CDN_PURGE_URL, data=b"{}", method="POST", headers={"Content-Type": "application/json"})
    try:
        with _urlrequest.urlopen(req, timeout=20) as resp:
            print(f"      CDN purge triggered ({resp.status}).")
    except Exception as e:
        print(f"      CDN purge skipped/failed: {e}")


def main():
    print("1/3  Ingesting new spreadsheet/CSV feeds...")
    IngestionPipeline().run_incremental_ingestion(
        progress_callback=lambda pct, msg: print(f"      [{pct:>3}%] {msg}")
    )

    print("2/3  Recomputing decision matrix and exporting market_data.json...")
    backup_dir = backup_export_dir()
    try:
        export_market_matrix()
        validate_export_files()
    except Exception:
        print("      [!] Export failed or produced invalid output — rolling back")
        print("          web_public/data to the last known-good export so the")
        print("          working tree (and git) never sees the broken output.")
        restore_export_dir(backup_dir)
        raise
    else:
        # No longer needed once the new export has validated successfully.
        if backup_dir and os.path.isdir(backup_dir):
            shutil.rmtree(backup_dir)

    print("3/3  Publishing everything (data + code + website changes)...")
    # Stage the whole repo, not just the data file, so code fixes and
    # index.html/layout edits are never silently left behind.
    run_git("add", "-A")
    status = run_git("status", "--porcelain")
    if not status:
        print("      No changes since last publish — nothing to push.")
        return

    print("      Changes to publish:")
    print("      " + status.replace("\n", "\n      "))

    run_git("commit", "-m", "Update market data / app changes")

    print("      Syncing with remote (in case the social-poster bot")
    print("      pushed a rendered card since your last publish)...")
    sync_with_remote_before_push()

    run_git("push")
    purge_cdn_if_configured()
    print("✅ Pushed. GitHub Actions will redeploy the site within ~1 minute.")


if __name__ == "__main__":
    try:
        main()
    except DatabaseLockedError as e:
        # The single most common way publish.py fails: the MB-EGX desktop
        # app is still open from an earlier session and DuckDB only allows
        # one process to hold the database file at a time. This is a known,
        # actionable situation — no traceback needed, just tell the person
        # what to do.
        print("\n" + "=" * 60)
        print(" [!] Can't update the database — it's already open in")
        print("     another program (usually the MB-EGX desktop app).")
        print("     Close that app, then run publish.py again.")
        print("=" * 60)
        print(f"\n     Details: {e}")
        sys.exit(1)
