"""What an ETF actually holds, with each holding's performance — the Sector ETFs
tab's right-hand panel (user, 2026-09-11: "a right panel to show the list of
component tickers in that ETF and sort by performance").

Two questions, two sources, joined on the ticker:

  * WHAT the fund holds, and at what weight — from the ISSUER, because only the
    issuer knows. State Street publishes a daily holdings workbook for every SPDR
    (the eleven sector funds and SPY); Invesco serves QQQ's book as JSON.

    Deliberately NOT inferred from a screener's sector filter, which would have
    been one request: Finviz's "Technology ∩ S&P 500" returns 86 names against
    XLK's ~70 real holdings, so the panel would have listed tickers the fund does
    not own — and it carries no weights, which is half of reading an ETF.

  * HOW each holding is doing — from one Finviz performance walk per index (the
    S&P 500 covers every SPDR holding, the Nasdaq-100 covers QQQ). Cached and
    shared by all thirteen funds, so once the walk is warm, flipping between
    sector buttons costs no scraping at all.

The S&P 500 walk is ~25 pages. A web request must never sit on that, so on a cold
cache the walk runs in a background thread and the panel renders the holdings
straight away (weights, no performance yet) and re-requests itself until the
numbers land.

Live sources only (issuer files, Finviz), never parquet — this is a live view
(CLAUDE.md). Everything soft-fails: a blocked issuer serves the last good copy if
there is one, otherwise the panel shows a reason instead of an error page.
"""
from __future__ import annotations

import datetime as _dt
import io
import json
import re
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

from . import resources_bridge

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_SSGA_URL = ("https://www.ssga.com/us/en/intermediary/library-content/products/"
             "fund-data/etfs/us/holdings-daily-us-en-{sym}.xlsx")
_INVESCO_URL = ("https://dng-api.invesco.com/cache/v1/accounts/en_US/shareclasses/"
                "{sym}/holdings/fund?idType=ticker&interval=monthly&productType=ETF")

# Which issuer publishes which fund's book.
_SSGA_FUNDS = frozenset({"XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU",
                         "XLB", "XLRE", "XLC", "SPY"})
_INVESCO_FUNDS = frozenset({"QQQ"})

# Which Finviz index covers every holding of a fund, for the performance join.
_PERF_INDEX = {"QQQ": "idx_ndx"}          # every SPDR: idx_sp500
_PERF_URL = "https://finviz.com/screener.ashx?v=141&f={code}&o=-marketcap"

HOLDINGS_TTL_S = 12 * 3600    # issuers publish once a day
PERF_TTL_S = 15 * 60          # Finviz's free screener is itself ~15 min delayed
_FAIL_BACKOFF_S = 120         # after a failed walk, don't re-hammer Finviz
_MAX_DOWNLOAD = 8 * 1024 * 1024

_CACHE_DIR = resources_bridge.TRADEHUNTER_ROOT / "state" / "cache"

# (url key, row field, button label). Best first for every performance window;
# "weight" sorts heaviest first — how the fund itself is built.
SORTS = [
    ("1d", "chg_1d", "1D"), ("1w", "perf_1w", "1W"), ("1m", "perf_1m", "1M"),
    ("3m", "perf_3m", "3M"), ("ytd", "perf_ytd", "YTD"), ("weight", "weight", "Wt"),
]
_SORT_FIELD = {k: f for k, f, _ in SORTS}
_SORT_LABEL = {k: lbl for k, _, lbl in SORTS}
# 1M by default: the same window the RRG leaders table ranks sectors by, so a
# fund and its members are read on the same clock.
DEFAULT_SORT = "1m"


# ── small helpers ───────────────────────────────────────────────────────────

def _float(v) -> float | None:
    try:
        f = float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return f if f == f else None


_EQUITY_TICKER = re.compile(r"^[A-Z][A-Z0-9]*(\.[A-Z])?$")


