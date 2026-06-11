"""QC / triage connectique (roadmap Step 14) — REAL data.

Reads the real ``qc_results`` and ``forward_diagnostics`` datasets from the
Parquet warehouse. FINAL rows give the per-instrument verdict; non-FINAL,
non-USABLE rows are the triage queue. Nothing is synthetic; an empty warehouse
yields an empty, clearly-labeled report.
"""
from __future__ import annotations

import app._paths  # noqa: F401

from utils.config import load_config
from storage.parquet_store import ParquetStore

from app.data.contracts import ParityOutlier, QCCheck, QCReport

_RANK = {"REJECT": 0, "CAUTION": 1, "USABLE": 2}


class QCProvider:
    def __init__(self) -> None:
        self._cfg = load_config()
        self._pq = ParquetStore(self._cfg.environment.data_dir)

    def _latest_trade_date(self, dataset: str) -> str | None:
        root = self._pq._dataset_root(dataset)  # noqa: SLF001
        if not root.exists():
            return None
        dates = sorted(p.name.split("=")[1] for p in root.glob("trade_date=*"))
        return dates[-1] if dates else None

    def get_report(self, trade_date: str | None = None) -> QCReport:
        td = trade_date or self._latest_trade_date("qc_results")
        qc = self._pq.read("qc_results", trade_date=td) if td else None
        if qc is None or qc.empty:
            return QCReport(source="none", trade_date=None)

        finals = qc[qc["check_name"] == "FINAL"]
        usable = int((finals["status"] == "USABLE").sum())
        caution = int((finals["status"] == "CAUTION").sum())
        reject = int((finals["status"] == "REJECT").sum())

        checks = qc[qc["check_name"] != "FINAL"]
        non_usable = checks[checks["status"] != "USABLE"]
        by_check = []
        for name, grp in non_usable.groupby("check_name"):
            by_check.append({
                "check": name,
                "caution": int((grp["status"] == "CAUTION").sum()),
                "reject": int((grp["status"] == "REJECT").sum()),
            })
        by_check.sort(key=lambda d: (d["reject"], d["caution"]), reverse=True)

        non_usable = non_usable.assign(_r=non_usable["status"].map(_RANK)).sort_values(
            ["_r", "check_name"]
        )
        failures = [
            QCCheck(
                check_ts=str(r["check_ts"]), check_name=str(r["check_name"]),
                target=str(r["target"]) if r["target"] == r["target"] else None,
                status=str(r["status"]), detail=str(r["detail"]),
            )
            for _, r in non_usable.iterrows()
        ]

        # parity outliers from the real forward diagnostics
        td_fd = self._latest_trade_date("forward_diagnostics")
        fd = self._pq.read("forward_diagnostics", trade_date=td_fd) if td_fd else None
        outliers = []
        if fd is not None and not fd.empty:
            bad = fd[fd["quality_label"] != "inlier"].sort_values(
                "residual", key=lambda s: s.abs(), ascending=False
            )
            outliers = [
                ParityOutlier(
                    expiry=str(r["expiry"]), strike=float(r["strike"]),
                    parity_forward=round(float(r["parity_forward"]), 4),
                    residual=round(float(r["residual"]), 4),
                    quality_label=str(r["quality_label"]),
                )
                for _, r in bad.iterrows()
            ]

        return QCReport(
            source=f"qc_results + forward_diagnostics @ {td}", trade_date=td,
            usable=usable, caution=caution, reject=reject, by_check=by_check,
            failures=failures, parity_outliers=outliers,
        )
