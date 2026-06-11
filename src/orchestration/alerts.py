"""Alert rules + routing (Step 15 task d; completes Step 14 task e).

Four alerts the operator actually needs: collector death, missing partitions,
elevated failure rates, and QC fails. Each fires with a severity and a route
(page/slack/email). Reason codes from the validation framework map straight to
routes here, so 'who gets notified' is explicit.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

# severity -> route (who gets notified)
_ROUTE = {"CRITICAL": "page", "WARN": "slack", "INFO": "email"}


def _alert(now, run_date, name, severity, detail, correlation_id=None, status="FIRING"):
    return {"alert_ts": now, "run_date": run_date, "alert_name": name,
            "severity": severity, "route": _ROUTE[severity], "status": status,
            "detail": detail, "correlation_id": correlation_id}


def evaluate_alerts(*, run_date: str, now: dt.datetime | None = None,
                    collector_last_heartbeat: dt.datetime | None = None,
                    heartbeat_timeout_sec: float = 120.0,
                    expected_partitions: list[str] | None = None,
                    present_partitions: list[str] | None = None,
                    job_ledger: pd.DataFrame | None = None,
                    failure_rate_warn: float = 0.25,
                    validation: pd.DataFrame | None = None,
                    correlation_id: str | None = None) -> pd.DataFrame:
    """Return alert rows (one per firing condition). Empty => all clear."""
    now = now or dt.datetime.now(dt.timezone.utc)
    rows: list[dict] = []

    # (1) collector death: heartbeat older than timeout (or never seen)
    if collector_last_heartbeat is None:
        rows.append(_alert(now, run_date, "collector_death", "CRITICAL",
                           "no collector heartbeat seen", correlation_id))
    else:
        age = (now - collector_last_heartbeat).total_seconds()
        if age > heartbeat_timeout_sec:
            rows.append(_alert(now, run_date, "collector_death", "CRITICAL",
                               f"last heartbeat {age:.0f}s ago (timeout {heartbeat_timeout_sec:.0f}s)",
                               correlation_id))

    # (2) missing partitions
    if expected_partitions is not None:
        present = set(present_partitions or [])
        missing = [p for p in expected_partitions if p not in present]
        if missing:
            rows.append(_alert(now, run_date, "missing_partitions", "CRITICAL",
                               f"{len(missing)} missing: {missing[:5]}", correlation_id))

    # (3) elevated failure rate in the job ledger (latest attempt per job)
    if job_ledger is not None and len(job_ledger):
        latest = (job_ledger.sort_values("attempt")
                  .groupby("job_name").tail(1))
        fail_rate = float((latest["status"] == "FAILED").mean())
        if fail_rate > failure_rate_warn:
            rows.append(_alert(now, run_date, "elevated_failure_rate", "WARN",
                               f"{fail_rate:.0%} of jobs failing latest attempt",
                               correlation_id))

    # (4) QC fails: any FAIL row in validation -> alert, FAIL-count drives severity
    if validation is not None and len(validation):
        fails = validation[validation["status"] == "FAIL"]
        if len(fails):
            worst = fails.iloc[0]
            sev = "CRITICAL" if len(fails) >= 3 else "WARN"
            rows.append(_alert(now, run_date, "qc_fail", sev,
                               f"{len(fails)} QC fail(s); e.g. {worst['underlying']}/"
                               f"{worst['target']} {worst['reason_code']}",
                               correlation_id))

    return pd.DataFrame(rows, columns=["alert_ts", "run_date", "alert_name",
                                       "severity", "route", "status", "detail",
                                       "correlation_id"])
