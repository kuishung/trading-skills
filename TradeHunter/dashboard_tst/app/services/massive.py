"""The Massive (formerly Polygon.io) REST client - Options v2's data source from v4.134
(OPTIONS_V2_DESIGN.md §13.2, Part M1).

What it reads (the user's plans, 2026-10-10)
--------------------------------------------
* **Options Starter** - the whole-chain snapshot (greeks, IV, open interest, the day
  bar; 15-minute delayed; NO bid/ask - ``last_quote`` comes back only on plans with
  quotes) and daily bars of option contracts (expired ones too, 2 years). Unlimited
  requests; this client still keeps itself to ``max_rps`` a second.
* **Stocks Basic** (free) - end-of-day daily bars, ~5 requests a minute: every
  ``/v2/aggs/ticker/<stock>`` call goes through a separate per-minute window.
* For the Options Screener (OPTIONS_SCREENER_DESIGN.md §4, v4.136): the options
  contracts reference list (``option_underlyings`` - the universe, not paced), the
  grouped daily bars of every stock (``grouped_daily``) and the ticker reference list
  (``reference_tickers``) - both paced like a stock request.
* Stocks Basic serves about two years of history: ``stocks_plan_start(day)`` is the
  oldest session worth asking for (``STOCKS_PLAN_DAYS`` back - a margin inside the plan).

The universe walk (v4.137)
--------------------------
``option_underlyings`` walks every live contract within 60 days, one 1,000-row page at a
time (~500-800 pages; ``contract_type=call`` halves that - every listed underlying has
calls and the counts only order the passes). It is long, so it is RESUMABLE and VISIBLE:
``on_page(page_no, counts, next_url)`` after every page (a callback error never stops the
walk), ``start_url`` resumes at a page's URL, and a failure raises a ``MassiveError``
carrying ``.counts`` (the pages read so far) and ``.resume_url`` (the page that failed;
None when the walk cannot be resumed). Its requests get a longer timeout (60 s) and more
retries (4, backing off 2 / 4 / 8 / 16 s) than a chain read.

The key
-------
``TST_MASSIVE_API_KEY`` (``app/.env`` on Hermes, gitignored). It is sent ONLY as
``Authorization: Bearer <key>`` - never as the ``apiKey`` query parameter, never in a
URL, a log line or an exception text (every message passes ``_scrub``). A ``next_url``
is fetched exactly as Massive gave it (it carries the cursor; the key stays in the
header) and only when it points at the configured host, so the key is never sent
anywhere else. ``TST_MASSIVE_BASE_URL`` (default ``https://api.massive.com``) lets tests
and the browser check point at a local fake.

Errors
------
Everything that goes wrong is a ``MassiveError`` with a ``kind``:
``config`` (no key on this PC), ``auth`` (401), ``plan`` (403 - the plan does not
include that endpoint, or that date: ``.window`` is True when Massive's message speaks of
the data "timeframe", e.g. a Stocks Basic day older than two years; the server's message
is kept in the text), ``rate`` (429 that did not clear: ``Retry-After`` or 15 s doubling
to 2 min, 4 retries), ``http`` (any other status; 5xx retried twice; a body that is not
JSON; a 2xx body whose ``status`` is ``ERROR`` - ``NOT_AUTHORIZED`` there is ``plan``; a
``next_url`` to another host), ``network`` (connection / timeout, retried twice).
``.status`` is the HTTP status when there was one. A failure inside a paged read also
carries ``.resume_url`` - the URL of the page that failed.

Sync on purpose: the collector is a sync loop (the web app's "Refresh now", removed with
the Options page in v4.135, ran it in a worker thread). ``http`` (an ``httpx.Client``-like object), ``sleep`` and ``now`` are
injectable so the tests run with no network and a fake clock.
"""
from __future__ import annotations

import collections
import datetime as _dt
import math
import os
import re
import threading
import time
from urllib.parse import quote as _urlquote
from urllib.parse import urlencode, urljoin, urlsplit

from . import clock

try:   # app/.env into the environment (no-op when absent) - the settings module does it
    from .. import config as _config  # noqa: F401
except Exception:  # noqa: BLE001 - the client also works outside the app package
    _config = None

DEFAULT_BASE_URL = "https://api.massive.com"
ENV_KEY = "TST_MASSIVE_API_KEY"
ENV_BASE = "TST_MASSIVE_BASE_URL"
ENV_QUOTES = "TST_MASSIVE_QUOTES"

