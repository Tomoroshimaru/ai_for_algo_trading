# Data model & storage (Step 4)

## Layers
| Layer | Meaning | Retention (default) |
|-------|---------|---------------------|
| `raw` | Immutable evidentiary records (ticks/events) | 30 days |
| `normalized` | Deterministic market-state snapshots | 365 days |
| `derived` | Analytics recomputable from normalized/raw | 730 days |

Retention is stored in the metadata DB (`retention_policy` table) and editable
without code changes.

## Two stores
- **Metadata store** (`metadata.sqlite`): config, jobs, reference instruments,
  schema registry, retention. Migrations are idempotent via `PRAGMA user_version`.
- **Parquet warehouse**: large raw/derived datasets, columnar + partitioned.

## Partitioning
```
warehouse/<layer>/<dataset>/trade_date=YYYY-MM-DD/underlying=<SYM>/data.parquet
```
Daily queries filter on `trade_date` (and optionally `underlying`) and touch only
the relevant partition. Recomputing one derived partition rewrites only that
directory; the raw layer is never rewritten.

## Schemas
Defined in `src/storage/schemas.py`, version `SCHEMA_VERSION`. All 10 datasets:
raw_events, market_state, forwards, iv_points, surface_params, model_prices,
greeks, scenarios, positions, qc_results. Rules:
- Flat columns only (no nested structures).
- Every timestamp is `timestamp[us, tz=UTC]`; never mix time zones in one field.
- Every row carries `trade_date` + `schema_version`.
- Replay and live writes use the **same** schema (single code path).

## Schema evolution
- Bump `SCHEMA_VERSION` and add a metadata migration when fields change.
- Backfill compatibility: readers tolerate older `schema_version` rows; writers
  always stamp the current version. Extra (non-schema) columns are dropped with
  an explicit log; missing required columns are rejected (write-ahead validation).

## Write-ahead validation
`ParquetStore.write_partition` validates before any bytes are written:
missing required columns, nulls in non-nullable columns, and type mismatches all
raise `SchemaValidationError` with an explicit message.

## Lineage — "which raw records produced this snapshot?"
Every normalized/derived row carries `source_session_id`. One call answers it:
```python
from storage.parquet_store import ParquetStore, lineage_raw_for_snapshot
store = ParquetStore(data_dir)
raw_rows = lineage_raw_for_snapshot(store, "2026-06-11", "SPY", session_id)
```
Equivalent one-liner over the raw dataset:
```python
raw = store.read("raw_events", trade_date="2026-06-11", underlying="SPY")
raw[raw.session_id == session_id]
```

## Market-state snapshots (Step 5)
The `market_state` dataset is produced by the **pure** builder in
`src/snapshots/builder.py` (raw events in, snapshots out; no I/O, no clock
reads). Guarantees:
- **Deterministic**: same events + same `SnapshotParams` -> identical rows.
- **Reference spot**: mid when the bid/ask spread is within `max_spread_pct`,
  otherwise documented fallbacks `last` -> `close` -> `mid_wide`, recorded in
  `reference_type` (fallbacks are labeled, never hidden). Chosen spot in
  `reference_price`.
- **Staleness**: `age_sec` is the gap between `snapshot_ts` and the freshest
  eligible quote; `is_stale = age_sec > max_age_sec`.
- **Option join**: each instrument takes its most recent quote at or before
  `snapshot_ts`; quotes after the snapshot are excluded.
- **Completeness**: `compute_completeness` reports fresh-observation fraction
  per (underlying, maturity), bounded in [0, 1].

## Forward & implied-carry engine (Step 6)
The **pure** engine in `src/forward/engine.py` turns a market-state snapshot into
a robust forward per maturity plus a full diagnostics trail.
- **Parity forward** per strike: `F = K + (C - P) / DF`, `DF = exp(-r*T)`
  (`DF = 1` when no rate is supplied; assumption recorded in `method`).
- **Liquidity weighting**: `weight = 1 / (1 + call_spread% + put_spread%)`,
  stale legs get weight 0.
- **Robust estimate**: weighted median of per-strike forwards; outliers rejected
  by MAD with a relative parity-residual floor (so MAD=0 still rejects gross
  outliers). A few bad pairs cannot move the forward.
- **Implied carry**: `ln(F / spot) / T` (cost-of-carry rate).
- **Outputs**: `forwards` (forward, implied_carry, n_pairs, is_reliable) and
  `forward_diagnostics` (per-strike call/put mids, parity_forward, weight,
  residual, quality_label in {inlier, outlier, stale, incomplete}) so any
  poor-quality maturity is explainable from stored data alone.

