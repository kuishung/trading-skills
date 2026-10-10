"""The Options v2 page (OPTIONS_V2_DESIGN.md §9, on the Massive data path of §13.6):
``routes/options_page.py``, ``options.html`` and the ``_opt_*.html`` fragments.

Every test drives the real FastAPI app through ``TestClient`` against a fresh SQLite
file brought to the Alembic head by the real migrations (conftest). ``get_db`` and
``security.current_user`` are overridden so the member and the handler share ONE
session per request. The data is seeded through ``opt_store``'s own writers
(``upsert_quotes`` with ``chain_bs`` rows as Massive would file them - source
"massive", data type "delayed" - ``upsert_daily`` + ``recompute_underlying``,
``set_earnings``, ``set_collector_status``), never a raw row. The screener runs on the
wall clock, so the seeded expiry is the Friday nearest 42 days after today's ET date.

NO network: "Refresh now" is driven either with ``opt_massive.ingest_symbol`` replaced
by a recorder, or with the REAL ``massive.Client`` + ``ingest_symbol`` over a fake HTTP
layer (``_FakeHttp``) - the key in those tests is a made-up string, and every test
checks it never reaches the page.
"""
from __future__ import annotations

import ast
import datetime as _dt
import json
import re
from pathlib import Path

import httpx
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from markupsafe import escape
from sqlalchemy.orm import sessionmaker

from app import models
from app.db import get_db
from app.main import app
from app.security import current_user
from app.services import clock, massive, opt_massive, opt_rules, opt_screen, opt_store

from .fixtures.options import chain_bs

APP_DIR = Path(__file__).resolve().parent.parent / "app"
TEMPLATES = APP_DIR / "templates"
FRAGMENTS = ("_opt_basket.html", "_opt_rules.html", "_opt_results.html", "_opt_trade.html",
             "_opt_status.html", "_opt_help.html")
FORBIDDEN = ("option_engine", "chart_state", "strategy_rules", "strike_picker", "premium_gauge",
             "order_ticket", "telegram_push", "option_exits", "option_nightly", "option_data",
             "option_backfill", "option_vol", "option_words", "option_sizing", "option_prefs",
             "opt_connector_pkg")
REMOVED_PATHS = ("/options/connector", "/options/connector/download", "/options/data/next",
                 "/options/data/contribute", "/options/data/contribute_history", "/options/data/failed")
FAKE_KEY = "mk-test-NOT-A-REAL-KEY-7c1d"
SPOT = 100.0
DTE = 42


# ───────────────────────────────── the seeded world ─────────────────────────────────

def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


def _friday(dte: int) -> _dt.date:
    """The Friday nearest ``dte`` days after today's ET date (within 3 days): a real
    listed expiry, so the tests never depend on the weekday they run on."""
    d = clock.et_date() + _dt.timedelta(days=dte)
    shift = 4 - d.weekday()
    if shift > 3:
        shift -= 7
    return d + _dt.timedelta(days=shift)


def _chain_rows(*, iv: float = 0.45, spread: float = 0.10, strikes=range(80, 121),
                dte: int = DTE, quotes: bool = True) -> tuple[str, list[dict]]:
    """(expiry, opt_massive-shaped rows) - a Black-Scholes chain on the Friday nearest
    ``dte`` days out, spot 100, strikes on the $1 grid. ``quotes=False`` = what Massive
    Options Starter gives: no bid / ask, the price a model price from the IV."""
    today = clock.et_date()
    exp = _friday(dte).isoformat()
    ch = chain_bs(SPOT, iv, [exp], [float(k) for k in strikes], today=today.isoformat(), spread=spread)
    rows = []
    for leg in ch["legs"].values():
        r = {k: leg[k] for k in ("expiry", "right", "strike", "bid", "ask", "mid", "last", "bid_size",
                                 "ask_size", "volume", "iv", "delta", "gamma", "theta", "vega")}
        r["oi"] = leg["open_interest"]
        r["und_price"] = SPOT
        if not quotes:
            r.update(bid=None, ask=None, bid_size=None, ask_size=None, mid=round(leg["theo"], 4))
        rows.append(r)
    return exp, rows


def _days(n: int) -> list[str]:
    """The last ``n`` weekdays before today's ET date, oldest first."""
    out, d = [], clock.et_date() - _dt.timedelta(days=1)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= _dt.timedelta(days=1)
    return out[::-1]


def _history(*, falling: bool = False) -> tuple[list[dict], list[dict]]:
    """300 daily bars (close 100, high 101, low 99 -> ATR 2.0) and a year of IV30 points
    ending high in its range (rank ~74) or, with ``falling``, at its low (rank 0)."""
    days = _days(300)
    bars = [{"on": d, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1_000_000}
            for d in days]
    n = 260
    if falling:
        ivs = [{"on": days[-n + i], "iv": 40.0 - 20.0 * i / (n - 1)} for i in range(n)]
    else:
        ivs = [{"on": days[-n + i], "iv": 20.0 + 20.0 * i / (n - 2)} for i in range(n - 1)]
        ivs.append({"on": days[-1], "iv": 35.0})
    return bars, ivs


def _seed(db, sym: str, *, falling_iv: bool = False, as_of=None, quotes: bool = True) -> None:
    _, rows = _chain_rows(quotes=quotes)
    opt_store.upsert_quotes(db, sym, rows, source="massive", mdt="delayed", kind="cycle", spot=SPOT,
                            as_of=as_of or _utcnow())
    bars, ivs = _history(falling=falling_iv)
    opt_store.upsert_daily(db, sym, bars, ivs, source="massive")
    opt_store.recompute_underlying(db, sym)
    opt_store.set_earnings(db, sym, (clock.et_date() + _dt.timedelta(days=120)).isoformat())
    opt_store.mark_history_done(db, sym)


@pytest.fixture
def world(engine, db, user, monkeypatch):
    """The member (basket LRCX, MSFT, KO, NVDA), a second member 'Kui' (basket LRCX) and
    an administrator; Massive data for LRCX / MSFT (bull puts pass) and KO (IV rank 0 -
    no bull put passes), none for NVDA (waiting for its first read). The data plan has
    no quotes (TST_MASSIVE_QUOTES unset) and no key is configured."""
    from app.routes import options_page as op

    monkeypatch.delenv("TST_MASSIVE_API_KEY", raising=False)
    monkeypatch.delenv("TST_MASSIVE_QUOTES", raising=False)
    monkeypatch.delenv("TST_OPTIONS_CYCLE_MIN", raising=False)
    opt_store.reset_state()
    op.reset_refresh_limits()
    other = models.User(email="kui@local.test", display_name="Kui", role=models.ROLE_MEMBER,
                        status=models.APPROVED, created_at=_dt.datetime(2026, 1, 6, tzinfo=_dt.timezone.utc))
    admin = models.User(email="boss@local.test", display_name="Boss", role=models.ROLE_ADMIN,
                        status=models.APPROVED, created_at=_dt.datetime(2026, 1, 7, tzinfo=_dt.timezone.utc))
    db.add_all([other, admin])
    db.commit()
    res = op._add_symbols(db, user, ["LRCX", "MSFT", "KO", "NVDA"], "paste")
    assert res["added"] == 4 and res["new"] == ["LRCX", "MSFT", "KO", "NVDA"]
    op._add_symbols(db, other, ["LRCX"], "paste")
    _seed(db, "LRCX")
    _seed(db, "MSFT")
    _seed(db, "KO", falling_iv=True)

    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    state = {"uid": user.id}

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    def _user(s=Depends(get_db)):
        """The same session the handler gets (FastAPI caches get_db per request)."""
        return s.get(models.User, state["uid"])

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[current_user] = _user
    try:
        yield {"client": TestClient(app, follow_redirects=False), "uid": user.id, "oid": other.id,
               "aid": admin.id, "Session": Session, "state": state, "op": op}
    finally:
        app.dependency_overrides.clear()
        opt_store.reset_state()
        op.reset_refresh_limits()


