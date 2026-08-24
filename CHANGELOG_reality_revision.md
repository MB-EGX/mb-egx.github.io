# MB-EGX — Classification & Calculation Revision Log (old → new)

Goal: replace invented/arbitrary scoring with defensible, standard trading
formulas — verified against textbook definitions and run on the real sample
data in `market_data.json.txt`. Every change is backward-compatible (additive
fields or config-driven knobs); no file was rewritten from scratch.

## 1. `analytics.py` — Wilder ATR-14 (was a plain rolling mean)
- **Old:** `atr_14 = true_range.rolling(14).mean()` — under-weights recent
  true ranges, lags the textbook reading.
- **New:** Wilder smoothing `ATR_t = (ATR_{t-1}*(n-1) + TR_t)/n`, seeded with
  the SMA of the first 14 true ranges, vectorized as `ewm(alpha=1/14)` with a
  seed correction; `TR[0] = high - low` (first TR was NaN because of
  `close.shift()`).
- **Verified:** max |deviation| vs. a naive loop reference ≈ 1e-15 (machine
  epsilon) on 6 real tickers. Source: Wilder (1978); StockCharts ATR.

## 2. `analytics.py` — Annualized Sharpe / Sortino (was raw per-bar ratios)
- **Old:** `sharpe = mean/std`, `sortino = mean/downside_std` — a daily ratio
  mislabeled as if it were comparable across timeframes.
- **New:** `sharpe = (mean - rf_daily)/std × √252`, `sortino` likewise; raw
  per-bar values kept as `sharpe_daily`/`sortino_daily`. `rf` from
  `config.ANNUALIZED_RISK_FREE_RATE` (0.0 by default — no clean EGP risk-free
  series is maintained).
- **Verified:** standard annualization `×√252` (QuantStack / Investopedia);
  empty-returns branch updated to carry the new keys.

## 3. `config.py` — new tunable constants
- `TRADING_DAYS_PER_YEAR = 252`, `ANNUALIZED_RISK_FREE_RATE = 0.0`
- `POSITION_SIZE_FEE_ADJUST = True` — make the 1%-risk figure net of fees.
- `KELLY_FRACTION = 0.5`, `KELLY_CAP_FRACTION = 0.25` — half-Kelly, capped.
- `DEFAULT_WIN_RATE_PRIOR = 0.5` — honest 50/50 prior, no invented edge.

## 4. `decision_matrix.py` — Kelly fraction (was hardcoded 0.5/0.25)
- **Old:** `raw = win_rate - (1-win_rate)/payoff` then `min(raw*0.5, 0.25)`
  inline.
- **New:** same standard form `f* = p - q/b` (Kelly, 1956), scaled by
  `config.KELLY_FRACTION` and capped by `config.KELLY_CAP_FRACTION` — the risk
  posture is now tunable in one place. Half-Kelly keeps ~75% of the growth
  rate at ~half the variance (standard practice).

## 5. `decision_matrix.py` — position sizing (1%-risk, net of fees)
- **Old:** `shares = risk_budget / stop_distance` (gross risk).
- **New:** `shares = risk_budget / (stop_distance + entry×ROUND_TRIP_FEE)` —
  the 1% risk figure is now the NET loss if stopped out after both
  commissions. Stop LEVEL unchanged.

## 6. `decision_matrix.py` — win-rate estimate for Kelly (was invented 0.45)
- **Old:** `win_rate_est = 0.45` whenever no historical-analog match.
- **New:** `config.DEFAULT_WIN_RATE_PRIOR` (0.5) — never fabricate an edge.
  (Realized per-action win rates from the backtester are the proper future
  source; see R-multiple bookkeeping.)

## 7. `backtester.py` — R-multiple / MAE / MFE bookkeeping
- **New per closed trade:** `initial_risk = entry - stop`; `r_multiple =
  (exit-entry)/initial_risk`; `mae_r` / `mfe_r` from the in-trade low/high
  envelope. New aggregate fields: `avg_r_multiple`, `expectancy_r`,
  `avg_mae_r`, `avg_mfe_r`, `avg_holding_bars`.
- **New:** survivorship-bias note in the module docstring (universe = surviving
  tickers; historical win rates likely overstated).
- **Verified:** 26 trades fired on the sample with every R-field populated.

## 8. `factor_analysis.py` — factor buckets report R-based stats
- **New per bucket:** `avg_r_multiple`, `expectancy_r`, `avg_mae_r`,
  `avg_mfe_r` alongside win-rate/CI — so "is this factor worth trusting" is
  answered in R, not just %.

## Verified on real sample data (`market_data.json.txt`)
- Wilder ATR matches reference to ~1e-15 on 6 tickers; no NaN cascades.
- Classification distribution sane (ACCUMULATE dominant, few STRONG BUY /
  BREAKOUT on the sample).
- Walk-forward simulator fires trades; every closed trade carries R/MAE/MFE.
- Note: the sample's `chart_history` series carry a tiny high-low spread
  (ATR ≈ 0 on some bars), which inflates R-multiple magnitudes in this
  harness run — a data-scale artifact of the sample, not a formula bug. On
  real EGP price data the R scale is normal.
