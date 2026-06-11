"""Offline tests for the surface engine (Step 9)."""
import datetime as dt
import math

import numpy as np
import pandas as pd

from surface.engine import (
    fit_surface, fit_slice, total_var_at, slice_comparison, SurfaceParams, MODEL,
)
from storage.parquet_store import ParquetStore

UTC = dt.timezone.utc
T = dt.datetime(2026, 6, 11, 14, 0, tzinfo=UTC)


def _iv_points(slices):
    """slices: list of (expiry, ttm, a, b, c). iv from w=a+bk+ck^2."""
    rows = []
    ks = np.linspace(-0.15, 0.15, 9)
    for expiry, ttm, a, b, c in slices:
        for k in ks:
            w = a + b * k + c * k * k
            iv = math.sqrt(w / ttm)
            rows.append({"snapshot_ts": T, "underlying": "SPY", "expiry": expiry,
                         "strike": 100 * math.exp(k), "option_right": "C",
                         "iv": iv, "moneyness": float(k), "forward": 100.0,
                         "delta": 0.5, "ttm_years": ttm, "method": "black76",
                         "status": "solved"})
    return pd.DataFrame(rows)


def _val(params, expiry, name):
    m = params[(params.expiry == expiry) & (params.param_name == name)]
    return float(m["param_value"].iloc[0])


def test_reproduces_known_smile_within_tol():
    ivp = _iv_points([("2026-09-18", 0.25, 0.02, 0.0, 0.5)])
    params, grid, diag = fit_surface(ivp, "s1")
    assert _val(params, "2026-09-18", "max_err_iv") < 1e-6
    assert abs(_val(params, "2026-09-18", "c") - 0.5) < 1e-9
    assert diag["slices"][0]["warnings"] == []


def test_determinism_identical_params():
    ivp = _iv_points([("2026-09-18", 0.25, 0.02, 0.1, 0.5)])
    a = fit_surface(ivp, "s1")[0]
    b = fit_surface(ivp, "s1")[0]
    pd.testing.assert_frame_equal(a, b)


def test_grid_reconstructed():
    ivp = _iv_points([("2026-09-18", 0.25, 0.02, 0.0, 0.5)])
    _, grid, _ = fit_surface(ivp, "s1", SurfaceParams(n_grid=21))
    assert len(grid) == 21 and set(grid["model"]) == {MODEL}


def test_sparse_slice_flagged():
    rows = _iv_points([("2026-09-18", 0.25, 0.02, 0.0, 0.5)]).iloc[:2]  # 2 points
    params, _, diag = fit_surface(rows, "s1")
    assert "SPARSE_SLICE" in diag["slices"][0]["warnings"]


def test_nonconvex_flagged():
    ivp = _iv_points([("2026-09-18", 0.25, 0.05, 0.0, -0.2)])  # concave in variance
    params, _, diag = fit_surface(ivp, "s1")
    assert "NONCONVEX_VARIANCE" in diag["slices"][0]["warnings"]
    assert _val(params, "2026-09-18", "butterfly_ok") == 0.0


def test_calendar_violation_flagged():
    # ATM variance decreases with maturity -> violation
    ivp = _iv_points([("2026-09-18", 0.25, 0.05, 0.0, 0.4),
                      ("2026-12-18", 0.50, 0.02, 0.0, 0.4)])
    _, _, diag = fit_surface(ivp, "s1")
    far = [s for s in diag["slices"] if s["expiry"] == "2026-12-18"][0]
    assert "CALENDAR_VIOLATION" in far["warnings"]


def test_no_calendar_violation_when_increasing():
    ivp = _iv_points([("2026-09-18", 0.25, 0.02, 0.0, 0.4),
                      ("2026-12-18", 0.50, 0.05, 0.0, 0.4)])
    _, _, diag = fit_surface(ivp, "s1")
    assert all("CALENDAR_VIOLATION" not in s["warnings"] for s in diag["slices"])


def test_cross_maturity_interpolation_in_variance():
    ivp = _iv_points([("2026-09-18", 0.25, 0.02, 0.0, 0.4),
                      ("2026-12-18", 0.50, 0.06, 0.0, 0.4)])
    _, _, diag = fit_surface(ivp, "s1")
    from surface.engine import fit_slice as _fs
    # rebuild fits to interpolate
    fits = []
    for s in [("2026-09-18", 0.25), ("2026-12-18", 0.50)]:
        g = ivp[ivp.expiry == s[0]]
        fits.append(_fs("SPY", s[0], s[1], g["moneyness"].to_numpy(float),
                        g["iv"].to_numpy(float), SurfaceParams()))
    w_mid = total_var_at(fits, "SPY", 0.0, 0.375)   # halfway in T
    assert abs(w_mid - 0.04) < 1e-6                  # linear midpoint of 0.02 and 0.06


def test_raw_points_preserved_for_operator():
    ivp = _iv_points([("2026-09-18", 0.25, 0.02, 0.0, 0.5)])
    _, grid, _ = fit_surface(ivp, "s1")
    raw, fit = slice_comparison(grid, ivp, "SPY", "2026-09-18")
    assert len(raw) == 9 and "iv_raw" in raw.columns and "iv_fit" in fit.columns


def test_persist_params_and_grid(tmp_path):
    ivp = _iv_points([("2026-09-18", 0.25, 0.02, 0.0, 0.5)])
    params, grid, _ = fit_surface(ivp, "s1")
    store = ParquetStore(tmp_path)
    store.write_partition("surface_params", "2026-06-11", "SPY", params)
    store.write_partition("surface_grid", "2026-06-11", "SPY", grid)
    pb = store.read("surface_params", trade_date="2026-06-11", underlying="SPY")
    gb = store.read("surface_grid", trade_date="2026-06-11", underlying="SPY")
    assert "c" in set(pb["param_name"]) and len(gb) == 21
