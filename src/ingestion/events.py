"""Common event model for the raw ingestion layer (Step 3c).

Every observation becomes an append-only, self-describing record carrying the
three timestamps needed to reason about staleness downstream: source (broker,
if available), receipt (when the callback saw it), and collector (process clock).
Operational anomalies (pacing, entitlement, reconnects) are first-class events
so the raw layer records *what was seen*, including failures, never papering over
gaps (cours.txt:533-545).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import Enum
from typing import Any


def _iso(ts: dt.datetime | None) -> str | None:
    return ts.isoformat() if ts is not None else None


def _parse(ts: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(ts) if ts else None


@dataclass(frozen=True)
class MarketEvent:
    session_id: str
    instrument_key: str       # canonical key from the universe master
    field: str                # bid | ask | last | close | bid_size | ...
    value: float
    source_ts: dt.datetime | None
    receipt_ts: dt.datetime
    collector_ts: dt.datetime

    def to_json(self) -> dict[str, Any]:
        return {
            "type": "market",
            "session_id": self.session_id,
            "instrument_key": self.instrument_key,
            "field": self.field,
            "value": self.value,
            "source_ts": _iso(self.source_ts),
            "receipt_ts": _iso(self.receipt_ts),
            "collector_ts": _iso(self.collector_ts),
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "MarketEvent":
        return cls(
            session_id=d["session_id"],
            instrument_key=d["instrument_key"],
            field=d["field"],
            value=d["value"],
            source_ts=_parse(d.get("source_ts")),
            receipt_ts=_parse(d["receipt_ts"]),
            collector_ts=_parse(d["collector_ts"]),
        )


class OpsEventKind(str, Enum):
    CONNECT = "CONNECT"
    DISCONNECT = "DISCONNECT"
    RECONNECT = "RECONNECT"
    PACING = "PACING"
    ENTITLEMENT = "ENTITLEMENT"
    ERROR = "ERROR"


@dataclass(frozen=True)
class OpsEvent:
    session_id: str
    kind: OpsEventKind
    detail: str
    ts: dt.datetime

    def to_json(self) -> dict[str, Any]:
        return {
            "type": "ops",
            "session_id": self.session_id,
            "kind": self.kind.value,
            "detail": self.detail,
            "ts": _iso(self.ts),
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "OpsEvent":
        return cls(
            session_id=d["session_id"],
            kind=OpsEventKind(d["kind"]),
            detail=d["detail"],
            ts=_parse(d["ts"]),
        )
