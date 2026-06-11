"""Offline tests for per-position risk analytics (Step 11)."""
import datetime as dt
import math

import pandas as pd
import pytest

from risk.analytics import (
    compute_risk, compute_line_risk, aggregate_risk, reconcile_greeks, RiskParams,
)
from pricing.engine import european_price, CALL
from storage.parquet_store import ParquetStore

UTC = dt.timezone.utc
TS = dt.datetime(2026, 6, 11, 14, 0, tzinfo=UTC)
EXP = "2026-12-18"
TTM = (dt.date(2026, 12, 18) - TS.date()).days / 365.0


def _market_state(spot=100.0):
    return pd.DataFrame([{"snapshot_ts": TS, "underlying": "SPY",
                          "instrument_key": "STK:SPY:USD", "reference_price": spot}])


def _iv_points(vol=0.2):
    return pd.DataFrame([{"snapshot_ts": TS, "underlying": "SPY", "expiry": EXP,
                          "strike": 100.0, "option_right": CALL, "iv": vol,
                          "status": "solved"}])


def _positions(qty=10.0):
    return pd.DataFrame([{"as_of_ts": TS, "account": "U1", "underlying": "SPY",
                          "instrument_key": "OPT:SPY:20261218:100:C", "quantity": qty,
                          "avg_cost": 5.0}])


def test_line_price_matches_pricer_and_dollar_conventions():
    line, agg, _ = compute_risk(_positions(10), _market_state(100), _iv_points(0.2),
                                "s1", TS)
    r = line.iloc[0]
    px = european_price(100, 100, TTM, 0.2, CALL)
    assert abs(r["price"] - px) < 1e-9
    assert abs(r["position_value"] - px * 100 * 10) < 1e-6
    # dollar conventions
    assert abs(r["dollar_delta"] - r["delta"] * 100 * 100 * 10) < 1e-6
    assert abs(r["dollar_vega"] - (r["vega"] / 100) * 100 * 10) < 1e-9
    assert abs(r["dollar_theta"] - (r["theta"] / 365) * 100 * 10) < 1e-9
    assert abs(r["dollar_gamma"] - r["gamma"] * 100**2 * 100 * 10 * 0.01) < 1e-6


def test_determinism_same_inputs_same_aggregate():
    a = compute_risk(_positions(7), _market_state(100), _iv_points(0.2), "s1", TS)[1]
    b = compute_risk(_positions(7), _market_state(100), _iv_points(0.2), "s1", TS)[1]
    pd.testing.assert_frame_equal(a, b)


def test_aggregate_portfolio_equals_sum_of_lines():
    pos = pd.concat([_positions(10),
                     pd.DataFrame([{"as_of_ts": TS, "account": "U1", "underlying": "SPY",
                                    "instrument_key": "STK:SPY:USD", "quantity": 50.0,
                                    "avg_cost": 99.0}])], ignore_index=True)
    line, agg, _ = compute_risk(pos, _market_state(100), _iv_points(0.2), "s1", TS)
    port = agg[agg["group_key"] == "portfolio"].iloc[0]
    assert abs(port["dollar_delta"] - line["dollar_delta"].sum()) < 1e-6
    assert port["n_lines"] == 2
    # stock line contributes delta 1 * 50 shares
    stock = line[line["instrument_key"] == "STK:SPY:USD"].iloc[0]
    assert stock["delta"] == 1.0 and abs(stock["dollar_delta"] - 50 * 100) < 1e-9


def test_grouping_keys_present():
    line, agg, _ = compute_risk(_positions(10), _market_state(100), _iv_points(0.2),
                                "s1", TS)
    assert set(agg["group_key"]) == {"portfolio", "underlying", "expiry", "instrument"}


def test_missing_vol_flagged_not_priced():
    line, agg, _ = compute_risk(_positions(10), _market_state(100),
                                _iv_points(0.2).iloc[:0], "s1", TS)
    assert line.iloc[0]["status"] == "no_vol" and pd.isna(line.iloc[0]["price"])
    # excluded from aggregate, but coverage exposes the partial book
    port = agg[agg["group_key"] == "portfolio"].iloc[0]
    assert port["n_lines"] == 0 and port["n_total"] == 1 and port["coverage"] == 0.0


def test_coverage_ratio_reported():
    pos = pd.concat([_positions(10),
                     pd.DataFrame([{"as_of_ts": TS, "account": "U1", "underlying": "SPY",
                                    "instrument_key": "OPT:SPY:20261218:200:C",
                                    "quantity": 5.0, "avg_cost": 1.0}])],
                    ignore_index=True)  # 2nd option has no vol -> unpriceable
    _, agg, _ = compute_risk(pos, _market_state(100), _iv_points(0.2), "s1", TS)
    port = agg[agg["group_key"] == "portfolio"].iloc[0]
    assert port["n_lines"] == 1 and port["n_total"] == 2 and port["coverage"] == 0.5


def test_missing_spot_flagged():
    line, _, _ = compute_risk(_positions(10), _market_state(100).iloc[:0],
                              _iv_points(0.2), "s1", TS)
    assert line.iloc[0]["status"] == "no_spot"


def test_reconciliation_breach_surfaced():
    line, _, _ = compute_risk(_positions(10), _market_state(100), _iv_points(0.2), "s1", TS)
    comp_delta = float(line.iloc[0]["delta"])
    broker = pd.DataFrame([
        {"instrument_key": "OPT:SPY:20261218:100:C", "delta": comp_delta - 0.05},  # breach
    ])
    recon = reconcile_greeks(line, broker, "s1", TS, RiskParams(recon_threshold=0.01))
    row = recon[recon["greek"] == "delta"].iloc[0]
    assert row["breach"] and abs(row["abs_diff"] - 0.05) < 1e-9


def test_reconciliation_within_threshold_no_breach():
    line, _, _ = compute_risk(_positions(10), _market_state(100), _iv_points(0.2), "s1", TS)
    comp_delta = float(line.iloc[0]["delta"])
    broker = pd.DataFrame([{"instrument_key": "OPT:SPY:20261218:100:C",
                            "delta": comp_delta + 0.001}])
    recon = reconcile_greeks(line, broker, "s1", TS, RiskParams(recon_threshold=0.01))
    assert not recon.iloc[0]["breach"]


def test_persist_line_and_aggregate(tmp_path):
    line, agg, _ = compute_risk(_positions(10), _market_state(100), _iv_points(0.2), "s1", TS)
    store = ParquetStore(tmp_path)
    store.write_partition("greeks", "2026-06-11", "SPY", line)
    store.write_partition("risk_aggregates", "2026-06-11", "SPY", agg)
    lb = store.read("greeks", trade_date="2026-06-11", underlying="SPY")
    ab = store.read("risk_aggregates", trade_date="2026-06-11", underlying="SPY")
    assert len(lb) == 1 and "dollar_gamma" in lb.columns
    assert len(ab) >= 3
