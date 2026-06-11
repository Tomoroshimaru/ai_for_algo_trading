# Change Management

> Step 16 (e). Rules for the three change classes that can silently break
> downstream consumers or invalidate history.

## Schema changes (datasets / interface contracts)
- Source of truth: `src/storage/schemas.py`. Never edit a partition's columns by hand.
- **Additive** (new nullable column): allowed within a minor version; bump `schema_version`, regenerate `INTERFACE_CONTRACTS.md`, add a test.
- **Breaking** (rename/drop/retype, partition-key change): major version only. Provide a migration or replay plan to rebuild affected partitions; old partitions keep their old `schema_version` and must remain readable.
- Every PR touching schemas updates `tests/test_storage.py` (dataset roster).

## Threshold changes (QC / risk / alerting)
- All thresholds live in `configs/` - never hard-coded at call sites.
- A threshold change is a reviewed change: record old->new value, rationale, and the run_date it takes effect. QC `reason_code` semantics must not change meaning silently.
- Re-run QC (RB-6) on a recent day and confirm the PASS/WARN/FAIL shift is intended before merging.

## Model changes (forward / IV / surface / pricing / scenarios)
- A model change (new fit, new solver, new greek convention) requires:
  1. a `model` / `method` label change in the affected dataset so outputs are attributable;
  2. a replay comparison (`compare_replay_vs_live`) quantifying the diff vs the previous model on a baseline day;
  3. sign-off recorded in the PR.
- Never change a model and a threshold in the same PR - you lose attribution.
