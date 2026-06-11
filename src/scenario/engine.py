"""Scenario engine & margin-style diagnostics (Step 12).

Approximates worst-case losses under configured spot, volatility and time
shocks for generic risk control / capacity planning / margin diagnostics. It
encodes NO strategy logic (cours.txt:861-863).

The scenario grid is VERSIONED and persisted as data (scenario_defs), never a
mutable notebook cell - the definition is part of the lineage and must be
queryable alongside results (junior note). A report is regenerable exactly from
(positions, analytics snapshot, scenario version).

Two PnL paths per line:
- pnl_full: full repricing through the Step 10 engine (the REFERENCE).
- pnl_greeks: local Taylor approximation from Step 11 Greeks,
    dPnL ~ delta*dS + 0.5*gamma*dS^2 + vega*dVol + theta*dt   (x mult x qty)
  where dS = S*spot_shock, dVol = vol_shock (abs vol pts), dt = -roll/365.
For small shocks the two agree within documented limits (test-enforced).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, asdict

import pandas as pd

from pricing.engine import PricingRequest, price as price_option, EUROPEAN


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    family: str            # spot | vol | time | joint
    spot_shock: float      # relative: -0.10 = -10%
    vol_shock: float       # absolute vol points: +0.05 = +5 vol pts
    time_roll_days: float   # calendar days rolled forward (>=0)
    label: str


def build_grid(version: str = "v1") -> list[Scenario]:
    """Deterministic, versioned scenario grid. Bump the version (and add a new
    branch) rather than mutating an existing one - old reports must stay
    reproducible."""
    if version != "v1":
        raise ValueError(f"unknown scenario grid version {version!r}")
    sc: list[Scenario] = []
    sc.append(Scenario("base", "spot", 0.0, 0.0, 0.0, "base (no shock)"))
    for s in (-0.20, -0.10, -0.05, 0.05, 0.10, 0.20):
        sc.append(Scenario(f"spot_{s:+.2f}", "spot", s, 0.0, 0.0, f"spot {s:+.0%}"))
    for v in (-0.05, -0.02, 0.02, 0.05, 0.10):
        sc.append(Scenario(f"vol_{v:+.2f}", "vol", 0.0, v, 0.0, f"vol {v:+.0%} pts"))
    for d in (1.0, 7.0, 30.0):
        sc.append(Scenario(f"time_{int(d)}d", "time", 0.0, 0.0, d, f"roll {int(d)}d"))
    # margin-style joint shocks: spot down + vol up (crash), spot up + vol down
    sc.append(Scenario("joint_crash", "joint", -0.15, 0.08, 1.0, "crash: spot-15% vol+8pts"))
    sc.append(Scenario("joint_rally", "joint", 0.15, -0.04, 1.0, "rally: spot+15% vol-4pts"))
    return sc


def grid_to_frame(grid: list[Scenario], version: str) -> pd.DataFrame:
    """Materialize the exact grid for persistence (task f, lineage)."""
    rows = [{"version": version, "underlying": "_GRID", **asdict(s)} for s in grid]
    cols = ["version", "scenario_id", "underlying", "family", "spot_shock",
            "vol_shock", "time_roll_days", "label"]
    return pd.DataFrame(rows, columns=cols)


def _reprice_line(ln, sc: Scenario, day_count: float, rate: float, div_yield: float):
    """Return (base_value, scen_value, pnl_full, pnl_greeks) for one risk line."""
    qty = float(ln["quantity"]); mult = float(ln["multiplier"]); spot = ln["spot"]
    if spot is None or pd.isna(spot):
        return None
    new_spot = spot * (1.0 + sc.spot_shock)
    dS = new_spot - spot
    # ----- linear / stock leg ----------------------------------------------
    if ln["status"] == "linear" or pd.isna(ln.get("strike")):
        base = spot * mult * qty
        scen = new_spot * mult * qty
        pnl = scen - base
        return base, scen, pnl, pnl           # delta=1, exact == approx
    # ----- option leg -------------------------------------------------------
    strike = float(ln["strike"]); right = ln["option_right"]; vol = float(ln["vol"])
    base_px = float(ln["price"])
    ttm = (dt.date.fromisoformat(ln["expiry"]) - ln["snapshot_ts"].date()).days / day_count
    new_vol = max(vol + sc.vol_shock, 1e-6)
    new_ttm = max(ttm - sc.time_roll_days / day_count, 0.0)
    scen_px = price_option(PricingRequest(new_spot, strike, new_ttm, new_vol, right,
                                          EUROPEAN, rate, div_yield)).price
    base = base_px * mult * qty
    scen = scen_px * mult * qty
    pnl_full = scen - base
    # Greeks Taylor approximation (per-line Greeks already in the risk line).
    # theta is dPrice/d(calendar time); rolling forward advances calendar time by
    # +roll/365 years, so the time term is theta * (+roll/365) (NOT the change in
    # time-to-maturity, which has the opposite sign).
    dVol = sc.vol_shock
    cal_dt_years = sc.time_roll_days / day_count
    per_unit = (ln["delta"] * dS
                + 0.5 * ln["gamma"] * dS * dS
                + ln["vega"] * dVol
                + ln["theta"] * cal_dt_years)
    pnl_greeks = per_unit * mult * qty
    return base, scen, pnl_full, pnl_greeks


def run_scenarios(risk_line: pd.DataFrame, grid: list[Scenario], version: str,
                  source_session_id: str, snapshot_ts, *, day_count: float = 365.0,
                  rate: float = 0.0, div_yield: float = 0.0):
    """Reprice the full portfolio under every scenario.

    Returns (results_df line-level, summary_df with worst-case flag).
    """
    priceable = risk_line[risk_line["status"].isin(["priced", "linear"])]
    rows: list[dict] = []
    for sc in grid:
        for _, ln in priceable.iterrows():
            out = _reprice_line(ln, sc, day_count, rate, div_yield)
            if out is None:
                continue
            base, scen, pnl_full, pnl_greeks = out
            rows.append({"snapshot_ts": snapshot_ts, "version": version,
                         "scenario_id": sc.scenario_id, "underlying": ln["underlying"],
                         "instrument_key": ln["instrument_key"], "family": sc.family,
                         "base_value": base, "scen_value": scen,
                         "pnl_full": pnl_full, "pnl_greeks": pnl_greeks,
                         "source_session_id": source_session_id})
    rcols = ["snapshot_ts", "version", "scenario_id", "underlying", "instrument_key",
             "family", "base_value", "scen_value", "pnl_full", "pnl_greeks",
             "source_session_id"]
    results = pd.DataFrame(rows, columns=rcols)
    summary = _summarize(results, version, source_session_id, snapshot_ts)
    return results, summary


def _summarize(results, version, source_session_id, snapshot_ts) -> pd.DataFrame:
    if len(results) == 0:
        return pd.DataFrame()
    out: list[dict] = []
    fam = dict(zip(results["scenario_id"], results["family"]))
    # portfolio level
    port = results.groupby("scenario_id").agg(
        n_lines=("instrument_key", "size"),
        pnl_full=("pnl_full", "sum"), pnl_greeks=("pnl_greeks", "sum")).reset_index()
    worst_id = port.loc[port["pnl_full"].idxmin(), "scenario_id"]
    for _, r in port.iterrows():
        out.append({"snapshot_ts": snapshot_ts, "version": version,
                    "scenario_id": r["scenario_id"], "underlying": "ALL",
                    "family": fam[r["scenario_id"]], "group_key": "portfolio",
                    "group_value": "ALL", "n_lines": int(r["n_lines"]),
                    "pnl_full": float(r["pnl_full"]), "pnl_greeks": float(r["pnl_greeks"]),
                    "approx_error": float(r["pnl_greeks"] - r["pnl_full"]),
                    "is_worst_case": bool(r["scenario_id"] == worst_id),
                    "source_session_id": source_session_id})
    # underlying level
    und = results.groupby(["scenario_id", "underlying"]).agg(
        n_lines=("instrument_key", "size"),
        pnl_full=("pnl_full", "sum"), pnl_greeks=("pnl_greeks", "sum")).reset_index()
    for _, r in und.iterrows():
        out.append({"snapshot_ts": snapshot_ts, "version": version,
                    "scenario_id": r["scenario_id"], "underlying": r["underlying"],
                    "family": fam[r["scenario_id"]], "group_key": "underlying",
                    "group_value": r["underlying"], "n_lines": int(r["n_lines"]),
                    "pnl_full": float(r["pnl_full"]), "pnl_greeks": float(r["pnl_greeks"]),
                    "approx_error": float(r["pnl_greeks"] - r["pnl_full"]),
                    "is_worst_case": False, "source_session_id": source_session_id})
    cols = ["snapshot_ts", "version", "scenario_id", "underlying", "family",
            "group_key", "group_value", "n_lines", "pnl_full", "pnl_greeks",
            "approx_error", "is_worst_case", "source_session_id"]
    return pd.DataFrame(out, columns=cols)


def top_contributors(results: pd.DataFrame, scenario_id: str, n: int = 5) -> pd.DataFrame:
    """Most negative per-line PnL under a scenario (explainable worst case)."""
    sub = results[results["scenario_id"] == scenario_id].copy()
    return sub.sort_values("pnl_full").head(n)[
        ["instrument_key", "underlying", "base_value", "scen_value", "pnl_full"]]


def worst_case(summary: pd.DataFrame):
    """Return the portfolio worst-case summary row."""
    port = summary[(summary["group_key"] == "portfolio") & summary["is_worst_case"]]
    return port.iloc[0] if len(port) else None
