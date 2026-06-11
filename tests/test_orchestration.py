"""Step 15 - orchestration, logging, observability (offline)."""
import datetime as dt
import tempfile

import pandas as pd
import pytest

from storage.parquet_store import ParquetStore
from orchestration.runner import Job, JobRunner
from orchestration.logging_ctx import (
    new_correlation_id, correlation_id_from_session, StructuredLogger,
)
from orchestration.alerts import evaluate_alerts
from orchestration.health import last_healthy_runs, backlog, health_summary
from orchestration.metrics import compute_run_metrics
from pipeline.daily import run_day, RunResult

UTC = dt.timezone.utc
DATE = "2026-06-11"


@pytest.fixture
def store():
    return ParquetStore(tempfile.mkdtemp())


# ---- correlation IDs (task b) ----------------------------------------------
def test_correlation_id_links_session_to_jobs():
    assert correlation_id_from_session("abc") == "sess-abc"
    assert new_correlation_id("qc").startswith("qc-")
    rec = StructuredLogger("t", correlation_id="sess-abc", job_name="qc").info("e", n=3)
    assert rec["correlation_id"] == "sess-abc" and rec["event"] == "e" and rec["n"] == 3


# ---- job runner: success + ledger ------------------------------------------
def test_job_success_writes_ledger(store):
    runner = JobRunner(store)
    job = Job("qc", lambda **k: 42)
    res = runner.run(job, DATE)
    assert res.status == "SUCCEEDED" and res.rows_out == 42 and res.attempt == 1
    led = store.read("job_runs", trade_date=DATE, underlying="qc")
    assert len(led) == 1 and led.iloc[0]["status"] == "SUCCEEDED"
    assert led.iloc[0]["correlation_id"] == res.correlation_id


