# Interface Contracts (frozen)

> Step 16 (a). These contracts are **frozen**: changing a dataset schema or a
> public entrypoint signature is a breaking change governed by
> [CHANGE_MANAGEMENT.md](CHANGE_MANAGEMENT.md). Generated from
> `src/storage/schemas.py` (single source of truth).

## Storage layout

All datasets are Parquet, partitioned by `trade_date` then `underlying`
(`<root>/<layer>/<dataset>/trade_date=<d>/underlying=<u>/data.parquet`).
Writes are **single-partition overwrite** (idempotent): re-writing a
partition replaces it, so reruns never duplicate rows. Every row carries the
common trailer columns `trade_date` (partition) and `schema_version`.

## Datasets by layer

### raw

- **`raw_events`** — 10 cols: `session_id`, `underlying`, `instrument_key`, `field`, `value`, `source_ts`, `receipt_ts`, `collector_ts`, `trade_date`, `schema_version`

### normalized

- **`market_state`** — 16 cols: `snapshot_ts`, `underlying`, `instrument_key`, `bid`, `ask`, `last`, `mid`, `spread_pct`, `reference_price`, `reference_type`, `is_stale`, `is_market_open`, `age_sec`, `source_session_id`, `trade_date`, `schema_version`

### derived

- **`forwards`** — 11 cols: `snapshot_ts`, `underlying`, `expiry`, `forward`, `implied_carry`, `method`, `n_pairs`, `is_reliable`, `source_session_id`, `trade_date`, `schema_version`
- **`forward_diagnostics`** — 13 cols: `snapshot_ts`, `underlying`, `expiry`, `strike`, `call_mid`, `put_mid`, `parity_forward`, `weight`, `residual`, `quality_label`, `source_session_id`, `trade_date`, `schema_version`
- **`iv_points`** — 19 cols: `snapshot_ts`, `underlying`, `expiry`, `strike`, `option_right`, `iv`, `moneyness`, `forward`, `delta`, `ttm_years`, `method`, `status`, `n_iter`, `residual`, `bracket_lo`, `bracket_hi`, `source_session_id`, `trade_date`, `schema_version`
- **`surface_params`** — 9 cols: `snapshot_ts`, `underlying`, `expiry`, `model`, `param_name`, `param_value`, `source_session_id`, `trade_date`, `schema_version`
- **`surface_grid`** — 11 cols: `snapshot_ts`, `underlying`, `expiry`, `log_moneyness`, `ttm_years`, `total_variance`, `iv`, `model`, `source_session_id`, `trade_date`, `schema_version`
- **`model_prices`** — 11 cols: `snapshot_ts`, `underlying`, `instrument_key`, `expiry`, `strike`, `option_right`, `price`, `model`, `source_session_id`, `trade_date`, `schema_version`
- **`greeks`** — 29 cols: `snapshot_ts`, `underlying`, `instrument_key`, `account`, `expiry`, `option_right`, `strike`, `quantity`, `multiplier`, `spot`, `vol`, `price`, `position_value`, `delta`, `gamma`, `vega`, `theta`, `rho`, `vanna`, `volga`, `dollar_delta`, `dollar_gamma`, `dollar_vega`, `dollar_theta`, `status`, `model`, `source_session_id`, `trade_date`, `schema_version`
- **`risk_aggregates`** — 21 cols: `snapshot_ts`, `underlying`, `group_key`, `group_value`, `n_lines`, `n_total`, `coverage`, `position_value`, `delta`, `gamma`, `vega`, `theta`, `rho`, `dollar_delta`, `dollar_gamma`, `dollar_vega`, `dollar_theta`, `model`, `source_session_id`, `trade_date`, `schema_version`
- **`risk_recon`** — 13 cols: `snapshot_ts`, `underlying`, `instrument_key`, `greek`, `computed`, `broker`, `diff`, `abs_diff`, `threshold`, `breach`, `source_session_id`, `trade_date`, `schema_version`
- **`scenarios`** — 9 cols: `scenario_id`, `snapshot_ts`, `underlying`, `shock_type`, `shock_value`, `pnl`, `source_session_id`, `trade_date`, `schema_version`
- **`scenario_defs`** — 10 cols: `version`, `scenario_id`, `underlying`, `family`, `spot_shock`, `vol_shock`, `time_roll_days`, `label`, `trade_date`, `schema_version`
- **`scenario_results`** — 13 cols: `snapshot_ts`, `version`, `scenario_id`, `underlying`, `instrument_key`, `family`, `base_value`, `scen_value`, `pnl_full`, `pnl_greeks`, `source_session_id`, `trade_date`, `schema_version`
- **`scenario_summary`** — 15 cols: `snapshot_ts`, `version`, `scenario_id`, `underlying`, `family`, `group_key`, `group_value`, `n_lines`, `pnl_full`, `pnl_greeks`, `approx_error`, `is_worst_case`, `source_session_id`, `trade_date`, `schema_version`
- **`positions`** — 8 cols: `as_of_ts`, `account`, `underlying`, `instrument_key`, `quantity`, `avg_cost`, `trade_date`, `schema_version`
- **`qc_results`** — 8 cols: `check_ts`, `underlying`, `check_name`, `target`, `status`, `detail`, `trade_date`, `schema_version`
- **`validation_results`** — 13 cols: `check_ts`, `run_date`, `underlying`, `target`, `check_name`, `status`, `reason_code`, `metric_value`, `threshold`, `detail`, `source_session_id`, `trade_date`, `schema_version`
- **`qc_anomalies`** — 14 cols: `check_ts`, `run_date`, `underlying`, `metric_name`, `value`, `baseline_mean`, `baseline_std`, `zscore`, `is_anomaly`, `n_baseline`, `detail`, `source_session_id`, `trade_date`, `schema_version`
- **`job_runs`** — 14 cols: `run_id`, `job_name`, `run_date`, `attempt`, `status`, `started_ts`, `ended_ts`, `duration_sec`, `correlation_id`, `rows_out`, `error`, `detail`, `trade_date`, `schema_version`
- **`ops_metrics`** — 9 cols: `metric_ts`, `run_date`, `scope`, `metric_name`, `value`, `correlation_id`, `detail`, `trade_date`, `schema_version`
- **`alerts`** — 10 cols: `alert_ts`, `run_date`, `alert_name`, `severity`, `route`, `status`, `detail`, `correlation_id`, `trade_date`, `schema_version`

