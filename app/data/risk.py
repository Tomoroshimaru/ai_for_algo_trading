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
from utils.config import load_config
from storage.parquet_store import ParquetStore


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
        self._cfg = load_config()
        self._pq = ParquetStore(self._cfg.environment.data_dir)

    def _pq_latest(self, dataset: str) -> str | None:
        root = self._pq._dataset_root(dataset)  # noqa: SLF001
        if not root.exists():
            return None
        dates = sorted(d.name.split("=")[1] for d in root.glob("trade_date=*"))
        return dates[-1] if dates else None

    @staticmethod
    def _f(v, default=0.0):
        try:
            return float(v) if v == v else default  # noqa: PLR0124 (nan check)
        except (TypeError, ValueError):
            return default

    def get_risk(self, underlying: str) -> RiskReport:
        real = self._real_risk(underlying)
        return real if real is not None else self._synthetic_risk(underlying)

    # ---- REAL: pricing (Step 10) + risk engine (Step 11) ---------------
    def _real_risk(self, underlying: str) -> RiskReport | None:
        td = self._pq_latest("greeks")
        if not td:
            return None
        g = self._pq.read("greeks", trade_date=td)
        if g is None or g.empty:
            return None
        g = g[g["underlying"] == underlying]
        if g.empty:
            return None
        rows: list[GreekRow] = []
        for _, r in g.iterrows():
            qty = self._f(r["quantity"]); mult = self._f(r["multiplier"], 1.0)
            udelta = self._f(r["delta"]); ugamma = self._f(r["gamma"])
            right = r["option_right"] if isinstance(r["option_right"], str) else "-"
            rows.append(GreekRow(
                label=str(r["instrument_key"]),
                expiry=str(r["expiry"]) if isinstance(r["expiry"], str) else "",
                strike=self._f(r["strike"]), right=right, qty=qty, multiplier=mult,
                vol=round(self._f(r["vol"]), 4), price=round(self._f(r["price"]), 4),
                delta=round(qty * mult * udelta, 2), gamma=round(qty * mult * ugamma, 4),
                vega=round(self._f(r["dollar_vega"]), 2), theta=round(self._f(r["dollar_theta"]), 2),
            ))
        # broker reconciliation breaches (real)
        breaches: list[dict] = []
        td_rec = self._pq_latest("risk_recon")
        if td_rec:
            rec = self._pq.read("risk_recon", trade_date=td_rec)
            if rec is not None and not rec.empty:
                rec = rec[(rec["underlying"] == underlying) & (rec["breach"])]
                breaches = [
                    {"instrument": str(x["instrument_key"]), "greek": str(x["greek"]),
                     "computed": round(self._f(x["computed"]), 4), "broker": round(self._f(x["broker"]), 4),
                     "abs_diff": round(self._f(x["abs_diff"]), 4), "threshold": round(self._f(x["threshold"]), 4)}
                    for _, x in rec.iterrows()
                ]
        # real aggregates (portfolio + by-expiry), authoritative net greeks incl rho
        groups: list[dict] = []
        td_agg = self._pq_latest("risk_aggregates")
        if td_agg:
            agg = self._pq.read("risk_aggregates", trade_date=td_agg)
            if agg is not None and not agg.empty:
                a = agg[(agg["underlying"].isin([underlying, "ALL"]))
                        & (agg["group_key"].isin(["portfolio", "expiry"]))]
                for _, x in a.sort_values(["group_key", "group_value"]).iterrows():
                    groups.append({
                        "group": str(x["group_key"]), "value": str(x["group_value"]),
                        "n_lines": int(x["n_lines"]),
                        "delta": round(self._f(x["dollar_delta"]), 0),
                        "gamma": round(self._f(x["dollar_gamma"]), 0),
                        "vega": round(self._f(x["dollar_vega"]), 0),
                        "theta": round(self._f(x["dollar_theta"]), 0),
                        "rho": round(self._f(x["rho"]), 2),
                    })
        spot = self._f(g["spot"].iloc[0])
        model = str(g["model"].iloc[0]) if "model" in g else None
        return RiskReport(
            source=f"REAL — pricing (Step 10) + risk engine (Step 11), model={model} @ {td}",
            underlying=underlying, spot=round(spot, 4), rows=rows,
            net_delta=round(sum(r.delta for r in rows), 2),
            net_gamma=round(sum(r.gamma for r in rows), 4),
            net_vega=round(sum(r.vega for r in rows), 2),
            net_theta=round(sum(r.theta for r in rows), 2),
            gross_delta=round(sum(abs(r.delta) for r in rows), 2),
            net_value=round(sum(self._f(v) for v in g["position_value"]), 2),
            model=model, recon_breaches=breaches, groups=groups,
        )

    # ---- SYNTHETIC fallback (analytic Black-76) ------------------------
    def _synthetic_risk(self, underlying: str) -> RiskReport:
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
