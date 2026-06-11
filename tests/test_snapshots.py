"""Offline tests for the pure market-state snapshot builder (Step 5)."""
import datetime as dt

import pandas as pd

from ingestion.events import MarketEvent
from snapshots.builder import (
    SnapshotParams, build_snapshots, choose_reference, parse_instrument,
    compute_completeness, regular_grid,
)
from storage.parquet_store import ParquetStore, lineage_raw_for_snapshot

UTC = dt.timezone.utc
T = dt.datetime(2026, 6, 11, 14, 0, 0, tzinfo=UTC)


def _ev(key, field, value, secs):
    ts = T + dt.timedelta(seconds=secs)
    return MarketEvent("s1", key, field, value, None, ts, ts)


def test_parse_instrument():
    assert parse_instrument("STK:SPY:USD") == ("SPY", None)
    assert parse_instrument("OPT:SPY:20261218:500:C") == ("SPY", "2026-12-18")


def test_choose_reference_mid_then_fallbacks():
    p = SnapshotParams(max_spread_pct=5.0)
    # tight spread -> mid
    v, t, mid, spr = choose_reference(100.0, 100.5, 99.0, 98.0, p)
    assert t == "mid" and round(v, 3) == 100.25
    # wide spread -> fallback to last
    v, t, mid, spr = choose_reference(100.0, 130.0, 110.0, 98.0, p)
    assert t == "last" and v == 110.0 and spr > 5.0
    # no last -> close
    v, t, _, _ = choose_reference(100.0, 130.0, None, 98.0, p)
    assert t == "close" and v == 98.0
    # nothing usable but wide mid -> labeled mid_wide
    v, t, _, _ = choose_reference(100.0, 130.0, None, None, p)
    assert t == "mid_wide"
    # truly nothing
    v, t, _, _ = choose_reference(None, None, None, None, p)
    assert t == "none" and v is None


def test_determinism_same_inputs_same_rows():
    events = [_ev("STK:SPY:USD", "bid", 100.0, 0), _ev("STK:SPY:USD", "ask", 100.4, 1)]
    times = [T + dt.timedelta(seconds=2)]
    a = build_snapshots(events, times, "s1")
    b = build_snapshots(list(reversed(events)), times, "s1")  # order must not matter
    pd.testing.assert_frame_equal(a, b)


def test_stale_flag_and_age():
    events = [_ev("STK:SPY:USD", "bid", 100.0, 0), _ev("STK:SPY:USD", "ask", 100.4, 0)]
    p = SnapshotParams(max_age_sec=5.0)
    fresh = build_snapshots(events, [T + dt.timedelta(seconds=3)], "s1", p)
    stale = build_snapshots(events, [T + dt.timedelta(seconds=20)], "s1", p)
    assert bool(fresh.iloc[0]["is_stale"]) is False and fresh.iloc[0]["age_sec"] == 3
    assert bool(stale.iloc[0]["is_stale"]) is True and stale.iloc[0]["age_sec"] == 20


def test_option_join_most_recent_eligible():
    events = [
        _ev("STK:SPY:USD", "bid", 100.0, 0), _ev("STK:SPY:USD", "ask", 100.4, 0),
        _ev("OPT:SPY:20261218:500:C", "bid", 5.0, 1),
        _ev("OPT:SPY:20261218:500:C", "bid", 5.2, 4),   # most recent <= t wins
        _ev("OPT:SPY:20261218:500:C", "ask", 5.6, 30),  # after t -> excluded
    ]
    snap = build_snapshots(events, [T + dt.timedelta(seconds=5)], "s1")
    opt = snap[snap.instrument_key == "OPT:SPY:20261218:500:C"].iloc[0]
    assert opt["bid"] == 5.2 and pd.isna(opt["ask"])   # ask@30s not yet eligible


def test_market_open_flag():
    p = SnapshotParams(market_open_utc=dt.time(13, 30), market_close_utc=dt.time(20, 0))
    events = [_ev("STK:SPY:USD", "last", 100.0, 0)]
    snap = build_snapshots(events, [T], "s1", p)             # 14:00 UTC -> open
    assert bool(snap.iloc[0]["is_market_open"]) is True
    closed = build_snapshots(events, [T.replace(hour=21)], "s1", p)
    assert bool(closed.iloc[0]["is_market_open"]) is False


def test_completeness_per_underlying_and_maturity():
    events = [
        _ev("STK:SPY:USD", "bid", 100.0, 0), _ev("STK:SPY:USD", "ask", 100.4, 0),
        _ev("OPT:SPY:20261218:500:C", "bid", 5.0, 0),
        _ev("OPT:SPY:20261218:510:C", "bid", 4.0, 0),
    ]
    snap = build_snapshots(events, [T + dt.timedelta(seconds=1)], "s1")
    comp = compute_completeness(snap)
    row = comp[(comp.underlying == "SPY") & (comp.expiry == "2026-12-18")].iloc[0]
    assert row["n_instruments"] == 2 and row["completeness"] == 1.0


def test_regular_grid():
    g = regular_grid(T, T + dt.timedelta(seconds=10), 5)
    assert g == [T, T + dt.timedelta(seconds=5), T + dt.timedelta(seconds=10)]


def test_snapshots_persist_to_warehouse_and_lineage(tmp_path):
    events = [_ev("STK:SPY:USD", "bid", 100.0, 0), _ev("STK:SPY:USD", "ask", 100.4, 1)]
    snap = build_snapshots(events, [T + dt.timedelta(seconds=2)], "s1")
    store = ParquetStore(tmp_path)
    for underlying, grp in snap.groupby("underlying"):
        store.write_partition("market_state", "2026-06-11", underlying, grp)
    back = store.read("market_state", trade_date="2026-06-11", underlying="SPY")
    assert len(back) == 1 and back.iloc[0]["reference_type"] == "mid"
    assert back.iloc[0]["source_session_id"] == "s1"   # lineage column persisted
