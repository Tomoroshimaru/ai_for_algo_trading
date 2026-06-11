"""Historical reconstruction & replay driver (Step 13).

Replays historical days through the SAME pipeline as live (pipeline.daily.run_day)
and archives derived analytics in versioned partitions, so a newer code version
never silently overwrites older historical analytics (task d). Missing raw/snapshot
partitions are flagged rather than masked by interpolation (task c / acceptance).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pandas as pd

from pipeline.daily import run_day, RunResult, PipelineParams
from storage.parquet_store import ParquetStore

# RunResult attribute -> dataset name (only non-empty frames are persisted).
_OUTPUTS = {
    "forwards": "forwards",
    "forward_diagnostics": "forward_diagnostics",
    "iv_points": "iv_points",
    "surface_params": "surface_params",
    "surface_grid": "surface_grid",
    "risk_line": "greeks",
    "risk_aggregates": "risk_aggregates",
    "risk_recon": "risk_recon",
}


@dataclass
class ReplayDayResult:
    trade_date: str
    present: bool          # raw/snapshot partition existed
    partial_data: bool     # present but incomplete (no options / no IV solved)
    n_options: int
    n_iv_solved: int
    status: str            # OK | PARTIAL | MISSING
    result: RunResult | None = None


def detect_partitions(store: ParquetStore, dates: list[str],
                      dataset: str = "market_state") -> dict[str, bool]:
    """Map each date -> whether its input partition exists & is non-empty (task c)."""
    return {d: len(store.read(dataset, trade_date=d)) > 0 for d in dates}


def replay_day(store: ParquetStore, trade_date: str, code_version: str,
               session_id: str, *, positions: pd.DataFrame | None = None,
               broker_greeks: pd.DataFrame | None = None,
               params: PipelineParams = PipelineParams(),
               persist: bool = True) -> ReplayDayResult:
    """Replay a single historical day end-to-end and (optionally) archive outputs."""
    snap = store.read("market_state", trade_date=trade_date)
    if len(snap) == 0:
        return ReplayDayResult(trade_date, False, True, 0, 0, "MISSING")
    res = run_day(snap, session_id, positions=positions,
                  broker_greeks=broker_greeks, params=params)
    n_opt = res.diagnostics.get("n_options", 0)
    n_solved = res.diagnostics.get("n_iv_solved", 0)
    partial = (n_opt == 0) or (n_solved == 0)
    if persist:
        _persist(store, res, trade_date, code_version)
    status = "PARTIAL" if partial else "OK"
    return ReplayDayResult(trade_date, True, partial, n_opt, n_solved, status, res)


def _persist(store: ParquetStore, res: RunResult, trade_date: str, code_version: str) -> None:
    for attr, dataset in _OUTPUTS.items():
        df = getattr(res, attr, None)
        if df is None or len(df) == 0:
            continue
        for und, g in df.groupby("underlying"):
            store.write_partition(dataset, trade_date, str(und), g,
                                  code_version=code_version)


def replay_range(store: ParquetStore, dates: list[str], code_version: str,
                 session_id: str, *, positions: pd.DataFrame | None = None,
                 params: PipelineParams = PipelineParams(),
                 persist: bool = True) -> tuple[list[ReplayDayResult], pd.DataFrame]:
    """Batch replay over a date range (task b). Returns (per-day results, QA report).

    The QA report is emitted as qc_results rows: one partition-presence check and
    one analytics-coverage check per date, so missing data is surfaced explicitly.
    """
    results: list[ReplayDayResult] = []
    qa_rows: list[dict] = []
    now = dt.datetime.now(dt.timezone.utc)
    for d in dates:
        r = replay_day(store, d, code_version, session_id,
                       positions=positions, params=params, persist=persist)
        results.append(r)
        qa_rows.append({"check_ts": now, "underlying": "_REPLAY",
                        "check_name": "replay_partition_present",
                        "target": d, "status": "PASS" if r.present else "FAIL",
                        "detail": f"code_version={code_version}"})
        qa_rows.append({"check_ts": now, "underlying": "_REPLAY",
                        "check_name": "replay_analytics_coverage",
                        "target": d,
                        "status": "PASS" if r.status == "OK" else (
                            "WARN" if r.status == "PARTIAL" else "FAIL"),
                        "detail": f"n_options={r.n_options} n_iv_solved={r.n_iv_solved}"})
    qa = pd.DataFrame(qa_rows, columns=["check_ts", "underlying", "check_name",
                                        "target", "status", "detail"])
    return results, qa


def compare_replay_vs_live(store: ParquetStore, dataset: str, trade_date: str,
                           underlying: str, code_version: str, *,
                           key_cols: list[str], value_cols: list[str]) -> dict:
    """Compare replay vs live outputs on an overlapping date (task e / acceptance).

    Returns max absolute difference per value column on the joined keys, plus row
    counts. Same code version => differences should be ~0 (machine precision).
    """
    live = store.read(dataset, trade_date=trade_date, underlying=underlying)
    rep = store.read(dataset, trade_date=trade_date, underlying=underlying,
                     code_version=code_version)
    out = {"dataset": dataset, "trade_date": trade_date, "underlying": underlying,
           "n_live": int(len(live)), "n_replay": int(len(rep)),
           "max_abs_diff": {}, "aligned_rows": 0}
    if len(live) == 0 or len(rep) == 0:
        return out
    merged = live.merge(rep, on=key_cols, suffixes=("_live", "_rep"))
    out["aligned_rows"] = int(len(merged))
    for c in value_cols:
        lc, rc = f"{c}_live", f"{c}_rep"
        if lc in merged and rc in merged:
            out["max_abs_diff"][c] = float((merged[lc] - merged[rc]).abs().max())
    return out
