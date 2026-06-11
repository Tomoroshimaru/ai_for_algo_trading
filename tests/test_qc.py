"""Offline tests for quote QC (Step 7) - named checks, reason codes, determinism."""
import datetime as dt

import pandas as pd

from qc.quality import (
    QCParams, run_qc, chk_bid_positive, chk_spread, chk_crossed_locked,
    chk_intrinsic, PER_QUOTE_CHECKS,
)
from storage.parquet_store import ParquetStore

UTC = dt.timezone.utc
T = dt.datetime(2026, 6, 11, 14, 0, tzinfo=UTC)


def _row(key, bid=None, ask=None, mid=None, spread=1.0, stale=False, ref=None,
         oi=None, vol=None):
    return {"snapshot_ts": T, "underlying": "SPY", "instrument_key": key,
            "bid": bid, "ask": ask, "last": None, "mid": mid, "spread_pct": spread,
            "reference_price": ref, "is_stale": stale, "age_sec": 1.0,
            "open_interest": oi, "volume": vol}


def _ctx(spot=100.0):
    return {"spot": spot}


def test_named_checks_individually():
    p = QCParams()
    assert chk_bid_positive(_row("x", bid=-1), _ctx(), p).reason_code == "BID_NONPOSITIVE"
    assert chk_spread(_row("x", spread=80), _ctx(), p).reason_code == "SPREAD_ABSURD"
    assert chk_spread(_row("x", spread=20), _ctx(), p).status == "CAUTION"
    cr = chk_crossed_locked(_row("x", bid=5, ask=4), _ctx(), p)
    assert cr.reason_code == "CROSSED_MARKET" and cr.status == "REJECT"
    locked = chk_crossed_locked(_row("x", bid=4, ask=4), _ctx(), p)
    assert locked.reason_code == "LOCKED_MARKET" and locked.status == "CAUTION"


def test_intrinsic_bounds():
    p = QCParams()
    # call below intrinsic: spot=100,K=90 -> intrinsic 10, mid 5 -> reject
    r = _row("OPT:SPY:20261218:90:C", mid=5.0); r["_strike"], r["_right"] = 90.0, "C"
    assert chk_intrinsic(r, _ctx(100), p).reason_code == "BELOW_INTRINSIC"
    # call above underlying: mid 120 > spot 100 -> reject
    r = _row("OPT:SPY:20261218:90:C", mid=120.0); r["_strike"], r["_right"] = 90.0, "C"
    assert chk_intrinsic(r, _ctx(100), p).reason_code == "ABOVE_BOUND"


def test_each_check_logged_separately():
    # one quote that violates two checks: nonpositive bid AND crossed
    snap = pd.DataFrame([
        _row("STK:SPY:USD", ref=100.0),
        _row("OPT:SPY:20261218:100:C", bid=-1, ask=-2, mid=5.0, spread=1.0),
    ])
    _, qc = run_qc(snap, "s1")
    names = set(qc[qc.target == "OPT:SPY:20261218:100:C"]["check_name"])
    assert {"bid_positive", "crossed_locked", "FINAL"} <= names  # logged separately


def test_usable_caution_reject_classification():
    snap = pd.DataFrame([
        _row("STK:SPY:USD", ref=100.0),
        _row("OPT:SPY:20261218:100:C", bid=6, ask=6.1, mid=6.05, spread=1.0),    # usable
        _row("OPT:SPY:20261218:101:C", bid=5, ask=5.1, mid=5.05, spread=20.0),   # caution (wide)
        _row("OPT:SPY:20261218:102:C", bid=4, ask=3, mid=3.5, spread=1.0),       # reject (crossed)
    ])
    filt, qc = run_qc(snap, "s1")
    finals = dict(zip(qc[qc.check_name == "FINAL"]["target"],
                      qc[qc.check_name == "FINAL"]["status"]))
    assert finals["OPT:SPY:20261218:100:C"] == "USABLE"
    assert finals["OPT:SPY:20261218:101:C"] == "CAUTION"
    assert finals["OPT:SPY:20261218:102:C"] == "REJECT"
    # reject removed from filtered chain, caution retained
    keys = set(filt["instrument_key"])
    assert "OPT:SPY:20261218:102:C" not in keys and "OPT:SPY:20261218:101:C" in keys


def test_determinism_same_thresholds():
    snap = pd.DataFrame([
        _row("STK:SPY:USD", ref=100.0),
        _row("OPT:SPY:20261218:100:C", bid=6, ask=7, mid=6.5, spread=15.0),
    ])
    a = run_qc(snap, "s1")[1]
    b = run_qc(snap, "s1")[1]
    pd.testing.assert_frame_equal(a, b)


def test_parity_outlier_rejected_at_chain_level():
    rows = [_row("STK:SPY:USD", ref=100.0)]
    # consistent pairs parityF=K+(C-P)=101; one broken pair
    for k, cp in [(95, 6), (98, 3), (100, 1), (102, -1)]:
        rows.append(_row(f"OPT:SPY:20261218:{k}:C", bid=5+cp, ask=5+cp+0.1, mid=5.0+cp, spread=1.0))
        rows.append(_row(f"OPT:SPY:20261218:{k}:P", bid=5, ask=5.1, mid=5.0, spread=1.0))
    rows.append(_row("OPT:SPY:20261218:105:C", bid=40, ask=40.1, mid=40.0, spread=1.0))  # broken
    rows.append(_row("OPT:SPY:20261218:105:P", bid=5, ask=5.1, mid=5.0, spread=1.0))
    snap = pd.DataFrame(rows)
    _, qc = run_qc(snap, "s1")
    po = qc[qc.check_name == "parity_outlier"]
    assert "OPT:SPY:20261218:105:C" in set(po["target"])


def test_persist_qc_results(tmp_path):
    snap = pd.DataFrame([
        _row("STK:SPY:USD", ref=100.0),
        _row("OPT:SPY:20261218:100:C", bid=-1, ask=2, mid=5.0, spread=1.0),
    ])
    _, qc = run_qc(snap, "s1")
    store = ParquetStore(tmp_path)
    store.write_partition("qc_results", "2026-06-11", "SPY", qc)
    back = store.read("qc_results", trade_date="2026-06-11", underlying="SPY")
    assert "BID_NONPOSITIVE" in " ".join(back["detail"].tolist())
