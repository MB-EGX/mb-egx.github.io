# Quant Engine — Patch Notes

## What changed in this drop

### 1. `config.py` — extended
* New `ACTION_THRESHOLDS` dict. Every hard-coded magic number in
  `decision_matrix` (sell-avoid price ratio, RSI caps, breakout momentum
  RSI floor, take-profit floors, sector-rotation cut-offs, etc.) now
  lives here. Tune the whole engine from one file.
* New `SCORE_WEIGHTS` entries `breakout_crossover` (35) and
  `breakout_momentum` (28) so the two new breakout labels (see below)
  have their own weights instead of sharing one.
* New `CHART_HISTORY_DAYS` constant (365) used by `export_json.py`.

### 2. `analytics.py` — fixed + extended
* **`_compute_adx` now uses proper Wilder smoothing.** The previous
  implementation used `dx.rolling().mean()` for the ADX itself, which
  under-weights recent DX values and produces a lagging trend-strength
  reading. The fix uses `ewm(alpha=1/period, adjust=False)` end-to-end
  (TR, +DM, -DM, and the final ADX), matching TradingView / TA-Lib.
* **`get_all_market_data_bulk(days=None)`** — added a `days` parameter
  that pushes a `WHERE date >= ?` clause into DuckDB. Used by the
  chart-history export so the worker does not pay RAM/transfer cost
  for ancient bars. All existing call sites pass no `days` and keep
  full-history behavior.
* `compute_sector_analytics` now sources its thresholds from
  `ACTION_THRESHOLDS` instead of hard-coding them.
* Added a `if __name__ == "__main__"` smoke block.

### 3. `decision_matrix.py` — semantics improved
* **BREAKOUT BUY is now three distinct labels** (was one bucket):
  * `⚡ BREAKOUT BUY (X-OVER + MOMENTUM)` — both signals fire (strongest)
  * `⚡ BREAKOUT BUY (X-OVER)`           — SMA-50 golden cross only
  * `⚡ BREAKOUT BUY (MOMENTUM)`         — price > EMA20 AND RSI ≥ 52
  The original `OR`-merge masked whether a signal was a confirmed
  crossover or just an RSI overheat. The substring `"⚡ BREAKOUT BUY"`
  still matches all three, so the Top-10 category filter keeps working.
* All action-classification thresholds now read from
  `config.ACTION_THRESHOLDS` (sell-avoid, strong-buy, breakout gap
  floor, dip thresholds, ADX gate, volume gates, ATR trailing
  multiplier, take-profit floors, default take-profit %).

### 4. `export_json.py` — bug fixes
* **Demo trades are now filtered out** of the public web export
  (matching what `app_gui.export_trade_ledger` already did). A count of
  excluded demo trades is printed to stdout.
* **Output path is anchored to the script's own directory** (was
  CWD-relative, which broke when run from anywhere other than the
  project root).
* **365-day chart slice is pushed into SQL** via the new
  `get_all_market_data_bulk(days=...)` instead of pulling every bar of
  every ticker just to tail-trim in pandas.

### 5. New supporting files
* **`requirements.txt`** — pinned `~=` versions of the 7 dependencies.
  Stops `launch.bat`'s `--upgrade` flag from pulling a surprise
  matplotlib tomorrow.
* **`.gitignore`** — Python caches, venvs, DuckDB files, the log, the
  drop folder, and the generated web payload.
* **`CHANGELOG.md`** — this file.

### 6. Files unchanged
* `db_manager.py` — clean. The `is_demo` column on `portfolio_closed`
  was flagged as a potential migration concern; the existing code
  already defends against it (`bool(r[8]) if len(r) > 8 else False`),
  and the GUI is the only writer in practice, so no schema change is
  required.
* `ingestion.py` — clean. The Excel-serial-date heuristic, exclusion
  list, ROW_NUMBER dedupe, and incremental file-tracker are all solid.
* `chart_widget.py` — clean.
* `app_gui.py` — clean. This is the v3 (`56faa86b`) — full i18n EN/AR,
  MatrixTableModel, 5 themes, ColumnChooserDialog, integrated
  StockSectorChartWidget. See step 7 for the v2 cleanup.
* `launch.bat` — clean. Will now find `requirements.txt` in the same
  directory.

## 7. What to delete from the old tree

The dump contains **two `app_gui.py` versions**. The deprecated one is
`6e5474cc__0acc13a2-e59a-4c9c-b8fc-c8be83a0da1b.py` (1123 lines, has
`[STARTUP TIMING] _mark()` probes, no i18n, no chart widget, uses
plain `QTableWidget` for the matrix). **Delete it.** Keep the v3
file (1355 lines) which is what this patch provides as `app_gui.py`.

## Known limitations (out of scope for this patch)

* **No unit tests.** The pattern engine, the indicator pipeline, and
  the position-sizing math are all non-trivial and would benefit from
  pytest coverage.
* **No `README.md`.** The intended user is the original author; a
  walk-through doc was not in scope.
* **Action-threshold tuning is still a manual process.** The
  parameters are now in one place, but no auto-calibration against
  historical P&L has been wired up.
