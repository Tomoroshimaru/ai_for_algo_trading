"""Per-position & portfolio risk analytics (Step 11).

Produces the canonical risk snapshot reused everywhere (scenarios, dashboards).
Line-level AND aggregate outputs are both stored: debugging always starts at the
line level - a total Greek without its line breakdown makes the system opaque
(junior note, cours.txt:849-853).

SENSITIVITY SET (task a)
- Instrument level: price, delta, gamma, vega, theta, rho, vanna, volga and the
  monetized $-sensitivities below.
- Portfolio level: summed position_value + summed monetized sensitivities,
  grouped by instrument, expiry (maturity), underlying, and a portfolio total.

DOLLAR CONVENTIONS (documented & stable - acceptance criterion). With
  mult = contract multiplier (100 for equity options, 1 for stock),
  qty = signed contracts/shares, S = spot:
- dollar_delta = delta * S * mult * qty        # cash-equivalent spot exposure
- dollar_gamma = gamma * S^2 * mult * qty * 0.01  # change in $delta per +1% spot
- dollar_vega  = (vega/100) * mult * qty        # P&L per +1 vol point (1%)
- dollar_theta = (theta/365) * mult * qty       # P&L per calendar day
These conventions are fixed here and must not be redefined downstream.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pandas as pd

from forward.engine import parse_option_key
from pricing.engine import PricingRequest, price as price_option, CALL, PUT, EUROPEAN

SENSITIVITIES = ["delta", "gamma", "vega", "theta", "rho", "vanna", "volga"]
DOLLAR_SENSITIVITIES = ["dollar_delta", "dollar_gamma", "dollar_vega", "dollar_theta"]


@dataclass(frozen=True)
class RiskParams:
    rate: float = 0.0
    div_yield: float = 0.0
    option_multiplier: float = 100.0
    stock_multiplier: float = 1.0
    style: str = EUROPEAN
    day_count: float = 365.0
    recon_threshold: float = 0.01      # abs delta diff vs broker to flag


def _spot_map(market_state: pd.DataFrame) -> dict[str, float]:
    """Latest reference_price per underlying (the stock/underlying row)."""
    ms = market_state.dropna(subset=["reference_price"]).sort_values("snapshot_ts")
    return {u: float(g["reference_price"].iloc[-1]) for u, g in ms.groupby("underlying")}


def _vol_map(iv_points: pd.DataFrame) -> dict[tuple, float]:
    """Solved IV keyed by (underlying, expiry, strike, right)."""
    if iv_points is None or len(iv_points) == 0:
        return {}
    sol = iv_points[(iv_points["status"] == "solved") & iv_points["iv"].notna()]
    return {(r["underlying"], r["expiry"], float(r["strike"]), r["option_right"]):
            float(r["iv"]) for _, r in sol.iterrows()}


def compute_line_risk(positions: pd.DataFrame, market_state: pd.DataFrame,
                      iv_points: pd.DataFrame, source_session_id: str,
                      snapshot_ts, params: RiskParams = RiskParams()) -> pd.DataFrame:
    """Join positions to analytics and compute per-line price/Greeks/$-sensitivities."""
    spots = _spot_map(market_state)
    vols = _vol_map(iv_points)
    model = f"bsm_analytic|{params.style}"
    rows: list[dict] = []
    for _, pos in positions.iterrows():
        key = str(pos["instrument_key"])
        und = str(pos["underlying"])
        qty = float(pos["quantity"])
        spot = spots.get(und)
        base = {"snapshot_ts": snapshot_ts, "underlying": und, "instrument_key": key,
                "account": pos.get("account"), "quantity": qty, "spot": spot,
                "model": model, "source_session_id": source_session_id}
        parsed = parse_option_key(key)
        if parsed is None or not key.startswith("OPT"):
            # treat as linear underlying exposure (stock): delta=1, no convexity
            mult = params.stock_multiplier
            px = spot if spot is not None else None
            dd = (qty * mult * spot) if spot is not None else None
            rows.append({**base, "expiry": None, "option_right": None, "strike": None,
                         "multiplier": mult, "vol": None, "price": px,
                         "position_value": (px * mult * qty) if px is not None else None,
                         "delta": 1.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0,
                         "rho": 0.0, "vanna": 0.0, "volga": 0.0,
                         "dollar_delta": dd, "dollar_gamma": 0.0,
                         "dollar_vega": 0.0, "dollar_theta": 0.0,
                         "status": "linear" if spot is not None else "no_spot"})
            continue
        und_k, expiry, strike, right = parsed
        vol = vols.get((und, expiry, strike, right))
        mult = params.option_multiplier
        if spot is None or vol is None:
            rows.append({**base, "expiry": expiry, "option_right": right,
                         "strike": strike, "multiplier": mult, "vol": vol,
                         "price": None, "position_value": None,
                         **{g: None for g in SENSITIVITIES + DOLLAR_SENSITIVITIES},
                         "status": "no_spot" if spot is None else "no_vol"})
            continue
        ttm = (dt.date.fromisoformat(expiry) - snapshot_ts.date()).days / params.day_count
        res = price_option(PricingRequest(spot, strike, ttm, vol, right,
                                          params.style, params.rate, params.div_yield))
        pos_val = res.price * mult * qty
        rows.append({**base, "expiry": expiry, "option_right": right, "strike": strike,
                     "multiplier": mult, "vol": vol, "price": res.price,
                     "position_value": pos_val,
                     "delta": res.delta, "gamma": res.gamma, "vega": res.vega,
                     "theta": res.theta, "rho": res.rho, "vanna": res.vanna,
                     "volga": res.volga,
                     "dollar_delta": res.delta * spot * mult * qty,
                     "dollar_gamma": res.gamma * spot * spot * mult * qty * 0.01,
                     "dollar_vega": res.vega_per_pct * mult * qty,
                     "dollar_theta": res.theta_per_day * mult * qty,
                     "status": "priced"})
    cols = ["snapshot_ts", "underlying", "instrument_key", "account", "expiry",
            "option_right", "strike", "quantity", "multiplier", "spot", "vol",
            "price", "position_value", *SENSITIVITIES, *DOLLAR_SENSITIVITIES,
            "status", "model", "source_session_id"]
    return pd.DataFrame(rows, columns=cols).sort_values(
        ["underlying", "instrument_key"]).reset_index(drop=True)


_AGG_COLS = ["position_value", *SENSITIVITIES[:5], *DOLLAR_SENSITIVITIES]


def aggregate_risk(line: pd.DataFrame, source_session_id: str,
                   snapshot_ts) -> pd.DataFrame:
    """Aggregate by portfolio, underlying, expiry (maturity), and instrument."""
    priced = line[line["status"].isin(["priced", "linear"])].copy()
    model = priced["model"].iloc[0] if len(priced) else "n/a"
    out: list[dict] = []

    def _emit(group_key, group_value, g, und):
        row = {"snapshot_ts": snapshot_ts, "underlying": und,
               "group_key": group_key, "group_value": str(group_value),
               "n_lines": int(len(g)), "model": model,
               "source_session_id": source_session_id}
        for c in _AGG_COLS:
            row[c] = float(g[c].sum())
        out.append(row)

    _emit("portfolio", "ALL", priced, "ALL")
    for und, g in priced.groupby("underlying"):
        _emit("underlying", und, g, und)
    for (und, exp), g in priced[priced["expiry"].notna()].groupby(["underlying", "expiry"]):
        _emit("expiry", exp, g, und)
    for (und, key), g in priced.groupby(["underlying", "instrument_key"]):
        _emit("instrument", key, g, und)
    cols = ["snapshot_ts", "underlying", "group_key", "group_value", "n_lines",
            *_AGG_COLS, "model", "source_session_id"]
    return pd.DataFrame(out, columns=cols).sort_values(
        ["group_key", "underlying", "group_value"]).reset_index(drop=True)


def reconcile_greeks(line: pd.DataFrame, broker_greeks: pd.DataFrame,
                     source_session_id: str, snapshot_ts,
                     params: RiskParams = RiskParams()) -> pd.DataFrame:
    """Compare computed per-line Greeks to broker-returned Greeks (task e).

    `broker_greeks`: columns instrument_key + any of delta/gamma/vega/theta/rho.
    Discrepancies with abs_diff > threshold are flagged breach=True.
    """
    rows: list[dict] = []
    if broker_greeks is None or len(broker_greeks) == 0:
        return pd.DataFrame(rows)
    bmap = {str(r["instrument_key"]): r for _, r in broker_greeks.iterrows()}
    for _, ln in line.iterrows():
        b = bmap.get(str(ln["instrument_key"]))
        if b is None:
            continue
        for greek in ["delta", "gamma", "vega", "theta", "rho"]:
            if greek not in broker_greeks.columns or pd.isna(b.get(greek)):
                continue
            comp = ln.get(greek)
            if comp is None or pd.isna(comp):
                continue
            diff = float(comp) - float(b[greek])
            rows.append({"snapshot_ts": snapshot_ts, "underlying": ln["underlying"],
                         "instrument_key": ln["instrument_key"], "greek": greek,
                         "computed": float(comp), "broker": float(b[greek]),
                         "diff": diff, "abs_diff": abs(diff),
                         "threshold": params.recon_threshold,
                         "breach": abs(diff) > params.recon_threshold,
                         "source_session_id": source_session_id})
    cols = ["snapshot_ts", "underlying", "instrument_key", "greek", "computed",
            "broker", "diff", "abs_diff", "threshold", "breach", "source_session_id"]
    return pd.DataFrame(rows, columns=cols)


def compute_risk(positions, market_state, iv_points, source_session_id, snapshot_ts,
                 broker_greeks=None, params: RiskParams = RiskParams()):
    """Full Step 11 pipeline -> (line_df, aggregate_df, recon_df)."""
    line = compute_line_risk(positions, market_state, iv_points,
                             source_session_id, snapshot_ts, params)
    agg = aggregate_risk(line, source_session_id, snapshot_ts)
    recon = reconcile_greeks(line, broker_greeks, source_session_id, snapshot_ts, params)
    return line, agg, recon
