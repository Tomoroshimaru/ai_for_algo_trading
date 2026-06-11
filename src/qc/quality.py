"""Quote normalization and quality control (Step 7).

The goal is not to maximize quote count but to keep quotes that are economically
meaningful and consistent with a tradable state. QC is deliberately NOT a
monolithic if-statement (cours.txt:716-718): it is a registry of small, named
checks, each producing its own reason code, so thresholds are tunable and every
rejection is auditable. The same quote under a fixed thresholds version is always
classified the same way (deterministic).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from forward.engine import parse_option_key, _weighted_median, _mad

# Severity ordering: worst wins when aggregating per-quote.
_RANK = {"USABLE": 0, "CAUTION": 1, "REJECT": 2}


@dataclass(frozen=True)
class QCParams:
    thresholds_version: str = "v1"
    max_spread_pct: float = 10.0     # wider -> caution
    reject_spread_pct: float = 50.0  # absurd spread -> reject
    max_age_sec: float = 5.0
    min_open_interest: float = 0.0   # below -> caution (0 disables if OI absent)
    min_volume: float = 0.0
    mad_k: float = 5.0               # parity-residual outlier multiplier
    parity_rel_floor: float = 1e-4


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str          # USABLE | CAUTION | REJECT
    reason_code: str     # "" when USABLE
    detail: str = ""


def _f(row: dict, k: str):
    v = row.get(k)
    return None if v is None or (isinstance(v, float) and pd.isna(v)) else v


# --- per-quote checks (each named, each independent) ----------------------- #
def chk_bid_positive(row, ctx, p) -> CheckResult:
    bid = _f(row, "bid")
    if bid is None:
        return CheckResult("bid_positive", "CAUTION", "INCOMPLETE_QUOTE", "missing bid")
    if bid <= 0:
        return CheckResult("bid_positive", "REJECT", "BID_NONPOSITIVE", f"bid={bid}")
    return CheckResult("bid_positive", "USABLE", "")


def chk_spread(row, ctx, p) -> CheckResult:
    sp = _f(row, "spread_pct")
    if sp is None:
        return CheckResult("spread", "CAUTION", "NO_SPREAD", "spread% unavailable")
    if sp > p.reject_spread_pct:
        return CheckResult("spread", "REJECT", "SPREAD_ABSURD", f"spread%={sp:.2f}")
    if sp > p.max_spread_pct:
        return CheckResult("spread", "CAUTION", "SPREAD_WIDE", f"spread%={sp:.2f}")
    return CheckResult("spread", "USABLE", "")


def chk_age(row, ctx, p) -> CheckResult:
    if bool(row.get("is_stale")):
        age = _f(row, "age_sec")
        return CheckResult("age", "CAUTION", "STALE_QUOTE", f"age_sec={age}")
    return CheckResult("age", "USABLE", "")


def chk_crossed_locked(row, ctx, p) -> CheckResult:
    bid, ask = _f(row, "bid"), _f(row, "ask")
    if bid is None or ask is None:
        return CheckResult("crossed_locked", "USABLE", "")
    if bid > ask:
        return CheckResult("crossed_locked", "REJECT", "CROSSED_MARKET", f"bid={bid}>ask={ask}")
    if bid == ask and bid > 0:
        return CheckResult("crossed_locked", "CAUTION", "LOCKED_MARKET", f"bid==ask=={bid}")
    return CheckResult("crossed_locked", "USABLE", "")


def chk_intrinsic(row, ctx, p) -> CheckResult:
    """Impossible price relative to (undiscounted) intrinsic value."""
    spot, strike, right = ctx.get("spot"), row.get("_strike"), row.get("_right")
    mid = _f(row, "mid")
    if spot is None or mid is None:
        return CheckResult("intrinsic", "USABLE", "")
    if right == "C":
        lower, upper = max(0.0, spot - strike), spot
    else:
        lower, upper = max(0.0, strike - spot), strike
    if mid < lower - 1e-9:
        return CheckResult("intrinsic", "REJECT", "BELOW_INTRINSIC",
                           f"mid={mid:.4f}<intrinsic={lower:.4f}")
    if mid > upper + 1e-9:
        return CheckResult("intrinsic", "REJECT", "ABOVE_BOUND",
                           f"mid={mid:.4f}>bound={upper:.4f}")
    return CheckResult("intrinsic", "USABLE", "")


def chk_liquidity(row, ctx, p) -> CheckResult:
    oi, vol = _f(row, "open_interest"), _f(row, "volume")
    if oi is not None and p.min_open_interest > 0 and oi < p.min_open_interest:
        return CheckResult("liquidity", "CAUTION", "LOW_OI", f"oi={oi}")
    if vol is not None and p.min_volume > 0 and vol < p.min_volume:
        return CheckResult("liquidity", "CAUTION", "LOW_VOLUME", f"vol={vol}")
    return CheckResult("liquidity", "USABLE", "")


PER_QUOTE_CHECKS = [
    chk_bid_positive, chk_spread, chk_age, chk_crossed_locked,
    chk_intrinsic, chk_liquidity,
]


def _aggregate(results: list[CheckResult]) -> str:
    return max(results, key=lambda r: _RANK[r.status]).status


def run_qc(
    snapshot: pd.DataFrame, source_session_id: str, params: QCParams = QCParams()
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (filtered_quotes, qc_table). Rejects are dropped from filtered.

    The raw snapshot is left untouched by the caller, so both raw and filtered
    views remain available for audit (task f).
    """
    # spot per underlying from STK rows
    spot = {}
    options = []
    for _, r in snapshot.iterrows():
        key = str(r["instrument_key"])
        if key.startswith("STK:") and pd.notna(r.get("reference_price")):
            spot[r["underlying"]] = float(r["reference_price"])
        parsed = parse_option_key(key)
        if parsed is not None:
            d = r.to_dict()
            d["_underlying"], d["_expiry"], d["_strike"], d["_right"] = parsed
            options.append(d)

    qc_rows: list[dict] = []
    status_by_key: dict[str, str] = {}

    # 1) per-quote checks
    for row in options:
        ctx = {"spot": spot.get(row["_underlying"])}
        results = [chk(row, ctx, params) for chk in PER_QUOTE_CHECKS]
        for res in results:
            if res.status != "USABLE":
                qc_rows.append(_qc_row(row, res, params))
        status_by_key[row["instrument_key"]] = _aggregate(results)

    # 2) chain-level checks (need neighbors): monotonicity + parity residual
    for res_key, res in _chain_checks(options, spot, params):
        qc_rows.append(_qc_row_for_key(options, res_key, res, params))
        prev = status_by_key.get(res_key, "USABLE")
        status_by_key[res_key] = res.status if _RANK[res.status] > _RANK[prev] else prev

    # 3) emit FINAL decision per quote
    filtered_keys = []
    for row in options:
        final = status_by_key[row["instrument_key"]]
        qc_rows.append({
            "snapshot_ts": row["snapshot_ts"], "underlying": row["_underlying"],
            "check_name": "FINAL", "target": row["instrument_key"], "status": final,
            "detail": f"thresholds={params.thresholds_version}",
        })
        if final != "REJECT":
            filtered_keys.append(row["instrument_key"])

    qc_table = pd.DataFrame(qc_rows, columns=[
        "snapshot_ts", "underlying", "check_name", "target", "status", "detail",
    ]).rename(columns={"snapshot_ts": "check_ts"}).sort_values(
        ["target", "check_name"]).reset_index(drop=True)

    filtered = snapshot[snapshot["instrument_key"].isin(filtered_keys)].copy()
    fmap = {k: status_by_key[k] for k in filtered_keys}
    filtered["qc_status"] = filtered["instrument_key"].map(fmap)
    return filtered.reset_index(drop=True), qc_table


