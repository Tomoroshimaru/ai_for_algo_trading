"""Implied-volatility inversion engine (Step 8).

Scalar Black-76 inversion via a bracketed root solver, then a batch wrapper over
a chain (junior note: scalar first, readable, well-tested; cours.txt:746-749).
Every solved IV carries enough metadata to judge reliability: convergence status,
iteration count, final residual, the bracket used, delta, and the pricing model.

Black-76 (options on a forward F, discount factor DF):
  call = DF * (F*N(d1) - K*N(d2)),  put = DF * (K*N(-d2) - F*N(-d1))
  d1 = (ln(F/K) + 0.5*sigma^2*T) / (sigma*sqrt(T)),  d2 = d1 - sigma*sqrt(T)

American options are inverted under a documented proxy convention (European
Black-76 on the parity forward, early-exercise premium ignored), flagged via
method='american_proxy'. This is explicit, not hidden.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from forward.engine import parse_option_key

_SQRT2 = math.sqrt(2.0)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / _SQRT2))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def black76_price(forward: float, strike: float, ttm: float, sigma: float,
                  right: str, df: float = 1.0) -> float:
    """Undiscounted-by-DF Black-76 price for a call ('C') or put ('P')."""
    if ttm <= 0 or sigma <= 0:
        intrinsic = max(0.0, forward - strike) if right == "C" else max(0.0, strike - forward)
        return df * intrinsic
    v = sigma * math.sqrt(ttm)
    d1 = (math.log(forward / strike) + 0.5 * v * v) / v
    d2 = d1 - v
    if right == "C":
        return df * (forward * _norm_cdf(d1) - strike * _norm_cdf(d2))
    return df * (strike * _norm_cdf(-d2) - forward * _norm_cdf(-d1))


def black76_delta(forward: float, strike: float, ttm: float, sigma: float,
                  right: str, df: float = 1.0) -> float:
    if ttm <= 0 or sigma <= 0:
        return float("nan")
    v = sigma * math.sqrt(ttm)
    d1 = (math.log(forward / strike) + 0.5 * v * v) / v
    return df * (_norm_cdf(d1) if right == "C" else _norm_cdf(d1) - 1.0)


@dataclass(frozen=True)
class IVResult:
    iv: float | None
    status: str          # solved | near_intrinsic | no_arbitrage | short_dated | no_bracket
    n_iter: int
    residual: float
    bracket_lo: float
    bracket_hi: float
    delta: float | None


@dataclass(frozen=True)
class IVParams:
    sigma_lo: float = 1e-4
    sigma_hi: float = 5.0
    tol_price: float = 1e-8       # residual tolerance on price
    max_iter: int = 100
    min_ttm: float = 1.0 / 365.0  # below this -> short_dated fallback
    intrinsic_buffer: float = 1e-6


def invert_black76(price: float, forward: float, strike: float, ttm: float,
                   right: str, df: float = 1.0, params: IVParams = IVParams()) -> IVResult:
    """Scalar bracketed inversion (bisection: deterministic and robust)."""
    intrinsic = (max(0.0, forward - strike) if right == "C"
                 else max(0.0, strike - forward)) * df
    upper_bound = (df * forward) if right == "C" else (df * strike)

    # (c) no-arbitrage / intrinsic bounds detect unsolvable inputs
    if price <= 0 or price >= upper_bound:
        return IVResult(None, "no_arbitrage", 0, float("nan"),
                        params.sigma_lo, params.sigma_hi, None)
    if price <= intrinsic + params.intrinsic_buffer:
        return IVResult(None, "near_intrinsic", 0, price - intrinsic,
                        params.sigma_lo, params.sigma_hi, None)
    # (e) short-dated fallback
    if ttm < params.min_ttm:
        return IVResult(None, "short_dated", 0, float("nan"),
                        params.sigma_lo, params.sigma_hi, None)

    def f(sig: float) -> float:
        return black76_price(forward, strike, ttm, sig, right, df) - price

    lo, hi = params.sigma_lo, params.sigma_hi
    f_lo, f_hi = f(lo), f(hi)
    if f_lo * f_hi > 0:    # price not bracketed within [sigma_lo, sigma_hi]
        return IVResult(None, "no_bracket", 0, min(abs(f_lo), abs(f_hi)), lo, hi, None)

    a, b, fa = lo, hi, f_lo
    mid, fm = a, fa
    for i in range(1, params.max_iter + 1):
        mid = 0.5 * (a + b)
        fm = f(mid)
        if abs(fm) < params.tol_price or (b - a) < 1e-12:
            delta = black76_delta(forward, strike, ttm, mid, right, df)
            return IVResult(mid, "solved", i, fm, lo, hi, delta)
        if fa * fm < 0:
            b = mid
        else:
            a, fa = mid, fm
    delta = black76_delta(forward, strike, ttm, mid, right, df)
    return IVResult(mid, "solved", params.max_iter, fm, lo, hi, delta)


def invert_chain(
    filtered_quotes: pd.DataFrame,
    forwards: pd.DataFrame,
    source_session_id: str,
    rate: float | None = None,
    day_count: float = 365.0,
    american: bool = False,
    params: IVParams = IVParams(),
) -> pd.DataFrame:
    """Batch interface (f): solve an entire chain into an iv_points table.

    `filtered_quotes` are QC-passed option rows (need instrument_key + mid).
    `forwards` provides the per-(underlying, expiry) forward (Step 6 output).
    """
    import datetime as dt

    fwd_map = {(r["underlying"], r["expiry"]): r["forward"]
               for _, r in forwards.iterrows()}
    method = "american_proxy" if american else "black76"
    rows: list[dict] = []
    for _, q in filtered_quotes.iterrows():
        parsed = parse_option_key(str(q["instrument_key"]))
        if parsed is None:
            continue
        und, expiry, strike, right = parsed
        forward = fwd_map.get((und, expiry))
        mid = q.get("mid")
        snapshot_ts = q["snapshot_ts"]
        ttm = (dt.date.fromisoformat(expiry) - snapshot_ts.date()).days / day_count
        df = math.exp(-rate * ttm) if (rate is not None and ttm > 0) else 1.0
        base = {"snapshot_ts": snapshot_ts, "underlying": und, "expiry": expiry,
                "strike": strike, "option_right": right, "forward": forward,
                "ttm_years": ttm, "method": method,
                "source_session_id": source_session_id,
                "moneyness": (math.log(strike / forward)
                              if forward and forward > 0 else None)}
        if forward is None or forward <= 0 or mid is None or pd.isna(mid):
            rows.append({**base, "iv": None, "delta": None, "status": "no_arbitrage",
                         "n_iter": 0, "residual": None,
                         "bracket_lo": None, "bracket_hi": None})
            continue
        res = invert_black76(float(mid), float(forward), strike, ttm, right, df, params)
        rows.append({**base, "iv": res.iv, "delta": res.delta, "status": res.status,
                     "n_iter": res.n_iter, "residual": res.residual,
                     "bracket_lo": res.bracket_lo, "bracket_hi": res.bracket_hi})
    cols = ["snapshot_ts", "underlying", "expiry", "strike", "option_right", "iv",
            "moneyness", "forward", "delta", "ttm_years", "method", "status",
            "n_iter", "residual", "bracket_lo", "bracket_hi", "source_session_id"]
    return pd.DataFrame(rows, columns=cols).sort_values(
        ["underlying", "expiry", "strike", "option_right"]).reset_index(drop=True)
