"""Few, well-labeled operational metrics (Step 15 task c / junior note).

Deliberately small: enough to answer the four operator questions - is data
flowing? are surfaces building? are QC checks passing? are scenarios current? -
before expanding. Each metric is one tidy row for the ops_metrics table.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd


def compute_run_metrics(run_result, *, run_date: str, correlation_id: str,
                        market_state: pd.DataFrame | None = None,
                        scenario_runtime_sec: float | None = None) -> pd.DataFrame:
    """Extract the headline metrics from one analytics run."""
    now = dt.datetime.now(dt.timezone.utc)
    iv = run_result.iv_points
    rows: list[dict] = []

    def add(scope, name, value, detail=None):
        rows.append({"metric_ts": now, "run_date": run_date, "scope": scope,
                     "metric_name": name,
                     "value": float(value) if value is not None else None,
                     "correlation_id": correlation_id, "detail": detail})

    # is data flowing? quote count + stale ratio
    if market_state is not None and len(market_state):
        add("system", "quote_count", len(market_state))
        if "is_stale" in market_state:
            add("system", "stale_ratio", float(market_state["is_stale"].mean()))
    # forward failures (quality_label == fallback)
    fd = run_result.forward_diagnostics
    if fd is not None and len(fd) and "quality_label" in fd:
        add("system", "forward_failures", int((fd["quality_label"] == "fallback").sum()))
    # are surfaces building? solver failures + n surfaces
    if iv is not None and len(iv):
        add("system", "solver_failures", int((iv["status"] != "solved").sum()))
    if run_result.surface_params is not None and len(run_result.surface_params):
        n_slices = run_result.surface_params[["underlying", "expiry"]].drop_duplicates().shape[0]
        add("system", "surfaces_built", n_slices)
    else:
        add("system", "surfaces_built", 0)
    # are scenarios current? runtime
    if scenario_runtime_sec is not None:
        add("system", "scenario_runtime_sec", scenario_runtime_sec)

    return pd.DataFrame(rows, columns=["metric_ts", "run_date", "scope",
                                       "metric_name", "value", "correlation_id", "detail"])