## Quote QC & normalization (Step 7)
`src/qc/quality.py` decides which option quotes may enter the solver/surface
layers. QC is a **registry of small named checks** (not a monolithic if): each
emits its own `reason_code`, logged separately for tuning and postmortems.
- **Per-quote checks**: `bid_positive`, `spread` (wide->caution, absurd->reject),
  `age` (stale->caution), `crossed_locked` (bid>ask->reject, bid==ask->caution),
  `intrinsic` (below intrinsic / above bound -> reject), `liquidity` (low OI/vol).
- **Chain-level checks**: `monotonicity` (call non-increasing / put non-decreasing
  in strike -> caution), `parity_outlier` (MAD on K+C-P -> reject).
- **Classification**: worst check wins -> `USABLE | CAUTION | REJECT`. Rejects are
  dropped from the filtered chain; cautions are retained but flagged (`qc_status`).
- **Determinism**: a fixed `thresholds_version` always classifies a quote the same.
- **Audit**: the raw snapshot (`market_state`) is untouched; the QC table is
  persisted to `qc_results` (one row per fired check + a FINAL decision per quote),
  so every rejection or downgrade is explainable.

Reason codes: BID_NONPOSITIVE, INCOMPLETE_QUOTE, SPREAD_WIDE, SPREAD_ABSURD,
NO_SPREAD, STALE_QUOTE, CROSSED_MARKET, LOCKED_MARKET, BELOW_INTRINSIC,
ABOVE_BOUND, LOW_OI, LOW_VOLUME, MONOTONICITY_VIOLATION, PARITY_OUTLIER.

## IV inversion engine (Step 8)
`src/iv/inversion.py` converts filtered option prices into implied vols.
- **Scalar solver first** (junior note): `invert_black76` uses a bracketed
  bisection on sigma in `[1e-4, 5.0]` - deterministic and robust - over Black-76
  pricing `black76_price` (options on the parity forward, optional DF=exp(-rT)).
- **No-arbitrage / intrinsic bounds** detect unsolvable inputs before solving.
- **Status labels**: `solved`, `near_intrinsic`, `no_arbitrage`, `short_dated`,
  `no_bracket` - pathological cases are explicit, never silent.
- **Diagnostics per point**: n_iter, final residual, bracket_lo/hi, delta,
  ttm_years, method (`black76` | `american_proxy`). American options use a
  documented European-proxy convention (early-exercise premium ignored).
- **Batch wrapper** `invert_chain` solves a whole QC-filtered chain against the
  Step 6 forwards, writing coordinates (strike, log-moneyness, maturity, delta)
  + diagnostics to `iv_points`.

Reference round-trip recovers injected sigma to ~1e-11; finite price
perturbations move IV monotonically and bounded (no numerical explosions).

## Surface engine & parameter storage (Step 9)
`src/surface/engine.py` builds volatility surfaces from solved IV points and
keeps the raw points and the fitted form strictly separate (junior note: raw
points are never discarded - operators must compare fit vs calibration inputs).
- **Coordinates**: per maturity, points -> log-moneyness `k=log(K/F)` and total
  variance `w = iv^2 * T`.
- **Fit**: per-slice closed-form least squares `w(k)=a+b*k+c*k^2`
  (`model=poly2_totalvar`). Chosen over nonlinear SVI because the acceptance
  criterion requires identical params on repeated runs - OLS is exactly
  reproducible. SVI is a documented upgrade path.
- **Cross-maturity**: `total_var_at` interpolates total variance linearly in T
  between fitted slices (variance space).
- **Diagnostics / quality flags** (per maturity): rmse_iv, max_err_iv, n_points,
  n_rejected, butterfly_ok (convexity c>=0), and warnings SPARSE_SLICE,
  NONCONVEX_VARIANCE, NONPOSITIVE_VARIANCE, POOR_FIT, CALENDAR_VIOLATION (ATM
  total variance must be non-decreasing in T).
- **Storage**: `surface_params` (tidy: a,b,c + metrics + flags + warnings per
  expiry) and `surface_grid` (reconstructed regular log-moneyness grid). Raw
  `iv_points` remain available for audit.
- **Operator plotting**: `slice_comparison` returns tidy raw-vs-fit tables;
  `render_slice_png` renders a raw-points vs fitted-slice chart (matplotlib).

## Pricing engine (Step 10)
`src/pricing/engine.py` is the SINGLE module allowed to map a state vector
(spot, ttm, vol, rate, carry) to price + Greeks (junior note: no pricing logic
in dataframes/notebooks).
- **Typed API**: `price(PricingRequest) -> PricingResult`. Request validates
  right/style/non-negativity; result carries price, delta, gamma, vega, theta,
  rho, vanna, volga, method, n_steps + unit helpers (vega_per_pct,
  theta_per_day, rho_per_bp).
