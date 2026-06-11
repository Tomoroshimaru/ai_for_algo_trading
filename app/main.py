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

APP_DIR = Path(__file__).resolve().parent
app = FastAPI(title="Volatility Infra — Operator Console")
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
provider = AppProvider()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {
        "health": provider.get_health(),
        "universe": provider.get_universe_summary(),
    })


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
    expiries = provider.list_expiries(sym)
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
