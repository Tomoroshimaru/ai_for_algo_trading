"""Instrument master: contract resolution + data-quality checks (Step 2b/f).

Resolves human-readable requests into broker contracts, builds the canonical
representation, and enforces fail-loud data quality. An unresolved or ambiguous
contract is surfaced as an explicit exception with diagnostics, never silently
skipped (cours.txt:524 acceptance criteria).
"""
from __future__ import annotations

from typing import Any

from connectivity.session import IBSession
from universe.schema import UnderlyingInstrument, underlying_key

_RAW_ATTRS = (
    "conId", "symbol", "secType", "currency", "exchange", "primaryExchange",
    "localSymbol", "tradingClass", "multiplier", "lastTradeDateOrContractMonth",
    "strike", "right",
)


class InstrumentResolutionError(RuntimeError):
    """Raised when a contract cannot be resolved or is ambiguous."""

    def __init__(self, symbol: str, diagnostics: dict[str, Any]) -> None:
        super().__init__(f"Could not resolve {symbol!r}: {diagnostics}")
        self.symbol = symbol
        self.diagnostics = diagnostics


class DataQualityError(RuntimeError):
    """Raised when a resolved instrument fails a data-quality invariant."""


def contract_to_raw(contract: Any) -> dict[str, Any]:
    """Capture the broker payload as evidence (cours.txt junior note)."""
    return {attr: getattr(contract, attr, None) for attr in _RAW_ATTRS}


def _qc_underlying(inst: UnderlyingInstrument) -> None:
    problems = []
    if not inst.currency:
        problems.append("currency is empty")
    if inst.multiplier is None or inst.multiplier <= 0:
        problems.append(f"impossible multiplier: {inst.multiplier!r}")
    if inst.con_id is None:
        problems.append("con_id (broker key) missing")
    if problems:
        raise DataQualityError(
            f"{inst.canonical_key}: " + "; ".join(problems)
        )


def _raw_to_underlying(
    raw: dict[str, Any], sec_type: str, currency: str, exchange: str
) -> UnderlyingInstrument:
    sym = raw.get("symbol") or ""
    cur = raw.get("currency") or currency
    return UnderlyingInstrument(
        canonical_key=underlying_key(sym, sec_type, cur),
        symbol=sym,
        sec_type=sec_type,
        currency=cur,
        exchange=raw.get("exchange") or exchange,
        primary_exchange=raw.get("primaryExchange") or None,
        con_id=raw.get("conId"),
        multiplier=1.0,  # cash equity / index
        listing_status="ACTIVE",
    )


def resolve_underlying(
    session: IBSession,
    symbol: str,
    *,
    sec_type: str = "STK",
    currency: str = "USD",
    exchange: str = "SMART",
    primary_exchange: str | None = None,
) -> tuple[UnderlyingInstrument, dict[str, Any]]:
    """Resolve one underlying. Returns (canonical instrument, raw payload)."""
    from ib_insync import Index, Stock

    if sec_type == "STK":
        req = Stock(symbol, exchange, currency, primaryExchange=primary_exchange or "")
    elif sec_type == "IND":
        req = Index(symbol, exchange, currency)
    else:
        raise ValueError(f"Unsupported underlying sec_type: {sec_type!r}")

    qualified = session.ib.qualifyContracts(req)
    raws = [contract_to_raw(c) for c in qualified]
    by_conid = {r["conId"]: r for r in raws if r.get("conId")}

    if not by_conid:
        raise InstrumentResolutionError(
            symbol,
            {"sec_type": sec_type, "currency": currency, "exchange": exchange,
             "reason": "no contract returned", "candidates": raws},
        )
    if len(by_conid) > 1:
        raise InstrumentResolutionError(
            symbol,
            {"reason": "ambiguous: multiple conIds",
             "candidates": list(by_conid.values())},
        )

    raw = next(iter(by_conid.values()))
    inst = _raw_to_underlying(raw, sec_type, currency, exchange)
    _qc_underlying(inst)
    return inst, raw


def get_underlying(session: IBSession, symbol: str, **kwargs: Any) -> UnderlyingInstrument:
    """Convenience: return only the canonical underlying instrument."""
    inst, _raw = resolve_underlying(session, symbol, **kwargs)
    return inst
