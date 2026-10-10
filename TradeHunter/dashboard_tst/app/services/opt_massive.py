"""Massive -> the Options data layer (OPTIONS_V2_DESIGN.md §13.3, Part M1): the chain
snapshot turned into ``opt_quote`` rows, the stock price, the fetch window, and the IV30
history behind IV rank.

What it does
------------
* ``ingest_symbol`` - one chain snapshot (Options Starter: greeks, IV, open interest, the
  day bar; 15 min delayed; no bid/ask) -> the spot (``estimate_spot``) -> the window
  (``window``, th_ibkr.plan's ticker-relative rule) -> ``opt_store.upsert_quotes`` with
  ``source="massive"``, ``mdt="delayed"``, each row stamped with the read time minus the
  15-min delay (a row carrying a quote keeps its quote time), ``mid`` = the bid/ask
  midpoint when the plan has quotes, else the
  Black-Scholes price from the contract's own IV (``model_price``) -> the spot ->
  today's IV30 from the stored chain into ``opt_underlying_daily``.
* ``backfill_history`` - two years of Stocks Basic daily bars, and an IV30 series for
  the last ~260 sessions rebuilt from daily bars of the monthly option contracts the
  stock was trading near (per expiry, only the few strikes it closed near during its
  15-45 DTE window, call and put, one request each; IV solved from the closes,
  interpolated to 30 days in variance-time). The option strikes and the IV's stock
  price come from UNADJUSTED closes (an expired contract kept its pre-split strike);
  the adjusted bars are what is filed (HV, ATR).
* ``daily_update`` - the last ten days of bars, then the stock statistics.

Both bar reads end at the last PUBLISHED session (``published_session``): the day
itself once it is past 20:00 ET on a trading day, else the trading day before - a
read in session never files a part-day bar as a close.

The chain is cut to the standard contracts first (``standard_rows``): a contract that
does not deliver 100 shares, or an adjusted one (another root after a corporate
action) at the same expiry / right / strike as the standard one, is dropped.

Units: a contract's ``iv`` is a FRACTION; a day's IV30 (``iv_series``, ``iv30``) is
PERCENT. Model prices use ``opt_constants.RISK_FREE`` and no dividend, the same as
``payoff`` - so a model mid, a payoff curve and a solved IV agree with each other.

Everything is ticker-relative (CLAUDE.md): the window is spot +/- k x spot x IV x
sqrt(DTE); the strike grids follow the price's magnitude. Nothing here prints or logs
the API key (``massive`` keeps it in the Authorization header).
"""
from __future__ import annotations

import datetime as _dt
import logging
import math
import statistics
import time

from ..models import OptUnderlyingDaily
from . import clock, opt_store, option_metrics
from .black_scholes import black_scholes
from .massive import MassiveError, option_ticker
from .opt_constants import ATM_MAX_DIST_PCT, RISK_FREE
from .payoff import implied_vol

log = logging.getLogger(__name__)

SOURCE = "massive"
MDT = "delayed"                 # Options Starter: 15-minute delayed
DELAY_S = 900                   # a row with no feed stamp shows the market 15 min ago

# window() - th_ibkr.plan's defaults (§3.2 spec)
DEFAULT_IV = 0.40               # width when no IV is known (fraction)
MAX_WEEKLY_DTE = 63
MAX_DTE = 1100
SIGMA_K = 2.5
MIN_SIDE = 6
MAX_SIDE = 40

SNAP_STRIKE_LO, SNAP_STRIKE_HI = 0.3, 3.0   # snapshot strike range x a known spot
HINT_DELTA_LO, HINT_DELTA_HI = 0.35, 0.65   # rows whose IV hints the window width
HINT_MIN_DTE = 7

# estimate_spot() - put-call parity
PARITY_MIN_DTE = 7
PARITY_EXPIRIES = 3             # the nearest expiries >= 7 DTE tried, in order
PARITY_MIN_STRIKES = 2          # pairs needed for an estimate ...
PARITY_MAX_STRIKES = 4          # ... and used at most (nearest the 50-delta strike)

# backfill_history()
IV_DTE_LO, IV_DTE_HI = 15, 45   # an expiry speaks for a day 15-45 days before it
IV_MAX_STRIKES = 6              # strikes per expiry at most (each = 2 requests)
IV_EMPTY_STRIKES = 2            # an expiry stops after this many strikes in a row with no bars
IV_EMPTY_EXPIRIES = 3           # expiries are read newest first; this many empty in a row ends it
HISTORY_MIN_POINTS = 20         # history_done needs this many bars AND IV points
DAILY_UPDATE_DAYS = 10
BARS_PUBLISHED = _dt.time(20, 0)   # ET: a session's Stocks Basic daily bar is out by then
STD_MULTIPLIER = 100            # shares per standard contract


