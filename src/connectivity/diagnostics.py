"""Step 1 bootstrap diagnostics: prove end-to-end connectivity, no orders.

Gathers a minimal evidence bundle (session health, one resolved contract, one
market-data snapshot) and persists it as a JSON artifact. Pure of any trading
action, per Step 1 acceptance criteria (cours.txt:476-497).

The evidence gathering takes an already-connected ``IBSession`` so it can be
unit-tested offline with a fake IB client.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from utils.config import AppConfig
from connectivity.session import IBSession


def gather_evidence(session: IBSession, cfg: AppConfig, symbol: str = "SPY") -> dict[str, Any]:
    """Collect a read-only connectivity evidence bundle."""
    ib = session.ib
    ib.reqMarketDataType(cfg.ibkr.market_data_type)
    health = session.health_check(cfg.qc.clock_skew_tolerance_sec)

    from ib_insync import Stock

    qualified = ib.qualifyContracts(Stock(symbol, "SMART", "USD"))
    if not qualified:
        raise RuntimeError(f"Could not resolve a contract for symbol {symbol!r}")
    contract = qualified[0]

    tickers = ib.reqTickers(contract)
    sample_price = tickers[0].marketPrice() if tickers else None

    return {
        "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "environment": cfg.environment.name,
        "connected": health.connected,
        "connection_state": health.state.value,
        "server_time_utc": (
            health.server_time.isoformat() if health.server_time else None
        ),
        "clock_skew_sec": health.clock_skew_sec,
        "market_data_type": cfg.ibkr.market_data_type,
        "resolved_contract": {
            "symbol": getattr(contract, "symbol", symbol),
            "conId": getattr(contract, "conId", None),
            "exchange": getattr(contract, "exchange", None),
            "currency": getattr(contract, "currency", None),
        },
        "sample_market_price": sample_price,
    }


def write_evidence(report: dict[str, Any], log_dir: Path) -> Path:
    """Persist the evidence bundle as a timestamped JSON artifact."""
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = report["timestamp_utc"].replace(":", "").replace("-", "").replace(".", "_")
    path = log_dir / f"bootstrap_{stamp}.json"
    path.write_text(json.dumps(report, indent=2, default=str))
    return path
