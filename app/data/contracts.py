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


@dataclass(frozen=True)
class ForwardPoint:
    """One maturity of the real put-call-parity forward curve (Step 6)."""
    expiry: str
    forward: float
    implied_carry: float | None
    method: str
    n_pairs: int | None
    is_reliable: bool
    tenor_years: float


@dataclass(frozen=True)
class MarketBase:
    """Real base market state for an underlying (Step 5)."""
    source: str
    underlying: str
    spot: float | None
    reference_type: str | None
    is_stale: bool
    snapshot_ts: str | None


@dataclass(frozen=True)
class BookLeg:
    label: str
    expiry: str
    strike: float
    right: str            # C | P
    qty: float            # signed (negative = short)
    multiplier: float
    vol: float            # synthetic ATM vol used
    base_price: float


@dataclass(frozen=True)
class ScenarioResult:
    """Spot x vol stress grid with worst-case + contributors (Step 12).

    Base spot and forward curve are REAL (Steps 5-6); the illustrative book,
    implied vols and Black-76 reval are SYNTHETIC until Steps 8/10/11 land.
    """
    source: str
    underlying: str
    spot: float
    base_value: float
    spot_shocks: list[float]
    vol_shocks: list[float]
    pnl_matrix: list[list[float]]          # rows=vol_shocks, cols=spot_shocks
    worst_spot_shock: float
    worst_vol_shock: float
    worst_pnl: float
    contributors: list[dict] = field(default_factory=list)
    book: list[BookLeg] = field(default_factory=list)
    forward_curve: list[ForwardPoint] = field(default_factory=list)


@dataclass(frozen=True)
class GreekRow:
    """Position-level price + Greeks (roadmap Step 11)."""
    label: str
    expiry: str
    strike: float
    right: str
    qty: float
    multiplier: float
    vol: float
    price: float
    # per-position (qty * multiplier * unit greek), market conventions:
    delta: float          # $ per 1.0 move in forward
    gamma: float          # delta change per 1.0 move
    vega: float           # $ per 1 vol point (0.01)
    theta: float          # $ per calendar day


@dataclass(frozen=True)
class RiskReport:
    """Aggregated risk table for a book (roadmap Step 11).

    Base spot + forward curve are REAL (Steps 5-6); book / IV / analytic Greeks
    are SYNTHETIC until the IV solver (Step 8) and risk engine (Step 11) land.
    """
    source: str
    underlying: str
    spot: float
    rows: list[GreekRow] = field(default_factory=list)
    net_delta: float = 0.0
    net_gamma: float = 0.0
    net_vega: float = 0.0
    net_theta: float = 0.0
    gross_delta: float = 0.0
    net_value: float = 0.0


@dataclass(frozen=True)
class QCCheck:
    """One QC check outcome (roadmap Step 14, read from real qc_results)."""
    check_ts: str | None
    check_name: str
    target: str | None
    status: str           # USABLE | CAUTION | REJECT
    detail: str


@dataclass(frozen=True)
class ParityOutlier:
    """Put-call-parity diagnostic flagged non-inlier (real forward_diagnostics)."""
    expiry: str
    strike: float
    parity_forward: float
    residual: float
    quality_label: str


@dataclass(frozen=True)
class QCReport:
    """Triage view over the real QC + parity diagnostics (Step 14)."""
    source: str
    trade_date: str | None
    usable: int = 0
    caution: int = 0
    reject: int = 0
    by_check: list[dict] = field(default_factory=list)     # {check, caution, reject}
    failures: list[QCCheck] = field(default_factory=list)   # non-USABLE, non-FINAL
    parity_outliers: list[ParityOutlier] = field(default_factory=list)
