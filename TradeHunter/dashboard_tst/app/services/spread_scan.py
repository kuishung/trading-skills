"""Bull put spread screener: build every candidate spread on a chain, score it the
way Barchart's "Bull Put Spread" screen does, and file the results nightly.

Where the numbers come from
---------------------------
* The chain (strikes, expiries, bid/ask, volume, open interest, delta, IV) is
  Cboe's public delayed feed, read server-side by ``option_quotes`` - the same
  feed the Portfolio monitor uses, so a candidate here and a position there are
  priced by the same source.
* **IV percentile** is the one column no free feed serves per day. It is
  computed from ``iv_history``, which this module fills with each symbol's
  ``iv30`` every night. Until a symbol has enough history the column is None and
  the page says so rather than inventing a number. ``deploy/iv_seed_ibkr.py``
  backfills a year from IB Gateway so the wait can be skipped.
* **OTM probability** is ``(1 - |short delta|) x 100``, the standard
  approximation Barchart's figure tracks closely.
* **Credit** is ``short bid - long ask``: what a limit order at the market's
  own prices would actually collect. ``credit_mid`` is the optimistic mid-mid
  figure shown alongside.

Two layers, like the Portfolio monitor: ``build_candidates`` is pure (a chain
in, rows out - unit-testable on synthetic data); ``run_scan`` / ``scan_symbol``
do the I/O.
"""
from __future__ import annotations

import datetime as _dt
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import option_quotes

log = logging.getLogger("dashboard_tst.spread_scan")

# What the nightly scan STORES. Deliberately wider than the screen's defaults
# (DTE 30-45, moneyness -10..0) so a member can widen the filter on the page
# without waiting for tomorrow's run. Beyond these, candidates are not spreads
# anyone sells: sub-20 DTE is gamma territory, past 60 the credit stops paying
# for the time, and a short put more than 25% under the price has no premium.
STORE_DTE_MIN = 20
STORE_DTE_MAX = 60
STORE_MONEYNESS_MIN = -25.0     # percent; short strike no further than this below spot
STORE_MONEYNESS_MAX = 0.0       # short strike at or below spot (OTM puts only)
STORE_LONG_LEGS = 5             # long strikes considered below each short: the next N listed
STORE_MIN_SHORT_BID = 0.05      # a short leg with no bid is not a candidate

# How many days of candidates to keep. The page reads the latest scan; a couple
# of older days let a member compare, and anything beyond that is churn.
KEEP_DAYS = 3

# The page's DEFAULT filter - Barchart's, as the user set them (2026-09-13).
DEFAULT_FILTER = {
    "iv_pct_min": 40.0,
    "dte_min": 30, "dte_max": 45,
    "monthly_only": True,
    "short_vol_min": 100, "short_oi_min": 500,
    "moneyness_min": -10.0, "moneyness_max": 0.0,
    "long_vol_min": 100, "long_oi_min": 500,
    "short_bid_min": 0.05, "long_ask_min": 0.05,
    "otm_prob_min": 75.0,
    "credit_pct_min": 0.0,
    "flag_earnings": False,
    "include_no_iv": True,      # show rows whose IV percentile is not yet known
}

# Percentile needs this many prior observations before it is shown at all.
IV_MIN_OBS = 20


# --------------------------------------------------------------------- helpers

def is_monthly(expiry: str) -> bool:
    """Third Friday of the month - the standard monthly expiry. Saturday-dated
    legacy symbols would be the day after, but Cboe lists Fridays."""
    try:
        d = _dt.date.fromisoformat(expiry)
    except (TypeError, ValueError):
        return False
    return d.weekday() == 4 and 15 <= d.day <= 21


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _int(v):
    f = _num(v)
    return None if f is None else int(f)


# ------------------------------------------------------------------ pure core