SNAPSHOT_LIMIT = 250          # the chain snapshot's largest page
AGGS_LIMIT = 50000            # daily bars: two years fit one page many times over
MAX_PAGES = 400               # a runaway cursor stops here (SPY's whole chain is ~40 pages)
STOCK_WINDOW_S = 60.0         # Stocks Basic: N requests per rolling minute ...
STOCK_WINDOW_PAD_S = 0.5      # ... plus a little slack for the server's own clock
RATE_FIRST_S = 15.0           # 429 with no Retry-After: wait 15 s, doubling ...
RATE_MAX_S = 120.0            # ... up to 2 min (a Retry-After is capped here too)
RATE_RETRIES = 4              # 429s tolerated per request before MassiveError("rate")
SERVER_RETRIES = 2            # 5xx retried twice
NETWORK_RETRIES = 2           # a dropped connection / timeout retried twice
RETRY_BASE_S = 1.0            # 1 s, 2 s between those retries

KINDS = ("auth", "plan", "rate", "http", "network", "config")
_LABELS = (("/v3/snapshot/options/", "the options chain snapshot"),
           ("/v2/aggs/ticker/O:", "option daily bars"),
           ("/v2/aggs/ticker/", "stock daily bars"),
           ("/v2/aggs/grouped/", "grouped daily stock bars"),
           ("/v3/reference/options/contracts", "the options contracts list"),
           ("/v3/reference/tickers", "the ticker reference list"))

REFERENCE_LIMIT = 1000        # the reference endpoints' largest page
UNIVERSE_MAX_PAGES = 5000     # the whole US options list within 60 days is ~500-800 pages
UNIVERSE_TIMEOUT_S = 60.0     # a deep contracts-list page can be slow: its own timeout ...
UNIVERSE_RETRIES = 4          # ... and retries (network and 5xx) ...
UNIVERSE_BACKOFF_S = 2.0      # ... waiting 2, 4, 8, 16 s
STOCKS_PLAN_DAYS = 725        # Stocks Basic: ~2 years of history; ask no further back (a margin inside)


class MassiveError(RuntimeError):
    """A Massive request that failed. ``kind`` is one of ``KINDS``; ``status`` the
    HTTP status when the server answered. The text never carries the key.

    Extra facts (default None / False / 0): ``window`` - a ``plan`` refusal whose message
    speaks of the data timeframe (a date outside the plan, not a missing endpoint);
    ``resume_url`` - in a paged read, the URL of the page that failed; ``counts`` /
    ``pages`` - ``option_underlyings`` only: the per-underlying counts of the pages read
    before the failure (a copy) and how many pages that was."""

    def __init__(self, kind: str, message: str, status: int | None = None):
        super().__init__(message)
        self.kind = kind if kind in KINDS else "http"
        self.status = status
        self.window = False
        self.resume_url: str | None = None
        self.counts: dict | None = None
        self.pages = 0


# ────────────────────────────────── settings ──────────────────────────────────

def api_key() -> str | None:
    """``TST_MASSIVE_API_KEY``, stripped; None when unset or blank."""
    v = (os.environ.get(ENV_KEY) or "").strip()
    return v or None


def base_url() -> str:
    """``TST_MASSIVE_BASE_URL`` (no trailing slash) or ``https://api.massive.com``."""
    v = (os.environ.get(ENV_BASE) or "").strip().rstrip("/")
    return v or DEFAULT_BASE_URL


def quotes_enabled() -> bool:
    """``TST_MASSIVE_QUOTES`` - does the plan include bid/ask quotes (Advanced and up)?
    Default False: Options Starter has none."""
    return (os.environ.get(ENV_QUOTES) or "").strip().lower() in {"1", "true", "yes", "on"}


_env_api_key = api_key        # the module function, reachable where a parameter shadows it
_env_base_url = base_url


def stocks_plan_start(day) -> _dt.date:
    """The oldest session worth asking Stocks Basic for on ET date ``day``:
    ``STOCKS_PLAN_DAYS`` (725) days back - the plan keeps about two years, and a request
    for an older day is refused (403, a "timeframe" message)."""
    d = _to_date(day)
    if d is None:
        raise ValueError("stocks_plan_start: a date is needed, got %r" % (day,))
    return d - _dt.timedelta(days=STOCKS_PLAN_DAYS)


def option_ticker(symbol, expiry, right, strike) -> str:
    """The Massive option ticker: ``"O:" + ROOT + YYMMDD + C|P + strike x 1000 (8
    digits)`` - ``option_ticker("SPY", "2025-12-19", "C", 650)`` ->
    ``"O:SPY251219C00650000"``. ``right`` takes C / P / call / put; ``expiry`` a date
    or ``YYYY-MM-DD``. The root is the symbol without punctuation (BRK.B -> BRKB, the
    OCC root)."""
    root = re.sub(r"[^A-Z0-9]", "", str(symbol or "").strip().upper())
    d = _to_date(expiry)
    r = str(right or "").strip().upper()[:1]
    k = _num(strike)
    if not root or d is None or r not in ("C", "P") or k is None or k <= 0:
        raise ValueError("option_ticker: need a symbol, an expiry, C|P and a positive strike")
    return "O:%s%s%s%08d" % (root, d.strftime("%y%m%d"), r, int(round(k * 1000)))


