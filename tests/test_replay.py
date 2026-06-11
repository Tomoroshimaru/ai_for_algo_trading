"""Step 13 - historical reconstruction & replay (offline)."""
import datetime as dt

import pandas as pd
import pytest

from storage.parquet_store import ParquetStore
from iv.inversion import black76_price
from pipeline.daily import run_day, PipelineParams
from replay.backfill import (
    detect_partitions, replay_day, replay_range, compare_replay_vs_live,
)

UTC = dt.timezone.utc
TS = dt.datetime(2026, 6, 11, 14, 0, tzinfo=UTC)
DATE = "2026-06-11"
EXP = "20261218"


def _snapshot(spot=100.0, with_options=True):
    F = spot + 5.0
    Tt = (dt.date(2026, 12, 18) - TS.date()).days / 365.0
    def row(key, b, a, ref=None):
        m = (b + a) / 2
        return {"snapshot_ts": TS, "underlying": "SPY", "instrument_key": key,
                "bid": b, "ask": a, "last": m, "mid": m,
                "spread_pct": round((a - b) / max(m, .5) * 100, 3),
                "reference_price": ref, "reference_type": "last",
                "is_stale": False, "is_market_open": True, "age_sec": 1.0,
                "source_session_id": "s1"}
    rows = [row("STK:SPY:USD", spot - .05, spot + .05, spot)]
    if with_options:
        for K in (90, 100, 110):
            for r in ("C", "P"):
                px = black76_price(F, K, Tt, 0.2, r)
                rows.append(row(f"OPT:SPY:{EXP}:{K}:{r}", px - .1, px + .1))
    return pd.DataFrame(rows)


def _seed_market_state(store, date=DATE, **kw):
    store.write_partition("market_state", date, "SPY", _snapshot(**kw))


# ---- versioned partitions (task d) -----------------------------------------
def test_versioned_partition_isolates_live_and_replay(tmp_path):
    store = ParquetStore(tmp_path)
    df = _snapshot()
    store.write_partition("market_state", DATE, "SPY", df)               # live
    store.write_partition("market_state", DATE, "SPY", df.head(1),
                          code_version="v2")                            # replay archive
    live = store.read("market_state", trade_date=DATE, underlying="SPY")
    v2 = store.read("market_state", trade_date=DATE, underlying="SPY", code_version="v2")
    assert len(live) == len(df)        # live read ignores versioned subtree
    assert len(v2) == 1                 # version read sees only its archive
    # re-running a NEW version must not overwrite the old one
    store.write_partition("market_state", DATE, "SPY", df.head(2), code_version="v3")
    assert len(store.read("market_state", trade_date=DATE, underlying="SPY", code_version="v2")) == 1


# ---- shared pipeline (junior note: no dual code path) ----------------------
def test_run_day_chains_forwards_iv_surface():
    res = run_day(_snapshot(), "s1")
    assert len(res.forwards) >= 1
    assert (res.iv_points["status"] == "solved").sum() >= 4
    assert len(res.surface_params) >= 1
    assert res.risk_line is None       # no positions provided


def test_run_day_runs_risk_when_positions_given():
    pos = pd.DataFrame([{"as_of_ts": TS, "account": "D", "underlying": "SPY",
                         "instrument_key": f"OPT:SPY:{EXP}:100:C",
                         "quantity": 10.0, "avg_cost": 5.0}])
    res = run_day(_snapshot(), "s1", positions=pos, snapshot_ts=TS)
    assert res.risk_line is not None and len(res.risk_line) == 1
    assert res.risk_aggregates is not None


# ---- missing-partition detection (task c) ----------------------------------
def test_detect_partitions_flags_missing(tmp_path):
    store = ParquetStore(tmp_path)
    _seed_market_state(store, DATE)
    present = detect_partitions(store, [DATE, "2026-06-12"])
    assert present[DATE] is True and present["2026-06-12"] is False


def test_replay_day_missing_is_flagged_not_masked(tmp_path):
    store = ParquetStore(tmp_path)
    r = replay_day(store, "2099-01-01", "v1", "s1")
    assert r.status == "MISSING" and r.present is False and r.partial_data is True


def test_replay_day_partial_when_no_options(tmp_path):
    store = ParquetStore(tmp_path)
    _seed_market_state(store, DATE, with_options=False)
    r = replay_day(store, DATE, "v1", "s1")
    assert r.status == "PARTIAL" and r.n_options == 0


# ---- batch replay + QA report (tasks a/b) ----------------------------------
def test_replay_range_qa_report(tmp_path):
    store = ParquetStore(tmp_path)
    _seed_market_state(store, DATE)
    results, qa = replay_range(store, [DATE, "2026-06-12"], "v1", "s1")
    assert results[0].status == "OK" and results[1].status == "MISSING"
    miss = qa[(qa["check_name"] == "replay_partition_present") & (qa["target"] == "2026-06-12")]
    assert miss.iloc[0]["status"] == "FAIL"
    ok = qa[(qa["check_name"] == "replay_partition_present") & (qa["target"] == DATE)]
    assert ok.iloc[0]["status"] == "PASS"
    # outputs archived under the version, not the live tree
    assert len(store.read("forwards", trade_date=DATE, underlying="SPY", code_version="v1")) >= 1
    assert len(store.read("forwards", trade_date=DATE, underlying="SPY")) == 0


# ---- replay vs live alignment (task e / acceptance) ------------------------
def test_replay_matches_live_same_code_version(tmp_path):
    store = ParquetStore(tmp_path)
    _seed_market_state(store, DATE)
    snap = store.read("market_state", trade_date=DATE)
    # 'live' run: persist forwards to the flat tree
    live = run_day(snap, "s1")
    for und, g in live.forwards.groupby("underlying"):
        store.write_partition("forwards", DATE, str(und), g)
    # 'replay' run through the SAME pipeline, archived under v1
    replay_day(store, DATE, "v1", "s1")
    cmp = compare_replay_vs_live(store, "forwards", DATE, "SPY", "v1",
                                 key_cols=["underlying", "expiry"],
                                 value_cols=["forward"])
    assert cmp["aligned_rows"] >= 1
    assert cmp["max_abs_diff"]["forward"] < 1e-9   # identical code path => exact
