"""Versioned, explicit dataset schemas and data-layer taxonomy (Step 4d/e).

Schemas are intentionally flat and readable (no nested structures) and every
timestamp is stored in UTC, never mixing time zones in one field
(cours.txt:611-614). The same schema is used for replay and live writes so the
two paths cannot diverge. Lineage columns (``source_session_id``) let us answer
"which raw records produced this snapshot?" with a single filter.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pyarrow as pa

SCHEMA_VERSION = 1


class DataLayer(str, Enum):
    RAW = "raw"            # immutable evidentiary records
    NORMALIZED = "normalized"  # deterministic market-state snapshots
    DERIVED = "derived"    # analytics recomputable from normalized/raw


_TS = pa.timestamp("us", tz="UTC")


def _with_common(fields: list[pa.Field]) -> pa.Schema:
    """Append partition/version columns shared by every dataset."""
    return pa.schema(
        fields
        + [
            pa.field("trade_date", pa.string(), nullable=False),       # YYYY-MM-DD
            pa.field("schema_version", pa.int32(), nullable=False),
        ]
    )


@dataclass(frozen=True)
class Dataset:
    name: str
    layer: DataLayer
    schema: pa.Schema
    partition_cols: tuple[str, ...] = ("trade_date", "underlying")


# --- RAW ------------------------------------------------------------------- #
_RAW_EVENTS = _with_common([
    pa.field("session_id", pa.string(), nullable=False),
    pa.field("underlying", pa.string(), nullable=False),
    pa.field("instrument_key", pa.string(), nullable=False),
    pa.field("field", pa.string(), nullable=False),
    pa.field("value", pa.float64(), nullable=False),
    pa.field("source_ts", _TS, nullable=True),
    pa.field("receipt_ts", _TS, nullable=False),
    pa.field("collector_ts", _TS, nullable=False),
])

# --- NORMALIZED ------------------------------------------------------------ #
_MARKET_STATE = _with_common([
    pa.field("snapshot_ts", _TS, nullable=False),
    pa.field("underlying", pa.string(), nullable=False),
    pa.field("instrument_key", pa.string(), nullable=False),
    pa.field("bid", pa.float64()), pa.field("ask", pa.float64()),
    pa.field("last", pa.float64()), pa.field("mid", pa.float64()),
    pa.field("spread_pct", pa.float64()),
    pa.field("reference_type", pa.string()),   # mid | last | close | fallback
    pa.field("is_stale", pa.bool_()),
    pa.field("age_sec", pa.float64()),
    pa.field("source_session_id", pa.string(), nullable=False),  # lineage
])

# --- DERIVED --------------------------------------------------------------- #
_FORWARDS = _with_common([
    pa.field("snapshot_ts", _TS, nullable=False),
    pa.field("underlying", pa.string(), nullable=False),
    pa.field("expiry", pa.string(), nullable=False),  # YYYY-MM-DD
    pa.field("forward", pa.float64(), nullable=False),
    pa.field("implied_carry", pa.float64()),
    pa.field("method", pa.string(), nullable=False),
    pa.field("source_session_id", pa.string()),
])

_IV_POINTS = _with_common([
    pa.field("snapshot_ts", _TS, nullable=False),
    pa.field("underlying", pa.string(), nullable=False),
    pa.field("expiry", pa.string(), nullable=False),
    pa.field("strike", pa.float64(), nullable=False),
    pa.field("option_right", pa.string(), nullable=False),  # C | P
    pa.field("iv", pa.float64()),
    pa.field("moneyness", pa.float64()),
    pa.field("forward", pa.float64()),
    pa.field("method", pa.string()),
    pa.field("source_session_id", pa.string()),
])

_SURFACE_PARAMS = _with_common([
    pa.field("snapshot_ts", _TS, nullable=False),
    pa.field("underlying", pa.string(), nullable=False),
    pa.field("expiry", pa.string(), nullable=False),
    pa.field("model", pa.string(), nullable=False),
    pa.field("param_name", pa.string(), nullable=False),   # flat key/value, no nesting
    pa.field("param_value", pa.float64(), nullable=False),
    pa.field("source_session_id", pa.string()),
])

_MODEL_PRICES = _with_common([
    pa.field("snapshot_ts", _TS, nullable=False),
    pa.field("underlying", pa.string(), nullable=False),
    pa.field("instrument_key", pa.string(), nullable=False),
    pa.field("expiry", pa.string()), pa.field("strike", pa.float64()),
    pa.field("option_right", pa.string()),
    pa.field("price", pa.float64(), nullable=False),
    pa.field("model", pa.string(), nullable=False),
    pa.field("source_session_id", pa.string()),
])

_GREEKS = _with_common([
    pa.field("snapshot_ts", _TS, nullable=False),
    pa.field("underlying", pa.string(), nullable=False),
    pa.field("instrument_key", pa.string(), nullable=False),
    pa.field("delta", pa.float64()), pa.field("gamma", pa.float64()),
    pa.field("vega", pa.float64()), pa.field("theta", pa.float64()),
    pa.field("rho", pa.float64()),
    pa.field("model", pa.string(), nullable=False),
    pa.field("source_session_id", pa.string()),
])

_SCENARIOS = _with_common([
    pa.field("scenario_id", pa.string(), nullable=False),
    pa.field("snapshot_ts", _TS, nullable=False),
    pa.field("underlying", pa.string(), nullable=False),
    pa.field("shock_type", pa.string(), nullable=False),   # spot | vol | time
    pa.field("shock_value", pa.float64(), nullable=False),
    pa.field("pnl", pa.float64()),
    pa.field("source_session_id", pa.string()),
])

_POSITIONS = _with_common([
    pa.field("as_of_ts", _TS, nullable=False),
    pa.field("account", pa.string(), nullable=False),
    pa.field("underlying", pa.string(), nullable=False),
    pa.field("instrument_key", pa.string(), nullable=False),
    pa.field("quantity", pa.float64(), nullable=False),
    pa.field("avg_cost", pa.float64()),
])

_QC_RESULTS = _with_common([
    pa.field("check_ts", _TS, nullable=False),
    pa.field("underlying", pa.string(), nullable=False),
    pa.field("check_name", pa.string(), nullable=False),
    pa.field("target", pa.string()),
    pa.field("status", pa.string(), nullable=False),   # PASS | WARN | FAIL
    pa.field("detail", pa.string()),
])


DATASETS: dict[str, Dataset] = {
    "raw_events": Dataset("raw_events", DataLayer.RAW, _RAW_EVENTS),
    "market_state": Dataset("market_state", DataLayer.NORMALIZED, _MARKET_STATE),
    "forwards": Dataset("forwards", DataLayer.DERIVED, _FORWARDS),
    "iv_points": Dataset("iv_points", DataLayer.DERIVED, _IV_POINTS),
    "surface_params": Dataset("surface_params", DataLayer.DERIVED, _SURFACE_PARAMS),
    "model_prices": Dataset("model_prices", DataLayer.DERIVED, _MODEL_PRICES),
    "greeks": Dataset("greeks", DataLayer.DERIVED, _GREEKS),
    "scenarios": Dataset("scenarios", DataLayer.DERIVED, _SCENARIOS),
    "positions": Dataset("positions", DataLayer.DERIVED, _POSITIONS),
    "qc_results": Dataset("qc_results", DataLayer.DERIVED, _QC_RESULTS),
}


def get_dataset(name: str) -> Dataset:
    try:
        return DATASETS[name]
    except KeyError:
        raise KeyError(
            f"Unknown dataset '{name}'. Known: {sorted(DATASETS)}"
        ) from None
