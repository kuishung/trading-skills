"""The Options Screener's market tables (OPTIONS_SCREENER_DESIGN.md §3).

They live in their OWN database (``app/screener_db.py``, ``TST_SCREENER_DATABASE_URL``,
default ``screener.db`` beside ``tst.db``) on their own declarative base, ``ScrBase``, with
their own Alembic environment (``alembic_screener/``, version table
``alembic_version_screener``): ~1M contract rows are replaced every market pass, a churn
and a size the platform DB should not carry. Same rules as the main DB: ORM only,
portable types, schema changes through migrations.

Written ONLY by ``services/scr_store`` (the Hermes screener collector); read by
``services/screener/frame`` (the web app). All datetimes naive UTC; a contract's ``iv`` is a
FRACTION, every per-underlying IV / HV figure is PERCENT (the ``opt_*`` convention).
"""
from __future__ import annotations

import datetime as _dt

from sqlalchemy import (Boolean, Column, DateTime, Float, Index, Integer, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import declarative_base

ScrBase = declarative_base()


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


class ScrContract(ScrBase):
    """The latest read of one option contract - one row per (symbol, expiry, right,
    strike), replaced per underlying each market pass. Only standard contracts with open
    interest or volume are kept (§3)."""

    __tablename__ = "scr_contract"
    __table_args__ = (
        UniqueConstraint("symbol", "expiry", "right", "strike", name="uq_scr_contract"),
        Index("ix_scr_contract_symbol", "symbol"),
    )

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False)          # the underlying, our spelling (BRK-B)
    expiry = Column(String(10), nullable=False)          # YYYY-MM-DD
    right = Column(String(1), nullable=False)            # C | P
    strike = Column(Float, nullable=False)
    weekly = Column(Boolean, nullable=False, default=False)   # not the standard monthly
    price = Column(Float, nullable=True)                 # ESTIMATED: Black-Scholes from its own IV, else last
    last = Column(Float, nullable=True)                  # the day bar close (last trade of the session)
    chg_pct = Column(Float, nullable=True)               # the contract's day change, percent
    volume = Column(Integer, nullable=True)
    oi = Column(Integer, nullable=True)                  # open interest as of the prior close
    vol_prev = Column(Integer, nullable=True)            # the previous session's final volume
    oi_prev = Column(Integer, nullable=True)             # the previous session's open interest
    iv = Column(Float, nullable=True)                    # FRACTION
    delta = Column(Float, nullable=True)                 # signed (puts negative)
    gamma = Column(Float, nullable=True)
    theta = Column(Float, nullable=True)
    vega = Column(Float, nullable=True)
    last_trade = Column(DateTime, nullable=True)         # day.last_updated
    session = Column(String(10), nullable=False)         # ET session date the row describes
    as_of = Column(DateTime, nullable=False)             # read time - 15 min (Options Starter delay)