# ────────────────────────────────── helpers ──────────────────────────────────

def _num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _pos(v):
    f = _num(v)
    return f if (f is not None and f > 0) else None


def _sym(s) -> str:
    return str(s or "").strip().upper()[:20]


def _to_date(x) -> _dt.date | None:
    if isinstance(x, _dt.datetime):
        return x.date()
    if isinstance(x, _dt.date):
        return x
    s = str(x or "").strip()
    try:
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            return _dt.date.fromisoformat(s[:10])
        if len(s) == 8 and s.isdigit():
            return _dt.date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None
    return None


def _naive_utc(ts) -> _dt.datetime | None:
    if not isinstance(ts, _dt.datetime):
        return None
    if ts.tzinfo is not None:
        ts = ts.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return ts


def _now(now=None) -> _dt.datetime:
    return _naive_utc(now) or _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


def _day(today, t_now: _dt.datetime) -> _dt.date:
    """The ET date: ``today`` when given, else New York's date at ``t_now``."""
    return _to_date(today) or clock.et_date(t_now)


def _right(r) -> str | None:
    s = str(r or "").strip().upper()
    return s[:1] if s[:1] in ("C", "P") else None


def third_friday(year: int, month: int) -> _dt.date:
    d = _dt.date(year, month, 15)
    return d + _dt.timedelta(days=(4 - d.weekday()) % 7)


def is_monthly(d: _dt.date, listed=None) -> bool:
    """th_ibkr.is_monthly: the third Friday, or the Thursday before it when that Friday
    is not listed (an exchange holiday - Good Friday, Juneteenth observed)."""
    tf = third_friday(d.year, d.month)
    if d == tf:
        return True
    return d == tf - _dt.timedelta(days=1) and listed is not None and tf not in listed


def monthly_expiry(year: int, month: int) -> _dt.date:
    """The standard monthly expiration: the third Friday, or the Thursday before it
    when the Friday is an NYSE holiday."""
    tf = third_friday(year, month)
    return tf if clock.is_trading_day(tf) else tf - _dt.timedelta(days=1)


def _close_time_utc(on) -> _dt.datetime | None:
    """16:00 ET on date ``on`` as naive UTC (the moment a daily close shows)."""
    d = _to_date(on)
    if d is None:
        return None
    off = clock.et_now(_dt.datetime(d.year, d.month, d.day, 20, 0)).utcoffset() or _dt.timedelta(hours=-5)
    return _dt.datetime.combine(d, clock.SESSION_CLOSE) - off


def _iv_day(day: _dt.date, t_now: _dt.datetime) -> str:
    """The session a snapshot's IV belongs to: ``day`` itself on a trading day once the
    session has opened; before the open, and on a weekend / holiday, the last session."""
    et = clock.et_now(t_now)
    if et.date() == day and clock.is_trading_day(day) and et.time() < clock.SESSION_OPEN:
        return clock.prev_trading_day(day).isoformat()
    return clock.last_trading_day(day).isoformat()


# ────────────────────────────────── pricing + spot ──────────────────────────────────

def model_price(spot, strike, dte_days, iv, right) -> float | None:
    """The Black-Scholes price of a contract from its own IV (a FRACTION) at
    ``RISK_FREE``, no dividend, T = max(DTE, 0.5) / 365 - the "mid" when the plan has
    no bid/ask. Rounded to the cent; None without a spot, strike, IV or right."""
    S, K, sig, d = _pos(spot), _pos(strike), _pos(iv), _num(dte_days)
    r = _right(right)
    if S is None or K is None or sig is None or d is None or r is None:
        return None
    try:
        p = black_scholes(S, K, max(d, 0.5) / 365.0, RISK_FREE, sig,
                          "call" if r == "C" else "put").price
    except (ValueError, OverflowError, ZeroDivisionError):
        return None
    return round(max(p, 0.0), 2)


def _bar_session(r) -> _dt.date | None:
    """The ET session of a snapshot row's day bar (its ``last_updated``), or None."""
    t = r.get("last_updated")
    return clock.et_date(t) if isinstance(t, _dt.datetime) else None


