# Known Limitations & Future Enhancements

> Step 16 (c). Honest record of what is NOT done, so nobody is surprised.

## Known limitations
1. **No committed scheduler.** `JobRunner` is the execution contract but cron/systemd/Airflow wiring is not in-repo; jobs are run manually or from tests. (DEPLOYMENT s3.)
2. **Collector heartbeat not emitted.** `collector_death` alert logic is tested but the live collector does not yet write the heartbeat row it consumes. (DEPLOYMENT s4.)
3. **`backlog()` takes an explicit date list**, not a trading calendar; wire it to the existing calendar at scheduler integration.
4. **Synthetic data paths.** Some console views and tests use synthetic vols, clearly labeled; not a substitute for a live-data soak test.
5. **Two residual pandas `FutureWarning`s** (concat) in the suite - cosmetic.
6. **No multi-account / multi-currency netting** beyond what `risk` aggregates expose today.

## Future enhancements
- Commit a sample crontab + systemd units; promote `JobRunner` to a thin daemon.
- Emit collector heartbeat -> close the `collector_death` loop end to end.
- Persist `ops_metrics`/`alerts` from the live path and back the console with them directly.
- Calendar-aware backlog and SLA timers.
- Expand QC anomaly detection with seasonality-aware baselines.