class ScrUnderlying(ScrBase):
    """One row per underlying: identity, the stock facts and technicals (from daily bars),
    the option-wide figures of the latest pass, the IV history state."""

    __tablename__ = "scr_underlying"
    __table_args__ = (UniqueConstraint("symbol", name="uq_scr_underlying_symbol"),)

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False)
    name = Column(String(160), nullable=True)
    sec_type = Column(String(8), nullable=True)          # stock | etf | index | other
    exchange = Column(String(8), nullable=True)          # NYSE | NASDAQ | AMEX | INDEX | OTHER
    spot = Column(Float, nullable=True)
    spot_src = Column(String(8), nullable=True)          # massive | parity | close
    spot_as_of = Column(DateTime, nullable=True)
    prev_close = Column(Float, nullable=True)
    chg_pct = Column(Float, nullable=True)               # spot vs prev_close, percent
    stock_volume = Column(Float, nullable=True)          # the last session's share volume
    avg_vol20 = Column(Float, nullable=True)
    avg_vol50 = Column(Float, nullable=True)
    sma20 = Column(Float, nullable=True)
    sma50 = Column(Float, nullable=True)
    sma200 = Column(Float, nullable=True)
    rsi14 = Column(Float, nullable=True)
    atr14 = Column(Float, nullable=True)
    atr_pct = Column(Float, nullable=True)               # atr14 / close, percent
    hv20 = Column(Float, nullable=True)                  # PERCENT
    hv60 = Column(Float, nullable=True)                  # PERCENT
    hi52 = Column(Float, nullable=True)
    lo52 = Column(Float, nullable=True)
    perf5 = Column(Float, nullable=True)                 # 5-session change, percent
    perf20 = Column(Float, nullable=True)                # 20-session change, percent
    trend = Column(String(10), nullable=True)            # up | down | sideways (MATP classify_trend rule)
    iv30 = Column(Float, nullable=True)                  # PERCENT
    iv30_prev = Column(Float, nullable=True)             # PERCENT, the previous session's
    iv_rank = Column(Float, nullable=True)               # 0..100
    iv_pct = Column(Float, nullable=True)                # 0..100
    iv_hi = Column(Float, nullable=True)
    iv_lo = Column(Float, nullable=True)
    iv_n = Column(Integer, nullable=True)
    exp_move30 = Column(Float, nullable=True)            # one-sigma 30-day move, percent of spot
    call_vol = Column(Integer, nullable=True)
    put_vol = Column(Integer, nullable=True)
    call_oi = Column(Integer, nullable=True)
    put_oi = Column(Integer, nullable=True)
    n_contracts = Column(Integer, nullable=True)
    earnings_date = Column(String(10), nullable=True)
    earnings_src = Column(String(8), nullable=True)      # nasdaq
    bars_as_of = Column(DateTime, nullable=True)
    history_done = Column(Boolean, nullable=False, default=False)
    history_tries = Column(Integer, nullable=False, default=0)
    history_next = Column(DateTime, nullable=True)
    pass_id = Column(Integer, nullable=True)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class ScrUnderlyingDaily(ScrBase):
    """Daily history per underlying: Stocks Basic grouped daily bars (adjusted OHLCV and the
    unadjusted close) and the day's IV30 (PERCENT)."""

    __tablename__ = "scr_underlying_daily"
    __table_args__ = (UniqueConstraint("symbol", "on", name="uq_scr_und_daily"),)

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False)
    on = Column(String(10), nullable=False)              # YYYY-MM-DD (ET session)
    open = Column(Float, nullable=True)
    high = Column(Float, nullable=True)
    low = Column(Float, nullable=True)
    close = Column(Float, nullable=True)                 # split-adjusted
    close_raw = Column(Float, nullable=True)             # as traded that day
    volume = Column(Float, nullable=True)
    iv30 = Column(Float, nullable=True)                  # PERCENT


class ScrUniverse(ScrBase):
    """The optionable underlyings Massive lists (reference contracts, daily)."""

    __tablename__ = "scr_universe"
    __table_args__ = (UniqueConstraint("symbol", name="uq_scr_universe_symbol"),)

    id = Column(Integer, primary_key=True)
    symbol = Column(String(20), nullable=False)
    n_contracts = Column(Integer, nullable=True)
    first_seen = Column(DateTime, nullable=True)
    last_seen = Column(DateTime, nullable=True)
    active = Column(Boolean, nullable=False, default=True)


class ScrPass(ScrBase):
    """One market pass over the universe. ``finished`` is NULL while it runs; the web app
    reloads its arrays when a newer pass finishes."""

    __tablename__ = "scr_pass"

    id = Column(Integer, primary_key=True)
    kind = Column(String(8), nullable=False, default="cycle")    # cycle | eod | manual
    session = Column(String(10), nullable=True)
    started = Column(DateTime, nullable=False)
    finished = Column(DateTime, nullable=True)
    n_symbols = Column(Integer, nullable=True)
    n_ok = Column(Integer, nullable=True)
    n_failed = Column(Integer, nullable=True)
    n_contracts = Column(Integer, nullable=True)
    requests = Column(Integer, nullable=True)
    ms = Column(Integer, nullable=True)


class ScrStatus(ScrBase):
    """The screener collector's heartbeat: a single row, ``id = 1``."""

    __tablename__ = "scr_status"

    id = Column(Integer, primary_key=True)
    state = Column(String(10), nullable=True)            # starting | universe | pass | stocks | history | idle | error | stopped
    detail = Column(Text, nullable=True)
    heartbeat = Column(DateTime, nullable=True)
    pid = Column(Integer, nullable=True)
    version = Column(String(16), nullable=True)
    pass_id = Column(Integer, nullable=True)
    symbols_total = Column(Integer, nullable=True)
    symbols_done = Column(Integer, nullable=True)
    universe_n = Column(Integer, nullable=True)
    universe_on = Column(String(10), nullable=True)
    history_done_n = Column(Integer, nullable=True)
    history_total = Column(Integer, nullable=True)
    last_error = Column(Text, nullable=True)
    api_ok = Column(Boolean, nullable=True)
