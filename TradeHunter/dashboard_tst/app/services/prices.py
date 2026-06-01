"""Live daily OHLC fetch for the MATP price chart.

The chart fetches candles **live** from the Yahoo Finance chart API rather than
from the stored parquet, so it works for any symbol without bars being seeded /
Resilio-synced on the host. Uses ``httpx`` (already a dependency) — no API key.
A short in-process TTL cache avoids hammering Yahoo on repeated page loads.

Returns lightweight-charts-ready dicts: {time:'YYYY-MM-DD', open, high, low,
close}. Any failure (network, rate-limit, bad symbol) yields [] so the chart
degrades to an empty state instead of erroring.
"""
from __future__ import annotations

import datetime as _dt
import time

import httpx

_CACHE: dict[str, tuple[float, list[dict]]] = {}
_TTL = 600.0  # seconds — analyst-target charts don't need sub-10-min freshness
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# query1/query2 are interchangeable hosts; try both before giving up.
_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")


def fetch_daily_ohlc(symbol: str, *, rng: str = "2y") -> list[dict]:
    sym = symbol.strip().upper()
    if not sym:
        return []
    now = time.time()
    hit = _CACHE.get(sym)
    if hit and hit[0] > now:
        return hit[1]

    bars = _fetch(sym, rng)
    # Only cache non-empty results, so a transient failure isn't sticky.
    if bars:
        _CACHE[sym] = (now + _TTL, bars)
    return bars


# --- Next earnings date (Yahoo calendarEvents) ------------------------------
# Yahoo's quoteSummary now requires a consent cookie + a matching crumb. We do
# that handshake once (cached ~1h) and reuse it across symbols. Everything is
# soft-fail: any hiccup -> None, and the chart simply omits the earnings line.
_EARN_CACHE: dict[str, tuple[float, dict | None]] = {}
_EARN_TTL = 6 * 3600.0   # earnings dates move slowly
_EARN_MISS_TTL = 900.0   # but don't hammer on a miss
_YH_SESSION: dict = {"crumb": None, "cookies": None, "exp": 0.0}


def _yahoo_session():
    """(crumb, cookies) for authenticated quoteSummary, cached. -> (None, None)
    on any failure."""
    now = time.time()
    if _YH_SESSION["crumb"] and _YH_SESSION["exp"] > now:
        return _YH_SESSION["crumb"], _YH_SESSION["cookies"]
    try:
        with httpx.Client(headers={"User-Agent": _UA}, timeout=6.0, follow_redirects=True) as c:
            c.get("https://fc.yahoo.com")  # sets the A1/A3 consent cookies
            r = c.get("https://query2.finance.yahoo.com/v1/test/getcrumb")
            crumb = (r.text or "").strip()
            if not crumb or "<" in crumb:  # an HTML error page = no usable crumb
                return None, None
            _YH_SESSION.update(crumb=crumb, cookies=c.cookies, exp=now + 3600.0)
            return crumb, c.cookies
    except Exception:
        return None, None


def fetch_next_earnings(symbol: str) -> dict | None:
    """Soonest upcoming earnings for `symbol` (today or later) from Yahoo, cached
    + soft-fail. Returns ``{"date": "YYYY-MM-DD", "days": <int from today>}`` or
    None when unavailable."""
    sym = symbol.strip().upper()
    if not sym:
        return None
    now = time.time()
    hit = _EARN_CACHE.get(sym)
    if hit and hit[0] > now:
        return hit[1]
    res = _fetch_next_earnings(sym)
    _EARN_CACHE[sym] = (now + (_EARN_TTL if res else _EARN_MISS_TTL), res)
    return res


def _fetch_next_earnings(sym: str) -> dict | None:
    crumb, cookies = _yahoo_session()
    if not crumb:
        return None
    today = _dt.date.today()
    for host in _HOSTS:
        try:
            r = httpx.get(
                f"https://{host}/v10/finance/quoteSummary/{sym}",
                params={"modules": "calendarEvents", "crumb": crumb},
                headers={"User-Agent": _UA}, cookies=cookies, timeout=6.0,
            )
            if r.status_code != 200:
                continue
            res = (r.json().get("quoteSummary") or {}).get("result") or []
            if not res:
                continue
            raw_dates = (
                ((res[0].get("calendarEvents") or {}).get("earnings") or {}).get("earningsDate") or []
            )
            days = []
            for d in raw_dates:
                raw = d.get("raw")
                if raw is not None:
                    days.append(_dt.datetime.utcfromtimestamp(raw).date())
            days = sorted(set(days))
            nxt = next((d for d in days if d >= today), days[-1] if days else None)
            if nxt is None:
                continue
            return {"date": nxt.isoformat(), "days": (nxt - today).days}
        except Exception:
            continue
    return None


_US_TICKER = __import__("re").compile(r"^[A-Z]{1,5}([.\-][A-Z])?$")


def search_tickers(q: str, *, limit: int = 8) -> list[dict]:
    """Typeahead: match a ticker or company name to US equities via Yahoo's
    search API. Returns [{symbol, name}] (US stocks only). [] on any failure."""
    q = (q or "").strip()
    if not q:
        return []
    try:
        r = httpx.get(
            "https://query1.finance.yahoo.com/v1/finance/search",
            params={"q": q, "quotesCount": limit + 6, "newsCount": 0},
            headers={"User-Agent": _UA}, timeout=6.0,
        )
        r.raise_for_status()
        out: list[dict] = []
        for it in (r.json().get("quotes") or []):
            if it.get("quoteType") not in ("EQUITY", "ETF"):  # stocks + ETFs
                continue
            sym = (it.get("symbol") or "").upper()
            if not _US_TICKER.match(sym):  # US-listed plain tickers only
                continue
            name = it.get("shortname") or it.get("longname") or ""
            kind = "ETF" if it.get("quoteType") == "ETF" else ""
            out.append({"symbol": sym, "name": name, "kind": kind})
            if len(out) >= limit:
                break
        return out
    except Exception:
        return []


def _fetch(sym: str, rng: str) -> list[dict]:
    params = {"range": rng, "interval": "1d"}
    headers = {"User-Agent": _UA}
    for host in _HOSTS:
        url = f"https://{host}/v8/finance/chart/{sym}"
        try:
            r = httpx.get(url, params=params, headers=headers, timeout=8.0)
            r.raise_for_status()
            res = (r.json().get("chart", {}).get("result") or [None])[0]
            if not res:
                continue
            ts = res.get("timestamp") or []
            quote = (res.get("indicators", {}).get("quote") or [{}])[0]
            o, h, l, c = (
                quote.get("open") or [],
                quote.get("high") or [],
                quote.get("low") or [],
                quote.get("close") or [],
            )
            out: list[dict] = []
            for i, t in enumerate(ts):
                try:
                    bo, bh, bl, bc = o[i], h[i], l[i], c[i]
                except IndexError:
                    continue
                if None in (bo, bh, bl, bc):
                    continue  # Yahoo leaves gaps as null
                day = _dt.datetime.utcfromtimestamp(t).strftime("%Y-%m-%d")
                out.append(
                    {"time": day, "open": round(bo, 2), "high": round(bh, 2),
                     "low": round(bl, 2), "close": round(bc, 2)}
                )
            if out:
                return out
        except Exception:
            continue
    return []
