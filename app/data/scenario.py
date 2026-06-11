"""Scenario / stress connectique (roadmap Step 12).

Base market state (spot) and the forward curve are REAL (Steps 5-6, read from
the Parquet warehouse). The illustrative book, ATM implied vols and Black-76
revaluation are SYNTHETIC and clearly labeled until the IV solver (Step 8),
pricer (Step 10) and risk engine (Step 11) are implemented. Swapping in the
real pricer later means replacing ``_price`` and ``build_book`` only.
"""
from __future__ import annotations

import datetime as dt
import math

import app._paths  # noqa: F401  side-effect: src/ on sys.path

from utils.config import load_config
from storage.parquet_store import ParquetStore

from app.data.contracts import (
    BookLeg,
    ForwardPoint,
    MarketBase,
    ScenarioResult,
)

_SPOT_SHOCKS = [-0.10, -0.05, -0.02, 0.0, 0.02, 0.05, 0.10]
_VOL_SHOCKS = [-0.05, -0.02, 0.0, 0.02, 0.05]   # additive vol points


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _black76(forward: float, strike: float, tenor: float, vol: float, right: str) -> float:
    """Undiscounted Black-76 option value (DF=1, consistent with forward engine)."""
    if tenor <= 0 or vol <= 0 or forward <= 0 or strike <= 0:
        intrinsic = max(forward - strike, 0.0) if right == "C" else max(strike - forward, 0.0)
        return intrinsic
    srt = vol * math.sqrt(tenor)
    d1 = (math.log(forward / strike) + 0.5 * vol * vol * tenor) / srt
    d2 = d1 - srt
    if right == "C":
        return forward * _norm_cdf(d1) - strike * _norm_cdf(d2)
    return strike * _norm_cdf(-d2) - forward * _norm_cdf(-d1)


