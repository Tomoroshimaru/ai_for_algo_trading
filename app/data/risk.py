"""Risk / Greeks connectique (roadmap Step 11).

Reuses the real base state (spot, forward curve) and the illustrative book from
the scenario layer, then computes analytic Black-76 Greeks. Base is REAL
(Steps 5-6); book / IV / Greeks are SYNTHETIC until Step 8 (IV) and Step 11
(risk engine) land — at which point only ``_greeks`` and the book source change.
"""
from __future__ import annotations

import math

import app._paths  # noqa: F401

from app.data.contracts import GreekRow, RiskReport
from app.data.scenario import ScenarioProvider, _black76


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _greeks(forward: float, strike: float, tenor: float, vol: float, right: str) -> dict:
    """Undiscounted Black-76 unit greeks (DF=1), per 1 contract.

    delta wrt forward; vega per 1 vol point (0.01); theta per calendar day.
    """
    if tenor <= 0 or vol <= 0 or forward <= 0 or strike <= 0:
        intrinsic_delta = (1.0 if forward > strike else 0.0) if right == "C" else \
                          (-1.0 if forward < strike else 0.0)
        return {"delta": intrinsic_delta, "gamma": 0.0, "vega": 0.0, "theta": 0.0}
    srt = vol * math.sqrt(tenor)
    d1 = (math.log(forward / strike) + 0.5 * vol * vol * tenor) / srt
    npdf = _norm_pdf(d1)
    delta = _norm_cdf(d1) if right == "C" else _norm_cdf(d1) - 1.0
    gamma = npdf / (forward * srt)
    vega_per_vol = forward * npdf * math.sqrt(tenor)   # per 1.00 vol
    theta_per_year = -(forward * npdf * vol) / (2.0 * math.sqrt(tenor))
    return {
        "delta": delta,
        "gamma": gamma,
        "vega": vega_per_vol * 0.01,    # per 1 vol point
        "theta": theta_per_year / 365.0,  # per calendar day
    }


class RiskProvider:
    def __init__(self) -> None:
        self._scen = ScenarioProvider()

    def get_risk(self, underlying: str) -> RiskReport:
        base = self._scen.get_market_base(underlying)
        curve = self._scen.get_forward_curve(underlying)
        spot = base.spot or (curve[0].forward if curve else float("nan"))
        legs = self._scen.build_book(curve)
        fwd_by_exp = {fp.expiry: fp for fp in curve}

        rows: list[GreekRow] = []
        for leg in legs:
            fp = fwd_by_exp[leg.expiry]
            g = _greeks(fp.forward, leg.strike, fp.tenor_years, leg.vol, leg.right)
            scale = leg.qty * leg.multiplier
            price = _black76(fp.forward, leg.strike, fp.tenor_years, leg.vol, leg.right)
            rows.append(GreekRow(
                label=leg.label, expiry=leg.expiry, strike=leg.strike, right=leg.right,
                qty=leg.qty, multiplier=leg.multiplier, vol=leg.vol, price=round(price, 4),
                delta=round(scale * g["delta"], 2), gamma=round(scale * g["gamma"], 4),
                vega=round(scale * g["vega"], 2), theta=round(scale * g["theta"], 2),
            ))
        real = base.spot is not None and bool(curve)
        src = ("base spot + forward curve REAL (Steps 5-6); book/IV/analytic Greeks "
               "SYNTHETIC (Steps 8/11 PENDING)") if real else \
              "no real market_state/forwards on disk — run Steps 5-6 first"
        return RiskReport(
            source=src, underlying=underlying, spot=round(float(spot), 4), rows=rows,
            net_delta=round(sum(r.delta for r in rows), 2),
            net_gamma=round(sum(r.gamma for r in rows), 4),
            net_vega=round(sum(r.vega for r in rows), 2),
            net_theta=round(sum(r.theta for r in rows), 2),
            gross_delta=round(sum(abs(r.delta) for r in rows), 2),
            net_value=round(sum(r.qty * r.multiplier * r.price for r in rows), 2),
        )