def _norm(ticker: str) -> str:
    """An issuer's ticker in the dotted share-class form the rest of the codebase
    uses ('BRK.B'; issuers variously write 'BRK/B' or 'BRK B') — or '' for anything
    that is not shaped like a US equity ticker, so every parser's ``if not tick``
    skips it. This is the one choke point both issuer parsers share.

    SSGA books carry corporate-action placeholders under identifier codes — SPY's
    '2602335D' is a zero-weight Hologic contra line — which would otherwise render
    as a phantom holding at the bottom of the list with no performance."""
    t = (ticker or "").strip().upper().replace("/", ".").replace(" ", ".")
    return t if _EQUITY_TICKER.match(t) else ""


_SHARE_CLASS = re.compile(r"^([A-Z]{1,5})\.([A-Z])$")


def chart_symbol(ticker: str) -> str:
    """The form the price chart can fetch. Yahoo spells share classes with a dash
    ('BRK-B'). Mapped HERE, for known US equities only, rather than inside the
    shared price service: a dot on Yahoo can also be an exchange suffix ('RY.TO',
    'VOD.L'), which a blanket replace would break."""
    m = _SHARE_CLASS.match(ticker or "")
    return f"{m.group(1)}-{m.group(2)}" if m else ticker


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _write_json(path, obj) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _download(url: str, accept: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": accept})
    with urllib.request.urlopen(req, timeout=25) as resp:
        blob = resp.read(_MAX_DOWNLOAD + 1)
    if len(blob) > _MAX_DOWNLOAD:
        raise ValueError("holdings file unexpectedly large")
    return blob


# ── issuer holdings ─────────────────────────────────────────────────────────

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
# An index-future line: SSGA lists them as e.g. 'XAK TECHNOLOGY    SEP26'.
_FUTURES_NAME = re.compile(r"\b(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{2}\s*$")


def _xlsx_rows(blob: bytes) -> list[dict[str, str]]:
    """Cell text by column letter, row by row, from the first worksheet.

    Standard library only (an .xlsx is a zip of XML) — openpyxl is not a platform
    dependency, and one small read-only table is not worth adding it to the Hermes
    deploy for. Part sizes are capped so a malformed file cannot balloon in memory.
    """
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = z.namelist()
        if any(z.getinfo(n).file_size > _MAX_DOWNLOAD * 4 for n in names):
            raise ValueError("oversized workbook part")
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(_NS + "si"):
                shared.append("".join(t.text or "" for t in si.iter(_NS + "t")))
        sheets = sorted(n for n in names
                        if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
        if not sheets:
            raise ValueError("workbook has no sheets")
        root = ET.fromstring(z.read(sheets[0]))
    rows: list[dict[str, str]] = []
    for row in root.iter(_NS + "row"):
        vals: dict[str, str] = {}
        for c in row.iter(_NS + "c"):
            m = re.match(r"[A-Z]+", c.get("r") or "")
            if not m:
                continue
            kind, v = c.get("t"), c.find(_NS + "v")
            if kind == "s" and v is not None and v.text is not None:
                try:
                    val = shared[int(v.text)]
                except (ValueError, IndexError):
                    val = ""
            elif kind == "inlineStr":
                val = "".join(x.text or "" for x in c.iter(_NS + "t"))
            else:
                val = v.text if (v is not None and v.text is not None) else ""
            vals[m.group(0)] = val.strip()
        rows.append(vals)
    return rows


def _ssga(sym: str) -> dict:
    rows = _xlsx_rows(_download(_SSGA_URL.format(sym=sym.lower()), "*/*"))
    as_of = None
    for r in rows[:8]:                      # "Holdings: As of 09-Sep-2026"
        for v in r.values():
            m = re.search(r"As of (\d{2}-[A-Za-z]{3}-\d{4})", v)
            if m:
                try:
                    as_of = _dt.datetime.strptime(m.group(1), "%d-%b-%Y").date().isoformat()
                except ValueError:
                    pass
    hdr = next((i for i, r in enumerate(rows)
                if "Ticker" in r.values() and "Weight" in r.values()), None)
    if hdr is None:
        raise ValueError("holdings header not found")
    col = {v: k for k, v in rows[hdr].items()}
    out = []
    for r in rows[hdr + 1:]:
        tick = _norm(r.get(col["Ticker"], ""))
        name = r.get(col.get("Name", ""), "")
        # Cash and the money-market sweep carry "-" as their ticker; index futures
        # are named with a contract month. Neither is a company you can chart.
        if (not tick or tick == "-" or _FUTURES_NAME.search(name.upper())
                or _is_future_ticker(tick)):
            continue
        w = _float(r.get(col["Weight"], ""))
        if w is None or w <= 0:
            continue
        out.append({"symbol": tick, "name": name, "weight": w})
    if not out:
        raise ValueError("no holdings parsed")
    return {"symbol": sym, "as_of": as_of, "source": "State Street", "rows": out}


# Invesco's own security classification, ALLOW-listed. Taken from inspecting QQQ's
# book (2026-09-11), not assumed: it carries COM (99 names), ADR (ARM, PDD) and
# DRNY (ASML, an NY-registered ADR) — alongside IFUT (the NQ index future, which
# leaked into the first version of this panel as a "holding"), CURR / CURRCOL
# (cash, pending dividends, collateral) and SYN (the future's synthetic contra
# line). A common-stock-only filter would have silently dropped ASML, ARM and PDD,
# three real Nasdaq-100 companies; an equity allow-list keeps them and nothing else.
_INVESCO_EQUITY_TYPES = frozenset({"COM", "ADR", "DRNY"})


def _is_future_ticker(tick: str) -> bool:
    """A listed future's code ends in its contract-year digit ('NQU6', 'IXTU6'); a
    US equity ticker never does. The backstop behind each issuer's own signal, so a
    futures line that gets renamed or re-coded still cannot pass for a holding."""
    return bool(tick) and tick[-1].isdigit()


def _invesco(sym: str) -> dict:
    data = json.loads(_download(_INVESCO_URL.format(sym=sym), "application/json")
                      .decode("utf-8"))
    out = []
    for h in data.get("holdings") or []:
        if (h.get("securityTypeCode") or "").upper() not in _INVESCO_EQUITY_TYPES:
            continue
        tick = _norm(h.get("ticker") or "")
        if not tick or _is_future_ticker(tick):
            continue
        w = _float(h.get("percentageOfTotalNetAssets"))
        if w is None or w <= 0:
            continue
        out.append({"symbol": tick, "name": (h.get("issuerName") or "").strip(),
                    "weight": w})
    if not out:
        raise ValueError("no holdings parsed")
    return {"symbol": sym, "as_of": data.get("effectiveDate"), "source": "Invesco",
            "rows": out}


_HOLD_MEM: dict[str, tuple[float, dict]] = {}


def holdings(etf: str) -> dict:
    """{symbol, as_of, source, rows:[{symbol, name, weight}], error, stale}.

    Order of preference: memory, disk (both 12h), the issuer, and — if the issuer
    fails — the last good copy on disk however old, marked ``stale``. A day-old
    book is far more useful than an empty panel, and the as-of date says how old.
    """
    sym = (etf or "").strip().upper()
    base = {"symbol": sym, "as_of": None, "source": None, "rows": [],
            "error": None, "stale": False}
    if sym not in _SSGA_FUNDS and sym not in _INVESCO_FUNDS:
        return {**base, "error": f"No holdings source is wired for {sym or 'this fund'}."}

    now = time.time()
    hit = _HOLD_MEM.get(sym)
    if hit and now - hit[0] <= HOLDINGS_TTL_S:
        return hit[1]
    path = _CACHE_DIR / f"etf_holdings_{sym.lower()}.json"
    disk = _read_json(path)
    if disk and disk.get("rows") and now - float(disk.get("fetched_at", 0)) <= HOLDINGS_TTL_S:
        data = {**base, **disk}
        _HOLD_MEM[sym] = (float(disk["fetched_at"]), data)
        return data
    try:
        fresh = _ssga(sym) if sym in _SSGA_FUNDS else _invesco(sym)
    except Exception:  # noqa: BLE001
        if disk and disk.get("rows"):
            return {**base, **disk, "stale": True}
        return {**base, "error": f"Couldn't load {sym}'s holdings from the issuer right now."}
    data = {**base, **fresh, "fetched_at": now}
    _HOLD_MEM[sym] = (now, data)
    _write_json(path, data)
    return data


# ── performance, warmed in the background ───────────────────────────────────

_PERF_LOCK = threading.Lock()
_PERF_RUNNING: set[str] = set()
_PERF_FAILED: dict[str, float] = {}


def _walk(code: str, url: str) -> None:
    from resources import finviz_screener as fv

    try:
        rows = fv.fetch_performance_rows(url, cache_ttl_s=PERF_TTL_S,
                                         page_sleep_s=0.3, force_refresh=True)
        with _PERF_LOCK:
            if rows:
                _PERF_FAILED.pop(code, None)
            else:
                _PERF_FAILED[code] = time.time()
    except Exception:  # noqa: BLE001
        with _PERF_LOCK:
            _PERF_FAILED[code] = time.time()
    finally:
        with _PERF_LOCK:
            _PERF_RUNNING.discard(code)


def _perf_rows(etf: str) -> tuple[list[dict], str]:
    """(rows, state) — state is 'ready', 'loading' or 'failed'. Never blocks: a
    cold cache starts ONE background walk per index (concurrent clicks share it)."""
    from resources import finviz_screener as fv

    code = _PERF_INDEX.get(etf, "idx_sp500")
    url = _PERF_URL.format(code=code)
    rows = fv.fetch_performance_rows(url, cache_ttl_s=PERF_TTL_S, cache_only=True)
    if rows:
        return rows, "ready"
    with _PERF_LOCK:
        failed_at = _PERF_FAILED.get(code)
        if failed_at and time.time() - failed_at < _FAIL_BACKOFF_S:
            return [], "failed"
        if code not in _PERF_RUNNING:
            _PERF_RUNNING.add(code)
            threading.Thread(target=_walk, args=(code, url), daemon=True,
                             name=f"etf-perf-{code}").start()
    return [], "loading"


# ── the panel ───────────────────────────────────────────────────────────────

def _etf_name(sym: str) -> str:
    from .etf import ETF_UNIVERSE, INDEX_ETFS

    return dict(ETF_UNIVERSE + INDEX_ETFS).get(sym, sym)


def components(etf: str, sort: str = DEFAULT_SORT) -> dict:
    """Everything _sector_etf_holdings.html renders: the fund's holdings, each
    joined to its performance, sorted best-first by the chosen window.

    A holding Finviz has no row for keeps its weight and sorts to the bottom of a
    performance sort rather than being dropped — the fund still owns it.
    """
    sym = (etf or "").strip().upper()
    sort = sort if sort in _SORT_FIELD else DEFAULT_SORT
    hold = holdings(sym)
    perf_rows, perf_state = _perf_rows(sym) if hold["rows"] else ([], "ready")
    perf = {r["symbol"]: r for r in perf_rows}

    rows = []
    for h in hold["rows"]:
        p = perf.get(h["symbol"]) or {}
        rows.append({
            "symbol": h["symbol"], "chart_symbol": chart_symbol(h["symbol"]),
            # Finviz's "NVIDIA Corp" reads better than the issuer's "NVIDIA CORP"
            "name": p.get("company") or h["name"].title(),
            "weight": h["weight"], "price": p.get("price"),
            "chg_1d": p.get("chg_1d"), "perf_1w": p.get("perf_1w"),
            "perf_1m": p.get("perf_1m"), "perf_3m": p.get("perf_3m"),
            "perf_ytd": p.get("perf_ytd"),
        })
    field = _SORT_FIELD[sort]
    rows.sort(key=lambda r: (r[field] is None, -(r[field] or 0.0), r["symbol"]))

    # The value column shows the sorted window; sorting by weight shows 1D beside
    # it, because a weight sort is how the fund is built, not how it is doing.
    perf_field = field if sort != "weight" else "chg_1d"
    perf_label = _SORT_LABEL[sort] if sort != "weight" else "1D"
    return {
        "etf": sym, "etf_name": _etf_name(sym), "rows": rows,
        "sort": sort, "sorts": SORTS, "perf_field": perf_field, "perf_label": perf_label,
        "as_of": hold["as_of"], "source": hold["source"], "stale": hold["stale"],
        "error": hold["error"], "perf_state": perf_state,
        "n_priced": sum(1 for r in rows if r["chg_1d"] is not None),
    }
