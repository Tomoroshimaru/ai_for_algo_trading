# Environment & Bootstrap Runbook (Step 1)

Provision a fresh machine to a working, connectivity-proven state.

## 1. Prerequisites
- [uv](https://docs.astral.sh/uv/) installed.
- IB Gateway **or** TWS installed, logged in to a **paper** account, API enabled
  (Configuration > API > Settings: "Enable ActiveX and Socket Clients").

## 2. Provision the Python environment
```bash
uv sync            # creates .venv (Python 3.12) from uv.lock (reproducible)
```

## 3. Configure
```bash
cp .env.example .env
# edit .env -> set IBKR_ACCOUNT (e.g. DU1234567) and adjust IBKR_PORT if needed
```
Port convention: IB Gateway paper = `4002`, live = `4001`; TWS paper = `7497`, live = `7496`.
Non-secret settings live in `configs/settings.toml`; secrets/env-specific values
are injected via environment variables and are never committed.

## 4. Prove connectivity (no orders placed)
```bash
uv run python scripts/bootstrap_smoke_test.py
```
Expected: a JSON evidence bundle printed and written to `artifacts/logs/bootstrap_*.json`
(session state, server time + clock skew, one resolved contract, one market-data sample).
If the Gateway is down the script fails loudly with exit code 1 and an actionable message.

## 5. Run the tests
```bash
uv run pytest -q
```

## Troubleshooting
| Symptom | Cause | Fix |
|---|---|---|
| `ConnectionError ... after N attempts` | Gateway/TWS not running or API disabled | Start it, enable API, check port |
| `clock_skew_sec` high / state DEGRADED | Host clock drift | Sync system time (NTP) |
| `sample_market_price` is NaN | No live market-data entitlement | Use `IBKR_MARKET_DATA_TYPE=3` (delayed) |