- **European**: closed-form Black-Scholes-Merton with carry; analytic Greeks.
  Consistent with the inversion engine - equals `iv.inversion.black76_price`
  with F=S*exp((r-q)T), DF=exp(-rT) (verified to 1e-10), so solved IVs reprice.
- **American**: Cox-Ross-Rubinstein binomial tree (single-name, carry q),
  Greeks via central finite differences with documented bumps (spot 1e-4 rel,
  vol/rate 1e-4 abs, time 1e-4 abs).
- **Vectorized**: `european_price_array` prices whole strike/vol arrays.
- **Unit conventions** (see module docstring): vol annualized decimal, T years,
  r/q continuous, theta per calendar year, vega per 1.00 vol, rho per 1.00 rate.

Verified: Hull reference call = 10.4506; put-call parity to 1e-9; CRR American
call (no div) converges to European (~1/N); American put shows early-exercise
premium; sign conventions and T->0 / sigma->0 limits covered; 512-step tree in
~3 ms; 10k vectorized prices < 2 s.

## Per-position & portfolio risk analytics (Step 11)
`src/risk/analytics.py` produces the canonical risk snapshot reused by scenarios
and dashboards. Line-level AND aggregate outputs are both persisted - debugging
starts at the line level (junior note).
- **Sensitivity set**: instrument-level price, delta, gamma, vega, theta, rho,
  vanna, volga + monetized $-sensitivities; portfolio-level = summed.
- **Join**: positions (source of record or hypothetical) x market_state (spot)
  x iv_points (solved vol), priced via the Step 10 engine. Options parsed from
  instrument_key; stock legs booked as linear delta=1 exposure. Unpriceable
  lines flagged status=no_spot/no_vol and excluded from aggregates.
- **Dollar conventions (fixed & stable)**: dollar_delta = delta*S*mult*qty;
  dollar_gamma = gamma*S^2*mult*qty*0.01 (per +1% spot); dollar_vega =
  (vega/100)*mult*qty (per +1 vol pt); dollar_theta = (theta/365)*mult*qty
  (per day). mult=100 options / 1 stock.
- **Aggregation**: by portfolio (ALL), underlying, expiry (maturity), instrument.
  Each row carries n_lines (priced), n_total (all) and coverage=n_lines/n_total
  so a partial book (no_spot/no_vol lines excluded) cannot be misread as complete.
- **Reconciliation**: `reconcile_greeks` compares computed vs broker Greeks;
  abs_diff > threshold -> breach=True (surfaced automatically).
- **Storage**: line-level `greeks` (enriched), `risk_aggregates`, `risk_recon`.

Verified: line price == pricer to 1e-9; all four $-conventions checked; portfolio
sum == sum of lines; deterministic aggregates on identical inputs; recon breach
/ no-breach both fire; missing spot/vol flagged. End-to-end on real spot 728.87:
3-line book -> $delta 1.12M, $gamma 23k, $vega 2169/pt, $theta -103/day; broker
delta discrepancy 0.125 surfaced as breach.

## Scenario engine & margin-style diagnostics (Step 12)
`src/scenario/engine.py` approximates worst-case losses under spot / vol / time
shocks for generic risk control, capacity planning and margin diagnostics. No
strategy logic.
- **Versioned grid** (`build_grid(version)`): spot ladder, vol shifts, time
  roll-downs, plus margin-style joint crash/rally. Unknown versions are rejected;
  bump the version rather than mutate. The exact grid is persisted to
  `scenario_defs` (lineage, queryable alongside results - junior note).
- **Full reprice = reference** (`pnl_full`, Step 10 engine). A Greeks Taylor
  approximation (`pnl_greeks`) is provided for speed:
  dPnL ~ delta*dS + 0.5*gamma*dS^2 + vega*dVol + theta*(+roll/365), x mult x qty.
  theta is dPrice/d(calendar time) -> the time term uses +roll/365 (calendar
  advance), not the change in time-to-maturity.
- **Outputs**: `scenario_results` (line-level base/scen value + both PnLs),
  `scenario_summary` (per-scenario portfolio & underlying totals, approx_error,
  is_worst_case). `top_contributors()` explains a scenario line-by-line.
- **Agreement**: full vs Greeks agree within ~0.1% for small shocks (5% spot)
  and diverge for large ones (~5% at -20% spot) - full stays the reference.

Verified: base scenario PnL==0; exact regeneration on (positions, snapshot,
version); full/Greeks agree small / diverge large; theta sign correct (time roll
loses money on long gamma in BOTH paths); worst-case PnL == sum of line
contributors; underlying attribution sums to portfolio. End-to-end on real spot
728.87: worst case spot_-0.20 = -187k, explained by short put -100k + long call
-72k + stock -15k.