def massive_symbol(symbol) -> str:
    """An underlying symbol in Massive's spelling for a URL path: upper-case, and a
    share class written with a dash, a slash or a space (Yahoo's ``BRK-B``, ``BRK/B``,
    IBKR's ``BRK B``) gets Massive's dot - ``BRK.B``. Massive (Polygon) knows share
    classes only by the dot, so ``BRK-B`` would return no chain and no bars. Callers
    keep their own spelling as the storage key; only the request uses this one."""
    sym = str(symbol or "").strip().upper()
    return re.sub(r"[-/\s]+", ".", sym)


def our_symbol(ticker) -> str:
    """Massive's spelling of an underlying back to ours (the inverse of
    ``massive_symbol``): upper-case, a share-class dot becomes a dash - ``BRK.B`` ->
    ``BRK-B``. An index prefix (``I:SPX``, how the ticker reference list names an index)
    is kept as it is. The options contracts list names an index option's underlying
    WITHOUT the prefix (``SPX``), so the universe holds the bare symbol."""
    sym = str(ticker or "").strip().upper()
    return re.sub(r"[./\s]+", "-", sym)


# ────────────────────────────────── small helpers ──────────────────────────────────

def _num(v):
    """A finite float, else None (bools, NaN, inf, junk)."""
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


def _count(v):
    f = _num(v)
    return int(round(f)) if (f is not None and f >= 0) else None


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


def _day_str(x) -> str:
    d = _to_date(x)
    if d is None:
        raise ValueError("a date is needed (YYYY-MM-DD), got %r" % (x,))
    return d.isoformat()


def ts_to_utc(v) -> _dt.datetime | None:
    """A Massive timestamp as a naive-UTC datetime. ``last_updated`` fields are
    nanoseconds; bar ``t`` is milliseconds - the magnitude decides (s / ms / us / ns).
    0, negatives and junk are None."""
    f = _num(v)
    if f is None or f <= 0:
        return None
    if f >= 1e17:
        secs = f / 1e9
    elif f >= 1e14:
        secs = f / 1e6
    elif f >= 1e11:
        secs = f / 1e3
    else:
        secs = f
    try:
        return _dt.datetime.fromtimestamp(secs, _dt.timezone.utc).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def _scrub(text, key: str | None = None) -> str:
    """Text safe to show or log: the key (when known) and anything that looks like an
    ``apiKey=`` parameter or a Bearer token are masked; capped at 300 characters."""
    s = str(text or "")
    if key:
        s = s.replace(key, "***")
    s = re.sub(r"(?i)(api_?key=)[^&\s\"']+", r"\1***", s)
    s = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+", r"\1***", s)
    return s[:300]


def _label(path: str) -> str:
    for prefix, name in _LABELS:
        if prefix in path:
            return name
    return "this endpoint"


# ────────────────────────────────── pacing ──────────────────────────────────

class _RateBucket:
    """A token bucket as a GCRA schedule: ``rate`` requests a second with bursts of
    ``burst``. ``reserve()`` books the next slot and returns how long to wait for it -
    it never loops, so a fake clock that only moves when slept on is enough."""

    def __init__(self, rate: float, burst: int, now):
        self.interval = 1.0 / rate if rate and rate > 0 else 0.0
        self.burst = max(1, int(burst))
        self._now = now
        self._tat = None
        self._lock = threading.Lock()

    def reserve(self) -> float:
        if self.interval <= 0:
            return 0.0
        with self._lock:
            t = self._now()
            tat = t if self._tat is None else max(self._tat, t)
            wait = max(0.0, tat - t - (self.burst - 1) * self.interval)
            self._tat = tat + self.interval
            return wait


class _MinuteWindow:
    """At most ``n`` requests in any rolling ``window`` seconds (Stocks Basic's "5 a
    minute"). ``reserve()`` returns the wait before the next request may go."""

    def __init__(self, n: int, window: float, now):
        self.n = max(0, int(n or 0))
        self.window = float(window)
        self._now = now
        self._sent: collections.deque = collections.deque()
        self._lock = threading.Lock()

    def reserve(self) -> float:
        if self.n <= 0:
            return 0.0
        with self._lock:
            t = self._now()
            while self._sent and self._sent[0] <= t - self.window:
                self._sent.popleft()
            wait = 0.0
            if len(self._sent) >= self.n:
                wait = max(0.0, self._sent[0] + self.window - t)
                self._sent.popleft()
            self._sent.append(t + wait)
            return wait


# ────────────────────────────────── the client ──────────────────────────────────

