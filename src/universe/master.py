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


# --------------------------------------------------------------------------- #
# Option-chain discovery (Step 2c/d)                                          #
# --------------------------------------------------------------------------- #
import datetime as _dt  # noqa: E402

from universe.schema import OptionChain, OptionInstrument, option_key  # noqa: E402


def normalize_expiry(raw: str) -> _dt.date:
    """Normalize an IBKR expiry string ('YYYYMMDD') into a date."""
    s = str(raw).strip()
    if len(s) != 8 or not s.isdigit():
        raise DataQualityError(f"Unexpected expiry format: {raw!r}")
    return _dt.date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def select_standard_chain(
    params: list[Any],
    symbol: str,
    currency: str,
    *,
    routing_exchange: str = "SMART",
) -> OptionChain:
    """Merge the broker's per-exchange chains into one standard chain.

    The broker returns one entry per (exchange, tradingClass). We keep only the
    standard class (tradingClass == symbol), take the UNION of expirations and
    strikes across exchanges, and require a single consistent multiplier.
    Fail-loud if no standard chain exists or the multiplier is inconsistent.
    """
    std = [p for p in params if getattr(p, "tradingClass", None) == symbol]
    if not std:
        raise InstrumentResolutionError(
            symbol,
            {"reason": "no standard option chain (tradingClass == symbol)",
             "trading_classes": sorted({getattr(p, "tradingClass", "?") for p in params})},
        )
    multipliers = {float(p.multiplier) for p in std if getattr(p, "multiplier", None)}
    if len(multipliers) != 1:
        raise DataQualityError(
            f"{symbol}: inconsistent option multipliers across chains: {multipliers}"
        )
    multiplier = multipliers.pop()
    expirations = sorted({normalize_expiry(e) for p in std for e in p.expirations})
    strikes = sorted({float(k) for p in std for k in p.strikes})
    con_id = int(getattr(std[0], "underlyingConId", 0)) or 0

    if not expirations or not strikes:
        raise DataQualityError(f"{symbol}: empty option chain (expiries/strikes)")

    return OptionChain(
        underlying_symbol=symbol,
        underlying_con_id=con_id,
        currency=currency,
        exchange=routing_exchange,
        trading_class=symbol,
        multiplier=multiplier,
        expirations=tuple(expirations),
        strikes=tuple(strikes),
    )


def get_option_chain(session: IBSession, underlying) -> OptionChain:
    """Discover and normalize the standard option chain for an underlying."""
    params = session.ib.reqSecDefOptParams(
        underlying.symbol, "", underlying.sec_type, underlying.con_id
    )
    return select_standard_chain(params, underlying.symbol, underlying.currency)


def _qc_option(opt: OptionInstrument) -> None:
    problems = []
    if not opt.currency:
        problems.append("currency empty")
    if opt.multiplier is None or opt.multiplier <= 0:
        problems.append(f"impossible multiplier: {opt.multiplier!r}")
    if opt.strike is None or opt.strike <= 0:
        problems.append(f"impossible strike: {opt.strike!r}")
    if opt.right not in ("C", "P"):
        problems.append(f"invalid right: {opt.right!r}")
    if not isinstance(opt.expiry, _dt.date):
        problems.append("expiry not a date")
    if problems:
        raise DataQualityError(f"{opt.canonical_key}: " + "; ".join(problems))


def materialize_options(
    chain: OptionChain,
    *,
    session_date: _dt.date | None = None,
    maturity_days: int | None = None,
) -> list[OptionInstrument]:
    """Expand a chain into canonical OptionInstrument rows (strike x right).

    Optional maturity-window filter keeps only expiries within ``maturity_days``
    of ``session_date`` (Step 2 output: filter by maturity window).
    """
    expiries = list(chain.expirations)
    if maturity_days is not None:
        ref = session_date or _dt.date.today()
        horizon = ref + _dt.timedelta(days=maturity_days)
        expiries = [e for e in expiries if ref <= e <= horizon]

    out: list[OptionInstrument] = []
    for expiry in expiries:
        for strike in chain.strikes:
            for right in ("C", "P"):
                opt = OptionInstrument(
                    canonical_key=option_key(
                        chain.underlying_symbol, chain.currency, expiry, strike, right
                    ),
                    underlying_symbol=chain.underlying_symbol,
                    sec_type="OPT",
                    currency=chain.currency,
                    exchange=chain.exchange,
                    expiry=expiry,
                    strike=strike,
                    right=right,
                    multiplier=chain.multiplier,
                    trading_class=chain.trading_class,
                    con_id=None,  # resolved lazily at subscription time (Step 3)
                )
                _qc_option(opt)
                out.append(opt)
    return out


def load_active_universe(
    session: IBSession,
    symbols: list[str],
    session_date: _dt.date,
    *,
    store: Any | None = None,
    config_fp: str = "",
    maturity_days: int | None = None,
) -> dict[str, Any]:
    """Resolve underlyings + option chains for a session and optionally persist.

    Returns a summary; when ``store`` is provided, underlyings (JSON) and option
    instruments (Parquet) are persisted under the session date so the same day
    can be reconstructed (Step 2g).
    """
    underlyings: list[UnderlyingInstrument] = []
    raws: list[dict[str, Any]] = []
    options: list[OptionInstrument] = []

    for sym in symbols:
        inst, raw = resolve_underlying(session, sym)
        underlyings.append(inst)
        raws.append(raw)
        chain = get_option_chain(session, inst)
        options.extend(
            materialize_options(
                chain, session_date=session_date, maturity_days=maturity_days
            )
        )

    if store is not None:
        store.save_underlyings(session_date.isoformat(), underlyings, raws, config_fp)
        store.save_options(session_date.isoformat(), options)

    return {
        "session_date": session_date.isoformat(),
        "underlying_count": len(underlyings),
        "option_count": len(options),
        "underlyings": [u.canonical_key for u in underlyings],
    }
