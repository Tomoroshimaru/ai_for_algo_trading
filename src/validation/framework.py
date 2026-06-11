"""Validation framework (Step 14).

Automated controls that decide whether a daily analytics run is trustworthy.
Validation is a PRODUCT, not a last-minute dashboard: every flag is SPECIFIC and
actionable - which underlying, which maturity, which metric, which threshold,
with a machine-readable reason_code and human context telling the operator where
to look next (junior note, cours.txt:945-948).

Checks (task a): coverage, stale-data rate, forward stability, solver
convergence, surface smoothness, no-arbitrage, reconciliation deltas. Each emits
validation_results rows (PASS/WARN/FAIL). The failed subset IS the triage table
(task d): filter status != PASS.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class ValidationThresholds:
    # coverage: min solved IV points per (underlying, expiry) slice
    min_points_per_slice_warn: int = 3
    min_points_per_slice_fail: int = 2
    # stale-data rate per underlying
    stale_rate_warn: float = 0.20
    stale_rate_fail: float = 0.50
    # forward parity residual (abs)
    fwd_residual_warn: float = 0.50
    fwd_residual_fail: float = 2.00
    # solver convergence: fraction solved per slice
    solver_conv_warn: float = 0.90
    solver_conv_fail: float = 0.70
    # surface smoothness: fit RMSE in vol points
    surface_rmse_warn: float = 0.010
    surface_rmse_fail: float = 0.030


# reason codes (machine-readable; stable for downstream routing/escalation)
RC = {
    "coverage": "COVERAGE_LOW",
    "stale": "STALE_HIGH",
    "fwd": "FWD_RESIDUAL_HIGH",
    "fwd_quality": "FWD_LOW_QUALITY",
    "solver": "SOLVER_NONCONV",
    "smooth": "SURFACE_ROUGH",
    "butterfly": "BUTTERFLY_ARB",
    "calendar": "CALENDAR_ARB",
    "recon": "RECON_BREACH",
}


def _tier(value: float, warn: float, fail: float, *, higher_is_worse: bool) -> str:
    if higher_is_worse:
        if value >= fail:
            return "FAIL"
        if value >= warn:
            return "WARN"
    else:
        if value <= fail:
            return "FAIL"
        if value <= warn:
            return "WARN"
    return "PASS"


class _Rows:
    def __init__(self, now, run_date, session_id):
        self.rows: list[dict] = []
        self._now, self._d, self._sid = now, run_date, session_id

    def add(self, underlying, target, check_name, status, reason_code,
            metric_value, threshold, detail):
        self.rows.append({
            "check_ts": self._now, "run_date": self._d, "underlying": underlying,
            "target": target, "check_name": check_name, "status": status,
            "reason_code": reason_code if status != "PASS" else None,
            "metric_value": float(metric_value) if metric_value is not None else None,
            "threshold": float(threshold) if threshold is not None else None,
            "detail": detail, "source_session_id": self._sid})


def _surface_metric(surface_params: pd.DataFrame, und: str, exp: str, name: str):
    if surface_params is None or len(surface_params) == 0:
        return None
    m = surface_params[(surface_params["underlying"] == und)
                       & (surface_params["expiry"] == exp)
                       & (surface_params["param_name"] == name)]
    return float(m["param_value"].iloc[0]) if len(m) else None


def run_validation(run_result, run_date: str, source_session_id: str, *,
                   market_state: pd.DataFrame | None = None,
                   thresholds: ValidationThresholds = ValidationThresholds()) -> pd.DataFrame:
    """Run all checks on a pipeline RunResult; return validation_results rows."""
    t = thresholds
    now = dt.datetime.now(dt.timezone.utc)
    R = _Rows(now, run_date, source_session_id)
    iv = run_result.iv_points
    fwd_diag = run_result.forward_diagnostics
    surf = run_result.surface_params

    # (1) stale-data rate per underlying -------------------------------------
    if market_state is not None and len(market_state) and "is_stale" in market_state:
        for und, g in market_state.groupby("underlying"):
            rate = float(g["is_stale"].mean())
            status = _tier(rate, t.stale_rate_warn, t.stale_rate_fail, higher_is_worse=True)
            R.add(und, "ALL", "stale_data_rate", status, RC["stale"], rate,
                  t.stale_rate_fail,
                  f"{rate:.0%} of {len(g)} quotes stale (warn>={t.stale_rate_warn:.0%})")

    # per-slice checks: coverage + solver convergence ------------------------
    if iv is not None and len(iv):
        for (und, exp), g in iv.groupby(["underlying", "expiry"]):
            n_solved = int((g["status"] == "solved").sum())
            n_total = int(len(g))
            # (2) coverage
            cov_status = _tier(n_solved, t.min_points_per_slice_warn,
                               t.min_points_per_slice_fail, higher_is_worse=False)
            R.add(und, exp, "coverage", cov_status, RC["coverage"], n_solved,
                  t.min_points_per_slice_fail,
                  f"{n_solved}/{n_total} solved IV points on {exp}")
            # (3) solver convergence
            conv = n_solved / n_total if n_total else 0.0
            conv_status = _tier(conv, t.solver_conv_warn, t.solver_conv_fail,
                                higher_is_worse=False)
            R.add(und, exp, "solver_convergence", conv_status, RC["solver"], conv,
                  t.solver_conv_fail,
                  f"{conv:.0%} solved on {exp} ({n_total - n_solved} failed)")

    # (4) forward stability per (underlying, expiry) -------------------------
    if fwd_diag is not None and len(fwd_diag) and "residual" in fwd_diag:
        for (und, exp), g in fwd_diag.groupby(["underlying", "expiry"]):
            resid_series = pd.to_numeric(g["residual"], errors="coerce").abs()
            if resid_series.notna().sum() == 0:
                continue                      # no usable residual for this slice
            resid = float(resid_series.max())
            status = _tier(resid, t.fwd_residual_warn, t.fwd_residual_fail,
                           higher_is_worse=True)
            R.add(und, exp, "forward_stability", status, RC["fwd"], resid,
                  t.fwd_residual_fail,
                  f"max |parity residual|={resid:.3f} on {exp}")
            if "quality_label" in g and (g["quality_label"] == "fallback").any():
                R.add(und, exp, "forward_quality", "WARN", RC["fwd_quality"], None,
                      None, f"forward used a fallback method on {exp}")

    # (5/6) surface smoothness + no-arbitrage per slice ----------------------
    if surf is not None and len(surf):
        for (und, exp) in surf[["underlying", "expiry"]].drop_duplicates().itertuples(index=False):
            rmse = _surface_metric(surf, und, exp, "rmse_iv")
            if rmse is not None:
                status = _tier(rmse, t.surface_rmse_warn, t.surface_rmse_fail,
                               higher_is_worse=True)
                R.add(und, exp, "surface_smoothness", status, RC["smooth"], rmse,
                      t.surface_rmse_fail, f"fit RMSE={rmse:.4f} vol pts on {exp}")
            bok = _surface_metric(surf, und, exp, "butterfly_ok")
            if bok is not None and bok < 1.0:
                R.add(und, exp, "no_arbitrage_butterfly", "FAIL", RC["butterfly"],
                      bok, 1.0, f"non-convex variance (butterfly arb) on {exp}")
    # calendar arb from surface diagnostics
    surf_diag = (run_result.diagnostics or {}).get("surface", {})
    for sl in surf_diag.get("slices", []):
        for w in sl.get("warnings", []):
            if "CALENDAR" in w:
                R.add(sl["underlying"], sl["expiry"], "no_arbitrage_calendar", "FAIL",
                      RC["calendar"], None, None,
                      f"calendar arbitrage flagged on {sl['expiry']}")

    # (7) reconciliation deltas vs broker ------------------------------------
    recon = run_result.risk_recon
    if recon is not None and len(recon) and "breach" in recon:
        for (und, key), g in recon.groupby(["underlying", "instrument_key"]):
            breaches = g[g["breach"]]
            if len(breaches):
                worst = breaches.loc[breaches["abs_diff"].idxmax()]
                R.add(und, key, "reconciliation", "FAIL", RC["recon"],
                      float(worst["abs_diff"]), float(worst["threshold"]),
                      f"{worst['greek']} diff={worst['diff']:+.4f} vs broker on {key}")

    cols = ["check_ts", "run_date", "underlying", "target", "check_name", "status",
            "reason_code", "metric_value", "threshold", "detail", "source_session_id"]
    return pd.DataFrame(R.rows, columns=cols)


def summarize(validation: pd.DataFrame) -> dict:
    """Daily pass/warn/fail roll-up (task b)."""
    if len(validation) == 0:
        return {"PASS": 0, "WARN": 0, "FAIL": 0, "overall": "PASS"}
    counts = validation["status"].value_counts().to_dict()
    out = {k: int(counts.get(k, 0)) for k in ("PASS", "WARN", "FAIL")}
    out["overall"] = "FAIL" if out["FAIL"] else ("WARN" if out["WARN"] else "PASS")
    return out


def triage_view(validation: pd.DataFrame) -> pd.DataFrame:
    """Failed/warning records with context, sorted FAIL-first (task d / acceptance).

    Lets an operator see which underlying + maturity failed and why, in minutes.
    """
    if len(validation) == 0:
        return validation
    bad = validation[validation["status"] != "PASS"].copy()
    order = {"FAIL": 0, "WARN": 1}
    bad["_o"] = bad["status"].map(order)
    return bad.sort_values(["_o", "underlying", "target"]).drop(columns="_o").reset_index(drop=True)
