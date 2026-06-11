"""Shared daily analytics pipeline (Step 13 foundation).

This is the SINGLE code path used by BOTH live processing and historical replay.
There is deliberately no 'historical only' fork - dual code paths drift and become
inconsistent (junior note, cours.txt:915-918). Live and replay differ only in
where the input snapshot comes from and where outputs are written, never in how
analytics are computed.

Stages (cours.txt:896-898): snapshot -> forwards -> IV -> surface -> risk.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from forward.engine import compute_forwards, ForwardParams
from iv.inversion import invert_chain, IVParams
from surface.engine import fit_surface, SurfaceParams
from risk.analytics import compute_risk, RiskParams


@dataclass
class RunResult:
    """All derived frames for one snapshot. Empty frames where a stage had no input."""
    forwards: pd.DataFrame
    forward_diagnostics: pd.DataFrame
    iv_points: pd.DataFrame
    surface_params: pd.DataFrame
    surface_grid: pd.DataFrame
    risk_line: pd.DataFrame | None = None
    risk_aggregates: pd.DataFrame | None = None
    risk_recon: pd.DataFrame | None = None
    diagnostics: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PipelineParams:
    forward: ForwardParams = ForwardParams()
    iv: IVParams = IVParams()
    surface: SurfaceParams = SurfaceParams()
    risk: RiskParams = RiskParams()
    rate: float | None = None
    american: bool = False


def run_day(snapshot: pd.DataFrame, source_session_id: str, *,
            positions: pd.DataFrame | None = None,
            broker_greeks: pd.DataFrame | None = None,
            snapshot_ts=None, params: PipelineParams = PipelineParams()) -> RunResult:
    """Compute the full derived analytics chain for one normalized snapshot.

    `snapshot` is a market_state-shaped frame (STK + OPT rows). Identical call for
    live and replay. `positions` is optional: when provided the risk stage runs.
    """
    options = snapshot[snapshot["instrument_key"].str.startswith("OPT")]

    forwards, fwd_diag = compute_forwards(snapshot, source_session_id, params.forward)

    if len(options) and len(forwards):
        iv_points = invert_chain(options, forwards, source_session_id,
                                 rate=params.rate, american=params.american,
                                 params=params.iv)
    else:
        iv_points = _empty("iv_points")

    if len(iv_points):
        surf_params, surf_grid, surf_diag = fit_surface(
            iv_points, source_session_id, params.surface)
    else:
        surf_params, surf_grid, surf_diag = _empty("surface_params"), _empty("surface_grid"), {}

    res = RunResult(forwards=forwards, forward_diagnostics=fwd_diag,
                    iv_points=iv_points, surface_params=surf_params,
                    surface_grid=surf_grid,
                    diagnostics={"forward": _fwd_diag_summary(fwd_diag),
                                 "surface": surf_diag,
                                 "n_options": int(len(options)),
                                 "n_iv_solved": int((iv_points["status"] == "solved").sum())
                                 if len(iv_points) else 0})

    if positions is not None and len(positions):
        ts = snapshot_ts if snapshot_ts is not None else snapshot["snapshot_ts"].max()
        line, agg, recon = compute_risk(positions, snapshot, iv_points,
                                        source_session_id, ts,
                                        broker_greeks=broker_greeks, params=params.risk)
        res.risk_line, res.risk_aggregates, res.risk_recon = line, agg, recon
    return res


def _empty(dataset_name: str) -> pd.DataFrame:
    from storage.schemas import get_dataset
    return get_dataset(dataset_name).schema.empty_table().to_pandas()


def _fwd_diag_summary(fwd_diag: pd.DataFrame) -> dict:
    if fwd_diag is None or len(fwd_diag) == 0:
        return {"n": 0}
    return {"n": int(len(fwd_diag))}