def _qc_row(row, res: CheckResult, p) -> dict:
    return {
        "snapshot_ts": row["snapshot_ts"], "underlying": row["_underlying"],
        "check_name": res.name, "target": row["instrument_key"],
        "status": res.status, "detail": f"{res.reason_code}: {res.detail}",
    }


def _qc_row_for_key(options, key, res: CheckResult, p) -> dict:
    row = next(o for o in options if o["instrument_key"] == key)
    return _qc_row(row, res, p)


def _chain_checks(options, spot, p):
    """Yield (instrument_key, CheckResult) for monotonicity and parity outliers."""
    # monotonicity per (underlying, expiry, right)
    groups: dict[tuple, list] = {}
    for o in options:
        groups.setdefault((o["_underlying"], o["_expiry"], o["_right"]), []).append(o)
    for (und, exp, right), legs in groups.items():
        legs = sorted(legs, key=lambda x: x["_strike"])
        for i, o in enumerate(legs):
            mid = _f(o, "mid")
            if mid is None:
                continue
            lo = _f(legs[i - 1], "mid") if i > 0 else None
            hi = _f(legs[i + 1], "mid") if i < len(legs) - 1 else None
            bad = False
            if right == "C" and lo is not None and hi is not None and mid > lo and mid > hi:
                bad = True   # local bump: call should be non-increasing in K
            if right == "P" and lo is not None and hi is not None and mid < lo and mid < hi:
                bad = True   # local dip: put should be non-decreasing in K
            if bad:
                yield o["instrument_key"], CheckResult(
                    "monotonicity", "CAUTION", "MONOTONICITY_VIOLATION",
                    f"{right} mid={mid} vs neighbors {lo},{hi}")

    # parity residual outliers per (underlying, expiry)
    pairs: dict[tuple, dict] = {}
    for o in options:
        k = (o["_underlying"], o["_expiry"], o["_strike"])
        pairs.setdefault(k, {})[o["_right"]] = o
    by_mat: dict[tuple, list] = {}
    for (und, exp, strike), leg in pairs.items():
        if "C" in leg and "P" in leg:
            cm, pm = _f(leg["C"], "mid"), _f(leg["P"], "mid")
            if cm is not None and pm is not None:
                by_mat.setdefault((und, exp), []).append((strike, cm - pm + strike, leg))
    for (und, exp), items in by_mat.items():
        vals = [v for _, v, _ in items]
        if len(vals) < 3:
            continue
        center = _weighted_median(vals, [1.0] * len(vals))
        scale = max(_mad(vals, center), p.parity_rel_floor * abs(center))
        thr = p.mad_k * scale
        for strike, v, leg in items:
            if abs(v - center) > thr:
                for right in ("C", "P"):
                    yield leg[right]["instrument_key"], CheckResult(
                        "parity_outlier", "REJECT", "PARITY_OUTLIER",
                        f"parityF={v:.4f} vs {center:.4f} thr={thr:.4f}")
