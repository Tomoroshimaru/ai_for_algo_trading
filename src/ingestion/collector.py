"""Tick normalization, failure classification, and session summaries (Step 3c/f/g).

The per-tick callback only normalizes, stamps, and persists - never computes
analytics - which is the documented way to avoid dropped events and
undebuggable behavior (cours.txt:553-555). The live streaming/reconnect loop is
built on top of this in the next step.
"""
from __future__ import annotations

import datetime as dt
import math
from typing import Any, Callable

from ingestion.events import MarketEvent, OpsEvent, OpsEventKind
from ingestion.store import RawEventStore

# ib_insync Ticker attribute -> canonical field name.
_TICK_FIELDS = {
    "bid": "bid",
    "ask": "ask",
    "last": "last",
    "close": "close",
    "bidSize": "bid_size",
    "askSize": "ask_size",
    "lastSize": "last_size",
    "volume": "volume",
}

# IBKR error codes -> structured ops classification.
_PACING_CODES = {100, 162, 420}        # pacing violations / historical pacing
_ENTITLEMENT_CODES = {354, 10089, 10167, 10168, 10197}  # market-data not subscribed


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def normalize_ticker(
    ticker: Any,
    instrument_key: str,
    session_id: str,
    *,
    now: dt.datetime | None = None,
) -> list[MarketEvent]:
    """Turn one broker ticker into normalized, stamped events (no analytics)."""
    ts = now or _utcnow()
    source_ts = getattr(ticker, "time", None)
    if source_ts is not None and source_ts.tzinfo is None:
        source_ts = source_ts.replace(tzinfo=dt.timezone.utc)

    events: list[MarketEvent] = []
    for attr, field in _TICK_FIELDS.items():
        raw = getattr(ticker, attr, None)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isnan(value):  # IBKR uses NaN for "no value yet" - drop it
            continue
        events.append(
            MarketEvent(
                session_id=session_id,
                instrument_key=instrument_key,
                field=field,
                value=value,
                source_ts=source_ts,
                receipt_ts=ts,
                collector_ts=ts,
            )
        )
    return events


def classify_error(code: int, message: str, session_id: str,
                   *, now: dt.datetime | None = None) -> OpsEvent:
    """Map an IBKR error code into a structured ops event."""
    if code in _PACING_CODES:
        kind = OpsEventKind.PACING
    elif code in _ENTITLEMENT_CODES:
        kind = OpsEventKind.ENTITLEMENT
    else:
        kind = OpsEventKind.ERROR
    return OpsEvent(session_id, kind, f"[{code}] {message}", now or _utcnow())


class Collector:
    """Thin wiring: normalize + stamp + persist. No analytics in the callback."""

    def __init__(self, store: RawEventStore, session_id: str, trade_date: str,
                 clock: Callable[[], dt.datetime] = _utcnow) -> None:
        self._store = store
        self._session_id = session_id
        self._trade_date = trade_date
        self._clock = clock

    def on_ticker(self, ticker: Any, instrument_key: str) -> int:
        events = normalize_ticker(
            ticker, instrument_key, self._session_id, now=self._clock()
        )
        return self._store.append_market(self._trade_date, self._session_id, events)

    def on_error(self, code: int, message: str) -> OpsEvent:
        ev = classify_error(code, message, self._session_id, now=self._clock())
        self._store.append_ops(self._trade_date, self._session_id, ev)
        return ev


def build_session_summary(
    market_events: list[MarketEvent],
    ops_events: list[OpsEvent],
    *,
    expected_instruments: int | None = None,
) -> dict[str, Any]:
    """Aggregate counts, reconnects, failures, and coverage (Step 3g)."""
    by_field: dict[str, int] = {}
    by_instrument: dict[str, int] = {}
    for e in market_events:
        by_field[e.field] = by_field.get(e.field, 0) + 1
        by_instrument[e.instrument_key] = by_instrument.get(e.instrument_key, 0) + 1

    ts = [e.collector_ts for e in market_events]
    distinct = len(by_instrument)
    summary: dict[str, Any] = {
        "total_market_events": len(market_events),
        "distinct_instruments": distinct,
        "by_field": by_field,
        "first_ts": min(ts).isoformat() if ts else None,
        "last_ts": max(ts).isoformat() if ts else None,
        "reconnect_count": sum(o.kind == OpsEventKind.RECONNECT for o in ops_events),
        "pacing_count": sum(o.kind == OpsEventKind.PACING for o in ops_events),
        "entitlement_count": sum(o.kind == OpsEventKind.ENTITLEMENT for o in ops_events),
        "error_count": sum(o.kind == OpsEventKind.ERROR for o in ops_events),
    }
    if expected_instruments:
        summary["coverage_ratio"] = round(distinct / expected_instruments, 4)
    return summary