class ScenarioProvider:
    def __init__(self) -> None:
        self._cfg = load_config()
        self._pq = ParquetStore(self._cfg.environment.data_dir)

    # ---- real reads -----------------------------------------------------
    def _latest_trade_date(self, dataset: str) -> str | None:
        root = self._pq._dataset_root(dataset)  # noqa: SLF001 (read-only path helper)
        if not root.exists():
            return None
        dates = sorted(p.name.split("=")[1] for p in root.glob("trade_date=*"))
        return dates[-1] if dates else None

    def get_market_base(self, underlying: str, trade_date: str | None = None) -> MarketBase:
        td = trade_date or self._latest_trade_date("market_state")
        df = self._pq.read("market_state", trade_date=td, underlying=underlying) if td else None
        if df is None or df.empty:
            return MarketBase("none", underlying, None, None, False, None)
        stk = df[df["instrument_key"] == f"STK:{underlying}:USD"]
        row = (stk if not stk.empty else df).sort_values("snapshot_ts").iloc[-1]
        return MarketBase(
            source=f"market_state @ {td}", underlying=underlying,
            spot=float(row["reference_price"]) if row["reference_price"] == row["reference_price"] else None,
            reference_type=row.get("reference_type"),
            is_stale=bool(row.get("is_stale", False)),
            snapshot_ts=str(row["snapshot_ts"]),
        )

    def get_forward_curve(self, underlying: str, trade_date: str | None = None) -> list[ForwardPoint]:
        td = trade_date or self._latest_trade_date("forwards")
        df = self._pq.read("forwards", trade_date=td, underlying=underlying) if td else None
        if df is None or df.empty:
            return []
        td0 = dt.date.fromisoformat(td)
        pts: list[ForwardPoint] = []
        for _, r in df.sort_values("expiry").iterrows():
            tenor = max((dt.date.fromisoformat(r["expiry"]) - td0).days, 1) / 365.0
            pts.append(ForwardPoint(
                expiry=r["expiry"], forward=float(r["forward"]),
                implied_carry=float(r["implied_carry"]) if r["implied_carry"] == r["implied_carry"] else None,
                method=str(r["method"]), n_pairs=int(r["n_pairs"]) if r["n_pairs"] == r["n_pairs"] else None,
                is_reliable=bool(r["is_reliable"]), tenor_years=round(tenor, 4),
            ))
        return pts

    # ---- synthetic book + reval ----------------------------------------
    @staticmethod
    def _atm_vol(tenor: float) -> float:
        return 0.16 + 0.06 * math.sqrt(max(tenor, 1e-6))

    def build_book(self, curve: list[ForwardPoint]) -> list[BookLeg]:
        """Illustrative short-ATM-straddle per reliable maturity (synthetic)."""
        legs: list[BookLeg] = []
        for fp in curve:
            if not fp.is_reliable:
                continue
            vol = self._atm_vol(fp.tenor_years)
            k = round(fp.forward)
            for right in ("C", "P"):
                base = _black76(fp.forward, k, fp.tenor_years, vol, right)
                legs.append(BookLeg(
                    label=f"short {right} {fp.expiry} K={k:g}", expiry=fp.expiry,
                    strike=float(k), right=right, qty=-1.0, multiplier=100.0,
                    vol=round(vol, 4), base_price=round(base, 4),
                ))
        return legs

    def _real_book(self, underlying: str, curve: list[ForwardPoint]) -> list[BookLeg] | None:
        """Real positions from the greeks dataset (Step 11); None if absent."""
        td = self._latest_trade_date("greeks")
        if not td:
            return None
        g = self._pq.read("greeks", trade_date=td)
        if g is None or g.empty:
            return None
        g = g[g["underlying"] == underlying]
        if g.empty:
            return None
        legs: list[BookLeg] = []
        for _, r in g.iterrows():
            right = r["option_right"] if isinstance(r["option_right"], str) else "-"
            vol = float(r["vol"]) if r["vol"] == r["vol"] else 0.0  # noqa: PLR0124
            exp = str(r["expiry"]) if isinstance(r["expiry"], str) else ""
            strike = float(r["strike"]) if r["strike"] == r["strike"] else 0.0  # noqa: PLR0124
            legs.append(BookLeg(
                label=str(r["instrument_key"]), expiry=exp, strike=strike, right=right,
                qty=float(r["quantity"]), multiplier=float(r["multiplier"]),
                vol=round(vol, 4), base_price=round(float(r["price"]), 4),
            ))
        return legs

    def run_scenario(self, underlying: str) -> ScenarioResult:
        base = self.get_market_base(underlying)
        curve = self.get_forward_curve(underlying)
        spot = base.spot or (curve[0].forward if curve else float("nan"))
        fwd_by_exp = {fp.expiry: fp for fp in curve}

        real_book = self._real_book(underlying, curve)
        legs = real_book if real_book is not None else self.build_book(curve)

        # per-option (forward, tenor) meta, real where possible
        ref = self._latest_trade_date("greeks") or self._latest_trade_date("forwards")
        ref_d = dt.date.fromisoformat(ref) if ref else dt.date.today()
        meta: dict[str, tuple[float, float]] = {}
        for leg in legs:
            if leg.right not in ("C", "P"):
                continue
            fp = fwd_by_exp.get(leg.expiry)
            if fp is not None:
                meta[leg.label] = (fp.forward, fp.tenor_years)
            else:
                t = max((dt.date.fromisoformat(leg.expiry) - ref_d).days, 1) / 365.0
                meta[leg.label] = (spot, t)

        def book_value(spot_shock: float, vol_shock: float) -> tuple[float, list[float]]:
            total = 0.0
            per_leg = []
            for leg in legs:
                if leg.right not in ("C", "P"):          # linear instrument (stock)
                    val = leg.qty * leg.multiplier * spot * (1.0 + spot_shock)
                else:
                    f0, tenor = meta[leg.label]
                    f_shocked = f0 * (1.0 + spot_shock)
                    v_shocked = max(leg.vol + vol_shock, 0.01)
                    val = leg.qty * leg.multiplier * _black76(
                        f_shocked, leg.strike, tenor, v_shocked, leg.right)
                per_leg.append(val)
                total += val
            return total, per_leg

        base_value, base_legs = book_value(0.0, 0.0)
        matrix: list[list[float]] = []
        worst = (0.0, 0.0, 0.0)
        for vs in _VOL_SHOCKS:
            row = []
            for ss in _SPOT_SHOCKS:
                val, _ = book_value(ss, vs)
                pnl = val - base_value
                row.append(round(pnl, 2))
                if pnl < worst[2]:
                    worst = (ss, vs, pnl)
            matrix.append(row)

        _, worst_legs = book_value(worst[0], worst[1])
        contributors = sorted(
            ({"leg": leg.label, "pnl": round(worst_legs[i] - base_legs[i], 2)}
             for i, leg in enumerate(legs)),
            key=lambda d: d["pnl"],
        )
        if real_book is not None:
            src = ("book + base vol + forward REAL (greeks/forwards, Steps 6/11); "
                   "spot×vol stress is parametric (Black-76 bump)")
        elif base.spot is not None and curve:
            src = ("base spot + forward curve REAL (Steps 5-6); illustrative book/IV "
                   "SYNTHETIC")
        else:
            src = "no real market_state/forwards on disk — run Steps 5-6 first"
        return ScenarioResult(
            source=src, underlying=underlying, spot=round(float(spot), 4),
            base_value=round(base_value, 2), spot_shocks=_SPOT_SHOCKS, vol_shocks=_VOL_SHOCKS,
            pnl_matrix=matrix, worst_spot_shock=worst[0], worst_vol_shock=worst[1],
            worst_pnl=round(worst[2], 2), contributors=contributors, book=legs,
            forward_curve=curve,
        )
