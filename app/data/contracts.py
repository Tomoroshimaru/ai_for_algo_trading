"""Frontend data contracts (typed boundary between UI and backend).

These dataclasses are the *only* shapes the templates and API routes depend
on. The backend can evolve freely as long as a provider keeps returning these
objects, satisfying the roadmap's layer-separation principle.

Every object carries a ``source`` provenance label so the UI never hides a
fallback (roadmap Part II: "Never hide a fallback").
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class HealthMetrics:
    """Connectivity / system-health snapshot (roadmap Step 1 + Step 15)."""
    source: str                      # e.g. "artifact:bootstrap_...json" | "none"
    environment: str | None
    connected: bool
    connection_state: str            # CONNECTED | DEGRADED | DISCONNECTED | ...
    server_time_utc: str | None
    clock_skew_sec: float | None
    market_data_type: int | None
    sample_symbol: str | None
    sample_market_price: float | None
    evidence_time_utc: str | None


@dataclass(frozen=True)
class UniverseSummary:
    """Instrument-master summary (roadmap Step 2)."""
    source: str                      # "backend" | "none"
    session_date: str | None
    config_fingerprint: str | None
    generated_utc: str | None
    underlying_count: int
    option_count: int
    underlyings: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class SurfacePoint:
    strike: float
    log_moneyness: float
    iv: float
    total_variance: float


@dataclass(frozen=True)
class SurfaceSlice:
    """One maturity slice for the surface explorer (roadmap Step 9).

    ``points`` are 'accepted' IV observations; ``fitted_*`` is the smooth
    slice. For now IVs are synthetic (Steps 8-9 pending) but strikes and the
    forward anchor come from the real backend universe artifact.
    """
    source: str
    real_universe: bool
    underlying: str
    expiry: str
    session_date: str | None
    forward: float
    tenor_years: float
    points: list[SurfacePoint] = field(default_factory=list)
    fitted_k: list[float] = field(default_factory=list)
    fitted_iv: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class SessionMetrics:
    """Per-collector-session ingestion metrics (roadmap Step 15)."""
    session_id: str
    market_events: int
    ops_events: int
    corrupt_lines: int
    duration_sec: float
    event_rate_hz: float
    reconnects: int
    disconnects: int
    errors: int
    stale_ratio: float
    max_staleness_sec: float
    last_event_utc: str | None


@dataclass(frozen=True)
class ObservabilitySnapshot:
    """System-health / event-rate snapshot for one trade date (Step 15).

    Built by replaying the real append-only raw-event store, so corrupt-line
    counts and reconnects are observed, never inferred.
    """
    source: str
    trade_date: str | None
    sessions: list[SessionMetrics] = field(default_factory=list)
    total_market_events: int = 0
    total_ops_events: int = 0
    total_corrupt: int = 0
    total_reconnects: int = 0
    total_errors: int = 0
    distinct_instruments: int = 0
    stale_quote_tolerance_sec: float = 0.0
