"""Structured logging with correlation IDs (Step 15 task b).

A correlation_id links a collector session to every downstream analytics job that
consumes its data, so an operator can trace one trading session end to end across
ingestion -> forwards -> surface -> risk -> QC. Logs are emitted as single-line
JSON for machine parsing; the same fields feed the job ledger and metrics.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import uuid


def new_correlation_id(prefix: str = "corr") -> str:
    """Fresh correlation id. Use the collector session id as `prefix` to bind a
    full lineage to one live session."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def correlation_id_from_session(session_id: str) -> str:
    """Deterministic correlation id derived from a collector session id, so live
    collection and the analytics jobs it triggers share one trace key."""
    return f"sess-{session_id}"


class StructuredLogger:
    """Thin wrapper emitting JSON records with a bound context (correlation_id,
    job_name, run_date...). Never raises on serialization."""

    def __init__(self, name: str, **context):
        self._log = logging.getLogger(name)
        self._ctx = {k: v for k, v in context.items() if v is not None}

    def bind(self, **extra) -> "StructuredLogger":
        child = StructuredLogger(self._log.name)
        child._log = self._log
        child._ctx = {**self._ctx, **{k: v for k, v in extra.items() if v is not None}}
        return child

    def _emit(self, level: int, event: str, **fields) -> dict:
        record = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                  "level": logging.getLevelName(level), "event": event,
                  **self._ctx, **fields}
        try:
            self._log.log(level, json.dumps(record, default=str))
        except (TypeError, ValueError):       # pragma: no cover - defensive
            self._log.log(level, str(record))
        return record

    def info(self, event: str, **f) -> dict:
        return self._emit(logging.INFO, event, **f)

    def warning(self, event: str, **f) -> dict:
        return self._emit(logging.WARNING, event, **f)

    def error(self, event: str, **f) -> dict:
        return self._emit(logging.ERROR, event, **f)
