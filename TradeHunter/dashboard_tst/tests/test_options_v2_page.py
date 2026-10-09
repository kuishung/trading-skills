"""The Options v2 page (OPTIONS_V2_DESIGN.md §9, §5.4, §6): ``routes/options_page.py``,
``options.html`` and the ``_opt_*.html`` fragments.

Every test drives the real FastAPI app through ``TestClient`` against a fresh SQLite
file brought to the Alembic head by the real migrations (conftest). ``get_db`` and
``security.current_user`` are overridden so the member and the handler share ONE
session per request. The shared pool is seeded through ``opt_store``'s own writers
(``upsert_quotes`` with ``chain_bs`` rows, ``upsert_daily`` + ``recompute_underlying``,
``set_earnings``, ``set_collector_status``), never a raw row. The screener runs on the
wall clock, so the seeded expiry is the Friday nearest 42 days after today's ET date
(a real listed expiry, never a weekend) and every quote is stamped "now".
"""
from __future__ import annotations

import ast
import datetime as _dt
import io
import json
import re
import zipfile
from pathlib import Path

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from markupsafe import escape
from sqlalchemy.orm import sessionmaker

from app import models
from app.db import get_db
from app.main import app
from app.security import current_user
from app.services import clock, opt_rules, opt_screen, opt_store

from .fixtures.options import chain_bs

APP_DIR = Path(__file__).resolve().parent.parent / "app"
TEMPLATES = APP_DIR / "templates"
FRAGMENTS = ("_opt_basket.html", "_opt_rules.html", "_opt_results.html", "_opt_trade.html",
             "_opt_status.html", "_opt_connector.html")
FORBIDDEN = ("option_engine", "chart_state", "strategy_rules", "strike_picker", "premium_gauge",
             "order_ticket", "telegram_push", "option_exits", "option_nightly", "option_data",
             "option_backfill", "option_vol", "option_words", "option_sizing", "option_prefs")
SPOT = 100.0
DTE = 42


# ───────────────────────────────── the seeded world ─────────────────────────────────

def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


def _friday(dte: int) -> _dt.date:
    """The Friday nearest ``dte`` days after today's ET date (within 3 days): a real
    listed expiry - opt_store refuses a member's row with a weekend expiry, and a
    ``today + 42`` landing on a Saturday made these tests depend on the weekday."""
    d = clock.et_date() + _dt.timedelta(days=dte)
    shift = 4 - d.weekday()
    if shift > 3:
        shift -= 7
    return d + _dt.timedelta(days=shift)


def _chain_rows(*, iv: float = 0.45, spread: float = 0.10, strikes=range(80, 121),
                dte: int = DTE) -> tuple[str, list[dict]]:
    """(expiry, th_ibkr-shaped rows) - a Black-Scholes chain on the Friday nearest
    ``dte`` days out (39-45 days for the default), spot 100, strikes on the $1 grid."""
    today = clock.et_date()
    exp = _friday(dte).isoformat()
    ch = chain_bs(SPOT, iv, [exp], [float(k) for k in strikes], today=today.isoformat(), spread=spread)
    rows = []
    for leg in ch["legs"].values():
        r = {k: leg[k] for k in ("expiry", "right", "strike", "bid", "ask", "mid", "last", "bid_size",
                                 "ask_size", "volume", "iv", "delta", "gamma", "theta", "vega")}
        r["oi"] = leg["open_interest"]
        r["und_price"] = SPOT
        rows.append(r)
    return exp, rows


def _days(n: int, *, weekdays: bool = False) -> list[str]:
    """The last ``n`` calendar days (or weekdays) before today's ET date, oldest first."""
    out, d = [], clock.et_date() - _dt.timedelta(days=1)
    while len(out) < n:
        if not weekdays or d.weekday() < 5:
            out.append(d.isoformat())
        d -= _dt.timedelta(days=1)
    return out[::-1]


def _history(*, falling: bool = False, weekdays: bool = False) -> tuple[list[dict], list[dict]]:
    """300 daily bars (close 100, high 101, low 99 -> ATR 2.0) and a year of IV30 points
    ending high in its range (rank ~74) or, with ``falling``, at its low (rank 0).
    ``weekdays``: trading-day dates only (what a member's connector sends - IBKR has
    no weekend bars, and opt_store.validate_history refuses them)."""
    days = _days(300, weekdays=weekdays)
    bars = [{"on": d, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1_000_000}
            for d in days]
    n = 260
    if falling:
        ivs = [{"on": days[-n + i], "iv": 40.0 - 20.0 * i / (n - 1)} for i in range(n)]
    else:
        ivs = [{"on": days[-n + i], "iv": 20.0 + 20.0 * i / (n - 2)} for i in range(n - 1)]
        ivs.append({"on": days[-1], "iv": 35.0})
    return bars, ivs


def _seed(db, sym: str, *, falling_iv: bool = False, as_of=None) -> None:
    _, rows = _chain_rows()
    opt_store.upsert_quotes(db, sym, rows, source="hermes", mdt="live", kind="cycle", spot=SPOT, as_of=as_of)
    bars, ivs = _history(falling=falling_iv)
    opt_store.upsert_daily(db, sym, bars, ivs, source="hermes")
    opt_store.recompute_underlying(db, sym)
    opt_store.set_earnings(db, sym, (clock.et_date() + _dt.timedelta(days=120)).isoformat())
    opt_store.mark_history_done(db, sym)


@pytest.fixture
def world(engine, db, user):
    """The member (basket LRCX, MSFT, KO, NVDA), a second member 'Kui' with an empty
    basket, IBKR data for LRCX / MSFT (bull puts pass) and KO (IV rank 0 - no bull put
    passes), none for NVDA (waiting for its first read)."""
    from app.routes import options_page as op

    opt_store.reset_state()
    other = models.User(email="kui@local.test", display_name="Kui", role=models.ROLE_MEMBER,
                        status=models.APPROVED, created_at=_dt.datetime(2026, 1, 6, tzinfo=_dt.timezone.utc))
    db.add(other)
    db.commit()
    res = op._add_symbols(db, user, ["LRCX", "MSFT", "KO", "NVDA"], "paste")
    assert res["added"] == 4 and res["new"] == ["LRCX", "MSFT", "KO", "NVDA"]
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
               "Session": Session, "state": state, "op": op}
    finally:
        app.dependency_overrides.clear()
        opt_store.reset_state()


