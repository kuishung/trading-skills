"""Ticker logos (v4.221) - the company's logo beside a ticker wherever the platform shows one.

The user (2026-10-25): *"I will want the logo of the ticker in wherever it is shown be it in the watchlist or in a
chart or in the vol surface"*.

Where the images come from - two free public logo services that need no key (checked 2026-10-25 on AMD, AAPL,
NVDA, BRK-B, SPY; both answer 404 for an unknown ticker):

1. Financial Modeling Prep, ``https://financialmodelingprep.com/image-stock/<SYM>.png`` - the brand logo;
2. Parqet, ``https://assets.parqet.com/logos/symbol/<SYM>?format=png`` - a square tile, when the first has none.

Massive (Polygon) has logos in its ticker details too, but every lookup would spend the Stocks Basic allowance
(5 requests a minute) the screener collector depends on, so it is not used.

The server fetches a ticker's logo ONCE and keeps it in ``state/logos/<SYM>.png`` (refreshed after ``KEEP_DAYS``);
a ticker neither service knows is remembered (``<SYM>.none``) and asked again after ``MISS_DAYS``. The pages never
call the outside services themselves (``GET /logo/<SYM>``, ``routes/logos.py``) - no member's browser talks to a
third party and a slow service only delays the first view of a logo. With no logo, the answer is a small SVG with
the ticker's first letters on a colour picked from the ticker, so every place still shows a badge.

v4.222 (user: *"when loading the logo it takes too long can we download it in hermes so it load fast?"*): a page never
waits for a download any more. ``GET /logo/<SYM>`` answers at once - the stored logo, or the letters badge (kept by
the browser two minutes) while the ticker is queued - and a background worker in the web app downloads the queue,
one logo every ``FETCH_GAP_S``. At start and every ``WARM_EVERY_S`` it queues every ticker the platform shows that
has no fresh logo: the members' watchlists, MATP tickers and Curated calls first, then every stock and ETF in the
options data (``warm_symbols``). ``status()`` feeds Settings -> Data Ingest -> Ticker Logos.
"""
from __future__ import annotations

import collections
import datetime as _dt
import hashlib
import logging
import os
import re
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

LOGO_DIR = Path(__file__).resolve().parents[2] / "state" / "logos"
SOURCES = (
    ("fmp", "https://financialmodelingprep.com/image-stock/{sym}.png"),
    ("parqet", "https://assets.parqet.com/logos/symbol/{sym}?format=png"),
)
KEEP_DAYS = 30            # a stored logo is fetched again after this many days (kept when the fetch fails)
MISS_DAYS = 3             # a ticker with no logo anywhere is asked again after this many days
TIMEOUT_S = 6.0
MIN_BYTES = 120           # an "image" smaller than this is an error page or an empty placeholder
MAX_BYTES = 400_000
FETCH_GAP_S = 0.4          # between two downloads (both services are public; be polite)
WARM_EVERY_S = 12 * 3600   # how often the background pass looks for tickers without a fresh logo
WARM_DELAY_S = 20          # the first pass waits this long after the web app starts
ENV_OFF = "TST_LOGO_WARM"  # "0" = no background downloads (the tests set it)
_SYM_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,11}$")
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def clean_symbol(sym) -> str | None:
    """``brk.b`` -> ``BRK-B``; None for anything that is not a ticker."""
    s = str(sym or "").strip().upper().replace(".", "-").replace("/", "-")
    return s if _SYM_RE.match(s) else None


