"""Step 14 - validation framework & anomaly detection (offline)."""
import datetime as dt

import pandas as pd

from pipeline.daily import run_day, RunResult
from iv.inversion import black76_price
from validation.framework import (
    run_validation, summarize, triage_view, ValidationThresholds, RC,
)
from validation.anomaly import detect_anomaly, detect_metric_anomalies, AnomalyParams

UTC = dt.timezone.utc
TS = dt.datetime(2026, 6, 11, 14, 0, tzinfo=UTC)
DATE = "2026-06-11"
EXP = "20261218"


def _clean_snapshot():
    import math
    spot = 100.0; F = spot + 5.0
    Tt = (dt.date(2026, 12, 18) - TS.date()).days / 365.0
    def vol(K):                          # genuinely convex smile in log-moneyness
        k = math.log(K / F)
        return 0.20 + 0.5 * k * k
    def row(key, b, a, ref=None):
        m = (b + a) / 2
        return {"snapshot_ts": TS, "underlying": "SPY", "instrument_key": key,
                "bid": b, "ask": a, "last": m, "mid": m,
                "spread_pct": round((a - b) / max(m, .5) * 100, 3),
                "reference_price": ref, "reference_type": "last", "is_stale": False,
                "is_market_open": True, "age_sec": 1.0, "source_session_id": "s1"}
    rows = [row("STK:SPY:USD", spot - .05, spot + .05, spot)]
    for K in (90, 95, 100, 105, 110):
        for r in ("C", "P"):
            px = black76_price(F, K, Tt, vol(K), r)
            rows.append(row(f"OPT:SPY:{EXP}:{K}:{r}", px - .1, px + .1))
    return pd.DataFrame(rows)


def _empty(ds):
    from storage.schemas import get_dataset
    return get_dataset(ds).schema.empty_table().to_pandas()


def test_clean_run_passes():
    res = run_day(_clean_snapshot(), "s1")
    v = run_validation(res, DATE, "s1", market_state=_clean_snapshot())
    s = summarize(v)
    assert s["FAIL"] == 0
    assert s["overall"] in ("PASS", "WARN")
    # every failure (if any) carries a reason code
    assert v[v["status"] != "PASS"]["reason_code"].notna().all()


def test_stale_data_rate_fails_with_context():
    ms = _clean_snapshot()
    ms["is_stale"] = True                     # 100% stale -> FAIL
    res = run_day(ms, "s1")
    v = run_validation(res, DATE, "s1", market_state=ms)
    row = v[v["check_name"] == "stale_data_rate"].iloc[0]
    assert row["status"] == "FAIL" and row["reason_code"] == RC["stale"]
    assert "stale" in row["detail"] and row["metric_value"] == 1.0


def test_coverage_and_solver_fail_on_thin_slice():
    iv = _empty("iv_points")
    iv.loc[0] = {**{c: None for c in iv.columns}}
    iv.loc[0, ["underlying", "expiry", "status"]] = ["SPY", EXP, "solved"]
    iv.loc[1] = {**{c: None for c in iv.columns}}
    iv.loc[1, ["underlying", "expiry", "status"]] = ["SPY", EXP, "no_solution"]
    res = RunResult(forwards=_empty("forwards"), forward_diagnostics=_empty("forward_diagnostics"),
                    iv_points=iv, surface_params=_empty("surface_params"),
                    surface_grid=_empty("surface_grid"))
    v = run_validation(res, DATE, "s1")
    cov = v[v["check_name"] == "coverage"].iloc[0]
    conv = v[v["check_name"] == "solver_convergence"].iloc[0]
    assert cov["status"] == "FAIL" and cov["reason_code"] == RC["coverage"]
    assert conv["status"] == "FAIL" and conv["reason_code"] == RC["solver"]
    assert EXP in cov["target"] or cov["target"] == EXP   # maturity identified


