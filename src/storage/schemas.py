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
    pa.field("reference_price", pa.float64()),  # chosen spot
    pa.field("reference_type", pa.string()),    # mid | last | close | mid_wide | none
    pa.field("is_stale", pa.bool_()),
    pa.field("is_market_open", pa.bool_()),
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
    pa.field("n_pairs", pa.int32()),
    pa.field("is_reliable", pa.bool_()),
    pa.field("source_session_id", pa.string()),
])

_FORWARD_DIAGNOSTICS = _with_common([
    pa.field("snapshot_ts", _TS, nullable=False),
    pa.field("underlying", pa.string(), nullable=False),
    pa.field("expiry", pa.string(), nullable=False),
    pa.field("strike", pa.float64(), nullable=False),
    pa.field("call_mid", pa.float64()),
    pa.field("put_mid", pa.float64()),
    pa.field("parity_forward", pa.float64()),
    pa.field("weight", pa.float64()),
    pa.field("residual", pa.float64()),
    pa.field("quality_label", pa.string(), nullable=False),  # inlier|outlier|stale|incomplete
    pa.field("source_session_id", pa.string()),
])

_IV_POINTS = _with_common([
    pa.field("snapshot_ts", _TS, nullable=False),
    pa.field("underlying", pa.string(), nullable=False),
    pa.field("expiry", pa.string(), nullable=False),
    pa.field("strike", pa.float64(), nullable=False),
    pa.field("option_right", pa.string(), nullable=False),  # C | P
    pa.field("iv", pa.float64()),
    pa.field("moneyness", pa.float64()),         # log(K / F)
    pa.field("forward", pa.float64()),
    pa.field("delta", pa.float64()),
    pa.field("ttm_years", pa.float64()),
    pa.field("method", pa.string()),             # black76 | american_proxy
    pa.field("status", pa.string()),             # solved | near_intrinsic | no_arbitrage | short_dated | no_bracket
    pa.field("n_iter", pa.int32()),
    pa.field("residual", pa.float64()),
    pa.field("bracket_lo", pa.float64()),
    pa.field("bracket_hi", pa.float64()),
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
    pa.field("account", pa.string()),
    pa.field("expiry", pa.string()),
    pa.field("option_right", pa.string()),
    pa.field("strike", pa.float64()),
    pa.field("quantity", pa.float64()),
    pa.field("multiplier", pa.float64()),
    pa.field("spot", pa.float64()),
    pa.field("vol", pa.float64()),
    pa.field("price", pa.float64()),
    pa.field("position_value", pa.float64()),
    pa.field("delta", pa.float64()), pa.field("gamma", pa.float64()),
    pa.field("vega", pa.float64()), pa.field("theta", pa.float64()),
    pa.field("rho", pa.float64()),
    pa.field("vanna", pa.float64()), pa.field("volga", pa.float64()),
    pa.field("dollar_delta", pa.float64()),
    pa.field("dollar_gamma", pa.float64()),
    pa.field("dollar_vega", pa.float64()),
    pa.field("dollar_theta", pa.float64()),
    pa.field("status", pa.string()),
    pa.field("model", pa.string(), nullable=False),
    pa.field("source_session_id", pa.string()),
])

_RISK_AGGREGATES = _with_common([
    pa.field("snapshot_ts", _TS, nullable=False),
    pa.field("underlying", pa.string(), nullable=False),
    pa.field("group_key", pa.string(), nullable=False),    # portfolio|underlying|expiry|instrument
    pa.field("group_value", pa.string(), nullable=False),
    pa.field("n_lines", pa.int32()),
    pa.field("n_total", pa.int32()),
    pa.field("coverage", pa.float64()),
    pa.field("position_value", pa.float64()),
    pa.field("delta", pa.float64()), pa.field("gamma", pa.float64()),
    pa.field("vega", pa.float64()), pa.field("theta", pa.float64()),
    pa.field("rho", pa.float64()),
    pa.field("dollar_delta", pa.float64()),
    pa.field("dollar_gamma", pa.float64()),
    pa.field("dollar_vega", pa.float64()),
    pa.field("dollar_theta", pa.float64()),
    pa.field("model", pa.string(), nullable=False),
    pa.field("source_session_id", pa.string()),
])

