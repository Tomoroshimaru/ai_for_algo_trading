"""Anomaly detection against rolling baselines (Step 14 task c).

Flags key daily metrics (quote counts, forward residuals, fit errors, scenario
losses) that deviate from their recent history by more than `z_threshold`
rolling standard deviations. Output feeds the qc_anomalies table for regression
monitoring and escalation.

The baseline excludes the current value (trailing window only) so a metric cannot
mask its own anomaly. Requires at least `min_baseline` history points; below that
no z-score is computed (reported as not-an-anomaly with n_baseline noted).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class AnomalyParams:
    z_threshold: float = 3.0
    min_baseline: int = 5


def detect_anomaly(history: list[float], value: float,
                   params: AnomalyParams = AnomalyParams()) -> dict:
    """Z-score of `value` vs the trailing `history` (which excludes `value`)."""
    n = len(history)
    if n < params.min_baseline:
        return {"value": float(value), "baseline_mean": None, "baseline_std": None,
                "zscore": None, "is_anomaly": False, "n_baseline": n}
    s = pd.Series(history, dtype=float)
    mean = float(s.mean()); std = float(s.std(ddof=1))
    if std == 0.0:
        z = 0.0 if value == mean else float("inf")
    else:
        z = (value - mean) / std
    return {"value": float(value), "baseline_mean": mean, "baseline_std": std,
            "zscore": z, "is_anomaly": abs(z) >= params.z_threshold,
            "n_baseline": n}


def detect_metric_anomalies(metric_history: pd.DataFrame, current: dict,
                            run_date: str, source_session_id: str, *,
                            params: AnomalyParams = AnomalyParams()) -> pd.DataFrame:
    """Detect anomalies for a batch of current metrics against history.

    `metric_history` is a long frame [run_date, underlying, metric_name, value]
    of PRIOR days. `current` maps (underlying, metric_name) -> value for today.
    Returns qc_anomalies rows.
    """
    now = dt.datetime.now(dt.timezone.utc)
    rows: list[dict] = []
    for (und, metric), value in current.items():
        h = metric_history[(metric_history["underlying"] == und)
                           & (metric_history["metric_name"] == metric)]
        hist = h.sort_values("run_date")["value"].astype(float).tolist() if len(h) else []
        r = detect_anomaly(hist, value, params)
        detail = (f"{metric}={value:.4g} vs baseline "
                  f"{r['baseline_mean']:.4g}+/-{r['baseline_std']:.4g} "
                  f"(z={r['zscore']:.2f}, n={r['n_baseline']})"
                  if r["zscore"] is not None else
                  f"{metric}={value:.4g} (insufficient baseline n={r['n_baseline']})")
        rows.append({"check_ts": now, "run_date": run_date, "underlying": und,
                     "metric_name": metric, "value": r["value"],
                     "baseline_mean": r["baseline_mean"], "baseline_std": r["baseline_std"],
                     "zscore": r["zscore"], "is_anomaly": r["is_anomaly"],
                     "n_baseline": int(r["n_baseline"]), "detail": detail,
                     "source_session_id": source_session_id})
    cols = ["check_ts", "run_date", "underlying", "metric_name", "value",
            "baseline_mean", "baseline_std", "zscore", "is_anomaly", "n_baseline",
            "detail", "source_session_id"]
    return pd.DataFrame(rows, columns=cols)