def build_candidates(symbol: str, chain: dict, *, today: _dt.date | None = None,
                     dte_min: int = STORE_DTE_MIN, dte_max: int = STORE_DTE_MAX,
                     moneyness_min: float = STORE_MONEYNESS_MIN,
                     moneyness_max: float = STORE_MONEYNESS_MAX,
                     long_legs: int = STORE_LONG_LEGS,
                     earnings: str | None = None) -> list[dict]:
    """Every bull put spread worth storing on this chain, as plain dicts keyed
    like ``SpreadCandidate`` columns (minus scan_on / iv_pct, which the caller
    fills). Pure: no network, no database."""
    today = today or _dt.date.today()
    spot = _num(chain.get("spot"))
    if not spot or spot <= 0:
        return []
    legs = chain.get("legs") or {}
    sym = (symbol or "").upper()

    by_exp: dict[str, list[tuple[float, dict]]] = {}
    for (exp, right, strike), leg in legs.items():
        if right != "P":
            continue
        by_exp.setdefault(exp, []).append((float(strike), leg))

    earn_date = None
    if earnings:
        try:
            earn_date = _dt.date.fromisoformat(earnings)
        except (TypeError, ValueError):
            earn_date = None

    out: list[dict] = []
    for exp, rows in by_exp.items():
        try:
            dte = (_dt.date.fromisoformat(exp) - today).days
        except (TypeError, ValueError):
            continue
        if dte < dte_min or dte > dte_max:
            continue
        rows.sort(key=lambda t: t[0])
        strikes = [k for k, _ in rows]
        leg_at = {k: l for k, l in rows}
        monthly = is_monthly(exp)
        earn_before = bool(earn_date and today <= earn_date <= _dt.date.fromisoformat(exp))

        for i, ks in enumerate(strikes):
            mny = (ks - spot) / spot * 100.0
            if mny < moneyness_min or mny > moneyness_max:
                continue
            s = leg_at[ks]
            sb, sa = _num(s.get("bid")), _num(s.get("ask"))
            if sb is None or sb < STORE_MIN_SHORT_BID:
                continue
            sd = _num(s.get("delta"))
            sd = None if sd is None else abs(sd)
            # the next N listed strikes BELOW the short
            for kl in strikes[max(0, i - long_legs):i][::-1]:
                l = leg_at[kl]
                lb, la = _num(l.get("bid")), _num(l.get("ask"))
                width = ks - kl
                if width <= 0:
                    continue
                credit = (sb - la) if (sb is not None and la is not None) else None
                s_mid = (sb + sa) / 2 if (sb is not None and sa is not None) else None
                l_mid = (lb + la) / 2 if (lb is not None and la is not None) else None
                credit_mid = (s_mid - l_mid) if (s_mid is not None and l_mid is not None) else None
                ld = _num(l.get("delta"))
                out.append({
                    "symbol": sym, "spot": spot, "expiry": exp, "dte": dte,
                    "monthly": monthly, "iv30": _num(chain.get("iv30")),
                    "short_strike": ks, "long_strike": kl, "width": width,
                    "moneyness": round(mny, 3),
                    "short_bid": sb, "short_ask": sa, "long_bid": lb, "long_ask": la,
                    "credit": None if credit is None else round(credit, 4),
                    "credit_mid": None if credit_mid is None else round(credit_mid, 4),
                    "credit_pct": (None if credit is None else round(credit / width * 100.0, 2)),
                    "max_loss": (None if credit is None else round((width - credit) * 100.0, 2)),
                    "short_delta": sd,
                    "long_delta": None if ld is None else abs(ld),
                    "otm_prob": None if sd is None else round((1.0 - sd) * 100.0, 2),
                    "short_iv": _num(s.get("iv")),
                    "short_vol": _int(s.get("volume")), "short_oi": _int(s.get("open_interest")),
                    "long_vol": _int(l.get("volume")), "long_oi": _int(l.get("open_interest")),
                    "earnings": earnings, "earnings_before_expiry": earn_before,
                })
    return out


def percentile(history: list[float], current: float) -> float | None:
    """Share of past readings strictly below ``current``, 0..100. None when the
    history is too short to mean anything (``IV_MIN_OBS``)."""
    vals = [v for v in history if v is not None]
    if len(vals) < IV_MIN_OBS or current is None:
        return None
    below = sum(1 for v in vals if v < current)
    return round(below / len(vals) * 100.0, 1)


# ------------------------------------------------------------------- database

def et_today() -> str:
    from .spread_monitor import et_today as _et

    return _et()


def record_iv(db, symbol: str, on: str, iv30: float, spot: float | None,
              source: str = "cboe") -> None:
    """Upsert one day's IV30 (percent). Portable query-then-write, per the
    platform data rule. Does not commit."""
    from ..models import IVHistory

    if iv30 is None:
        return
    row = (db.query(IVHistory)
             .filter(IVHistory.symbol == symbol, IVHistory.on == on)
             .one_or_none())
    if row is None:
        row = IVHistory(symbol=symbol, on=on)
        db.add(row)
    row.iv30 = float(iv30)
    row.spot = spot
    row.source = source


def iv_percentile(db, symbol: str, current: float | None, on: str) -> tuple[float | None, int]:
    """(percentile, observations) from the last 252 trading days BEFORE ``on``."""
    from ..models import IVHistory

    if current is None:
        return None, 0
    cutoff = (_dt.date.fromisoformat(on) - _dt.timedelta(days=370)).isoformat()
    rows = (db.query(IVHistory.iv30)
              .filter(IVHistory.symbol == symbol,
                      IVHistory.on < on, IVHistory.on >= cutoff)
              .order_by(IVHistory.on.desc())
              .limit(252)
              .all())
    hist = [r[0] for r in rows]
    return percentile(hist, current), len(hist)


