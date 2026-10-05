"""The Options page (step 1): ``routes/options_page.py``, its ten templates and the
``main.py`` / ``menus.py`` / ``base.html`` wiring - part D §D8.2's synthetic cases.

Every test drives the real FastAPI app through ``TestClient`` against a fresh SQLite
file brought to the Alembic head by the real migrations (conftest). ``get_db`` and
``security.current_user`` are overridden so the member and the handler share ONE
session per request; the world is seeded through ``option_store``'s own writers
(``replace_snapshot`` / ``upsert_iv_daily`` / ``upsert_signal``), never a raw row.

Engines that have not landed on this checkout (option_sizing, order_ticket,
option_nightly, strike_picker, option_exits) are either absent - the page must still
answer and say so - or replaced by a contract-shaped stub in ``sys.modules`` for the
one case that needs a number (contracts 0 is rendered as 0, never as 1).
"""
from __future__ import annotations

import datetime as _dt
import math
import types

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app import models
from app.db import get_db
from app.main import app
from app.security import current_user
from app.services import clock, option_prefs, option_store

from .conftest import make_engine

# ───────────────────────────────── the seeded world ─────────────────────────────────

GOLDEN_PICK = {
    "symbol": "LRCX", "strategy": "bull_put", "family": "credit_vertical",
    "legs": [{"expiry": "2026-11-20", "right": "P", "strike": 330.0, "side": "sell", "qty": 1,
              "price": 5.70, "bid": 5.65, "ask": 5.75, "iv": 0.46, "delta": -0.250, "oi": 2140, "volume": 412},
             {"expiry": "2026-11-20", "right": "P", "strike": 320.0, "side": "buy", "qty": 1,
              "price": 3.60, "bid": 3.55, "ask": 3.65, "iv": 0.47, "delta": -0.174, "oi": 1630, "volume": 230}],
    "expiry": "2026-11-20", "dte": 48, "net": -2.10, "width": 10.0,
    "max_profit": 210.0, "max_loss": 790.0, "breakevens": [327.90],
    "pop": 0.75, "pop_kind": "keep", "pop_model": 0.73,
    "greeks": {"delta": 0.076, "theta": 0.021, "vega": -0.048, "gamma": -0.010},
    "liquidity": {"tier": "clean", "widest": 0.10, "min_oi": 1630, "vol_ok": True, "worst_fill": 2.00, "notes": []},
    "constraint": {"ok": True, "detail": "330 sits under support 340.9"},
    "chart_stop": 336.2, "chart_stop_pl": -120.7, "rule_stop_pl": -158.0,
    "checks": [{"name": "open interest >= 500", "ok": True}],
    "score": 0.328, "why": ["best fit to your rules", "most credit per $ risked"],
    "words": {"collect": "you collect $200-$210 (worst likely fill to mid)",
              "risk": "you risk $790, but the chart stop at 336.2 would lose about $121"},
    "sizing": None, "status": "ok",
    "rules_line": "delta 0.20-0.30 · 30-60 days · width 0.5-1.5 ATR ($6-17) · credit >= 25% · under support 340.9",
    "considered": 23, "degenerate": None,
}
LONG_PICK = {
    "symbol": "LRCX", "strategy": "buy_call", "family": "long",
    "legs": [{"expiry": "2026-12-19", "right": "C", "strike": 360.0, "side": "buy", "qty": 1,
              "price": 9.20, "bid": 9.00, "ask": 9.40, "iv": 0.45, "delta": 0.40, "oi": 500, "volume": 12}],
    "expiry": "2026-12-19", "dte": 77, "net": 9.20, "width": None,
    "max_profit": None, "max_loss": 920.0, "breakevens": [369.20],
    "pop": 0.46, "pop_kind": "profit", "pop_model": 0.46,
    "greeks": {"delta": 0.40, "theta": -0.09, "vega": 0.22, "gamma": 0.01},
    "liquidity": {"tier": "limit", "widest": 0.40, "min_oi": 500, "vol_ok": False, "worst_fill": 9.40, "notes": []},
    "constraint": {"ok": True, "detail": ""},
    "chart_stop": 336.2, "chart_stop_pl": -520.0, "rule_stop_pl": -460.0,
    "checks": [], "score": 0.2, "why": ["best fit to your rules"], "words": {},
    "sizing": None, "status": "ok", "rules_line": "delta 0.60-0.70 · 45-90 days", "considered": 9, "degenerate": None,
}

SETUP = {
    "kind": "support_bounce", "direction": "long", "level": 340.9, "zone": [339.1, 342.0],
    "touches": 3, "quality": 80, "close": 349.2, "trend_days": 34, "atr": 11.54,
    "ema": {"e20": 346.1, "e50": 335.8, "e200": 301.2},
    "plan": {"entry": 349.2, "stop": 336.2, "target": 375.2, "r": 13.0}, "stop": 336.2, "target": 375.2,
    "levels": {"support": 340.9, "resistance": 372.0},
    "sup": {"level": 340.9, "zone": [339.1, 342.0], "touches": [], "bounce": None, "d_ema": None,
            "w_ema": None, "vol_ratio": 1.7, "vol_high": True},
    "tl": None, "tl_bounce": None, "rng": {"low": 318.6, "high": 372.0, "sideways": False},
    "evidence": ["EMA20 > EMA50 > EMA200 for 34 sessions"],
}
IV = {"iv30": 46.0, "hv20": 38.0, "hv60": 36.1, "iv_hv_premium": 1.21, "iv_rank": 62.0, "iv_pct": 71.0,
      "iv_n": 252, "state": "ok", "basis": "rank", "provisional": False, "iv_front": 50.0, "iv_back": 45.0,
      "term_ratio": 1.11, "skew25": 4.1, "skew_norm": 0.08, "expected_move": 24.1,
      "earnings_date": "2026-10-22", "earnings_days": 19, "verdict": "SELL",
      "verdict_why": "IV rank 62 (>= 50) and priced for 21% more movement than the stock has actually shown",
      "gates": {"buy": False, "sell_directional": True, "sell_neutral": True, "mid": False},
      "iv30_src": "cboe", "atm_iv30": 45.6, "lo": 28.0, "hi": 57.0}


