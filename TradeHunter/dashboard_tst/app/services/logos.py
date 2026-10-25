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
"""
from __future__ import annotations

import hashlib
import logging
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


__all__ = ["LOGO_DIR", "SOURCES", "KEEP_DAYS", "MISS_DAYS", "clean_symbol", "fetch", "logo_png", "initials_svg"]
