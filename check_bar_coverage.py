"""
check_bar_coverage.py
======================
Ground-truth diagnostic: how many bars does EACH ticker actually have
in market_data right now, and what date range does that cover?

This exists because comments in scripts (e.g. launch_and_publish.bat's
reference to an old diagnose_backtest_coverage.py run) can go stale the
moment more data is ingested — the only trustworthy answer is a live
query against quant_master.duckdb itself.

USAGE (run from the repo folder, where quant_master.duckdb lives):
    python check_bar_coverage.py --db quant_master.duckdb

Prints, per ticker:
    - bar count (rows in market_data)
    - earliest date, latest date
    - whether it clears the walk-forward backtester's 310-bar floor
      (WALK_FORWARD_BACKTEST_DEFAULTS: min_train_bars=250 + test_bars=60)
    - whether it clears the ~500-bar bar needed for 4 reliable folds

Also prints an overall summary: how many tickers clear each bar, and
the min/median/max bar count across the whole universe, so you know
exactly where you stand before running any backtest.
"""
import argparse
import statistics
import duckdb

MIN_ONE_FOLD = 310   # min_train_bars(250) + test_bars(60)
MIN_FOUR_FOLDS = 250 + 60 * 4  # 490 — rough floor for min_folds=4 reliability


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="quant_master.duckdb")
    ap.add_argument("--top-n", type=int, default=20, help="How many tickers to print in detail (sorted by bar count, ascending — the worst-covered first).")
    args = ap.parse_args()

    con = duckdb.connect(args.db, read_only=True)
    rows = con.execute("""
        SELECT ticker, COUNT(*) AS n_bars, MIN(date) AS first_date, MAX(date) AS last_date
        FROM market_data
        GROUP BY ticker
        ORDER BY n_bars ASC
    """).fetchall()
    con.close()

    if not rows:
        print("No rows found in market_data at all. Nothing has been ingested yet.")
        return

    counts = [r[1] for r in rows]
    n_tickers = len(rows)
    clears_one_fold = sum(1 for c in counts if c >= MIN_ONE_FOLD)
    clears_four_folds = sum(1 for c in counts if c >= MIN_FOUR_FOLDS)

    print(f"Tickers in market_data: {n_tickers}")
    print(f"Bar count — min: {min(counts)}  median: {statistics.median(counts):.0f}  max: {max(counts)}")
    print(f"Tickers with >= {MIN_ONE_FOLD} bars (can produce at least 1 backtest fold): {clears_one_fold}/{n_tickers}")
    print(f"Tickers with >= {MIN_FOUR_FOLDS} bars (can reach the 4-fold 'reliable' bar): {clears_four_folds}/{n_tickers}")

    print(f"\nWorst-covered {min(args.top_n, n_tickers)} ticker(s) (lowest bar count first):")
    print(f"  {'Ticker':<14}{'Bars':<8}{'First date':<14}{'Last date':<14}")
    for ticker, n_bars, first_date, last_date in rows[: args.top_n]:
        print(f"  {ticker:<14}{n_bars:<8}{str(first_date)[:10]:<14}{str(last_date)[:10]:<14}")

    print(f"\nBest-covered {min(args.top_n, n_tickers)} ticker(s):")
    for ticker, n_bars, first_date, last_date in rows[-args.top_n:][::-1]:
        print(f"  {ticker:<14}{n_bars:<8}{str(first_date)[:10]:<14}{str(last_date)[:10]:<14}")

    if clears_one_fold == 0:
        print(f"\n⚠️  NO ticker currently clears the {MIN_ONE_FOLD}-bar floor for even one backtest fold.")
        print("   The walk-forward backtester (backtester.py) and factor_analysis.py")
        print("   cannot produce ANY result yet — every threshold in config.py is still")
        print("   unvalidated. Ingesting deeper historical CSVs is the only fix.")
    elif clears_four_folds == 0:
        print(f"\n⚠️  Some tickers can produce a fold, but NONE reach ~{MIN_FOUR_FOLDS} bars for")
        print("   the 4-fold 'reliable' bar. Any backtest result right now would be")
        print("   flagged reliable=false (see backtester._aggregate_results) — treat it")
        print("   as illustrative only, not something to retune thresholds off of yet.")
    else:
        print(f"\n✅ {clears_four_folds} ticker(s) have enough history for a reliable backtest.")
        print("   Run: python backtest_tools.py run --save")
        print("   Then: python backtest_tools.py factors --out backtests/factor_backtest.json")


if __name__ == "__main__":
    main()
