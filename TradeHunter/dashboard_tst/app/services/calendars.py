"""Economic + earnings calendars — the data behind the /calendar pages.

DATA-SOURCE RULE (CLAUDE.md): both calendars are LIVE / OPERATIONAL views — they
show what is scheduled now and next — so every row here is fetched live over
HTTP. Never the parquet store, which is reserved for backtesting and offline
analysis.

Sources, both free and key-less:

  * ECONOMIC — TradingView's own economic-calendar feed
    (``economic-calendar.tradingview.com/events``): the same data behind the
    widget already embedded on the Today board. Taking the JSON instead of the
    iframe is the whole point of this module — it lets us group by market day,
    filter by country and importance, theme it like the rest of the app, and sit
    it next to the earnings tab. The widget can do none of that.
  * EARNINGS — Nasdaq's public calendar API
    (``api.nasdaq.com/api/calendar/earnings?date=YYYY-MM-DD``), one call per
    calendar day; a week view fans its calls out concurrently.

Both are undocumented public endpoints, so everything soft-fails: a source that
is down, rate-limited or reshaped returns [] and the page renders its empty
state instead of 500ing. Short in-process TTL caches stop a page refresh (or
five members reading the same week) from re-hitting them.
"""
from __future__ import annotations

import datetime as _dt
import re
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# ONE pooled client for both feeds — same reasoning as services/prices.py: a
# fresh connection per call pays a full TLS handshake, which is most of what
# makes a cold panel feel slow.
_CLIENT: httpx.Client | None = None


def _client() -> httpx.Client:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = httpx.Client(
            headers={"User-Agent": _UA},
            timeout=12.0,
            follow_redirects=True,
            limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
        )
    return _CLIENT


# ─────────────────────────── market clock ───────────────────────────
try:
    from zoneinfo import ZoneInfo

    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover — Windows without the tzdata package
    _ET = None


def market_date(utc_dt: _dt.datetime) -> _dt.date:
    """The US market date a naive-UTC stamp belongs to. Both calendars group by
    this: a release is "Thursday's number" in New York regardless of where the
    reader sits (a Malaysian evening is still the US morning)."""
    if _ET is None:
        return utc_dt.date()
    return utc_dt.replace(tzinfo=_dt.timezone.utc).astimezone(_ET).date()


def today_et() -> _dt.date:
    """Today's US market date — the default anchor for both calendars."""
    now = _dt.datetime.now(_dt.timezone.utc)
    return now.astimezone(_ET).date() if _ET else now.date()


# ───────────────────────────── economic ─────────────────────────────
_ECON_URL = "https://economic-calendar.tradingview.com/events"
_ECON_CACHE: dict[tuple, tuple[float, list[dict]]] = {}
_ECON_TTL = 300.0        # 5 min — actuals print through the session
_ECON_MISS_TTL = 60.0    # but never get stuck on a transient failure

# TradingView's importance scale, exactly as it comes back on the wire.
IMPORTANCE = {1: "high", 0: "medium", -1: "low"}

# The countries the filter offers. Codes are TradingView's (ISO-2, plus "EU"
# for the euro area). Order = how the pills render.
COUNTRIES: list[tuple[str, str]] = [
    ("US", "United States"),
    ("EU", "Euro area"),
    ("GB", "United Kingdom"),
    ("JP", "Japan"),
    ("CN", "China"),
    ("DE", "Germany"),
    ("CA", "Canada"),
    ("AU", "Australia"),
]
COUNTRY_LABELS = dict(COUNTRIES)


def _parse_z(s: str) -> _dt.datetime | None:
    """'2026-08-19T03:35:00.000Z' -> naive UTC datetime (what _time.html wants)."""
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return _dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _num(v) -> str | None:
    """Trim a reading to a readable string: 1.4332 -> '1.4332', 3.0 -> '3'."""
    if v is None:
        return None
    if isinstance(v, str):
        return v.strip() or None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    s = f"{f:,.4f}".rstrip("0").rstrip(".")
    return s or "0"


def fmt_value(raw, unit: str | None, scale: str | None) -> str | None:
    """One economic reading written the way TradingView writes it: '2.8%',
    '$1.2B', '236K'. `unit` is the symbol ('%', '$', ...), `scale` the magnitude
    ('K', 'M', 'B', 'T')."""
    n = _num(raw)
    if n is None:
        return None
    scale = (scale or "").strip()
    unit = (unit or "").strip()
    if unit == "$":
        return f"${n}{scale}"
    if unit == "%":
        return f"{n}{scale}%"
    return f"{n}{scale}{(' ' + unit) if unit else ''}"


