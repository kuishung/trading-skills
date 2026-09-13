"""The official RRG's own numbers, read from the feed the embed itself reads.

The Sector page's left panel groups sectors by the quadrant they sit in on the
Optuma RRG embed. Until v4.70 those coordinates came from a seed file in the
repo (``rrg_seed.json``) or a hand POST to ``/api/rrg`` - so they went stale the
moment the chart moved on, and the panel contradicted the chart beside it
(user, 2026-09-13: the chart showed only Industrials and Utilities lagging, the
panel listed five).

How the embed gets its data (traced 2026-09-13 in the browser): the page loads a
PUBLIC listing, ``statestreet.json`` on S3, which maps each universe and
timeframe to a CloudFront-signed JSON file (signature valid ~2 days, re-issued
by the listing). That file holds, per sector, a dated series of
``[date, rs_ratio, rs_momentum, ...]``. No key, no login. So the panel can read
exactly what the chart draws, as of the same date.

Codes in the US file are ``SXLC``, ``SXLK`` ... - the SPDR sector tickers with a
leading ``S``; they map to ``XLC``, ``XLK`` ... Real Estate (XLRE) is not in
that universe, so it keeps the panel's own estimate, as before.

Soft-fail throughout: any problem returns ``{}`` and the panel falls back to the
seed / posted rows / its own estimate, exactly as it did before this module.
"""
from __future__ import annotations

import logging
import threading
import time

import httpx

log = logging.getLogger("dashboard_tst.rrg_feed")

LIST_URL = "https://s3.eu-central-1.amazonaws.com/eu.rrg.optuma.com/list/statestreet.json"
UNIVERSE = "SPDR S&P US Sectors ETF"
_TF_KEY = {"weekly": "1 Week", "daily": "1 Day"}
SOURCE = "optuma-feed"

# The chart updates once a day after the close; re-reading more often than every
# few hours buys nothing and hammers a feed we do not own.
_TTL = 4 * 3600.0
_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()
_UA = "TradeHunter/1.0 (+https://app.tradehunter.net; sector rotation panel)"


def _code_to_symbol(code: str) -> str | None:
    """``SXLC`` -> ``XLC``. Anything that does not map to a known SPDR sector is
    dropped rather than guessed."""
    from .etf import ETF_UNIVERSE

    c = (code or "").strip().upper()
    known = {s for s, _ in ETF_UNIVERSE}
    if c in known:
        return c
    if c.startswith("S") and c[1:] in known:
        return c[1:]
    return None


def fetch_points(timeframe: str = "weekly", *, ttl: float = _TTL) -> dict:
    """``{symbol: {rs_ratio, rs_momentum, as_of, source}}`` for the latest date in
    the feed. Cached per timeframe; ``{}`` on any failure."""
    tf = (timeframe or "weekly").strip().lower()
    key = _TF_KEY.get(tf)
    if key is None:
        return {}
    now = time.time()
    with _lock:
        hit = _cache.get(tf)
        if hit and (now - hit[0]) < ttl:
            return hit[1]
    out: dict = {}
    try:
        lst = httpx.get(LIST_URL, headers={"User-Agent": _UA}, timeout=20.0)
        lst.raise_for_status()
        url = (lst.json().get("symbollists") or {}).get(UNIVERSE, {}).get(key)
        if not url:
            raise ValueError(f"no {UNIVERSE} / {key} in the listing")
        r = httpx.get(url, headers={"User-Agent": _UA}, timeout=30.0)
        r.raise_for_status()
        for dl in r.json().get("datalists") or []:
            sym = _code_to_symbol(dl.get("code"))
            data = dl.get("data") or []
            if not sym or not data:
                continue
            last = data[-1]
            try:
                x, y = float(last[1]), float(last[2])
            except (TypeError, ValueError, IndexError):
                continue
            # same sanity check as /api/rrg: an RRG coordinate sits near 100
            if not (50.0 <= x <= 150.0 and 50.0 <= y <= 150.0):
                continue
            out[sym] = {"rs_ratio": x, "rs_momentum": y,
                        "as_of": str(last[0])[:10], "source": SOURCE}
    except Exception as exc:  # noqa: BLE001 - network, JSON shape, anything
        log.warning("rrg feed (%s) unavailable: %s", tf, exc)
        return {}
    with _lock:
        _cache[tf] = (now, out)
    return out


def refresh_into_db(db, timeframes=("weekly", "daily")) -> dict:
    """Upsert the feed's latest points into ``rrg_points`` so the panel keeps the
    last good reading if the feed is down later. Portable query-then-write.
    Returns ``{timeframe: n_rows}``. Commits."""
    from ..models import RRGPoint

    done = {}
    for tf in timeframes:
        pts = fetch_points(tf)
        n = 0
        for sym, p in pts.items():
            row = (db.query(RRGPoint)
                     .filter(RRGPoint.symbol == sym, RRGPoint.timeframe == tf)
                     .one_or_none())
            if row is None:
                row = RRGPoint(symbol=sym, timeframe=tf)
                db.add(row)
            elif str(row.as_of or "") > p["as_of"]:
                continue        # never overwrite a newer reading with an older one
            row.rs_ratio = p["rs_ratio"]
            row.rs_momentum = p["rs_momentum"]
            row.as_of = p["as_of"]
            row.source = p["source"]
            n += 1
        done[tf] = n
    db.commit()
    return done
