"""Job framework: scheduling unit, retries, ledger, idempotent restart (Step 15).

Jobs (task a): universe refresh, live collection, incremental analytics, EOD
reconciliation, replay, QC. Each is just a named callable run through `JobRunner`,
which provides uniform retries, structured logging with a correlation_id, and a
durable run ledger (`job_runs`).

Idempotent restart (task e / acceptance): before executing, the runner checks the
ledger for a prior SUCCEEDED run of (job_name, run_date). If found and not forced,
it SKIPS - so restarting a failed/relaunched pipeline never silently re-executes
or duplicates outputs. Combined with ParquetStore's single-partition overwrite
semantics, outputs cannot be duplicated even on a forced rerun.
"""
from __future__ import annotations

import datetime as dt
import time
import uuid
from dataclasses import dataclass
from typing import Callable

import pandas as pd

from orchestration.logging_ctx import StructuredLogger, new_correlation_id
from storage.parquet_store import ParquetStore

_LEDGER_COLS = ["run_id", "job_name", "run_date", "attempt", "status",
                "started_ts", "ended_ts", "duration_sec", "correlation_id",
                "rows_out", "error", "detail"]


@dataclass
class JobResult:
    job_name: str
    run_date: str
    status: str            # SUCCEEDED | FAILED | SKIPPED
    attempt: int
    correlation_id: str
    rows_out: int | None = None
    error: str | None = None
    duration_sec: float | None = None
    value: object = None   # the job callable's return value (when SUCCEEDED)


@dataclass
class Job:
    name: str
    func: Callable[..., object]    # returns int rows_out, or (rows_out, value)
    max_retries: int = 2
    retry_backoff_sec: float = 0.0  # 0 in tests; real schedules set >0


class JobRunner:
    def __init__(self, store: ParquetStore, *, clock: Callable[[], dt.datetime] | None = None):
        self.store = store
        self._now = clock or (lambda: dt.datetime.now(dt.timezone.utc))

    # -- ledger helpers ------------------------------------------------------
    def _read_ledger(self, run_date: str, job_name: str) -> pd.DataFrame:
        return self.store.read("job_runs", trade_date=run_date, underlying=job_name)

    def _append_ledger(self, row: dict) -> None:
        existing = self._read_ledger(row["run_date"], row["job_name"])
        new = pd.DataFrame([row], columns=_LEDGER_COLS)
        out = (pd.concat([existing[_LEDGER_COLS], new], ignore_index=True)
               if len(existing) else new)
        self.store.write_partition("job_runs", row["run_date"], row["job_name"], out)

    def last_successful(self, run_date: str, job_name: str):
        led = self._read_ledger(run_date, job_name)
        ok = led[led["status"] == "SUCCEEDED"] if len(led) else led
        return None if len(ok) == 0 else ok.sort_values("attempt").iloc[-1]

    # -- execution -----------------------------------------------------------
    def run(self, job: Job, run_date: str, *, correlation_id: str | None = None,
            force: bool = False, **kwargs) -> JobResult:
        cid = correlation_id or new_correlation_id(job.name)
        log = StructuredLogger("orchestration", job_name=job.name,
                               run_date=run_date, correlation_id=cid)

        if not force and self.last_successful(run_date, job_name=job.name) is not None:
            log.info("job_skipped_already_succeeded")
            return JobResult(job.name, run_date, "SKIPPED", 0, cid)

        prior = self._read_ledger(run_date, job.name)
        attempt0 = (int(prior["attempt"].max()) + 1) if len(prior) else 1

        last_err = None
        for i in range(job.max_retries + 1):
            attempt = attempt0 + i
            started = self._now()
            log.info("job_started", attempt=attempt)
            try:
                ret = job.func(correlation_id=cid, run_date=run_date, **kwargs)
                rows_out, value = ret if isinstance(ret, tuple) else (ret, None)
                ended = self._now()
                dur = (ended - started).total_seconds()
                self._append_ledger({
                    "run_id": uuid.uuid4().hex, "job_name": job.name, "run_date": run_date,
                    "attempt": attempt, "status": "SUCCEEDED", "started_ts": started,
                    "ended_ts": ended, "duration_sec": dur, "correlation_id": cid,
                    "rows_out": int(rows_out) if rows_out is not None else None,
                    "error": None, "detail": None})
                log.info("job_succeeded", attempt=attempt, rows_out=rows_out, duration_sec=dur)
                return JobResult(job.name, run_date, "SUCCEEDED", attempt, cid,
                                 rows_out=int(rows_out) if rows_out is not None else None,
                                 duration_sec=dur, value=value)
            except Exception as exc:           # noqa: BLE001 - record any job failure
                ended = self._now()
                dur = (ended - started).total_seconds()
                last_err = f"{type(exc).__name__}: {exc}"
                self._append_ledger({
                    "run_id": uuid.uuid4().hex, "job_name": job.name, "run_date": run_date,
                    "attempt": attempt, "status": "FAILED", "started_ts": started,
                    "ended_ts": ended, "duration_sec": dur, "correlation_id": cid,
                    "rows_out": None, "error": last_err, "detail": None})
                log.error("job_failed", attempt=attempt, error=last_err)
                if i < job.max_retries and job.retry_backoff_sec:
                    time.sleep(job.retry_backoff_sec)
        return JobResult(job.name, run_date, "FAILED", attempt0 + job.max_retries, cid,
                         error=last_err)
