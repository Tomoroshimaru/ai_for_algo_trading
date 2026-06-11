# Volatility Infrastructure Platform

IBKR-based, strategy-agnostic volatility infrastructure. Course project
following the 16-step Industrial Roadmap (v4.0). Strategy-agnostic by design:
it builds and QCs vol surfaces, greeks, risk and scenarios; it does not trade.

## Quick start
```bash
uv sync
cp .env.example .env            # fill IBKR host/port/clientId
uv run pytest -q                # full offline suite
uv run python scripts/bootstrap_smoke_test.py   # live connectivity (needs gateway)
uv run uvicorn app.main:app --reload            # operator console :8000
```

## Architecture (data flow)
`connectivity` -> `ingestion`/`snapshots` (raw_events -> market_state) ->
`forward` -> `iv` -> `surface` -> `pricing`/`risk` -> `scenario`, with
`qc`/`validation` gating, `orchestration` running it (jobs, ledger, metrics,
alerts, health), `storage` persisting partitioned Parquet, and `replay`
re-deriving history deterministically.

## Documentation set (docs/)
| Doc | Purpose |
|---|---|
| `INTERFACE_CONTRACTS.md` | Frozen dataset schemas + public entrypoints (a) |
| `RUNBOOKS.md` | SOPs with concrete commands (RB-1..RB-10) (b) |
| `DEPLOYMENT.md` | Environment, gateway, scheduling, heartbeat, console (b) |
| `RELEASE_CHECKLIST.md` | Pre-release gate (b) |
| `LIMITATIONS.md` | Known gaps + future work (c) |
| `OWNERSHIP_SUPPORT.md` | Owners + support/escalation model (d) |
| `CHANGE_MANAGEMENT.md` | Schema / threshold / model change rules (e) |
| `HANDOVER.md` | End-to-end handover walkthrough (f) |
| `data_model.md` | Per-step design notes & datasets |
| `environment.md` | Detailed environment setup |

Each `src/<package>/` has its own `README.md`. Public functions carry docstrings.
