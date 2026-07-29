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
import subprocess
import sys

# MUST be imported before ingestion/export_json (and, transitively,
# analytics/decision_matrix) - config.py sets the OPENBLAS_NUM_THREADS /
# MKL_NUM_THREADS / OMP_NUM_THREADS / NUMEXPR_NUM_THREADS env vars as a
# module-level side effect, and those only take effect if set BEFORE
# numpy/pandas are imported anywhere in this process. Importing it here
# first also means any ProcessPoolExecutor spawned later (Windows uses
# 'spawn') inherits the already-capped environment for its child
# processes, regardless of each module's own internal import order.
import config  # noqa: F401

from ingestion import IngestionPipeline
from export_json import export_market_matrix


def run_git(*args):
    result = subprocess.run(["git", *args], capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout, result.stderr, file=sys.stderr)
        raise SystemExit(f"git {' '.join(args)} failed")
    return result.stdout.strip()


def main():
    print("1/3  Ingesting new spreadsheet/CSV feeds...")
    IngestionPipeline().run_incremental_ingestion(
        progress_callback=lambda pct, msg: print(f"      [{pct:>3}%] {msg}")
    )

    print("2/3  Recomputing decision matrix and exporting market_data.json...")
    export_market_matrix()

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
    run_git("push")
    print("✅ Pushed. GitHub Actions will redeploy the site within ~1 minute.")


if __name__ == "__main__":
    main()
