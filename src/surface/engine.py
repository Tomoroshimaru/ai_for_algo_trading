"""Volatility surface engine and parameter storage (Step 9).

Raw solved IV points are fitted slice-by-slice in TOTAL-VARIANCE space against
log-moneyness, then interpolated across maturities in variance space. Raw points
are never discarded (junior note): the fitted form and the exact calibration
inputs both remain queryable so operators can debug suspicious Greeks later.

Parameterization choice: per-slice weighted least squares of total variance on
[1, k, k^2]  ->  w(k) = a + b*k + c*k^2  (k = log(K/F), w = iv^2 * T).
This closed-form fit is chosen over nonlinear SVI specifically because the
acceptance criterion demands that repeated runs return identical parameters; an
ordinary least-squares solution is exactly reproducible, whereas a nonlinear SVI
fit without a solver library risks non-deterministic local minima. SVI is a
documented future upgrade. No-arbitrage diagnostics (convexity, positivity,
calendar monotonicity) are reported so a poor parameterization is visible.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

MODEL = "poly2_totalvar"


@dataclass(frozen=True)
class SurfaceParams:
    n_grid: int = 21          # log-moneyness grid points for reconstruction
    min_points: int = 3       # need >=3 for a quadratic slice
    fit_tol_iv: float = 5e-3  # acceptance tolerance on reproducing market IV


@dataclass
class SliceFit:
    underlying: str
    expiry: str
    ttm: float
    a: float
    b: float
    c: float
    n_points: int
    rmse_iv: float
    max_err_iv: float
    k_min: float
    k_max: float
    warnings: list[str] = field(default_factory=list)

    def total_var(self, k: float) -> float:
        return self.a + self.b * k + self.c * k * k

    def iv(self, k: float) -> float:
        w = self.total_var(k)
        return math.sqrt(w / self.ttm) if (w > 0 and self.ttm > 0) else float("nan")


def _fit_quadratic(k: np.ndarray, w: np.ndarray) -> tuple[float, float, float]:
    """Closed-form least squares for w = a + b k + c k^2 (deterministic)."""
    deg = 2 if len(k) >= 3 else (1 if len(k) >= 2 else 0)
    X = np.vander(k, N=deg + 1, increasing=True)  # columns [1, k, k^2]
    coef, *_ = np.linalg.lstsq(X, w, rcond=None)
    a = float(coef[0])
    b = float(coef[1]) if deg >= 1 else 0.0
    c = float(coef[2]) if deg >= 2 else 0.0
    return a, b, c


def fit_slice(underlying: str, expiry: str, ttm: float,
              k: np.ndarray, iv: np.ndarray, p: SurfaceParams) -> SliceFit:
    warnings: list[str] = []
    order = np.argsort(k)
    k, iv = k[order], iv[order]
    w = iv * iv * ttm
    if len(k) < p.min_points:
        warnings.append("SPARSE_SLICE")
    a, b, c = _fit_quadratic(k, w)
    fit = SliceFit(underlying, expiry, ttm, a, b, c, len(k), 0.0, 0.0,
                   float(k.min()), float(k.max()), warnings)
    iv_fit = np.array([fit.iv(kk) for kk in k])
    err = np.abs(iv_fit - iv)
    fit.rmse_iv = float(np.sqrt(np.mean(err ** 2)))
    fit.max_err_iv = float(np.max(err))
    # no-arbitrage / sanity diagnostics
    if c < 0:
        warnings.append("NONCONVEX_VARIANCE")
    w_fit = a + b * k + c * k * k
    if np.any(w_fit <= 0):
        warnings.append("NONPOSITIVE_VARIANCE")
    if fit.max_err_iv > p.fit_tol_iv:
        warnings.append("POOR_FIT")
    return fit


def fit_surface(iv_points: pd.DataFrame, source_session_id: str,
                p: SurfaceParams = SurfaceParams()) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Return (surface_params_df, surface_grid_df, diagnostics).

    Only `status == solved` points with finite iv/moneyness enter calibration;
    rejected points are counted per slice (never silently dropped).
    """
    df = iv_points.copy()
    solved = df[(df["status"] == "solved") & df["iv"].notna() & df["moneyness"].notna()]
    param_rows: list[dict] = []
    grid_rows: list[dict] = []
    fits: list[SliceFit] = []
    diagnostics = {"slices": [], "n_input": len(df), "n_solved": len(solved)}

    for (und, expiry), g in solved.groupby(["underlying", "expiry"]):
        ttm = float(g["ttm_years"].iloc[0])
        snap_ts = g["snapshot_ts"].iloc[0]
        n_rejected = int(((df["underlying"] == und) & (df["expiry"] == expiry)).sum() - len(g))
        fit = fit_slice(und, expiry, ttm, g["moneyness"].to_numpy(float),
                        g["iv"].to_numpy(float), p)
        fits.append(fit)
        diagnostics["slices"].append({
            "underlying": und, "expiry": expiry, "ttm": ttm, "n_points": fit.n_points,
            "n_rejected": n_rejected, "rmse_iv": fit.rmse_iv,
            "max_err_iv": fit.max_err_iv, "warnings": list(fit.warnings),
        })
        # tidy params + metrics + flags + warnings -> surface_params rows
        kv = {"a": fit.a, "b": fit.b, "c": fit.c, "ttm_years": ttm,
              "n_points": float(fit.n_points), "n_rejected": float(n_rejected),
              "rmse_iv": fit.rmse_iv, "max_err_iv": fit.max_err_iv,
              "butterfly_ok": 0.0 if "NONCONVEX_VARIANCE" in fit.warnings else 1.0}
        for warn in fit.warnings:
            kv[f"warning:{warn}"] = 1.0
        for name, val in kv.items():
            param_rows.append({"snapshot_ts": snap_ts, "underlying": und,
                               "expiry": expiry, "model": MODEL,
                               "param_name": name, "param_value": float(val),
                               "source_session_id": source_session_id})
        # reconstructed grid on a regular log-moneyness span
        if fit.k_max > fit.k_min:
            ks = np.linspace(fit.k_min, fit.k_max, p.n_grid)
            for kk in ks:
                w = fit.total_var(float(kk))
                grid_rows.append({"snapshot_ts": snap_ts, "underlying": und,
                                  "expiry": expiry, "log_moneyness": float(kk),
                                  "ttm_years": ttm, "total_variance": float(w),
                                  "iv": fit.iv(float(kk)), "model": MODEL,
                                  "source_session_id": source_session_id})

    # (d/e) calendar no-arbitrage across adjacent maturities at k=0 (ATM total var)
    _flag_calendar(fits, diagnostics)

    params_df = pd.DataFrame(param_rows, columns=[
        "snapshot_ts", "underlying", "expiry", "model", "param_name",
        "param_value", "source_session_id"]).sort_values(
        ["underlying", "expiry", "param_name"]).reset_index(drop=True)
    grid_df = pd.DataFrame(grid_rows, columns=[
        "snapshot_ts", "underlying", "expiry", "log_moneyness", "ttm_years",
        "total_variance", "iv", "model", "source_session_id"]).sort_values(
        ["underlying", "expiry", "log_moneyness"]).reset_index(drop=True)
    return params_df, grid_df, diagnostics


