#!/usr/bin/env python
"""Step 1 bootstrap smoke test (entrypoint).

Proves the environment can reach IBKR end-to-end WITHOUT placing any order:
connect -> health/clock -> resolve one contract -> one market-data snapshot
-> persist a JSON evidence artifact.

Run (IB Gateway/TWS must be running with the API enabled):
    uv run python scripts/bootstrap_smoke_test.py
"""
from __future__ import annotations

import json
import logging
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from utils.config import load_config  # noqa: E402
from connectivity.session import IBSession  # noqa: E402
from connectivity.diagnostics import gather_evidence, write_evidence  # noqa: E402


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_config()
    try:
        with IBSession(cfg.ibkr) as session:
            report = gather_evidence(session, cfg)
            artifact = write_evidence(report, cfg.environment.log_dir)
    except ConnectionError as exc:
        print(
            f"[FAIL] {exc}\n"
            f"       Is IB Gateway/TWS running with the API enabled on "
            f"{cfg.ibkr.host}:{cfg.ibkr.port}?",
            file=sys.stderr,
        )
        return 1

    print(json.dumps(report, indent=2, default=str))
    print(f"[OK] connectivity proven; evidence written to {artifact}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
