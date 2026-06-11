# Volatility Infrastructure Platform

IBKR-based, strategy-agnostic volatility infrastructure. Course project following the 16-step Industrial Roadmap (v4.0).

See `docs/environment.md` for setup.

## Operator console (frontend)

Local-only operator console (Uvicorn + FastAPI + Jinja2 HTML, Plotly charts).
It binds to the **real** preliminary backend: System Health reads the latest
Step 1 bootstrap evidence, and the Universe summary + Vol-Surface strikes/forward
come from the Step 2 instrument master. Implied vols are synthetic and clearly
labeled until the Step 8 solver / Step 9 surface fit are implemented.

```bash
uv sync --group frontend
uv run uvicorn app.main:app --reload    # http://127.0.0.1:8000
```

Routes: `/` overview, `/health`, `/surfaces` (+ JSON API `/api/health`,
`/api/surfaces/{underlying}/{expiry}` = directly-queryable surface grid).
