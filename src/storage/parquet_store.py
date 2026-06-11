"""Columnar, partitioned Parquet store with write-ahead validation (Step 4b/c/g).

Layout: ``<root>/<layer>/<dataset>/trade_date=<d>/underlying=<u>/data.parquet``.
Partitioning by trade date + underlying makes daily queries cheap and lets us
recompute a single derived partition without touching the raw layer
(cours.txt:617-620). Every write is validated against the dataset schema first;
malformed records are rejected early with an explicit log (write-ahead
validation), never silently coerced.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from storage.schemas import SCHEMA_VERSION, DATASETS, get_dataset

logger = logging.getLogger(__name__)


class SchemaValidationError(ValueError):
    """Raised when a record batch does not conform to the dataset schema."""


def validate(dataset_name: str, df: pd.DataFrame) -> pa.Table:
    """Write-ahead validation: enforce columns, types, and non-null fields."""
    ds = get_dataset(dataset_name)
    schema = ds.schema
    expected = set(schema.names)
    actual = set(df.columns)

    missing = expected - actual
    if missing:
        raise SchemaValidationError(
            f"[{dataset_name}] missing required columns: {sorted(missing)}"
        )
    extra = actual - expected
    if extra:
        logger.info("[%s] dropping non-schema columns: %s", dataset_name, sorted(extra))
        df = df.drop(columns=list(extra))

    df = df[list(schema.names)]
    # pyarrow does not enforce field nullability on from_pandas, so check here.
    non_nullable = [f.name for f in schema if not f.nullable]
    null_cols = [c for c in non_nullable if df[c].isna().any()]
    if null_cols:
        raise SchemaValidationError(
            f"[{dataset_name}] null values in non-nullable columns: {sorted(null_cols)}"
        )
    try:
        table = pa.Table.from_pandas(df, schema=schema, preserve_index=False)
    except (pa.lib.ArrowInvalid, pa.lib.ArrowTypeError, TypeError) as exc:
        raise SchemaValidationError(
            f"[{dataset_name}] type/null violation against schema: {exc}"
        ) from exc
    return table


class ParquetStore:
    def __init__(self, data_dir: Path) -> None:
        self.root = Path(data_dir) / "warehouse"

    def _dataset_root(self, dataset_name: str) -> Path:
        ds = get_dataset(dataset_name)
        return self.root / ds.layer.value / dataset_name

    def _partition_dir(self, dataset_name: str, trade_date: str, underlying: str) -> Path:
        return (
            self._dataset_root(dataset_name)
            / f"trade_date={trade_date}"
            / f"underlying={underlying}"
        )

    def write_partition(
        self, dataset_name: str, trade_date: str, underlying: str, df: pd.DataFrame
    ) -> Path:
        """Validate then (over)write exactly one partition; other partitions untouched."""
        if df.empty:
            raise SchemaValidationError(f"[{dataset_name}] refusing to write empty frame")
        df = df.copy()
        df["trade_date"] = trade_date
        df["underlying"] = underlying
        df["schema_version"] = SCHEMA_VERSION
        table = validate(dataset_name, df)
        part = self._partition_dir(dataset_name, trade_date, underlying)
        if part.exists():
            shutil.rmtree(part)        # idempotent recompute of this partition only
        part.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, part / "data.parquet")
        logger.info("wrote %d rows -> %s", table.num_rows, part)
        return part / "data.parquet"

    def read(
        self,
        dataset_name: str,
        *,
        trade_date: str | None = None,
        underlying: str | None = None,
    ) -> pd.DataFrame:
        """Read a dataset, optionally filtered by partition keys (efficient daily query)."""
        root = self._dataset_root(dataset_name)
        if not root.exists():
            return get_dataset(dataset_name).schema.empty_table().to_pandas()
        base = root
        if trade_date is not None:
            base = base / f"trade_date={trade_date}"
            if underlying is not None:
                base = base / f"underlying={underlying}"
        if not base.exists():
            return get_dataset(dataset_name).schema.empty_table().to_pandas()
        files = sorted(base.rglob("*.parquet"))
        if not files:
            return get_dataset(dataset_name).schema.empty_table().to_pandas()
        return pd.concat((pd.read_parquet(f) for f in files), ignore_index=True)


def underlying_from_key(instrument_key: str) -> str:
    """'STK:SPY:USD' -> 'SPY'; 'OPT:SPY:20260101:500:C' -> 'SPY'."""
    parts = instrument_key.split(":")
    return parts[1] if len(parts) > 1 else instrument_key


def migrate_raw_events_to_parquet(
    raw_store, parquet_store: ParquetStore, trade_date: str, session_id: str
) -> int:
    """Bridge Step 3 (JSONL) -> Step 4 (Parquet): identical schema, replayable."""
    events, corrupt = raw_store.replay_market(trade_date, session_id)
    if corrupt:
        logger.warning("replay found %d corrupt lines for %s", corrupt, session_id)
    if not events:
        return 0
    rows = []
    for e in events:
        rows.append(
            {
                "session_id": e.session_id,
                "underlying": underlying_from_key(e.instrument_key),
                "instrument_key": e.instrument_key,
                "field": e.field,
                "value": e.value,
                "source_ts": e.source_ts,
                "receipt_ts": e.receipt_ts,
                "collector_ts": e.collector_ts,
            }
        )
    df = pd.DataFrame(rows)
    total = 0
    for underlying, grp in df.groupby("underlying"):
        parquet_store.write_partition("raw_events", trade_date, underlying, grp)
        total += len(grp)
    return total


def lineage_raw_for_snapshot(
    parquet_store: ParquetStore,
    trade_date: str,
    underlying: str,
    session_id: str,
) -> pd.DataFrame:
    """Answer 'which raw records produced this snapshot?' in one call."""
    raw = parquet_store.read("raw_events", trade_date=trade_date, underlying=underlying)
    if raw.empty:
        return raw
    return raw[raw["session_id"] == session_id].reset_index(drop=True)
