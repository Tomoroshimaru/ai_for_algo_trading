"""Offline tests for the live streaming collector (Step 3a/b/e + acceptance)."""
import datetime as dt

from utils.config import load_config
from connectivity.session import IBSession
from ingestion.store import RawEventStore
from ingestion.events import MarketEvent, OpsEventKind
from ingestion.service import (
    StreamingCollector, Subscription, detect_missing_intervals,
)

UTC = dt.timezone.utc
BASE = dt.datetime(2026, 6, 11, 14, 0, tzinfo=UTC)


class FakeEvent:
    def __init__(self): self._handlers = []
    def __iadd__(self, h): self._handlers.append(h); return self
    def emit(self, *a): [h(*a) for h in self._handlers]


class FakeContract:
    def __init__(self, con_id): self.conId = con_id


class FakeTicker:
    def __init__(self, con_id, **px):
        self.contract = FakeContract(con_id)
        self.time = BASE
        for k, v in px.items(): setattr(self, k, v)


class FakeIB:
    """Minimal ib_insync stand-in driving the collector loop deterministically."""
    def __init__(self, tickers, clock_holder):
        self._connected = False
        self._tickers = tickers
        self._clock = clock_holder       # mutable [seconds] advanced by sleep()
        self.pendingTickersEvent = FakeEvent()
        self.errorEvent = FakeEvent()
        self.req_mkt_data_calls = 0
    def connect(self, *a, **k): self._connected = True
    def isConnected(self): return self._connected
    def disconnect(self): self._connected = False
    def reqMktData(self, *a, **k): self.req_mkt_data_calls += 1
    def sleep(self, s):
        self._clock[0] += s               # advance time
        self.pendingTickersEvent.emit(self._tickers)   # deliver ticks


def _session(make_ib):
    cfg = load_config(load_env=False)
    return IBSession(cfg.ibkr, ib_factory=make_ib, sleep=lambda s: None).connect()


def test_on_pending_persists_known_ignores_unknown(tmp_path):
    clock = [0.0]
    tickers = [FakeTicker(1, bid=100.0, ask=100.5), FakeTicker(999, bid=1.0)]
    s = _session(lambda: FakeIB(tickers, clock))
    store = RawEventStore(tmp_path)
    subs = [Subscription(FakeContract(1), "STK:SPY:USD")]
    col = StreamingCollector(s, store, subs, "sA", "2026-06-11", clock=lambda: BASE)
    n = col._on_pending(tickers)
    assert n == 2          # only conId=1 (bid, ask); conId=999 ignored
    mkt, _ = store.replay_market("2026-06-11", "sA")
    assert {e.instrument_key for e in mkt} == {"STK:SPY:USD"}


def test_heartbeat_triggers_controlled_reconnect(tmp_path):
    clock = [0.0]
    tickers = [FakeTicker(1, bid=100.0)]
    s = _session(lambda: FakeIB(tickers, clock))
    store = RawEventStore(tmp_path)
    subs = [Subscription(FakeContract(1), "STK:SPY:USD")]
    col = StreamingCollector(s, store, subs, "sA", "2026-06-11", clock=lambda: BASE)
    s.ib._connected = False               # simulate a dropped connection
    col._heartbeat()
    assert col.reconnect_count == 1
    assert s.ib.isConnected()             # new client connected
    assert s.ib.req_mkt_data_calls == 1   # re-subscribed on the fresh client
    ops, _ = store.replay_ops("2026-06-11", "sA")
    kinds = [o.kind for o in ops]
    assert OpsEventKind.DISCONNECT in kinds and OpsEventKind.RECONNECT in kinds


def test_run_loop_collects_until_deadline(tmp_path):
    clock = [0.0]
    tickers = [FakeTicker(1, bid=100.0, ask=100.5)]
    s = _session(lambda: FakeIB(tickers, clock))
    store = RawEventStore(tmp_path)
    subs = [Subscription(FakeContract(1), "STK:SPY:USD")]
    # clock derived from the holder advanced by FakeIB.sleep -> loop terminates
    col = StreamingCollector(
        s, store, subs, "sA", "2026-06-11",
        heartbeat_interval=100, poll_interval=1.0,
        clock=lambda: BASE + dt.timedelta(seconds=clock[0]),
    )
    col.run(duration_sec=3)
    mkt, corrupt = store.replay_market("2026-06-11", "sA")
    assert corrupt == 0
    assert len(mkt) >= 3 * 2               # >=3 polls x (bid+ask)


def test_kill_and_restart_does_not_corrupt_store(tmp_path):
    """Two sessions write independent files; both replay intact (acceptance)."""
    store = RawEventStore(tmp_path)
    for sid in ("sA", "sB"):
        clock = [0.0]
        s = _session(lambda: FakeIB([FakeTicker(1, bid=100.0, ask=100.5)], clock))
        subs = [Subscription(FakeContract(1), "STK:SPY:USD")]
        col = StreamingCollector(
            s, store, subs, sid, "2026-06-11",
            heartbeat_interval=100, poll_interval=1.0,
            clock=lambda c=clock: BASE + dt.timedelta(seconds=c[0]),
        )
        col.run(duration_sec=2)
    a, ca = store.replay_market("2026-06-11", "sA")
    b, cb = store.replay_market("2026-06-11", "sB")
    assert ca == 0 and cb == 0 and len(a) >= 4 and len(b) >= 4


def test_detect_missing_intervals():
    K = "STK:SPY:USD"
    evs = [
        MarketEvent("s", K, "bid", 1.0, None, BASE, BASE),
        MarketEvent("s", K, "bid", 1.0, None, BASE, BASE + dt.timedelta(seconds=1)),
        MarketEvent("s", K, "bid", 1.0, None, BASE, BASE + dt.timedelta(seconds=30)),
    ]
    gaps = detect_missing_intervals(evs, K, interval_sec=1.0, gap_factor=2.0)
    assert len(gaps) == 1                  # the 1s->30s jump is a gap