def _row(key, label, fit, *, score=None, step=1, why=None, must=None, reasons=(), reason_key=None, shown=False):
    return {"key": key, "label": label, "fit": fit, "score": score, "step": step, "why": why,
            "must_happen": must, "reasons": list(reasons), "reason_key": reason_key, "shown": shown}


def strategies_ok():
    """The ten rows: bull_put recommended, bull_call / leaps_call also fit (unbuilt),
    buy_call and iron_condor the two shown near misses, the rest rejected unseen."""
    return [
        _row("bull_put", "Bull put spread", "recommended", score=90.1, step=1,
             why="Uptrend for 34 days. It bounced off support at 340 on high volume.",
             must="LRCX stays above 330 until Nov 20. You keep the credit if it does nothing, drifts up, or even dips a little.",
             shown=True),
        _row("bull_call", "Bull call spread", "also_fits", score=76.0, step=2, why="...", must="...",
             reasons=["not available yet"], reason_key="not_available_yet", shown=True),
        _row("leaps_call", "Buy LEAPS", "also_fits", score=61.0, step=4, why="...", must="...",
             reasons=["not available yet"], reason_key="not_available_yet", shown=True),
        _row("buy_call", "Buy call", "rejected", step=2, reasons=["options too expensive to buy (IV rank 62)"],
             reason_key="expensive", shown=True),
        _row("iron_condor", "Iron condor", "rejected", step=3,
             reasons=["the chart is trending; a range strategy wants flat EMAs"], reason_key="trending_not_sideways", shown=True),
        _row("buy_put", "Buy put", "rejected", step=2, reasons=["wrong direction"], reason_key="wrong_direction"),
        _row("bear_put", "Bear put spread", "rejected", step=2, reasons=["wrong direction"], reason_key="wrong_direction"),
        _row("bear_call", "Bear call spread", "rejected", step=1, reasons=["wrong direction"], reason_key="wrong_direction"),
        _row("calendar", "Calendar spread", "rejected", step=4, reasons=["near-term IV is below later IV"], reason_key="front_iv_under_back"),
        _row("diagonal_call", "Diagonal call spread", "rejected", step=4, reasons=["no setup"], reason_key="no_setup"),
    ]


def strategies_earnings_blocked():
    rows = strategies_ok()
    rows[0] = _row("bull_put", "Bull put spread", "rejected", step=1,
                   reasons=["earnings Oct 22 fall inside every expiry in the window and your rule says no"],
                   reason_key="earnings_inside", shown=True)
    return rows


def _sig(strategies, picks, *, status="ok", headline="Uptrend: EMA 20 above 50 above 200 for 34 days. "
         "It bounced off support at 340 on high volume (1.7x normal). Options are expensive (IV rank 62 "
         "over the last year), so you're paid to sell a put spread below that support."):
    return {"status": status, "headline": headline, "setup": SETUP, "iv": IV, "strategies": strategies,
            "picks": picks, "computed_ms": 120, "engine_version": option_store._engine_version() or "test"}


def _chain(symbol, snap_on, as_of):
    rows = []
    for strike, bid, ask, iv, delta, oi in [(300.0, 1.0, 1.1, 0.49, -0.08, 900), (310.0, 2.0, 2.1, 0.48, -0.12, 1200),
                                            (320.0, 3.55, 3.65, 0.47, -0.174, 1630), (330.0, 5.65, 5.75, 0.46, -0.25, 2140),
                                            (340.0, 8.0, 8.2, 0.45, -0.35, 1800), (350.0, 12.0, 12.3, 0.44, -0.50, 700)]:
        rows.append({"expiry": "2026-11-20", "right": "P", "strike": strike, "bid": bid, "ask": ask, "mid": None,
                     "last": None, "bid_size": 5, "ask_size": 7, "iv": iv, "delta": delta, "gamma": 0.01,
                     "theta": -0.05, "vega": 0.3, "rho": 0.0, "theo": None, "oi": oi, "volume": 100,
                     "prev_close": None})
        rows.append({"expiry": "2026-11-20", "right": "C", "strike": strike, "bid": bid * 2, "ask": ask * 2 + 0.1,
                     "iv": iv, "delta": 1 + delta, "oi": oi, "volume": 50, "gamma": 0.01, "theta": -0.05, "vega": 0.3})
    rows.append({"expiry": "2026-12-19", "right": "C", "strike": 360.0, "bid": 9.0, "ask": 9.4, "iv": 0.45,
                 "delta": 0.40, "oi": 500, "volume": 12})
    return {"symbol": symbol, "snap_on": snap_on, "kind": "eod", "source": "cboe", "as_of": as_of,
            "spot": 349.2, "iv30": 46.0, "rows": rows, "partial": False}


