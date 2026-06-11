"""Observability connectique (roadmap Step 15) — REAL data.

Replays the append-only raw-event store (Step 3) to compute per-session and
aggregate ingestion metrics: event rates, reconnects, errors, corrupt lines and
quote staleness. Nothing here is synthetic; if the store is empty the snapshot
is simply empty and labeled as such.
"""
from __future__ import annotations

import datetime as dt

import app._paths  # noqa: F401  side-effect: src/ on sys.path

from utils.config import load_config
from ingestion.events import OpsEventKind
from ingestion.store import RawEventStore

from app.data.contracts import ObservabilitySnapshot, SessionMetrics


class ObservabilityProvider:
    def __init__(self) -> None:
        self._cfg = load_config()
        self._store = RawEventStore(self._cfg.environment.data_dir)
        self._tol = float(self._cfg.qc.clock_skew_tolerance_sec)

    def _layer_dir(self, layer: str):
        return self._store.root / layer

    def latest_trade_date(self) -> str | None:
        dates: set[str] = set()
        for layer in ("market", "ops"):
            d = self._layer_dir(layer)
            if d.exists():
                dates.update(p.name for p in d.iterdir() if p.is_dir())
        return sorted(dates)[-1] if dates else None

    def _session_ids(self, trade_date: str) -> list[str]:
        ids: set[str] = set()
        for layer in ("market", "ops"):
            d = self._layer_dir(layer) / trade_date
            if d.exists():
                ids.update(p.stem for p in d.glob("*.jsonl"))
        return sorted(ids)

    def _session_metrics(self, trade_date: str, sid: str) -> tuple[SessionMetrics, set[str]]:
        market, corrupt_m = self._store.replay_market(trade_date, sid)
        ops, corrupt_o = self._store.replay_ops(trade_date, sid)

        receipts = [e.receipt_ts for e in market if e.receipt_ts]
        duration = 0.0
        if len(receipts) >= 2:
            duration = (max(receipts) - min(receipts)).total_seconds()
        rate = (len(market) / duration) if duration > 0 else 0.0

        stale = 0
        max_stale = 0.0
        for e in market:
            if e.source_ts and e.receipt_ts:
                lag = (e.receipt_ts - e.source_ts).total_seconds()
                max_stale = max(max_stale, lag)
                if lag > self._tol:
                    stale += 1
        stale_ratio = (stale / len(market)) if market else 0.0

        kinds = [e.kind for e in ops]
        last_ts = None
        all_ts = receipts + [e.ts for e in ops if e.ts]
        if all_ts:
            last_ts = max(all_ts).isoformat()

        instruments = {e.instrument_key for e in market}
        metrics = SessionMetrics(
            session_id=sid,
            market_events=len(market),
            ops_events=len(ops),
            corrupt_lines=corrupt_m + corrupt_o,
            duration_sec=round(duration, 3),
            event_rate_hz=round(rate, 3),
            reconnects=kinds.count(OpsEventKind.RECONNECT),
            disconnects=kinds.count(OpsEventKind.DISCONNECT),
            errors=kinds.count(OpsEventKind.ERROR),
            stale_ratio=round(stale_ratio, 4),
            max_staleness_sec=round(max_stale, 4),
            last_event_utc=last_ts,
        )
        return metrics, instruments

    def get_snapshot(self, trade_date: str | None = None) -> ObservabilitySnapshot:
        td = trade_date or self.latest_trade_date()
        if td is None:
            return ObservabilitySnapshot(source="none", trade_date=None,
                                         stale_quote_tolerance_sec=self._tol)
        sessions: list[SessionMetrics] = []
        instruments: set[str] = set()
        for sid in self._session_ids(td):
            m, instr = self._session_metrics(td, sid)
            sessions.append(m)
            instruments |= instr
        return ObservabilitySnapshot(
            source=f"raw_events store @ {td}",
            trade_date=td,
            sessions=sessions,
            total_market_events=sum(s.market_events for s in sessions),
            total_ops_events=sum(s.ops_events for s in sessions),
            total_corrupt=sum(s.corrupt_lines for s in sessions),
            total_reconnects=sum(s.reconnects for s in sessions),
            total_errors=sum(s.errors for s in sessions),
            distinct_instruments=len(instruments),
            stale_quote_tolerance_sec=self._tol,
        )