def _as(world, uid):
    world["state"]["uid"] = uid


def _ids(html: str) -> list[str]:
    return re.findall(r'data-cand-id="([^"]+)"', html)


def _trigger(resp) -> dict:
    return json.loads(resp.headers.get("HX-Trigger") or "{}")


def _th_ibkr():
    """bridge/th_ibkr.py loaded by path (it ships with the connector, outside the app
    package); importable without ib_insync."""
    import importlib.util

    path = APP_DIR.parent / "bridge" / "th_ibkr.py"
    spec = importlib.util.spec_from_file_location("th_ibkr_for_page_tests", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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
    assert 'href="/options/connector/download"' in html and 'id="optHelpBtn"' in html
    assert 'id="optConnPill"' in html and 'id="optRulesHost"' in html and 'id="optResultsHost"' in html
    assert 'id="optConnUpdate"' in html                                      # the out-of-date connector's link
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
        for path in ("/options", "/options/basket", "/options/results", "/options/data/next"):
            assert c.get(path).status_code == 303, path
        for path in ("/options/data/contribute", "/options/data/contribute_history", "/options/data/failed"):
            assert c.post(path, json={}).status_code == 303, path
        u.menu_access = None
        s.commit()
    finally:
        s.close()
    assert c.get("/options/basket").status_code == 200


def test_router_order_and_no_v1_engine_imports():
    paths = [getattr(r, "path", None) for r in app.routes]
    catch_all = paths.index("/options/{symbol}")
    for p in ("/options/basket", "/options/rules", "/options/results", "/options/trade", "/options/payoff",
              "/options/status", "/options/connector"):
        assert paths.index(p) < catch_all, p
    # The v1 nav badge (positions at an exit line) went with the Positions tab: no route, no poller.
    assert "/options/badge" not in paths
    assert "/options/badge" not in (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert "/options/data/next" in paths and "/options/connector/download" in paths
    assert "/options/data/failed" in paths
    src = (APP_DIR / "routes" / "options_page.py").read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            imported.update(a.name.rsplit(".", 1)[-1] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.update(a.name for a in node.names)
            imported.add((node.module or "").rsplit(".", 1)[-1])
    assert not (imported & set(FORBIDDEN)), imported & set(FORBIDDEN)
    assert "threading" not in imported                                       # no v4.131 first-read thread


def test_every_route_answers(world):
    c = world["client"]
    for path in ["/options/basket", "/options/basket?sort=fresh", "/options/basket?sort=iv",
                 "/options/basket?sort=symbol", "/options/basket?part=rows&sort=iv", "/options/results",
                 "/options/results?strategy=nope",
                 "/options/results?strategy=iron_condor&sort=pop&dir=asc", "/options/status",
                 "/options/connector", "/options/rules", "/options/trade?strategy=bull_put&id=junk",
                 "/options/payoff?strategy=bull_put&id=junk", "/options/data/next"]:
        r = c.get(path)
        assert r.status_code == 200, (path, r.status_code, r.text[:300])


# ───────────────────────────────── the basket ─────────────────────────────────

def test_basket_rows_freshness_and_waiting_for_first_read(world):
    html = world["client"].get("/options/basket").text
    assert html.count('class="opt-brow') == 4
    for sym in ("LRCX", "MSFT", "KO", "NVDA"):
        assert f'data-sym="{sym}"' in html
    nvda = html.split('data-sym="NVDA"', 1)[1].split('class="opt-brow', 1)[0]
    assert "waiting for first read" in nvda
    lrcx = html.split('data-sym="LRCX"', 1)[1].split('class="opt-brow', 1)[0]
    assert "Hermes live" in lrcx and "bg-emerald-400" in lrcx and "waiting for first read" not in lrcx
    assert 'class="opt-count' in html                                        # the cell the counts are painted into
    assert "onclick" not in html and "<script" not in html
    # no promise of a Hermes read during the US session (its gateway is off then, §4.0)
    assert "within a few minutes" not in html and "after 20:10 ET" in html


def test_basket_add_remove_import_cap_and_duplicates(world):
    c = world["client"]
    r = c.post("/options/basket/add", data={"symbol": "amd"})
    assert r.status_code == 200 and 'data-sym="AMD"' in r.text
    ev = _trigger(r)
    assert "options:universe-changed" in ev and "added" in ev["options:toast"]["msg"]
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
        assert r.json() == {"added": 60, "skipped": 0, "over_cap": 10, "total": 60}
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
    """Review #12: a refresh (after a contribution, a sort click, add / remove) answers
    the rows container only, so the Add / Import footer - the typed ticker, the pasted
    list, an open panel, the scan criteria - survives, and the chosen sort rides along."""
    c = world["client"]
    full = c.get("/options/basket?sort=iv").text
    assert 'id="optBasketRows"' in full and 'id="optAddForm"' in full and 'id="optPasteBox"' in full
    assert 'data-sort="iv"' in full
    rows = c.get("/options/basket?part=rows&sort=fresh").text
    assert 'class="opt-basket-rows-inner" data-sort="fresh"' in rows and rows.count('class="opt-brow') == 4
    for footer in ('id="optAddForm"', 'id="optPasteBox"', 'id="optScanRank"', "+ Add ticker", "Import"):
        assert footer not in rows, footer
    # every refresh target is the rows container, and asks for the rows only
    assert 'hx-target="#optBasketRows"' in rows and "part=rows" in rows
    assert 'hx-target="closest .opt-basket"' not in full
    # add / remove with part=rows answer the rows only, in the sort they were given
    r = c.post("/options/basket/add", data={"symbol": "AMD", "sort": "symbol", "part": "rows"})
    assert 'data-sym="AMD"' in r.text and 'data-sort="symbol"' in r.text and 'id="optAddForm"' not in r.text
    order = re.findall(r'class="opt-brow[^"]*"[^>]*data-sym="([A-Z]+)"', r.text)
    assert order == sorted(order)
    r = c.post("/options/basket/remove", data={"symbol": "AMD", "sort": "iv", "part": "rows"})
    assert 'data-sym="AMD"' not in r.text and 'data-sort="iv"' in r.text and 'id="optPasteBox"' not in r.text
    # the page keeps the sort and passes it on every refresh; basket edits carry it too
    src = (TEMPLATES / "options.html").read_text(encoding="utf-8")
    for needle in ("'/options/basket?part=rows&sort='", "th.options.basketSort", "htmx:configRequest",
                   "d.parameters.sort = state.bsort", "'#optBasketRows'"):
        assert needle in src, needle
    assert "options:basket-refresh from:body" not in src                      # no whole-basket reload any more


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
        # the blur after a typed (and already saved) value does not post it again
        assert html.count('hx-trigger="change changed, keyup changed delay:600ms"') >= len(
            [1 for _, _, f in opt_rules.fields(strategy) if f.kind in ("int", "num", "choice")])
        assert f'hx-post="/options/rules?strategy={strategy}"' in html
        assert "<script" not in html


def test_number_steps_line_up_with_min_and_default(world):
    """Review (low): a browser counts steps from ``min``; with min 0.01 / step 0.05 the
    arrows turned the house 0.50 into 0.51. Every number field's rendered step now puts
    both its min and its default on the grid."""
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
    # the case the review measured: the $ band (min 0) and the theta / debit caps
    f = opt_rules.SCHEMA["bull_put"]["max_leg_spread"]
    assert op._input_step("max_leg_spread", f) in (0.05, 0.01)
    th = opt_rules.SCHEMA["buy_call"]["theta_pct_max"]
    n = (th.default - th.lo) / op._input_step("theta_pct_max", th)
    assert abs(n - round(n)) < 1e-6


def test_rule_change_saves_and_changes_the_results(world):
    c = world["client"]
    before = _ids(c.get("/options/results?strategy=bull_put").text)
    assert len(before) == 6                                                  # 3 per ticker x LRCX, MSFT

    r = c.post("/options/rules?strategy=bull_put", data={"shared.per_ticker": "1"})
    assert r.status_code == 200
    assert _trigger(r)["options:rules-changed"]["strategy"] == "bull_put"
    assert "Saved" in r.text and 'hx-swap-oob="true"' in r.text
    assert 'id="optRules"' not in r.text                                     # the inputs are never re-rendered
    s = world["Session"]()
    try:
        assert opt_rules.read(s, s.get(models.User, world["uid"]))["shared"]["per_ticker"] == 1
    finally:
        s.close()
    after = _ids(c.get("/options/results?strategy=bull_put").text)
    assert len(after) == 2 and set(after) <= set(before)

    # an out-of-bounds value is clamped, saved and explained in the field's own slot ...
    r = c.post("/options/rules?strategy=bull_put", data={"bull_put.short_delta_hi": "5"})
    slot = r.text.split('id="rerr-bull_put-short_delta_hi"', 1)[1].split("</span>", 1)[0]
    assert "over the highest allowed" in slot
    # ... and the value in force goes back into its box (review: the box kept "5")
    hi = opt_rules.SCHEMA["bull_put"]["short_delta_hi"].hi
    assert _trigger(r)["options:rules-clamped"] == {"rf-bull_put-short_delta_hi": {"sent": "5", "value": f"{hi:g}"}}
    # the next save of ANOTHER field does not wipe that warning: only posted fields' slots are re-sent
    r = c.post("/options/rules?strategy=bull_put", data={"shared.per_ticker": "2"})
    assert 'id="rerr-shared-per_ticker"' in r.text and 'id="rdot-shared-per_ticker"' in r.text
    assert 'id="rerr-bull_put-short_delta_hi"' not in r.text and 'id="rdot-bull_put-short_delta_hi"' not in r.text
    assert "options:rules-clamped" not in _trigger(r)
    # a save of that field with a good value clears it
    r = c.post("/options/rules?strategy=bull_put", data={"bull_put.short_delta_hi": "0.3"})
    slot = r.text.split('id="rerr-bull_put-short_delta_hi"', 1)[1].split("</span>", 1)[0]
    assert slot.rstrip().endswith('role="status">')
    src = (TEMPLATES / "options.html").read_text(encoding="utf-8")
    assert "options:rules-clamped" in src and "el.value = v.value" in src

    # a checkbox posts with its hidden 'off' twin: on wins when ticked, off alone un-ticks
    c.post("/options/rules?strategy=bull_put", data={"shared.monthly_only": ["off", "on"]})
    s = world["Session"]()
    try:
        assert opt_rules.read(s, s.get(models.User, world["uid"]))["shared"]["monthly_only"] is True
    finally:
        s.close()
    c.post("/options/rules?strategy=bull_put", data={"shared.monthly_only": "off"})
    s = world["Session"]()
    try:
        assert opt_rules.read(s, s.get(models.User, world["uid"]))["shared"]["monthly_only"] is False
    finally:
        s.close()

    r = c.post("/options/rules/reset?strategy=bull_put")
    assert r.status_code == 200 and 'id="optRules"' in r.text and "options:rules-changed" in _trigger(r)
    assert len(_ids(c.get("/options/results?strategy=bull_put").text)) == 6


def test_failed_requests_are_never_silent():
    """Review #14: a failed autosave / reset / basket edit, or a request that never
    reached the server, says so - rose 'Not saved (HTTP n)' in the rules' message line,
    an error toast for the basket."""
    src = (TEMPLATES / "options.html").read_text(encoding="utf-8")
    for needle in ("htmx:responseError", "htmx:sendError", "optRulesMsg", "'Not reset'", "'Not saved'",
                   "'HTTP ' + (xhr.status || '?')", "the server could not be reached",
                   "Your basket was not updated", "elt.closest('#optRules')", "elt.closest('#optBasket')"):
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
    assert re.search(r"(now|\d+ min) · Hermes live", html)                  # the Data column
    assert "Why so few?" in html and "IV rank outside 30-100" in html
    ev = _trigger(r)["options:counts"]
    assert ev["strategy"] == "bull_put"
    assert ev["counts"] == {"LRCX": 3, "MSFT": 3, "KO": 0, "NVDA": 0}
    assert "IV rank" in ev["reasons"]["KO"] and ev["summary"].startswith("6 trades pass")
    watch = json.loads(re.search(r"data-syms='([^']*)'", html).group(1))
    assert set(watch) == {"LRCX", "MSFT", "KO", "NVDA"}                        # review #11: the whole basket
    # server-side sort: max loss ascending
    r = c.get("/options/results?strategy=bull_put&sort=max_loss&dir=asc")
    by_id = {row["id"]: row for row in expected["rows"]}
    losses = [by_id[i]["max_loss"] for i in _ids(r.text)]
    assert losses == sorted(losses) and 'data-sort="max_loss"' in r.text
    # a strategy nothing passes says so, with the funnel (no 9-18 month expiry is seeded)
    r = c.get("/options/results?strategy=leaps_call")
    assert _ids(r.text) == [] and "No trade passes" in r.text and "Why so few?" in r.text
    assert "<script" not in r.text


def test_results_load_only_what_can_pass(world, monkeypatch):
    """Review #15: the list read every stored contract of every basket ticker. Now a
    ticker that fails the stock filters on its stored facts is never loaded, and the
    others load only the strategy's expiry window and quotes younger than max_age_h + 96 h."""
    c = world["client"]
    calls: list[tuple[str, dict]] = []
    real = opt_store.chain_view

    def spy(db, symbol, **kw):
        calls.append((symbol, kw))
        return real(db, symbol, **kw)

    monkeypatch.setattr(opt_store, "chain_view", spy)
    assert len(_ids(c.get("/options/results?strategy=bull_put").text)) == 6
    # KO fails the IV-rank range on its stored IV rank; NVDA has nothing stored
    assert sorted(s for s, _ in calls) == ["LRCX", "MSFT"]
    for _, kw in calls:
        assert (kw["dte_min"], kw["dte_max"], kw["max_age_h"]) == (30, 60, 24 + 96)
    # LEAPS: months x 30.44 (9 -> 273.96, 18 -> 547.92); LRCX / MSFT (IV rank ~74) fail its 0-50 range
    calls.clear()
    c.get("/options/results?strategy=leaps_call")
    assert [s for s, _ in calls] == ["KO"] and (calls[0][1]["dte_min"], calls[0][1]["dte_max"]) == (273, 548)
    # a diagonal loads the union of its two windows
    calls.clear()
    c.post("/options/rules?strategy=diagonal_call", data={"diagonal_call.iv_rank_max": "100"})
    c.get("/options/results?strategy=diagonal_call")
    assert {(kw["dte_min"], kw["dte_max"]) for _, kw in calls} == {(30, 365)}
    # a price floor above every stock: nothing is loaded at all, and the reason is the price
    calls.clear()
    c.post("/options/rules?strategy=bull_put", data={"shared.price_min": "500"})
    r = c.get("/options/results?strategy=bull_put")
    assert calls == [] and _ids(r.text) == []
    reasons = _trigger(r)["options:counts"]["reasons"]
    assert "stock price" in reasons["LRCX"] and "stock price" in reasons["MSFT"]
    assert "no option data" in reasons["NVDA"]                                  # nothing stored is still "no data"


def test_results_match_a_full_load_for_every_strategy(world):
    """The stubs keep the screen exact: for every strategy, the trades, the funnel (every
    rule's count), each ticker's verdict and n_considered equal a screen over the FULL
    chains - with an extra MSFT expiry outside most windows (120 days) in the pool."""
    s = world["Session"]()
    try:
        _, rows = _chain_rows(dte=120)
        opt_store.upsert_quotes(s, "MSFT", rows, source="hermes", mdt="live", kind="cycle", spot=SPOT)
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
    """A ticker with expiries in the window but every quote older than the age
    pre-filter is not 'no option data yet' - it says its quotes are too old."""
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
    # a stock filter the ticker fails stays its reason: fresh quotes would not fix it
    s = world["Session"]()
    try:
        opt_store.set_earnings(s, "AMD", None)
    finally:
        s.close()
    reasons = _trigger(world["client"].get("/options/results?strategy=bull_put"))["options:counts"]["reasons"]
    assert reasons["AMD"] == "earnings date unknown"


def test_data_column_tooltip_shows_the_wall_clock_age_next_to_the_market_age(world):
    """Shared decision: data.age_min is the MARKET-time age (the column); the tooltip
    adds data.wall_age_min, the plain clock age (a Friday-close quote read on Sunday)."""
    op = world["op"]
    oldest = _dt.datetime(2026, 10, 9, 20, 15)
    c = {"id": "X", "symbol": "LRCX", "legs": [], "dte": 40,
         "data": {"as_of_oldest": oldest, "age_min": 0, "wall_age_min": 2 * 1440 + 30,
                  "sources": ["hermes·live", "member:Kui·delayed"], "mixed": True}}
    v = op._row_view("bull_put", c, rth=False)
    assert v["data_text"].startswith("now · Hermes live + Kui (delayed)")
    tip = v["data_tip"]
    assert "Market-time age under a minute" in tip and "market is closed" in tip
    assert "read 2 d ago (2026-10-09 20:15 UTC)" in tip and "more than one source" in tip
    html = world["client"].get("/options/results?strategy=bull_put").text
    assert "Market-time age" in html and "on the clock, read" in html


def test_results_page_script_debounces_and_never_closes_an_open_trade():
    """Review #11 / #12 / #15, page side: one results request in flight (a newer trigger
    queues one follow-up), a contribution re-screens after 5 s, never while a trade is
    open (it runs when the trade closes), and an open 'Why so few?' stays open."""
    src = (TEMPLATES / "options.html").read_text(encoding="utf-8")
    for needle in ("RESCREEN_MS = 5000", "function requestResults(bg)", "res.queued = true",
                   "res.afterClose = true", "function closedByMember()", "e.detail.shouldSwap = false",
                   "state.funnelOpen", "function scheduleRescreen()"):
        assert needle in src, needle
    # the old per-symbol watch list (only listed + no-data tickers) is gone
    assert "watch.indexOf(sym)" not in src


# ───────────────────────────────── trade detail ─────────────────────────────────

def test_trade_detail_payoff_legs_sources_and_refresh_window(world):
    c = world["client"]
    cid = _ids(c.get("/options/results?strategy=bull_put").text)[0]
    sym = cid.split("|")[0]
    r = c.get("/options/trade", params={"strategy": "bull_put", "id": cid})
    assert r.status_code == 200
    html = r.text
    assert 'class="po-pane' in html                                          # the payoff chart (existing partial)
    assert "Breakeven" in html and "Max profit" in html and "Max loss" in html and "POP" in html
    assert html.count(">Hermes<") == 2 and "Refresh these legs live" in html
    spec = json.loads(re.search(r'data-spec="([^"]*)"', html).group(1).replace("&#34;", '"').replace("&quot;", '"'))
    legs = cid.split("|")[2:]
    assert spec["expiries"] == sorted({legs[0], legs[3]}) and spec["symbol"] == sym
    assert 4 <= spec["min_side"] <= spec["max_side"] == 40 and 0 < spec["sigma_k"] <= 10
    # the connector's own planner, over a strike list TWICE as dense as the stored one,
    # puts every leg of the trade inside the narrow window
    th = _th_ibkr()
    window = th.plan_spec({"expiries": [legs[0]], "strikes": [50 + 0.5 * i for i in range(201)]}, spec,
                          today=clock.et_date())
    assert [w["expiry"] for w in window] == [legs[0]]
    for strike in (float(legs[2]), float(legs[5])):
        assert strike in window[0]["strikes"], (strike, window[0]["strikes"])
    assert len(window[0]["strikes"]) <= 2 * 40
    assert "/options/payoff?strategy=bull_put&amp;id=" in html                 # the $ | R toggle's own url
    r = c.get("/options/payoff", params={"strategy": "bull_put", "id": cid, "units": "R"})
    assert r.status_code == 200 and 'class="po-pane' in r.text

    # Kui's connector re-reads the chain: the legs now show Kui as their source
    _as(world, world["oid"])
    try:
        _, rows = _chain_rows()
        r = c.post("/options/data/contribute", json={"symbol": sym, "spot": SPOT, "mdt": "live", "rows": rows,
                                                     "connector_version": "2.0", "client_as_of": "2020-01-01T00:00:00Z"})
        assert r.status_code == 200 and r.json()["ok"] is True
    finally:
        _as(world, world["uid"])
    html = c.get("/options/trade", params={"strategy": "bull_put", "id": cid}).text
    assert html.count(">Kui<") == 2 and ">Hermes<" not in html
    assert "Kui (live)" in c.get("/options/results?strategy=bull_put").text
    # an id that no longer exists
    gone = c.get("/options/trade", params={"strategy": "bull_put", "id": f"{sym}|bull_put|2020-01-17|P|90|2020-01-17|P|85"})
    assert gone.status_code == 200 and "no longer in the data" in gone.text


def test_trade_detail_fits_a_phone_and_pauses_the_loop():
    """Review (low): the opened trade sticks to the left edge of the sideways-scrolling
    table at the visible width; "Refresh these legs live" pauses the background loop."""
    src = (TEMPLATES / "options.html").read_text(encoding="utf-8")
    for needle in ("function fitTradeBox()", "box.style.position = 'sticky'", "sc.clientWidth + 'px'",
                   "window.addEventListener('resize', fitTradeBox)", "loop.paused++", "loop.paused > 0",
                   "loop.paused = Math.max(0, loop.paused - 1)"):
        assert needle in src, needle


# ───────────────────────────────── contributions (§6) ─────────────────────────────────

def test_data_next_leases_the_stalest_symbol(world):
    c = world["client"]
    r = c.get("/options/data/next")
    d = r.json()
    assert d["symbol"] == "NVDA" and d["history_done"] is False                # never read: first
    assert d["spec"]["symbol"] == "NVDA" and d["spec"]["sigma_k"] == 2.5
    # a CHUNK: no quotes stored yet -> the nearest 6 expiries, 25 strikes a side at most
    assert d["spec"]["expiries"] is None and d["spec"]["max_expiries"] == 6 and d["spec"]["max_side"] == 25
    # NVDA is leased; the others were read seconds ago -> nothing to do now
    assert c.get("/options/data/next").json() == {"wait": 30}
    opt_store.release_lease("NVDA")
    assert c.get("/options/data/next").json()["symbol"] == "NVDA"


def test_data_failed_backs_the_ticker_off_and_releases_the_lease(world):
    """Review #10: a read that fails (slowly or at once) is reported; the server releases
    the lease and backs the ticker off, so the loop moves on instead of re-reading it."""
    c = world["client"]
    assert c.get("/options/data/next").json()["symbol"] == "NVDA"
    assert opt_store.is_leased("NVDA")
    r = c.post("/options/data/failed", json={"symbol": "nvda", "error": "TWS did not finish within 150 s"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] is True and out["symbol"] == "NVDA" and out["backoff_s"] == 600
    assert not opt_store.is_leased("NVDA")
    assert c.get("/options/data/next").json() == {"wait": 30}                # backed off; the rest are fresh
    s = world["Session"]()
    try:
        row = (s.query(models.OptRefreshLog).filter(models.OptRefreshLog.symbol == "NVDA")
                 .order_by(models.OptRefreshLog.id.desc()).first())
        assert row.kind == "member" and row.n_contracts == 0 and "150 s" in row.error
        assert row.source_user_id == world["uid"]
        assert "NVDA" not in opt_store.freshness(s, ["NVDA"])               # a failure never looks fresh
    finally:
        s.close()
    assert c.post("/options/data/failed", json={"symbol": "NVDA", "error": "again"}).status_code == 429
    assert c.post("/options/data/failed", json={"symbol": "ZZZZ", "error": "x"}).status_code == 400
    assert c.post("/options/data/failed", json={"error": "x"}).status_code == 400
    assert c.post("/options/data/failed", json=[1]).status_code == 400
    _as(world, world["oid"])                                                 # NVDA is not in Kui's basket
    try:
        r = c.post("/options/data/failed", json={"symbol": "LRCX", "error": "x"})
        assert r.status_code == 400 and "your basket" in r.json()["error"]
    finally:
        _as(world, world["uid"])
    src = (TEMPLATES / "options.html").read_text(encoding="utf-8")
    for needle in ("'/options/data/failed'", "CHAIN_TIMEOUT_MS = 160000", "function shouldReport(err)",
                   "JSON.stringify(n.spec || {})"):
        assert needle in src, needle


def test_a_refused_contribution_is_a_failed_read(world):
    """Shared decision: the loop's contribution refused by the server (400) releases the
    member's lease and backs the ticker off (report_failure), answering backoff_s so the
    page does not report it twice; a 429 frees the lease too. A trade refresh, or a
    ticker outside the member's basket, backs nothing off."""
    c = world["client"]
    _, rows = _chain_rows()
    body = {"symbol": "NVDA", "spot": None, "mdt": "live", "rows": rows, "connector_version": "2.0"}
    assert c.get("/options/data/next").json()["symbol"] == "NVDA"
    assert opt_store.is_leased("NVDA")
    r = c.post("/options/data/contribute", json=dict(body, kind="trade"))     # a trade refresh: nothing backs off
    assert r.status_code == 400 and "backoff_s" not in r.json() and opt_store.is_leased("NVDA")
    r = c.post("/options/data/contribute", json=body)
    assert r.status_code == 400 and "spot" in r.json()["error"] and r.json()["backoff_s"] == 600
    assert not opt_store.is_leased("NVDA") and opt_store.backoff_until("NVDA") is not None
    assert c.get("/options/data/next").json() == {"wait": 30}
    s = world["Session"]()
    try:
        row = (s.query(models.OptRefreshLog).filter(models.OptRefreshLog.symbol == "NVDA")
                 .order_by(models.OptRefreshLog.id.desc()).first())
        assert row.n_contracts == 0 and row.error.startswith("refused:") and row.source_user_id == world["uid"]
    finally:
        s.close()
    _as(world, world["oid"])                                                 # LRCX is not in Kui's basket
    try:
        r = c.post("/options/data/contribute", json=dict(body, symbol="LRCX"))
        assert r.status_code == 400 and "backoff_s" not in r.json()
        assert opt_store.backoff_until("LRCX") is None
    finally:
        _as(world, world["uid"])
    # a 429 frees the member's own lease (nothing was stored)
    opt_store.reset_state()
    assert opt_store.check_rate(world["uid"], "NVDA", bucket="chain") is None    # a post seconds ago
    assert c.get("/options/data/next").json()["symbol"] == "NVDA" and opt_store.is_leased("NVDA")
    r = c.post("/options/data/contribute", json=dict(body, spot=SPOT))
    assert r.status_code == 429 and not opt_store.is_leased("NVDA")
    src = (TEMPLATES / "options.html").read_text(encoding="utf-8")
    for needle in ("r.backoff_s", "e.backoff = r.backoff_s", "err.backoff", "r.status === 400"):
        assert needle in src, needle


def test_contribute_validates_stores_as_member_and_rate_limits(world):
    c = world["client"]
    _, rows = _chain_rows()
    body = {"symbol": "LRCX", "spot": SPOT + 0.2, "mdt": "live", "rows": rows[:30] + [{"right": "X"}],
            "connector_version": "2.0", "client_as_of": _utcnow().isoformat() + "Z"}
    r = c.post("/options/data/contribute", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] is True and out["stored"] == 30 and out["dropped"] == 1 and out["as_of"].endswith("Z")
    s = world["Session"]()
    try:
        q = s.query(models.OptQuote).filter(models.OptQuote.symbol == "LRCX", models.OptQuote.source == "member")
        assert q.count() == 30 and {x.source_user_id for x in q} == {world["uid"]}
        log = (s.query(models.OptRefreshLog).filter(models.OptRefreshLog.symbol == "LRCX")
                 .order_by(models.OptRefreshLog.id.desc()).first())
        assert log.source == "member" and log.kind == "member" and log.source_user_id == world["uid"]
    finally:
        s.close()
    r = c.post("/options/data/contribute", json=body)                         # again within 20 s
    assert r.status_code == 429 and r.json()["ok"] is False
    r = c.post("/options/data/contribute", json=dict(body, symbol="ZZZZ"))     # in nobody's basket
    assert r.status_code == 400 and "basket" in r.json()["error"]
    r = c.post("/options/data/contribute", json=dict(body, symbol="MSFT", spot=None))
    assert r.status_code == 400 and "spot" in r.json()["error"]
    r = c.post("/options/data/contribute", json=dict(body, symbol="MSFT", spot=SPOT * 1.5))
    assert r.status_code == 400 and "%" in r.json()["error"]                  # 50% from the stored spot
    assert c.post("/options/data/contribute", json=[1, 2]).status_code == 400
    # the trade-detail refresh has its own bucket, so it is not blocked by the loop's read
    r = c.post("/options/data/contribute", json=dict(body, kind="trade"))
    assert r.status_code == 200 and r.json()["ok"] is True


def test_a_contribution_releases_only_its_own_members_lease(world):
    c = world["client"]
    _, rows = _chain_rows()
    body = {"symbol": "NVDA", "spot": SPOT, "mdt": "live", "rows": rows, "connector_version": "2.0"}
    assert c.get("/options/data/next").json()["symbol"] == "NVDA"             # the member's loop holds NVDA
    assert c.post("/options/data/contribute", json=dict(body, kind="trade")).status_code == 200
    assert opt_store.is_leased("NVDA")                                       # a trade refresh is not the loop's read
    _as(world, world["oid"])
    try:
        assert c.post("/options/data/contribute", json=body).status_code == 200
        assert opt_store.is_leased("NVDA")                                   # Kui's post never frees the member's read
    finally:
        _as(world, world["uid"])
    assert c.post("/options/data/contribute", json=body).status_code == 200
    assert not opt_store.is_leased("NVDA")


def test_contribution_bodies_over_5_mb_are_refused_unread(world):
    """Review (low): the 4000 / 800 caps applied only after the whole body was read and
    decoded. A body over 5 MB is refused by its Content-Length (or while streaming),
    after the sign-in check and before any JSON parsing."""
    c = world["client"]
    op = world["op"]
    assert op.MAX_BODY_BYTES == 5 * 1024 * 1024
    junk = b"{" * (op.MAX_BODY_BYTES + 10)                                   # not JSON: a 400 would mean it was parsed
    for path in ("/options/data/contribute", "/options/data/contribute_history", "/options/data/failed"):
        r = c.post(path, content=junk, headers={"Content-Type": "application/json"})
        assert r.status_code == 413, (path, r.status_code)
        assert r.json()["ok"] is False and "5 MB" in r.json()["error"]
    big = {"symbol": "LRCX", "spot": SPOT, "mdt": "live", "rows": [], "pad": "x" * op.MAX_BODY_BYTES}
    assert c.post("/options/data/contribute", json=big).status_code == 413
    r = c.post("/options/data/contribute", content=b"{nope", headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and "not JSON" in r.json()["error"]


def test_contribute_history_stores_daily_rows_and_recomputes(world):
    c = world["client"]
    bars, ivs = _history(weekdays=True)
    r = c.post("/options/data/contribute_history", json={"symbol": "NVDA", "bars": bars[-260:], "iv_series": ivs})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] is True and out["stored"] == 260 and out["iv_rank"] is not None
    assert abs(out["atr14"] - 2.0) < 1e-9
    s = world["Session"]()
    try:
        rows = s.query(models.OptUnderlyingDaily).filter(models.OptUnderlyingDaily.symbol == "NVDA").all()
        assert len(rows) == 260 and {x.source for x in rows} == {"member"}
        u = opt_store.underlying(s, "NVDA")
        assert u["iv_rank"] == out["iv_rank"] and u["history_done"] is False   # Hermes still owes the 2-year pull
    finally:
        s.close()
    # review #5: another member cannot re-file (overwrite) a member's history that is on file
    _add(world, world["oid"], ["NVDA"])
    _as(world, world["oid"])
    try:
        flat = [dict(b, close=100.0, high=100.0, low=100.0) for b in bars[-260:]]
        r = c.post("/options/data/contribute_history", json={"symbol": "NVDA", "bars": flat, "iv_series": []})
        assert r.status_code == 200 and r.json()["stored"] == 0 and "already on file" in r.json()["note"]
    finally:
        _as(world, world["uid"])
    s = world["Session"]()
    try:
        assert abs(opt_store.underlying(s, "NVDA")["atr14"] - 2.0) < 1e-9       # untouched
    finally:
        s.close()
    r = c.post("/options/data/contribute_history", json={"symbol": "LRCX", "bars": bars, "iv_series": ivs})
    assert r.status_code == 200 and r.json()["stored"] == 0                    # Hermes already has it
    r = c.post("/options/data/contribute_history", json={"symbol": "ZZZZ", "bars": bars, "iv_series": ivs})
    assert r.status_code == 400 and "basket" in r.json()["error"]
    r = c.post("/options/data/contribute_history", json={"symbol": "KO", "bars": bars + bars + bars, "iv_series": []})
    assert r.status_code == 400                                                # over 800 points

    # review #5: every post goes through opt_store.validate_history - bars required,
    # weekday dates only, every close inside the spot band
    _add(world, world["uid"], ["AMD"])
    s = world["Session"]()
    try:
        opt_store.set_spot(s, "AMD", SPOT, source="hermes", mdt="live")
    finally:
        s.close()
    _, iv_only = _history(weekdays=True)
    r = c.post("/options/data/contribute_history", json={"symbol": "AMD", "bars": [], "iv_series": iv_only})
    assert r.status_code == 400 and "bars" in r.json()["error"]               # IV alone can no longer move the IV rank
    weekend = [b for b in _history()[0] if _dt.date.fromisoformat(b["on"]).weekday() >= 5][-5:]
    r = c.post("/options/data/contribute_history", json={"symbol": "AMD", "bars": weekend, "iv_series": []})
    assert r.status_code == 400 and "weekend" in r.json()["error"]
    wild = [dict(b) for b in bars[-60:]]
    wild[10] = dict(wild[10], close=1000.0, high=1000.0)
    r = c.post("/options/data/contribute_history", json={"symbol": "AMD", "bars": wild, "iv_series": []})
    assert r.status_code == 400
    s = world["Session"]()
    try:
        assert s.query(models.OptUnderlyingDaily).filter(models.OptUnderlyingDaily.symbol == "AMD").count() == 0
    finally:
        s.close()
    r = c.post("/options/data/contribute_history", json={"symbol": "AMD", "bars": bars[-60:], "iv_series": []})
    assert r.status_code == 200 and r.json()["stored"] == 60
    r = c.post("/options/data/contribute_history", json={"symbol": "AMD", "bars": bars[-5:], "iv_series": []})
    assert r.status_code == 200 and r.json()["stored"] == 0                    # now on file


# ───────────────────────────────── strip + connector ─────────────────────────────────

def test_status_strip_renders_each_collector_state(world):
    c = world["client"]
    html = c.get("/options/status").text
    assert 'data-state="none"' in html and "no heartbeat yet" in html

    def put(**fields):
        s = world["Session"]()
        try:
            opt_store.set_collector_status(s, **fields)
        finally:
            s.close()

    put(state="cycle", gateway="127.0.0.1:4002", gateway_ok=True, mdt="live", cycle_n=12,
        symbols_total=30, symbols_done=18, last_error=None)
    html = c.get("/options/status").text
    assert 'data-state="running"' in html
    assert "Hermes: running" in html and "cycle 12" in html and "18/30 tickers this pass" in html and "live data" in html
    put(state="error", gateway_ok=False, last_error="connect refused on 4002")
    html = c.get("/options/status").text
    assert 'data-state="error"' in html and "gateway down" in html and "connect refused on 4002" in html
    # The collector's "waiting" (Gateway off by the supervisor's design - the weekday blackout)
    # writes gateway_ok False too; the strip must read it as neutral, never "gateway down".
    put(state="waiting", gateway_ok=False, last_error=None, last_eod_on="2026-10-08",
        phase_detail="Gateway off for the manual-trading blackout until 20:10 ET - "
                     "members' IBKR connectors carry the session")
    html = c.get("/options/status").text
    assert 'data-state="waiting"' in html and "Gateway off by design" in html and "gateway down" not in html
    assert "manual-trading blackout until 20:10 ET" in html and "2026-10-08" in html and "bg-slate-500" in html
    put(state="cycle", gateway_ok=True, heartbeat=_utcnow() - _dt.timedelta(minutes=9))
    html = c.get("/options/status").text
    assert 'data-state="stale"' in html and "no heartbeat for 9 min" in html
    assert "<script" not in html


def test_connector_help_and_download(world):
    c = world["client"]
    r = c.get("/options/connector")
    assert r.status_code == 200 and "Enable ActiveX and Socket Clients" in r.text and "4002" in r.text
    # review #13: the browser's Local Network Access permission, and how to allow it
    assert "Local Network Access" in r.text and "Local network access" in r.text and "Allow" in r.text
    # review (low): no promise of Hermes reads during the session; the out-of-date state
    assert "through the day" not in r.text and "after 20:10 ET" in r.text
    assert "Out of date" in r.text and "download 2.0" in r.text
    r = c.get("/options/connector/download")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    disp = r.headers["content-disposition"]
    assert disp.startswith("attachment;") and re.search(r'filename="TradeHunter-IBKR-Connector-[\w.\-]+\.zip"', disp)
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "TradeHunter-IBKR-Connector/ibkr_bridge.py" in names and "TradeHunter-IBKR-Connector/README.txt" in names
    assert all(n.startswith("TradeHunter-IBKR-Connector/") for n in names)           # one folder, no loose files


# ───────────────────────────────── the page script + the fragments ─────────────────────────────────

def test_options_html_carries_the_probe_the_loop_and_the_dropdown_memory():
    src = (TEMPLATES / "options.html").read_text(encoding="utf-8")
    # the connector pill: /health every 10 s with a 2 s timeout (4 s the first time, review #13)
    assert "'/health'" in src and "PROBE_MS = 10000" in src and "PROBE_TIMEOUT_MS = 2000" in src
    assert "PROBE_FIRST_TIMEOUT_MS = 4000" in src
    assert "AbortController" in src and "setInterval(probe, PROBE_MS)" in src
    # the contribution loop: next -> connector /chain2 -> contribute (+ history once) - green and visible only
    for needle in ("'/options/data/next'", "'/chain2?symbol='", "'/options/data/contribute'",
                   "'/options/data/contribute_history'", "'/underlying?symbol='", "document.hidden",
                   "state.pill !== 'green'", "BETWEEN_MS = 10000"):
        assert needle in src, needle
    # the dropdown and the basket width are remembered per browser
    assert "th.options.strategy" in src and "th.options.basketW" in src
    # the "Refresh these legs live" path and the counts painter
    assert "data-refresh-legs" in src and "kind: 'trade'" in src and "options:counts" in src


def test_old_connector_and_blocked_browser_get_their_own_pill():
    """Review #9: a 1.x connector (no tws_connected, version 'TradeHunterIBKRBridge/1.6')
    is 'out of date - download 2.0' with the Download link, and starts no loop.
    Review #13: a denied Local Network Access permission reads 'blocked by the browser';
    the red tooltip names the permission."""
    src = (TEMPLATES / "options.html").read_text(encoding="utf-8")
    for needle in ("hasOwnProperty.call(h, 'tws_connected')", "v.major >= 2", "split('/').pop()",
                   "is out of date - download 2.0", "setPill('old'", "updLink.classList.toggle('hidden', st !== 'old')",
                   "'local-network-access'", "navigator.permissions.query", "lna.state === 'denied'",
                   "blocked by the browser", "Local Network Access", "setPill('blocked'"):
        assert needle in src, needle
    # the loop only ever runs on green: 'old' / 'blocked' never start it
    tick = src.split("function tick()", 1)[1].split("function ", 1)[0]
    assert "state.pill !== 'green'" in tick


def test_no_swapped_fragment_contains_a_script():
    for name in FRAGMENTS:
        text = (TEMPLATES / name).read_text(encoding="utf-8")
        body = re.sub(r"\{#.*?#\}", "", text, flags=re.S)                   # comments may mention it
        assert "<script" not in body.lower(), name
        assert "onclick=" not in body.lower(), name