@pytest.fixture
def world(engine, db, user):
    """The member (with ONE rule override, so their hash differs from the house hash),
    a second member, four basket tickers and the signal rows behind the three basket
    states plus an earnings-blocked card."""
    from app.routes import options_page as op

    other = models.User(email="other@local.test", display_name="Other", role=models.ROLE_MEMBER,
                        status=models.APPROVED, created_at=_dt.datetime(2026, 1, 6, tzinfo=_dt.timezone.utc))
    db.add(other)
    db.commit()

    _, errs = option_prefs.write(db, user, "credit", {"credit_vertical.short_delta_hi": "0.22"})
    assert errs == []
    mine = option_prefs.prefs_hash(option_prefs.read(db, user))
    house = option_prefs.HOUSE_HASH
    assert mine != house

    day = clock.last_trading_day(clock.et_date()).isoformat()
    as_of = _dt.datetime.combine(_dt.date.fromisoformat(day), _dt.time(20, 0))      # 16:00 ET as naive UTC
    for sym in ("LRCX", "MA", "ISRG", "KO"):
        ch = _chain(sym, day, as_of)
        option_store.replace_snapshot(db, ch)
        option_store.upsert_iv_daily(db, ch)
    db.commit()

    ok_picks = {"bull_put": [GOLDEN_PICK], "buy_call": [LONG_PICK]}
    none_picks = {"bull_put": [{"status": "nearest", "legs": [],
                                "degenerate": {"reason_key": "no_band", "text": "No strike sits in your delta 0.20-0.22 band; nearest: 335P at delta 0.33.",
                                               "nearest": "Nov 20 335/325 at delta 0.33", "rule_label": "short strike delta", "fix": "widen the band"}}]}
    for sym, h, strategies, picks in [("LRCX", mine, strategies_ok(), ok_picks), ("LRCX", house, strategies_ok(), ok_picks),
                                      ("MA", mine, strategies_ok(), none_picks), ("MA", house, strategies_ok(), ok_picks),
                                      ("ISRG", house, strategies_ok(), ok_picks),
                                      ("KO", mine, strategies_earnings_blocked(), {}), ("KO", house, strategies_earnings_blocked(), {})]:
        option_store.upsert_signal(db, _sig(strategies, picks), prefs_hash=h, symbol=sym, snap_on=day, kind="eod", as_of=as_of)
    db.commit()

    res = op._add_symbols(db, user, ["LRCX", "MA", "ISRG", "KO"], "paste")
    assert res == {"added": 4, "skipped": 0, "over_cap": 0, "total": 4}

    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    uid, oid = user.id, other.id
    state = {"uid": uid}

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    def _user(s=Depends(get_db)):
        """The same session the handler gets (FastAPI caches get_db per request), so
        a write through the handler's session reaches this user row."""
        return s.get(models.User, state["uid"])

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[current_user] = _user
    try:
        yield {"client": TestClient(app, follow_redirects=False), "uid": uid, "oid": oid, "mine": mine,
               "house": house, "day": day, "as_of": as_of, "Session": Session, "state": state, "op": op}
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Keep every test off the wire once the engines have landed: card_for's lazy
    compute (Yahoo bars behind chart_state) and Refresh (Cboe) are the two paths
    on this page that would otherwise reach a market feed."""
    monkeypatch.setattr(option_store, "_lazy_compute", lambda *a, **k: None)
    try:
        from app.services import option_data, option_nightly
    except ImportError:
        return

    def _refresh(db, sym, user, **kw):
        raise option_data.ChainError(f"{sym}: Cboe HTTP 429 (no network in tests)")

    monkeypatch.setattr(option_nightly, "refresh_symbol", _refresh)


def _sizing_stub(monkeypatch):
    """option_sizing.size as the page needs it (II.2.4): the REAL module when it has
    landed on this checkout, else a contract-shaped stub - floor, never round up,
    never max(1, ...)."""
    import sys

    try:
        from app.services import option_sizing as real
        if callable(getattr(real, "size", None)):
            return real
    except ImportError:
        pass

    mod = types.ModuleType("app.services.option_sizing")

    def size(pick, nlv, prefs):
        acct = (prefs or {}).get("account") or {}
        risk_pct = float(acct.get("risk_pct") or 1.0)
        gap = float(((prefs or {}).get("shared") or {}).get("gap_mult") or 2.0)
        src = acct.get("nlv_source")
        if not nlv:
            return {"contracts": None, "nlv": None, "nlv_source": None, "risk_pct": risk_pct,
                    "note": "sized once you tell us the account value (My rules -> Shared)", "line": None}
        budget = nlv * risk_pct / 100.0
        loss = max(0.0, -float(pick.get("chart_stop_pl") or 0.0))
        ml = float(pick.get("max_loss") or 0.0)
        by_stop = math.floor(budget / loss) if loss > 0 else None
        by_gap = math.floor(budget * gap / ml) if ml > 0 else None
        by_not = math.floor(nlv * 0.10 / ml) if ml > 0 else None
        n = min(x for x in (by_stop, by_gap, by_not) if x is not None)
        out = {"nlv": nlv, "nlv_source": src or "prefs", "risk_pct": risk_pct, "risk_budget": budget, "gap_mult": gap,
               "loss_at_stop_usd": loss, "by_chart_stop": by_stop, "by_gap": by_gap, "by_notional": by_not,
               "contracts": n, "capital_at_risk_usd": n * loss, "max_loss_total_usd": n * ml,
               "max_loss_pct_nlv": round(n * ml / nlv * 100, 1), "note": None}
        out["line"] = (f"{n} contracts: about ${n * loss:,.0f} if the stop fires, up to ${n * ml:,.0f} "
                       f"({out['max_loss_pct_nlv']}% of your account) if the stock gaps past it") if n else None
        if n == 0:
            out["note"] = f"Not even one contract fits your {risk_pct:g}% - lower the risk or choose a narrower spread"
        return out

    mod.size = size
    monkeypatch.setitem(sys.modules, "app.services.option_sizing", mod)
    return mod


# ───────────────────────────────── the shell, the menu, the router ─────────────────────────────────

def test_menu_shows_options_and_the_shell_renders(world):
    c = world["client"]
    r = c.get("/options")
    assert r.status_code == 200
    html = r.text
    assert 'href="/options"' in html and ">Options<" in html          # the nav item (menus.MENUS)
    assert "My rules" in html and "toggle from:closest details once" in html
    assert 'hx-get="/options/badge"' in html                           # base.html's badge points here now
    assert 'hx-get="/options/status/strip"' in html
    s = world["Session"]()
    try:
        u = s.get(models.User, world["uid"])
        assert (u.prefs or {}).get("options_seen_at")                  # GET /options clears ideas_new
    finally:
        s.close()
    # ?tab=positions / ?symbol= / ?pause=7 are accepted
    assert c.get("/options?tab=positions&focus=3").status_code == 200
    assert c.get("/options?symbol=lrcx&pause=7").status_code == 200


def test_router_order_fixed_paths_before_the_legacy_catch_all():
    paths = [getattr(r, "path", None) for r in app.routes]
    assert paths.index("/options/basket") < paths.index("/options/{symbol}")
    assert "/options/track-idea" in paths and "/options/track" in paths    # the legacy POST /options/track is untouched
    from app import menus
    assert ("options", "Options", None, "/options") in menus.MENUS
    assert {"ivscan", "spreads", "positions"} <= set(menus.HIDDEN_KEYS)


def test_member_without_the_options_grant_is_redirected(world):
    c = world["client"]
    s = world["Session"]()
    try:
        u = s.get(models.User, world["uid"])
        u.menu_access = ["calendar_month"]
        s.commit()
        assert c.get("/options/basket").status_code == 303           # require_menu("options") on the new router
        assert c.get("/options").status_code == 303
        u.menu_access = None
        s.commit()
    finally:
        s.close()
    assert c.get("/options/basket").status_code == 200


# ───────────────────────────────── every route answers ─────────────────────────────────

def test_every_route_answers(world):
    c = world["client"]
    for path in ["/options/basket", "/options/basket?sort=iv&compact=1", "/options/card/LRCX",
                 "/options/picks/LRCX?strategy=bull_put", "/options/picks/LRCX?strategy=buy_call",
                 "/options/picks/LRCX?strategy=bull_call", "/options/chart/LRCX?strategy=bull_put&pick=0",
                 "/options/chart/LRCX?strategy=buy_call", "/options/payoff/LRCX?strategy=bull_put&pick=0",
                 "/options/payoff/LRCX?strategy=bull_put&pick=0&units=R", "/options/chain/LRCX",
                 "/options/chain/LRCX?expiry=2026-11-20&all=1", "/options/ticket/LRCX?strategy=bull_put",
                 "/options/ticket/LRCX?strategy=bull_put&dip=1&contracts=2", "/options/positions",
                 "/options/positions?status=closed", "/options/badge", "/options/status/strip",
                 "/options/card/ZZZZ", "/options/picks/ZZZZ", "/options/chart/ZZZZ", "/options/chain/ZZZZ",
                 "/options/ticket/ZZZZ", "/options/payoff/ZZZZ"] + [f"/options/rules?tab={t}" for t in option_prefs.TABS]:
        r = c.get(path)
        assert r.status_code == 200, (path, r.status_code, r.text[:300])
    r = c.post("/options/basket/add", data={"symbol": "nvda"})
    assert r.status_code == 200 and 'data-sym="NVDA"' in r.text and "options:basket-changed" in r.headers.get("HX-Trigger", "")
    r = c.post("/options/basket/remove", data={"symbol": "NVDA"})
    assert r.status_code == 200 and 'data-sym="NVDA"' not in r.text
    r = c.post("/options/refresh/LRCX")
    assert r.status_code in (200, 429)
    if r.status_code == 200:
        assert "Could not refresh" in r.text or "Refreshed from Cboe" in r.text
    r = c.post("/options/live/LRCX", json={"chain": {"ok": False, "error": "no bridge"},
                                            "diag": {"page_origin": "http://x", "page_secure": False, "bridge": "http://127.0.0.1:9224",
                                                     "err_name": "TypeError", "err_message": "Failed to fetch"}})
    assert r.status_code == 200 and "Could not reach your IBKR bridge on this PC (127.0.0.1:9224)" in r.text
    assert "Start the bridge" in r.text and "Diagnostics" in r.text
    r = c.post("/options/telegram", json={"action": "verify", "code": "123456"})
    assert r.status_code == 200 and "Enter the 6-digit code the bot sent you after /start." in r.text
    r = c.post("/options/telegram", data={"action": "request_code", "chat_id": "abc"})
    assert r.status_code == 200 and "Enter the chat id" in r.text
    r = c.post("/options/telegram", data={"action": "pause", "pause_days": "7"})
    assert r.status_code == 200 and "paused" in r.text
    r = c.post("/options/rules/reset", data={"tab": "time"})
    assert r.status_code == 200 and "options:rules-changed" in r.headers.get("HX-Trigger", "")
    r = c.post("/options/track-idea", data={"symbol": "LRCX", "strategy": "bull_put", "pick": "7", "contracts": "2"})
    assert r.status_code == 409 and "The strikes changed" in r.headers.get("HX-Trigger", "")
    r = c.post("/options/positions/999/close", data={"reason": "manual"})
    assert r.status_code == 200


# ───────────────────────────────── the basket ─────────────────────────────────

def test_the_three_basket_states_render(world):
    c = world["client"]
    html = c.get("/options/basket").text
    # LRCX: a row under the member's hash with a real pick -> has_picks, the plain idea word
    # MA:   a row under the member's hash with only the 'nearest' stub -> no_strike_passes (faded)
    # ISRG: only the house row -> not_checked (grey dot, 'open the card to check')
    assert "sell put" in html
    assert 'title="recommended, but no strike passes your rules today"' in html
    assert 'title="not checked under your rules yet - open the card to check"' in html
    assert html.count('data-sym="') == 4
    # the IV cell: a full-year rank is amber at 62 (sell premium); the trend arrow is up
    assert "text-amber-300" in html and "&#8599;" in html
    # the route never recomputes pick_state: it reads it off basket_rows_for
    s = world["Session"]()
    try:
        ctx = world["op"]._basket_context(s, s.get(models.User, world["uid"]))
    finally:
        s.close()
    states = {it["row"].symbol: it["pick_state"] for it in ctx["items"]}
    assert states["LRCX"] == "has_picks" and states["MA"] == "no_strike_passes" and states["ISRG"] == "not_checked"
    assert states["KO"] is None                                      # no recommendation -> no pick state


def test_basket_is_scoped_per_owner(world):
    c = world["client"]
    world["state"]["uid"] = world["oid"]
    try:
        html = c.get("/options/basket").text
        assert 'data-sym="' not in html and "Your basket is empty" in html
    finally:
        world["state"]["uid"] = world["uid"]
    assert 'data-sym="LRCX"' in c.get("/options/basket").text


def test_import_seventy_symbols_caps_at_sixty(world):
    c = world["client"]
    syms = [f"T{i:03d}" for i in range(70)]
    r = c.post("/options/basket/import", json={"source": "paste", "text": ", ".join(syms)})
    assert r.status_code == 200
    assert r.json() == {"added": 56, "skipped": 0, "over_cap": 14, "total": 60}   # 4 already in the basket
    assert "options:basket-changed" in r.headers.get("HX-Trigger", "")
    # from an empty basket the 70-symbol case is exactly 60 + over_cap 10
    world["state"]["uid"] = world["oid"]
    try:
        r = c.post("/options/basket/import", json={"source": "paste", "text": " ".join(syms)})
        assert r.json() == {"added": 60, "skipped": 0, "over_cap": 10, "total": 60}
        # duplicates and junk are skipped, never counted as added
        r = c.post("/options/basket/import", json={"source": "paste", "text": "T000, t001, XX1234567890123"})
        assert r.json()["added"] == 0 and r.json()["skipped"] >= 2
        # the positions source imports this member's OPEN option_trades symbols (none here)
        r = c.post("/options/basket/import", json={"source": "positions"})
        assert r.json()["added"] == 0
    finally:
        world["state"]["uid"] = world["uid"]


# ───────────────────────────────── the card and the picks ─────────────────────────────────

def test_card_headline_gauge_chips_and_must_happen(world):
    c = world["client"]
    html = c.get("/options/card/LRCX").text
    assert 'class="opt-card' in html and 'data-strategy="bull_put"' in html
    assert "Uptrend: EMA 20 above 50 above 200 for 34 days" in html          # the stored headline, verbatim
    assert "What has to happen:" in html and "LRCX stays above 330 until Nov 20" in html
    assert "&#10003; Bull put spread" in html                                 # the recommended chip first
    assert "Bull call spread &middot; not available yet" in html             # an unbuilt fit is never recommended
    assert "Buy call &middot; expensive" in html and "Iron condor &middot; trending, not sideways" in html
    assert "other strategies" in html
    assert "step " not in html.lower()                                        # the build-phase words never reach a member
    assert "Live quotes need TWS on your PC" in html                          # the touch / narrow hint
    assert 'hx-get="/options/chart/LRCX?strategy=bull_put&pick=0"' in html
    # no signal yet: the chart still shows, nothing else does
    html = c.get("/options/card/ZZZZ").text
    assert "No read yet for ZZZZ" in html and 'hx-get="/options/chart/ZZZZ"' in html and "opt-chip" not in html


def test_picks_table_in_collect_risk_chance_words(world):
    c = world["client"]
    html = c.get("/options/picks/LRCX?strategy=bull_put").text
    assert "Strikes under your rules" in html
    assert "Nov 20 · 330/320 put" in html                             # the legs cell, from the pick's legs
    assert "$200-$210" in html and "risk $790" in html and "about 75% chance of keeping it" in html
    assert "27% on risk" in html and "best fit to your rules" in html and "clean" in html
    assert "toggle from:closest details once" in html                       # the full-chain expander's lazy load
    assert ">Order ticket<" in html and ">Track this<" in html
    assert "sized once you tell us the account value" in html               # no option_sizing / no NLV: said plainly
    assert 'id="optPayoff"' in html and "/options/payoff/LRCX?strategy=bull_put&pick=0" in html
    # the greek words travel as titles, never as bare numbers
    assert "not a promise" in html
    # a rejected (not earnings) strategy with strikes: the ghost button and the amber banner
    html = c.get("/options/picks/LRCX?strategy=buy_call").text
    assert "Not recommended today: options too expensive to buy (IV rank 62)" in html
    assert "Order ticket (not recommended)" in html and "pay $920" in html and "about 46% chance of profit" in html
    # an unbuilt strategy: no strikes, no ticket
    html = c.get("/options/picks/LRCX?strategy=bull_call").text
    assert "that strategy is not in TradeHunter yet" in html and "Order ticket" not in html
    # nothing passes: the decision reads as a decision, not a blank
    html = c.get("/options/picks/MA?strategy=bull_put").text
    assert "No strike passes your rules for Bull put spread today" in html
    assert "Closest that didn't pass" in html and "short strike delta" in html and "Credit spreads" in html


def test_contracts_zero_renders_the_note_not_one(world, monkeypatch):
    _sizing_stub(monkeypatch)
    c = world["client"]
    # account value 100,000 at 1%: 8 / 2 / 12 -> 2 contracts, both figures on the line
    r = c.post("/options/rules", data={"tab": "shared", "nlv": "100000", "risk_pct": "1"})
    assert r.status_code == 200
    html = c.get("/options/picks/LRCX?strategy=bull_put").text
    assert "2 contracts: about $242 if the stop fires, up to $1,580" in html
    assert 'name="contracts" min="1" max="500" value="2"' in html
    # account value 5,000: 0 contracts - said plainly, the box reads 0, Track is disabled
    r = c.post("/options/rules", data={"tab": "shared", "nlv": "5000"})
    assert r.status_code == 200
    html = c.get("/options/picks/LRCX?strategy=bull_put").text
    assert "Not even one contract fits your 1%" in html
    assert 'name="contracts" min="1" max="500" value="0"' in html
    assert 'value="1"' not in html.split('name="contracts"')[1][:80]
    assert "disabled" in html.split("Track this")[0][-400:]
    # the ticket shows the sizing note and no orders
    html = c.get("/options/ticket/LRCX?strategy=bull_put&contracts=0").text
    assert "Not even one contract fits" in html


def test_remember_this_never_resets_the_safety_switches(world):
    c = world["client"]
    # the '[remember this]' click posts only tab=shared&nlv=: no checkbox on the form
    r = c.post("/options/rules", data={"tab": "shared", "nlv": "50000"})
    assert r.status_code == 200
    s = world["Session"]()
    try:
        u = s.get(models.User, world["uid"])
        prefs = option_prefs.read(s, u)
        assert prefs["shared"]["chart_constraint"] is True                  # not read as an unticked box
        assert prefs["shared"]["earnings_rule"] == "none_inside"
        assert prefs["account"]["nlv"] == 50000.0
        assert option_prefs.prefs_hash(prefs) == world["mine"]              # nlv never changes the hash
    finally:
        s.close()
    # a real credit-tab save re-renders the drawer with the override dot and the trigger
    r = c.post("/options/rules", data={"tab": "credit", "credit_vertical.short_delta_hi": "0.25"})
    assert r.status_code == 200 and "options:rules-changed" in r.headers.get("HX-Trigger", "")
    assert "changed from the house default" in r.text and "Save Credit spreads rules" in r.text
    r = c.post("/options/rules", data={"tab": "credit", "credit_vertical.short_delta_hi": "0.9"})
    assert "must be between 0.05 and 0.5" in r.text
    r = c.post("/options/rules/reset", data={"tab": "credit"})
    assert "house defaults" in r.text


# ───────────────────────────────── ticket, chart, payoff, chain ─────────────────────────────────

def test_rejected_for_earnings_strategy_produces_no_ticket(world):
    c = world["client"]
    html = c.get("/options/ticket/KO?strategy=bull_put").text
    assert "No ticket: earnings Oct 22 fall inside this trade and your rule says no." in html
    assert "<pre" not in html and "ORDER" not in html                      # no legs, no prices
    # the picks fragment hides the strike table entirely and offers no buttons
    html = c.get("/options/picks/KO?strategy=bull_put").text
    assert "your rule says no" in html and "Order ticket" not in html and "Track this" not in html
    assert "allow defined-risk trades through earnings in My rules" in html
    # Track refuses it too
    r = c.post("/options/track-idea", data={"symbol": "KO", "strategy": "bull_put", "pick": "0", "contracts": "1"})
    assert r.status_code == 409
    # the ticket panel on a good card carries the header line and both renderings (or says the
    # ticket text is not on this build yet) and never a condition unless dip=1
    html = c.get("/options/ticket/LRCX?strategy=bull_put&contracts=2").text
    assert "Order ticket" in html and "Enter on the dip" in html
    assert ("Prices are from" in html and "21:30 Malaysia time" in html) or "not available on this build" in html
    assert "Tracking only: TradeHunter never sends an order." in html


def test_chart_fragment_context_long_vs_credit(world):
    op = world["op"]
    s = world["Session"]()
    try:
        u = s.get(models.User, world["uid"])
        credit = op._chart_context_for(s, u, "LRCX", strategy="bull_put", pick=0, trade=0)
        assert credit["family"] == "credit_vertical"
        assert len(credit["spread"]["legs"]) == 2 and credit["spread"]["breakevens"] == [327.9]
        assert credit["spread"]["short"] == 330.0 and credit["spread"]["long"] == 320.0   # the legacy keys ride along
        assert credit["levels"] is None
        assert credit["bounce"]["level"] == 340.9                        # from the STORED setup.sup, no detector
        long_ = op._chart_context_for(s, u, "LRCX", strategy="buy_call", pick=0, trade=0)
        assert long_["family"] == "long" and long_["spread"] is None
        assert long_["levels"] == {"entry": 349.2, "stop": 336.2, "target": 375.2, "label": ""}
    finally:
        s.close()
    c = world["client"]
    html = c.get("/options/chart/LRCX?strategy=bull_put&pick=0").text
    assert "var SPREAD = " in html and '"legs"' in html and "chart_setup_seed" not in html
    html = c.get("/options/chart/LRCX?strategy=buy_call").text
    assert '"stop": 336.2' in html


def test_payoff_pane_and_chain_expander(world):
    c = world["client"]
    r = c.get("/options/payoff/LRCX?strategy=bull_put&pick=0")
    assert r.status_code == 200
    assert ("<svg" in r.text) or ("payoff chart is not drawn on this build yet" in r.text)
    assert "min-h-[300px]" in r.text or "viewBox" in r.text               # always the same pane height
    html = c.get("/options/chain/LRCX?expiry=2026-11-20&strategy=bull_put&pick=0").text
    assert "CALLS" in html and "PUTS" in html and "bg-emerald-500/20" not in html.split("<tbody>")[0]
    assert html.count("bg-rose-500/20") >= 2                             # the pick's two put legs highlighted
    assert "Gamma" in html and "show all strikes" in html
    html = c.get("/options/chain/LRCX?expiry=2026-11-20&all=1").text
    assert "show &plusmn;12 strikes" in html


# ───────────────────────────────── track, positions, badge, strip ─────────────────────────────────

def test_track_idea_then_positions_tab_and_close(world):
    c = world["client"]
    r = c.post("/options/track-idea", data={"symbol": "LRCX", "strategy": "bull_put", "pick": "0", "contracts": "2", "note": "first"})
    assert r.status_code == 201
    assert "options:tracked" in r.headers.get("HX-Trigger", "")
    assert 'class="opt-positions' in r.text and "LRCX" in r.text and "Nov 20" in r.text
    assert "Track a trade by hand" not in r.text
    s = world["Session"]()
    try:
        t = s.query(models.OptionTrade).filter(models.OptionTrade.user_id == world["uid"]).one()
        assert t.strategy == "bull_put" and t.family == "credit_vertical" and t.contracts == 2
        assert t.front_expiry == "2026-11-20" and t.back_expiry is None and t.net_entry == -2.10
        assert t.max_loss == 790.0 and t.chart_stop == 336.2 and t.chart_target is None
        assert t.legs[0]["entry_price"] == 5.70 and t.legs[0]["entry_delta"] == -0.25 and t.legs[0]["oi"] == 2140
        assert t.signal_id is not None and t.earnings_date_at_entry == "2026-10-22" and t.meta == {}
        assert t.note == "first"
        tid = t.id
    finally:
        s.close()
    # the rejected-but-allowed strategy's note starts with the rejection sentence
    r = c.post("/options/track-idea", data={"symbol": "LRCX", "strategy": "buy_call", "pick": "0", "contracts": "1"})
    assert r.status_code == 201
    s = world["Session"]()
    try:
        t2 = s.query(models.OptionTrade).filter(models.OptionTrade.strategy == "buy_call").one()
        assert t2.note.startswith("Not recommended: options too expensive to buy (IV rank 62).")
        assert t2.family == "long" and t2.chart_target == 375.2
    finally:
        s.close()
    html = c.get(f"/options/positions?focus={tid}").text
    assert f'hx-get="/options/chart/LRCX?trade={tid}"' in html and "Mark closed" in html
    assert f"/options/payoff/LRCX?trade={tid}" in html
    r = c.post(f"/options/positions/{tid}/close", data={"reason": "manual"})
    assert r.status_code == 200
    s = world["Session"]()
    try:
        assert s.get(models.OptionTrade, tid).status == "closed"
    finally:
        s.close()
    assert "LRCX" in c.get("/options/positions?status=closed").text
    assert c.get(f"/options/chart/LRCX?trade={tid}").status_code == 200
    assert c.get(f"/options/payoff/LRCX?trade={tid}").status_code == 200
    # the positions source now imports the open trade's symbol (buy_call still open)
    world["state"]["uid"] = world["oid"]
    try:
        assert c.post("/options/basket/import", json={"source": "positions"}).json()["added"] == 0
    finally:
        world["state"]["uid"] = world["uid"]


def test_badge_keys_and_status_strip(world):
    c = world["client"]
    d = c.get("/options/badge").json()
    assert set(d) == {"run_on", "finished_at", "ok", "errors", "stale", "running", "job_missed", "ideas_new", "urgent", "watch"}
    assert d["run_on"] is None and d["urgent"] == 0 and d["watch"] == 0 and d["ideas_new"] == 0
    assert d["job_missed"] is True                                        # no nightly run has ever finished
    # an open trade whose newest check is WATCH with the earnings-now-inside row counts in urgent
    s = world["Session"]()
    try:
        t = models.OptionTrade(user_id=world["uid"], symbol="LRCX", strategy="bull_put", family="credit_vertical",
                               legs=GOLDEN_PICK["legs"], front_expiry="2026-11-20", net_entry=-2.1, contracts=1,
                               max_loss=790.0, chart_stop=336.2, earnings_date_at_entry=None, meta={}, status="open")
        s.add(t)
        s.flush()
        s.add(models.OptionTradeCheck(trade_id=t.id, checked_on=world["day"], spot=349.2, mark=2.0, pl=10.0,
                                      dte=47, state="WATCH", action="watch it", reasons=["x"], urgent=False, source="cboe"))
        s.commit()
    finally:
        s.close()
    d = c.get("/options/badge").json()
    assert d["urgent"] == 1 and d["watch"] == 0                           # earnings Oct 22 <= Nov 20, unknown at entry -> urgent
    html = c.get("/options/positions").text
    assert "now fall inside this trade (the date was unknown or later when you entered)" in html
    html = c.get("/options/status/strip").text
    assert "Options" in html and "4 tickers" in html and "job: never run" in html
    # a finished nightly row turns the pill
    from app.services import job_runs
    s = world["Session"]()
    try:
        run = job_runs.start(s, "nightly", world["day"], source="cboe")
        job_runs.finish(s, run, ok=4, errors=0, rows=100, pushed=0, note="", detail={})
    finally:
        s.close()
    html = c.get("/options/status/strip").text
    assert "4/4 tickers" in html
    d = c.get("/options/badge").json()
    assert d["run_on"] == world["day"] and d["ok"] == 4


def test_live_grades_in_request_and_persists_only_the_iv_series(world):
    c = world["client"]
    s = world["Session"]()
    try:
        n_snap = s.query(models.OptionChainSnapshot).filter(models.OptionChainSnapshot.symbol == "LRCX").count()
        n_hist = s.query(models.IVDaily).filter(models.IVDaily.symbol == "LRCX", models.IVDaily.kind == "history").count()
    finally:
        s.close()
    today = clock.et_date()
    series = [{"on": (today - _dt.timedelta(days=d)).isoformat(), "iv": 30.0 + d} for d in range(10, 1, -1)]
    puts = [{"strike": k, "bid": b, "ask": b + 0.1, "iv": 46.0, "delta": dl, "oi": 1000, "volume": 10, "gamma": 0.01, "theta": -0.05, "vega": 0.3}
            for k, b, dl in [(320.0, 3.5, -0.17), (330.0, 5.6, -0.25), (340.0, 8.0, -0.35)]]
    payload = {"chain": {"ok": True, "symbol": "LRCX", "spot": 349.2, "expiry": "20261120", "puts": puts, "calls": [], "oi_ok": True},
               "iv": {"iv_current": 46.0, "iv_rank": 59.0, "iv_percentile": 70.0, "series": series},
               "nlv": 100000.0, "diag": {"data_mode": "live"}}
    r = c.post("/options/live/LRCX?strategy=bull_put", json=payload)
    assert r.status_code == 200, r.text[:300]
    assert "live &middot; TWS" in r.text and "Live from your TWS" in r.text
    assert "IV rank 59 (TWS, live)" in r.text
    assert "A year of IV history from your TWS was filed" in r.text
    # the stored (delayed) rank sits beside the live one; the bootstrap marks the symbol's
    # signal rows stale_iv so the next read recomputes the gauge from the fuller window
    # (that recompute is the engines' job and is kept off the wire here)
    assert "62 (delayed)" in r.text or "9 of 60 days" in r.text or "against 9 days" in r.text
    s = world["Session"]()
    try:
        rows = s.query(models.OptionSignal).filter(models.OptionSignal.symbol == "LRCX", models.OptionSignal.snap_on == world["day"]).all()
        assert rows and all(r_.status == "stale_iv" for r_ in rows)
    finally:
        s.close()
    s = world["Session"]()
    try:
        assert s.query(models.OptionChainSnapshot).filter(models.OptionChainSnapshot.symbol == "LRCX").count() == n_snap
        rows = s.query(models.IVDaily).filter(models.IVDaily.symbol == "LRCX", models.IVDaily.kind == "history").all()
        # every point is filed EXCEPT the day the server read itself (never overwritten)
        assert len(rows) == n_hist + len([p for p in series if p["on"] != world["day"]])
        assert all(0.1 <= r_.iv30 <= 1000 and r_.iv30 >= 30 for r_ in rows)      # percent, as sent
        own = s.query(models.IVDaily).filter(models.IVDaily.symbol == "LRCX", models.IVDaily.on == world["day"]).one()
        assert own.source == "cboe" and own.iv30 == 46.0
        u = s.get(models.User, world["uid"])
        from app.services import trade_prefs
        assert trade_prefs.read(u)["nlv"] == 0.0                                   # the live NLV is never written
        assert s.query(models.OptionJob).filter(models.OptionJob.job == "bootstrap").count() == 1
    finally:
        s.close()
    # an older bridge (no series): the ONE sentence, the quotes still graded
    payload["iv"] = {"iv_current": 46.0}
    r = c.post("/options/live/LRCX", json=payload)
    assert r.status_code == 200 and "Your bridge is older than 1.6 - restart bridge\\start_ibkr_bridge.bat" in r.text


# ───────────────────────────────── the templates ─────────────────────────────────

def test_templates_carry_no_scrollbar_rules_and_the_toggle_trigger():
    from pathlib import Path
    tdir = Path(__file__).resolve().parent.parent / "app" / "templates"
    names = ["options.html", "_options_basket.html", "_options_card.html", "_options_picks.html", "_options_chart.html",
             "_options_rules.html", "_options_ticket.html", "_options_status.html", "_options_chain.html",
             "_options_positions_tab.html"]
    for n in names:
        src = (tdir / n).read_text(encoding="utf-8")
        assert "scrollbar-" not in src and "::-webkit-scrollbar" not in src, n
        assert "thPayoffLoad" not in src and "<canvas" not in src, n              # no client-side payoff painter
        assert "step 1" not in src.lower() and "phase" not in src.lower(), n
    assert "toggle from:closest details once" in (tdir / "options.html").read_text(encoding="utf-8")
    assert "toggle from:closest details once" in (tdir / "_options_picks.html").read_text(encoding="utf-8")
    base = (tdir / "base.html").read_text(encoding="utf-8")
    assert "--po-exp" in base and "--po-today" in base and "--po-profit-fill" in base and "--po-loss-fill" in base
    assert 'hx-get="/options/badge"' in base and "d.job_missed" in base and "d.ideas_new" in base
    assert "scrollbar-width: thin" in base                                        # the house rule is untouched
