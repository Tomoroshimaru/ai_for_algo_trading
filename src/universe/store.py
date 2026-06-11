"""Versioned persistence of the instrument universe (Step 2e/g).

Writes both the raw broker payloads (evidence) and the normalized canonical
representation, partitioned by session date and stamped with a configuration
fingerprint so any trading day can be reconstructed deterministically.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from universe.schema import UnderlyingInstrument


def config_fingerprint(payload: dict[str, Any]) -> str:
    """Deterministic short hash of the config that produced a universe."""
    blob = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:12]


class UniverseStore:
    """Filesystem store, one directory per session date."""

    def __init__(self, data_dir: Path) -> None:
        self.root = Path(data_dir) / "universe"

    def session_dir(self, session_date: str) -> Path:
        return self.root / session_date

    def save_underlyings(
        self,
        session_date: str,
        instruments: list[UnderlyingInstrument],
        raw_payloads: list[dict[str, Any]],
        config_fp: str,
    ) -> Path:
        """Persist canonical + raw + manifest. Returns the session directory."""
        out = self.session_dir(session_date)
        out.mkdir(parents=True, exist_ok=True)

        # Deterministic ordering by canonical key -> reproducible files.
        ordered = sorted(instruments, key=lambda i: i.canonical_key)
        (out / "underlyings_canonical.json").write_text(
            json.dumps([asdict(i) for i in ordered], indent=2, default=str)
        )
        (out / "underlyings_raw.json").write_text(
            json.dumps(raw_payloads, indent=2, default=str)
        )
        manifest = {
            "session_date": session_date,
            "config_fingerprint": config_fp,
            "underlying_count": len(ordered),
            "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
        return out

    def load_underlyings(self, session_date: str) -> list[UnderlyingInstrument]:
        path = self.session_dir(session_date) / "underlyings_canonical.json"
        if not path.exists():
            raise FileNotFoundError(f"No universe persisted for {session_date}: {path}")
        rows = json.loads(path.read_text())
        return [UnderlyingInstrument(**row) for row in rows]

    def load_manifest(self, session_date: str) -> dict[str, Any]:
        path = self.session_dir(session_date) / "manifest.json"
        if not path.exists():
            raise FileNotFoundError(f"No manifest for {session_date}: {path}")
        return json.loads(path.read_text())