class Client:
    """One process's connection to Massive. Thread-safe; at most ``concurrency``
    requests in flight; ``max_rps`` a second overall; ``stocks_per_min`` stock-bar
    requests per rolling minute. ``requests`` counts the HTTP requests sent (retries
    included). A missing key raises ``MassiveError("config")`` at the first request,
    so a client can be built (and the key checked with ``has_key``) on any PC."""

    def __init__(self, api_key=None, base_url=None, *, max_rps=20.0, concurrency=1,
                 stocks_per_min=5, timeout=20.0, http=None, sleep=time.sleep, now=time.monotonic):
        key = api_key if api_key is not None else _env_api_key()
        self._key = (str(key).strip() or None) if key is not None else None
        self.base_url = str(base_url or _env_base_url()).strip().rstrip("/")
        parts = urlsplit(self.base_url)
        self._origin = (parts.scheme.lower(), parts.netloc.lower())
        self.timeout = float(timeout)
        self._sleep = sleep
        self._now = now
        rps = _num(max_rps) or 0.0
        self._bucket = _RateBucket(rps, max(1, int(rps)) if rps > 0 else 1, now)
        self._stocks = _MinuteWindow(stocks_per_min, STOCK_WINDOW_S + STOCK_WINDOW_PAD_S, now)
        self._sem = threading.BoundedSemaphore(max(1, int(concurrency or 1)))
        self._own_http = http is None
        if http is None:
            import httpx

            http = httpx.Client(timeout=self.timeout, follow_redirects=False)
        self._http = http
        self.requests = 0
        self._count_lock = threading.Lock()

    # -- lifecycle --
    @property
    def has_key(self) -> bool:
        return bool(self._key)

    def close(self) -> None:
        if self._own_http:
            try:
                self._http.close()
            except Exception:  # noqa: BLE001
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __repr__(self) -> str:   # never the key
        return "<massive.Client %s key=%s>" % (self.base_url, "set" if self._key else "missing")

    # -- the endpoints --
    def chain_snapshot(self, symbol, *, exp_gte=None, exp_lte=None, strike_gte=None,
                       strike_lte=None) -> dict:
        """The option chain snapshot of ``symbol`` (every page, following ``next_url``):
        ``{"symbol", "rows", "underlying_price", "underlying_as_of", "pages", "as_of"}``.
        Each row: ``expiry, right (C|P), strike, iv (FRACTION or None), delta, gamma,
        theta, vega, oi, volume, day_close, day_vwap, prev_close, day_change_pct, bid,
        ask, bid_size, ask_size (None unless the plan returns quotes), last_updated``
        (naive UTC: the newer of the quote's and the day bar's), plus ``ticker``,
        ``multiplier``, ``und_price`` / ``und_as_of`` when Massive sent them. ``as_of``
        is the newest row ``last_updated``; ``underlying_price`` the newest
        ``underlying_asset.price`` (absent on a stocks plan without snapshots). A
        failure on any page raises - no partial chain comes back. ``symbol`` in the
        result is the caller's spelling (upper-cased); the request path uses Massive's
        (``massive_symbol``: BRK-B -> BRK.B)."""
        sym = str(symbol or "").strip().upper()
        if not sym:
            raise ValueError("chain_snapshot: no symbol")
        params = {"limit": SNAPSHOT_LIMIT}
        if exp_gte is not None:
            params["expiration_date.gte"] = _day_str(exp_gte)
        if exp_lte is not None:
            params["expiration_date.lte"] = _day_str(exp_lte)
        if strike_gte is not None and _num(strike_gte) is not None:
            params["strike_price.gte"] = _fmt_strike(strike_gte)
        if strike_lte is not None and _num(strike_lte) is not None:
            params["strike_price.lte"] = _fmt_strike(strike_lte)
        path = "/v3/snapshot/options/%s" % _urlquote(massive_symbol(sym), safe=":.")
        rows: list[dict] = []
        und_price = und_as_of = None
        pages = 0
        for payload in self._pages(path, params):
            pages += 1
            for res in payload.get("results") or ():
                row = parse_snapshot_result(res)
                if row is None:
                    continue
                rows.append(row)
                up, ua = row.get("und_price"), row.get("und_as_of")
                if up is not None and (und_price is None or (ua is not None and (und_as_of is None or ua > und_as_of))):
                    und_price, und_as_of = up, ua
        stamps = [r["last_updated"] for r in rows if r.get("last_updated") is not None]
        return {"symbol": sym, "rows": rows, "underlying_price": und_price,
                "underlying_as_of": und_as_of, "pages": pages,
                "as_of": max(stamps) if stamps else None}

    def stock_daily(self, symbol, start, end, *, adjusted=True) -> list[dict]:
        """Daily bars of a stock (Stocks Basic): ``[{"on", "open", "high", "low",
        "close", "volume"}]`` oldest first, ``on`` the ET session date. Split-adjusted by
        default; ``adjusted=False`` asks for the raw bars (``adjusted=false``) - the
        prices that traded that day. The path uses Massive's spelling of the symbol
        (``massive_symbol``: BRK-B -> BRK.B). Paced by the per-minute window."""
        sym = massive_symbol(symbol)
        if not sym:
            raise ValueError("stock_daily: no symbol")
        return self._daily(sym, start, end, stock=True, adjusted=bool(adjusted))

    def option_daily(self, option_ticker, start, end) -> list[dict]:
        """Daily bars of one option contract (``O:...`` ticker; expired contracts too) -
        the same shape as ``stock_daily``. Not paced per minute (Options Starter has no
        request cap)."""
        t = str(option_ticker or "").strip().upper()
        if not t.startswith("O:"):
            raise ValueError("option_daily: an O: ticker is needed")
        return self._daily(t, start, end, stock=False)

    def option_underlyings(self, exp_lte=None, *, contract_type="call", start_url=None, counts=None,
                           on_page=None, timeout=UNIVERSE_TIMEOUT_S, retries=UNIVERSE_RETRIES) -> dict[str, int]:
        """Every underlying with listed options (the Options Screener's universe,
        OPTIONS_SCREENER_DESIGN.md §4.1): ``/v3/reference/options/contracts`` with
        ``expired=false`` (``expiration_date.lte`` and ``contract_type`` when given -
        ``call`` by default: every listed underlying has calls, it halves the pages and
        the counts only order the passes), every page -> ``{symbol: number of
        contracts}`` in OUR spelling (``our_symbol``: BRK.B -> BRK-B; an index option's
        underlying comes back bare, ``SPX``). Not paced per minute (Options Starter has
        no request cap).

        Resumable and visible (the walk is ~500-800 pages):

        * ``start_url`` - resume at that page (a ``MassiveError.resume_url`` of an earlier
          walk; it must be on the configured host, else ``MassiveError("http")`` with no
          ``resume_url``); its query already carries the filters, so ``exp_lte`` /
          ``contract_type`` are not added to it. ``counts`` - the counts of the pages read
          before it (copied, then added to).
        * ``on_page(page_no, counts, next_url)`` after every page: ``page_no`` counts the
          pages of THIS call from 1, ``counts`` is a copy of the counts so far,
          ``next_url`` the absolute URL of the next page (None after the last). An error
          in the callback is swallowed - it never stops the walk.
        * Each request gets ``timeout`` seconds and ``retries`` retries on a network
          error or a 5xx (waiting 2, 4, 8, 16 s ...).
        * A failure raises ``MassiveError`` with ``.counts`` (a copy of the counts of the
          complete pages), ``.pages`` (pages read in this call) and ``.resume_url`` (the
          URL of the page that failed - None when the walk cannot be resumed: a next page
          on another host, the page cap, a bad ``start_url``)."""
        params = None
        if start_url is None:
            params = {"expired": "false", "limit": REFERENCE_LIMIT}
            if exp_lte is not None:
                params["expiration_date.lte"] = _day_str(exp_lte)
            ct = str(contract_type or "").strip().lower()
            if ct:
                if ct not in ("call", "put"):
                    raise ValueError("option_underlyings: contract_type is call, put or None")
                params["contract_type"] = ct
        got: dict[str, int] = {}
        for k, v in (counts or {}).items():
            sym, n = our_symbol(k), _count(v)
            if sym and n:
                got[sym] = got.get(sym, 0) + n
        n_retries = max(0, int(retries if retries is not None else UNIVERSE_RETRIES))
        pages = 0
        try:
            for payload in self._pages("/v3/reference/options/contracts", params,
                                       max_pages=UNIVERSE_MAX_PAGES, start_url=start_url,
                                       timeout=timeout, net_retries=n_retries,
                                       server_retries=n_retries, backoff_s=UNIVERSE_BACKOFF_S):
                for res in payload.get("results") or ():
                    if not isinstance(res, dict):
                        continue
                    sym = our_symbol(res.get("underlying_ticker"))
                    if sym:
                        got[sym] = got.get(sym, 0) + 1
                pages += 1
                if on_page is not None:
                    try:
                        on_page(pages, dict(got), self._next_url(payload))
                    except Exception:  # noqa: BLE001 - a progress hook never stops the walk
                        pass
        except MassiveError as exc:
            exc.counts = dict(got)
            exc.pages = pages
            raise
        return got

    def grouped_daily(self, day, *, adjusted=True) -> list[dict]:
        """One session's daily bar of EVERY US stock (Stocks Basic grouped daily,
        ``/v2/aggs/grouped/locale/us/market/stocks/<day>``): ``[{"symbol", "on", "open",
        "high", "low", "close", "volume"}]`` with ``symbol`` in our spelling and ``on`` =
        ``day``. Split-adjusted by default; ``adjusted=False`` = the prices that traded
        that day. Paced by the per-minute window (a stock request). A day with no bars
        (a holiday, not published yet) is ``[]``."""
        on = _day_str(day)
        path = "/v2/aggs/grouped/locale/us/market/stocks/%s" % on
        params = {"adjusted": "true" if adjusted else "false"}
        out: dict[str, dict] = {}
        for payload in self._pages(path, params, stock=True):
            for b in payload.get("results") or ():
                if not isinstance(b, dict):
                    continue
                sym = our_symbol(b.get("T"))
                close = _pos(b.get("c"))
                if not sym or close is None:
                    continue
                vol = _num(b.get("v"))
                out[sym] = {"symbol": sym, "on": on, "open": _pos(b.get("o")),
                            "high": _pos(b.get("h")), "low": _pos(b.get("l")), "close": close,
                            "volume": vol if (vol is not None and vol >= 0) else None}
        return [out[s] for s in sorted(out)]

    def reference_tickers(self, market="stocks") -> list[dict]:
        """The active tickers of one market (``/v3/reference/tickers?market=<market>
        &active=true``, every page; paced as a stock request): ``[{"symbol", "name",
        "type", "primary_exchange"}]`` - ``symbol`` in our spelling (an index keeps
        Massive's ``I:`` prefix), ``type`` Massive's code (CS, ETF, ADRC, INDEX ...),
        ``primary_exchange`` its MIC (XNYS, XNAS ...) or None."""
        m = str(market or "").strip().lower() or "stocks"
        params = {"market": m, "active": "true", "limit": REFERENCE_LIMIT}
        out: dict[str, dict] = {}
        for payload in self._pages("/v3/reference/tickers", params, stock=True,
                                   max_pages=UNIVERSE_MAX_PAGES):
            for res in payload.get("results") or ():
                if not isinstance(res, dict):
                    continue
                sym = our_symbol(res.get("ticker"))
                if not sym:
                    continue
                name = str(res.get("name") or "").strip() or None
                typ = str(res.get("type") or "").strip().upper() or None
                exch = str(res.get("primary_exchange") or "").strip().upper() or None
                out[sym] = {"symbol": sym, "name": name, "type": typ, "primary_exchange": exch}
        return [out[s] for s in sorted(out)]

    # -- internals --
    def _daily(self, ticker: str, start, end, *, stock: bool, adjusted: bool = True) -> list[dict]:
        path = "/v2/aggs/ticker/%s/range/1/day/%s/%s" % (
            _urlquote(ticker, safe=":.-"), _day_str(start), _day_str(end))
        params = {"adjusted": "true" if adjusted else "false", "sort": "asc", "limit": AGGS_LIMIT}
        by_day: dict[str, dict] = {}
        for payload in self._pages(path, params, stock=stock):
            for b in payload.get("results") or ():
                bar = parse_agg(b)
                if bar is not None:
                    by_day[bar["on"]] = bar
        return [by_day[d] for d in sorted(by_day)]

    def _next_url(self, payload) -> str | None:
        """A page's ``next_url`` made absolute (a relative one resolves on our host), or
        None after the last page. Not checked against our host here - ``_pages`` does."""
        nxt = payload.get("next_url") if isinstance(payload, dict) else None
        if not nxt:
            return None
        return urljoin(self.base_url + "/", str(nxt))

    def _same_origin(self, url: str) -> bool:
        parts = urlsplit(url)
        return (parts.scheme.lower(), parts.netloc.lower()) == self._origin

    def _pages(self, path: str, params: dict | None, *, stock: bool = False, max_pages: int = MAX_PAGES,
               start_url: str | None = None, timeout: float | None = None, net_retries: int | None = None,
               server_retries: int | None = None, backoff_s: float | None = None):
        """Yield each JSON page, following ``next_url`` (as given, same host only), at
        most ``max_pages`` of them. ``start_url`` (same host only) replaces the first
        request (its query carries the filters; ``params`` is not added). ``timeout`` /
        ``net_retries`` / ``server_retries`` / ``backoff_s`` are per-call overrides of the
        client's defaults (``_get``). A ``MassiveError`` raised for a page carries
        ``.resume_url`` = that page's full URL."""
        label = _label(path)
        if start_url:
            url = urljoin(self.base_url + "/", str(start_url))
            if not self._same_origin(url):
                raise MassiveError("http", "the resume URL for %s points at another host - not followed" % label)
            params = None
        else:
            url = self.base_url + path
        seen = set()
        for _ in range(max(1, int(max_pages))):
            try:
                payload = self._get(url, params, label=label, stock=stock, timeout=timeout,
                                    net_retries=net_retries, server_retries=server_retries,
                                    backoff_s=backoff_s)
            except MassiveError as exc:
                exc.resume_url = _full_url(url, params)
                raise
            yield payload
            nxt = self._next_url(payload)                    # absolute as given; a relative one on our host
            if not nxt:
                return
            if not self._same_origin(nxt):
                raise MassiveError("http", "Massive's next page points at another host - not followed")
            if nxt in seen:
                return
            seen.add(nxt)
            url, params = nxt, None
        raise MassiveError("http", "Massive kept paging past %d pages for %s" % (max(1, int(max_pages)), label))

    def _get(self, url: str, params, *, label: str, stock: bool, timeout: float | None = None,
             net_retries: int | None = None, server_retries: int | None = None,
             backoff_s: float | None = None) -> dict:
        """One request with the client's pacing and retries. ``timeout`` (seconds),
        ``net_retries`` / ``server_retries`` (default ``NETWORK_RETRIES`` /
        ``SERVER_RETRIES``) and ``backoff_s`` (None: 1 s, 2 s, ... ; else ``backoff_s``
        doubling - 2, 4, 8, 16 s) override the defaults for one call."""
        if not self._key:
            raise MassiveError("config", "%s is not set on this PC" % ENV_KEY)
        headers = {"Authorization": "Bearer " + self._key, "Accept": "application/json"}
        req_timeout = float(timeout) if _pos(timeout) is not None else self.timeout
        net_max = NETWORK_RETRIES if net_retries is None else max(0, int(net_retries))
        server_max = SERVER_RETRIES if server_retries is None else max(0, int(server_retries))

        def retry_wait(n: int) -> float:
            if _pos(backoff_s) is not None:
                return float(backoff_s) * 2 ** (n - 1)
            return RETRY_BASE_S * n

        rate_n = server_n = net_n = 0
        rate_wait = RATE_FIRST_S
        while True:
            if stock:
                w = self._stocks.reserve()
                if w > 0:
                    self._sleep(w)
            w = self._bucket.reserve()
            if w > 0:
                self._sleep(w)
            try:
                with self._sem:
                    with self._count_lock:
                        self.requests += 1
                    resp = self._http.get(url, params=params, headers=headers, timeout=req_timeout)
            except Exception as exc:  # noqa: BLE001 - httpx.TransportError and friends
                if not _is_transport_error(exc):
                    raise
                if net_n < net_max:
                    net_n += 1
                    self._sleep(retry_wait(net_n))
                    continue
                raise MassiveError("network", "could not reach Massive for %s (%s: %s)" % (
                    label, type(exc).__name__, _scrub(exc, self._key))) from None
            status = int(getattr(resp, "status_code", 0) or 0)
            if 200 <= status < 300:
                try:
                    data = resp.json()
                except Exception:  # noqa: BLE001
                    raise MassiveError("http", "Massive sent a reply that is not JSON for %s" % label,
                                       status) from None
                if not isinstance(data, dict):
                    raise MassiveError("http", "Massive sent an unexpected reply for %s" % label, status)
                body_status = str(data.get("status") or "").strip().upper()
                if body_status == "NOT_AUTHORIZED":
                    raise _plan_error("your Massive plan does not include %s (HTTP %d, NOT_AUTHORIZED)%s"
                                      % (label, status, _server_message(resp, self._key)), status)
                if body_status == "ERROR":
                    raise MassiveError("http", "Massive answered status ERROR for %s%s" % (
                        label, _server_message(resp, self._key)), status)
                return data
            if status == 401:
                raise MassiveError("auth", "Massive rejected the API key (HTTP 401)", status)
            if status == 403:
                raise _plan_error("your Massive plan does not include %s (HTTP 403)%s"
                                  % (label, _server_message(resp, self._key)), status)
            if status == 429:
                if rate_n < RATE_RETRIES:
                    rate_n += 1
                    wait = _retry_after(resp)
                    if wait is None:
                        wait = rate_wait
                        rate_wait = min(RATE_MAX_S, rate_wait * 2)
                    self._sleep(min(RATE_MAX_S, wait))
                    continue
                raise MassiveError("rate", "Massive kept answering 'too many requests' for %s (HTTP 429)"
                                   % label, status)
            if status >= 500 and server_n < server_max:
                server_n += 1
                self._sleep(retry_wait(server_n))
                continue
            raise MassiveError("http", "Massive answered HTTP %d for %s%s" % (
                status, label, _server_message(resp, self._key)), status)


