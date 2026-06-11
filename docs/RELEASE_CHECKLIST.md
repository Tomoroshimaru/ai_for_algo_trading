# Release Checklist

> Step 16 (b). Run top to bottom before tagging a release.

- [ ] `uv run pytest -q` green (full offline suite).
- [ ] `uv run python scripts/bootstrap_smoke_test.py` exits 0 against a live gateway (or documented why skipped).
- [ ] Replay determinism check on a known day (RB-5) matches the prior baseline.
- [ ] No interface-contract change unless intended; if changed, `schema_version` bumped and `INTERFACE_CONTRACTS.md` regenerated + CHANGE_MANAGEMENT followed.
- [ ] Thresholds/config changes reviewed and recorded (CHANGE_MANAGEMENT).
- [ ] No secrets in the diff (`.env` is gitignored; example only).
- [ ] Version bumped in `pyproject.toml`.
- [ ] Docs updated: affected module README + runbook.
- [ ] LIMITATIONS.md reflects any new known gap.
- [ ] Tag `vMAJOR.MINOR.PATCH`; record the `code_version` used by replay.
