"""Metadata store: configuration, jobs, reference entities, retention (Step 4a/f).

A small SQLite database holds everything that is *about* the data rather than
the bulk data itself: schema versions, collector/analytics job runs, reference
instruments, and retention policy. Migrations are explicit and idempotent,
tracked via SQLite's ``PRAGMA user_version`` so replay and live share one path.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

from storage.schemas import DATASETS, SCHEMA_VERSION

# Ordered migrations. Each is applied once; user_version is the high-water mark.
_MIGRATIONS: list[str] = [
    # v1 - initial schema
    """
    CREATE TABLE schema_registry (
        dataset TEXT NOT NULL,
        layer TEXT NOT NULL,
        version INTEGER NOT NULL,
        registered_at TEXT NOT NULL,
        PRIMARY KEY (dataset, version)
    );
    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        status TEXT NOT NULL,
        detail TEXT,
        trade_date TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE instruments (
        instrument_key TEXT PRIMARY KEY,
        underlying TEXT NOT NULL,
        sec_type TEXT NOT NULL,
        con_id INTEGER,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE retention_policy (
        layer TEXT PRIMARY KEY,
        retention_days INTEGER NOT NULL
    );
    INSERT INTO retention_policy (layer, retention_days) VALUES
        ('raw', 30), ('normalized', 365), ('derived', 730);
    """,
]


class MetadataStore:
    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / "metadata.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def migrate(self) -> int:
        """Apply pending migrations idempotently. Returns the resulting version."""
        with self._connect() as conn:
            current = conn.execute("PRAGMA user_version;").fetchone()[0]
            for version in range(current, len(_MIGRATIONS)):
                conn.executescript(_MIGRATIONS[version])
                conn.execute(f"PRAGMA user_version = {version + 1};")
            final = conn.execute("PRAGMA user_version;").fetchone()[0]
        return final

    def register_all_schemas(self) -> None:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        with self._connect() as conn:
            for ds in DATASETS.values():
                conn.execute(
                    "INSERT OR REPLACE INTO schema_registry "
                    "(dataset, layer, version, registered_at) VALUES (?, ?, ?, ?)",
                    (ds.name, ds.layer.value, SCHEMA_VERSION, now),
                )

    def record_job(self, name: str, status: str, detail: str = "",
                   trade_date: str | None = None) -> int:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO jobs (name, status, detail, trade_date, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, status, detail, trade_date, now),
            )
            return cur.lastrowid

    def upsert_instrument(self, instrument_key: str, underlying: str,
                          sec_type: str, con_id: int | None = None) -> None:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO instruments "
                "(instrument_key, underlying, sec_type, con_id, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (instrument_key, underlying, sec_type, con_id, now),
            )

    def retention_days(self, layer: str) -> int | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT retention_days FROM retention_policy WHERE layer = ?", (layer,)
            ).fetchone()
            return row[0] if row else None

    def list_schemas(self) -> list[dict]:
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT dataset, layer, version FROM schema_registry ORDER BY dataset"
            )]