def _plan_error(message: str, status: int | None) -> MassiveError:
    """A ``plan`` MassiveError; ``window`` is set when Massive's message speaks of the
    data timeframe (a date outside the plan's history, not an endpoint it lacks)."""
    exc = MassiveError("plan", message, status)
    exc.window = "timeframe" in message.lower()
    return exc


def _full_url(url: str, params) -> str:
    """``url`` with ``params`` encoded onto it - the exact page a later call can resume
    at (the key is never in it: it travels in the header)."""
    if not params:
        return url
    return url + ("&" if "?" in url else "?") + urlencode(params)


def _fmt_strike(v) -> str:
    f = _num(v)
    return ("%.4f" % f).rstrip("0").rstrip(".")


def _is_transport_error(exc) -> bool:
    try:
        import httpx
    except Exception:  # noqa: BLE001
        httpx = None
    if httpx is not None and isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, (ConnectionError, TimeoutError, OSError))


def _retry_after(resp) -> float | None:
    try:
        raw = resp.headers.get("Retry-After")
    except Exception:  # noqa: BLE001
        return None
    f = _num(raw)
    if f is not None and f >= 0:
        return f
    if raw:   # an HTTP date
        try:
            from email.utils import parsedate_to_datetime

            when = parsedate_to_datetime(str(raw))
            if when.tzinfo is None:
                when = when.replace(tzinfo=_dt.timezone.utc)
            return max(0.0, (when - _dt.datetime.now(_dt.timezone.utc)).total_seconds())
        except Exception:  # noqa: BLE001
            return None
    return None