# ---- retries (task a) ------------------------------------------------------
def test_job_retries_then_succeeds(store):
    calls = {"n": 0}
    def flaky(**k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient")
        return 7
    res = JobRunner(store).run(Job("analytics", flaky, max_retries=2), DATE)
    assert res.status == "SUCCEEDED" and res.attempt == 3
    led = store.read("job_runs", trade_date=DATE, underlying="analytics")
    assert list(led.sort_values("attempt")["status"]) == ["FAILED", "FAILED", "SUCCEEDED"]


def test_job_exhausts_retries_and_fails(store):
    res = JobRunner(store).run(Job("x", lambda **k: (_ for _ in ()).throw(ValueError("boom")),
                                   max_retries=1), DATE)
    assert res.status == "FAILED" and "boom" in res.error
    led = store.read("job_runs", trade_date=DATE, underlying="x")
    assert len(led) == 2 and (led["status"] == "FAILED").all()


# ---- idempotent restart (task e / acceptance) ------------------------------
def _metric_rows(n):
    now = dt.datetime.now(UTC)
    return pd.DataFrame([{"metric_ts": now, "run_date": DATE, "scope": "system",
                          "metric_name": "quote_count", "value": float(i),
                          "correlation_id": "c", "detail": None} for i in range(n)])


def test_restart_does_not_duplicate_outputs(store):
    """A succeeded job, re-run, is SKIPPED -> no double execution / duplication."""
    side = {"runs": 0}
    def job_fn(**k):
        side["runs"] += 1
        store.write_partition("ops_metrics", DATE, "SPY", _metric_rows(1))
        return 1
    runner = JobRunner(store)
    job = Job("analytics", job_fn)
    r1 = runner.run(job, DATE)
    r2 = runner.run(job, DATE)            # restart
    assert r1.status == "SUCCEEDED" and r2.status == "SKIPPED"
    assert side["runs"] == 1              # executed exactly once
    assert len(store.read("ops_metrics", trade_date=DATE, underlying="SPY")) == 1


def test_force_rerun_overwrites_not_duplicates(store):
    def job_fn(**k):
        store.write_partition("ops_metrics", DATE, "SPY", _metric_rows(2))
        return 2
    runner = JobRunner(store)
    runner.run(Job("analytics", job_fn), DATE)
    runner.run(Job("analytics", job_fn), DATE, force=True)   # forced re-exec
    # single-partition overwrite => still 2 rows, not 4
    assert len(store.read("ops_metrics", trade_date=DATE, underlying="SPY")) == 2


# ---- alerts (task d) -------------------------------------------------------
def test_collector_death_detected_within_interval():
    now = dt.datetime(2026, 6, 11, 16, 0, tzinfo=UTC)
    hb = now - dt.timedelta(seconds=300)
    a = evaluate_alerts(run_date=DATE, now=now, collector_last_heartbeat=hb,
                        heartbeat_timeout_sec=120)
    row = a[a["alert_name"] == "collector_death"].iloc[0]
    assert row["severity"] == "CRITICAL" and row["route"] == "page"
    # healthy heartbeat -> no alert
    ok = evaluate_alerts(run_date=DATE, now=now,
                         collector_last_heartbeat=now - dt.timedelta(seconds=10),
                         heartbeat_timeout_sec=120)
    assert len(ok) == 0


def test_missing_partitions_alert():
    a = evaluate_alerts(run_date=DATE,
                        expected_partitions=["SPY", "QQQ", "IWM"],
                        present_partitions=["SPY"],
                        collector_last_heartbeat=dt.datetime.now(UTC))
    row = a[a["alert_name"] == "missing_partitions"].iloc[0]
    assert "QQQ" in row["detail"] and row["severity"] == "CRITICAL"


def test_qc_fail_alert_severity_scales():
    val = pd.DataFrame([{"status": "FAIL", "underlying": "SPY", "target": "2026-12-18",
                         "reason_code": "COVERAGE_LOW"}] * 3)
    a = evaluate_alerts(run_date=DATE, collector_last_heartbeat=dt.datetime.now(UTC),
                        validation=val)
    row = a[a["alert_name"] == "qc_fail"].iloc[0]
    assert row["severity"] == "CRITICAL" and "COVERAGE_LOW" in row["detail"]


def test_elevated_failure_rate_alert():
    led = pd.DataFrame([
        {"job_name": "a", "attempt": 1, "status": "FAILED"},
        {"job_name": "b", "attempt": 1, "status": "SUCCEEDED"},
    ])
    a = evaluate_alerts(run_date=DATE, collector_last_heartbeat=dt.datetime.now(UTC),
                        job_ledger=led, failure_rate_warn=0.25)
    assert (a["alert_name"] == "elevated_failure_rate").any()


# ---- health / dashboard (task f / acceptance) ------------------------------
def test_last_healthy_run_and_backlog(store):
    runner = JobRunner(store)
    runner.run(Job("qc", lambda **k: 1), "2026-06-10")
    runner.run(Job("qc", lambda **k: 1), DATE)
    led = pd.concat([store.read("job_runs", trade_date=d, underlying="qc")
                     for d in ("2026-06-10", DATE)], ignore_index=True)
    lh = last_healthy_runs(led)
    assert lh.iloc[0]["job_name"] == "qc" and lh.iloc[0]["run_date"] == DATE
    bk = backlog(led, "qc", ["2026-06-09", "2026-06-10", DATE])
    assert bk == ["2026-06-09"]          # the only date with no healthy run


def test_health_summary_answers_four_questions():
    spot = 100.0; F = 105.0
    import math
    Tt = 0.5
    def vol(K): k = math.log(K / F); return 0.20 + 0.5 * k * k
    from iv.inversion import black76_price
    rows = [{"snapshot_ts": dt.datetime(2026,6,11,16,tzinfo=UTC), "underlying": "SPY",
             "instrument_key": "STK:SPY:USD", "bid": 99.9, "ask": 100.1, "last": 100,
             "mid": 100, "spread_pct": .2, "reference_price": 100, "reference_type": "last",
             "is_stale": False, "is_market_open": True, "age_sec": 1.0,
             "source_session_id": "s1"}]
    for K in (90, 95, 100, 105, 110):
        for r in ("C", "P"):
            px = black76_price(F, K, Tt, vol(K), r)
            rows.append({**rows[0], "instrument_key": f"OPT:SPY:20261218:{K}:{r}",
                         "bid": px - .1, "ask": px + .1, "last": px, "mid": px,
                         "reference_price": None})
    ms = pd.DataFrame(rows)
    res = run_day(ms, "s1")
    m = compute_run_metrics(res, run_date=DATE, correlation_id="sess-s1", market_state=ms,
                            scenario_runtime_sec=1.2)
    h = health_summary(run_date=DATE, metrics=m)
    assert h["data_flowing"] is True
    assert h["surfaces_building"] is True
    assert h["quote_count"] == 11
    assert "scenario_runtime_sec" in set(m["metric_name"])
