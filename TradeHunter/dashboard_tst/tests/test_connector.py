"""The member connector 2.0 (bridge/ibkr_bridge.py).

OPTIONS_V2_DESIGN.md §5. No ib_insync and no network: the bridge is loaded from its
file as a fresh module per test, ``th_ibkr`` is replaced by a fake with the §3.1
signatures, and the HTTP handler runs on an ephemeral loopback port in a thread.

Since v4.134 (§13) the Options page no longer offers the connector as a download (its
zip builder ``app/services/opt_connector_pkg.py`` and the build_zip tests are gone); the
bridge itself stays for the legacy hidden pages and the basket's TWS-scanner import, so
its own tests stay here.
"""
from __future__ import annotations

import asyncio
import http.client
import importlib.util
import json
import re
import threading
import time
import types
from pathlib import Path
from urllib.parse import quote, urlencode

import pytest

DASH = Path(__file__).resolve().parent.parent
BRIDGE_DIR = DASH / "bridge"
BRIDGE_PY = BRIDGE_DIR / "ibkr_bridge.py"
SITE = "https://app.tradehunter.net"


# ───────────────────────────────────────────── fixtures / fakes

@pytest.fixture
def bridge(tmp_path):
    spec = importlib.util.spec_from_file_location("ibkr_bridge_v2_under_test", BRIDGE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.RUN["config_path"] = tmp_path / "connector.json"
    yield mod
    w = mod._worker
    if w is not None and hasattr(w, "close"):
        w.close()


class FakeIB:
    """Enough of ib_insync.IB for the connection keeper."""

    def __init__(self, accounts=("DU1234567",), fail=None):
        self.connected = False
        self.calls = []
        self.accounts = list(accounts)
        self.fail = fail

    def isConnected(self):  # noqa: N802
        return self.connected

    async def connectAsync(self, host, port, clientId, timeout, readonly):  # noqa: N803
        self.calls.append((host, port, clientId, readonly))
        if self.fail:
            raise self.fail
        self.connected = True

    def disconnect(self):
        self.connected = False

    def managedAccounts(self):  # noqa: N802
        return self.accounts


def connected_ib():
    ib = FakeIB()
    ib.connected = True
    return ib


def fake_th(*, spot=101.0, spot_mdt="live", quote_mdt="live", quote_sleep=0.0, spot_fail=False,
            with_fetch=False):
    """A th_ibkr stand-in (design §3.1 parts; ``fetch`` only when asked) that records
    how it was called."""
    calls = {"chain_defs": 0, "spot": 0, "plan": [], "quote": [], "daily_bars": [],
             "iv_history": [], "account": 0, "fetch": [], "reset_mdt": 0, "clear_cache": 0}

    async def chain_defs(ib, symbol):
        calls["chain_defs"] += 1
        return {"symbol": symbol, "con_id": 1, "exchange": "SMART",
                "expiries": ["2026-11-20", "2026-12-18"], "strikes": [90.0, 100.0, 110.0],
                "multiplier": 100}

    async def spot_fn(ib, symbol):
        calls["spot"] += 1
        if spot_fail:
            raise RuntimeError("no market data")
        return {"spot": spot, "bid": spot - 0.05, "ask": spot + 0.05, "last": spot,
                "close": spot, "mdt": spot_mdt}

    def plan(defs, *, spot, **kw):
        calls["plan"].append({"spot": spot, **kw})
        return [{"expiry": e, "dte": 40, "strikes": defs["strikes"]} for e in defs["expiries"]]

    async def quote(ib, symbol, window, *, max_lines=60, **kw):
        calls["quote"].append({"symbol": symbol, "window": window, "max_lines": max_lines,
                               "deadline": kw.get("deadline")})
        if quote_sleep:
            await asyncio.sleep(quote_sleep)
        rows = [{"expiry": w["expiry"], "right": r, "strike": k, "bid": 1.0, "ask": 1.2,
                 "mid": 1.1, "last": None, "bid_size": 3, "ask_size": 4, "volume": 10,
                 "oi": 100, "iv": 0.31, "delta": 0.4 if r == "C" else -0.4, "gamma": 0.02,
                 "theta": -0.05, "vega": 0.1, "und_price": 101.0}
                for w in window for k in w["strikes"] for r in ("C", "P")]
        rows[0]["vega"] = float("nan")                      # must leave as JSON null
        return {"symbol": symbol, "spot": 101.0, "mdt": quote_mdt, "rows": rows,
                "requested": len(rows), "filled": len(rows), "ms": 5}

    async def daily_bars(ib, symbol, duration="2 Y"):
        calls["daily_bars"].append(duration)
        return [{"on": f"d{i:03d}", "open": 1, "high": 2, "low": 0.5, "close": 1.5,
                 "volume": 1000} for i in range(300)]

    async def iv_history(ib, symbol, duration="1 Y"):
        calls["iv_history"].append(duration)
        return [{"on": f"d{i:03d}", "iv": 30.0 + i / 100} for i in range(280)]

    async def account(ib):
        calls["account"] += 1
        return {"net_liquidation": 125000.0, "currency": "USD"}

    def reset_mdt():
        calls["reset_mdt"] += 1

    def clear_cache():
        calls["clear_cache"] += 1

    mod = types.SimpleNamespace(VERSION="2.0", MDT_NAMES={1: "live", 2: "frozen", 3: "delayed",
                                                          4: "delayed_frozen"},
                                chain_defs=chain_defs, spot=spot_fn, plan=plan, quote=quote,
                                daily_bars=daily_bars, iv_history=iv_history, account=account,
                                reset_mdt=reset_mdt, clear_cache=clear_cache)
    if with_fetch:
        async def fetch(ib, symbol, spec=None, *, max_lines=60, **kw):
            calls["fetch"].append({"symbol": symbol, "spec": dict(spec or {}), "max_lines": max_lines})
            return {"symbol": symbol, "spot": spot, "mdt": quote_mdt, "rows": [], "requested": 0,
                    "filled": 0, "ms": 1, "spot_mdt": spot_mdt, "n_expiries": 0}
        mod.fetch = fetch
    return mod, calls


@pytest.fixture
def served(bridge):
    srv = bridge.make_server(0)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    port = srv.server_address[1]
    try:
        yield bridge, port
    finally:
        srv.shutdown()
        srv.server_close()


class Resp(types.SimpleNamespace):
    def json(self):
        return json.loads(self.body.decode("utf-8"))

    @property
    def text(self):
        return self.body.decode("utf-8")


def call(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        return Resp(status=r.status, headers=r.headers, body=r.read())
    finally:
        c.close()


def wait_until(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


# ───────────────────────────────────────────── config file

def test_default_config_path_windows_and_elsewhere(bridge, tmp_path):
    win = bridge.default_config_path(environ={"APPDATA": str(tmp_path)}, platform="win32")
    assert win == tmp_path / "TradeHunter" / "connector.json"
    nix = bridge.default_config_path(environ={}, platform="linux", home=tmp_path)
    assert nix == tmp_path / ".config" / "tradehunter" / "connector.json"
    # Windows without APPDATA falls back to the home folder form
    assert bridge.default_config_path(environ={}, platform="win32", home=tmp_path) == nix


def test_load_missing_gives_the_defaults(bridge, tmp_path):
    conf, problems = bridge.load_config(tmp_path / "nope.json")
    assert problems == []
    assert conf == {"tws_host": "127.0.0.1", "tws_port": 7496, "client_id": 86,
                    "max_lines": 40, "allowed_origins": []}
    conf["allowed_origins"].append("x")                       # a copy, not the module's list
    assert bridge.DEFAULT_CONFIG["allowed_origins"] == []


def test_save_then_load_round_trip(bridge, tmp_path):
    p = tmp_path / "sub" / "connector.json"
    out = bridge.save_config({"tws_host": "192.168.1.20", "tws_port": "4002", "client_id": 90,
                              "max_lines": 60, "allowed_origins": "https://MY-box:8443/\n\n"}, p)
    assert out == p and p.exists() and not p.with_name("connector.json.tmp").exists()
    conf, problems = bridge.load_config(p)
    assert problems == []
    assert conf == {"tws_host": "192.168.1.20", "tws_port": 4002, "client_id": 90,
                    "max_lines": 60, "allowed_origins": ["https://my-box:8443"]}


def test_load_corrupt_file_falls_back_with_a_problem(bridge, tmp_path):
    p = tmp_path / "connector.json"
    p.write_text("{not json", encoding="utf-8")
    conf, problems = bridge.load_config(p)
    assert conf["tws_port"] == 7496 and len(problems) == 1 and "could not be read" in problems[0]


def test_load_bad_field_keeps_its_default_and_the_good_ones(bridge, tmp_path):
    p = tmp_path / "connector.json"
    p.write_text('﻿{"tws_port": "abc", "client_id": 91, "junk": 1}', encoding="utf-8")  # BOM ok
    conf, problems = bridge.load_config(p)
    assert conf["tws_port"] == 7496 and conf["client_id"] == 91 and "junk" not in conf
    assert len(problems) == 1 and "TWS port" in problems[0]


@pytest.mark.parametrize("data, needle", [
    ({"tws_port": 0}, "TWS port"),
    ({"tws_port": 70000}, "TWS port"),
    ({"tws_port": True}, "TWS port"),
    ({"tws_port": 7496.5}, "TWS port"),
    ({"client_id": -1}, "Client ID"),
    ({"max_lines": 4}, "Market-data lines"),
    ({"max_lines": 201}, "Market-data lines"),
    ({"tws_host": "bad host"}, "TWS host"),
    ({"tws_host": ""}, "TWS host"),
    ({"allowed_origins": ["https://x.example/path"]}, "Not a web origin"),
    ({"allowed_origins": ["ftp://x.example"]}, "Not a web origin"),
    ({"allowed_origins": 5}, "must be a list"),
    ({"allowed_origins": [f"https://h{i}.example" for i in range(21)]}, "At most"),
])
def test_validate_rejects(bridge, data, needle):
    clean, errors = bridge.validate_config(data)
    assert len(errors) == 1 and needle in errors[0]
    assert clean == bridge.validate_config({})[0]            # bad field keeps the base value


def test_validate_partial_update_merges_over_base(bridge):
    base = {"tws_host": "10.0.0.5", "tws_port": 4001, "client_id": 87, "max_lines": 30,
            "allowed_origins": ["https://a.example"]}
    clean, errors = bridge.validate_config({"tws_port": "7497"}, base=base)
    assert errors == [] and clean == dict(base, tws_port=7497)
    clean, errors = bridge.validate_config({"allowed_origins": "https://B.example, http://c.example:8080"})
    assert clean["allowed_origins"] == ["https://b.example", "http://c.example:8080"]
    assert bridge.validate_config([1, 2])[1] == ["Settings must be a JSON object."]


def test_save_refuses_invalid(bridge, tmp_path):
    with pytest.raises(ValueError):
        bridge.save_config({"tws_port": -5}, tmp_path / "c.json")
    assert not (tmp_path / "c.json").exists()


def test_cli_overrides_win_for_the_run(bridge):
    conf = dict(bridge.DEFAULT_CONFIG, allowed_origins=["https://a.example"])
    bridge.RUN["cli_origins"] = ["https://cli.example"]
    bridge.apply_config(conf, overrides={"port": 4002, "client_id": 99})
    assert (bridge.CFG["host"], bridge.CFG["port"], bridge.CFG["client_id"],
            bridge.CFG["max_lines"]) == ("127.0.0.1", 4002, 99, 40)
    assert bridge.CFG["allowed"] == ["https://a.example", "https://cli.example"]


# ───────────────────────────────────────────── origin / host / same-origin rules

@pytest.mark.parametrize("origin, ok", [
    ("https://app.tradehunter.net", True),
    ("https://tradehunter.net", True),
    ("https://beta.app.tradehunter.net", True),
    ("http://app.tradehunter.net", False),            # plain HTTP on the real domain
    ("https://tradehunter.net.evil.com", False),
    ("https://eviltradehunter.net", False),
    ("http://127.0.0.1:8000", True),                  # the web app in development
    ("http://localhost:8099", True),
    ("http://127.0.0.1:8010", True),
    # any OTHER local port is another program on the member's PC, not TradeHunter:
    # it needs an explicit extra origin (review: the 2.0 blanket loopback rule)
    ("http://localhost:5173", False),
    ("http://127.0.0.1", False),
    ("http://127.0.0.1:7999", False),
    ("http://127.0.0.1:8100", False),
    ("http://127.0.0.1:9224", False),
    ("https://127.0.0.1:8000", False),
    ("http://127.0.0.1.evil.com:8000", False),
    ("https://evil.example", False),
    ("null", False),
    ("", False),
    (None, False),
    ("http://[bad", False),
])
def test_origin_allowed(bridge, origin, ok):
    assert bridge.origin_allowed(origin) is ok


def test_origin_allowed_configured_extra(bridge):
    assert not bridge.origin_allowed("https://my-box:8443")
    bridge.apply_config(dict(bridge.DEFAULT_CONFIG, allowed_origins=["https://my-box:8443"]))
    assert bridge.origin_allowed("https://my-box:8443")
    # a local dev server outside 8000-8099 is allowed only as an explicit extra
    assert not bridge.origin_allowed("http://localhost:5173")
    bridge.apply_config(dict(bridge.DEFAULT_CONFIG, allowed_origins=["http://localhost:5173"]))
    assert bridge.origin_allowed("http://localhost:5173")


@pytest.mark.parametrize("host, ok", [
    ("127.0.0.1:9224", True), ("localhost:9224", True), ("LOCALHOST", True),
    ("[::1]:9224", True), (None, True),
    ("evil.example:9224", False), ("127.0.0.1.evil.example", False),
])
def test_host_allowed(bridge, host, ok):
    assert bridge.host_allowed(host) is ok


# ───────────────────────────────────────────── /health

def test_health_shape_and_speed_from_cached_state(served):
    bridge, port = served
    bridge.th_ibkr = fake_th()[0]
    bridge.STATE.set(tws_connected=True, mdt="delayed", account_type="paper", error=None)
    t0 = time.perf_counter()
    r = call(port, "GET", "/health", headers={"Origin": SITE})
    elapsed = time.perf_counter() - t0
    assert r.status == 200 and elapsed < 0.2
    h = r.json()
    assert {"ok", "version", "tws_connected", "tws", "client_id", "mdt", "account_type",
            "error"} <= set(h)
    assert h == {"ok": True, "version": "2.0", "tws_connected": True, "connected": True,
                 "tws": "127.0.0.1:7496", "client_id": 86, "mdt": "delayed",
                 "account_type": "paper", "error": None}
    assert r.headers["Access-Control-Allow-Origin"] == SITE
    assert "2.0" in r.headers["Server"]
    assert bridge._worker is None                       # /health never starts the IB worker


def test_health_not_connected_reports_the_error(served):
    bridge, port = served
    bridge.th_ibkr = fake_th()[0]
    bridge.STATE.set(tws_connected=False, error="Cannot reach TWS on 127.0.0.1:7496.")
    h = call(port, "GET", "/health").json()
    assert h["tws_connected"] is False and "Cannot reach TWS" in h["error"]


def test_health_mentions_a_missing_th_ibkr(served):
    bridge, port = served
    bridge.th_ibkr = None
    assert "th_ibkr.py" in call(port, "GET", "/health").json()["error"]


def test_refuses_foreign_origin_and_foreign_host(served):
    _, port = served
    r = call(port, "GET", "/health", headers={"Origin": "https://evil.example"})
    assert r.status == 403 and "Access-Control-Allow-Origin" not in r.headers
    r = call(port, "GET", "/health", headers={"Host": "evil.example:9224"})
    assert r.status == 403


def test_cross_site_requests_without_an_origin_do_no_work(live):
    """Review #0: an <img> / no-cors fetch from another site carries no Origin, but
    Sec-Fetch-Site says cross-site. It used to run the TWS read in full (the attacker
    only could not see the answer) - now it is refused before any work."""
    bridge, port, calls = live
    hostile = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "image"}
    spec = quote(json.dumps({"max_side": 400, "max_dte": 4000, "max_weekly_dte": 4000, "n": 1}))
    for path in ("/chain2?symbol=SPY&spec=" + spec, "/underlying?symbol=SPY", "/account",
                 "/scan?iv_rank=30", "/chain?symbol=SPY", "/iv?symbol=SPY", "/health"):
        r = call(port, "GET", path, headers=hostile)
        assert r.status == 403, path
        assert "Access-Control-Allow-Origin" not in r.headers
    r = call(port, "GET", "/chain2?symbol=SPY&spec=" + spec, headers={"Sec-Fetch-Site": "same-site"})
    assert r.status == 403                                     # another local site: same rule
    assert calls["chain_defs"] == 0 and calls["quote"] == [] and calls["spot"] == 0
    assert calls["daily_bars"] == [] and calls["account"] == 0

    # what stays open: the settings page (the Options page links to it), same-origin
    # polls, a direct visit / curl, and the allow-listed page's CORS fetch
    nav = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
    assert call(port, "GET", "/", headers=nav).status == 200
    assert call(port, "GET", "/health", headers={"Sec-Fetch-Site": "same-origin"}).status == 200
    assert call(port, "GET", "/health", headers={"Sec-Fetch-Site": "none"}).status == 200
    assert call(port, "GET", "/health").status == 200
    r = call(port, "GET", "/account", headers={"Origin": SITE, "Sec-Fetch-Site": "cross-site",
                                               "Sec-Fetch-Mode": "cors"})
    assert r.status == 200 and r.json()["ok"] and calls["account"] == 1


def test_preflight_private_network_and_settings_refused(served):
    _, port = served
    r = call(port, "OPTIONS", "/chain2", headers={
        "Origin": SITE, "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Private-Network": "true"})
    assert r.status == 204
    assert r.headers["Access-Control-Allow-Origin"] == SITE
    assert r.headers["Access-Control-Allow-Private-Network"] == "true"
    r = call(port, "OPTIONS", "/settings", headers={
        "Origin": SITE, "Access-Control-Request-Method": "POST"})
    assert r.status == 403 and "Access-Control-Allow-Origin" not in r.headers
    r = call(port, "OPTIONS", "/health", headers={"Origin": "https://evil.example"})
    assert r.status == 403


# ───────────────────────────────────────────── settings page + POST /settings

def test_settings_page_is_self_contained(served):
    bridge, port = served
    r = call(port, "GET", "/", headers={"Origin": SITE})
    assert r.status == 200 and r.headers["Content-Type"].startswith("text/html")
    assert "Access-Control-Allow-Origin" not in r.headers           # same-origin page
    page = r.text
    for preset in ('data-port="7496"', 'data-port="7497"', 'data-port="4001"', 'data-port="4002"'):
        assert preset in page
    assert "Save &amp; reconnect" in page and 'id="statusText"' in page
    assert "Enable ActiveX and Socket Clients" in page
    assert not re.search(r"<script[^>]+src=|<link[^>]+href=|@import|cdn", page, re.I)
    assert str(bridge.config_path()) in page or "connector.json" in page


def test_settings_page_escapes_values(served, tmp_path):
    bridge, port = served
    bridge.RUN["load_problems"] = ['<img src=x onerror="alert(1)">']
    page = call(port, "GET", "/").text
    assert "<img src=x" not in page and "&lt;img src=x" in page


def _own(port):
    return {"Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json"}


def test_post_settings_same_origin_saves_and_reconnects(served):
    bridge, port = served
    w = types.SimpleNamespace(n=0)
    w.request_reconnect = lambda: setattr(w, "n", w.n + 1)
    bridge._worker = w
    bridge.RUN["overrides"] = {"port": 4002}
    with bridge._cache_lock:
        bridge._cache["x"] = (time.time() + 60, {"stale": True})
    body = json.dumps({"tws_host": "127.0.0.1", "tws_port": "7497", "client_id": "88",
                       "max_lines": "30", "allowed_origins": ""})
    r = call(port, "POST", "/settings", body=body, headers=_own(port))
    assert r.status == 200, r.text
    j = r.json()
    assert j["ok"] is True and j["reconnecting"] is True
    assert j["config"] == {"tws_host": "127.0.0.1", "tws_port": 7497, "client_id": 88,
                           "max_lines": 30, "allowed_origins": []}
    saved = json.loads(bridge.config_path().read_text(encoding="utf-8"))
    assert saved == j["config"]
    assert (bridge.CFG["port"], bridge.CFG["client_id"], bridge.CFG["max_lines"]) == (7497, 88, 30)
    assert bridge.RUN["overrides"] == {}                 # a Save replaces the CLI values
    assert w.n == 1 and bridge._cache == {}
    assert "Access-Control-Allow-Origin" not in r.headers


def test_post_settings_localhost_origin_and_no_origin_are_same_origin(served):
    bridge, port = served
    r = call(port, "POST", "/settings", body='{"tws_port": 4001}',
             headers={"Origin": f"http://localhost:{port}", "Content-Type": "application/json"})
    assert r.status == 200 and r.json()["config"]["tws_port"] == 4001
    r = call(port, "POST", "/settings", body='{"tws_port": 4002}',     # curl / a script
             headers={"Content-Type": "application/json"})
    assert r.status == 200 and r.json()["config"]["tws_port"] == 4002


@pytest.mark.parametrize("headers", [
    {"Origin": SITE},                                          # allow-listed for data, not settings
    {"Origin": "https://evil.example"},
    {"Origin": "http://127.0.0.1:8000"},                       # another local port
    {"Origin": "null"},
    {"Sec-Fetch-Site": "cross-site"},                          # no Origin, but the browser says cross-site
    {"Referer": "https://evil.example/page"},
])
def test_post_settings_cross_origin_refused(served, headers):
    bridge, port = served
    bridge.save_config({"tws_port": 7496}, bridge.config_path())
    before = bridge.config_path().read_text(encoding="utf-8")
    h = dict(headers, **{"Content-Type": "application/json"})
    r = call(port, "POST", "/settings", body='{"tws_port": 4002}', headers=h)
    assert r.status == 403
    assert "Access-Control-Allow-Origin" not in r.headers
    assert bridge.config_path().read_text(encoding="utf-8") == before
    assert bridge.CFG["port"] == 7496


def test_post_settings_foreign_host_refused(served):
    bridge, port = served
    h = dict(_own(port), Host="evil.example:9224")
    r = call(port, "POST", "/settings", body='{"tws_port": 4002}', headers=h)
    assert r.status == 403 and not bridge.config_path().exists()


def test_post_settings_invalid_values_400_and_nothing_written(served):
    bridge, port = served
    r = call(port, "POST", "/settings", body='{"tws_port": 99999, "max_lines": 1}', headers=_own(port))
    assert r.status == 400
    j = r.json()
    assert j["ok"] is False and len(j["errors"]) == 2
    assert not bridge.config_path().exists()
    r = call(port, "POST", "/settings", body="{nope", headers=_own(port))
    assert r.status == 400


def test_post_settings_form_redirects(served):
    bridge, port = served
    body = urlencode({"tws_host": "127.0.0.1", "tws_port": "4002", "client_id": "86",
                      "max_lines": "40", "allowed_origins": "https://my-box:8443"})
    h = {"Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/x-www-form-urlencoded"}
    r = call(port, "POST", "/settings", body=body, headers=h)
    assert r.status == 303 and r.headers["Location"] == "/?saved=1"
    conf, _ = bridge.load_config(bridge.config_path())
    assert conf["tws_port"] == 4002 and conf["allowed_origins"] == ["https://my-box:8443"]
    assert "Saved." in call(port, "GET", "/?saved=1").text
    bad = urlencode({"tws_port": "x"})
    r = call(port, "POST", "/settings", body=bad, headers=h)
    assert r.status == 400 and r.headers["Content-Type"].startswith("text/html")
    assert "TWS port must be" in r.text


def test_post_other_path_404(served):
    _, port = served
    assert call(port, "POST", "/health", body="{}", headers=_own(port)).status == 404


# ───────────────────────────────────────────── /chain2, /underlying, /account

@pytest.fixture
def live(served):
    """A real worker loop with a connected fake IB (no keeper) and a fake th_ibkr."""
    bridge, port = served
    bridge._worker = bridge._Worker(ib_factory=connected_ib, keep=False)
    th, calls = fake_th()
    bridge.th_ibkr = th
    return bridge, port, calls


def _chain2(port, symbol, spec, origin=SITE):
    q = "/chain2?symbol=" + quote(symbol) + ("&spec=" + quote(json.dumps(spec)) if spec is not None else "")
    return call(port, "GET", q, headers={"Origin": origin})


def test_chain2_routes_spec_to_plan_and_quote(live):
    bridge, port, calls = live
    bridge.CFG["max_lines"] = 33
    spec = {"symbol": "LRCX", "spot": 95.0, "iv_hint": 0.46, "expiries": None,
            "max_weekly_dte": 63, "max_dte": 1100, "sigma_k": 2.5, "min_side": 6, "max_side": 40}
    r = _chain2(port, "lrcx", spec)
    assert r.status == 200 and r.headers["Access-Control-Allow-Origin"] == SITE
    j = r.json()
    assert j["ok"] is True and j["symbol"] == "LRCX" and j["mdt"] == "live"
    assert j["connector_version"] == "2.0" and len(j["rows"]) == 12
    assert j["rows"][0]["vega"] is None                       # NaN went out as null
    assert calls["plan"] == [{"spot": 101.0, "iv_hint": 0.46, "max_weekly_dte": 63,
                              "max_dte": 1100, "sigma_k": 2.5, "min_side": 6, "max_side": 40}]
    assert calls["quote"][0]["max_lines"] == 33 and calls["quote"][0]["symbol"] == "LRCX"
    # the same (symbol, spec) inside 20 s is one TWS read
    assert _chain2(port, "LRCX", spec).json()["rows"] == j["rows"]
    assert len(calls["quote"]) == 1
    # a different window is a new read
    _chain2(port, "LRCX", dict(spec, expiries=["2026-12-18"]))
    assert len(calls["quote"]) == 2 and calls["plan"][1]["expiries"] == ["2026-12-18"]
    # the status /health reports picks up the chain's data type
    assert call(port, "GET", "/health").json()["mdt"] == "live"


def test_chain2_uses_th_fetch_when_present(live):
    bridge, port, _ = live
    th, calls = fake_th(with_fetch=True, quote_mdt="frozen")
    bridge.th_ibkr = th
    bridge.CFG["max_lines"] = 25
    spec = {"symbol": "MSFT", "spot": 410.0, "iv_hint": 0.3, "expiries": ["2026-11-20"]}
    j = _chain2(port, "msft", spec).json()
    assert j["ok"] is True and j["mdt"] == "frozen" and j["connector_version"] == "2.0"
    assert calls["fetch"] == [{"symbol": "MSFT", "max_lines": 25,
                               "spec": {"spot": 410.0, "iv_hint": 0.3, "expiries": ["2026-11-20"]}}]
    assert calls["quote"] == [] and calls["plan"] == []          # fetch did the whole read


def test_real_th_ibkr_has_what_the_connector_calls():
    """Contract check against Part B's module (skipped while it does not import)."""
    import inspect
    import sys
    sys.path.insert(0, str(BRIDGE_DIR))
    try:
        th = importlib.import_module("th_ibkr")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"th_ibkr not importable: {exc}")
    finally:
        sys.path.remove(str(BRIDGE_DIR))
    assert th.MDT_NAMES == {1: "live", 2: "frozen", 3: "delayed", 4: "delayed_frozen"}
    for name in ("chain_defs", "spot", "quote", "daily_bars", "iv_history", "account"):
        assert inspect.iscoroutinefunction(getattr(th, name)), name
    assert "max_lines" in inspect.signature(th.quote).parameters
    plan_params = inspect.signature(th.plan).parameters
    assert "spot" in plan_params
    spec = importlib.util.spec_from_file_location("ibkr_bridge_contract", BRIDGE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert all(k in plan_params for k in mod.PLAN_KEYS)
    assert "max_expiries" in plan_params and "deadline" in inspect.signature(th.quote).parameters
    if hasattr(th, "fetch"):
        assert inspect.iscoroutinefunction(th.fetch)
        assert "max_lines" in inspect.signature(th.fetch).parameters
        assert "deadline" in inspect.signature(th.fetch).parameters


def test_chain2_falls_back_to_the_spec_spot(live):
    bridge, port, _ = live
    th, calls = fake_th(spot_fail=True, quote_mdt=3)
    bridge.th_ibkr = th
    j = _chain2(port, "KO", {"spot": 62.5}).json()
    assert j["ok"] and calls["plan"][0]["spot"] == 62.5 and j["mdt"] == "delayed"
    j = _chain2(port, "PEP", {}).json()                         # no spot anywhere
    assert j["ok"] is False and "No price for PEP" in j["error"]


def test_chain2_bad_input_is_400(live):
    _, port, calls = live
    assert _chain2(port, "", {}).status == 400
    assert _chain2(port, "LRCX;rm", {}).status == 400
    assert _chain2(port, "LRCX", {"sigma_k": -1}).status == 400
    assert _chain2(port, "LRCX", {"expiries": ["soon"]}).status == 400
    assert _chain2(port, "LRCX", {"min_side": "6"}).status == 400
    r = call(port, "GET", "/chain2?symbol=LRCX&spec=" + quote("[1]"), headers={"Origin": SITE})
    assert r.status == 400 and r.json()["ok"] is False
    assert calls["quote"] == []


def test_chain2_timeout_answers_with_an_error(live):
    bridge, port, _ = live
    th, _ = fake_th(quote_sleep=3.0)
    bridge.th_ibkr = th
    bridge.CHAIN2_TIMEOUT = 0.3
    t0 = time.monotonic()
    j = _chain2(port, "SLOW", {}).json()
    assert time.monotonic() - t0 < 2.5
    assert j["ok"] is False and "did not finish" in j["error"]
    assert "lower" not in j["error"]           # fewer lines would only make a read slower


def test_chain2_member_chunk_deadline_and_partial(live):
    """#16/#19: the member's chunk (max_expiries / max_side) reaches plan, the read gets
    a deadline CHAIN2_MARGIN under the connector's own limit, and a partial read comes
    back as data (not a timeout with nothing)."""
    bridge, port, calls = live
    assert (bridge.CHAIN2_TIMEOUT, bridge.CHAIN2_MARGIN) == (150.0, 15.0)
    spec = {"symbol": "NVDA", "spot": 180.0, "iv_hint": 0.45, "expiries": None, "max_weekly_dte": 63,
            "max_dte": 1100, "sigma_k": 2.5, "min_side": 6, "max_side": 25, "max_expiries": 6}
    j = _chain2(port, "NVDA", spec).json()
    assert j["ok"] is True and j["partial"] is False          # always in the answer
    assert calls["plan"][0]["max_expiries"] == 6 and calls["plan"][0]["max_side"] == 25
    assert 130.0 < calls["quote"][0]["deadline"] <= 135.0

    seen = {}

    async def fetch(ib, symbol, spec=None, *, max_lines=60, deadline=None, **kw):
        seen.update(spec=dict(spec or {}), deadline=deadline)
        return {"symbol": symbol, "spot": 180.0, "mdt": "live", "partial": True, "ms": 1,
                "rows": [{"expiry": "2026-11-20", "right": "C", "strike": 180.0, "bid": 9.0,
                          "ask": 9.2, "mid": 9.1, "delta": 0.52}], "requested": 300, "filled": 1}

    bridge.th_ibkr.fetch = fetch
    j = _chain2(port, "NVDA", dict(spec, max_expiries=5)).json()
    assert j["ok"] is True and j["partial"] is True and len(j["rows"]) == 1
    assert seen["spec"]["max_expiries"] == 5 and seen["spec"]["max_side"] == 25
    assert 130.0 < seen["deadline"] <= 135.0
    assert _chain2(port, "NVDA", dict(spec, max_expiries=81)).status == 400
    assert _chain2(port, "NVDA", dict(spec, max_expiries="6")).status == 400


def test_chain2_busy_after_waiting_for_another_read(live):
    bridge, port, _ = live
    th, _ = fake_th(quote_sleep=2.5)
    bridge.th_ibkr = th
    bridge.CHAIN2_TIMEOUT, bridge.CHAIN2_MARGIN, bridge.CHAIN2_MIN_LEFT = 5.0, 1.0, 3.0
    out = {}

    def first():
        out["a"] = _chain2(port, "AAA", {}).json()

    t = threading.Thread(target=first)
    t.start()
    time.sleep(0.3)                                         # AAA holds the one quote slot
    out["b"] = _chain2(port, "BBB", {}).json()              # waits ~2.2 s: 1.8 s left < 3 s
    t.join(10)
    assert out["a"]["ok"] is True
    assert out["b"]["ok"] is False and "busy" in out["b"]["error"]
    assert out["b"].get("busy") is True and out["a"].get("busy") is None    # the shared decision's flag


def test_chain2_without_th_ibkr(live):
    bridge, port, _ = live
    bridge.th_ibkr = None
    j = _chain2(port, "LRCX", {}).json()
    assert j["ok"] is False and "th_ibkr.py" in j["error"]


def test_underlying_caches_history_per_day(live):
    bridge, port, calls = live
    r = call(port, "GET", "/underlying?symbol=nvda", headers={"Origin": SITE})
    j = r.json()
    assert r.status == 200 and j["ok"] and j["symbol"] == "NVDA"
    assert j["spot"] == 101.0 and j["mdt"] == "live"
    assert len(j["bars"]) == 260 and j["bars"][-1]["on"] == "d299"      # the last 260
    assert len(j["iv_series"]) == 260 and j["iv_series"][-1]["on"] == "d279"
    assert calls["daily_bars"] == ["1 Y"] and calls["iv_history"] == ["1 Y"]
    call(port, "GET", "/underlying?symbol=NVDA", headers={"Origin": SITE})
    assert len(calls["daily_bars"]) == 1 and calls["spot"] == 1


def test_underlying_empty_history_is_an_error_and_not_cached(live):
    """#18 (connector side): ib_insync answers a failed historical request with an
    EMPTY list. That must not be cached for the day as the symbol's history."""
    bridge, port, calls = live
    th = bridge.th_ibkr
    real = th.daily_bars
    n = {"bars": 0}

    async def flaky(ib, symbol, duration="2 Y"):
        n["bars"] += 1
        return [] if n["bars"] == 1 else await real(ib, symbol, duration)

    th.daily_bars = flaky
    j = call(port, "GET", "/underlying?symbol=CRWD", headers={"Origin": SITE}).json()
    assert j["ok"] is False and "no daily bars" in j["error"]
    assert calls["iv_history"] == []                    # no second request wasted
    j = call(port, "GET", "/underlying?symbol=CRWD", headers={"Origin": SITE}).json()
    assert j["ok"] is True and len(j["bars"]) == 260 and n["bars"] == 2


def test_account(live):
    _, port, calls = live
    j = call(port, "GET", "/account", headers={"Origin": SITE}).json()
    assert j == {"ok": True, "net_liquidation": 125000.0, "currency": "USD"}


def test_legacy_endpoints_still_routed(live):
    _, port, _ = live
    for path in ("/chain", "/iv"):
        j = call(port, "GET", path, headers={"Origin": SITE}).json()
        assert j == {"ok": False, "error": "symbol is required"}            # the 1.x reply
    assert call(port, "GET", "/nope", headers={"Origin": SITE}).status == 404


# ───────────────────────────────────────────── the connection keeper

def test_keeper_connects_read_only_and_reconnects_on_save(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "KEEP_TICK", 0.02)
    th, th_calls = fake_th(spot_mdt="delayed")
    bridge.th_ibkr = th
    ibs = []

    def factory():
        ibs.append(FakeIB(accounts=("DU999",)))
        return ibs[-1]

    bridge._worker = bridge._Worker(ib_factory=factory, keep=True)
    assert wait_until(lambda: bridge.STATE.snapshot()["tws_connected"])
    assert ibs[0].calls == [("127.0.0.1", 7496, 86, True)]                   # readonly=True
    assert wait_until(lambda: bridge.STATE.snapshot()["mdt"] == "delayed")    # the SPY probe
    snap = bridge.STATE.snapshot()
    assert snap["account_type"] == "paper" and snap["error"] is None

    conf, errors = bridge.update_settings({"tws_port": 4001})
    assert errors == []
    assert wait_until(lambda: len(ibs[0].calls) == 2 and ibs[0].connected)
    assert ibs[0].calls[1] == ("127.0.0.1", 4001, 86, True)
    assert th_calls["reset_mdt"] == 1 and th_calls["clear_cache"] == 1   # another login, fresh verdicts


def test_keeper_records_an_unreachable_tws(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "KEEP_TICK", 0.02)
    bridge._worker = bridge._Worker(
        ib_factory=lambda: FakeIB(fail=ConnectionRefusedError("refused")), keep=True)
    assert wait_until(lambda: bridge.STATE.snapshot()["error"] is not None)
    h = bridge.health_payload()
    assert h["tws_connected"] is False and "Cannot reach TWS on 127.0.0.1:7496" in h["error"]


def test_live_account_type(bridge):
    assert bridge._account_type(types.SimpleNamespace(managedAccounts=lambda: ["U1234567"])) == "live"
    assert bridge._account_type(types.SimpleNamespace(managedAccounts=lambda: [])) is None


# ───────────────────────────────────────────── the installer

def test_installer_and_requirements():
    ps = (BRIDGE_DIR / "install_bridge.ps1").read_bytes()
    text = ps.decode("ascii")                                  # ASCII only (PS 5.1, no BOM)
    for needle in ("py -3.12", "winget install -e --id Python.Python.3.12", "Read-Host",
                   "pip install --user", "-Uninstall", "tradehunter", "Startup",
                   "http://127.0.0.1:9224/"):
        assert needle in text
    assert "&&" not in text                                   # PowerShell 5.1 has no &&
    reqs = [ln.strip() for ln in (BRIDGE_DIR / "requirements.txt").read_text(encoding="ascii").splitlines()
            if ln.strip() and not ln.strip().startswith("#")]
    assert len(reqs) == 1 and reqs[0].startswith("ib_insync")
    (BRIDGE_DIR / "start_ibkr_bridge.bat").read_bytes().decode("ascii")