def _parity_spot(rows, day: _dt.date) -> float | None:
    """S = K e^(-rT) + C - P from the day closes of a call and a put at the same strike
    that both traded (volume > 0) in the snapshot's NEWEST session - both legs' day bars
    stamped that session - on the nearest expiry >= 7 DTE: the 2-4 traded strikes
    nearest the 50-delta strike, median of their estimates. The next expiries (up to
    three in all) are tried when one has too few traded pairs. Massive's ``day`` is the
    contract's most recent bar, so a put that has not printed yet today still carries
    the last session's close; pairing it with today's call would mix two sessions.
    A row with no stamp is not used."""
    newest = max((s for s in (_bar_session(r) for r in rows or ()) if s is not None), default=None)
    if newest is None:
        return None
    by_exp: dict[_dt.date, dict[str, dict[float, dict]]] = {}
    for r in rows or ():
        d, rt, k = _to_date(r.get("expiry")), _right(r.get("right")), _pos(r.get("strike"))
        if d is None or rt is None or k is None or (d - day).days < PARITY_MIN_DTE:
            continue
        by_exp.setdefault(d, {"C": {}, "P": {}})[rt][k] = r
    for d in sorted(by_exp)[:PARITY_EXPIRIES]:
        calls, puts = by_exp[d]["C"], by_exp[d]["P"]
        both = sorted(set(calls) & set(puts))
        traded = [k for k in both
                  if _pos(calls[k].get("day_close")) and _pos(puts[k].get("day_close"))
                  and (calls[k].get("volume") or 0) > 0 and (puts[k].get("volume") or 0) > 0
                  and _bar_session(calls[k]) == newest == _bar_session(puts[k])]
        if len(traded) < PARITY_MIN_STRIKES:
            continue

        def delta_gap(k):
            gaps = [abs(abs(x) - 0.5) for x in (_num(calls[k].get("delta")), _num(puts[k].get("delta")))
                    if x is not None]
            return min(gaps) if gaps else None

        with_delta = [(delta_gap(k), k) for k in both if delta_gap(k) is not None]
        if with_delta:
            atm = min(with_delta)[1]
        else:
            atm = min(traded, key=lambda k: abs(calls[k]["day_close"] - puts[k]["day_close"]))
        near = sorted(traded, key=lambda k: (abs(k - atm), k))[:PARITY_MAX_STRIKES]
        disc = math.exp(-RISK_FREE * (d - day).days / 365.0)
        ests = [k * disc + calls[k]["day_close"] - puts[k]["day_close"] for k in near]
        est = statistics.median(ests)
        if est > 0:
            return est
    return None


def estimate_spot(snapshot, *, stored_close=None, today=None) -> tuple[float | None, str]:
    """The stock price for one snapshot, best source first:
    1. the snapshot's ``underlying_price`` (a stocks plan with snapshots) -> ``"massive"``;
    2. put-call parity on the nearest expiry >= 7 DTE, from calls and puts whose day bars
       are both from the snapshot's newest session (``_parity_spot``) -> ``"parity"``;
    3. ``stored_close`` (the last Stocks Basic close on file) -> ``"close"``;
    else ``(None, "none")``."""
    snap = snapshot or {}
    u = _pos(snap.get("underlying_price"))
    if u is not None:
        return round(u, 4), "massive"
    day = _to_date(today) or clock.et_date()
    p = _parity_spot(snap.get("rows") or (), day)
    if p is not None:
        return round(p, 4), "parity"
    c = _pos(stored_close)
    if c is not None:
        return c, "close"
    return None, "none"


# ────────────────────────────────── the window ──────────────────────────────────

