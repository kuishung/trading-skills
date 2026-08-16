"""My Watchlist — the per-user starred-ticker list.

Every function here takes the acting user and scopes by ``user_id``, so this
module is the single place per-user isolation is enforced: a caller cannot read
or mutate someone else's list even by passing a symbol they don't own.

The list holds symbols only. A starred ticker's market data still comes from the
shared ``MATPLevel`` row when one exists; when it doesn't (the user starred
something straight off the Sector or Company page that no MATP run has covered
yet), ``placeholder_level`` supplies a stand-in with the same attribute surface
the watchlist grid reads, so the board renders it with a live price and blank
target columns instead of dropping it.

Portable ORM only (per the CLAUDE.md data-handling rule) -- query-then-write,
no SQLite-specific upsert, so the Postgres swap stays a config change.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import MATPLevel, UserWatchlist


def _clean(symbol: str) -> str:
    return (symbol or "").strip().upper()


def symbols(db: Session, user) -> list[str]:
    """This user's starred symbols, newest first."""
    if user is None:
        return []
    rows = (
        db.query(UserWatchlist)
        .filter(UserWatchlist.user_id == user.id)
        .order_by(UserWatchlist.created_at.desc().nullslast(), UserWatchlist.id.desc())
        .all()
    )
    return [r.symbol for r in rows]


def symbol_set(db: Session, user) -> set[str]:
    """Same as `symbols` but as a set — for rendering the star filled/hollow."""
    return set(symbols(db, user))


def is_starred(db: Session, user, symbol: str) -> bool:
    if user is None:
        return False
    sym = _clean(symbol)
    return bool(
        db.query(UserWatchlist.id)
        .filter(UserWatchlist.user_id == user.id, UserWatchlist.symbol == sym)
        .first()
    )


def add(db: Session, user, symbol: str, note: str | None = None) -> bool:
    """Star a symbol. Returns True if it was added, False if already there
    (idempotent — double-clicking the star must not raise)."""
    sym = _clean(symbol)
    if not sym or user is None:
        return False
    existing = (
        db.query(UserWatchlist)
        .filter(UserWatchlist.user_id == user.id, UserWatchlist.symbol == sym)
        .first()
    )
    if existing is not None:
        if note and not existing.note:
            existing.note = note
            db.commit()
        return False
    db.add(UserWatchlist(user_id=user.id, symbol=sym, note=note or None))
    db.commit()
    return True


def remove(db: Session, user, symbol: str) -> bool:
    """Unstar a symbol. Returns True if a row was deleted. Only ever touches
    THIS user's row — another member's star on the same symbol is untouched."""
    sym = _clean(symbol)
    if not sym or user is None:
        return False
    row = (
        db.query(UserWatchlist)
        .filter(UserWatchlist.user_id == user.id, UserWatchlist.symbol == sym)
        .first()
    )
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def toggle(db: Session, user, symbol: str) -> bool:
    """Star if absent, unstar if present. Returns the NEW starred state."""
    if is_starred(db, user, symbol):
        remove(db, user, symbol)
        return False
    add(db, user, symbol)
    return True


class PlaceholderLevel:
    """Stand-in for a starred symbol with no MATPLevel row yet.

    Exposes exactly the attributes the watchlist grid reads, all None except the
    symbol, so `_wl_macros.ticker_grid` renders it unchanged: live price fills in
    from the runtime quote, and MATP/MBP/trend show as "-" until an MATP run
    covers the ticker. Not persisted — built per request.
    """

    __slots__ = ("symbol", "exchange", "matp", "mbp", "trend", "signal",
                 "status", "filter_id", "as_of", "last_earnings_date", "n_targets")

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.exchange = None
        self.matp = None
        self.mbp = None
        self.trend = None
        self.signal = None
        self.status = "active"
        self.filter_id = None
        self.as_of = None
        self.last_earnings_date = None
        self.n_targets = None


def levels_for(db: Session, user, all_levels: list | None = None) -> list:
    """This user's starred tickers as grid-ready rows.

    Uses the shared MATPLevel when one exists (so targets/trend/signal show), and
    a PlaceholderLevel when it doesn't. Order follows the star order (newest
    first) so a just-added ticker appears at the top where the user expects it.
    Pass `all_levels` to reuse a query the caller already made.
    """
    syms = symbols(db, user)
    if not syms:
        return []
    if all_levels is None:
        all_levels = db.query(MATPLevel).filter(MATPLevel.symbol.in_(syms)).all()
    by_sym = {lv.symbol: lv for lv in all_levels}
    return [by_sym.get(s) or PlaceholderLevel(s) for s in syms]
