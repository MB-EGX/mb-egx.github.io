# MB-EGX cleanup plan

## Safe to remove now
These are not runtime dependencies and are either generated output or temporary delivery notes:

- `quant_app.log`
- `CHANGELOG.md`
- `FINAL_MANIFEST.txt`
- `PARITY_MANIFEST.txt`
- `PHASE1_MANIFEST.txt`

## Remove after adopting the new consolidated files
These are replaced by the two new consolidated CLIs below:

### Replaced by `backtest_tools.py`
- `check_backtest_readiness.py`
- `diagnose_backtest_coverage.py`
- `run_backtest.py`
- `run_factor_backtest.py`
- `export_backtest_summary.py`

### Replaced by `data_repair_tools.py`
- `fix_future_dates.py`
- `fix_session_pick_dates.py`
- `reconstruct_from_git_history.py`
- `revert_false_achievements.py`
- `purge_false_leaderboard_hits.py`
- `verify_purge.py`

## Optional archive / move out of repo root
These are useful, but they do not belong in the runtime root if you want a cleaner app folder:

- `MB-EGX-Trader-Guide.docx` → move to `docs/`
- `dryrun_post_state.py` → move to `tests/` or `sandbox/`
- `remind_feed_csv.vbs` → keep only if you still use Windows Task Scheduler reminder popups
- `needs_review.csv` → remove after finalizing ticker mapping

## Keep as separate files
These still make sense as their own files:

- `export_subscribers.py` (Firebase admin/export concern is separate)
- `build_ticker_map.py` + `normalize_historical_csvs.py` + `ticker_map.csv` (historical data onboarding flow)
- core runtime modules like `analytics.py`, `decision_matrix.py`, `db_manager.py`, `session_picks.py`, `social_poster.py`, `telegram_bot.py`

## New consolidated files

### 1) `backtest_tools.py`
Unified commands:

- `python backtest_tools.py readiness`
- `python backtest_tools.py diagnose --tickers COMI HRHO`
- `python backtest_tools.py run --save`
- `python backtest_tools.py factors --out backtests/factor_backtest.json`
- `python backtest_tools.py export-summary`

### 2) `data_repair_tools.py`
Unified commands:

- `python data_repair_tools.py future-dates --fix`
- `python data_repair_tools.py session-pick-dates --fix`
- `python data_repair_tools.py reconstruct-git --ticker ABUK.CA --ticker EAST.CA`
- `python data_repair_tools.py revert-achievements --fix`
- `python data_repair_tools.py purge-leaderboard --fix`
- `python data_repair_tools.py verify-purge`

## Included patched files

- `app_gui.py` now imports `build_summary` from `backtest_tools.py`
- `launch_and_publish.bat` now calls `backtest_tools.py` instead of the old separate backtest scripts

## Not changed in this pass

- `app_gui.py` is still very large. It should eventually be split into `gui/dialogs.py`, `gui/models.py`, `gui/workers.py`, and `gui/main_window.py`, but that is a larger/high-risk refactor and I left it out of this safe cleanup pass.