def _as(world, uid):
    world["state"]["uid"] = uid


def _ids(html: str) -> list[str]:
    return re.findall(r'data-cand-id="([^"]+)"', html)


def _trigger(resp, header: str = "HX-Trigger") -> dict:
    return json.loads(resp.headers.get(header) or "{}")


def _screen(world, strategy="bull_put", syms=("LRCX", "MSFT", "KO", "NVDA")):
    """What opt_screen says passes for the member's basket over FULL chains (no window,
    no age pre-filter), read straight from the store."""
    s = world["Session"]()
    try:
        u = s.get(models.User, world["uid"])
        chains = {x: opt_store.chain_view(s, x) for x in syms}
        return opt_screen.screen(strategy, chains, opt_store.underlyings(s, list(syms)),
                                 opt_rules.for_strategy(opt_rules.read(s, u), strategy))
    finally:
        s.close()


def _add(world, uid, syms):
    s = world["Session"]()
    try:
        world["op"]._add_symbols(s, s.get(models.User, uid), list(syms), "paste")
    finally:
        s.close()


def _put_status(world, **fields):
    s = world["Session"]()
    try:
        opt_store.set_collector_status(s, **fields)
    finally:
        s.close()


def _src(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


# ───────────────────────────────── a fake Massive (no network) ─────────────────────────────────

class _Resp:
    def __init__(self, status: int, payload=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON")
        return self._payload


class _FakeHttp:
    """Stands in for the client's httpx.Client: answers every GET with ``answer`` (a
    _Resp, an exception to raise, or a callable(url, params) -> _Resp) and records it."""

    def __init__(self, answer):
        self.answer = answer
        self.calls: list[dict] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {})})
        a = self.answer
        if isinstance(a, BaseException):
            raise a
        return a(url, params) if callable(a) else a

    def close(self):
        pass


def _snapshot_json(sym: str, *, quotes: bool = False, age_min: float = 16.0) -> dict:
    """One chain-snapshot page as Massive sends it (/v3/snapshot/options/{sym}):
    details / greeks / implied_volatility / open_interest / day / underlying_asset, and
    ``last_quote`` only with ``quotes`` (Options Starter has none). Timestamps are
    nanoseconds, ``age_min`` minutes old (the 15-min delay plus a little)."""
    _, rows = _chain_rows()
    stamp = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=age_min)
    ns = int(stamp.timestamp() * 1_000_000_000)
    results = []
    for r in rows:
        res = {"details": {"contract_type": "call" if r["right"] == "C" else "put", "exercise_style": "american",
                           "expiration_date": r["expiry"], "shares_per_contract": 100, "strike_price": r["strike"],
                           "ticker": massive.option_ticker(sym, r["expiry"], r["right"], r["strike"])},
               "greeks": {"delta": r["delta"], "gamma": r["gamma"], "theta": r["theta"], "vega": r["vega"]},
               "implied_volatility": r["iv"], "open_interest": r["oi"],
               "break_even_price": r["strike"],
               "day": {"close": r["last"], "open": r["last"], "high": r["last"], "low": r["last"],
                       "previous_close": r["last"], "volume": r["volume"], "vwap": r["last"],
                       "change": 0.0, "change_percent": 0.0, "last_updated": ns},
               "underlying_asset": {"ticker": sym, "price": SPOT, "change_to_break_even": 0.0,
                                    "last_updated": ns, "timeframe": "DELAYED"}}
        if quotes:
            res["last_quote"] = {"bid": r["bid"], "ask": r["ask"], "bid_size": 10, "ask_size": 10,
                                 "midpoint": r["mid"], "last_updated": ns, "timeframe": "DELAYED"}
        results.append(res)
    return {"status": "OK", "request_id": "test", "results": results}


def _fake_massive(monkeypatch, op, answer, *, key=FAKE_KEY, real_sleep=False) -> _FakeHttp:
    """The refresh route's client becomes a REAL massive.Client over a fake HTTP layer.
    ``real_sleep``: keep the route's own capped wait (the 429 test); otherwise the
    client's retry pauses are skipped."""
    http = _FakeHttp(answer)

    def factory():
        return massive.Client(api_key=key, base_url="http://massive.test", http=http,
                              sleep=op._refresh_sleep if real_sleep else (lambda s: None))

    monkeypatch.setattr(op, "_massive_client", factory)
    return http


# ───────────────────────────────── shell, grant, router ─────────────────────────────────

def test_page_renders_for_an_approved_member(world):
    r = world["client"].get("/options")
    assert r.status_code == 200
    html = r.text
    assert 'href="/options"' in html and ">Options<" in html                 # the nav item
    assert 'id="optStrategy"' in html
    for key in opt_rules.STRATEGIES:                                          # the dropdown: all ten, one shown at a time
        assert f'<option value="{key}"' in html and str(escape(opt_rules.LABELS[key])) in html
    assert 'value="bull_put" selected' in html
    assert 'hx-get="/options/status"' in html and 'every 60s' in html
    assert 'id="optHelpBtn"' in html and 'id="optRulesHost"' in html and 'id="optResultsHost"' in html
    # the v4.133 connector pill and download are gone
    for gone in ('id="optConnPill"', 'id="optConnUpdate"', "/options/connector", "Download connector"):
        assert gone not in html, gone
    # the basket is loaded by the page script (with the remembered sort), not by an hx-get
    assert 'id="optBasket"' in html and "'/options/basket?sort='" in html and 'id="optDivider"' in html
    # old query strings from v1 links still land on the page
    assert world["client"].get("/options?symbol=lrcx&tab=positions").status_code == 200


def test_member_without_the_options_grant_is_redirected(world):
    c = world["client"]
    s = world["Session"]()
    try:
        u = s.get(models.User, world["uid"])
        u.menu_access = ["calendar_month"]
        s.commit()
        for path in ("/options", "/options/basket", "/options/results", "/options/help", "/options/status"):
            assert c.get(path).status_code == 303, path
        assert c.post("/options/refresh/LRCX").status_code == 303
        u.menu_access = None
        s.commit()
    finally:
        s.close()
    assert c.get("/options/basket").status_code == 200


