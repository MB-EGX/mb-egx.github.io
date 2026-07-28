"""
publish.py — one-command refresh for the whole pipeline.

Run this after you drop new spreadsheets/CSVs into market_data_feeds/:

    python publish.py

It will:
  1. Ingest any new/changed files in market_data_feeds/ into DuckDB.
  2. Recompute the decision matrix and regenerate
     web_public/data/market_data.json.
  3. Commit + push that file. The .github/workflows/deploy-pages.yml
     workflow (triggered by the push) then redeploys the live site
     automatically — nothing further to do on GitHub's side.

Run this from the repo root (same directory as export_json.py / config.py).
"""
import subprocess
import sys

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

    print("3/3  Publishing to GitHub Pages...")
    run_git("add", "web_public/data/market_data.json")
    status = run_git("status", "--porcelain", "web_public/data/market_data.json")
    if not status:
        print("      No data changes since last publish — nothing to push.")
        return
    run_git("commit", "-m", "Update market data")
    run_git("push")
    print("✅ Pushed. GitHub Actions will redeploy the site within ~1 minute.")


if __name__ == "__main__":
    main()