def window(rows, spot, *, today, iv_hint=None, max_weekly_dte=MAX_WEEKLY_DTE, max_dte=MAX_DTE,
           sigma_k=SIGMA_K, min_side=MIN_SIDE, max_side=MAX_SIDE) -> list[dict]:
    """The rows worth storing - ``bridge/th_ibkr.plan``'s rule applied to rows already
    fetched. Pure; the rows come back in their own order.

    Expiries: every one with 0 <= DTE <= ``max_weekly_dte``, plus the monthlies
    (``is_monthly`` against the expiries present) up to ``max_dte``. Strikes per expiry
    (that expiry's own listed strikes): those within spot +/- ``sigma_k`` x spot x iv x
    sqrt(max(DTE, 1) / 365), at least ``min_side`` and at most ``max_side`` on each side
    of the spot, nearest first - ticker-relative. ``iv_hint`` is a FRACTION (above 5 it
    is read as a percent); missing -> 0.40. Both rights of a kept strike are kept.
    Without a spot only the expiry rule applies."""
    day = _to_date(today) or clock.et_date()
    s = _pos(spot)
    iv = _pos(iv_hint)
    if iv is None:
        iv = DEFAULT_IV
    elif iv > 5.0:
        iv = iv / 100.0
    k = max(0.0, float(sigma_k))
    lo_n = max(0, int(min_side))
    hi_n = max(lo_n, int(max_side))
    strikes_by: dict[_dt.date, set] = {}
    for r in rows or ():
        d, kk = _to_date(r.get("expiry")), _pos(r.get("strike"))
        if d is not None and kk is not None:
            strikes_by.setdefault(d, set()).add(kk)
    listed = set(strikes_by)
    wk, mx = int(max_weekly_dte), int(max_dte)
    keep: dict[_dt.date, set | None] = {}
    for d in sorted(listed):
        dte = (d - day).days
        if dte < 0 or not (dte <= wk or (dte <= mx and is_monthly(d, listed))):
            continue
        if s is None:
            keep[d] = None
            continue
        ks = sorted(strikes_by[d])
        below = [x for x in ks if x < s]
        above = [x for x in ks if x >= s]
        half = k * s * iv * math.sqrt(max(dte, 1) / 365.0)
        n_below = min(len(below), max(lo_n, min(hi_n, sum(1 for x in below if x >= s - half))))
        n_above = min(len(above), max(lo_n, min(hi_n, sum(1 for x in above if x <= s + half))))
        keep[d] = set((below[-n_below:] if n_below else []) + above[:n_above])
    out = []
    for r in rows or ():
        d, kk = _to_date(r.get("expiry")), _pos(r.get("strike"))
        if d in keep and (keep[d] is None or kk in keep[d]):
            out.append(r)
    return out


def _iv_hint(und: dict, rows, day: _dt.date) -> float | None:
    """The window's IV: the stored IV30 (PERCENT -> fraction), else the median IV of
    the snapshot's near-the-money rows (|delta| 0.35-0.65, >= 7 DTE)."""
    v = _pos((und or {}).get("iv30"))
    if v is not None:
        return v / 100.0
    ivs = []
    for r in rows or ():
        d, iv, dl = _to_date(r.get("expiry")), _pos(r.get("iv")), _num(r.get("delta"))
        if d is None or iv is None or dl is None or (d - day).days < HINT_MIN_DTE:
            continue
        if HINT_DELTA_LO <= abs(dl) <= HINT_DELTA_HI:
            ivs.append(iv)
    return statistics.median(ivs) if ivs else None


def _last_close(db, sym: str) -> tuple[float | None, str | None]:
    """The newest stored daily close of ``sym`` and its date (ORM, portable)."""
    D = OptUnderlyingDaily
    row = (db.query(D.on, D.close)
             .filter(D.symbol == sym, D.close.isnot(None))
             .order_by(D.on.desc())
             .first())
    if row is None:
        return None, None
    return _pos(row[1]), row[0]


def standard_rows(symbol, rows) -> list[dict]:
    """The chain's standard contracts, in their own order. A row whose ``multiplier``
    (shares per contract) is known and not 100 is dropped - a mini, or an adjusted
    contract after a spin-off / special dividend / odd split, whose IV and greeks belong
    to another deliverable. When two rows share (expiry, right, strike) - an adjusted
    root listed beside the new standard one - the row whose ``ticker`` is the standard
    OCC ticker of ``symbol`` (``massive.option_ticker``) is kept, else the first seen."""
    def is_std(r) -> bool:
        t = str(r.get("ticker") or "").strip().upper()
        if not t:
            return False
        try:
            return t == option_ticker(symbol, r.get("expiry"), r.get("right"), r.get("strike"))
        except ValueError:
            return False

    out: list[dict] = []
    at: dict[tuple, int] = {}
    for r in rows or ():
        m = r.get("multiplier")
        if m is not None and m != STD_MULTIPLIER:
            continue
        key = (_to_date(r.get("expiry")), _right(r.get("right")), _pos(r.get("strike")))
        i = at.get(key)
        if i is None:
            at[key] = len(out)
            out.append(r)
        elif not is_std(out[i]) and is_std(r):
            out[i] = r
    return out


def _flat_chain(chain: dict) -> list[dict]:
    out = []
    for e in (chain or {}).get("expiries") or ():
        for side, rt in (("calls", "C"), ("puts", "P")):
            for r in e.get(side) or ():
                out.append(dict(r, expiry=e["expiry"], dte=e["dte"], right=rt))
    return out


# ────────────────────────────────── ingest ──────────────────────────────────

