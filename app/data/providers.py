"""Connectique: bind the operator console to the preliminary backend.

Real where the backend exists (Step 1 connectivity evidence, Step 2 universe),
synthetic only where it does not yet (Step 8 IV solver, Step 9 surface fit).
The synthetic surface is still anchored on the *real* strikes and forward, and
is explicitly labeled so no fallback is hidden.
"""
from __future__ import annotations

import datetime as dt
import glob
import hashlib
import json
import math
from functools import lru_cache
from pathlib import Path

import app._paths  # noqa: F401  side-effect: put src/ on sys.path
import numpy as np

from utils.config import load_config
from universe.store import UniverseStore
from storage.parquet_store import ParquetStore

from app.data.contracts import (
    HealthMetrics,
    SurfacePoint,
    SurfaceSlice,
    UniverseSummary,
)


class AppProvider:
    """Single entry point used by routes. Holds backend handles + fallbacks."""

    def __init__(self) -> None:
        self._cfg = load_config()
        self._store = UniverseStore(self._cfg.environment.data_dir)
        self._log_dir = self._cfg.environment.log_dir
        self._pq = ParquetStore(self._cfg.environment.data_dir)

    # ----- backend discovery helpers ------------------------------------
    def latest_session_date(self) -> str | None:
        root = self._store.root
        if not root.exists():
            return None
        dates = sorted(p.name for p in root.iterdir() if p.is_dir())
        return dates[-1] if dates else None

    def _latest_bootstrap(self) -> dict | None:
        files = sorted(glob.glob(str(self._log_dir / "bootstrap_*.json")))
        if not files:
            return None
        try:
            return json.loads(Path(files[-1]).read_text()) | {"_file": Path(files[-1]).name}
        except (OSError, json.JSONDecodeError):
            return None

    @lru_cache(maxsize=4)
    def _options_df(self, session_date: str):
        return self._store.load_options(session_date)

    # ----- Vue #1: System health (REAL) ---------------------------------
    def get_health(self) -> HealthMetrics:
        ev = self._latest_bootstrap()
        if ev is None:
            return HealthMetrics(
                source="none", environment=self._cfg.environment.name,
                connected=False, connection_state="DISCONNECTED",
                server_time_utc=None, clock_skew_sec=None, market_data_type=None,
                sample_symbol=None, sample_market_price=None, evidence_time_utc=None,
            )
        rc = ev.get("resolved_contract") or {}
        return HealthMetrics(
            source=f"artifact:{ev.get('_file')}",
            environment=ev.get("environment"),
            connected=bool(ev.get("connected")),
            connection_state=ev.get("connection_state", "UNKNOWN"),
            server_time_utc=ev.get("server_time_utc"),
            clock_skew_sec=ev.get("clock_skew_sec"),
            market_data_type=ev.get("market_data_type"),
            sample_symbol=rc.get("symbol"),
            sample_market_price=ev.get("sample_market_price"),
            evidence_time_utc=ev.get("timestamp_utc"),
        )

    # ----- Vue #1bis: Universe summary (REAL) ---------------------------
    def get_universe_summary(self, session_date: str | None = None) -> UniverseSummary:
        sd = session_date or self.latest_session_date()
        if sd is None:
            return UniverseSummary("none", None, None, None, 0, 0, [])
        manifest = self._store.load_manifest(sd)
        try:
            unders = [
                {"canonical_key": u.canonical_key, "symbol": u.symbol,
                 "sec_type": u.sec_type, "currency": u.currency,
                 "exchange": u.exchange, "listing_status": u.listing_status}
                for u in self._store.load_underlyings(sd)
            ]
        except FileNotFoundError:
            unders = []
        return UniverseSummary(
            source="backend", session_date=sd,
            config_fingerprint=manifest.get("config_fingerprint"),
            generated_utc=manifest.get("generated_utc"),
            underlying_count=manifest.get("underlying_count", len(unders)),
            option_count=manifest.get("option_count", 0),
            underlyings=unders,
        )

    # ----- Vue #3: Surface explorer (REAL strikes, SYNTHETIC iv) --------
    def list_expiries(self, underlying: str, session_date: str | None = None) -> list[str]:
        sd = session_date or self.latest_session_date()
        if sd is None:
            return []
        df = self._options_df(sd)
        sub = df[df["underlying_symbol"] == underlying]
        return sorted({str(x) for x in sub["expiry"]})

    def _forward_anchor(self, underlying: str, strikes: list[float]) -> float:
        h = self.get_health()
        if h.sample_symbol == underlying and h.sample_market_price:
            return float(h.sample_market_price)
        return float(strikes[len(strikes) // 2]) if strikes else float("nan")

    @staticmethod
    def _seed(underlying: str, expiry: str) -> int:
        return int(hashlib.sha256(f"{underlying}:{expiry}".encode()).hexdigest()[:8], 16)

    # ----- real surface (Steps 8-9) -------------------------------------
    def _pq_latest(self, dataset: str) -> str | None:
        root = self._pq._dataset_root(dataset)  # noqa: SLF001
        if not root.exists():
            return None
        dates = sorted(d.name.split("=")[1] for d in root.glob("trade_date=*"))
        return dates[-1] if dates else None

    def surface_expiries(self, underlying: str) -> list[str]:
        """Expiries that have a real fitted surface; falls back to the universe."""
        td = self._pq_latest("surface_grid")
        if td:
            g = self._pq.read("surface_grid", trade_date=td)
            if g is not None and not g.empty:
                sub = g[g["underlying"] == underlying]
                if not sub.empty:
                    return sorted({str(e) for e in sub["expiry"]})
        return self.list_expiries(underlying)

    def surface_params(self, underlying: str, expiry: str) -> dict | None:
        """Fitted surface parameters + arbitrage flag (real surface_params, Step 9)."""
        td = self._pq_latest("surface_params")
        if not td:
            return None
        sp = self._pq.read("surface_params", trade_date=td)
        if sp is None or sp.empty:
            return None
        sub = sp[(sp["underlying"] == underlying) & (sp["expiry"].astype(str) == expiry)]
        if sub.empty:
            return None
        out = {"model": str(sub["model"].iloc[0])}
        for _, r in sub.iterrows():
            out[str(r["param_name"])] = float(r["param_value"])
        return out

    def _real_surface_slice(self, underlying: str, expiry: str) -> SurfaceSlice | None:
        td = self._pq_latest("surface_grid")
        if not td:
            return None
        grid = self._pq.read("surface_grid", trade_date=td)
        if grid is None or grid.empty:
            return None
        g = grid[(grid["underlying"] == underlying) & (grid["expiry"].astype(str) == expiry)]
        if g.empty:
            return None
        g = g.sort_values("log_moneyness")
        tenor = float(g["ttm_years"].iloc[0])
        model = str(g["model"].iloc[0])
        fitted_k = [float(x) for x in g["log_moneyness"]]
        fitted_iv = [float(x) for x in g["iv"]]
        # observed solved IV points (real), if available
        points: list[SurfacePoint] = []
        forward = float("nan")
        td_iv = self._pq_latest("iv_points")
        if td_iv:
            iv = self._pq.read("iv_points", trade_date=td_iv)
            if iv is not None and not iv.empty:
                pts = iv[(iv["underlying"] == underlying) & (iv["expiry"].astype(str) == expiry)
                         & (iv["status"] == "solved")]
                if not pts.empty:
                    forward = float(pts["forward"].iloc[0])
                    for _, r in pts.sort_values("strike").iterrows():
                        k = float(r["moneyness"])
                        points.append(SurfacePoint(float(r["strike"]), k, float(r["iv"]),
                                                   float(r["iv"]) ** 2 * tenor))
        if forward != forward and points:  # noqa: PLR0124
            forward = points[len(points) // 2].strike
        return SurfaceSlice(
            source=f"REAL — IV solver (Step 8) + surface fit (Step 9), model={model}",
            real_universe=True, underlying=underlying, expiry=expiry, session_date=td,
            forward=forward, tenor_years=tenor, points=points,
            fitted_k=fitted_k, fitted_iv=fitted_iv,
        )

    def get_surface_slice(self, underlying: str, expiry: str,
                          session_date: str | None = None) -> SurfaceSlice:
        real = self._real_surface_slice(underlying, expiry)
        if real is not None:
            return real
        sd = session_date or self.latest_session_date()
        if sd is None:
            raise FileNotFoundError("No backend universe session available")
        df = self._options_df(sd)
        sub = df[(df["underlying_symbol"] == underlying) & (df["expiry"].astype(str) == expiry)]
        if sub.empty:
            raise KeyError(f"No options for {underlying} @ {expiry} in session {sd}")
        strikes = sorted({float(s) for s in sub["strike"]})
        forward = self._forward_anchor(underlying, strikes)
        tenor = max((dt.date.fromisoformat(expiry) - dt.date.fromisoformat(sd)).days, 1) / 365.0

        # Synthetic but deterministic smile in total-variance-friendly form.
        atm = 0.16 + 0.06 * math.sqrt(tenor)
        skew, curv = -0.09, 0.45
        rng = np.random.default_rng(self._seed(underlying, expiry))
        points: list[SurfacePoint] = []
        for k_strike in strikes:
            k = math.log(k_strike / forward)
            if abs(k) > 0.6:          # focus near the money for a readable slice
                continue
            iv_fit = atm + skew * k + curv * k * k
            iv = max(iv_fit + float(rng.normal(0.0, 0.004)), 0.01)
            points.append(SurfacePoint(k_strike, k, iv, iv * iv * tenor))

        ks = [p.log_moneyness for p in points]
        fitted_k = (
            list(np.linspace(min(ks), max(ks), 101)) if ks else []
        )
        fitted_iv = [atm + skew * k + curv * k * k for k in fitted_k]
        return SurfaceSlice(
            source=("synthetic smile — strikes & forward REAL (backend Step 2); "
                    "IV solver (Step 8) & surface fit (Step 9) PENDING"),
            real_universe=True, underlying=underlying, expiry=expiry, session_date=sd,
            forward=forward, tenor_years=tenor, points=points,
            fitted_k=fitted_k, fitted_iv=fitted_iv,
        )