_RISK_RECON = _with_common([
    pa.field("snapshot_ts", _TS, nullable=False),
    pa.field("underlying", pa.string(), nullable=False),
    pa.field("instrument_key", pa.string(), nullable=False),
    pa.field("greek", pa.string(), nullable=False),
    pa.field("computed", pa.float64()),
    pa.field("broker", pa.float64()),
    pa.field("diff", pa.float64()),
    pa.field("abs_diff", pa.float64()),
    pa.field("threshold", pa.float64()),
    pa.field("breach", pa.bool_()),
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


_SURFACE_GRID = _with_common([
    pa.field("snapshot_ts", _TS, nullable=False),
    pa.field("underlying", pa.string(), nullable=False),
    pa.field("expiry", pa.string(), nullable=False),
    pa.field("log_moneyness", pa.float64(), nullable=False),
    pa.field("ttm_years", pa.float64()),
    pa.field("total_variance", pa.float64()),
    pa.field("iv", pa.float64()),
    pa.field("model", pa.string()),
    pa.field("source_session_id", pa.string()),
])

_SCENARIO_DEFS = _with_common([
    pa.field("version", pa.string(), nullable=False),
    pa.field("scenario_id", pa.string(), nullable=False),
    pa.field("underlying", pa.string(), nullable=False),
    pa.field("family", pa.string(), nullable=False),       # spot|vol|time|joint
    pa.field("spot_shock", pa.float64()),                  # relative, -0.1 = -10%
    pa.field("vol_shock", pa.float64()),                   # absolute vol points
    pa.field("time_roll_days", pa.float64()),              # calendar days rolled fwd
    pa.field("label", pa.string()),
])

_SCENARIO_RESULTS = _with_common([
    pa.field("snapshot_ts", _TS, nullable=False),
    pa.field("version", pa.string(), nullable=False),
    pa.field("scenario_id", pa.string(), nullable=False),
    pa.field("underlying", pa.string(), nullable=False),
    pa.field("instrument_key", pa.string(), nullable=False),
    pa.field("family", pa.string(), nullable=False),
    pa.field("base_value", pa.float64()),
    pa.field("scen_value", pa.float64()),
    pa.field("pnl_full", pa.float64()),
    pa.field("pnl_greeks", pa.float64()),
    pa.field("source_session_id", pa.string()),
])

_SCENARIO_SUMMARY = _with_common([
    pa.field("snapshot_ts", _TS, nullable=False),
    pa.field("version", pa.string(), nullable=False),
    pa.field("scenario_id", pa.string(), nullable=False),
    pa.field("underlying", pa.string(), nullable=False),
    pa.field("family", pa.string(), nullable=False),
    pa.field("group_key", pa.string(), nullable=False),    # portfolio|underlying
    pa.field("group_value", pa.string(), nullable=False),
    pa.field("n_lines", pa.int32()),
    pa.field("pnl_full", pa.float64()),
    pa.field("pnl_greeks", pa.float64()),
    pa.field("approx_error", pa.float64()),
    pa.field("is_worst_case", pa.bool_()),
    pa.field("source_session_id", pa.string()),
])

DATASETS: dict[str, Dataset] = {
    "raw_events": Dataset("raw_events", DataLayer.RAW, _RAW_EVENTS),
    "market_state": Dataset("market_state", DataLayer.NORMALIZED, _MARKET_STATE),
    "forwards": Dataset("forwards", DataLayer.DERIVED, _FORWARDS),
    "forward_diagnostics": Dataset("forward_diagnostics", DataLayer.DERIVED, _FORWARD_DIAGNOSTICS),
    "iv_points": Dataset("iv_points", DataLayer.DERIVED, _IV_POINTS),
    "surface_params": Dataset("surface_params", DataLayer.DERIVED, _SURFACE_PARAMS),
    "surface_grid": Dataset("surface_grid", DataLayer.DERIVED, _SURFACE_GRID),
    "model_prices": Dataset("model_prices", DataLayer.DERIVED, _MODEL_PRICES),
    "greeks": Dataset("greeks", DataLayer.DERIVED, _GREEKS),
    "risk_aggregates": Dataset("risk_aggregates", DataLayer.DERIVED, _RISK_AGGREGATES),
    "risk_recon": Dataset("risk_recon", DataLayer.DERIVED, _RISK_RECON),
    "scenarios": Dataset("scenarios", DataLayer.DERIVED, _SCENARIOS),
    "scenario_defs": Dataset("scenario_defs", DataLayer.DERIVED, _SCENARIO_DEFS),
    "scenario_results": Dataset("scenario_results", DataLayer.DERIVED, _SCENARIO_RESULTS),
    "scenario_summary": Dataset("scenario_summary", DataLayer.DERIVED, _SCENARIO_SUMMARY),
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
