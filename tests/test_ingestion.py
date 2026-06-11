"""Offline tests for the raw ingestion layer (Step 3 core)."""
import datetime as dt
import math

from ingestion.events import MarketEvent, OpsEvent, OpsEventKind
from ingestion.store import RawEventStore
from ingestion.collector import (
    normalize_ticker, classify_error, build_session_summary, Collector,
)

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 6, 11, 14, 0, tzinfo=UTC)


class FakeTicker:
    def __init__(self, **kw):
        self.time = kw.pop("time", None)
        for k, v in kw.items():
            setattr(self, k, v)


def test_market_event_json_roundtrip():
    e = MarketEvent("s1", "STK:SPY:USD", "bid", 1.5, T0, T0, T0)
    assert MarketEvent.from_json(e.to_json()) == e


def test_ops_event_json_roundtrip():
    e = OpsEvent("s1", OpsEventKind.PACING, "[100] slow down", T0)
    assert OpsEvent.from_json(e.to_json()) == e


def test_normalize_ticker_drops_nan_and_missing():
    tk = FakeTicker(bid=100.0, ask=float("nan"), last=101.0, time=T0)
    events = normalize_ticker(tk, "STK:SPY:USD", "s1", now=T0)
    fields = {e.field: e.value for e in events}
    assert fields == {"bid": 100.0, "last": 101.0}   # ask NaN dropped, others absent
    assert all(e.source_ts == T0 and e.receipt_ts == T0 for e in events)


def test_classify_error_pacing_entitlement_other():
    assert classify_error(100, "x", "s1").kind == OpsEventKind.PACING
    assert classify_error(354, "x", "s1").kind == OpsEventKind.ENTITLEMENT
    assert classify_error(10167, "x", "s1").kind == OpsEventKind.ENTITLEMENT
    assert classify_error(504, "x", "s1").kind == OpsEventKind.ERROR


def test_store_append_and_replay_roundtrip(tmp_path):
    store = RawEventStore(tmp_path)
    evs = [MarketEvent("s1", "STK:SPY:USD", "bid", 1.0, None, T0, T0),
           MarketEvent("s1", "STK:SPY:USD", "ask", 2.0, None, T0, T0)]
    n = store.append_market("2026-06-11", "s1", evs)
    assert n == 2
    back, corrupt = store.replay_market("2026-06-11", "s1")
    assert corrupt == 0 and back == evs   # replay from disk, no broker


def test_store_is_append_only(tmp_path):
    store = RawEventStore(tmp_path)
    store.append_market("2026-06-11", "s1", [MarketEvent("s1","K","bid",1.0,None,T0,T0)])
    store.append_market("2026-06-11", "s1", [MarketEvent("s1","K","ask",2.0,None,T0,T0)])
    back, _ = store.replay_market("2026-06-11", "s1")
    assert [e.field for e in back] == ["bid", "ask"]   # nothing overwritten


def test_replay_tolerates_truncated_trailing_line(tmp_path):
    """Simulate a kill mid-write: last line is partial -> not corrupted store."""
    store = RawEventStore(tmp_path)
    store.append_market("2026-06-11", "s1", [MarketEvent("s1","K","bid",1.0,None,T0,T0)])
    path = store._path("market", "2026-06-11", "s1")
    with path.open("a") as fh:
        fh.write('{"type": "market", "session_id": "s1", "instrument')  # truncated
    back, corrupt = store.replay_market("2026-06-11", "s1")
    assert len(back) == 1 and corrupt == 1   # good event survives, bad counted


def test_collector_callback_persists_only(tmp_path):
    store = RawEventStore(tmp_path)
    col = Collector(store, "s1", "2026-06-11", clock=lambda: T0)
    tk = FakeTicker(bid=100.0, ask=100.5, time=T0)
    n = col.on_ticker(tk, "STK:SPY:USD")
    assert n == 2
    ev = col.on_error(100, "pacing")
    assert ev.kind == OpsEventKind.PACING
    mkt, _ = store.replay_market("2026-06-11", "s1")
    ops, _ = store.replay_ops("2026-06-11", "s1")
    assert len(mkt) == 2 and len(ops) == 1


def test_build_session_summary():
    evs = [MarketEvent("s1", "K1", "bid", 1.0, None, T0, T0),
           MarketEvent("s1", "K1", "ask", 2.0, None, T0, T0),
           MarketEvent("s1", "K2", "bid", 3.0, None, T0, T0)]
    ops = [OpsEvent("s1", OpsEventKind.RECONNECT, "x", T0),
           OpsEvent("s1", OpsEventKind.PACING, "y", T0)]
    s = build_session_summary(evs, ops, expected_instruments=4)
    assert s["total_market_events"] == 3
    assert s["distinct_instruments"] == 2
    assert s["by_field"] == {"bid": 2, "ask": 1}
    assert s["reconnect_count"] == 1 and s["pacing_count"] == 1
    assert s["coverage_ratio"] == 0.5
