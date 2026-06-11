# Runbooks (SOPs)

> Step 16 (b). Every recurring operational procedure, as concrete commands.
> Prereqs: `uv sync`; IB Gateway/TWS running with the API enabled for any
> live procedure (replay/QC are fully offline). Run from the repo root.

## RB-1 - Connectivity smoke test (is the pipe alive?)
Proves we can reach IBKR end to end without placing an order: connect ->
health/clock -> resolve one contract -> one snapshot -> JSON evidence artifact.
```bash
uv run python scripts/bootstrap_smoke_test.py
```
Expected: exit 0 and an evidence JSON under `artifacts/`. Non-zero exit =>
connection/clock-skew failure; see RB-6.

## RB-2 - Run the live collector (capture ticks)
```bash
uv run python scripts/run_collector.py --symbols SPY,AAPL --duration 60
```
Streams every tick into the append-only `raw_events` store with heartbeat-driven
reconnect; prints a session summary (and `session_id`) on exit. Note the
`session_id`: it seeds the correlation_id for every downstream job.

## RB-3 - Daily analytics run
Forwards -> IV inversion -> surface fit -> pricing/greeks -> risk -> scenarios,
all from one normalized snapshot. In code/notebook:
```python
from pipeline.daily import run_day
res = run_day(snapshot_df, source_session_id="<session_id>")
```
Persist via `ParquetStore.write_partition`, or run it under the job runner
(RB-4) so it lands in the ledger.

## RB-4 - Run a job with retries + ledger (idempotent)
```python
from orchestration.runner import Job, JobRunner
from orchestration.logging_ctx import correlation_id_from_session
runner = JobRunner(store)
runner.run(Job("incremental_analytics", my_fn), "2026-06-11",
           correlation_id=correlation_id_from_session("<session_id>"))
```
A prior SUCCEEDED run of the same (job, run_date) is **SKIPPED** -> safe to
relaunch. Use `force=True` only to deliberately recompute (overwrites, never
duplicates).

## RB-5 - Replay / backfill a historical day
```python
from replay.backfill import replay_day, compare_replay_vs_live
res = replay_day(store, "2026-06-10", code_version="v0.1.0")
diff = compare_replay_vs_live(store, "surface_grid", "2026-06-10")
```
Replay re-derives analytics from stored raw/normalized data. Determinism is
covered by `tests/test_replay*`.

## RB-6 - Read the QC report & triage failures
```python
from validation.framework import run_validation, summarize, triage_view
val = run_validation(res, "2026-06-11", "<session_id>", market_state=snapshot_df)
print(summarize(val))          # {'PASS':.., 'WARN':.., 'FAIL':.., 'overall':..}
print(triage_view(val))        # FAIL-first, each row: underlying/target/reason_code/detail
```
Every failing row names the **underlying**, the **maturity/instrument**, a
machine `reason_code`, and a human `detail` telling you where to look.

## RB-7 - Investigate a failed surface build (acceptance scenario)
1. `triage_view(val)` -> find the FAIL row for the underlying/expiry.
2. Read the `reason_code`:
   - `COVERAGE_LOW` / `SOLVER_NONCONV` -> too few solved IV points on that
     maturity. Check `iv_points` for that (underlying, expiry): how many
     `status != 'solved'`? Then check `market_state` stale ratio and quote
     count for those strikes (upstream data thin/stale).
   - `FWD_RESIDUAL_HIGH` / `FWD_LOW_QUALITY` -> bad forward. Inspect
     `forward_diagnostics` (parity residuals, `quality_label`) for that expiry;
     a fallback forward poisons the whole slice.
   - `SURFACE_ROUGH` -> fit RMSE over threshold. Inspect `surface_params`
     (`rmse_iv`, `max_err_iv`) and the input IV points for outliers.
   - `BUTTERFLY_ARB` / `CALENDAR_ARB` -> no-arbitrage violation; the smile is
     non-convex or out of order vs neighbours. Inspect `surface_grid`
     total-variance monotonicity.
3. Cross-reference with `ops_metrics` (solver_failures, forward_failures) and
   `alerts` for the same run_date/correlation_id.

## RB-8 - Restart after a failure (no duplication)
Just relaunch the job (RB-4). The ledger SKIPS already-SUCCEEDED work; partition
writes overwrite in place. To recompute a known-bad partition, rerun with
`force=True`. Confirm with `last_healthy_runs(ledger)` and `backlog(...)`.

## RB-9 - System health at a glance
```python
from orchestration.health import health_summary, last_healthy_runs, backlog
health_summary(run_date="2026-06-11", metrics=m, job_ledger=led, alerts=al)
```
Answers: data flowing? surfaces building? QC passing? scenarios current? Plus
the last healthy run per job and current backlog. The operator console
(`uv run uvicorn app.main:app`) renders the same signals.

## RB-10 - Run the test suite
```bash
uv run pytest -q        # full offline suite (must be green before release)
```