def _flag_calendar(fits: list[SliceFit], diagnostics: dict) -> None:
    """ATM total variance should be non-decreasing in maturity (no calendar arb)."""
    by_und: dict[str, list[SliceFit]] = {}
    for f in fits:
        by_und.setdefault(f.underlying, []).append(f)
    for und, slices in by_und.items():
        slices = sorted(slices, key=lambda s: s.ttm)
        prev = None
        for s in slices:
            atm = s.total_var(0.0)
            if prev is not None and atm < prev - 1e-12:
                s.warnings.append("CALENDAR_VIOLATION")
                for d in diagnostics["slices"]:
                    if d["underlying"] == und and d["expiry"] == s.expiry:
                        d["warnings"].append("CALENDAR_VIOLATION")
            prev = atm


def total_var_at(fits: list[SliceFit], underlying: str, k: float, ttm: float) -> float:
    """Interpolate total variance in maturity (linear in T) between fitted slices."""
    sl = sorted([f for f in fits if f.underlying == underlying], key=lambda s: s.ttm)
    if not sl:
        return float("nan")
    if ttm <= sl[0].ttm:
        return sl[0].total_var(k)
    if ttm >= sl[-1].ttm:
        return sl[-1].total_var(k)
    for lo, hi in zip(sl, sl[1:]):
        if lo.ttm <= ttm <= hi.ttm:
            wlo, whi = lo.total_var(k), hi.total_var(k)
            t = (ttm - lo.ttm) / (hi.ttm - lo.ttm)
            return (1 - t) * wlo + t * whi
    return sl[-1].total_var(k)


def slice_comparison(grid_df: pd.DataFrame, iv_points: pd.DataFrame,
                     underlying: str, expiry: str) -> pd.DataFrame:
    """Tidy raw-vs-fitted table for an operator plotting utility."""
    raw = iv_points[(iv_points["underlying"] == underlying) &
                    (iv_points["expiry"] == expiry) &
                    (iv_points["status"] == "solved")][["moneyness", "iv"]].rename(
        columns={"moneyness": "log_moneyness", "iv": "iv_raw"})
    fit = grid_df[(grid_df["underlying"] == underlying) &
                  (grid_df["expiry"] == expiry)][["log_moneyness", "iv"]].rename(
        columns={"iv": "iv_fit"})
    return raw, fit


def render_slice_png(grid_df, iv_points, underlying, expiry, path) -> str | None:
    """Render raw points vs fitted slice to PNG if matplotlib is available."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return None
    raw, fit = slice_comparison(grid_df, iv_points, underlying, expiry)
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.scatter(raw["log_moneyness"], raw["iv_raw"], c="k", label="raw solved", zorder=3)
    ax.plot(fit["log_moneyness"], fit["iv_fit"], "r-", label="fitted slice")
    ax.set_xlabel("log-moneyness  log(K/F)"); ax.set_ylabel("implied vol")
    ax.set_title(f"{underlying} {expiry} - raw vs fitted ({MODEL})")
    ax.legend(); fig.tight_layout(); fig.savefig(path, dpi=110); plt.close(fig)
    return path
