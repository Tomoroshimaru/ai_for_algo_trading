"""Offline tests for the pure forward & implied-carry engine (Step 6)."""
import datetime as dt
import math

import numpy as np
import pandas as pd

from forward.engine import (
    ForwardParams, compute_forwards, parse_option_key, _weighted_median, _mad,
)
from storage.parquet_store import ParquetStore

UTC = dt.timezone.utc
T = dt.datetime(2026, 6, 11, 14, 0, tzinfo=UTC)
EXP = "2026-12-18"


def _row(key, mid, spread=1.0, stale=False, ref=None):
    return {
        "snapshot_ts": T, "underlying": "SPY", "instrument_key": key,
        "mid": mid, "spread_pct": spread, "is_stale": stale,
        "reference_price": ref,
    }


def _chain(true_fwd, spot, strikes, outlier=None):
    """Build a snapshot where C-P = true_fwd-K so parity forward = true_fwd for all."""
    rows = [_row("STK:SPY:USD", spot, ref=spot)]
    for k in strikes:
        put = 5.0
        call = put + (true_fwd - k)
        rows.append(_row(f"OPT:SPY:20261218:{int(k)}:C", call))
        rows.append(_row(f"OPT:SPY:20261218:{int(k)}:P", put))
    if outlier is not None:
        k = outlier
        rows.append(_row(f"OPT:SPY:20261218:{int(k)}:C", 50.0))  # broken call
        rows.append(_row(f"OPT:SPY:20261218:{int(k)}:P", 5.0))
    return pd.DataFrame(rows)


def test_parse_option_key():
    assert parse_option_key("OPT:SPY:20261218:500:C") == ("SPY", "2026-12-18", 500.0, "C")
    assert parse_option_key("STK:SPY:USD") is None


def test_weighted_median_and_mad():
    assert _weighted_median([1, 2, 3], [1, 1, 1]) == 2
    assert _weighted_median([1, 100], [10, 1]) == 1   # weight pulls to 1
    assert _mad([10, 12, 14], 12) == 2


def test_recovers_known_forward():
    snap = _chain(101.0, 100.0, [95, 98, 100, 102, 105])
    fwd, diag = compute_forwards(snap, "s1")
    row = fwd.iloc[0]
    assert abs(row["forward"] - 101.0) < 1e-9
    assert row["n_pairs"] == 5 and bool(row["is_reliable"]) is True
    assert (diag["quality_label"] == "inlier").sum() == 5


def test_outlier_does_not_dominate_and_is_labeled():
    snap = _chain(101.0, 100.0, [95, 98, 100, 102, 105], outlier=110)
    fwd, diag = compute_forwards(snap, "s1", ForwardParams(mad_k=3.0))
    assert abs(fwd.iloc[0]["forward"] - 101.0) < 1e-9      # outlier rejected
    labels = dict(zip(diag["strike"], diag["quality_label"]))
    assert labels[110.0] == "outlier"


def test_stability_under_strike_perturbation():
    base = compute_forwards(_chain(101.0, 100.0, [95, 98, 100, 102, 105]), "s1")[0]
    drop = compute_forwards(_chain(101.0, 100.0, [95, 100, 102, 105]), "s1")[0]  # drop one
    assert abs(base.iloc[0]["forward"] - drop.iloc[0]["forward"]) < 1e-9


def test_implied_carry_sign():
    # forward > spot => positive cost-of-carry
    snap = _chain(105.0, 100.0, [95, 100, 105])
    fwd, _ = compute_forwards(snap, "s1")
    ic = fwd.iloc[0]["implied_carry"]
    T_yr = (dt.date(2026, 12, 18) - T.date()).days / 365.0
    assert abs(ic - math.log(105.0 / 100.0) / T_yr) < 1e-9 and ic > 0


def test_stale_pairs_excluded():
    rows = [_row("STK:SPY:USD", 100.0, ref=100.0),
            _row("OPT:SPY:20261218:100:C", 6.0, stale=True),
            _row("OPT:SPY:20261218:100:P", 5.0, stale=True)]
    fwd, diag = compute_forwards(pd.DataFrame(rows), "s1")
    assert int(fwd.iloc[0]["n_pairs"]) == 0 and bool(fwd.iloc[0]["is_reliable"]) is False
    assert (diag["quality_label"] == "stale").all()


def test_incomplete_pair_flagged():
    rows = [_row("STK:SPY:USD", 100.0, ref=100.0),
            _row("OPT:SPY:20261218:100:C", 6.0)]  # no put
    fwd, diag = compute_forwards(pd.DataFrame(rows), "s1")
    assert (diag["quality_label"] == "incomplete").any()


def test_persist_forwards_and_diagnostics(tmp_path):
    snap = _chain(101.0, 100.0, [95, 100, 105], outlier=120)
    fwd, diag = compute_forwards(snap, "s1")
    store = ParquetStore(tmp_path)
    store.write_partition("forwards", "2026-06-11", "SPY", fwd)
    store.write_partition("forward_diagnostics", "2026-06-11", "SPY", diag)
    fb = store.read("forwards", trade_date="2026-06-11", underlying="SPY")
    db = store.read("forward_diagnostics", trade_date="2026-06-11", underlying="SPY")
    assert len(fb) == 1 and "outlier" in set(db["quality_label"])
