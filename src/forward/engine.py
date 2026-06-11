"""Pure forward & implied-carry engine (Step 6).

Given a market-state snapshot (underlying spot + option mids), compute a robust
put-call-parity forward per maturity and an implied carry diagnostic. The engine
is pure and deterministic: snapshot in, forwards + diagnostics out. Robustness
comes from liquidity weighting and MAD-based outlier rejection so a few bad
pairs cannot dominate the estimate (cours.txt:666-672).

Parity: C - P = DF * (F - K)  =>  F = K + (C - P) / DF, with DF = exp(-r*T)
(DF = 1 when no rate curve is supplied; this assumption is recorded in ``method``).
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class ForwardParams:
    n_strikes_atm: int = 8       # eligible call-put pairs nearest the money
    mad_k: float = 3.0           # outlier if |resid| > mad_k * MAD
    rate: float | None = None    # flat annual rate for DF; None => DF=1
    day_count: float = 365.0
    parity_rel_floor: float = 1e-4  # residual floor so MAD=0 still rejects outliers


def parse_option_key(key: str) -> tuple[str, str, float, str] | None:
    """'OPT:SPY:20261218:500:C' -> ('SPY','2026-12-18',500.0,'C'); else None."""
    parts = key.split(":")
    if len(parts) != 5 or parts[0] != "OPT":
        return None
    d = parts[2]
    expiry = f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
    return parts[1], expiry, float(parts[3]), parts[4].upper()


def _weighted_median(values: list[float], weights: list[float]) -> float:
    pairs = sorted(zip(values, weights))
    total = sum(w for _, w in pairs)
    if total <= 0:
        # fall back to plain median of values
        n = len(values)
        sv = sorted(values)
        return sv[n // 2] if n % 2 else (sv[n // 2 - 1] + sv[n // 2]) / 2
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= total / 2:
            return v
    return pairs[-1][0]


def _mad(values: list[float], center: float) -> float:
    devs = sorted(abs(v - center) for v in values)
    n = len(devs)
    return devs[n // 2] if n % 2 else (devs[n // 2 - 1] + devs[n // 2]) / 2


def _year_fraction(expiry: str, snapshot_ts: dt.datetime, day_count: float) -> float:
    exp = dt.date.fromisoformat(expiry)
    days = (exp - snapshot_ts.date()).days
    return days / day_count


def compute_forwards(
    snapshot: pd.DataFrame,
    source_session_id: str,
    params: ForwardParams = ForwardParams(),
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (forwards_df, diagnostics_df) for one or more (underlying, expiry)."""
    # spot per underlying from the STK rows' reference_price
    spot: dict[str, float] = {}
    for _, r in snapshot.iterrows():
        if str(r["instrument_key"]).startswith("STK:") and pd.notna(r["reference_price"]):
            spot[r["underlying"]] = float(r["reference_price"])

    # collect option legs keyed by (underlying, expiry, strike)
    legs: dict[tuple, dict] = {}
    for _, r in snapshot.iterrows():
        parsed = parse_option_key(str(r["instrument_key"]))
        if parsed is None:
            continue
        und, expiry, strike, right = parsed
        key = (und, expiry, strike)
        leg = legs.setdefault(key, {"snapshot_ts": r["snapshot_ts"]})
        leg[right] = {
            "mid": r["mid"], "spread_pct": r["spread_pct"], "is_stale": bool(r["is_stale"]),
        }

    fwd_rows: list[dict] = []
    diag_rows: list[dict] = []
    by_maturity: dict[tuple, list] = {}
    for (und, expiry, strike), leg in legs.items():
        by_maturity.setdefault((und, expiry, leg["snapshot_ts"]), []).append((strike, leg))

    for (und, expiry, snap_ts), strike_legs in by_maturity.items():
        s = spot.get(und)
        # near-the-money selection by |K - spot| (fallback: all) 
        if s is not None:
            strike_legs = sorted(strike_legs, key=lambda kl: abs(kl[0] - s))
        eligible = strike_legs[: params.n_strikes_atm]

        df_factor = 1.0
        if params.rate is not None:
            T = _year_fraction(expiry, snap_ts, params.day_count)
            df_factor = math.exp(-params.rate * T) if T > 0 else 1.0

        cands: list[dict] = []
        for strike, leg in eligible:
            call, put = leg.get("C"), leg.get("P")
            cm = call["mid"] if call else None
            pm = put["mid"] if put else None
            base = {"strike": strike, "call_mid": cm, "put_mid": pm}
            if call is None or put is None or pd.isna(cm) or pd.isna(pm):
                diag_rows.append({**base, "underlying": und, "expiry": expiry,
                                  "snapshot_ts": snap_ts, "parity_forward": None,
                                  "weight": 0.0, "residual": None,
                                  "quality_label": "incomplete",
                                  "source_session_id": source_session_id})
                continue
            parity_fwd = strike + (cm - pm) / df_factor
            stale = call["is_stale"] or put["is_stale"]
            cs = call["spread_pct"] if pd.notna(call["spread_pct"]) else 1e9
            ps = put["spread_pct"] if pd.notna(put["spread_pct"]) else 1e9
            weight = 0.0 if stale else 1.0 / (1.0 + cs + ps)
            cands.append({**base, "parity_forward": parity_fwd, "weight": weight,
                          "stale": stale})

        usable = [c for c in cands if c["weight"] > 0]
        if not usable:
            for c in cands:
                diag_rows.append({"underlying": und, "expiry": expiry, "snapshot_ts": snap_ts,
                                  "strike": c["strike"], "call_mid": c["call_mid"],
                                  "put_mid": c["put_mid"], "parity_forward": c["parity_forward"],
                                  "weight": c["weight"], "residual": None,
                                  "quality_label": "stale",
                                  "source_session_id": source_session_id})
            fwd_rows.append({"snapshot_ts": snap_ts, "underlying": und, "expiry": expiry,
                             "forward": float("nan"), "implied_carry": None,
                             "method": "parity_wmedian", "n_pairs": 0, "is_reliable": False,
                             "source_session_id": source_session_id})
            continue

        vals = [c["parity_forward"] for c in usable]
        wts = [c["weight"] for c in usable]
        center = _weighted_median(vals, wts)
        mad = _mad(vals, center)
        # MAD can be 0 when most pairs agree exactly; floor the scale on a
        # relative parity tolerance so a gross outlier is still rejected.
        scale = max(mad, params.parity_rel_floor * abs(center))
        thr = params.mad_k * scale

        inliers = []
        for c in usable:
            resid = c["parity_forward"] - center
            is_out = thr > 0 and abs(resid) > thr
            c["residual"] = resid
            c["label"] = "outlier" if is_out else "inlier"
            if not is_out:
                inliers.append(c)
        # final robust forward from inliers only
        f_vals = [c["parity_forward"] for c in inliers]
        f_wts = [c["weight"] for c in inliers]
        forward = _weighted_median(f_vals, f_wts)

        implied_carry = None
        if s is not None and s > 0:
            T = _year_fraction(expiry, snap_ts, params.day_count)
            if T > 0 and forward > 0:
                implied_carry = math.log(forward / s) / T
        is_reliable = len(inliers) >= 2

        fwd_rows.append({"snapshot_ts": snap_ts, "underlying": und, "expiry": expiry,
                         "forward": forward, "implied_carry": implied_carry,
                         "method": "parity_wmedian", "n_pairs": len(inliers),
                         "is_reliable": is_reliable,
                         "source_session_id": source_session_id})
        for c in cands:
            label = c.get("label") or ("stale" if c.get("stale") else "inlier")
            diag_rows.append({"underlying": und, "expiry": expiry, "snapshot_ts": snap_ts,
                              "strike": c["strike"], "call_mid": c["call_mid"],
                              "put_mid": c["put_mid"],
                              "parity_forward": c.get("parity_forward"),
                              "weight": c["weight"], "residual": c.get("residual"),
                              "quality_label": label,
                              "source_session_id": source_session_id})

    fwd_cols = ["snapshot_ts", "underlying", "expiry", "forward", "implied_carry",
                "method", "n_pairs", "is_reliable", "source_session_id"]
    diag_cols = ["snapshot_ts", "underlying", "expiry", "strike", "call_mid", "put_mid",
                 "parity_forward", "weight", "residual", "quality_label",
                 "source_session_id"]
    fwd_df = pd.DataFrame(fwd_rows, columns=fwd_cols).sort_values(
        ["underlying", "expiry"]).reset_index(drop=True)
    diag_df = pd.DataFrame(diag_rows, columns=diag_cols).sort_values(
        ["underlying", "expiry", "strike"]).reset_index(drop=True)
    return fwd_df, diag_df
