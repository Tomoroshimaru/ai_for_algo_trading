"""Volatility-infra operator console (Uvicorn + FastAPI + Jinja2 HTML).

Run locally:
    uvicorn app.main:app --reload

Pages render server-side HTML; ``/api/*`` endpoints expose the same data as
JSON (the roadmap's directly-queryable surface grid, Step 9).
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import app._paths  # noqa: F401  side-effect: src/ on sys.path
import plotly.graph_objects as go
import plotly.io as pio
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.data.providers import AppProvider
from app.data.observability import ObservabilityProvider
from app.data.scenario import ScenarioProvider
from app.data.risk import RiskProvider
from app.data.qc import QCProvider

APP_DIR = Path(__file__).resolve().parent
app = FastAPI(title="Volatility Infra — Operator Console")
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
provider = AppProvider()
obs = ObservabilityProvider()
scen = ScenarioProvider()
risk_provider = RiskProvider()
qc_provider = QCProvider()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"tiles": _control_tower()})


@app.get("/health", response_class=HTMLResponse)
def health_page(request: Request):
    return templates.TemplateResponse(request, "health.html", {
        "health": provider.get_health(),
        "universe": provider.get_universe_summary(),
    })


@app.get("/api/health")
def api_health():
    return JSONResponse(asdict(provider.get_health()))


@app.get("/surfaces", response_class=HTMLResponse)
def surfaces_page(request: Request, underlying: str | None = None, expiry: str | None = None):
    universe = provider.get_universe_summary()
    symbols = [u["symbol"] for u in universe.underlyings] or ([underlying] if underlying else [])
    sym = underlying or (symbols[0] if symbols else None)
    if sym is None:
        raise HTTPException(503, "No backend universe available yet (run Step 2 first)")
    expiries = provider.surface_expiries(sym)
    exp = expiry or (expiries[0] if expiries else None)
    if exp is None:
        raise HTTPException(404, f"No expiries for {sym}")
    sl = provider.get_surface_slice(sym, exp)
    return templates.TemplateResponse(request, "surface.html", {
        "symbols": symbols, "expiries": expiries,
        "sym": sym, "exp": exp, "slice": sl, "plot": _surface_div(sl),
    })


@app.get("/api/surfaces/{underlying}/{expiry}")
def api_surface(underlying: str, expiry: str):
    try:
        sl = provider.get_surface_slice(underlying, expiry)
    except (KeyError, FileNotFoundError) as exc:
        raise HTTPException(404, str(exc)) from exc
    return JSONResponse(asdict(sl))


def _surface_div(sl) -> str:
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=[p.log_moneyness for p in sl.points], y=[p.iv for p in sl.points],
        mode="markers", name="accepted IV points",
        marker=dict(size=6, color="#6ab7ff"),
    ))
    fig.add_trace(go.Scatter(
        x=sl.fitted_k, y=sl.fitted_iv, mode="lines", name="fitted slice",
        line=dict(color="#ffb000", width=2),
    ))
    fig.update_layout(
        template="plotly_dark", height=460,
        margin=dict(l=50, r=20, t=40, b=50),
        title=f"{sl.underlying} {sl.expiry} — IV smile (F={sl.forward:g}, T={sl.tenor_years:.3f}y)",
        xaxis_title="log-moneyness  k = ln(K/F)", yaxis_title="implied volatility",
        paper_bgcolor="#0d1117", plot_bgcolor="#0d1117",
    )
    return pio.to_html(fig, include_plotlyjs="cdn", full_html=False)


@app.get("/observability", response_class=HTMLResponse)
def observability_page(request: Request):
    snap = obs.get_snapshot()
    return templates.TemplateResponse(request, "observability.html", {"snap": snap})


@app.get("/api/observability")
def api_observability():
    return JSONResponse(asdict(obs.get_snapshot()))


@app.get("/surface3d", response_class=HTMLResponse)
def surface3d_page(request: Request, underlying: str | None = None):
    universe = provider.get_universe_summary()
    symbols = [u["symbol"] for u in universe.underlyings]
    sym = underlying or (symbols[0] if symbols else None)
    if sym is None:
        raise HTTPException(503, "No backend universe available yet")
    expiries = provider.surface_expiries(sym)[:10]
    plot, used, src = _surface3d_div(sym, expiries)
    return templates.TemplateResponse(request, "surface3d.html", {
        "symbols": symbols, "sym": sym, "used": used, "src": src, "plot": plot,
    })


def _surface3d_div(underlying: str, expiries: list[str]):
    import numpy as np
    slices = []
    for e in expiries:
        try:
            sl = provider.get_surface_slice(underlying, e)
        except (KeyError, FileNotFoundError):
            continue
        if sl.points:
            slices.append(sl)
    if len(slices) < 2:
        return "<p class=\'prov\'>Not enough maturities to build a 3D surface.</p>", 0, ""
    kmin = max(min(p.log_moneyness for p in s.points) for s in slices)
    kmax = min(max(p.log_moneyness for p in s.points) for s in slices)
    kg = np.linspace(kmin, kmax, 40)
    z, y = [], []
    for s in sorted(slices, key=lambda s: s.tenor_years):
        ks = np.array([p.log_moneyness for p in s.points])
        ivs = np.array([p.iv for p in s.points])
        order = np.argsort(ks)
        z.append(list(np.interp(kg, ks[order], ivs[order])))
        y.append(round(s.tenor_years, 4))
    fig = go.Figure(data=[go.Surface(x=list(kg), y=y, z=z, colorscale="Viridis",
                                     colorbar=dict(title="IV"))])
    fig.update_layout(
        template="plotly_dark", height=560, paper_bgcolor="#0d1117",
        margin=dict(l=0, r=0, t=40, b=0),
        title=f"{underlying} — implied-vol surface ({len(slices)} maturities)",
        scene=dict(xaxis_title="log-moneyness k", yaxis_title="tenor (years)",
                   zaxis_title="implied vol"),
    )
    return pio.to_html(fig, include_plotlyjs="cdn", full_html=False), len(slices), slices[0].source


@app.get("/scenario", response_class=HTMLResponse)
def scenario_page(request: Request, underlying: str | None = None):
    universe = provider.get_universe_summary()
    symbols = [u["symbol"] for u in universe.underlyings]
    sym = underlying or (symbols[0] if symbols else "SPY")
    res = scen.run_scenario(sym)
    heatmap = _scenario_heatmap_div(res)
    return templates.TemplateResponse(request, "scenario.html", {
        "symbols": symbols, "sym": sym, "res": res, "heatmap": heatmap,
    })


@app.get("/api/scenario/{underlying}")
def api_scenario(underlying: str):
    return JSONResponse(asdict(scen.run_scenario(underlying)))


def _scenario_heatmap_div(res) -> str:
    if not res.book:
        return "<p class=\'prov\'>No reliable forward maturities — cannot build a book yet.</p>"
    xs = [f"{s:+.0%}" for s in res.spot_shocks]
    ys = [f"{v:+.2f}" for v in res.vol_shocks]
    fig = go.Figure(data=[go.Heatmap(
        z=res.pnl_matrix, x=xs, y=ys, colorscale="RdYlGn", zmid=0,
        colorbar=dict(title="PnL"),
        hovertemplate="spot %{x}, vol %{y}<br>PnL %{z:,.0f}<extra></extra>",
    )])
    fig.update_layout(
        template="plotly_dark", height=420, paper_bgcolor="#0d1117",
        margin=dict(l=10, r=10, t=40, b=10),
        title=f"{res.underlying} stress grid — book PnL (worst {res.worst_pnl:,.0f})",
        xaxis_title="spot shock", yaxis_title="vol shock (abs pts)",
    )
    return pio.to_html(fig, include_plotlyjs="cdn", full_html=False)


@app.get("/risk", response_class=HTMLResponse)
def risk_page(request: Request, underlying: str | None = None):
    universe = provider.get_universe_summary()
    symbols = [u["symbol"] for u in universe.underlyings]
    sym = underlying or (symbols[0] if symbols else "SPY")
    return templates.TemplateResponse(request, "risk.html", {
        "symbols": symbols, "sym": sym, "r": risk_provider.get_risk(sym),
    })


@app.get("/api/risk/{underlying}")
def api_risk(underlying: str):
    return JSONResponse(asdict(risk_provider.get_risk(underlying)))


@app.get("/qc", response_class=HTMLResponse)
def qc_page(request: Request):
    rep = qc_provider.get_report()
    return templates.TemplateResponse(request, "qc.html", {
        "rep": rep, "chart": _qc_chart_div(rep),
    })


@app.get("/api/qc")
def api_qc():
    return JSONResponse(asdict(qc_provider.get_report()))


def _qc_chart_div(rep) -> str:
    if not rep.by_check:
        return "<p class=\'prov\'>No failing checks — all instruments USABLE.</p>"
    names = [d["check"] for d in rep.by_check]
    fig = go.Figure(data=[
        go.Bar(name="REJECT", x=names, y=[d["reject"] for d in rep.by_check], marker_color="#f85149"),
        go.Bar(name="CAUTION", x=names, y=[d["caution"] for d in rep.by_check], marker_color="#d29922"),
    ])
    fig.update_layout(
        barmode="stack", template="plotly_dark", height=340, paper_bgcolor="#0d1117",
        margin=dict(l=10, r=10, t=40, b=10), title="Failing checks by rule",
        xaxis_title="check", yaxis_title="count",
    )
    return pio.to_html(fig, include_plotlyjs="cdn", full_html=False)


def _control_tower() -> list[dict]:
    """Aggregate one status tile per view, with data provenance + a real metric.

    Each provider call is isolated so a single failure never breaks the hub.
    provenance: "real" | "partial" | "synthetic".
    """
    tiles: list[dict] = []

    def tile(name, href, provenance, headline, sub):
        tiles.append({"name": name, "href": href, "provenance": provenance,
                      "headline": headline, "sub": sub})

    try:
        h = provider.get_health()
        tile("Connectivity", "/health", "real",
             h.connection_state, f"source: {h.source}")
    except Exception as exc:  # noqa: BLE001
        tile("Connectivity", "/health", "real", "unavailable", str(exc)[:60])
    try:
        u = provider.get_universe_summary()
        tile("Universe", "/surfaces", "real",
             f"{u.underlying_count} underlyings / {u.option_count} options",
             f"session {u.session_date}")
    except Exception as exc:  # noqa: BLE001
        tile("Universe", "/surfaces", "real", "unavailable", str(exc)[:60])
    try:
        o = obs.get_snapshot()
        tile("Observability", "/observability", "real",
             f"{o.total_market_events} events / {o.total_errors} errors",
             f"{len(o.sessions)} sessions @ {o.trade_date}")
    except Exception as exc:  # noqa: BLE001
        tile("Observability", "/observability", "real", "unavailable", str(exc)[:60])
    try:
        q = qc_provider.get_report()
        tile("QC / Triage", "/qc", "real",
             f"{q.reject} reject / {q.caution} caution / {q.usable} usable",
             f"{len(q.failures)} failing checks @ {q.trade_date}")
    except Exception as exc:  # noqa: BLE001
        tile("QC / Triage", "/qc", "real", "unavailable", str(exc)[:60])
    try:
        u = provider.get_universe_summary()
        sym = u.underlyings[0]["symbol"] if u.underlyings else "SPY"
        n_exp = len(provider.surface_expiries(sym))
        tile("Vol Surface (2D/3D)", "/surface3d", "partial",
             f"{n_exp} maturities ({sym})", "strikes REAL / IV synthetic (Step 8)")
    except Exception as exc:  # noqa: BLE001
        tile("Vol Surface (2D/3D)", "/surface3d", "partial", "unavailable", str(exc)[:60])
    try:
        sc = scen.run_scenario("SPY")
        tile("Scenario / Stress", "/scenario", "partial",
             f"worst PnL {sc.worst_pnl:,.0f}",
             "spot+forward REAL / book+pricing synthetic")
    except Exception as exc:  # noqa: BLE001
        tile("Scenario / Stress", "/scenario", "partial", "unavailable", str(exc)[:60])
    try:
        rk = risk_provider.get_risk("SPY")
        tile("Risk & Greeks", "/risk", "partial",
             f"vega {rk.net_vega:,.0f} / theta {rk.net_theta:,.0f}",
             "spot+forward REAL / greeks synthetic (Step 11)")
    except Exception as exc:  # noqa: BLE001
        tile("Risk & Greeks", "/risk", "partial", "unavailable", str(exc)[:60])
    return tiles
