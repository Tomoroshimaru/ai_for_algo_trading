"""Offline tests for the storage layer and data model (Step 4)."""
import datetime as dt

import pandas as pd
import pyarrow as pa
import pytest

from storage.schemas import DATASETS, SCHEMA_VERSION, DataLayer, get_dataset
from storage.parquet_store import (
    ParquetStore, SchemaValidationError, validate,
    migrate_raw_events_to_parquet, lineage_raw_for_snapshot, underlying_from_key,
)
from storage.metadata import MetadataStore
from ingestion.store import RawEventStore
from ingestion.events import MarketEvent

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 6, 11, 14, 0, tzinfo=UTC)
TD = "2026-06-11"


def test_all_required_datasets_exist():
    required = {
        "raw_events", "market_state", "forwards", "iv_points", "surface_params",
        "model_prices", "greeks", "scenarios", "positions", "qc_results",
    }
    assert required == set(DATASETS)


def test_schemas_are_flat_and_utc():
    for ds in DATASETS.values():
        for f in ds.schema:
            assert not pa.types.is_nested(f.type), f"{ds.name}.{f.name} is nested"
            if pa.types.is_timestamp(f.type):
                assert f.type.tz == "UTC", f"{ds.name}.{f.name} not UTC"
        assert "trade_date" in ds.schema.names and "schema_version" in ds.schema.names


def _raw_df():
    return pd.DataFrame([{
        "session_id": "s1", "instrument_key": "STK:SPY:USD", "field": "bid",
        "value": 100.0, "source_ts": T0, "receipt_ts": T0, "collector_ts": T0,
    }])


def test_write_read_roundtrip_partitioned(tmp_path):
    store = ParquetStore(tmp_path)
    path = store.write_partition("raw_events", TD, "SPY", _raw_df())
    assert "trade_date=2026-06-11" in str(path) and "underlying=SPY" in str(path)
    out = store.read("raw_events", trade_date=TD, underlying="SPY")
    assert len(out) == 1
    assert out.iloc[0]["schema_version"] == SCHEMA_VERSION
    assert out.iloc[0]["underlying"] == "SPY"


def test_write_ahead_validation_missing_column(tmp_path):
    store = ParquetStore(tmp_path)
    bad = _raw_df().drop(columns=["value"])
    with pytest.raises(SchemaValidationError, match="missing required columns"):
        store.write_partition("raw_events", TD, "SPY", bad)


def test_write_ahead_validation_null_in_non_nullable(tmp_path):
    store = ParquetStore(tmp_path)
    bad = _raw_df()
    bad["instrument_key"] = None     # non-nullable in schema
    with pytest.raises(SchemaValidationError):
        store.write_partition("raw_events", TD, "SPY", bad)


def test_recompute_derived_partition_leaves_raw_untouched(tmp_path):
    store = ParquetStore(tmp_path)
    store.write_partition("raw_events", TD, "SPY", _raw_df())
    raw_path = store._partition_dir("raw_events", TD, "SPY") / "data.parquet"
    mtime0 = raw_path.stat().st_mtime_ns
    # write + recompute a derived partition twice
    fwd = pd.DataFrame([{"snapshot_ts": T0, "expiry": "2026-12-18",
                         "forward": 101.0, "implied_carry": 0.01, "method": "putcall",
                         "source_session_id": "s1"}])
    store.write_partition("forwards", TD, "SPY", fwd)
    store.write_partition("forwards", TD, "SPY", fwd)   # recompute same partition
    assert raw_path.stat().st_mtime_ns == mtime0        # raw never rewritten
    assert len(store.read("forwards", trade_date=TD, underlying="SPY")) == 1


def test_overwrite_one_partition_keeps_others(tmp_path):
    store = ParquetStore(tmp_path)
    store.write_partition("raw_events", TD, "SPY", _raw_df())
    aapl = _raw_df(); aapl["instrument_key"] = "STK:AAPL:USD"
    store.write_partition("raw_events", TD, "AAPL", aapl)
    store.write_partition("raw_events", TD, "SPY", _raw_df())   # rewrite SPY only
    assert len(store.read("raw_events", trade_date=TD, underlying="AAPL")) == 1
    assert len(store.read("raw_events", trade_date=TD)) == 2     # both partitions


def test_underlying_from_key():
    assert underlying_from_key("STK:SPY:USD") == "SPY"
    assert underlying_from_key("OPT:SPY:20261218:500:C") == "SPY"


def test_migrate_raw_jsonl_to_parquet_and_lineage(tmp_path):
    raw = RawEventStore(tmp_path)
    raw.append_market(TD, "s1", [
        MarketEvent("s1", "STK:SPY:USD", "bid", 100.0, None, T0, T0),
        MarketEvent("s1", "STK:SPY:USD", "ask", 100.5, None, T0, T0),
    ])
    store = ParquetStore(tmp_path)
    n = migrate_raw_events_to_parquet(raw, store, TD, "s1")
    assert n == 2
    lin = lineage_raw_for_snapshot(store, TD, "SPY", "s1")
    assert len(lin) == 2 and set(lin["field"]) == {"bid", "ask"}
    assert lineage_raw_for_snapshot(store, TD, "SPY", "other").empty


def test_metadata_migrations_idempotent_and_registry(tmp_path):
    md = MetadataStore(tmp_path)
    assert md.migrate() == 1
    assert md.migrate() == 1            # idempotent
    md.register_all_schemas()
    md.register_all_schemas()           # INSERT OR REPLACE -> still 10
    schemas = md.list_schemas()
    assert len(schemas) == len(DATASETS)
    jid = md.record_job("collector", "OK", "ran", TD)
    assert jid >= 1
    assert md.retention_days("raw") == 30
    assert md.retention_days("derived") == 730
