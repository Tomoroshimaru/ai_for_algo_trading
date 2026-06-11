"""Offline tests for the scenario engine (Step 12)."""
import datetime as dt

import pandas as pd
import pytest

from risk.analytics import compute_risk
from scenario.engine import (
    build_grid, grid_to_frame, run_scenarios, top_contributors, worst_case, Scenario,
)
from pricing.engine import CALL, PUT
from storage.parquet_store import ParquetStore

UTC = dt.timezone.utc
TS = dt.datetime(2026, 6, 11, 14, 0, tzinfo=UTC)
EXP = "2026-12-18"


def _book():
    ms = pd.DataFrame([{"snapshot_ts": TS, "underlying": "SPY",
                        "instrument_key": "STK:SPY:USD", "reference_price": 100.0}])
    iv = pd.DataFrame([
        {"snapshot_ts": TS, "underlying": "SPY", "expiry": EXP, "strike": 100.0,
         "option_right": CALL, "iv": 0.2, "status": "solved"},
        {"snapshot_ts": TS, "underlying": "SPY", "expiry": EXP, "strike": 95.0,
         "option_right": PUT, "iv": 0.22, "status": "solved"},
    ])
    pos = pd.DataFrame([
        {"as_of_ts": TS, "account": "D", "underlying": "SPY",
         "instrument_key": "OPT:SPY:20261218:100:C", "quantity": 10.0, "avg_cost": 5.0},
        {"as_of_ts": TS, "account": "D", "underlying": "SPY",
         "instrument_key": "OPT:SPY:20261218:95:P", "quantity": -5.0, "avg_cost": 3.0},
        {"as_of_ts": TS, "account": "D", "underlying": "SPY",
         "instrument_key": "STK:SPY:USD", "quantity": 100.0, "avg_cost": 90.0},
    ])
    line, _, _ = compute_risk(pos, ms, iv, "s1", TS)
    return line


def test_grid_versioned_and_unknown_rejected():
    g = build_grid("v1")
    assert len(g) > 10 and any(s.family == "joint" for s in g)
    with pytest.raises(ValueError):
        build_grid("v2")


def test_base_scenario_zero_pnl():
    line = _book()
    res, summ = run_scenarios(line, build_grid("v1"), "v1", "s1", TS)
    base = summ[(summ["group_key"] == "portfolio") & (summ["scenario_id"] == "base")].iloc[0]
    assert abs(base["pnl_full"]) < 1e-6


def test_regeneration_is_exact():
    line = _book()
    a = run_scenarios(line, build_grid("v1"), "v1", "s1", TS)[1]
    b = run_scenarios(line, build_grid("v1"), "v1", "s1", TS)[1]
    pd.testing.assert_frame_equal(a, b)


def test_full_vs_greeks_agree_small_shock():
    line = _book()
    res, _ = run_scenarios(line, build_grid("v1"), "v1", "s1", TS)
    small = res[res["scenario_id"] == "spot_+0.05"]
    port_full = small["pnl_full"].sum()
    port_greeks = small["pnl_greeks"].sum()
    # within 2% of full PnL magnitude for a 5% spot move (2nd-order Taylor)
    assert abs(port_greeks - port_full) <= 0.02 * abs(port_full) + 1.0


def test_full_vs_greeks_diverge_large_shock():
    line = _book()
    res, _ = run_scenarios(line, build_grid("v1"), "v1", "s1", TS)
    big = res[res["scenario_id"] == "spot_-0.20"]
    # at -20% the Taylor approx is materially off vs full reprice (reference)
    assert abs(big["pnl_greeks"].sum() - big["pnl_full"].sum()) > 1e-6


def test_time_decay_sign_agrees_full_and_greeks():
    # Net-long-gamma book: rolling time forward must LOSE money in BOTH the full
    # reprice and the Greeks approximation (theta sign correctness).
    line = _book()
    res, _ = run_scenarios(line, build_grid("v1"), "v1", "s1", TS)
    t7 = res[res["scenario_id"] == "time_7d"]
    assert t7["pnl_full"].sum() < 0
    assert t7["pnl_greeks"].sum() < 0
    # and they agree in magnitude for a short roll
    assert abs(t7["pnl_greeks"].sum() - t7["pnl_full"].sum()) <= 0.5 * abs(t7["pnl_full"].sum()) + 1.0


def test_worst_case_is_explainable():
    line = _book()
    res, summ = run_scenarios(line, build_grid("v1"), "v1", "s1", TS)
    wc = worst_case(summ)
    assert wc is not None and wc["pnl_full"] < 0
    contrib = top_contributors(res, wc["scenario_id"], n=3)
    assert len(contrib) >= 1
    # sum of per-line PnL under worst scenario == portfolio worst pnl
    total = res[res["scenario_id"] == wc["scenario_id"]]["pnl_full"].sum()
    assert abs(total - wc["pnl_full"]) < 1e-6


def test_attribution_by_underlying_sums_to_portfolio():
    line = _book()
    _, summ = run_scenarios(line, build_grid("v1"), "v1", "s1", TS)
    sid = "spot_-0.10"
    port = summ[(summ["group_key"] == "portfolio") & (summ["scenario_id"] == sid)].iloc[0]
    und = summ[(summ["group_key"] == "underlying") & (summ["scenario_id"] == sid)]
    assert abs(und["pnl_full"].sum() - port["pnl_full"]) < 1e-6


def test_grid_and_results_persist(tmp_path):
    line = _book()
    grid = build_grid("v1")
    res, summ = run_scenarios(line, grid, "v1", "s1", TS)
    store = ParquetStore(tmp_path)
    store.write_partition("scenario_defs", "2026-06-11", "_GRID", grid_to_frame(grid, "v1"))
    for u, g in res.groupby("underlying"):
        store.write_partition("scenario_results", "2026-06-11", u, g)
    for u, g in summ.groupby("underlying"):
        store.write_partition("scenario_summary", "2026-06-11", u, g)
    defs = store.read("scenario_defs", trade_date="2026-06-11", underlying="_GRID")
    assert len(defs) == len(grid) and set(defs["version"]) == {"v1"}
    assert len(store.read("scenario_results", trade_date="2026-06-11", underlying="SPY")) > 0
