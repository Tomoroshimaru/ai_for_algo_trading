#!/usr/bin/env python
"""Step 3 market-data collector service (entrypoint).

Subscribes to underlying market data and streams every tick into the
append-only raw event store, with heartbeat-driven reconnect. Runs unattended
for a bounded duration and prints a session summary on exit.

Run (IB Gateway/TWS must be running with the API enabled):
    uv run python scripts/run_collector.py --symbols SPY,AAPL --duration 60
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from ib_insync import Stock  # noqa: E402

from utils.config import load_config  # noqa: E402
from connectivity.session import IBSession  # noqa: E402
from ingestion.store import RawEventStore  # noqa: E402
from ingestion.service import StreamingCollector, Subscription  # noqa: E402
from ingestion.collector import build_session_summary  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="IBKR market-data collector")
    ap.add_argument("--symbols", default="SPY", help="comma-separated underlyings")
    ap.add_argument("--duration", type=float, default=60.0, help="run seconds")
    ap.add_argument("--heartbeat", type=float, default=10.0)
    ap.add_argument("--poll", type=float, default=1.0)
    ap.add_argument("--delayed", action="store_true", help="use delayed market data")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_config()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    sid = "collector-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    trade_date = dt.date.today().isoformat()
    store = RawEventStore(cfg.environment.data_dir)

    try:
        with IBSession(cfg.ibkr) as session:
            if args.delayed:
                session.ib.reqMarketDataType(3)
            subs: list[Subscription] = []
            for sym in symbols:
                c = Stock(sym, "SMART", "USD")
                session.ib.qualifyContracts(c)
                subs.append(Subscription(c, f"STK:{sym}:USD", streaming=True))
            collector = StreamingCollector(
                session, store, subs, sid, trade_date,
                heartbeat_interval=args.heartbeat, poll_interval=args.poll,
            )
            collector.run(duration_sec=args.duration)
    except ConnectionError as exc:
        print(
            f"[FAIL] {exc}\n       Is IB Gateway/TWS running on "
            f"{cfg.ibkr.host}:{cfg.ibkr.port}?",
            file=sys.stderr,
        )
        return 1

    mkt, corrupt = store.replay_market(trade_date, sid)
    ops, _ = store.replay_ops(trade_date, sid)
    summary = build_session_summary(mkt, ops, expected_instruments=len(symbols))
    summary["corrupt_lines"] = corrupt
    summary["session_id"] = sid
    print(json.dumps(summary, indent=2, default=str))
    print(f"[OK] session {sid}: {len(mkt)} events replayable from disk")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
