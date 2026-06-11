# Ownership & Support Model

> Step 16 (d). Who owns what, and what operators can expect.

## Ownership boundaries (by package)
| Package | Responsibility | Owner |
|---|---|---|
| `connectivity` | IBKR session, reconnect, heartbeats | _TBD_ |
| `universe` | contract resolution, option-chain discovery | _TBD_ |
| `ingestion`, `snapshots` | raw capture, normalized market-state | _TBD_ |
| `forward`, `iv`, `surface`, `pricing`, `risk`, `scenario` | quant analytics | _TBD_ |
| `qc`, `validation` | QC framework, reason codes, anomalies | _TBD_ |
| `orchestration`, `storage`, `replay` | jobs, ledger, datasets, backfill | _TBD_ |
| `app` | operator console | _TBD_ |

_Fill owners at handover; until then the original author is the fallback._

## Support expectations
- **Severity routing** (from `alerts.py`): CRITICAL -> page (collector death, missing partitions, >=3 QC fails); WARN -> slack (elevated failure rate, isolated QC fails); INFO -> email.
- **First responder** uses RUNBOOKS RB-6/RB-7 to triage before escalating.
- **Detection SLO**: a collector/analytics failure is detectable within the heartbeat timeout (default 120s) + scheduler interval; documented, not yet measured in production (see LIMITATIONS).
- **Data corruption is structurally prevented** by idempotent partition writes + the job ledger; restart is always safe (RB-8).
