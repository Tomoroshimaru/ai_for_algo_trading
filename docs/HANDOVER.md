# Handover Walkthrough

> Step 16 (f). The junior engineer demonstrates operation end to end, unaided.
> This script IS the acceptance test for the whole platform.

The new engineer must perform and narrate each step:

1. **Set up the environment** (DEPLOYMENT s1)
   - `uv sync`, `cp .env.example .env`, fill IBKR settings, `uv run pytest -q` green.
   - Explain config precedence (env > .env > settings.toml).

2. **Run a connectivity smoke test** (RB-1)
   - `uv run python scripts/bootstrap_smoke_test.py` -> exit 0 + evidence JSON.
   - Explain what it proves (connect, clock skew, one contract, one snapshot) and
     that it places no order.

3. **Trigger a replay** (RB-5)
   - `replay_day(store, "<date>", code_version="v0.1.0")` on a stored day.
   - Explain determinism and `compare_replay_vs_live`.

4. **Read the QC report** (RB-6)
   - `summarize(val)` and `triage_view(val)`.
   - Interpret PASS/WARN/FAIL and read one `reason_code` aloud in plain English.

5. **Explain where to investigate a failed surface build** (RB-7)
   - Given a FAIL on an underlying/expiry, walk the chain: reason_code ->
     iv_points (solved?) -> forward_diagnostics (parity residual?) ->
     market_state (stale/thin?) -> surface_params (RMSE) -> cross-check
     ops_metrics + alerts on the same correlation_id.

6. **Show system health & safe restart** (RB-8/RB-9)
   - `health_summary(...)`, `last_healthy_runs(...)`, `backlog(...)`.
   - Relaunch a SUCCEEDED job and show it is SKIPPED (no duplication).

**Pass criteria:** completed without help from the original author, and the
engineer can articulate *why* each step matters, not just the commands.