def universe(db) -> list[str]:
    """S&P 500 + every member's watchlist + every ticker on the MATP board.
    Sorted, de-duplicated. Share classes are normalised to Cboe's dotted form
    handled inside option_quotes (BRK.B works as given)."""
    from ..models import MATPLevel, UserWatchlist
    from . import resources_bridge

    syms: set[str] = set()
    try:
        syms.update(resources_bridge.sp500_symbols())
    except Exception as exc:  # noqa: BLE001 - the scrape is best-effort
        log.warning("sp500 universe unavailable: %s", exc)
    for (s,) in db.query(UserWatchlist.symbol).distinct().all():
        syms.add(s)
    for (s,) in db.query(MATPLevel.symbol).distinct().all():
        syms.add(s)
    return sorted(x.strip().upper() for x in syms if x and x.strip())


def _earnings_for(symbol: str) -> str | None:
    try:
        from .prices import fetch_next_earnings

        e = fetch_next_earnings(symbol)
        return e.get("date") if e else None
    except Exception:  # noqa: BLE001
        return None


def scan_symbol(db, symbol: str, *, on: str, today: _dt.date | None = None,
                record: bool = True) -> tuple[list[dict], str | None]:
    """Fetch one chain, build its candidates, attach IV percentile, and (if
    ``record``) file today's IV30. Returns (rows, error). Does not commit."""
    sym = symbol.strip().upper()
    try:
        ch = option_quotes.fetch_chain(sym)
    except option_quotes.ChainError as exc:
        return [], str(exc)
    except Exception as exc:  # noqa: BLE001
        return [], f"{type(exc).__name__}: {exc}"

    earnings = _earnings_for(sym)
    rows = build_candidates(sym, ch, today=today, earnings=earnings)
    iv30 = _num(ch.get("iv30"))
    pct, n = iv_percentile(db, sym, iv30, on)
    for r in rows:
        r["scan_on"] = on
        r["iv_pct"] = pct
        r["iv_n"] = n
    if record and iv30 is not None:
        record_iv(db, sym, on, iv30, _num(ch.get("spot")))
    return rows, None


def replace_candidates(db, on: str, symbol: str, rows: list[dict]) -> None:
    """Drop this symbol's rows for ``on`` and insert the new ones. Does not commit."""
    from ..models import SpreadCandidate

    (db.query(SpreadCandidate)
       .filter(SpreadCandidate.scan_on == on, SpreadCandidate.symbol == symbol)
       .delete(synchronize_session=False))
    db.add_all(SpreadCandidate(**r) for r in rows)


def prune(db, keep_days: int = KEEP_DAYS) -> int:
    """Delete candidate rows older than the newest ``keep_days`` scan dates."""
    from ..models import SpreadCandidate

    days = [d for (d,) in db.query(SpreadCandidate.scan_on).distinct()
                                .order_by(SpreadCandidate.scan_on.desc()).all()]
    old = days[keep_days:]
    if not old:
        return 0
    return (db.query(SpreadCandidate)
              .filter(SpreadCandidate.scan_on.in_(old))
              .delete(synchronize_session=False))


# Cboe pacing. A 4-thread burst drew HTTP 429 after ~24 requests (2026-09-12);
# one request every PAUSE seconds with a backoff on 429 is the working shape.
PAUSE = 1.5
RETRIES = 3


def run_scan(db, *, symbols: list[str] | None = None, on: str | None = None,
             workers: int = 1, pause: float = PAUSE, fresh: bool = True,
             progress=None) -> dict:
    """The nightly job. Fetches every chain one at a time, ``pause`` seconds
    apart (Cboe rate-limits bursts), files the candidates and today's IV30 per
    symbol, records a ``SpreadScan`` row.

    ``workers`` > 1 is allowed for a short hand-picked list, never the universe.
    The database is written from this thread only, so SQLite never sees two
    writers.
    """
    from ..models import SpreadScan

    if fresh:
        option_quotes.clear_cache()
    on = on or et_today()
    today = _dt.date.fromisoformat(on)
    syms = symbols if symbols is not None else universe(db)

    scan = SpreadScan(scan_on=on, symbols=len(syms))
    db.add(scan)
    db.commit()

    # Phase 1 (threads): fetch chains + earnings, no DB. Phase 2 (here): DB.
    fetched: dict[str, tuple[dict | None, str | None, str | None]] = {}
    lock = threading.Lock()

    def _fetch(sym: str):
        try:
            ch = option_quotes.fetch_chain(sym, retries=RETRIES)
            err = None
        except option_quotes.ChainError as exc:
            ch, err = None, str(exc)
        except Exception as exc:  # noqa: BLE001
            ch, err = None, f"{type(exc).__name__}: {exc}"
        earn = _earnings_for(sym) if ch is not None else None
        with lock:
            fetched[sym] = (ch, err, earn)
        if progress:
            progress(sym, err)
        if pause > 0:
            time.sleep(pause)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(_fetch, syms))

    priced = errors = total = 0
    for sym in syms:
        ch, err, earn = fetched.get(sym, (None, "not fetched", None))
        if ch is None:
            errors += 1
            log.info("  %-6s skipped: %s", sym, err)
            continue
        rows = build_candidates(sym, ch, today=today, earnings=earn)
        iv30 = _num(ch.get("iv30"))
        pct, n = iv_percentile(db, sym, iv30, on)
        for r in rows:
            r["scan_on"] = on
            r["iv_pct"] = pct
            r["iv_n"] = n
        replace_candidates(db, on, sym, rows)
        if iv30 is not None:
            record_iv(db, sym, on, iv30, _num(ch.get("spot")))
        priced += 1
        total += len(rows)
    pruned = prune(db)

    scan.finished_at = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    scan.priced = priced
    scan.candidates = total
    scan.errors = errors
    scan.note = f"pruned {pruned} old rows" if pruned else None
    db.commit()
    return {"scan_on": on, "symbols": len(syms), "priced": priced,
            "candidates": total, "errors": errors, "pruned": pruned}


