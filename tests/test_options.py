"""Offline tests for option-chain discovery + persistence (Step 2c/d/e)."""
import datetime as dt

import pytest

from utils.config import load_config
from connectivity.session import IBSession
from universe.schema import OptionChain
from universe.master import (
    normalize_expiry, select_standard_chain, get_option_chain,
    materialize_options, load_active_universe,
    InstrumentResolutionError, DataQualityError,
)
from universe.store import UniverseStore, config_fingerprint


class FakeParam:
    """Mimics an ib_insync OptionChain params entry."""
    def __init__(self, exchange, tradingClass, multiplier, expirations, strikes,
                 underlyingConId=756733):
        self.exchange = exchange
        self.tradingClass = tradingClass
        self.multiplier = multiplier
        self.expirations = set(expirations)
        self.strikes = set(strikes)
        self.underlyingConId = underlyingConId


class FakeUnderlying:
    symbol = "SPY"; sec_type = "STK"; currency = "USD"; con_id = 756733


class FakeIB:
    def __init__(self, params):
        self._params = params; self._connected = False
    def connect(self, *a, **k): self._connected = True
    def isConnected(self): return self._connected
    def disconnect(self): self._connected = False
    def reqCurrentTime(self): return dt.datetime.now(dt.timezone.utc)
    def qualifyContracts(self, c):
        class C:  # SPY stock
            conId=756733; symbol="SPY"; secType="STK"; currency="USD"
            exchange="SMART"; primaryExchange="ARCA"; localSymbol="SPY"
            tradingClass="SPY"; multiplier=""; lastTradeDateOrContractMonth=""
            strike=0.0; right=""
        return [C()]
    def reqSecDefOptParams(self, sym, ftpExch, secType, conId):
        return list(self._params)


def _session(params):
    cfg = load_config(load_env=False)
    s = IBSession(cfg.ibkr, ib_factory=lambda: FakeIB(params), sleep=lambda s: None)
    s.connect()
    return s


def test_normalize_expiry_ok_and_bad():
    assert normalize_expiry("20260619") == dt.date(2026, 6, 19)
    with pytest.raises(DataQualityError):
        normalize_expiry("202606")


def test_select_standard_chain_merges_union():
    params = [
        FakeParam("NASDAQOM", "SPY", "100", ["20260619", "20260717"], [500, 510]),
        FakeParam("AMEX", "SPY", "100", ["20260619", "20260821"], [510, 520]),
        FakeParam("SMART", "2SPY", "100", ["20260612"], [609]),  # mini, ignored
    ]
    chain = select_standard_chain(params, "SPY", "USD")
    assert chain.trading_class == "SPY"
    assert chain.multiplier == 100.0
    assert chain.expirations == (dt.date(2026,6,19), dt.date(2026,7,17), dt.date(2026,8,21))
    assert chain.strikes == (500.0, 510.0, 520.0)  # union, sorted, deduped


def test_select_standard_chain_no_standard_raises():
    params = [FakeParam("SMART", "2SPY", "100", ["20260612"], [609])]
    with pytest.raises(InstrumentResolutionError):
        select_standard_chain(params, "SPY", "USD")


def test_select_standard_chain_inconsistent_multiplier_raises():
    params = [
        FakeParam("NASDAQOM", "SPY", "100", ["20260619"], [500]),
        FakeParam("AMEX", "SPY", "10", ["20260619"], [500]),
    ]
    with pytest.raises(DataQualityError):
        select_standard_chain(params, "SPY", "USD")


def test_get_option_chain_via_session():
    params = [FakeParam("NASDAQOM", "SPY", "100", ["20260619"], [500, 510])]
    s = _session(params)
    chain = get_option_chain(s, FakeUnderlying())
    assert isinstance(chain, OptionChain)
    assert chain.underlying_con_id == 756733


def test_materialize_options_counts_and_keys():
    chain = OptionChain("SPY", 756733, "USD", "SMART", "SPY", 100.0,
                        (dt.date(2026,6,19), dt.date(2026,7,17)), (500.0, 510.0))
    opts = materialize_options(chain)
    assert len(opts) == 2 * 2 * 2  # expiries x strikes x rights
    keys = {o.canonical_key for o in opts}
    assert "OPT:SPY:USD:20260619:500:C" in keys
    assert all(o.multiplier == 100.0 and o.con_id is None for o in opts)


def test_materialize_options_maturity_window():
    chain = OptionChain("SPY", 1, "USD", "SMART", "SPY", 100.0,
                        (dt.date(2026,6,19), dt.date(2026,12,18)), (500.0,))
    ref = dt.date(2026, 6, 11)
    opts = materialize_options(chain, session_date=ref, maturity_days=30)
    # only the 2026-06-19 expiry is within 30 days
    assert {o.expiry for o in opts} == {dt.date(2026, 6, 19)}


def test_store_options_parquet_roundtrip(tmp_path):
    chain = OptionChain("SPY", 1, "USD", "SMART", "SPY", 100.0,
                        (dt.date(2026,6,19),), (500.0, 510.0))
    opts = materialize_options(chain)
    store = UniverseStore(tmp_path)
    path = store.save_options("2026-06-11", opts)
    assert path.suffix == ".parquet"
    df = store.load_options("2026-06-11")
    assert len(df) == len(opts) == 4
    assert set(df["right"]) == {"C", "P"}
    manifest = store.load_manifest("2026-06-11")
    assert manifest["option_count"] == 4


def test_load_active_universe_orchestration(tmp_path):
    params = [FakeParam("NASDAQOM", "SPY", "100", ["20260619"], [500, 510])]
    s = _session(params)
    store = UniverseStore(tmp_path)
    summary = load_active_universe(
        s, ["SPY"], dt.date(2026, 6, 11), store=store,
        config_fp=config_fingerprint({"x": 1}),
    )
    assert summary["underlying_count"] == 1
    assert summary["option_count"] == 4  # 1 expiry x 2 strikes x 2 rights
    # both layers persisted under the same session date
    assert store.load_underlyings("2026-06-11")[0].symbol == "SPY"
    assert len(store.load_options("2026-06-11")) == 4
    m = store.load_manifest("2026-06-11")
    assert m["underlying_count"] == 1 and m["option_count"] == 4