def ingest_symbol(db, client, symbol, *, today=None, kind="cycle", now=None) -> dict:
    """Read ``symbol``'s chain from Massive and file it (§13.3). ``client`` is a
    ``massive.Client``; ``kind`` is ``cycle`` | ``eod`` | ``manual`` (the refresh-log
    kind); ``now`` (naive UTC) is for tests. Raises ``MassiveError`` when the read
    fails - nothing is written then. Only the standard contracts (``standard_rows``)
    reach the spot, the window and the store.

    Returns ``{"symbol", "stored", "expiries", "spot", "spot_kind", "iv30", "pages",
    "ms", "rows", "skipped_bad", "as_of"}`` - ``rows`` = contracts in the snapshot,
    ``stored`` = written, ``expiries`` = expiries written, ``iv30`` PERCENT or None."""
    t0 = time.monotonic()
    sym = _sym(symbol)
    if not sym:
        raise ValueError("ingest_symbol: no symbol")
    t_now = _now(now)
    day = _day(today, t_now)
    und = opt_store.underlying(db, sym) or {}
    stored_close, close_on = _last_close(db, sym)
    known = _pos(und.get("spot")) or stored_close
    kw = {"exp_gte": day, "exp_lte": day + _dt.timedelta(days=MAX_DTE)}
    if known:
        kw["strike_gte"] = round(known * SNAP_STRIKE_LO, 2)
        kw["strike_lte"] = round(known * SNAP_STRIKE_HI, 2)
    snap = client.chain_snapshot(sym, **kw)
    n_snap = len(snap.get("rows") or [])
    rows_in = standard_rows(sym, snap.get("rows") or [])

    spot, spot_kind = estimate_spot(dict(snap, rows=rows_in), stored_close=stored_close, today=day)
    kept = window(rows_in, spot, today=day, iv_hint=_iv_hint(und, rows_in, day))
    snap_as_of = _naive_utc(snap.get("as_of"))
    # Every row of a quote-less (Starter) snapshot is stamped with the READ time minus the
    # 15-min delay, not with its own day.last_updated: that stamp can be the contract's
    # last TRADE, so a thinly traded strike would look hours old while its IV, greeks and
    # model price are as current as the rest of the snapshot. Only a row carrying a quote
    # (a plan with quotes) keeps its own quote time.
    fallback = t_now - _dt.timedelta(seconds=DELAY_S)

    rows = []
    for r in kept:
        d = _to_date(r.get("expiry"))
        bid, ask = _num(r.get("bid")), _num(r.get("ask"))
        if bid is not None and ask is not None and ask >= bid >= 0:
            mid = round((bid + ask) / 2.0, 4)
        else:
            mid = model_price(spot, r.get("strike"), (d - day).days, r.get("iv"), r.get("right"))
        stamp = ((_naive_utc(r.get("last_updated")) or fallback)
                 if (bid is not None and ask is not None) else fallback)
        rows.append({
            "expiry": d.isoformat(), "right": r.get("right"), "strike": r.get("strike"),
            "bid": bid, "ask": ask, "mid": mid, "last": r.get("day_close"),
            "bid_size": r.get("bid_size"), "ask_size": r.get("ask_size"),
            "volume": r.get("volume"), "oi": r.get("oi"), "iv": r.get("iv"),
            "delta": r.get("delta"), "gamma": r.get("gamma"), "theta": r.get("theta"),
            "vega": r.get("vega"), "und_price": spot if spot else r.get("und_price"),
            "as_of": min(stamp, t_now),
        })
    res = opt_store.upsert_quotes(db, sym, rows, source=SOURCE, mdt=MDT, kind=kind,
                                  ms=int((time.monotonic() - t0) * 1000), now=t_now)

    if spot:
        if spot_kind == "close":
            s_as_of, s_mdt = _close_time_utc(close_on) or fallback, "eod"
        elif spot_kind == "massive":
            s_as_of, s_mdt = _naive_utc(snap.get("underlying_as_of")) or fallback, MDT
        else:
            s_as_of, s_mdt = fallback, MDT
        opt_store.set_spot(db, sym, spot, source=SOURCE, mdt=s_mdt, as_of=min(s_as_of, t_now),
                           now=t_now)

    iv30 = None
    if spot:
        day_s = day.isoformat()
        chain = opt_store.chain_view(db, sym, today=day_s, now=t_now)
        by_exp = option_metrics.atm_iv_by_expiry(_flat_chain(chain), spot, day_s, require_quote=False)
        v = option_metrics.iv30_constant_maturity(by_exp, day_s)
        if v is not None and v > 0:
            iv30 = round(v, 4)
            opt_store.upsert_daily(db, sym, iv_series=[{"on": _iv_day(day, t_now), "iv": iv30}],
                                   source=SOURCE, today=day_s, now=t_now)
            opt_store.recompute_underlying(db, sym, now=t_now)

    return {"symbol": sym, "stored": res.get("stored", 0), "expiries": res.get("n_expiries", 0),
            "spot": spot, "spot_kind": spot_kind, "iv30": iv30, "pages": snap.get("pages", 0),
            "ms": int((time.monotonic() - t0) * 1000), "rows": n_snap,
            "skipped_bad": res.get("skipped_bad", 0), "as_of": snap_as_of}