def test_router_order_removed_routes_and_no_v1_engine_imports():
    paths = [getattr(r, "path", None) for r in app.routes]
    catch_all = paths.index("/options/{symbol}")
    for p in ("/options/basket", "/options/rules", "/options/results", "/options/trade", "/options/payoff",
              "/options/status", "/options/help", "/options/refresh/{symbol}"):
        assert paths.index(p) < catch_all, p
    # The v1 nav badge went with the Positions tab; the v4.133 connector + contribution routes went in v4.134
    assert "/options/badge" not in paths
    assert "/options/badge" not in (TEMPLATES / "base.html").read_text(encoding="utf-8")
    for p in REMOVED_PATHS:
        assert p not in paths, p
    src = (APP_DIR / "routes" / "options_page.py").read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            imported.update(a.name.rsplit(".", 1)[-1] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.update(a.name for a in node.names)
            imported.add((node.module or "").rsplit(".", 1)[-1])
    assert not (imported & set(FORBIDDEN)), imported & set(FORBIDDEN)
    assert "Thread(" not in src                                               # no v4.131 first-read thread
    for gone in ("opt_connector_pkg", "MAX_BODY_BYTES", "contribute", "next_for_member", "report_failure"):
        assert gone not in src, gone
    assert not (TEMPLATES / "_opt_connector.html").exists()


def test_every_route_answers(world):
    c = world["client"]
    for path in ["/options/basket", "/options/basket?sort=fresh", "/options/basket?sort=iv",
                 "/options/basket?sort=symbol", "/options/basket?part=rows&sort=iv", "/options/results",
                 "/options/results?strategy=nope",
                 "/options/results?strategy=iron_condor&sort=pop&dir=asc", "/options/status",
                 "/options/help", "/options/rules", "/options/trade?strategy=bull_put&id=junk",
                 "/options/payoff?strategy=bull_put&id=junk"]:
        r = c.get(path)
        assert r.status_code == 200, (path, r.status_code, r.text[:300])
    for path in ("/options/data/contribute", "/options/data/contribute_history", "/options/data/failed"):
        assert c.post(path, json={}).status_code in (404, 405), path       # the contribution endpoints are gone


# ───────────────────────────────── the basket ─────────────────────────────────

def test_basket_rows_freshness_refresh_button_and_waiting(world):
    html = world["client"].get("/options/basket").text
    assert html.count('class="opt-brow') == 4
    for sym in ("LRCX", "MSFT", "KO", "NVDA"):
        assert f'data-sym="{sym}"' in html
        assert f'hx-post="/options/refresh/{sym}"' in html and f'data-refresh-sym="{sym}"' in html
    nvda = html.split('data-sym="NVDA"', 1)[1].split('class="opt-brow', 1)[0]
    assert "waiting for first read" in nvda and "next pass" in nvda
    lrcx = html.split('data-sym="LRCX"', 1)[1].split('class="opt-brow', 1)[0]
    assert "Massive (delayed)" in lrcx and "bg-emerald-400" in lrcx and "waiting for first read" not in lrcx
    assert 'hx-target="closest .opt-brow"' in html and 'hx-swap="outerHTML"' in html
    # the spinner is an inner span: with hx-disabled-elt="this" on the indicator itself, htmx 1.9
    # counts both in one counter and the spinning class never came off after an answer
    assert html.count('hx-indicator="find .opt-spin" hx-disabled-elt="this"') == 4
    assert html.count('<span class="opt-spin inline-block">&#8635;</span>') == 4
    assert 'class="opt-count' in html                                        # the cell the counts are painted into
    assert "onclick" not in html and "<script" not in html
    # the waiting note names the collector's pass and the refresh button - no Gateway / connector words
    assert "every 15 min while the US market is open" in html
    for word in ("Gateway", "connector", "20:10 ET", "IBKR's own"):
        assert word not in html, word


def test_basket_add_remove_import_cap_and_duplicates(world):
    c = world["client"]
    r = c.post("/options/basket/add", data={"symbol": "amd"})
    assert r.status_code == 200 and 'data-sym="AMD"' in r.text
    ev = _trigger(r)
    assert "options:universe-changed" in ev and "added" in ev["options:toast"]["msg"]
    assert "refresh" in ev["options:toast"]["msg"] and "Massive" in ev["options:toast"]["msg"]
    amd = r.text.split('data-sym="AMD"', 1)[1].split('class="opt-brow', 1)[0]
    assert "waiting for first read" in amd                                   # no first-read machinery: just no data yet
    r = c.post("/options/basket/add", data={"symbol": "AMD"})
    assert "already in your basket" in _trigger(r)["options:toast"]["msg"]
    r = c.post("/options/basket/remove", data={"symbol": "AMD"})
    assert r.status_code == 200 and 'data-sym="AMD"' not in r.text

    syms = [f"T{i:03d}" for i in range(70)]
    r = c.post("/options/basket/import", json={"source": "paste", "text": ", ".join(syms)})
    assert r.status_code == 200
    assert r.json() == {"added": 56, "skipped": 0, "over_cap": 14, "total": 60}      # 4 already in the basket
    assert "options:basket-changed" in _trigger(r)
    _as(world, world["oid"])
    try:
        r = c.post("/options/basket/import", json={"source": "paste", "text": " ".join(syms)})
        assert r.json() == {"added": 59, "skipped": 0, "over_cap": 11, "total": 60}  # Kui already holds LRCX
        r = c.post("/options/basket/import", json={"source": "paste", "text": "T000, t001, XX1234567890123"})
        assert r.json()["added"] == 0 and r.json()["skipped"] >= 2               # duplicates + junk, never added
        for src in ("watchlist", "ivscan_list", "ivscan_scan", "positions", "scanner"):
            assert c.post("/options/basket/import", json={"source": src, "symbols": []}).status_code == 200
    finally:
        _as(world, world["uid"])
    s = world["Session"]()
    try:
        n = s.query(models.OptionBasket).filter(models.OptionBasket.owner_key == f"u{world['uid']}",
                                                models.OptionBasket.active.is_(True)).count()
        assert n == 60
    finally:
        s.close()


def test_basket_refresh_swaps_only_the_rows_and_keeps_the_sort(world):
    """A refresh (a sort click, add / remove, a finished collector pass) answers the rows
    container only, so the Add / Import footer - the typed ticker, the pasted list, an
    open panel, the scan criteria - survives, and the chosen sort rides along."""
    c = world["client"]
    full = c.get("/options/basket?sort=iv").text
    assert 'id="optBasketRows"' in full and 'id="optAddForm"' in full and 'id="optPasteBox"' in full
    assert 'data-sort="iv"' in full
    rows = c.get("/options/basket?part=rows&sort=fresh").text
    assert 'class="opt-basket-rows-inner" data-sort="fresh"' in rows and rows.count('class="opt-brow') == 4
    for footer in ('id="optAddForm"', 'id="optPasteBox"', 'id="optScanRank"', "+ Add ticker", "Import"):
        assert footer not in rows, footer
    assert 'hx-target="#optBasketRows"' in rows and "part=rows" in rows
    r = c.post("/options/basket/add", data={"symbol": "AMD", "sort": "symbol", "part": "rows"})
    assert 'data-sym="AMD"' in r.text and 'data-sort="symbol"' in r.text and 'id="optAddForm"' not in r.text
    order = re.findall(r'class="opt-brow[^"]*"[^>]*data-sym="([A-Z]+)"', r.text)
    assert order == sorted(order)
    r = c.post("/options/basket/remove", data={"symbol": "AMD", "sort": "iv", "part": "rows"})
    assert 'data-sym="AMD"' not in r.text and 'data-sort="iv"' in r.text and 'id="optPasteBox"' not in r.text
    src = _src("options.html")
    for needle in ("'/options/basket?part=rows&sort='", "th.options.basketSort", "htmx:configRequest",
                   "d.parameters.sort = state.bsort", "'#optBasketRows'"):
        assert needle in src, needle


# ───────────────────────────────── "Refresh now" ─────────────────────────────────

def test_refresh_now_reads_the_ticker_and_answers_its_row(world, monkeypatch):
    """POST /options/refresh/<sym> -> opt_massive.ingest_symbol(db, <Massive client>, sym,
    kind="manual") on the server; the answer is that basket row, a toast, and (after the
    swap) options:refreshed, which re-screens the list."""
    op = world["op"]
    calls = []
    sentinel = object()
    monkeypatch.setattr(op, "_massive_client", lambda: sentinel)

    def fake_ingest(db, client, symbol, *, kind="cycle", **kw):
        calls.append((client, symbol, kind))
        _, rows = _chain_rows(quotes=False)
        opt_store.upsert_quotes(db, symbol, rows, source="massive", mdt="delayed", kind=kind, spot=SPOT,
                                as_of=_utcnow() - _dt.timedelta(minutes=16))
        return {"symbol": symbol, "stored": len(rows), "expiries": 1, "spot": SPOT, "spot_kind": "massive"}

    monkeypatch.setattr(opt_massive, "ingest_symbol", fake_ingest)
    c = world["client"]
    r = c.post("/options/refresh/nvda")
    assert r.status_code == 200, r.text
    assert calls == [(sentinel, "NVDA", "manual")]
    assert r.text.count('class="opt-brow') == 1 and 'data-sym="NVDA"' in r.text      # the row alone
    assert "waiting for first read" not in r.text and "Massive (delayed)" in r.text
    assert 'hx-post="/options/refresh/NVDA"' in r.text and 'id="optBasketRows"' not in r.text
    toast = _trigger(r)["options:toast"]
    assert toast["kind"] == "ok" and "NVDA" in toast["msg"] and "82 contracts" in toast["msg"]
    ev = _trigger(r, "HX-Trigger-After-Settle")["options:refreshed"]
    assert ev == {"symbol": "NVDA", "stored": 82, "src": "basket"}
    s = world["Session"]()
    try:
        log = (s.query(models.OptRefreshLog).filter(models.OptRefreshLog.symbol == "NVDA")
                 .order_by(models.OptRefreshLog.id.desc()).first())
        assert log.kind == "manual" and log.source == "massive" and log.n_contracts == 82
    finally:
        s.close()
    # the list now screens NVDA's chain (it has no history yet, so a stock filter is its reason)
    res = c.get("/options/results?strategy=bull_put")
    assert "no option data" not in _trigger(res)["options:counts"]["reasons"]["NVDA"]
    # from the trade detail: src=trade rides along (the page reloads the basket rows itself)
    r = c.post("/options/refresh/LRCX", data={"src": "trade"})
    assert r.status_code == 200 and _trigger(r, "HX-Trigger-After-Settle")["options:refreshed"]["src"] == "trade"
    # Massive answered nothing: said plainly, still a 200 with the row
    monkeypatch.setattr(opt_massive, "ingest_symbol", lambda db, client, symbol, **kw: {"stored": 0})
    r = c.post("/options/refresh/MSFT")
    assert r.status_code == 200 and "no option contracts" in _trigger(r)["options:toast"]["msg"]


def test_refresh_now_only_for_a_ticker_in_your_basket(world, monkeypatch):
    op = world["op"]
    calls = []
    monkeypatch.setattr(op, "_massive_client", lambda: object())
    monkeypatch.setattr(opt_massive, "ingest_symbol", lambda *a, **k: calls.append(a) or {"stored": 1})
    c = world["client"]
    for sym in ("ZZZZ", "bad!sym"):
        r = c.post(f"/options/refresh/{sym}")
        assert r.status_code == 400, sym
        assert "not in your basket" in _trigger(r)["options:toast"]["msg"] and r.headers["HX-Reswap"] == "none"
    _as(world, world["oid"])                                                 # Kui holds LRCX only
    try:
        assert c.post("/options/refresh/MSFT").status_code == 400
        assert c.post("/options/refresh/LRCX").status_code == 200
    finally:
        _as(world, world["uid"])
    assert len(calls) == 1
    # a ticker removed from the basket can no longer be refreshed
    c.post("/options/basket/remove", data={"symbol": "KO"})
    assert c.post("/options/refresh/KO").status_code == 400 and len(calls) == 1


def test_refresh_now_at_most_once_a_minute_per_ticker_per_member(world, monkeypatch):
    op = world["op"]
    clock_s = [1000.0]
    calls = []
    monkeypatch.setattr(op, "_now_s", lambda: clock_s[0])
    monkeypatch.setattr(op, "_massive_client", lambda: object())
    monkeypatch.setattr(opt_massive, "ingest_symbol", lambda db, client, sym, **kw: calls.append(sym) or {"stored": 5})
    c = world["client"]
    assert c.post("/options/refresh/LRCX").status_code == 200
    clock_s[0] += 30
    r = c.post("/options/refresh/LRCX")
    assert r.status_code == 429 and r.headers["Retry-After"] == "30"
    assert "try again in 30 s" in _trigger(r)["options:toast"]["msg"] and r.headers["HX-Reswap"] == "none"
    assert "options:refreshed" not in r.headers.get("HX-Trigger-After-Settle", "")
    assert c.post("/options/refresh/MSFT").status_code == 200                # another ticker: its own minute
    _as(world, world["oid"])
    try:
        assert c.post("/options/refresh/LRCX").status_code == 200            # another member: their own minute
    finally:
        _as(world, world["uid"])
    clock_s[0] += 31
    assert c.post("/options/refresh/LRCX").status_code == 200                # the minute is over
    assert calls == ["LRCX", "MSFT", "LRCX", "LRCX"]


@pytest.mark.parametrize("answer, status, words", [
    (_Resp(401, {"status": "ERROR", "error": "Unknown API Key"}), 502, "Massive rejected the API key"),
    (_Resp(403, {"status": "NOT_AUTHORIZED"}), 502, "plan does not include"),
    (_Resp(429, {"status": "ERROR"}), 503, "Massive is busy, try again in a minute"),
    (_Resp(500, {"status": "ERROR"}), 502, "Massive answered with an error (HTTP 500)"),
    (httpx.ConnectError("connection refused"), 503, "Massive could not be reached"),
])
def test_refresh_now_says_plainly_why_massive_failed(world, monkeypatch, answer, status, words):
    """Each MassiveError kind through the REAL client over a fake HTTP layer: the member
    gets the plain reason (body + toast), the row is not swapped, nothing is stored, and
    the key never reaches the page. A 429 answers at once (the route's client never waits
    out Massive's back-off inside the request)."""
    op = world["op"]
    http = _fake_massive(monkeypatch, op, answer, real_sleep=(status == 503 and "busy" in words))
    t0 = _dt.datetime.now()
    r = world["client"].post("/options/refresh/NVDA")
    assert (_dt.datetime.now() - t0).total_seconds() < 10
    assert r.status_code == status, r.text
    msg = _trigger(r)["options:toast"]["msg"]
    assert words in msg and words in r.text and msg.startswith("NVDA: ")
    assert r.headers["HX-Reswap"] == "none" and "HX-Trigger-After-Settle" not in r.headers
    assert FAKE_KEY not in r.text and FAKE_KEY not in json.dumps(dict(r.headers))
    assert http.calls and http.calls[0]["headers"]["Authorization"] == "Bearer " + FAKE_KEY
    assert all(FAKE_KEY not in call["url"] and FAKE_KEY not in json.dumps(call["params"]) for call in http.calls)
    s = world["Session"]()
    try:
        assert s.query(models.OptQuote).filter(models.OptQuote.symbol == "NVDA").count() == 0
    finally:
        s.close()


def test_refresh_now_without_a_key_says_massive_is_not_set_up(world, monkeypatch):
    """No TST_MASSIVE_API_KEY on the server (the real factory, no HTTP at all): the
    member reads "Massive is not set up on the server", and the minute is not used up."""
    c = world["client"]
    for _ in range(2):                                                        # the slot was given back
        r = c.post("/options/refresh/LRCX")
        assert r.status_code == 503
        assert _trigger(r)["options:toast"]["msg"] == "LRCX: Massive is not set up on the server"


def test_refresh_now_through_the_real_ingest_files_the_chain(world, monkeypatch):
    """End to end with no network: the real massive.Client parses a fake Options Starter
    snapshot (no last_quote), the real opt_massive.ingest_symbol files it, and the row
    and the list show it as Massive's delayed data priced from IV."""
    op = world["op"]
    http = _fake_massive(monkeypatch, op, _Resp(200, _snapshot_json("NVDA", quotes=False)))
    c = world["client"]
    r = c.post("/options/refresh/NVDA")
    assert r.status_code == 200, r.text
    assert len(http.calls) == 1 and http.calls[0]["url"].endswith("/v3/snapshot/options/NVDA")
    assert 'data-sym="NVDA"' in r.text and "Massive (delayed)" in r.text and "waiting for first read" not in r.text
    stored = _trigger(r, "HX-Trigger-After-Settle")["options:refreshed"]["stored"]
    assert stored > 0
    s = world["Session"]()
    try:
        q = s.query(models.OptQuote).filter(models.OptQuote.symbol == "NVDA").all()
        assert len(q) == stored and {x.source for x in q} == {"massive"} and {x.mdt for x in q} == {"delayed"}
        assert all(x.bid is None and x.ask is None and x.mid and x.mid > 0 for x in q)   # model prices
    finally:
        s.close()
    assert FAKE_KEY not in r.text


# ───────────────────────────────── rules ─────────────────────────────────

def test_rules_fragment_for_all_ten_strategies(world):
    c = world["client"]
    for strategy in opt_rules.STRATEGIES:
        r = c.get(f"/options/rules?strategy={strategy}")
        assert r.status_code == 200, strategy
        html = r.text
        assert 'id="optRules"' in html and f'data-strategy="{strategy}"' in html
        assert str(escape(opt_rules.LABELS[strategy])) in html and "Reset to defaults" in html
        for block, name, f in opt_rules.fields(strategy):
            assert f'name="{block}.{name}"' in html, (strategy, block, name)
            assert str(escape(f.label)) in html and str(escape(f.help)) in html, (strategy, name)
            assert f'id="rerr-{block}-{name}"' in html
        assert html.count('hx-trigger="change changed, keyup changed delay:600ms"') >= len(
            [1 for _, _, f in opt_rules.fields(strategy) if f.kind in ("int", "num", "choice")])
        assert f'hx-post="/options/rules?strategy={strategy}"' in html
        assert "<script" not in html


def test_bid_ask_rules_are_greyed_out_without_quotes(world, monkeypatch):
    """§13.5: while the data plan has no bid/ask (TST_MASSIVE_QUOTES 0), the two bid/ask
    rules are greyed out with opt_rules.UNUSED_WITHOUT_QUOTES' note - still editable and
    stored; with quotes they are ordinary fields again."""
    c = world["client"]
    html = c.get("/options/rules?strategy=bull_put").text
    unused = re.findall(r'data-field="([^"]+)" data-unused="1"', html)
    assert sorted(unused) == ["bull_put.max_leg_spread", "shared.max_leg_spread_pct"]
    for key, note in opt_rules.UNUSED_WITHOUT_QUOTES.items():
        assert str(escape(note)) in html
    assert html.count('class="opt-unused-note') == 2 and "opt-unused opacity-50" in html
    assert 'name="bull_put.max_leg_spread"' in html                          # still there, still saved
    r = c.post("/options/rules?strategy=bull_put", data={"bull_put.max_leg_spread": "0.4"})
    assert r.status_code == 200 and "Saved" in r.text
    # every strategy carries its own $ cap (§7 R10) and the shared % rule: both greyed
    for strategy in opt_rules.STRATEGIES:
        html = c.get(f"/options/rules?strategy={strategy}").text
        assert sorted(re.findall(r'data-field="([^"]+)" data-unused="1"', html)) == \
            sorted([f"{strategy}.max_leg_spread", "shared.max_leg_spread_pct"]), strategy
    monkeypatch.setenv("TST_MASSIVE_QUOTES", "1")
    html = c.get("/options/rules?strategy=bull_put").text
    assert 'data-unused="1"' not in html and "opt-unused-note" not in html


def test_number_steps_line_up_with_min_and_default(world):
    """A browser counts steps from ``min``; every number field's rendered step puts both
    its min and its default on the grid (the arrows never turn 0.50 into 0.51)."""
    op = world["op"]
    c = world["client"]
    for strategy in opt_rules.STRATEGIES:
        html = c.get(f"/options/rules?strategy={strategy}").text
        for block, name, f in opt_rules.fields(strategy):
            if f.kind not in ("int", "num"):
                continue
            st = op._input_step(name, f)
            assert st and st > 0, (strategy, name)
            n = (float(f.default) - float(f.lo)) / st
            assert abs(n - round(n)) < 1e-6, (strategy, name, f.lo, f.default, st)
            tag = re.search(r'<input type="number" id="rf-%s-%s"[^>]*>' % (re.escape(block), re.escape(name)), html, re.S)
            assert tag and f'step="{st:g}"' in tag.group(0), (strategy, name, st)
    f = opt_rules.SCHEMA["bull_put"]["max_leg_spread"]
    assert op._input_step("max_leg_spread", f) in (0.05, 0.01)


def test_rule_change_saves_and_changes_the_results(world):
    c = world["client"]
    before = _ids(c.get("/options/results?strategy=bull_put").text)
    assert len(before) == 6                                                  # 3 per ticker x LRCX, MSFT

    r = c.post("/options/rules?strategy=bull_put", data={"shared.per_ticker": "1"})
    assert r.status_code == 200
    assert _trigger(r)["options:rules-changed"]["strategy"] == "bull_put"
    assert "Saved" in r.text and 'hx-swap-oob="true"' in r.text
    assert 'id="optRules"' not in r.text                                     # the inputs are never re-rendered
    after = _ids(c.get("/options/results?strategy=bull_put").text)
    assert len(after) == 2 and set(after) <= set(before)

    # an out-of-bounds value is clamped, saved and explained in the field's own slot ...
    r = c.post("/options/rules?strategy=bull_put", data={"bull_put.short_delta_hi": "5"})
    slot = r.text.split('id="rerr-bull_put-short_delta_hi"', 1)[1].split("</span>", 1)[0]
    assert "over the highest allowed" in slot
    hi = opt_rules.SCHEMA["bull_put"]["short_delta_hi"].hi
    assert _trigger(r)["options:rules-clamped"] == {"rf-bull_put-short_delta_hi": {"sent": "5", "value": f"{hi:g}"}}
    r = c.post("/options/rules?strategy=bull_put", data={"shared.per_ticker": "2"})
    assert 'id="rerr-shared-per_ticker"' in r.text and 'id="rerr-bull_put-short_delta_hi"' not in r.text
    r = c.post("/options/rules?strategy=bull_put", data={"bull_put.short_delta_hi": "0.3"})
    slot = r.text.split('id="rerr-bull_put-short_delta_hi"', 1)[1].split("</span>", 1)[0]
    assert slot.rstrip().endswith('role="status">')
    src = _src("options.html")
    assert "options:rules-clamped" in src and "el.value = v.value" in src

    c.post("/options/rules?strategy=bull_put", data={"shared.monthly_only": ["off", "on"]})
    s = world["Session"]()
    try:
        assert opt_rules.read(s, s.get(models.User, world["uid"]))["shared"]["monthly_only"] is True
    finally:
        s.close()
    c.post("/options/rules?strategy=bull_put", data={"shared.monthly_only": "off"})
    r = c.post("/options/rules/reset?strategy=bull_put")
    assert r.status_code == 200 and 'id="optRules"' in r.text and "options:rules-changed" in _trigger(r)
    assert len(_ids(c.get("/options/results?strategy=bull_put").text)) == 6


def test_failed_requests_are_never_silent():
    """A failed autosave / reset / basket edit / refresh, or a request that never
    reached the server, says so; a refresh the server refused already carries its own
    toast, so the page adds none."""
    src = _src("options.html")
    for needle in ("htmx:responseError", "htmx:sendError", "optRulesMsg", "'Not reset'", "'Not saved'",
                   "'HTTP ' + (xhr.status || '?')", "the server could not be reached",
                   "Your basket was not updated", "elt.closest('#optRules')", "elt.closest('#optBasket')",
                   "elt.closest('[data-refresh-sym]')", "getResponseHeader('HX-Trigger')"):
        assert needle in src, needle


# ───────────────────────────────── results ─────────────────────────────────

def test_results_show_only_passing_trades_with_counts_and_data_words(world):
    c = world["client"]
    r = c.get("/options/results?strategy=bull_put")
    assert r.status_code == 200
    html = r.text
    expected = _screen(world)
    assert set(_ids(html)) == {row["id"] for row in expected["rows"]} and expected["n_passed"] == 6
    assert 'data-sym="KO"' not in html and 'data-sym="NVDA"' not in html     # KO fails IV rank, NVDA has no data
    assert re.search(r"(now|\d+ min) · Massive \(delayed\)", html)           # the Data column
    assert "Hermes live" not in html and "IBKR" not in html
    assert "Why so few?" in html and "IV rank outside 30-100" in html
    ev = _trigger(r)["options:counts"]
    assert ev["counts"] == {"LRCX": 3, "MSFT": 3, "KO": 0, "NVDA": 0}
    assert "IV rank" in ev["reasons"]["KO"] and ev["summary"].startswith("6 trades pass")
    watch = json.loads(re.search(r"data-syms='([^']*)'", html).group(1))
    assert set(watch) == {"LRCX", "MSFT", "KO", "NVDA"}
    r = c.get("/options/results?strategy=bull_put&sort=max_loss&dir=asc")
    by_id = {row["id"]: row for row in expected["rows"]}
    losses = [by_id[i]["max_loss"] for i in _ids(r.text)]
    assert losses == sorted(losses) and 'data-sort="max_loss"' in r.text
    r = c.get("/options/results?strategy=leaps_call")
    assert _ids(r.text) == [] and "No trade passes" in r.text and "Why so few?" in r.text
    assert "<script" not in r.text


def test_data_column_reads_massive_delayed(world):
    """§13.6: the Data column reads e.g. "16 min · Massive (delayed)" - the market-time
    age, then the source; the tooltip adds the wall-clock age and the UTC time."""
    op = world["op"]
    oldest = _dt.datetime(2026, 10, 9, 14, 44)
    c = {"id": "X", "symbol": "LRCX", "legs": [], "dte": 40,
         "data": {"as_of_oldest": oldest, "age_min": 16.2, "wall_age_min": 16.2,
                  "sources": ["massive·delayed"], "mixed": False, "priced": "model"}}
    v = op._row_view("bull_put", c, rth=True)
    assert v["data_text"] == "16 min · Massive (delayed)"
    assert "Market-time age 16 min" in v["data_tip"] and "(2026-10-09 14:44 UTC)" in v["data_tip"]
    assert not v["data_amber"]                                               # amber only past an hour in RTH
    c["data"].update(age_min=0, wall_age_min=2 * 1440 + 30)
    v = op._row_view("bull_put", c, rth=False)
    assert v["data_text"] == "now · Massive (delayed)" and "data from 2 d ago" in v["data_tip"]
    # a legacy IBKR row still reads in words until it ages out
    assert op._sources_words(["hermes·live", "member:Kui·delayed"]) == "Hermes live + Kui (delayed)"
    html = world["client"].get("/options/results?strategy=bull_put").text
    assert "Market-time age" in html and "on the clock, data from" in html


def test_no_quotes_trades_are_priced_from_iv_and_the_funnel_says_so(world):
    """Massive Starter: legs with no bid/ask pass on their model price; the Credit/Debit
    tooltip says it is estimated from IV, Liquidity shows OI and volume (no spread), and
    the funnel's information line says the bid/ask rules were not checked."""
    _add(world, world["uid"], ["AMD"])
    s = world["Session"]()
    try:
        _seed(s, "AMD", quotes=False)
    finally:
        s.close()
    r = world["client"].get("/options/results?strategy=bull_put")
    assert _trigger(r)["options:counts"]["counts"]["AMD"] == 3
    amd = [row for row in re.findall(r'<tr class="opt-cand.*?</tr>', r.text, re.S) if 'data-sym="AMD"' in row]
    assert len(amd) == 3
    assert all("estimated from IV" in row for row in amd)
    assert all(re.search(r"OI [\d,]+ · vol [\d,]+", row) for row in amd)
    assert "opt-funnel-info" in r.text and "not checked" in r.text
    assert str(escape(opt_screen.NO_QUOTES_LABEL)) in r.text


def test_results_load_only_what_can_pass(world, monkeypatch):
    """A ticker that fails the stock filters on its stored facts is never loaded, and the
    others load only the strategy's expiry window and quotes younger than max_age_h + 96 h."""
    c = world["client"]
    calls: list[tuple[str, dict]] = []
    real = opt_store.chain_view

    def spy(db, symbol, **kw):
        calls.append((symbol, kw))
        return real(db, symbol, **kw)

    monkeypatch.setattr(opt_store, "chain_view", spy)
    assert len(_ids(c.get("/options/results?strategy=bull_put").text)) == 6
    assert sorted(s for s, _ in calls) == ["LRCX", "MSFT"]
    for _, kw in calls:
        assert (kw["dte_min"], kw["dte_max"], kw["max_age_h"]) == (30, 60, 24 + 96)
    calls.clear()
    c.get("/options/results?strategy=leaps_call")
    assert [s for s, _ in calls] == ["KO"] and (calls[0][1]["dte_min"], calls[0][1]["dte_max"]) == (273, 548)
    calls.clear()
    c.post("/options/rules?strategy=diagonal_call", data={"diagonal_call.iv_rank_max": "100"})
    c.get("/options/results?strategy=diagonal_call")
    assert {(kw["dte_min"], kw["dte_max"]) for _, kw in calls} == {(30, 365)}
    calls.clear()
    c.post("/options/rules?strategy=bull_put", data={"shared.price_min": "500"})
    r = c.get("/options/results?strategy=bull_put")
    assert calls == [] and _ids(r.text) == []
    reasons = _trigger(r)["options:counts"]["reasons"]
    assert "stock price" in reasons["LRCX"] and "stock price" in reasons["MSFT"]
    assert "no option data" in reasons["NVDA"]


def test_results_match_a_full_load_for_every_strategy(world):
    """The stubs keep the screen exact: for every strategy, the trades, the funnel, each
    ticker's verdict and n_considered equal a screen over the FULL chains - with an
    extra MSFT expiry outside most windows (120 days) in the pool."""
    s = world["Session"]()
    try:
        _, rows = _chain_rows(dte=120)
        opt_store.upsert_quotes(s, "MSFT", rows, source="massive", mdt="delayed", kind="cycle", spot=SPOT)
    finally:
        s.close()
    op = world["op"]
    for strategy in opt_rules.STRATEGIES:
        full = _screen(world, strategy)
        s = world["Session"]()
        try:
            ctx = op._results_context(s, s.get(models.User, world["uid"]), strategy, "score", "desc")
        finally:
            s.close()
        assert ctx["error"] is None, (strategy, ctx["error"])
        assert sorted(v["id"] for v in ctx["views"]) == sorted(r["id"] for r in full["rows"]), strategy
        assert ctx["funnel"] == full["funnel"], strategy
        assert ctx["tickers"] == full["tickers"], strategy
        assert ctx["n_considered"] == full["n_considered"], strategy


def test_results_name_a_ticker_whose_quotes_are_all_too_old(world):
    _add(world, world["uid"], ["AMD"])
    s = world["Session"]()
    try:
        _seed(s, "AMD", as_of=_utcnow() - _dt.timedelta(days=10))
    finally:
        s.close()
    r = world["client"].get("/options/results?strategy=bull_put")
    reasons = _trigger(r)["options:counts"]["reasons"]
    assert "too old" in reasons["AMD"] and "waiting for a fresh read" in reasons["AMD"]
    assert 'data-sym="AMD"' not in r.text and len(_ids(r.text)) == 6
    s = world["Session"]()
    try:
        opt_store.set_earnings(s, "AMD", None)
    finally:
        s.close()
    reasons = _trigger(world["client"].get("/options/results?strategy=bull_put"))["options:counts"]["reasons"]
    assert reasons["AMD"] == "earnings date unknown"


def test_results_page_script_debounces_and_never_closes_an_open_trade():
    """One results request in flight (a newer trigger queues one follow-up); a finished
    collector pass (the strip's data-pass changed) or a refresh re-screens, never while a
    trade is open (it runs when the trade closes); an open 'Why so few?' stays open."""
    src = _src("options.html")
    for needle in ("RESCREEN_MS = 5000", "function requestResults(bg)", "res.queued = true",
                   "res.afterClose = true", "function closedByMember()", "e.detail.shouldSwap = false",
                   "state.funnelOpen", "function scheduleRescreen()", "t.id === 'optStatus'",
                   "getAttribute('data-pass')", "pass !== state.pass", "'options:refreshed'",
                   "String(state.openId).split('|')[0] === d.symbol", "d.src === 'trade'"):
        assert needle in src, needle


# ───────────────────────────────── trade detail ─────────────────────────────────

def test_trade_detail_with_quotes_shows_the_bid_ask(world):
    """Data with quotes (a plan that has them): each leg's price is the bid/ask mid, the
    Bid / Ask columns and 'At the bid / ask' are shown."""
    c = world["client"]
    cid = _ids(c.get("/options/results?strategy=bull_put").text)[0]
    sym = cid.split("|")[0]
    r = c.get("/options/trade", params={"strategy": "bull_put", "id": cid})
    assert r.status_code == 200
    html = r.text
    assert 'class="po-pane' in html                                          # the payoff chart (existing partial)
    assert "Breakeven" in html and "Max profit" in html and "Max loss" in html and "POP" in html
    assert html.count(">Massive (delayed)<") == 2
    assert html.count(">bid/ask mid<") == 2 and ">model (from IV)<" not in html
    assert html.count('data-price-kind="mid"') == 2
    assert ">Bid<" in html and ">Ask<" in html and "At the bid / ask" in html
    assert f'hx-post="/options/refresh/{sym}"' in html and "Refresh now" in html
    assert """hx-vals='{"src": "trade"}'""" in html and 'hx-swap="none"' in html
    assert 'hx-indicator="find .opt-spin"' in html
    assert "check the live bid/ask in TWS before entering" in html
    for gone in ("Refresh these legs live", "data-refresh-legs", "data-spec", "connector", "IBKR connector"):
        assert gone not in html, gone
    assert "/options/payoff?strategy=bull_put&amp;id=" in html
    r = c.get("/options/payoff", params={"strategy": "bull_put", "id": cid, "units": "R"})
    assert r.status_code == 200 and 'class="po-pane' in r.text
    gone = c.get("/options/trade", params={"strategy": "bull_put", "id": f"{sym}|bull_put|2020-01-17|P|90|2020-01-17|P|85"})
    assert gone.status_code == 200 and "no longer in the data" in gone.text


def test_trade_detail_without_quotes_is_priced_from_iv(world):
    """Massive Starter: every leg says 'model (from IV)', there are no Bid / Ask columns,
    no 'At the bid / ask' (net_natural is None), and the TWS reminder says the price is
    estimated."""
    _add(world, world["uid"], ["AMD"])
    s = world["Session"]()
    try:
        _seed(s, "AMD", quotes=False)
    finally:
        s.close()
    c = world["client"]
    cid = next(i for i in _ids(c.get("/options/results?strategy=bull_put").text) if i.startswith("AMD|"))
    html = c.get("/options/trade", params={"strategy": "bull_put", "id": cid}).text
    assert html.count(">model (from IV)<") == 2 and ">bid/ask mid<" not in html
    assert html.count('data-price-kind="model"') == 2
    assert ">Bid<" not in html and ">Ask<" not in html
    assert "At the bid / ask" not in html and "opt-natural" not in html
    assert "estimated from IV - check the live price in TWS" in html and "(estimated)" in html
    assert re.search(r"OI [\d,]+ · vol [\d,]+", html)


def test_leg_view_names_the_price_source():
    from app.routes import options_page as op
    now = _utcnow()
    model = op._leg_view({"side": "sell", "right": "P", "strike": 95, "expiry": "2026-11-20", "mid": 1.234,
                          "bid": None, "ask": None, "priced": "model", "source": "massive", "mdt": "delayed",
                          "as_of": now - _dt.timedelta(minutes=16)}, now)
    assert model["price_text"] == "1.23" and model["price_kind"] == "model (from IV)" and not model["quoted"]
    assert model["src_text"] == "Massive (delayed)" and model["age_text"] == "16 min"
    quoted = op._leg_view({"side": "buy", "right": "C", "strike": 105, "expiry": "2026-11-20", "mid": 2.0,
                           "bid": 1.95, "ask": 2.05, "source": "massive", "mdt": "delayed", "as_of": now}, now)
    assert quoted["price_kind"] == "bid/ask mid" and quoted["quoted"]


def test_trade_detail_fits_a_phone():
    src = _src("options.html")
    for needle in ("function fitTradeBox()", "box.style.position = 'sticky'", "sc.clientWidth + 'px'",
                   "window.addEventListener('resize', fitTradeBox)"):
        assert needle in src, needle


# ───────────────────────────────── the status strip ─────────────────────────────────

def test_status_strip_renders_each_collector_state(world):
    c = world["client"]
    html = c.get("/options/status").text
    assert 'data-state="none"' in html and "no heartbeat yet" in html

    _put_status(world, state="cycle", mdt="delayed", cycle_n=12, symbols_total=100, symbols_done=98,
                last_error=None, cycle_finished=_utcnow() - _dt.timedelta(minutes=14))
    html = c.get("/options/status").text
    assert 'data-state="running"' in html and "bg-emerald-400" in html
    assert "Massive: running · pass 12 · 98/100 tickers · data 15 min delayed" in html
    pass_12 = re.search(r'data-pass="([^"]*)"', html).group(1)
    assert pass_12.startswith("12|")

    _put_status(world, state="idle", symbols_total=None, symbols_done=None,
                cycle_finished=_utcnow() - _dt.timedelta(minutes=2), last_eod_on="2026-10-09")
    html = c.get("/options/status").text
    assert 'data-state="idle"' in html and "Massive: idle · pass 12 done · data 15 min delayed" in html
    assert "2026-10-09" in html and re.search(r'data-pass="([^"]*)"', html).group(1) != pass_12

    _put_status(world, state="history", symbols_total=20, symbols_done=3)
    html = c.get("/options/status").text
    assert 'data-state="history"' in html and "Massive: reading history · 3/20 tickers" in html

    _put_status(world, state="eod", symbols_total=100, symbols_done=40)
    html = c.get("/options/status").text
    assert 'data-state="eod"' in html and "Massive: end-of-day pass · 40/100 tickers" in html

    _put_status(world, state="error", last_error="Massive rejected the API key (HTTP 401)")
    html = c.get("/options/status").text
    assert 'data-state="error"' in html and "bg-rose-400" in html
    assert "error: Massive rejected the API key" in html

    _put_status(world, state="cycle", last_error=None, heartbeat=_utcnow() - _dt.timedelta(minutes=9))
    html = c.get("/options/status").text
    assert 'data-state="stale"' in html and "no heartbeat for 9 min" in html and "bg-amber-400" in html
    assert "<script" not in html


def test_status_strip_never_speaks_of_the_gateway(world):
    """The v4.133 line read 'Hermes: waiting · Gateway off by design'; the Massive
    collector has no Gateway, no blackout and no 'waiting' (an old row's 'waiting' or
    gateway columns read as idle, never as a Gateway state)."""
    c = world["client"]
    for st in ("starting", "cycle", "idle", "history", "eod", "error", "stopped", "waiting"):
        _put_status(world, state=st, gateway="127.0.0.1:4002", gateway_ok=False, mdt="delayed",
                    last_error="Massive rejected the API key" if st == "error" else None,
                    phase_detail="a step" if st != "waiting" else None)
        html = c.get("/options/status").text
        for word in ("Gateway", "gateway", "waiting", "blackout", "4002", "Hermes: "):
            assert word not in html, (st, word)
    _put_status(world, state="waiting")
    assert 'data-state="idle"' in c.get("/options/status").text


def test_status_strip_no_quotes_note_and_key_missing_for_admins(world, monkeypatch):
    c = world["client"]
    note = "prices are estimated from IV (no bid/ask on this plan) - check live in TWS before entering"
    html = c.get("/options/status").text
    assert note in html and "TST_MASSIVE_API_KEY" not in html                # a member never sees the key line
    _as(world, world["aid"])
    try:
        html = c.get("/options/status").text
        assert "TST_MASSIVE_API_KEY is not set on the server" in html and "opt-keymissing" in html
        monkeypatch.setenv("TST_MASSIVE_API_KEY", FAKE_KEY)
        html = c.get("/options/status").text
        assert "TST_MASSIVE_API_KEY is not set" not in html and FAKE_KEY not in html
    finally:
        _as(world, world["uid"])
    monkeypatch.setenv("TST_MASSIVE_QUOTES", "1")                            # a plan with quotes: no note
    assert note not in c.get("/options/status").text


def test_collector_error_text_is_scrubbed_of_the_key(world, monkeypatch):
    monkeypatch.setenv("TST_MASSIVE_API_KEY", FAKE_KEY)
    _put_status(world, state="error", last_error=f"odd failure near {FAKE_KEY} in a message")
    html = world["client"].get("/options/status").text
    assert 'data-state="error"' in html and FAKE_KEY not in html and "***" in html


def test_collector_error_shows_the_pause_reason_not_the_newest_ticker_failure(world):
    """In state error the reported detail is what paused the collector (its scope and next
    try); last_error is overwritten by any one ticker's failure since - the strip and the
    tray must show the same cause."""
    pause = ("your Massive plan does not include stock daily bars (HTTP 403) - end-of-day stock bars "
             "paused, the rest carries on; next try 16:25 ET")
    _put_status(world, state="error", phase_detail=pause,
                last_error="eod CCC: Massive answered HTTP 500 for the options chain snapshot")
    html = world["client"].get("/options/status").text
    assert 'data-state="error"' in html
    assert "Collector error: your Massive plan does not include stock daily bars" in html
    assert "next try 16:25 ET" in html and "eod CCC" not in html and "HTTP 500" not in html
    _put_status(world, phase_detail=None)                     # no detail reported: last_error still shows
    html = world["client"].get("/options/status").text
    assert "Collector error: eod CCC: Massive answered HTTP 500" in html


# ───────────────────────────────── help + the page script + the fragments ─────────────────────────────────

def test_help_explains_the_data(world, monkeypatch):
    def words(r):
        return " ".join(r.text.split())

    r = world["client"].get("/options/help")
    assert r.status_code == 200
    text = words(r)
    for needle in ("Massive", "Options Starter", "15 minutes delayed", "every 15 minutes while the US market is open",
                   "estimated from IV", "model (from IV)", "IBKR TWS", "never places", "Refresh now", "Yahoo",
                   "Stocks Basic", "market time", "greyed out"):
        assert needle in text, needle
    assert "data-close-help" in text and "<script" not in text
    for gone in ("Download the connector", "Local Network Access", "Enable ActiveX", "install_bridge", "Gateway"):
        assert gone not in text, gone
    monkeypatch.setenv("TST_MASSIVE_QUOTES", "1")
    text = words(world["client"].get("/options/help"))
    assert "Prices are the bid/ask mid" in text and "estimated from IV" not in text
    monkeypatch.setenv("TST_OPTIONS_CYCLE_MIN", "10")
    assert "every 10 minutes" in words(world["client"].get("/options/help"))


def test_options_html_has_no_connector_left_and_keeps_the_tws_scanner():
    src = _src("options.html")
    assert "connector" not in src.lower()
    for gone in ("optConnPill", "optConnStart", "optConnUpdate", "optContrib", "/options/data/",
                 "/chain2", "/underlying?symbol=", "/health", "opt_connector_pkg", "Refresh these legs live",
                 "data-refresh-legs", "Download connector", "tradehunter://start-bridge", "local-network-access",
                 "function probe(", "function tick(", "loop.paused", "th.options.histSent"):
        assert gone not in src, gone
    # the optional TWS scanner import still asks the member's own bridge on 127.0.0.1
    for needle in ("'http://127.0.0.1:'", "data-bridge-port", "BRIDGE + '/scan?iv_rank='", "importSyms('scanner'",
                   "'/options/help'", "th.options.strategy", "th.options.basketW", "options:counts"):
        assert needle in src, needle
    for name in FRAGMENTS:
        body = _src(name).lower()
        for gone in ("/options/connector", "/options/data/", "download connector", "refresh these legs live"):
            assert gone not in body, (name, gone)


def test_no_swapped_fragment_contains_a_script():
    for name in FRAGMENTS:
        text = _src(name)
        body = re.sub(r"\{#.*?#\}", "", text, flags=re.S)                   # comments may mention it
        assert "<script" not in body.lower(), name
        assert "onclick=" not in body.lower(), name