## Public entrypoints (frozen signatures)

| Area | Symbol | Module |
|---|---|---|
| Connectivity | `IBSession` | `connectivity.session` |
| Connectivity smoke test | `scripts/bootstrap_smoke_test.py` | `scripts` |
| Collector service | `scripts/run_collector.py` | `scripts` |
| Daily analytics | `run_day(snapshot, source_session_id) -> RunResult` | `pipeline.daily` |
| Replay one day | `replay_day(store, trade_date, code_version)` | `replay.backfill` |
| Replay range | `replay_range(store, dates, code_version)` | `replay.backfill` |
| Replay vs live | `compare_replay_vs_live(store, dataset, trade_date)` | `replay.backfill` |
| Validation | `run_validation(run_result, run_date, session_id) -> df` | `validation.framework` |
| QC triage | `triage_view(validation_df)` | `validation.framework` |
| Anomaly detection | `detect_metric_anomalies(history, current, ...)` | `validation.anomaly` |
| Job runner | `JobRunner(store).run(Job, run_date)` | `orchestration.runner` |
| Metrics | `compute_run_metrics(run_result, ...)` | `orchestration.metrics` |
| Alerts | `evaluate_alerts(...)` | `orchestration.alerts` |
| Health/dashboard | `health_summary(...) / last_healthy_runs / backlog` | `orchestration.health` |
| Storage | `ParquetStore(root).write_partition / read` | `storage.parquet_store` |
| Schemas | `get_dataset(name) / DATASETS / validate` | `storage.schemas` |