# --------------------------------------------------------------------- reading

def latest_scan(db):
    from ..models import SpreadScan

    return (db.query(SpreadScan).filter(SpreadScan.finished_at.isnot(None))
              .order_by(SpreadScan.scan_on.desc(), SpreadScan.id.desc()).first())


def clean_filter(raw: dict) -> dict:
    """A complete, typed filter dict from whatever the form or the saved prefs
    hold. Unknown keys dropped, bad values fall back to the default."""
    out = dict(DEFAULT_FILTER)
    for k, dflt in DEFAULT_FILTER.items():
        if k not in raw:
            continue
        v = raw[k]
        if isinstance(dflt, bool):
            out[k] = (v in (True, "1", "true", "on", "yes")) if not isinstance(v, bool) else v
        elif isinstance(dflt, int):
            try:
                out[k] = int(float(v))
            except (TypeError, ValueError):
                pass
        else:
            f = _num(v)
            if f is not None:
                out[k] = f
    if out["dte_max"] < out["dte_min"]:
        out["dte_min"], out["dte_max"] = out["dte_max"], out["dte_min"]
    if out["moneyness_max"] < out["moneyness_min"]:
        out["moneyness_min"], out["moneyness_max"] = out["moneyness_max"], out["moneyness_min"]
    return out


def query_candidates(db, on: str, f: dict, *, symbol: str | None = None,
                     order: str = "credit_pct", desc: bool = True, limit: int = 500):
    """Apply a cleaned filter as WHERE clauses. Returns ORM rows."""
    from sqlalchemy import or_

    from ..models import SpreadCandidate as C

    q = db.query(C).filter(C.scan_on == on)
    if symbol:
        q = q.filter(C.symbol == symbol.strip().upper())
    q = q.filter(C.dte >= f["dte_min"], C.dte <= f["dte_max"])
    if f["monthly_only"]:
        q = q.filter(C.monthly.is_(True))
    q = q.filter(C.moneyness >= f["moneyness_min"], C.moneyness <= f["moneyness_max"])
    q = q.filter(C.short_vol >= f["short_vol_min"], C.short_oi >= f["short_oi_min"])
    q = q.filter(C.long_vol >= f["long_vol_min"], C.long_oi >= f["long_oi_min"])
    q = q.filter(C.short_bid >= f["short_bid_min"], C.long_ask >= f["long_ask_min"])
    q = q.filter(C.otm_prob >= f["otm_prob_min"])
    if f["credit_pct_min"] > 0:
        q = q.filter(C.credit_pct >= f["credit_pct_min"])
    if f["iv_pct_min"] > 0:
        if f["include_no_iv"]:
            q = q.filter(or_(C.iv_pct >= f["iv_pct_min"], C.iv_pct.is_(None)))
        else:
            q = q.filter(C.iv_pct >= f["iv_pct_min"])
    if f["flag_earnings"]:
        q = q.filter(C.earnings_before_expiry.is_(False))

    cols = {"credit_pct": C.credit_pct, "credit": C.credit, "iv_pct": C.iv_pct,
            "otm_prob": C.otm_prob, "dte": C.dte, "symbol": C.symbol,
            "short_vol": C.short_vol, "short_oi": C.short_oi, "moneyness": C.moneyness,
            "width": C.width, "max_loss": C.max_loss}
    col = cols.get(order, C.credit_pct)
    q = q.order_by(col.desc() if desc else col.asc(), C.symbol, C.expiry, C.short_strike.desc())
    return q.limit(limit).all()
