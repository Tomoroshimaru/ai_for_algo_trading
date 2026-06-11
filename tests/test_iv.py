"""Offline tests for the IV inversion engine (Step 8)."""
import datetime as dt
import math

import pandas as pd

from iv.inversion import (
    black76_price, black76_delta, invert_black76, invert_chain, IVParams,
)
from storage.parquet_store import ParquetStore

UTC = dt.timezone.utc
T = dt.datetime(2026, 6, 11, 14, 0, tzinfo=UTC)


def test_black76_put_call_parity():
    F, K, t, sig = 100.0, 95.0, 0.5, 0.2
    c = black76_price(F, K, t, sig, "C")
    p = black76_price(F, K, t, sig, "P")
    assert abs((c - p) - (F - K)) < 1e-9   # undiscounted parity


def test_reference_roundtrip_recovers_sigma():
    F, K, t = 100.0, 100.0, 1.0
    for true_sig in [0.05, 0.1, 0.2, 0.5, 1.0]:
        for right in ("C", "P"):
            price = black76_price(F, K, t, true_sig, right)
            res = invert_black76(price, F, K, t, right)
            assert res.status == "solved"
            assert abs(res.iv - true_sig) < 1e-6


def test_atm_delta_around_half():
    F, K, t, sig = 100.0, 100.0, 1.0, 0.2
    price = black76_price(F, K, t, sig, "C")
    res = invert_black76(price, F, K, t, "C")
    assert 0.5 < res.delta < 0.6   # ATM-forward call delta slightly above 0.5


def test_records_convergence_metadata():
    F, K, t, sig = 100.0, 110.0, 0.25, 0.3
    price = black76_price(F, K, t, sig, "C")
    res = invert_black76(price, F, K, t, "C")
    assert res.n_iter > 0 and abs(res.residual) < 1e-8
    assert res.bracket_lo < res.iv < res.bracket_hi


def test_pathological_labels():
    # price above upper bound -> no_arbitrage
    assert invert_black76(101.0, 100.0, 90.0, 1.0, "C").status == "no_arbitrage"
    # price at intrinsic -> near_intrinsic
    assert invert_black76(10.0, 100.0, 90.0, 1.0, "C").status == "near_intrinsic"
    # negative price -> no_arbitrage
    assert invert_black76(-1.0, 100.0, 100.0, 1.0, "C").status == "no_arbitrage"
    # tiny ttm -> short_dated
    assert invert_black76(0.5, 100.0, 100.0, 1e-5, "C").status == "short_dated"


def test_finite_perturbation_plausible():
    F, K, t = 100.0, 100.0, 1.0
    base = black76_price(F, K, t, 0.2, "C")
    iv_lo = invert_black76(base * 0.99, F, K, t, "C").iv
    iv_hi = invert_black76(base * 1.01, F, K, t, "C").iv
    # higher price -> higher IV, small bounded change (no explosion)
    assert iv_lo < 0.2 < iv_hi and abs(iv_hi - iv_lo) < 0.02


def test_determinism():
    a = invert_black76(5.0, 100.0, 100.0, 1.0, "C")
    b = invert_black76(5.0, 100.0, 100.0, 1.0, "C")
    assert a == b


def _quote(key, mid):
    return {"snapshot_ts": T, "underlying": "SPY", "instrument_key": key, "mid": mid}


def test_batch_chain_and_persist(tmp_path):
    F, t = 105.0, (dt.date(2026, 12, 18) - T.date()).days / 365.0
    quotes = []
    for K in [95, 100, 105, 110]:
        quotes.append(_quote(f"OPT:SPY:20261218:{K}:C", black76_price(F, K, t, 0.25, "C")))
        quotes.append(_quote(f"OPT:SPY:20261218:{K}:P", black76_price(F, K, t, 0.25, "P")))
    fwd = pd.DataFrame([{"underlying": "SPY", "expiry": "2026-12-18", "forward": F}])
    iv = invert_chain(pd.DataFrame(quotes), fwd, "s1")
    solved = iv[iv.status == "solved"]
    assert len(solved) == 8
    assert (solved["iv"].sub(0.25).abs() < 1e-4).all()   # recovers flat 25% vol
    store = ParquetStore(tmp_path)
    store.write_partition("iv_points", "2026-06-11", "SPY", iv)
    back = store.read("iv_points", trade_date="2026-06-11", underlying="SPY")
    assert len(back) == 8 and "black76" in set(back["method"])