def _surprise(actual, forecast) -> int | None:
    """+1 above forecast, -1 below, 0 in line, None when either side is missing."""
    if actual is None or forecast is None:
        return None
    try:
        a, f = float(actual), float(forecast)
    except (TypeError, ValueError):
        return None
    if a > f:
        return 1
    if a < f:
        return -1
    return 0


def economic_events(
    start: _dt.date,
    end: _dt.date,
    countries: list[str] | tuple[str, ...] = ("US",),
    min_importance: int = 0,
) -> list[dict]:
    """Economic releases from `start` to `end` INCLUSIVE (US market dates),
    ordered by time.

    Each row is ``{when (naive UTC datetime), date, country, country_label,
    importance, impact, title, period, actual, forecast, previous, source,
    comment, surprise}``.

    `surprise` is +1 / -1 / 0 / None for actual vs forecast — DIRECTION only. It
    is deliberately not labelled good or bad: above forecast is bullish for
    payrolls and bearish for jobless claims, and this module has no business
    deciding which.

    `min_importance` filters on TradingView's scale (1 high, 0 medium, -1 low).
    """
    codes = tuple(sorted({(c or "").strip().upper() for c in countries if c}))
    key = (start, end, codes, min_importance)
    hit = _ECON_CACHE.get(key)
    if hit and hit[0] > time.time():
        return hit[1]

    rows = _fetch_economic(start, end, codes, min_importance)
    _ECON_CACHE[key] = (time.time() + (_ECON_TTL if rows else _ECON_MISS_TTL), rows)
    return rows


def _fetch_economic(start, end, codes, min_importance) -> list[dict]:
    # Query a day either side in UTC: an 08:30 ET print is 12:30 UTC and a Tokyo
    # release lands on the UTC day BEFORE its own market day, so a tight UTC
    # window clips both edges of the requested range. Trimmed back to the
    # requested market days at the end.
    frm = (start - _dt.timedelta(days=1)).isoformat() + "T00:00:00.000Z"
    to = (end + _dt.timedelta(days=2)).isoformat() + "T00:00:00.000Z"
    try:
        r = _client().get(
            _ECON_URL,
            params={"from": frm, "to": to, "countries": ",".join(codes) or "US"},
            # The feed is CORS-gated on TradingView's own origin.
            headers={"Origin": "https://www.tradingview.com",
                     "Referer": "https://www.tradingview.com/"},
        )
        if r.status_code != 200:
            return []
        payload = r.json()
    except Exception:  # noqa: BLE001
        return []
    if (payload or {}).get("status") != "ok":
        return []

    out: list[dict] = []
    for e in payload.get("result") or []:
        when = _parse_z(e.get("date") or "")
        if when is None:
            continue
        imp = e.get("importance")
        imp = -1 if imp is None else int(imp)
        if imp < min_importance:
            continue
        country = (e.get("country") or "").upper()
        if codes and country not in codes:
            continue
        unit, scale = e.get("unit"), e.get("scale")
        actual_raw, forecast_raw = e.get("actual"), e.get("forecast")
        out.append({
            "id": e.get("id"),
            "when": when,
            "date": market_date(when),
            "country": country,
            "country_label": COUNTRY_LABELS.get(country, country),
            "importance": imp,
            "impact": IMPORTANCE.get(imp, "low"),
            "title": (e.get("title") or "").strip(),
            "period": (e.get("period") or "").strip(),
            "actual": fmt_value(actual_raw, unit, scale),
            "forecast": fmt_value(forecast_raw, unit, scale),
            "previous": fmt_value(e.get("previous"), unit, scale),
            "source": (e.get("source") or "").strip(),
            "comment": (e.get("comment") or "").strip(),
            "surprise": _surprise(actual_raw, forecast_raw),
        })
    out = [e for e in out if start <= e["date"] <= end]
    out.sort(key=lambda e: (e["when"], -e["importance"], e["title"]))
    return out


