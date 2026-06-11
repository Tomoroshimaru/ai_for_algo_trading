# Deployment

> Step 16 (b). How to stand the platform up. See also `docs/environment.md`.

## 1. Environment setup
```bash
git clone <repo> && cd project
uv sync                      # runtime + dev deps (pinned in uv.lock)
uv sync --group frontend     # operator console deps (optional)
cp .env.example .env         # fill IBKR host/port/clientId, paths
uv run pytest -q             # must be green
```
Config precedence: env vars > `.env` > `configs/settings.toml` (see
`utils/config.py`). Thresholds, calendars and scenario grids live under `configs/`.

## 2. IBKR gateway
IB Gateway or TWS must run with the API enabled (read-only API is enough for
collection). Validate with the smoke test (RB-1). Clock skew beyond the
configured tolerance fails the smoke test by design.

## 3. Scheduling (wiring the jobs)
The `JobRunner` is the execution contract (retries, ledger, idempotence); it is
**not** itself a scheduler. Wire it to host scheduling:
- **Collector** (`run_collector.py`): long-running, supervised (systemd/
  supervisor) with auto-restart; one process per market session.
- **Daily analytics / QC / reconciliation**: cron (or Airflow/Prefect) calling
  the job via `JobRunner.run(...)` after the close. Idempotent SKIP means a
  retried cron tick is safe.
- **Replay/backfill**: on demand (RB-5).
> KNOWN GAP: no scheduler is committed in-repo yet (see LIMITATIONS).

## 4. Heartbeat -> collector_death alert
`alerts.evaluate_alerts` consumes a `collector_last_heartbeat`. Wire the
collector to emit a periodic heartbeat row to `ops_metrics`
(`scope='collector'`, `metric_name='heartbeat'`) and feed the latest value in.
> KNOWN GAP: the collector does not emit this row yet (see LIMITATIONS).

## 5. Operator console
```bash
uv run uvicorn app.main:app --reload    # http://127.0.0.1:8000
```
Local-only; renders health, universe and surface views from stored data.
