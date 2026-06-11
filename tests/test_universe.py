"""Offline tests for the instrument master (Step 2 foundations)."""
import datetime as dt

import pytest

from utils.config import load_config
from connectivity.session import IBSession
from universe.schema import UnderlyingInstrument, underlying_key, option_key
from universe.master import (
    resolve_underlying, get_underlying,
    InstrumentResolutionError, DataQualityError, _qc_underlying,
)
from universe.store import UniverseStore, config_fingerprint


class FakeContract:
    def __init__(self, conId, symbol="SPY", currency="USD", exchange="SMART",
                 primaryExchange="ARCA"):
        self.conId = conId
        self.symbol = symbol
        self.secType = "STK"
        self.currency = currency
        self.exchange = exchange
        self.primaryExchange = primaryExchange
        self.localSymbol = symbol
        self.tradingClass = symbol
        self.multiplier = ""
        self.lastTradeDateOrContractMonth = ""
        self.strike = 0.0
        self.right = ""


class FakeIB:
    def __init__(self, qualified):
        self._qualified = qualified
        self._connected = False
    def connect(self, *a, **k): self._connected = True
    def isConnected(self): return self._connected
    def disconnect(self): self._connected = False
    def reqCurrentTime(self): return dt.datetime.now(dt.timezone.utc)
    def qualifyContracts(self, contract): return list(self._qualified)


def _session(qualified):
    cfg = load_config(load_env=False)
    s = IBSession(cfg.ibkr, ib_factory=lambda: FakeIB(qualified), sleep=lambda s: None)
    s.connect()
    return s


# ---- schema keys ----
def test_keys_are_stable_and_uppercase():
    assert underlying_key("spy", "stk", "usd") == "STK:SPY:USD"
    k = option_key("SPY", "USD", dt.date(2026, 6, 19), 500.0, "C")
    assert k == "OPT:SPY:USD:20260619:500:C"


# ---- resolution ----
def test_resolve_underlying_success():
    s = _session([FakeContract(conId=756733)])
    inst, raw = resolve_underlying(s, "SPY")
    assert inst.canonical_key == "STK:SPY:USD"
    assert inst.con_id == 756733
    assert inst.multiplier == 1.0
    assert inst.currency == "USD"
    assert raw["conId"] == 756733  # raw evidence captured


def test_resolve_unresolved_raises_with_diagnostics():
    s = _session([])  # broker returns nothing
    with pytest.raises(InstrumentResolutionError) as ei:
        resolve_underlying(s, "NOPE")
    assert ei.value.symbol == "NOPE"
    assert "reason" in ei.value.diagnostics


def test_resolve_ambiguous_raises():
    s = _session([FakeContract(conId=1), FakeContract(conId=2)])
    with pytest.raises(InstrumentResolutionError) as ei:
        resolve_underlying(s, "SPY")
    assert "ambiguous" in ei.value.diagnostics["reason"]


def test_resolution_is_deterministic():
    s = _session([FakeContract(conId=756733)])
    a = get_underlying(s, "SPY")
    b = get_underlying(s, "SPY")
    assert a == b  # frozen dataclass equality


# ---- data quality ----
def test_qc_rejects_missing_currency():
    bad = UnderlyingInstrument(canonical_key="STK:X:", symbol="X", sec_type="STK", currency="", exchange="SMART", primary_exchange=None, con_id=1, multiplier=1.0, listing_status="ACTIVE")
    with pytest.raises(DataQualityError):
        _qc_underlying(bad)


def test_qc_rejects_impossible_multiplier():
    bad = UnderlyingInstrument(canonical_key="STK:X:USD", symbol="X", sec_type="STK", currency="USD", exchange="SMART", primary_exchange=None, con_id=1, multiplier=0.0, listing_status="ACTIVE")
    with pytest.raises(DataQualityError):
        _qc_underlying(bad)


def test_qc_rejects_missing_conid():
    bad = UnderlyingInstrument(canonical_key="STK:X:USD", symbol="X", sec_type="STK", currency="USD", exchange="SMART", primary_exchange=None, con_id=None, multiplier=1.0, listing_status="ACTIVE")
    with pytest.raises(DataQualityError):
        _qc_underlying(bad)


# ---- persistence / versioning ----
def test_store_roundtrip_and_versioning(tmp_path):
    store = UniverseStore(tmp_path)
    s = _session([FakeContract(conId=756733)])
    inst, raw = resolve_underlying(s, "SPY")
    fp = config_fingerprint({"source": "sp500", "currency": "USD"})
    out = store.save_underlyings("2026-06-11", [inst], [raw], fp)

    assert out.name == "2026-06-11"               # versioned by date
    assert (out / "underlyings_raw.json").exists()  # raw evidence persisted
    loaded = store.load_underlyings("2026-06-11")
    assert loaded == [inst]                        # canonical roundtrip
    manifest = store.load_manifest("2026-06-11")
    assert manifest["config_fingerprint"] == fp
    assert manifest["underlying_count"] == 1


def test_fingerprint_is_deterministic():
    a = config_fingerprint({"b": 2, "a": 1})
    b = config_fingerprint({"a": 1, "b": 2})
    assert a == b  # key order independent
