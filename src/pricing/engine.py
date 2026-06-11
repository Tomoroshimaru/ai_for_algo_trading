"""Pricing engine (Step 10) - the single source of truth for option valuation.

This is the ONLY module allowed to translate a state vector
(spot, maturity, volatility, rate, carry) into price and Greeks. Keeping it a
clean, tested library (no pricing logic in dataframes/notebooks) makes later
refactors safe (junior note, cours.txt:824-827).

UNIT CONVENTIONS (documented rigorously - task f):
- vol (sigma): annualized, decimal. 0.20 == 20%.
- ttm (T): year fraction (ACT/365 upstream).
- rate (r), div_yield (q): continuously compounded, annual, decimal.
- price: same currency unit as spot/strike.
- delta:  dPrice/dSpot                         (per 1.0 of spot)
- gamma:  d2Price/dSpot^2
- vega:   dPrice/dSigma  per 1.00 vol (=100 vol pts); .vega_per_pct = vega/100
- theta:  dPrice/d(calendar year); .theta_per_day = theta/365  (time decay < 0)
- rho:    dPrice/dRate   per 1.00 (=100%);       .rho_per_bp = rho*1e-4
- vanna:  d2Price/(dSpot dSigma);  volga: d2Price/dSigma^2

The European pricer is Black-Scholes-Merton with carry; with F = S*exp((r-q)T)
and DF = exp(-rT) it equals the Black-76 form used by the inversion engine
(`iv.inversion.black76_price`), so solved IVs reprice exactly (task a).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from iv.inversion import _norm_cdf, _norm_pdf

EUROPEAN, AMERICAN = "european", "american"
CALL, PUT = "C", "P"

# Documented finite-difference bumps for American (and fallback) Greeks.
_H_SPOT_REL = 1e-4   # relative spot bump for delta/gamma
_H_VOL = 1e-4        # absolute vol bump for vega/vanna/volga
_H_TIME = 1e-4       # absolute year bump for theta
_H_RATE = 1e-4       # absolute rate bump for rho
_DEFAULT_STEPS = 256


@dataclass(frozen=True)
class PricingRequest:
    spot: float
    strike: float
    ttm: float
    vol: float
    right: str = CALL                 # 'C' | 'P'
    style: str = EUROPEAN             # 'european' | 'american'
    rate: float = 0.0
    div_yield: float = 0.0
    steps: int = _DEFAULT_STEPS       # binomial steps (American only)

    def __post_init__(self):
        if self.right not in (CALL, PUT):
            raise ValueError(f"right must be 'C' or 'P', got {self.right!r}")
        if self.style not in (EUROPEAN, AMERICAN):
            raise ValueError(f"style must be european|american, got {self.style!r}")
        if self.ttm < 0 or self.vol < 0 or self.spot < 0 or self.strike < 0:
            raise ValueError("ttm, vol, spot, strike must be non-negative")


@dataclass(frozen=True)
class PricingResult:
    price: float
    delta: float
    gamma: float
    vega: float
    theta: float
    rho: float
    vanna: float
    volga: float
    method: str
    n_steps: int = 0

    @property
    def vega_per_pct(self) -> float:
        return self.vega / 100.0

    @property
    def theta_per_day(self) -> float:
        return self.theta / 365.0

    @property
    def rho_per_bp(self) -> float:
        return self.rho * 1e-4


# --------------------------------------------------------------------------- #
# European: closed-form Black-Scholes-Merton with continuous carry.
# --------------------------------------------------------------------------- #
def _intrinsic(spot, strike, right):
    return max(0.0, spot - strike) if right == CALL else max(0.0, strike - spot)


def european_price(spot, strike, ttm, vol, right, rate=0.0, div_yield=0.0) -> float:
    if ttm <= 0 or vol <= 0 or spot <= 0 or strike <= 0:
        return math.exp(-rate * max(ttm, 0.0)) * _intrinsic(
            spot * math.exp((rate - div_yield) * max(ttm, 0.0)), strike, right) \
            if ttm > 0 else _intrinsic(spot, strike, right)
    d1, d2 = _d1d2(spot, strike, ttm, vol, rate, div_yield)
    df_q, df_r = math.exp(-div_yield * ttm), math.exp(-rate * ttm)
    if right == CALL:
        return spot * df_q * _norm_cdf(d1) - strike * df_r * _norm_cdf(d2)
    return strike * df_r * _norm_cdf(-d2) - spot * df_q * _norm_cdf(-d1)


def _d1d2(spot, strike, ttm, vol, rate, div_yield):
    v = vol * math.sqrt(ttm)
    d1 = (math.log(spot / strike) + (rate - div_yield + 0.5 * vol * vol) * ttm) / v
    return d1, d1 - v


def _european_greeks(spot, strike, ttm, vol, right, rate, div_yield) -> dict:
    if ttm <= 0 or vol <= 0:
        return dict(delta=0.0, gamma=0.0, vega=0.0, theta=0.0, rho=0.0,
                    vanna=0.0, volga=0.0)
    d1, d2 = _d1d2(spot, strike, ttm, vol, rate, div_yield)
    df_q, df_r = math.exp(-div_yield * ttm), math.exp(-rate * ttm)
    nd1, sq = _norm_pdf(d1), math.sqrt(ttm)
    gamma = df_q * nd1 / (spot * vol * sq)
    vega = spot * df_q * nd1 * sq
    if right == CALL:
        delta = df_q * _norm_cdf(d1)
        theta = (-spot * df_q * nd1 * vol / (2 * sq)
                 - rate * strike * df_r * _norm_cdf(d2)
                 + div_yield * spot * df_q * _norm_cdf(d1))
        rho = strike * ttm * df_r * _norm_cdf(d2)
    else:
        delta = -df_q * _norm_cdf(-d1)
        theta = (-spot * df_q * nd1 * vol / (2 * sq)
                 + rate * strike * df_r * _norm_cdf(-d2)
                 - div_yield * spot * df_q * _norm_cdf(-d1))
        rho = -strike * ttm * df_r * _norm_cdf(-d2)
    vanna = -df_q * nd1 * d2 / vol
    volga = vega * d1 * d2 / vol
    return dict(delta=delta, gamma=gamma, vega=vega, theta=theta, rho=rho,
                vanna=vanna, volga=volga)


# --------------------------------------------------------------------------- #
# American: Cox-Ross-Rubinstein binomial tree (single-name, carry q).
# --------------------------------------------------------------------------- #
def american_price(spot, strike, ttm, vol, right, rate=0.0, div_yield=0.0,
                   steps=_DEFAULT_STEPS) -> float:
    if ttm <= 0 or vol <= 0 or spot <= 0:
        return _intrinsic(spot, strike, right)
    dt = ttm / steps
    u = math.exp(vol * math.sqrt(dt))
    d = 1.0 / u
    disc = math.exp(-rate * dt)
    p = (math.exp((rate - div_yield) * dt) - d) / (u - d)
    if not (0.0 <= p <= 1.0):           # numerical guard: tree not risk-neutral
        p = min(1.0, max(0.0, p))
    j = np.arange(steps + 1)
    prices = spot * (u ** (steps - j)) * (d ** j)
    if right == CALL:
        values = np.maximum(prices - strike, 0.0)
    else:
        values = np.maximum(strike - prices, 0.0)
    for i in range(steps, 0, -1):
        prices = prices[:i] / u            # spot lattice one step earlier
        cont = disc * (p * values[:i] + (1 - p) * values[1:i + 1])
        exer = (prices - strike) if right == CALL else (strike - prices)
        values = np.maximum(cont, np.maximum(exer, 0.0))
    return float(values[0])


def _american_greeks(spot, strike, ttm, vol, right, rate, div_yield, steps) -> dict:
    """Central finite differences with documented bumps (American has no
    simple closed-form Greeks)."""
    def P(s=spot, k=strike, t=ttm, v=vol, r=rate, q=div_yield):
        return american_price(s, k, t, v, right, r, q, steps)
    h = _H_SPOT_REL * spot
    p0 = P()
    delta = (P(s=spot + h) - P(s=spot - h)) / (2 * h)
    gamma = (P(s=spot + h) - 2 * p0 + P(s=spot - h)) / (h * h)
    vega = (P(v=vol + _H_VOL) - P(v=vol - _H_VOL)) / (2 * _H_VOL)
    rho = (P(r=rate + _H_RATE) - P(r=rate - _H_RATE)) / (2 * _H_RATE)
    if ttm > _H_TIME:
        theta = (P(t=ttm - _H_TIME) - P(t=ttm + _H_TIME)) / (2 * _H_TIME)
    else:
        theta = (P(t=max(ttm - _H_TIME, 1e-9)) - p0) / _H_TIME
    # second cross/vol: vanna, volga via vega bumps
    vega_up = (P(s=spot + h, v=vol + _H_VOL) - P(s=spot - h, v=vol + _H_VOL)) / (2 * h)
    vega_dn = (P(s=spot + h, v=vol - _H_VOL) - P(s=spot - h, v=vol - _H_VOL)) / (2 * h)
    vanna = (vega_up - vega_dn) / (2 * _H_VOL)
    volga = (P(v=vol + _H_VOL) - 2 * p0 + P(v=vol - _H_VOL)) / (_H_VOL * _H_VOL)
    return dict(delta=delta, gamma=gamma, vega=vega, theta=theta, rho=rho,
                vanna=vanna, volga=volga)


# --------------------------------------------------------------------------- #
# Public typed API.
# --------------------------------------------------------------------------- #
def price(req: PricingRequest) -> PricingResult:
    """Scalar typed entry point: state vector in, price + Greeks out."""
    if req.style == EUROPEAN:
        px = european_price(req.spot, req.strike, req.ttm, req.vol, req.right,
                            req.rate, req.div_yield)
        g = _european_greeks(req.spot, req.strike, req.ttm, req.vol, req.right,
                             req.rate, req.div_yield)
        return PricingResult(price=px, method="bsm_analytic", n_steps=0, **g)
    px = american_price(req.spot, req.strike, req.ttm, req.vol, req.right,
                        req.rate, req.div_yield, req.steps)
    g = _american_greeks(req.spot, req.strike, req.ttm, req.vol, req.right,
                        req.rate, req.div_yield, req.steps)
    return PricingResult(price=px, method="crr_binomial_fd", n_steps=req.steps, **g)


# --------------------------------------------------------------------------- #
# Vectorized European interface (price an entire array of strikes/vols).
# --------------------------------------------------------------------------- #
def european_price_array(spot, strike, ttm, vol, right, rate=0.0, div_yield=0.0):
    """Vectorized BSM price. Inputs broadcast as numpy arrays. right: 'C'/'P'."""
    spot, strike, ttm, vol = (np.asarray(x, float) for x in (spot, strike, ttm, vol))
    out = np.empty(np.broadcast(spot, strike, ttm, vol).shape)
    with np.errstate(divide="ignore", invalid="ignore"):
        v = vol * np.sqrt(ttm)
        d1 = (np.log(spot / strike) + (rate - div_yield + 0.5 * vol ** 2) * ttm) / v
        d2 = d1 - v
        from math import erf
        ncdf = np.vectorize(lambda x: 0.5 * (1.0 + erf(x / math.sqrt(2))))
        df_q, df_r = np.exp(-div_yield * ttm), np.exp(-rate * ttm)
        if right == CALL:
            out = spot * df_q * ncdf(d1) - strike * df_r * ncdf(d2)
        else:
            out = strike * df_r * ncdf(-d2) - spot * df_q * ncdf(-d1)
    intrinsic = (np.maximum(spot - strike, 0.0) if right == CALL
                 else np.maximum(strike - spot, 0.0))
    out = np.where((ttm <= 0) | (vol <= 0), intrinsic, out)
    return out