# ───────────────────────────── earnings ─────────────────────────────
_EARN_URL = "https://api.nasdaq.com/api/calendar/earnings"
_EARN_CACHE: dict[_dt.date, tuple[float, list[dict]]] = {}
_EARN_TTL = 1800.0        # 30 min — a day's roster barely moves
_EARN_MISS_TTL = 300.0    # weekends are legitimately empty; still retry sooner

_WHEN_LABELS = {
    "time-pre-market": ("pre", "Before open"),
    "time-after-hours": ("post", "After close"),
    "time-not-supplied": ("tbd", "Time TBD"),
}


def _money(s) -> float | None:
    """'$916,770,718,656' / '($0.12)' -> float. None for '', 'N/A', junk."""
    if not s:
        return None
    t = str(s).strip()
    neg = t.startswith("(") and t.endswith(")")
    t = re.sub(r"[^0-9.\-]", "", t)
    if not t or t in {"-", "."}:
        return None
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def _plain(s) -> str | None:
    """A plain Nasdaq cell, with their 'N/A' filler folded into a real None so the
    table renders one em-dash instead of two different kinds of nothing."""
    t = str(s or "").strip()
    return None if (not t or t.upper() in {"N/A", "NA", "-", "--"}) else t


def _eps(s) -> str | None:
    """Nasdaq writes negatives as '($0.12)'; render them as '-$0.12'."""
    if not s:
        return None
    t = str(s).strip()
    if not t or t.upper() in {"N/A", "NA", "-"}:
        return None
    if t.startswith("(") and t.endswith(")"):
        return "-" + t[1:-1]
    return t


def human_cap(v: float | None) -> str | None:
    """916770718656 -> '$916.8B'."""
    if not v:
        return None
    for cut, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= cut:
            return f"${v / cut:,.1f}{suf}"
    return f"${v:,.0f}"


def earnings_for(day: _dt.date) -> list[dict]:
    """Every company reporting on `day` (a US market date), biggest first.

    Each row is ``{symbol, name, when, when_label, market_cap, market_cap_disp,
    eps_forecast, n_ests, last_year_eps, last_year_date, fiscal_quarter, date}``.
    """
    hit = _EARN_CACHE.get(day)
    if hit and hit[0] > time.time():
        return hit[1]
    rows = _fetch_earnings(day)
    _EARN_CACHE[day] = (time.time() + (_EARN_TTL if rows else _EARN_MISS_TTL), rows)
    return rows


def _fetch_earnings(day: _dt.date) -> list[dict]:
    try:
        r = _client().get(
            _EARN_URL,
            params={"date": day.isoformat()},
            headers={"Accept": "application/json, text/plain, */*",
                     "Origin": "https://www.nasdaq.com",
                     "Referer": "https://www.nasdaq.com/"},
        )
        if r.status_code != 200:
            return []
        raw = ((r.json() or {}).get("data") or {}).get("rows") or []
    except Exception:  # noqa: BLE001
        return []

    out: list[dict] = []
    for it in raw:
        sym = (it.get("symbol") or "").strip().upper()
        if not sym:
            continue
        when, when_label = _WHEN_LABELS.get(
            (it.get("time") or "").strip(), ("tbd", "Time TBD"))
        cap = _money(it.get("marketCap"))
        out.append({
            "symbol": sym,
            "name": (it.get("name") or "").strip(),
            "when": when,
            "when_label": when_label,
            "market_cap": cap,
            "market_cap_disp": human_cap(cap),
            "eps_forecast": _eps(it.get("epsForecast")),
            "n_ests": _plain(it.get("noOfEsts")),
            "last_year_eps": _eps(it.get("lastYearEPS")),
            "last_year_date": _plain(it.get("lastYearRptDt")),
            "fiscal_quarter": _plain(it.get("fiscalQuarterEnding")),
            "date": day,
        })
    # Biggest company first: on a 200-name day that is the only ordering that
    # puts the market-moving reports on screen without scrolling. Unknown last.
    out.sort(key=lambda x: (x["market_cap"] is None, -(x["market_cap"] or 0), x["symbol"]))
    return out


def earnings_range(days: list[_dt.date]) -> list[tuple[_dt.date, list[dict]]]:
    """`earnings_for` over several days, fetched concurrently (one call each)."""
    if not days:
        return []
    with ThreadPoolExecutor(max_workers=min(7, len(days))) as ex:
        rows = list(ex.map(earnings_for, days))
    return list(zip(days, rows))