## Historical reconstruction & replay (Step 13)
Replay reconstructs historical analytics from stored snapshots using the SAME
code path as live - there is no 'historical only' fork (junior note: dual paths
drift).
- **Shared pipeline** `src/pipeline/daily.py::run_day`: snapshot -> forwards ->
  IV -> surface -> (risk if positions). Live and replay call this identically;
  they differ only in input source and output location.
- **Replay driver** `src/replay/backfill.py`: `replay_day` / `replay_range`
  batch over a date range (task b); `detect_partitions` flags missing inputs
  (task c); outputs are archived in versioned partitions via the new
  `code_version=` partition level so a newer code version never silently
  overwrites older historical analytics (task d).
- **Versioned partitions**: `ParquetStore.write_partition(..., code_version=V)`
  writes under `.../underlying=U/code_version=V/`. Live reads (`code_version=None`)
  ignore versioned subtrees entirely; live layout is unchanged.
- **QA & alignment**: `replay_range` emits qc_results rows (partition-presence +
  analytics-coverage per date). `compare_replay_vs_live` joins live vs replay and
  reports max abs diff per column (task e).

Verified end-to-end: a 21-business-day historical month reconstructed (20 OK, 1
MISSING gap flagged not masked); replay-vs-live forward max_abs_diff = 0.0 on
overlapping dates with the same code version; versioned archive isolated from the
live tree. Missing data is surfaced, never interpolated silently.

## Validation framework & anomaly detection (Step 14)
Validation is treated as a PRODUCT: every flag is specific and actionable (which
underlying, which maturity, which metric, which threshold) with a machine-readable
reason_code and human context - no generic red banners (junior note).
- **`src/validation/framework.py::run_validation`** runs 7 checks on a pipeline
  RunResult: stale_data_rate, coverage, solver_convergence, forward_stability
  (+forward_quality), surface_smoothness, no_arbitrage_butterfly/calendar,
  reconciliation. Emits `validation_results` rows (PASS/WARN/FAIL) with
  reason_code + metric_value + threshold + detail. Thresholds are configurable
  via `ValidationThresholds`.
- **`summarize`** -> daily pass/warn/fail roll-up (task b). **`triage_view`** ->
  failures only, FAIL-first, with full context (task d / acceptance): an operator
  sees the failing maturity and reason in seconds.
- **`src/validation/anomaly.py`** flags metrics (quote counts, forward residuals,
  fit errors, scenario losses) deviating > z_threshold from a trailing rolling
  baseline (which excludes the current value). Emits `qc_anomalies` rows.
- **Datasets**: `validation_results` (doubles as the triage table; metric_value
  over run_date gives regression-monitoring trends) and `qc_anomalies`.

Reason codes: COVERAGE_LOW, STALE_HIGH, FWD_RESIDUAL_HIGH, FWD_LOW_QUALITY,
SOLVER_NONCONV, SURFACE_ROUGH, BUTTERFLY_ARB, CALENDAR_ARB, RECON_BREACH.

## Orchestration, logging & observability (Step 15)
Makes the build operable: schedules, retries, metrics, alerting. "A correct
algorithm that cannot be monitored is not production-ready."
- **`src/orchestration/runner.py`** - `Job` (named callable) + `JobRunner` give
  every job uniform retries, a correlation_id, and a durable `job_runs` ledger.
  Jobs (task a): universe_refresh, live_collection, incremental_analytics,
  eod_reconciliation, replay, qc.
  - **Idempotent restart (task e)**: before executing, the runner checks the
    ledger for a prior SUCCEEDED run of (job, run_date); if found and not
    `force`, it SKIPS. With ParquetStore single-partition overwrite, restarting
    never duplicates or corrupts outputs (even a forced rerun overwrites).
- **`logging_ctx.py`** - structured JSON logs; `correlation_id_from_session`
  binds a collector session to all downstream analytics jobs (task b).
- **`metrics.py`** - few, well-labeled metrics (task c / junior note):
  quote_count, stale_ratio, forward_failures, solver_failures, surfaces_built,
  scenario_runtime_sec -> `ops_metrics`.
- **`alerts.py`** - four alerts (task d) with severity->route (CRITICAL=page,
  WARN=slack, INFO=email): collector_death, missing_partitions,
  elevated_failure_rate, qc_fail (consumes Step 14 reason codes; completes Step
  14 task e "who gets notified"). -> `alerts`.
- **`health.py`** - `health_summary` answers the four operator questions at a
  glance; `last_healthy_runs` + `backlog` give the last good run and outstanding
  work instantly (task f / acceptance).
- **Datasets**: `job_runs` (ledger), `ops_metrics`, `alerts`.
