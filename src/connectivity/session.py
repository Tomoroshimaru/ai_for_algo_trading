"""IBKR session management (Roadmap Step 1, connectivity primitive).

Responsibilities (and only these): own the IBKR connection lifecycle, expose a
clear connection state, reconnect with backoff, and answer a health probe. It
deliberately knows nothing about instruments, market data, or analytics
(cours.txt:1888 "The module should not know anything" about higher layers).

The concrete ``ib_insync.IB`` client is injected via ``ib_factory`` so the
session can be unit-tested offline with a fake client.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from utils.config import IBKRConfig

logger = logging.getLogger(__name__)


class ConnectionState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    DEGRADED = "DEGRADED"
    RECONNECTING = "RECONNECTING"


@dataclass(frozen=True)
class HealthReport:
    connected: bool
    state: ConnectionState
    server_time: Optional[dt.datetime]
    clock_skew_sec: Optional[float]


def _default_ib_factory():  # pragma: no cover - thin wrapper over ib_insync
    from ib_insync import IB

    return IB()


class IBSession:
    """Manage a single IBKR connection with retries and health checks.

    Example
    -------
    >>> from utils.config import load_config
    >>> cfg = load_config()
    >>> with IBSession(cfg.ibkr) as s:      # doctest: +SKIP
    ...     print(s.health_check(cfg.qc.clock_skew_tolerance_sec))
    """

    def __init__(
        self,
        config: IBKRConfig,
        *,
        ib_factory: Callable[[], object] | None = None,
        connect_timeout: float = 8.0,
        max_retries: int = 5,
        base_backoff: float = 1.0,
        max_backoff: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_retries < 1:
            raise ValueError("max_retries must be >= 1")
        self._cfg = config
        self._ib_factory = ib_factory or _default_ib_factory
        self._connect_timeout = connect_timeout
        self._max_retries = max_retries
        self._base_backoff = base_backoff
        self._max_backoff = max_backoff
        self._sleep = sleep
        self._ib: object | None = None
        self._state = ConnectionState.DISCONNECTED

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def ib(self) -> object:
        if self._ib is None:
            raise RuntimeError("Session not connected; call connect() first.")
        return self._ib

    def connect(self) -> "IBSession":
        """Connect, retrying with exponential backoff. Fail-loud if exhausted."""
        self._ib = self._ib_factory()
        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            self._state = (
                ConnectionState.CONNECTING if attempt == 1 else ConnectionState.RECONNECTING
            )
            try:
                self._ib.connect(
                    self._cfg.host,
                    self._cfg.port,
                    clientId=self._cfg.client_id,
                    timeout=self._connect_timeout,
                    account=self._cfg.account or "",
                )
                self._state = ConnectionState.CONNECTED
                logger.info(
                    "IBKR connected host=%s port=%s clientId=%s (attempt %d)",
                    self._cfg.host, self._cfg.port, self._cfg.client_id, attempt,
                )
                return self
            except Exception as exc:  # noqa: BLE001 - retry on any connect failure
                last_exc = exc
                if attempt >= self._max_retries:
                    break
                backoff = min(self._base_backoff * 2 ** (attempt - 1), self._max_backoff)
                logger.warning(
                    "IBKR connect attempt %d/%d failed: %s -> retry in %.1fs",
                    attempt, self._max_retries, exc, backoff,
                )
                self._sleep(backoff)
        self._state = ConnectionState.DISCONNECTED
        raise ConnectionError(
            f"Could not connect to IBKR at {self._cfg.host}:{self._cfg.port} "
            f"after {self._max_retries} attempts"
        ) from last_exc

    def disconnect(self) -> None:
        if self._ib is not None and self._ib.isConnected():
            self._ib.disconnect()
        self._state = ConnectionState.DISCONNECTED

    def health_check(self, clock_skew_tolerance_sec: float = 2.0) -> HealthReport:
        """Probe the live session. Sets DEGRADED if the clock skew exceeds tolerance."""
        connected = self._ib is not None and self._ib.isConnected()
        if not connected:
            self._state = ConnectionState.DISCONNECTED
            return HealthReport(False, self._state, None, None)

        server_time = self._ib.reqCurrentTime()
        epoch = (
            server_time.timestamp()
            if isinstance(server_time, dt.datetime)
            else float(server_time)
        )
        skew = abs(time.time() - epoch)
        self._state = (
            ConnectionState.CONNECTED
            if skew <= clock_skew_tolerance_sec
            else ConnectionState.DEGRADED
        )
        return HealthReport(True, self._state, server_time, skew)

    def __enter__(self) -> "IBSession":
        return self.connect()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.disconnect()