# ────────────────────────────────── history ──────────────────────────────────

def strike_grid(price) -> tuple[float, float, float]:
    """(finer, coarser, standard) listed-strike steps for a stock at ``price``: under $25
    0.5 / 1 / 2.5, under $100 1 / 2.5 / 5, under $200 2.5 / 5 / 5, under $500 2.5 / 5 / 10,
    else 5 / 10 / 10. The standard step is the exchanges' base listing increment ($2.50
    under $25, $5 to $200, $10 above) - the grid a stock with options always has; the
    finer ones are listed only on the more active names."""
    p = _pos(price) or 0.0
    std = 2.5 if p < 25 else 5.0 if p < 200 else 10.0
    if p < 25:
        return 0.5, 1.0, std
    if p < 100:
        return 1.0, 2.5, std
    if p < 500:
        return 2.5, 5.0, std
    return 5.0, 10.0, std


def _snap(price: float, step: float) -> float:
    return round(max(step, round(price / step) * step), 4)


def _on_grid(k: float, step: float) -> bool:
    q = k / step
    return abs(q - round(q)) < 1e-6


def _pick_strikes(closes, step: float, n_max: int = IV_MAX_STRIKES) -> list[float]:
    """The grid strikes nearest ``closes``; when more than ``n_max``, the strikes
    nearest the closes' quantiles (spread over the range the stock traded)."""
    ks = sorted({_snap(c, step) for c in closes})
    if len(ks) <= n_max:
        return ks
    cs = sorted(closes)
    return sorted({_snap(cs[min(len(cs) - 1, int((i + 0.5) * len(cs) / n_max))], step)
                   for i in range(n_max)})


def _bars_by_day(bars) -> dict[str, float]:
    out = {}
    for b in bars or ():
        c = _pos(b.get("close"))
        on = b.get("on")
        if c is not None and on:
            out[str(on)[:10]] = c
    return out


def _fetch_expiry(client, sym: str, expiry: _dt.date, days: list[_dt.date], closes: dict,
                  counter: list) -> dict[tuple, dict[str, float]]:
    """Daily closes of the call and put at the few strikes ``sym`` closed near on
    ``days`` (the expiry's 15-45 DTE window): ``{(right, strike): {on: close}}``.

    The grids of ``strike_grid`` are tried finest first (the coarser one when the finest
    needs more than ``IV_MAX_STRIKES`` strikes). While nothing has been found, a strike
    off the next coarser grid that returns no call bars says this grid is not listed for
    the expiry, so the rest is read on the next one - down to the standard grid. Two
    strikes in a row with no bars on either leg end the pass (nothing listed or traded
    there). Last, when the strikes found do not bracket the window's median close, the
    standard-grid strikes just below and above it (the listed strikes that bracket the
    closes) are read too - only then is an expiry with no bars empty. One
    ``option_daily`` request per contract, counted in ``counter[0]``. A request the
    server refused as bad (``http``) reads as no bars; any other ``MassiveError``
    propagates."""
    win = [closes[d.isoformat()] for d in days]
    med = statistics.median(win)
    tiers = sorted(set(strike_grid(med)))           # finest first, no repeats
    ti = 0 if len({_snap(c, tiers[0]) for c in win}) <= IV_MAX_STRIKES else min(1, len(tiers) - 1)
    std = tiers[-1]
    start, end = days[0], days[-1]

    def nxt(i: int) -> float | None:                 # the next coarser grid, or None
        return tiers[i + 1] if i + 1 < len(tiers) else None

    def order(ks, i: int):   # a strike only this grid has goes first: it probes the grid
        n = nxt(i)
        return sorted(ks, key=lambda k: (n is not None and _on_grid(k, n), abs(k - med), k))

    def read(right: str, k: float) -> dict[str, float]:
        counter[0] += 1
        try:
            bars = client.option_daily(option_ticker(sym, expiry, right, k), start, end)
        except MassiveError as exc:
            if exc.kind != "http":
                raise
            return {}
        return _bars_by_day(bars)

    legs: dict[tuple, dict[str, float]] = {}
    done: set[float] = set()

    def both(k: float, c=None) -> bool:
        c = read("C", k) if c is None else c
        p = read("P", k)
        if c:
            legs[("C", k)] = c
        if p:
            legs[("P", k)] = p
        return bool(c or p)

    empty_run = 0
    queue = order(_pick_strikes(win, tiers[ti]), ti)
    while queue:
        k = queue.pop(0)
        if k in done:
            continue
        done.add(k)
        c = read("C", k)
        n = nxt(ti)
        if not c and not legs and n is not None and not _on_grid(k, n):
            ti, empty_run = ti + 1, 0
            queue = [x for x in order(_pick_strikes(win, tiers[ti]), ti) if x not in done]
            continue
        empty_run = 0 if both(k, c) else empty_run + 1
        if empty_run >= IV_EMPTY_STRIKES:
            break

    found = sorted({k for (_, k) in legs})
    if not (found and found[0] <= med <= found[-1]):
        q = med / std
        for k in sorted({_snap(math.floor(q + 1e-9) * std, std), _snap(math.ceil(q - 1e-9) * std, std)},
                        key=lambda x: (abs(x - med), x)):
            if k not in done:
                done.add(k)
                both(k)
    return legs


