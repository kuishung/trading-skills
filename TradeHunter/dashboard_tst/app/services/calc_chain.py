"""The option calculator's view of ONE symbol's chain (OPTION_CALCULATOR_DESIGN.md §3).

Reads the screener collector's last market pass from ``screener.db`` (``ScrContract`` /
``ScrUnderlying`` - Massive's whole-chain snapshot with IV, greeks and open interest,
15 min delayed) and turns the member's typed legs (``{right, strike, expiry, qty}``)
into the engine's :class:`option_calc.Leg` (days to expiry and IV filled from the data).
Nothing here prices anything; that is ``option_calc``.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

from .option_calc import MAX_LEGS, Leg


class ChainError(RuntimeError):
    """The chain cannot serve this request; the message is for the member."""


def _sym(s) -> str:
    return str(s or "").strip().upper().replace(".", "-")


def _f(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def _i(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _date(v) -> _dt.date | None:
    if isinstance(v, _dt.datetime):
        return v.date()
    if isinstance(v, _dt.date):
        return v
    try:
        return _dt.date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Contract:
    expiry: str              # YYYY-MM-DD
    right: str               # C | P
    strike: float
    iv: float | None         # FRACTION, Massive's
    oi: int | None
    volume: int | None
    delta: float | None
    price: float | None      # the collector's estimate (Black-Scholes from its IV)

    @property
    def key(self) -> tuple[str, str, float]:
        return (self.expiry, self.right, self.strike)


@dataclass
class Chain:
    symbol: str
    spot: float | None
    spot_src: str | None
    as_of: _dt.datetime | None
    session: str | None
    name: str | None
    earnings_date: str | None
    iv_rank: float | None
    contracts: list[Contract] = field(default_factory=list)
    _by_key: dict = field(default_factory=dict, repr=False)

    def __post_init__(self):
        self._by_key = {c.key: c for c in self.contracts}

    def expiries(self) -> list[str]:
        return sorted({c.expiry for c in self.contracts})

    def strikes(self, expiry: str, right: str | None = None) -> list[float]:
        return sorted({c.strike for c in self.contracts
                       if c.expiry == expiry and (right is None or c.right == right)})

    def find(self, right: str, strike: float, expiry: str) -> Contract | None:
        return self._by_key.get((str(expiry)[:10], right.upper()[:1], float(strike)))

    def in_window(self, *, expiries, lo: float, hi: float, min_oi: int = 0) -> list[Contract]:
        """The contracts the pairing searches: on one of ``expiries``, strike inside
        ``[lo, hi]``, with an IV, and open interest at least ``min_oi``."""
        want = set(expiries)
        return [c for c in self.contracts
                if c.expiry in want and lo <= c.strike <= hi and c.iv and c.iv > 0
                and (c.oi or 0) >= min_oi]


def load_chain(sdb, symbol) -> Chain:
    """The last market pass's chain for ``symbol`` from the screener DB, or ChainError."""
    from ..screener_models import ScrContract as C, ScrUnderlying as U  # noqa: PLC0415

    sym = _sym(symbol)
    u = sdb.query(U).filter(U.symbol == sym).first()
    rows = (sdb.query(C.expiry, C.right, C.strike, C.iv, C.oi, C.volume, C.delta, C.price,
                      C.session, C.as_of)
               .filter(C.symbol == sym).all())
    if u is None and not rows:
        raise ChainError("%s is not in the screener's list of optionable stocks" % sym)
    if not rows:
        raise ChainError("the last market pass stored no option contracts for %s" % sym)
    contracts, session, as_of = [], None, None
    for e, right, k, iv, oi, vol, delta, price, sess, ao in rows:
        strike = _f(k)
        if strike is None or not right:
            continue
        contracts.append(Contract(expiry=str(e)[:10], right=str(right).upper()[:1], strike=strike,
                                  iv=_f(iv), oi=_i(oi), volume=_i(vol), delta=_f(delta),
                                  price=_f(price)))
        if sess and (session is None or sess > session):
            session = sess
        if ao and (as_of is None or ao > as_of):
            as_of = ao
    return Chain(symbol=sym, spot=_f(getattr(u, "spot", None)), spot_src=getattr(u, "spot_src", None),
                 as_of=as_of, session=session, name=getattr(u, "name", None),
                 earnings_date=getattr(u, "earnings_date", None), iv_rank=_f(getattr(u, "iv_rank", None)),
                 contracts=contracts)


def dte(expiry, today: _dt.date) -> int:
    """Calendar days from ``today`` to ``expiry`` (the sheet's convention), never negative."""
    d = _date(expiry)
    if d is None:
        raise ChainError("bad expiry %r" % (expiry,))
    return max((d - today).days, 0)


def normalise_legs(legs) -> list[dict]:
    """Validate the member's typed legs: 1..4 of ``{right, strike, expiry, qty}``.
    Returns clean dicts or raises ChainError with a message for the member."""
    out = []
    for i, raw in enumerate(legs or (), start=1):
        if not isinstance(raw, dict):
            raise ChainError("leg %d is not an object" % i)
        right = str(raw.get("right") or "").strip().upper()[:1]
        if right not in ("C", "P"):
            raise ChainError("leg %d: call or put?" % i)
        strike = _f(raw.get("strike"))
        if strike is None or strike <= 0:
            raise ChainError("leg %d: a strike is needed" % i)
        exp = _date(raw.get("expiry"))
        if exp is None:
            raise ChainError("leg %d: an expiry date is needed" % i)
        qty = _i(raw.get("qty"))
        if qty is None or qty == 0 or abs(qty) > 1000:
            raise ChainError("leg %d: quantity must be a whole number, + long / - short" % i)
        out.append({"right": right, "strike": strike, "expiry": exp.isoformat(), "qty": qty})
    if not out:
        raise ChainError("at least one leg is needed")
    if len(out) > MAX_LEGS:
        raise ChainError("at most %d legs" % MAX_LEGS)
    return out


def resolve_legs(chain: Chain, legs, today: _dt.date) -> tuple[list[Leg], list[dict]]:
    """The member's legs priced from the chain: returns the engine legs and, for the
    result row, the same legs with ``dte``, ``iv``, ``oi``, ``volume``, ``price`` filled."""
    engine_legs, resolved = [], []
    for raw in normalise_legs(legs):
        c = chain.find(raw["right"], raw["strike"], raw["expiry"])
        if c is None:
            raise ChainError("%s %s %s %g is not in the last market pass"
                             % (chain.symbol, raw["expiry"], "call" if raw["right"] == "C" else "put",
                                raw["strike"]))
        if not c.iv or c.iv <= 0:
            raise ChainError("%s %s %s %g has no implied volatility on this pass"
                             % (chain.symbol, c.expiry, "call" if c.right == "C" else "put", c.strike))
        days = dte(c.expiry, today)
        engine_legs.append(Leg(right=c.right, strike=c.strike, dte=days, iv=c.iv, qty=raw["qty"]))
        resolved.append({**raw, "dte": days, "iv": c.iv, "oi": c.oi, "volume": c.volume,
                         "price": c.price, "delta": c.delta})
    return engine_legs, resolved