def test_forward_residual_blowout_fails():
    fd = _empty("forward_diagnostics")
    fd.loc[0] = {**{c: None for c in fd.columns}}
    fd.loc[0, ["underlying", "expiry", "residual", "quality_label"]] = ["SPY", EXP, 5.0, "ok"]
    res = RunResult(forwards=_empty("forwards"), forward_diagnostics=fd,
                    iv_points=_empty("iv_points"), surface_params=_empty("surface_params"),
                    surface_grid=_empty("surface_grid"))
    v = run_validation(res, DATE, "s1")
    row = v[v["check_name"] == "forward_stability"].iloc[0]
    assert row["status"] == "FAIL" and row["reason_code"] == RC["fwd"]
    assert row["target"] == EXP and row["metric_value"] == 5.0


def test_butterfly_arbitrage_fails():
    sp = _empty("surface_params")
    sp.loc[0] = {**{c: None for c in sp.columns}}
    sp.loc[0, ["underlying", "expiry", "param_name", "param_value"]] = ["SPY", EXP, "butterfly_ok", 0.0]
    res = RunResult(forwards=_empty("forwards"), forward_diagnostics=_empty("forward_diagnostics"),
                    iv_points=_empty("iv_points"), surface_params=sp,
                    surface_grid=_empty("surface_grid"))
    v = run_validation(res, DATE, "s1")
    row = v[v["check_name"] == "no_arbitrage_butterfly"].iloc[0]
    assert row["status"] == "FAIL" and row["reason_code"] == RC["butterfly"] and row["target"] == EXP


def test_reconciliation_breach_fails_with_greek_context():
    rc = _empty("risk_recon")
    rc.loc[0] = {**{c: None for c in rc.columns}}
    rc.loc[0, ["underlying", "instrument_key", "greek", "diff", "abs_diff", "threshold", "breach"]] = \
        ["SPY", "OPT:SPY:20261218:100:C", "delta", 0.15, 0.15, 0.01, True]
    res = RunResult(forwards=_empty("forwards"), forward_diagnostics=_empty("forward_diagnostics"),
                    iv_points=_empty("iv_points"), surface_params=_empty("surface_params"),
                    surface_grid=_empty("surface_grid"), risk_recon=rc)
    v = run_validation(res, DATE, "s1")
    row = v[v["check_name"] == "reconciliation"].iloc[0]
    assert row["status"] == "FAIL" and row["reason_code"] == RC["recon"]
    assert "delta" in row["detail"]


def test_triage_view_fail_first_and_specific():
    ms = _clean_snapshot(); ms["is_stale"] = True
    res = run_day(ms, "s1")
    v = run_validation(res, DATE, "s1", market_state=ms)
    tri = triage_view(v)
    assert len(tri) >= 1
    assert tri.iloc[0]["status"] == "FAIL"          # FAIL sorted first
    assert (tri["status"] != "PASS").all()           # only actionable rows
    assert tri["reason_code"].notna().all()          # every row has a reason


# ---- anomaly detection (task c) --------------------------------------------
def test_anomaly_insufficient_baseline():
    r = detect_anomaly([10, 11, 12], 100.0, AnomalyParams(min_baseline=5))
    assert r["is_anomaly"] is False and r["zscore"] is None and r["n_baseline"] == 3


def test_anomaly_flagged_on_collapse():
    hist = [1000, 1010, 990, 1005, 995, 1002]    # stable quote count ~1000
    normal = detect_anomaly(hist, 1001.0)
    collapse = detect_anomaly(hist, 200.0)        # quote count collapse
    assert normal["is_anomaly"] is False
    assert collapse["is_anomaly"] is True and collapse["zscore"] < -3


def test_detect_metric_anomalies_frame():
    hist = pd.DataFrame([{"run_date": f"2026-05-{d:02d}", "underlying": "SPY",
                          "metric_name": "quote_count", "value": 1000 + d}
                         for d in range(1, 8)])
    cur = {("SPY", "quote_count"): 150.0}
    out = detect_metric_anomalies(hist, cur, DATE, "s1")
    assert len(out) == 1 and bool(out.iloc[0]["is_anomaly"]) is True
    assert "baseline" in out.iloc[0]["detail"]
