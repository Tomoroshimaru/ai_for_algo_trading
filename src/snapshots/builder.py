"""Pure market-state snapshot builder (Step 5).

Raw events in, snapshots out: no external calls, no clock reads, no I/O. This
purity makes replay deterministic and unit-testing trivial (cours.txt:660-664).
Given the same events and the same params, repeated runs produce identical rows.
Spot fallbacks are labeled (never hidden) and staleness is an explicit flag.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Iterable

import pandas as pd

from ingestion.events import MarketEvent

_QUOTE_FIELDS = ("bid", "ask", "last", "close")


@dataclass(frozen=True)
class SnapshotParams:
    max_age_sec: float = 5.0        # quote older than this at snapshot_ts -> stale
    max_spread_pct: float = 5.0     # mid considered unreliable beyond this spread
    market_open_utc: dt.time | None = None
    market_close_utc: dt.time | None = None


def parse_instrument(instrument_key: str) -> tuple[str, str | None]:
    """'STK:SPY:USD' -> ('SPY', None); 'OPT:SPY:20261218:500:C' -> ('SPY', '2026-12-18')."""
    parts = instrument_key.split(":")
    underlying = parts[1] if len(parts) > 1 else instrument_key
    if parts and parts[0] == "OPT" and len(parts) >= 3 and len(parts[2]) == 8:
        d = parts[2]
        return underlying, f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
    return underlying, None


def _mid_and_spread(bid: float | None, ask: float | None) -> tuple[float | None, float | None]:
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return None, None
    mid = (bid + ask) / 2.0
    spread_pct = (ask - bid) / mid * 100.0
    return mid, spread_pct


def choose_reference(
    bid: float | None, ask: float | None, last: float | None, close: float | None,
    params: SnapshotParams,
) -> tuple[float | None, str, float | None, float | None]:
    """Return (reference_price, reference_type, mid, spread_pct). Fallbacks labeled."""
    mid, spread_pct = _mid_and_spread(bid, ask)
    if mid is not None and spread_pct is not None and spread_pct <= params.max_spread_pct:
        return mid, "mid", mid, spread_pct
    if last is not None and last > 0:
        return last, "last", mid, spread_pct
    if close is not None and close > 0:
        return close, "close", mid, spread_pct
    if mid is not None:
        return mid, "mid_wide", mid, spread_pct   # wide-spread mid, explicitly labeled
    return None, "none", mid, spread_pct


def _latest_at(events: list[MarketEvent], snapshot_ts: dt.datetime) -> tuple[dict, dt.datetime | None]:
    """Most recent value per field at or before snapshot_ts (deterministic)."""
    values: dict[str, float] = {}
    freshest: dt.datetime | None = None
    # sort by collector_ts then field for a stable last-wins ordering
    for e in sorted(events, key=lambda x: (x.collector_ts, x.field)):
        if e.collector_ts > snapshot_ts:
            continue
        values[e.field] = e.value
        if freshest is None or e.collector_ts > freshest:
            freshest = e.collector_ts
    return values, freshest


def _is_open(snapshot_ts: dt.datetime, params: SnapshotParams) -> bool | None:
    if params.market_open_utc is None or params.market_close_utc is None:
        return None
    t = snapshot_ts.astimezone(dt.timezone.utc).time()
    return params.market_open_utc <= t <= params.market_close_utc


def build_snapshots(
    events: Iterable[MarketEvent],
    snapshot_times: list[dt.datetime],
    source_session_id: str,
    params: SnapshotParams = SnapshotParams(),
) -> pd.DataFrame:
    """Build market-state rows for every (snapshot_ts, instrument) pair."""
    by_instrument: dict[str, list[MarketEvent]] = {}
    for e in events:
        by_instrument.setdefault(e.instrument_key, []).append(e)

    rows: list[dict] = []
    for snapshot_ts in sorted(snapshot_times):
        is_open = _is_open(snapshot_ts, params)
        for instrument_key in sorted(by_instrument):
            vals, freshest = _latest_at(by_instrument[instrument_key], snapshot_ts)
            if freshest is None:
                continue   # no observation yet at this time -> instrument absent
            bid, ask = vals.get("bid"), vals.get("ask")
            last, close = vals.get("last"), vals.get("close")
            ref_price, ref_type, mid, spread_pct = choose_reference(
                bid, ask, last, close, params
            )
            age_sec = (snapshot_ts - freshest).total_seconds()
            underlying, _expiry = parse_instrument(instrument_key)
            rows.append({
                "snapshot_ts": snapshot_ts,
                "underlying": underlying,
                "instrument_key": instrument_key,
                "bid": bid, "ask": ask, "last": last, "mid": mid,
                "spread_pct": spread_pct,
                "reference_price": ref_price,
                "reference_type": ref_type,
                "is_stale": age_sec > params.max_age_sec,
                "is_market_open": is_open,
                "age_sec": age_sec,
                "source_session_id": source_session_id,
            })
    cols = [
        "snapshot_ts", "underlying", "instrument_key", "bid", "ask", "last", "mid",
        "spread_pct", "reference_price", "reference_type", "is_stale",
        "is_market_open", "age_sec", "source_session_id",
    ]
    return pd.DataFrame(rows, columns=cols)


def regular_grid(start: dt.datetime, end: dt.datetime, every_sec: float) -> list[dt.datetime]:
    """Deterministic snapshot trigger: a regular time grid (task a)."""
    out, t, step = [], start, dt.timedelta(seconds=every_sec)
    while t <= end:
        out.append(t)
        t += step
    return out


def compute_completeness(snapshots: pd.DataFrame) -> pd.DataFrame:
    """Completeness metrics per underlying and maturity (task f)."""
    if snapshots.empty:
        return pd.DataFrame(
            columns=["underlying", "expiry", "n_instruments", "n_obs", "n_fresh", "completeness"]
        )
    df = snapshots.copy()
    df["expiry"] = df["instrument_key"].map(lambda k: parse_instrument(k)[1] or "UNDERLYING")
    grp = df.groupby(["underlying", "expiry"], dropna=False)
    out = grp.agg(
        n_instruments=("instrument_key", "nunique"),
        n_obs=("instrument_key", "size"),            # instrument x snapshot rows
        n_fresh=("is_stale", lambda s: int((~s).sum())),
    ).reset_index()
    # completeness = fraction of (instrument x time) observations that are fresh [0,1]
    out["completeness"] = (out["n_fresh"] / out["n_obs"]).round(4)
    return out
