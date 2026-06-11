"""Live streaming collector service with reconnect + heartbeat (Step 3a/b/e).

A long-running process that subscribes to underlying/option market data and
persists every tick through the append-only store. The per-tick callback only
normalizes+stamps+persists; the run loop owns heartbeat monitoring and
controlled reconnect-with-backoff (delegated to IBSession), emitting structured
DISCONNECT/RECONNECT ops events. A restart starts a new session file, so a
kill-and-restart never corrupts previously written sessions (cours.txt:546-550).
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any, Callable

from connectivity.session import IBSession
from ingestion.collector import normalize_ticker, classify_error
from ingestion.events import MarketEvent, OpsEvent, OpsEventKind
from ingestion.store import RawEventStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Subscription:
    contract: Any              # qualified ib_insync Contract (has conId)
    instrument_key: str        # canonical key from the universe master
    streaming: bool = True     # True=persistent stream, False=one-shot snapshot (task a)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class StreamingCollector:
    def __init__(
        self,
        session: IBSession,
        store: RawEventStore,
        subscriptions: list[Subscription],
        session_id: str,
        trade_date: str,
        *,
        heartbeat_interval: float = 10.0,
        poll_interval: float = 1.0,
        clock: Callable[[], dt.datetime] = _utcnow,
    ) -> None:
        self._session = session
        self._store = store
        self._subs = subscriptions
        self._session_id = session_id
        self._trade_date = trade_date
        self._heartbeat_interval = heartbeat_interval
        self._poll_interval = poll_interval
        self._clock = clock
        self._key_by_conid = {s.contract.conId: s.instrument_key for s in subscriptions}
        self._reconnects = 0
        self._stop = False

    # -- ops bookkeeping ---------------------------------------------------- #
    def _emit(self, kind: OpsEventKind, detail: str) -> None:
        self._store.append_ops(
            self._trade_date, self._session_id,
            OpsEvent(self._session_id, kind, detail, self._clock()),
        )

    # -- callback: normalize + stamp + persist ONLY ------------------------- #
    def _on_pending(self, tickers: Any) -> int:
        written = 0
        for tk in tickers:
            con_id = getattr(getattr(tk, "contract", None), "conId", None)
            key = self._key_by_conid.get(con_id)
            if key is None:
                continue
            events = normalize_ticker(tk, key, self._session_id, now=self._clock())
            written += self._store.append_market(self._trade_date, self._session_id, events)
        return written

    def _on_error(self, reqId: Any, code: int, message: str, *_: Any) -> None:
        ev = classify_error(code, message, self._session_id, now=self._clock())
        self._store.append_ops(self._trade_date, self._session_id, ev)
        logger.warning("collector ops event %s: %s", ev.kind.value, ev.detail)

    # -- (re)wiring --------------------------------------------------------- #
    def _wire_and_subscribe(self) -> None:
        ib = self._session.ib
        ib.pendingTickersEvent += self._on_pending
        if hasattr(ib, "errorEvent"):
            ib.errorEvent += self._on_error
        for s in self._subs:
            ib.reqMktData(s.contract, "", not s.streaming, False)

    def _heartbeat(self) -> None:
        """Detect a dropped connection and recover with backoff."""
        if self._session.ib.isConnected():
            return
        self._emit(OpsEventKind.DISCONNECT, "heartbeat detected lost connection")
        logger.warning("connection lost - attempting controlled reconnect")
        self._session.connect()           # exponential backoff inside IBSession
        self._reconnects += 1
        self._wire_and_subscribe()         # new ib client -> re-subscribe
        self._emit(OpsEventKind.RECONNECT, f"reconnect #{self._reconnects} ok")

    def stop(self) -> None:
        self._stop = True

    @property
    def reconnect_count(self) -> int:
        return self._reconnects

    def run(self, duration_sec: float) -> None:
        """Run the collector for ``duration_sec`` seconds, unattended."""
        ib = self._session.ib
        self._emit(OpsEventKind.CONNECT, f"collector start ({len(self._subs)} subs)")
        self._wire_and_subscribe()
        start = self._clock()
        next_hb = start + dt.timedelta(seconds=self._heartbeat_interval)
        deadline = start + dt.timedelta(seconds=duration_sec)
        try:
            while not self._stop and self._clock() < deadline:
                ib.sleep(self._poll_interval)   # pumps loop -> fires _on_pending
                if self._clock() >= next_hb:
                    self._heartbeat()
                    next_hb = self._clock() + dt.timedelta(seconds=self._heartbeat_interval)
        finally:
            self._emit(OpsEventKind.DISCONNECT, "collector stop")


def detect_missing_intervals(
    events: list[MarketEvent],
    instrument_key: str,
    interval_sec: float,
    *,
    gap_factor: float = 2.0,
) -> list[tuple[str, str]]:
    """Find gaps between consecutive observations exceeding gap_factor*interval.

    Loss-aware reporting: missing data is surfaced as explicit intervals rather
    than silently skipped (cours.txt:535-537).
    """
    ts = sorted(e.collector_ts for e in events if e.instrument_key == instrument_key)
    threshold = interval_sec * gap_factor
    gaps: list[tuple[str, str]] = []
    for a, b in zip(ts, ts[1:]):
        if (b - a).total_seconds() > threshold:
            gaps.append((a.isoformat(), b.isoformat()))
    return gaps
