# Data model & storage (Step 4)

## Layers
| Layer | Meaning | Retention (default) |
|-------|---------|---------------------|
| `raw` | Immutable evidentiary records (ticks/events) | 30 days |
| `normalized` | Deterministic market-state snapshots | 365 days |
| `derived` | Analytics recomputable from normalized/raw | 730 days |

Retention is stored in the metadata DB (`retention_policy` table) and editable
without code changes.

## Two stores
- **Metadata store** (`metadata.sqlite`): config, jobs, reference instruments,
  schema registry, retention. Migrations are idempotent via `PRAGMA user_version`.
- **Parquet warehouse**: large raw/derived datasets, columnar + partitioned.

## Partitioning
```
warehouse/<layer>/<dataset>/trade_date=YYYY-MM-DD/underlying=<SYM>/data.parquet
```
Daily queries filter on `trade_date` (and optionally `underlying`) and touch only
the relevant partition. Recomputing one derived partition rewrites only that
directory; the raw layer is never rewritten.

## Schemas
Defined in `src/storage/schemas.py`, version `SCHEMA_VERSION`. All 10 datasets:
raw_events, market_state, forwards, iv_points, surface_params, model_prices,
greeks, scenarios, positions, qc_results. Rules:
- Flat columns only (no nested structures).
- Every timestamp is `timestamp[us, tz=UTC]`; never mix time zones in one field.
- Every row carries `trade_date` + `schema_version`.
- Replay and live writes use the **same** schema (single code path).

## Schema evolution
- Bump `SCHEMA_VERSION` and add a metadata migration when fields change.
- Backfill compatibility: readers tolerate older `schema_version` rows; writers
  always stamp the current version. Extra (non-schema) columns are dropped with
  an explicit log; missing required columns are rejected (write-ahead validation).

## Write-ahead validation
`ParquetStore.write_partition` validates before any bytes are written:
missing required columns, nulls in non-nullable columns, and type mismatches all
raise `SchemaValidationError` with an explicit message.

## Lineage — "which raw records produced this snapshot?"
Every normalized/derived row carries `source_session_id`. One call answers it:
```python
from storage.parquet_store import ParquetStore, lineage_raw_for_snapshot
store = ParquetStore(data_dir)
raw_rows = lineage_raw_for_snapshot(store, "2026-06-11", "SPY", session_id)
```
Equivalent one-liner over the raw dataset:
```python
raw = store.read("raw_events", trade_date="2026-06-11", underlying="SPY")
raw[raw.session_id == session_id]
```

## Market-state snapshots (Step 5)
The `market_state` dataset is produced by the **pure** builder in
`src/snapshots/builder.py` (raw events in, snapshots out; no I/O, no clock
reads). Guarantees:
- **Deterministic**: same events + same `SnapshotParams` -> identical rows.
- **Reference spot**: mid when the bid/ask spread is within `max_spread_pct`,
  otherwise documented fallbacks `last` -> `close` -> `mid_wide`, recorded in
  `reference_type` (fallbacks are labeled, never hidden). Chosen spot in
  `reference_price`.
- **Staleness**: `age_sec` is the gap between `snapshot_ts` and the freshest
  eligible quote; `is_stale = age_sec > max_age_sec`.
- **Option join**: each instrument takes its most recent quote at or before
  `snapshot_ts`; quotes after the snapshot are excluded.
- **Completeness**: `compute_completeness` reports fresh-observation fraction
  per (underlying, maturity), bounded in [0, 1].

## Forward & implied-carry engine (Step 6)
The **pure** engine in `src/forward/engine.py` turns a market-state snapshot into
a robust forward per maturity plus a full diagnostics trail.
- **Parity forward** per strike: `F = K + (C - P) / DF`, `DF = exp(-r*T)`
  (`DF = 1` when no rate is supplied; assumption recorded in `method`).
- **Liquidity weighting**: `weight = 1 / (1 + call_spread% + put_spread%)`,
  stale legs get weight 0.
- **Robust estimate**: weighted median of per-strike forwards; outliers rejected
  by MAD with a relative parity-residual floor (so MAD=0 still rejects gross
  outliers). A few bad pairs cannot move the forward.
- **Implied carry**: `ln(F / spot) / T` (cost-of-carry rate).
- **Outputs**: `forwards` (forward, implied_carry, n_pairs, is_reliable) and
  `forward_diagnostics` (per-strike call/put mids, parity_forward, weight,
  residual, quality_label in {inlier, outlier, stale, incomplete}) so any
  poor-quality maturity is explainable from stored data alone.
