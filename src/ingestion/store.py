"""Append-only, loss-aware raw event store (Step 3d).

Events are written as JSON Lines, one file per (trade_date, session_id). Appends
are atomic at line granularity and flushed immediately, so a kill-and-restart
cannot corrupt previously written events; at worst the final line is truncated,
which replay tolerates and counts rather than crashing on. A full day can be
replayed from disk without contacting the broker (cours.txt:548-550).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from ingestion.events import MarketEvent, OpsEvent


class RawEventStore:
    def __init__(self, data_dir: Path) -> None:
        self.root = Path(data_dir) / "raw_events"

    def _path(self, layer: str, trade_date: str, session_id: str) -> Path:
        return self.root / layer / trade_date / f"{session_id}.jsonl"

    def _append(self, path: Path, records: Iterable[dict]) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        n = 0
        # Append mode + flush keeps the store append-only and crash-resilient.
        with path.open("a", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")
                n += 1
            fh.flush()
        return n

    def append_market(
        self, trade_date: str, session_id: str, events: Iterable[MarketEvent]
    ) -> int:
        return self._append(
            self._path("market", trade_date, session_id),
            (e.to_json() for e in events),
        )

    def append_ops(self, trade_date: str, session_id: str, event: OpsEvent) -> int:
        return self._append(
            self._path("ops", trade_date, session_id), [event.to_json()]
        )

    @staticmethod
    def _iter_lines(path: Path) -> tuple[list[dict], int]:
        """Parse a JSONL file, tolerating a truncated trailing line."""
        if not path.exists():
            return [], 0
        records: list[dict] = []
        corrupt = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                corrupt += 1  # truncated/partial line from a crash
        return records, corrupt

    def replay_market(
        self, trade_date: str, session_id: str
    ) -> tuple[list[MarketEvent], int]:
        recs, corrupt = self._iter_lines(self._path("market", trade_date, session_id))
        return [MarketEvent.from_json(r) for r in recs], corrupt

    def replay_ops(
        self, trade_date: str, session_id: str
    ) -> tuple[list[OpsEvent], int]:
        recs, corrupt = self._iter_lines(self._path("ops", trade_date, session_id))
        return [OpsEvent.from_json(r) for r in recs], corrupt
