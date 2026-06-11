"""System-health / dashboard summary (Step 15 task f / acceptance).

Answers the four operator questions at a glance and exposes the last healthy run
per job plus the current backlog (expected but missing run dates), so an operator
can identify the last good run and outstanding work instantly.
"""
from __future__ import annotations

import pandas as pd


def last_healthy_runs(job_ledger: pd.DataFrame) -> pd.DataFrame:
    """Most recent SUCCEEDED run per job (the 'last healthy run')."""
    if job_ledger is None or len(job_ledger) == 0:
        return pd.DataFrame(columns=["job_name", "run_date", "ended_ts", "duration_sec"])
    ok = job_ledger[job_ledger["status"] == "SUCCEEDED"]
    if len(ok) == 0:
        return pd.DataFrame(columns=["job_name", "run_date", "ended_ts", "duration_sec"])
    idx = ok.sort_values(["run_date", "ended_ts"]).groupby("job_name").tail(1)
    return idx[["job_name", "run_date", "ended_ts", "duration_sec"]].reset_index(drop=True)


def backlog(job_ledger: pd.DataFrame, job_name: str, expected_dates: list[str]) -> list[str]:
    """Expected run dates with no SUCCEEDED run for `job_name` (current backlog)."""
    if job_ledger is None or len(job_ledger) == 0:
        return list(expected_dates)
    ok = job_ledger[(job_ledger["job_name"] == job_name)
                    & (job_ledger["status"] == "SUCCEEDED")]
    done = set(ok["run_date"]) if len(ok) else set()
    return [d for d in expected_dates if d not in done]


def health_summary(*, run_date: str, metrics: pd.DataFrame | None = None,
                   job_ledger: pd.DataFrame | None = None,
                   alerts: pd.DataFrame | None = None) -> dict:
    """One-glance health dict for the dashboard."""
    def metric(name):
        if metrics is None or len(metrics) == 0:
            return None
        m = metrics[metrics["metric_name"] == name]
        return float(m["value"].iloc[-1]) if len(m) else None

    quote_count = metric("quote_count")
    stale_ratio = metric("stale_ratio")
    surfaces = metric("surfaces_built")
    solver_fail = metric("solver_failures")
    n_alerts = 0 if alerts is None else int((alerts["status"] == "FIRING").sum()) if len(alerts) else 0
    return {
        "run_date": run_date,
        "data_flowing": bool(quote_count and quote_count > 0),
        "quote_count": quote_count,
        "stale_ratio": stale_ratio,
        "surfaces_building": bool(surfaces and surfaces > 0),
        "surfaces_built": surfaces,
        "solver_failures": solver_fail,
        "firing_alerts": n_alerts,
        "last_healthy_runs": last_healthy_runs(job_ledger).to_dict("records")
        if job_ledger is not None else [],
    }