def _server_message(resp, key) -> str:
    msg = ""
    try:
        data = resp.json()
        if isinstance(data, dict):
            msg = data.get("error") or data.get("message") or data.get("status") or ""
    except Exception:  # noqa: BLE001
        try:
            msg = resp.text
        except Exception:  # noqa: BLE001
            msg = ""
    msg = _scrub(msg, key).strip()
    return (": " + msg[:200]) if msg else ""


# ────────────────────────────────── parsing ──────────────────────────────────

def parse_snapshot_result(res) -> dict | None:
    """One chain-snapshot result as a row (see ``Client.chain_snapshot``), or None when
    the contract key (expiry, call/put, strike) is unusable. Missing pieces are None:
    greeks are absent on some deep-ITM contracts, ``last_quote`` on plans without
    quotes, ``underlying_asset.price`` on a stocks plan without snapshots."""
    if not isinstance(res, dict):
        return None
    det = res.get("details") or {}
    if not isinstance(det, dict):
        return None
    exp = _to_date(det.get("expiration_date"))
    ct = str(det.get("contract_type") or "").strip().lower()
    right = "C" if ct.startswith("c") else "P" if ct.startswith("p") else None
    strike = _pos(det.get("strike_price"))
    if exp is None or right is None or strike is None:
        return None
    greeks = res.get("greeks") if isinstance(res.get("greeks"), dict) else {}
    day = res.get("day") if isinstance(res.get("day"), dict) else {}
    q = res.get("last_quote") if isinstance(res.get("last_quote"), dict) else {}
    und = res.get("underlying_asset") if isinstance(res.get("underlying_asset"), dict) else {}
    iv = _num(res.get("implied_volatility"))
    bid, ask = _num(q.get("bid")), _num(q.get("ask"))
    bid = bid if (bid is not None and bid >= 0) else None
    ask = ask if (ask is not None and ask > 0) else None
    stamps = [s for s in (ts_to_utc(q.get("last_updated")), ts_to_utc(day.get("last_updated"))) if s]
    return {
        "expiry": exp.isoformat(), "right": right, "strike": strike,
        "iv": iv if (iv is not None and iv > 0) else None,
        "delta": _num(greeks.get("delta")), "gamma": _num(greeks.get("gamma")),
        "theta": _num(greeks.get("theta")), "vega": _num(greeks.get("vega")),
        "oi": _count(res.get("open_interest")),
        "volume": _count(day.get("volume")),
        "day_close": _pos(day.get("close")),
        "day_vwap": _pos(day.get("vwap")),
        "prev_close": _pos(day.get("previous_close")),
        "day_change_pct": _num(day.get("change_percent")),
        "bid": bid, "ask": ask,
        "bid_size": _count(q.get("bid_size")) if q else None,
        "ask_size": _count(q.get("ask_size")) if q else None,
        "last_updated": max(stamps) if stamps else None,
        "ticker": det.get("ticker") or None,
        "multiplier": _count(det.get("shares_per_contract")),
        "und_price": _pos(und.get("price")),
        "und_as_of": ts_to_utc(und.get("last_updated")),
    }


def parse_agg(b) -> dict | None:
    """One aggregate bar ``{t (ms), o, h, l, c, v}`` as ``{"on", "open", "high", "low",
    "close", "volume"}`` (``on`` = the ET date of ``t``), or None without a close."""
    if not isinstance(b, dict):
        return None
    when = ts_to_utc(b.get("t"))
    close = _pos(b.get("c"))
    if when is None or close is None:
        return None
    vol = _num(b.get("v"))
    return {"on": clock.et_date(when).isoformat(), "open": _pos(b.get("o")),
            "high": _pos(b.get("h")), "low": _pos(b.get("l")), "close": close,
            "volume": vol if (vol is not None and vol >= 0) else None}
