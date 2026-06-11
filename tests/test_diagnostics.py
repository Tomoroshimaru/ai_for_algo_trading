"""Offline tests for the Step 1 bootstrap diagnostics."""
import datetime as dt

from utils.config import load_config
from connectivity.session import IBSession
from connectivity.diagnostics import gather_evidence, write_evidence


class FakeContract:
    symbol = "SPY"
    conId = 756733
    exchange = "SMART"
    currency = "USD"


class FakeTicker:
    def marketPrice(self):
        return 512.34


class FakeIB:
    def __init__(self):
        self._connected = False
        self.md_type = None

    def connect(self, *a, **k):
        self._connected = True

    def isConnected(self):
        return self._connected

    def disconnect(self):
        self._connected = False

    def reqCurrentTime(self):
        return dt.datetime.now(dt.timezone.utc)

    def reqMarketDataType(self, n):
        self.md_type = n

    def qualifyContracts(self, contract):
        return [FakeContract()]

    def reqTickers(self, *contracts):
        return [FakeTicker()]


def _connected_session(fake):
    cfg = load_config(load_env=False)
    s = IBSession(cfg.ibkr, ib_factory=lambda: fake, sleep=lambda s: None)
    s.connect()
    return s, cfg


def test_gather_evidence_structure():
    fake = FakeIB()
    session, cfg = _connected_session(fake)
    report = gather_evidence(session, cfg, symbol="SPY")
    assert report["connected"] is True
    assert report["connection_state"] in {"CONNECTED", "DEGRADED"}
    assert report["resolved_contract"]["symbol"] == "SPY"
    assert report["resolved_contract"]["conId"] == 756733
    assert report["sample_market_price"] == 512.34
    assert fake.md_type == cfg.ibkr.market_data_type  # market data type was set
    assert report["server_time_utc"] is not None


def test_write_evidence_creates_artifact(tmp_path):
    fake = FakeIB()
    session, cfg = _connected_session(fake)
    report = gather_evidence(session, cfg)
    path = write_evidence(report, tmp_path)
    assert path.exists()
    assert path.name.startswith("bootstrap_") and path.suffix == ".json"
    import json
    loaded = json.loads(path.read_text())
    assert loaded["resolved_contract"]["symbol"] == "SPY"