def _day_iv(legs: dict, on: str, spot: float, dte: int) -> float | None:
    """The expiry's IV (FRACTION) on day ``on``: at the strike nearest the close with a
    bar that day (within ATM_MAX_DIST_PCT), the mean of the call's and the put's IV
    solved from their closes."""
    strikes = sorted({k for (_, k), bars in legs.items() if on in bars})
    if not strikes:
        return None
    k = min(strikes, key=lambda x: (abs(x - spot), x))
    if abs(k - spot) / spot > ATM_MAX_DIST_PCT:
        return None
    T = dte / 365.0
    ivs = []
    for rt, kind in (("C", "call"), ("P", "put")):
        price = (legs.get((rt, k)) or {}).get(on)
        if price is None:
            continue
        v = implied_vol(price, spot, k, T, kind)
        if v is not None:
            ivs.append(v)
    return statistics.fmean(ivs) if ivs else None


def iv30_history(client, symbol, bars, *, days_iv: int = 260) -> tuple[list[dict], int]:
    """The IV30 series (``[{"on", "iv"}]``, PERCENT, oldest first) for the last
    ``days_iv`` sessions of ``bars``, rebuilt from monthly option contracts' daily
    closes, and the number of ``option_daily`` requests it took.

    Per standard monthly expiry whose 15-45 DTE window overlaps those sessions: the
    strikes the stock closed near in that window (``_fetch_expiry``). Expiries are read
    newest first and three in a row with no bars at all end the reading - the options
    were not listed before then (a young listing keeps its recent history; a stock with
    no options costs a few requests). Per session: each expiry 15-45 days out gives its
    IV at the strike nearest the close (``_day_iv``); two such expiries interpolate to 30
    days in variance-time (``option_metrics.iv30_constant_maturity``), one is used as it
    is."""
    sym = _sym(symbol)
    closes = _bars_by_day(bars)
    sessions = [_dt.date.fromisoformat(d) for d in sorted(closes)][-max(0, int(days_iv)):]
    if not sessions:
        return [], 0
    counter = [0]
    first, last = sessions[0], sessions[-1]
    expiries: list[_dt.date] = []
    y, m = first.year, first.month
    stop = last + _dt.timedelta(days=IV_DTE_HI)
    while (y, m) <= (stop.year, stop.month):
        expiries.append(monthly_expiry(y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    legs_by: dict[_dt.date, dict] = {}
    empty_run = 0
    for e in sorted(expiries, reverse=True):
        days = [d for d in sessions if IV_DTE_LO <= (e - d).days <= IV_DTE_HI]
        if not days:
            continue
        legs = _fetch_expiry(client, sym, e, days, closes, counter)
        if legs:
            legs_by[e] = legs
            empty_run = 0
        else:
            empty_run += 1
            if empty_run >= IV_EMPTY_EXPIRIES:
                break
    points = []
    for d in sessions:
        on = d.isoformat()
        spot = closes[on]
        by_exp = {}
        for e, legs in legs_by.items():
            dte = (e - d).days
            if not (IV_DTE_LO <= dte <= IV_DTE_HI):
                continue
            v = _day_iv(legs, on, spot, dte)
            if v is not None:
                by_exp[e.isoformat()] = {"dte": dte, "atm_iv": v * 100.0}
        iv30 = option_metrics.iv30_constant_maturity(by_exp, on)
        if iv30 is not None and iv30 > 0:
            points.append({"on": on, "iv": round(iv30, 4)})
    return points, counter[0]


def published_session(day, now=None) -> _dt.date:
    """The newest session whose Stocks Basic daily bar is published at ``now`` (naive
    UTC; None = now), for a read on ET date ``day``: ``day`` itself when it is a trading
    day and ``now`` is at or past 20:00 ET on it, else the trading day before ``day``.
    A read in session (or before 20:00) must not take the day's part-day bar as its
    close; the ten-day lookback of the next read picks the day up."""
    d = _to_date(day) or clock.et_date(_now(now))
    et = clock.et_now(_now(now))
    if clock.is_trading_day(d) and (et.date(), et.time()) >= (d, BARS_PUBLISHED):
        return d
    return clock.prev_trading_day(d)


def backfill_history(db, client, symbol, *, today=None, years_bars=2, days_iv=260, now=None) -> dict:
    """The one-time history of ``symbol`` (§13.3): ``years_bars`` years of Stocks Basic
    daily bars (split-adjusted) up to the last published session (``published_session``)
    -> ``opt_underlying_daily`` (HV, ATR read them), the IV30 series for the last
    ``days_iv`` sessions (``iv30_history``, from one more read of those sessions'
    UNADJUSTED bars - an expired contract was listed at the pre-split strike and priced
    off the pre-split stock) -> ``opt_underlying_daily.iv30``, the stock statistics
    (``recompute_underlying``), and ``history_done`` once at least 20 bars AND 20 IV
    points are on file. Bars are filed before the option reads, so a failure part-way
    keeps them (and leaves ``history_done`` unset for a retry).

    Returns ``{"symbol", "bars", "iv_points", "requests", "ms", "history_done"}`` -
    ``requests`` = HTTP reads made (the two stock reads + every option contract)."""
    t0 = time.monotonic()
    sym = _sym(symbol)
    if not sym:
        raise ValueError("backfill_history: no symbol")
    t_now = _now(now)
    day = _day(today, t_now)
    day_s = day.isoformat()
    end = published_session(day, t_now)
    start = end - _dt.timedelta(days=int(years_bars * 366) + 3)
    bars = client.stock_daily(sym, start, end)
    requests = 1
    good = sorted((b for b in bars if _pos(b.get("close")) and b.get("on")), key=lambda b: str(b["on"])[:10])
    if good:
        opt_store.upsert_daily(db, sym, bars=good, source=SOURCE, today=day_s, now=t_now)
    iv_bars = good
    n_iv = max(0, int(days_iv))
    if good and n_iv:
        raw = client.stock_daily(sym, str(good[-min(n_iv, len(good))]["on"])[:10], end, adjusted=False)
        requests += 1
        iv_bars = [b for b in raw if _pos(b.get("close"))] or good
    points, n_opt = iv30_history(client, sym, iv_bars, days_iv=days_iv)
    requests += n_opt
    if points:
        opt_store.upsert_daily(db, sym, iv_series=points, source=SOURCE, today=day_s, now=t_now)
    opt_store.recompute_underlying(db, sym, now=t_now)
    done = len(good) >= HISTORY_MIN_POINTS and len(points) >= HISTORY_MIN_POINTS
    if done:
        opt_store.mark_history_done(db, sym, now=t_now)
    out = {"symbol": sym, "bars": len(good), "iv_points": len(points), "requests": requests,
           "ms": int((time.monotonic() - t0) * 1000), "history_done": done}
    log.info("opt_massive: history %s - %d bars, %d IV points, %d requests", sym, out["bars"],
             out["iv_points"], requests)
    return out


def daily_update(db, client, symbol, *, today=None, now=None) -> dict:
    """The EOD bar top-up: Stocks Basic bars for the ten calendar days up to the last
    published session (``published_session``) -> ``opt_underlying_daily`` ->
    ``recompute_underlying``. Returns ``{"symbol", "bars", "ms", "complete"}`` -
    ``complete`` is True when the bars include that session (False: Massive has not
    published it yet; a later read fills it)."""
    t0 = time.monotonic()
    sym = _sym(symbol)
    if not sym:
        raise ValueError("daily_update: no symbol")
    t_now = _now(now)
    day = _day(today, t_now)
    end = published_session(day, t_now)
    bars = client.stock_daily(sym, end - _dt.timedelta(days=DAILY_UPDATE_DAYS), end)
    good = [b for b in bars if _pos(b.get("close"))]
    if good:
        opt_store.upsert_daily(db, sym, bars=good, source=SOURCE, today=day.isoformat(), now=t_now)
    opt_store.recompute_underlying(db, sym, now=t_now)
    complete = any(str(b.get("on") or "")[:10] == end.isoformat() for b in good)
    return {"symbol": sym, "bars": len(good), "ms": int((time.monotonic() - t0) * 1000),
            "complete": complete}