def _lock(sym: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lk = _LOCKS.get(sym)
        if lk is None:
            lk = _LOCKS[sym] = threading.Lock()
        return lk


def _fetch_one(url: str) -> bytes | None:
    import httpx  # noqa: PLC0415

    try:
        r = httpx.get(url, timeout=TIMEOUT_S, follow_redirects=True, headers={"User-Agent": "TradeHunter/1.0"})
    except Exception as exc:  # noqa: BLE001 - a logo is decoration; any failure means "none from here"
        log.info("logo fetch %s failed: %s", url, type(exc).__name__)
        return None
    ctype = (r.headers.get("content-type") or "").lower()
    if r.status_code != 200 or not ctype.startswith("image/"):
        return None
    body = r.content
    if not MIN_BYTES <= len(body) <= MAX_BYTES or not body.startswith(b"\x89PNG"):
        return None
    return body


def fetch(sym: str) -> tuple[bytes | None, str | None]:
    """The logo of ``sym`` from the first source that has one: ``(png bytes, source)`` or ``(None, None)``."""
    for name, tpl in SOURCES:
        body = _fetch_one(tpl.format(sym=sym))
        if body:
            return body, name
    return None, None


def _age_days(p: Path) -> float:
    try:
        return (time.time() - p.stat().st_mtime) / 86400.0
    except OSError:
        return 1e9


def logo_png(sym, *, root: Path | None = None, fetcher=None) -> bytes | None:
    """The stored PNG of ``sym`` (fetched once, refreshed after ``KEEP_DAYS``), or None when no service has one."""
    s = clean_symbol(sym)
    if s is None:
        return None
    d = Path(root) if root is not None else LOGO_DIR
    png, miss = d / (s + ".png"), d / (s + ".none")
    if png.exists() and _age_days(png) < KEEP_DAYS:
        return png.read_bytes()
    if not png.exists() and miss.exists() and _age_days(miss) < MISS_DAYS:
        return None
    with _lock(s):
        if png.exists() and _age_days(png) < KEEP_DAYS:          # another request fetched it meanwhile
            return png.read_bytes()
        body, src = (fetcher or fetch)(s)
        try:
            d.mkdir(parents=True, exist_ok=True)
            if body:
                tmp = png.with_suffix(".tmp")
                tmp.write_bytes(body)
                tmp.replace(png)
                miss.unlink(missing_ok=True)
                log.info("logo %s stored from %s (%d bytes)", s, src, len(body))
                return body
            if png.exists():                                       # a refresh that failed: keep the old one
                png.touch()
                return png.read_bytes()
            miss.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
        except OSError as exc:
            log.warning("logo %s could not be stored: %s", s, exc)
            return body
    return None


# ───────────────────────── v4.222: the background download ─────────────────────────

_Q: collections.OrderedDict = collections.OrderedDict()     # symbol -> None, first in first out, no duplicates
_Q_LOCK = threading.Lock()
_WAKE = threading.Event()
_STATE = {"started": None, "warm_runs": 0, "last_warm_at": None, "last_warm_queued": 0,
          "fetched": 0, "found": 0, "missed": 0, "last_fetch_at": None, "last_symbol": None, "last_error": None}


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def is_fresh(sym: str, *, root: Path | None = None) -> bool:
    """True when ``sym`` needs no download: a logo younger than ``KEEP_DAYS`` or a recent "none"."""
    d = Path(root) if root is not None else LOGO_DIR
    png, miss = d / (sym + ".png"), d / (sym + ".none")
    if png.exists():
        return _age_days(png) < KEEP_DAYS
    return miss.exists() and _age_days(miss) < MISS_DAYS


def stored_png(sym, *, root: Path | None = None) -> bytes | None:
    """The stored logo of ``sym`` at once - never a download (an old one counts until it is refreshed)."""
    s = clean_symbol(sym)
    if s is None:
        return None
    png = (Path(root) if root is not None else LOGO_DIR) / (s + ".png")
    try:
        return png.read_bytes() if png.exists() else None
    except OSError:
        return None


def enqueue(symbols, *, front: bool = False, root: Path | None = None) -> int:
    """Queue the tickers that need a download; returns how many were added. ``front`` = ahead of the rest (a page
    is waiting for it)."""
    n = 0
    with _Q_LOCK:
        for sym in symbols or ():
            c = clean_symbol(sym)
            if not c or is_fresh(c, root=root):
                continue
            if c in _Q:
                if front:
                    _Q.move_to_end(c, last=False)
                continue
            _Q[c] = None
            if front:
                _Q.move_to_end(c, last=False)
            n += 1
    if n or front:
        _WAKE.set()
    return n


def queued() -> list[str]:
    with _Q_LOCK:
        return list(_Q)


def _next() -> str | None:
    with _Q_LOCK:
        if not _Q:
            return None
        sym, _v = _Q.popitem(last=False)
        return sym


def work_one(sym: str, *, root: Path | None = None, fetcher=None) -> bytes | None:
    """Download one queued ticker and count it."""
    try:
        body = logo_png(sym, root=root, fetcher=fetcher)
    except Exception as exc:  # noqa: BLE001 - one ticker never stops the worker
        _STATE["last_error"] = "%s: %s" % (sym, type(exc).__name__)
        log.exception("logo download for %s failed", sym)
        return None
    _STATE["fetched"] += 1
    _STATE["found" if body else "missed"] += 1
    _STATE["last_symbol"], _STATE["last_fetch_at"] = sym, _now_iso()
    return body


def warm_symbols() -> list[str]:
    """Every ticker the platform shows, the members' own first: watchlists, MATP tickers, Curated calls, then the
    stocks and ETFs of the options data. Soft: a database that cannot be read adds nothing."""
    first: list[str] = []
    try:
        from ..db import SessionLocal  # noqa: PLC0415
        from ..models import CuratedTicker, MATPLevel, UserWatchlist  # noqa: PLC0415

        db = SessionLocal()
        try:
            for model in (UserWatchlist, MATPLevel, CuratedTicker):
                first.extend(sym for (sym,) in db.query(model.symbol).distinct())
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        log.info("logo warm: the platform tickers could not be read (%s)", type(exc).__name__)
    rest: list[str] = []
    try:
        from .. import screener_db  # noqa: PLC0415
        from ..screener_models import ScrUnderlying as U  # noqa: PLC0415

        with screener_db.session() as sdb:
            rest = [sym for (sym,) in sdb.query(U.symbol)
                    .filter((U.sec_type.is_(None)) | (U.sec_type.in_(("stock", "etf"))))
                    .order_by(U.symbol)]
    except Exception as exc:  # noqa: BLE001
        log.info("logo warm: the options-data tickers could not be read (%s)", type(exc).__name__)
    seen, out = set(), []
    for sym in first + rest:
        c = clean_symbol(sym)
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def warm(symbols=None, *, root: Path | None = None) -> int:
    """Queue every ticker without a fresh logo (``symbols`` or ``warm_symbols()``); returns how many."""
    n = enqueue(warm_symbols() if symbols is None else symbols, root=root)
    _STATE["warm_runs"] += 1
    _STATE["last_warm_at"], _STATE["last_warm_queued"] = _now_iso(), n
    return n


def _worker() -> None:
    next_warm = time.monotonic() + WARM_DELAY_S
    while True:
        if time.monotonic() >= next_warm:
            try:
                warm()
            except Exception:  # noqa: BLE001
                log.exception("logo warm failed")
            next_warm = time.monotonic() + WARM_EVERY_S
        sym = _next()
        if sym is None:
            _WAKE.wait(timeout=max(1.0, min(60.0, next_warm - time.monotonic())))
            _WAKE.clear()
            continue
        if not is_fresh(sym):
            work_one(sym)
            time.sleep(FETCH_GAP_S)


def start_background() -> bool:
    """Start the download worker once (the web app's start-up). Off when ``TST_LOGO_WARM=0``."""
    if (os.environ.get(ENV_OFF) or "").strip() == "0" or _STATE["started"]:
        return False
    _STATE["started"] = _now_iso()
    threading.Thread(target=_worker, daemon=True, name="logo-download").start()
    return True


def status(*, root: Path | None = None) -> dict:
    """Settings -> Data Ingest -> Ticker Logos: what is stored, what is still queued, what the worker did."""
    d = Path(root) if root is not None else LOGO_DIR
    stored = missing = 0
    newest = None
    try:
        for f in d.iterdir():
            if f.suffix == ".png":
                stored += 1
                m = f.stat().st_mtime
                newest = m if newest is None or m > newest else newest
            elif f.suffix == ".none":
                missing += 1
    except OSError:
        pass
    with _Q_LOCK:
        nq = len(_Q)
    return {**_STATE, "stored": stored, "missing": missing, "queued": nq, "dir": str(d),
            "newest_at": (_dt.datetime.fromtimestamp(newest, _dt.timezone.utc).replace(microsecond=0).isoformat()
                          if newest else None),
            "sources": [n for n, _u in SOURCES], "keep_days": KEEP_DAYS, "miss_days": MISS_DAYS,
            "warm_every_h": WARM_EVERY_S // 3600, "off": (os.environ.get(ENV_OFF) or "").strip() == "0"}


def initials_svg(sym) -> str:
    """The fallback badge: the ticker's first letters on a colour picked from the ticker (stable per ticker)."""
    s = clean_symbol(sym) or "?"
    text = s.split("-")[0][:2] if len(s) > 1 else s
    hue = int(hashlib.md5(s.encode("ascii", "ignore")).hexdigest()[:4], 16) % 360
    fs = 30 if len(text) > 1 else 36
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" role="img" aria-label="%s">'
            '<rect width="64" height="64" rx="12" fill="hsl(%d,45%%,38%%)"/>'
            '<text x="32" y="33" text-anchor="middle" dominant-baseline="central" font-family="Segoe UI,Arial,sans-serif" '
            'font-size="%d" font-weight="700" fill="#fff">%s</text></svg>') % (s, hue, fs, text)


__all__ = ["LOGO_DIR", "SOURCES", "KEEP_DAYS", "MISS_DAYS", "clean_symbol", "fetch", "logo_png", "initials_svg",
           "stored_png", "is_fresh", "enqueue", "queued", "work_one", "warm", "warm_symbols", "start_background",
           "status"]
