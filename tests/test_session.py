"""Offline tests for IBSession using a fake IB client (Step 1 connectivity)."""
import datetime as dt
import time

import pytest

from utils.config import IBKRConfig
from connectivity.session import IBSession, ConnectionState


IBKR = IBKRConfig(host="127.0.0.1", port=4002, client_id=1, account=None, market_data_type=3)


class FakeIB:
    """Minimal stand-in for ib_insync.IB."""

    def __init__(self, fail_times=0, server_time=None):
        self.fail_times = fail_times
        self.calls = 0
        self._connected = False
        self._server_time = server_time
        self.disconnected = False

    def connect(self, host, port, clientId, timeout, account):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise TimeoutError("refused")
        self._connected = True

    def isConnected(self):
        return self._connected

    def disconnect(self):
        self._connected = False
        self.disconnected = True

    def reqCurrentTime(self):
        return self._server_time


def _session(fake, **kw):
    return IBSession(IBKR, ib_factory=lambda: fake, sleep=lambda s: None, **kw)


def test_connect_success():
    fake = FakeIB(fail_times=0)
    s = _session(fake).connect()
    assert s.state is ConnectionState.CONNECTED
    assert fake.calls == 1


def test_connect_retries_then_succeeds():
    sleeps = []
    fake = FakeIB(fail_times=2)
    s = IBSession(IBKR, ib_factory=lambda: fake, sleep=sleeps.append, max_retries=5)
    s.connect()
    assert s.state is ConnectionState.CONNECTED
    assert fake.calls == 3
    # exponential backoff: 1, 2 seconds before the 3rd (successful) attempt
    assert sleeps == [1.0, 2.0]


def test_connect_exhausts_retries_raises():
    fake = FakeIB(fail_times=99)
    s = _session(fake, max_retries=3)
    with pytest.raises(ConnectionError):
        s.connect()
    assert s.state is ConnectionState.DISCONNECTED
    assert fake.calls == 3


def test_health_check_ok():
    now = dt.datetime.now(dt.timezone.utc)
    fake = FakeIB(server_time=now)
    s = _session(fake).connect()
    rep = s.health_check(clock_skew_tolerance_sec=5.0)
    assert rep.connected is True
    assert rep.state is ConnectionState.CONNECTED
    assert rep.clock_skew_sec < 5.0


def test_health_check_degraded_on_clock_skew():
    stale = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=120)
    fake = FakeIB(server_time=stale)
    s = _session(fake).connect()
    rep = s.health_check(clock_skew_tolerance_sec=2.0)
    assert rep.state is ConnectionState.DEGRADED
    assert rep.clock_skew_sec > 2.0


def test_health_check_disconnected():
    fake = FakeIB()
    s = _session(fake)  # never connected
    rep = s.health_check()
    assert rep.connected is False
    assert rep.state is ConnectionState.DISCONNECTED


def test_context_manager_disconnects():
    fake = FakeIB()
    with _session(fake) as s:
        assert s.state is ConnectionState.CONNECTED
    assert fake.disconnected is True


def test_ib_property_before_connect_raises():
    s = _session(FakeIB())
    with pytest.raises(RuntimeError):
        _ = s.ib
