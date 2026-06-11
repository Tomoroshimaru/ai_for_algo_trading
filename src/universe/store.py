"""Versioned persistence of the instrument universe (Step 2e/g).

Writes both the raw broker payloads (evidence) and the normalized canonical
representation, partitioned by session date and stamped with a configuration
fingerprint so any trading day can be reconstructed deterministically.
Underlyings are small -> JSON. Options can reach 10^4-10^6 rows -> Parquet.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from universe.schema import OptionInstrument, UnderlyingInstrument


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

    def _update_manifest(self, session_date: str, **fields: Any) -> None:
        out = self.session_dir(session_date)
        out.mkdir(parents=True, exist_ok=True)
        path = out / "manifest.json"
        data = json.loads(path.read_text()) if path.exists() else {"session_date": session_date}
        data.update(fields)
        data["generated_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
        path.write_text(json.dumps(data, indent=2))

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
        ordered = sorted(instruments, key=lambda i: i.canonical_key)
        (out / "underlyings_canonical.json").write_text(
            json.dumps([asdict(i) for i in ordered], indent=2, default=str)
        )
        (out / "underlyings_raw.json").write_text(
            json.dumps(raw_payloads, indent=2, default=str)
        )
        self._update_manifest(
            session_date, config_fingerprint=config_fp, underlying_count=len(ordered)
        )
        return out

    def load_underlyings(self, session_date: str) -> list[UnderlyingInstrument]:
        path = self.session_dir(session_date) / "underlyings_canonical.json"
        if not path.exists():
            raise FileNotFoundError(f"No universe persisted for {session_date}: {path}")
        rows = json.loads(path.read_text())
        return [UnderlyingInstrument(**row) for row in rows]

    def save_options(
        self, session_date: str, options: list[OptionInstrument]
    ) -> Path:
        """Persist canonical option instruments as Parquet (columnar, scalable)."""
        import pandas as pd

        out = self.session_dir(session_date)
        out.mkdir(parents=True, exist_ok=True)
        rows = sorted(options, key=lambda o: o.canonical_key)
        df = pd.DataFrame([asdict(o) for o in rows])
        path = out / "options_canonical.parquet"
        df.to_parquet(path, index=False)
        self._update_manifest(session_date, option_count=len(rows))
        return path

    def load_options(self, session_date: str):
        import pandas as pd

        path = self.session_dir(session_date) / "options_canonical.parquet"
        if not path.exists():
            raise FileNotFoundError(f"No options persisted for {session_date}: {path}")
        return pd.read_parquet(path)

    def load_manifest(self, session_date: str) -> dict[str, Any]:
        path = self.session_dir(session_date) / "manifest.json"
        if not path.exists():
            raise FileNotFoundError(f"No manifest for {session_date}: {path}")
        return json.loads(path.read_text())
