"""Canonical instrument schema (Roadmap Step 2a).

The canonical key is broker-independent and stable across sessions; the broker
``conId`` is treated as an external foreign key, never as the sole identity
(cours.txt junior note). Multiplier and currency are always populated.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


def underlying_key(symbol: str, sec_type: str, currency: str) -> str:
    """Stable canonical key for an underlying, independent of broker session."""
    return f"{sec_type}:{symbol}:{currency}".upper()


def option_key(
    underlying_symbol: str,
    currency: str,
    expiry: dt.date,
    strike: float,
    right: str,
) -> str:
    """Stable canonical key for an option contract."""
    return (
        f"OPT:{underlying_symbol}:{currency}:"
        f"{expiry:%Y%m%d}:{strike:g}:{right}"
    ).upper()


@dataclass(frozen=True)
class UnderlyingInstrument:
    canonical_key: str
    symbol: str
    sec_type: str            # STK | IND
    currency: str
    exchange: str            # routing exchange (e.g. SMART)
    primary_exchange: str | None
    con_id: int | None       # broker foreign key
    multiplier: float        # 1.0 for cash equities / indices
    listing_status: str      # ACTIVE | INACTIVE


@dataclass(frozen=True)
class OptionInstrument:
    canonical_key: str
    underlying_symbol: str
    sec_type: str            # OPT
    currency: str
    exchange: str
    expiry: dt.date          # normalized date
    strike: float            # numeric
    right: str               # C | P
    multiplier: float
    trading_class: str
    con_id: int | None
