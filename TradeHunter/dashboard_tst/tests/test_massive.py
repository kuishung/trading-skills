"""The Massive REST client (``app/services/massive.py``, OPTIONS_V2_DESIGN.md §13.2).

No network: every test hands the client an ``httpx.Client`` on an ``httpx.MockTransport``
(the real request path - URL building, params, headers - with a fake server behind it)
and a fake clock whose ``sleep`` advances ``now``, so the pacing and the back-off are
checked to the second without waiting. The key below is a made-up test value.
"""
from __future__ import annotations

import datetime as _dt

import httpx
import pytest

from app.services import massive

KEY = "unit-test-key-0123456789abcdef"     # not a real key
BASE = "https://api.massive.test"


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(round(s, 6))
        self.t += s


class Server:
    """A scripted fake: ``replies`` is a list of (status, json body, headers) or an
    exception to raise, consumed in order (the last one repeats)."""

    def __init__(self, replies=None, handler=None):
        self.replies = list(replies or [])
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.handler is not None:
            return self.handler(request)
        rep = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(rep, Exception):
            raise rep
        status, body, headers = (rep + (None,))[:3] if len(rep) == 2 else rep
        return httpx.Response(status, json=body, headers=headers or {})


def make_client(server, clock=None, **kw):
    clock = clock or FakeClock()
    http = httpx.Client(transport=httpx.MockTransport(server))
    kw.setdefault("api_key", KEY)
    kw.setdefault("base_url", BASE)
    return massive.Client(http=http, sleep=clock.sleep, now=clock.now, **kw), clock


def ns(dt: _dt.datetime) -> int:
    return int(dt.replace(tzinfo=_dt.timezone.utc).timestamp()) * 1_000_000_000


def result(exp="2026-11-20", ct="call", strike=100.0, *, iv=0.30, greeks=True, quote=None,
           und_price=None, day_updated=None, und_updated=None, oi=1200, volume=35, close=4.1):
    day = {"last_updated": day_updated if day_updated is not None
           else ns(_dt.datetime(2026, 10, 5, 17, 45))}
    if close is not None:
        day.update(change=0.1, change_percent=2.5, close=close, high=close + 0.2, low=close - 0.2,
                   open=close - 0.1, previous_close=close - 0.1, vwap=close - 0.05)
    if volume is not None:
        day["volume"] = volume
    r = {"details": {"contract_type": ct, "exercise_style": "american", "expiration_date": exp,
                     "shares_per_contract": 100, "strike_price": strike,
                     "ticker": massive.option_ticker("SPY", exp, ct, strike)},
         "day": day,
         "underlying_asset": {"ticker": "SPY", "change_to_break_even": 1.0, "timeframe": "DELAYED"}}
    if close is not None:
        r["break_even_price"] = strike + close
    if oi is not None:
        r["open_interest"] = oi
    if iv is not None:
        r["implied_volatility"] = iv
    if greeks:
        r["greeks"] = {"delta": 0.52 if ct == "call" else -0.48, "gamma": 0.03, "theta": -0.05,
                       "vega": 0.14}
    if quote is not None:
        r["last_quote"] = quote
    if und_price is not None:
        r["underlying_asset"]["price"] = und_price
        r["underlying_asset"]["last_updated"] = und_updated or ns(_dt.datetime(2026, 10, 5, 17, 44))
    return r


# ───────────────────────────────────────────── settings + tickers

def test_option_ticker_format():
    assert massive.option_ticker("SPY", "2025-12-19", "C", 650) == "O:SPY251219C00650000"
    assert massive.option_ticker("aapl", _dt.date(2026, 1, 16), "put", 72.5) == "O:AAPL260116P00072500"
    assert massive.option_ticker("F", "2026-03-20", "call", 12.5) == "O:F260320C00012500"
    assert massive.option_ticker("BRK.B", "2026-06-18", "P", 480) == "O:BRKB260618P00480000"
    with pytest.raises(ValueError):
        massive.option_ticker("SPY", "2025-12-19", "X", 650)
    with pytest.raises(ValueError):
        massive.option_ticker("SPY", "not a date", "C", 650)


def test_api_key_and_base_url_from_env(monkeypatch):
    monkeypatch.setenv("TST_MASSIVE_API_KEY", "  abc  ")
    monkeypatch.setenv("TST_MASSIVE_BASE_URL", "http://127.0.0.1:9999/")
    assert massive.api_key() == "abc"
    assert massive.base_url() == "http://127.0.0.1:9999"
    monkeypatch.setenv("TST_MASSIVE_API_KEY", "   ")
    monkeypatch.delenv("TST_MASSIVE_BASE_URL")
    assert massive.api_key() is None
    assert massive.base_url() == "https://api.massive.com"
    monkeypatch.setenv("TST_MASSIVE_QUOTES", "1")
    assert massive.quotes_enabled() is True
    monkeypatch.setenv("TST_MASSIVE_QUOTES", "0")
    assert massive.quotes_enabled() is False


def test_ts_to_utc_magnitudes():
    want = _dt.datetime(2026, 10, 5, 17, 45, 0)
    assert massive.ts_to_utc(ns(want)) == want                      # ns (last_updated)
    assert massive.ts_to_utc(ns(want) // 1_000_000) == want         # ms (bar t)
    assert massive.ts_to_utc(ns(want) // 1_000) == want             # us
    assert massive.ts_to_utc(ns(want) // 1_000_000_000) == want     # s
    assert massive.ts_to_utc(1636520400000000000) == _dt.datetime(2021, 11, 10, 5, 0)
    assert massive.ts_to_utc(0) is None and massive.ts_to_utc(None) is None
    assert massive.ts_to_utc("junk") is None
    assert massive.ts_to_utc(ns(want)).tzinfo is None


# ───────────────────────────────────────────── the snapshot + pagination

def test_chain_snapshot_follows_next_url_with_the_key_only_in_the_header():
    nxt = BASE + "/v3/snapshot/options/SPY?cursor=YXA9MjUwJmFzPSZsaW1pdD0yNTA"
    server = Server([
        (200, {"status": "OK", "results": [result(strike=100.0), result(ct="put", strike=100.0)],
               "next_url": nxt}),
        (200, {"status": "OK", "results": [result(strike=105.0)]}),
    ])
    client, _ = make_client(server)
    out = client.chain_snapshot("spy", exp_gte="2026-10-05", exp_lte=_dt.date(2029, 10, 9),
                                strike_gte=30.0, strike_lte=300.5)
    assert out["symbol"] == "SPY"
    assert out["pages"] == 2 and len(out["rows"]) == 3
    assert [r["right"] for r in out["rows"]] == ["C", "P", "C"]
    assert len(server.requests) == 2
    first, second = server.requests
    assert first.url.path == "/v3/snapshot/options/SPY"
    q = dict(first.url.params)
    assert q == {"limit": "250", "expiration_date.gte": "2026-10-05",
                 "expiration_date.lte": "2029-10-09", "strike_price.gte": "30",
                 "strike_price.lte": "300.5"}
    assert str(second.url) == nxt                      # fetched exactly as given
    for req in server.requests:
        assert req.headers["Authorization"] == "Bearer " + KEY
        assert KEY not in str(req.url)
        assert "apikey" not in str(req.url).lower()
    assert client.requests == 2


def test_next_url_to_another_host_is_not_followed():
    server = Server([(200, {"results": [result()], "next_url": "https://evil.example/v3/x?cursor=1"})])
    client, _ = make_client(server)
    with pytest.raises(massive.MassiveError) as ei:
        client.chain_snapshot("SPY")
    assert ei.value.kind == "http"
    assert len(server.requests) == 1                    # the key never went to evil.example
    assert KEY not in str(ei.value)


def test_relative_next_url_resolves_on_the_configured_host():
    server = Server([(200, {"results": [result()], "next_url": "/v3/snapshot/options/SPY?cursor=Mg"}),
                     (200, {"results": [result(ct="put")]})])
    client, _ = make_client(server)
    out = client.chain_snapshot("SPY")
    assert out["pages"] == 2
    assert str(server.requests[1].url) == BASE + "/v3/snapshot/options/SPY?cursor=Mg"
    assert server.requests[1].headers["Authorization"] == "Bearer " + KEY


def test_snapshot_row_parsing_absent_parts_and_ns_stamps():
    day_t = _dt.datetime(2026, 10, 5, 17, 40, 5)
    q_t = _dt.datetime(2026, 10, 5, 17, 44, 59)
    und_t = _dt.datetime(2026, 10, 5, 17, 44, 30)
    server = Server([(200, {"results": [
        result(strike=60.0, greeks=False, iv=None, day_updated=ns(day_t)),          # deep ITM: no greeks / IV
        result(ct="put", strike=95.0, day_updated=ns(day_t),
               quote={"bid": 1.10, "ask": 1.25, "bid_size": 12, "ask_size": 30, "midpoint": 1.175,
                      "last_updated": ns(q_t), "timeframe": "DELAYED"}),
        result(strike=100.0, und_price=100.37, und_updated=ns(und_t), day_updated=ns(day_t)),
        {"details": {"contract_type": "call"}},                                       # unusable key
        result(ct="call", strike=110.0, day_updated=0, oi=None, volume=None, close=None),
    ]})])
    client, _ = make_client(server)
    out = client.chain_snapshot("SPY")
    rows = {(r["right"], r["strike"]): r for r in out["rows"]}
    assert len(out["rows"]) == 4

    itm = rows[("C", 60.0)]
    assert itm["delta"] is None and itm["gamma"] is None and itm["theta"] is None and itm["vega"] is None
    assert itm["iv"] is None
    assert itm["bid"] is None and itm["ask"] is None                 # Starter: no last_quote
    assert itm["bid_size"] is None and itm["ask_size"] is None
    assert itm["last_updated"] == day_t and itm["last_updated"].tzinfo is None
    assert itm["und_price"] is None

    put = rows[("P", 95.0)]
    assert (put["bid"], put["ask"], put["bid_size"], put["ask_size"]) == (1.10, 1.25, 12, 30)
    assert put["last_updated"] == q_t                                # the newer of quote / day
    assert put["iv"] == 0.30 and put["delta"] == -0.48
    assert put["oi"] == 1200 and put["volume"] == 35
    assert put["day_close"] == 4.1 and put["prev_close"] == pytest.approx(4.0)
    assert put["day_change_pct"] == 2.5
    assert put["expiry"] == "2026-11-20" and put["multiplier"] == 100
    assert put["ticker"] == "O:SPY261120P00095000"

    bare = rows[("C", 110.0)]
    assert bare["last_updated"] is None and bare["oi"] is None and bare["volume"] is None
    assert bare["day_close"] is None

    assert out["underlying_price"] == 100.37
    assert out["underlying_as_of"] == und_t
    assert out["as_of"] == q_t                                       # newest row stamp


def test_snapshot_without_underlying_price():
    server = Server([(200, {"results": [result(), result(ct="put")]})])
    client, _ = make_client(server)
    out = client.chain_snapshot("SPY")
    assert out["underlying_price"] is None and out["underlying_as_of"] is None
    assert out["pages"] == 1


# ───────────────────────────────────────────── errors

def test_429_retry_after_then_success():
    server = Server([(429, {"status": "ERROR"}, {"Retry-After": "3"}), (200, {"results": []})])
    client, clock = make_client(server)
    out = client.chain_snapshot("SPY")
    assert out["rows"] == [] and len(server.requests) == 2
    assert 3.0 in clock.sleeps


def test_429_backoff_doubles_then_rate_error():
    server = Server([(429, {"status": "ERROR", "error": "too many"})])
    client, clock = make_client(server)
    with pytest.raises(massive.MassiveError) as ei:
        client.chain_snapshot("SPY")
    assert ei.value.kind == "rate" and ei.value.status == 429
    assert [s for s in clock.sleeps if s >= 1] == [15.0, 30.0, 60.0, 120.0]
    assert len(server.requests) == 5


def test_retry_after_is_capped_at_two_minutes():
    server = Server([(429, {}, {"Retry-After": "3600"}), (200, {"results": []})])
    client, clock = make_client(server)
    client.chain_snapshot("SPY")
    assert max(clock.sleeps) == 120.0


@pytest.mark.parametrize("status,kind,words", [
    (401, "auth", "rejected the API key"),
    (403, "plan", "does not include the options chain snapshot"),
    (404, "http", "HTTP 404"),
    (400, "http", "HTTP 400"),
])
def test_status_kinds(status, kind, words):
    server = Server([(status, {"status": "ERROR", "error": "nope"})])
    client, _ = make_client(server)
    with pytest.raises(massive.MassiveError) as ei:
        client.chain_snapshot("SPY")
    assert ei.value.kind == kind and ei.value.status == status
    assert words in str(ei.value)
    assert len(server.requests) == 1                    # 4xx is not retried


def test_403_on_stock_bars_names_that_endpoint():
    server = Server([(403, {"status": "NOT_AUTHORIZED"})])
    client, _ = make_client(server)
    with pytest.raises(massive.MassiveError) as ei:
        client.stock_daily("AAPL", "2026-01-01", "2026-02-01")
    assert ei.value.kind == "plan" and "stock daily bars" in str(ei.value)


def test_5xx_retried_twice_then_ok_or_http():
    server = Server([(503, {}), (502, {}), (200, {"results": []})])
    client, _ = make_client(server)
    assert client.chain_snapshot("SPY")["rows"] == []
    assert len(server.requests) == 3

    server = Server([(500, {"error": "boom"})])
    client, _ = make_client(server)
    with pytest.raises(massive.MassiveError) as ei:
        client.chain_snapshot("SPY")
    assert ei.value.kind == "http" and ei.value.status == 500
    assert len(server.requests) == 3


def test_network_errors_are_retried_then_network_kind():
    server = Server([httpx.ConnectError("connection refused")])
    client, _ = make_client(server)
    with pytest.raises(massive.MassiveError) as ei:
        client.chain_snapshot("SPY")
    assert ei.value.kind == "network" and ei.value.status is None
    assert len(server.requests) == 3

    server = Server([httpx.ReadTimeout("slow"), (200, {"results": []})])
    client, _ = make_client(server)
    assert client.chain_snapshot("SPY")["pages"] == 1


def test_not_json_is_http_error():
    def handler(request):
        return httpx.Response(200, content=b"<html>gateway</html>", headers={"Content-Type": "text/html"})

    client, _ = make_client(Server(handler=handler))
    with pytest.raises(massive.MassiveError) as ei:
        client.chain_snapshot("SPY")
    assert ei.value.kind == "http"


def test_missing_key_is_config_and_sends_nothing(monkeypatch):
    monkeypatch.delenv("TST_MASSIVE_API_KEY", raising=False)
    server = Server([(200, {"results": []})])
    http = httpx.Client(transport=httpx.MockTransport(server))
    client = massive.Client(base_url=BASE, http=http)
    assert client.has_key is False
    for call in (lambda: client.chain_snapshot("SPY"),
                 lambda: client.stock_daily("SPY", "2026-01-01", "2026-01-10"),
                 lambda: client.option_daily("O:SPY260116C00600000", "2026-01-01", "2026-01-10")):
        with pytest.raises(massive.MassiveError) as ei:
            call()
        assert ei.value.kind == "config"
        assert "TST_MASSIVE_API_KEY is not set" in str(ei.value)
    assert server.requests == []


def test_key_never_in_error_text_or_repr():
    body = {"status": "ERROR", "error": "bad request apiKey=%s with Bearer %s" % (KEY, KEY)}
    client, _ = make_client(Server([(400, body)]))
    with pytest.raises(massive.MassiveError) as ei:
        client.chain_snapshot("SPY")
    assert KEY not in str(ei.value) and KEY not in repr(ei.value)
    assert KEY not in repr(client)

    client, _ = make_client(Server([httpx.ConnectError("failed for https://x/?apiKey=%s" % KEY)]))
    with pytest.raises(massive.MassiveError) as ei:
        client.chain_snapshot("SPY")
    assert KEY not in str(ei.value)


# ───────────────────────────────────────────── daily bars + pacing

def _bars_handler(seen):
    def handler(request):
        seen.append(request)
        # a midnight-ET bar stamp (EDT: 04:00 UTC), deliberately out of order
        t1 = int(_dt.datetime(2026, 10, 2, 4, 0, tzinfo=_dt.timezone.utc).timestamp() * 1000)
        t0 = int(_dt.datetime(2026, 10, 1, 4, 0, tzinfo=_dt.timezone.utc).timestamp() * 1000)
        return httpx.Response(200, json={"status": "OK", "resultsCount": 3, "results": [
            {"t": t1, "o": 101.0, "h": 103.0, "l": 100.5, "c": 102.5, "v": 1.5e6, "vw": 102.0, "n": 900},
            {"t": t0, "o": 100.0, "h": 101.5, "l": 99.0, "c": 101.0, "v": 1.2e6, "vw": 100.4, "n": 800},
            {"t": t0, "o": 1, "h": 1, "l": 1, "c": None, "v": 1},                      # no close: dropped
        ]})
    return handler


def test_stock_daily_path_params_and_parse():
    seen = []
    client, _ = make_client(Server(handler=_bars_handler(seen)))
    bars = client.stock_daily("aapl", _dt.date(2026, 9, 1), "2026-10-02")
    assert [b["on"] for b in bars] == ["2026-10-01", "2026-10-02"]       # oldest first, ET dates
    assert bars[0] == {"on": "2026-10-01", "open": 100.0, "high": 101.5, "low": 99.0,
                       "close": 101.0, "volume": 1.2e6}
    req = seen[0]
    assert req.url.path == "/v2/aggs/ticker/AAPL/range/1/day/2026-09-01/2026-10-02"
    assert dict(req.url.params)["adjusted"] == "true"
    assert dict(req.url.params)["limit"] == "50000"
    assert req.headers["Authorization"] == "Bearer " + KEY


def test_stock_daily_unadjusted_asks_for_adjusted_false():
    seen = []
    client, _ = make_client(Server(handler=_bars_handler(seen)))
    bars = client.stock_daily("AAPL", "2026-09-01", "2026-10-02", adjusted=False)
    assert [b["on"] for b in bars] == ["2026-10-01", "2026-10-02"]
    assert dict(seen[0].url.params)["adjusted"] == "false"
    client.stock_daily("AAPL", "2026-09-01", "2026-10-02")                 # the default stays adjusted
    assert dict(seen[1].url.params)["adjusted"] == "true"


@pytest.mark.parametrize("spelling", ["BRK-B", "brk-b", "BRK/B", "BRK B", " BRK.B "])
def test_share_class_underlying_uses_massives_dot_in_the_path(spelling):
    seen = []

    def handler(request):
        seen.append(request)
        if "/v3/snapshot/" in request.url.path:
            return httpx.Response(200, json={"results": [result()]})
        return _bars_handler([])(request)

    client, _ = make_client(Server(handler=handler))
    snap = client.chain_snapshot(spelling)
    assert snap["symbol"] == spelling.strip().upper()                     # the caller's spelling
    assert len(snap["rows"]) == 1
    client.stock_daily(spelling, "2026-09-01", "2026-10-02")
    assert seen[0].url.path == "/v3/snapshot/options/BRK.B"
    assert seen[1].url.path == "/v2/aggs/ticker/BRK.B/range/1/day/2026-09-01/2026-10-02"
    assert massive.massive_symbol(spelling) == "BRK.B"
    # the option ticker keeps the OCC root without punctuation
    assert massive.option_ticker(spelling, "2026-06-18", "P", 480) == "O:BRKB260618P00480000"


def test_option_daily_path_and_empty_results():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"status": "OK", "resultsCount": 0})

    client, _ = make_client(Server(handler=handler))
    t = massive.option_ticker("SPY", "2025-12-19", "C", 650)
    assert client.option_daily(t, "2025-11-01", "2025-12-19") == []
    assert seen[0].url.path == "/v2/aggs/ticker/O:SPY251219C00650000/range/1/day/2025-11-01/2025-12-19"
    with pytest.raises(ValueError):
        client.option_daily("SPY", "2025-11-01", "2025-12-19")


def test_stocks_per_minute_window_paces_only_stock_bars():
    seen = []
    client, clock = make_client(Server(handler=_bars_handler(seen)), stocks_per_min=5)
    for _ in range(10):                                   # options: not paced per minute
        client.option_daily("O:SPY251219C00650000", "2025-11-01", "2025-12-19")
    assert clock.sleeps == []
    for _ in range(5):
        client.stock_daily("AAPL", "2026-09-01", "2026-10-02")
    assert clock.sleeps == []                             # five fit the first minute
    t5 = clock.t
    client.stock_daily("AAPL", "2026-09-01", "2026-10-02")
    assert clock.sleeps and clock.sleeps[-1] >= 60.0      # the sixth waits out the minute
    assert clock.t - t5 >= 60.0
    n = len(clock.sleeps)
    for _ in range(4):                                    # the rest of the new window is free
        client.stock_daily("AAPL", "2026-09-01", "2026-10-02")
    assert len(clock.sleeps) == n
    client.stock_daily("AAPL", "2026-09-01", "2026-10-02")
    assert len(clock.sleeps) == n + 1 and clock.sleeps[-1] >= 60.0


def test_requests_per_second_bucket():
    seen = []
    client, clock = make_client(Server(handler=_bars_handler(seen)), max_rps=2)
    for _ in range(4):
        client.option_daily("O:SPY251219C00650000", "2025-11-01", "2025-12-19")
    assert clock.sleeps == [0.5, 0.5]                     # burst of 2, then 2 a second
    assert client.requests == 4


def test_client_from_env(monkeypatch):
    monkeypatch.setenv("TST_MASSIVE_API_KEY", KEY)
    monkeypatch.setenv("TST_MASSIVE_BASE_URL", BASE + "/")
    server = Server([(200, {"results": []})])
    client = massive.Client(http=httpx.Client(transport=httpx.MockTransport(server)),
                            sleep=lambda s: None, now=lambda: 0.0)
    assert client.has_key and client.base_url == BASE
    client.chain_snapshot("QQQ")
    assert server.requests[0].url.host == "api.massive.test"
    assert server.requests[0].headers["Authorization"] == "Bearer " + KEY
    assert client.requests == 1


# ───────────────────────────────────────────── Options Screener endpoints (v4.136)

def _contract(underlying, exp="2026-11-20", ct="call", strike=100.0):
    return {"underlying_ticker": underlying, "expiration_date": exp, "contract_type": ct,
            "strike_price": strike, "shares_per_contract": 100,
            "ticker": "O:%s261120C00100000" % underlying.replace(".", "")}


def test_option_underlyings_counts_every_page_in_our_spelling():
    nxt = BASE + "/v3/reference/options/contracts?cursor=PAGE2"
    pages = {
        None: {"results": [_contract("SPY"), _contract("SPY", ct="put"), _contract("BRK.B"),
                           {"no": "ticker"}], "next_url": nxt},
        "PAGE2": {"results": [_contract("SPY", strike=105.0), _contract("I:SPX")]},
    }
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=pages[request.url.params.get("cursor")])

    client, clock = make_client(Server(handler=handler), stocks_per_min=1)
    got = client.option_underlyings(exp_lte=_dt.date(2026, 12, 9))
    assert got == {"SPY": 3, "BRK-B": 1, "I:SPX": 1}
    first = seen[0]
    assert first.url.path == "/v3/reference/options/contracts"
    params = dict(first.url.params)
    assert params["expired"] == "false" and params["limit"] == "1000"
    assert params["expiration_date.lte"] == "2026-12-09"
    assert "apiKey" not in str(first.url) and first.headers["Authorization"] == "Bearer " + KEY
    assert str(seen[1].url) == nxt                       # the cursor followed as given
    assert clock.sleeps == []                            # options reference: not paced per minute


def test_option_underlyings_without_a_date_and_errors_raise():
    client, _ = make_client(Server([(200, {"results": [_contract("QQQ")]})]))
    assert client.option_underlyings() == {"QQQ": 1}
    client, _ = make_client(Server([(403, {"status": "NOT_AUTHORIZED"})]))
    with pytest.raises(massive.MassiveError) as ei:
        client.option_underlyings()
    assert ei.value.kind == "plan" and "the options contracts list" in str(ei.value)


def test_grouped_daily_path_params_parse_and_pacing():
    seen = []
    t = int(_dt.datetime(2026, 10, 8, 4, 0, tzinfo=_dt.timezone.utc).timestamp() * 1000)

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"status": "OK", "resultsCount": 4, "results": [
            {"T": "AAPL", "o": 200.0, "h": 205.0, "l": 199.0, "c": 204.5, "v": 5.5e7, "t": t},
            {"T": "BRK.B", "o": 480.0, "h": 485.0, "l": 478.0, "c": 482.0, "v": 3.1e6, "t": t},
            {"T": "NOCLOSE", "o": 1.0, "t": t},
            "junk",
        ]})

    client, clock = make_client(Server(handler=handler), stocks_per_min=1)
    bars = client.grouped_daily("2026-10-08")
    assert [b["symbol"] for b in bars] == ["AAPL", "BRK-B"]
    assert bars[0] == {"symbol": "AAPL", "on": "2026-10-08", "open": 200.0, "high": 205.0,
                       "low": 199.0, "close": 204.5, "volume": 5.5e7}
    assert seen[0].url.path == "/v2/aggs/grouped/locale/us/market/stocks/2026-10-08"
    assert dict(seen[0].url.params)["adjusted"] == "true"
    assert clock.sleeps == []
    client.grouped_daily(_dt.date(2026, 10, 7), adjusted=False)     # a stock request: the minute window
    assert dict(seen[1].url.params)["adjusted"] == "false"
    assert seen[1].url.path.endswith("/2026-10-07")
    assert clock.sleeps and clock.sleeps[-1] >= 60.0


def test_grouped_daily_empty_day_and_plan_label():
    client, _ = make_client(Server([(200, {"status": "OK", "resultsCount": 0})]))
    assert client.grouped_daily("2026-10-10") == []
    client, _ = make_client(Server([(403, {"status": "NOT_AUTHORIZED"})]))
    with pytest.raises(massive.MassiveError) as ei:
        client.grouped_daily("2026-10-08")
    assert ei.value.kind == "plan" and "grouped daily stock bars" in str(ei.value)


def test_reference_tickers_pages_and_is_stock_paced():
    nxt = BASE + "/v3/reference/tickers?cursor=T2"
    pages = {
        None: {"results": [
            {"ticker": "AAPL", "name": "Apple Inc.", "type": "CS", "primary_exchange": "XNAS"},
            {"ticker": "BRK.B", "name": "Berkshire Hathaway Inc. Class B", "type": "CS",
             "primary_exchange": "XNYS"},
            {"name": "no ticker"}], "next_url": nxt},
        "T2": {"results": [{"ticker": "SPY", "name": "SPDR S&P 500 ETF", "type": "ETF",
                            "primary_exchange": "ARCX"},
                           {"ticker": "I:SPX", "name": "S&P 500", "type": None}]},
    }
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=pages[request.url.params.get("cursor")])

    client, clock = make_client(Server(handler=handler), stocks_per_min=1)
    got = client.reference_tickers("stocks")
    assert [t["symbol"] for t in got] == ["AAPL", "BRK-B", "I:SPX", "SPY"]
    assert got[1] == {"symbol": "BRK-B", "name": "Berkshire Hathaway Inc. Class B", "type": "CS",
                      "primary_exchange": "XNYS"}
    assert got[2] == {"symbol": "I:SPX", "name": "S&P 500", "type": None, "primary_exchange": None}
    params = dict(seen[0].url.params)
    assert seen[0].url.path == "/v3/reference/tickers"
    assert (params["market"], params["active"], params["limit"]) == ("stocks", "true", "1000")
    assert clock.sleeps and clock.sleeps[-1] >= 60.0       # page 2 waited out the minute window
    n = len(seen)
    client.reference_tickers("indices")
    assert dict(seen[n].url.params)["market"] == "indices"


def test_our_symbol_is_the_inverse_of_massive_symbol():
    for ours in ("BRK-B", "AAPL", "BF-A"):
        assert massive.our_symbol(massive.massive_symbol(ours)) == ours
    assert massive.our_symbol(" brk.b ") == "BRK-B"
    assert massive.our_symbol("I:SPX") == "I:SPX"
    assert massive.our_symbol(None) == ""


def test_paging_cap_is_per_call():
    loop = {"results": [_contract("SPY")], "next_url": BASE + "/v3/reference/options/contracts?cursor=X"}
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        body = dict(loop, next_url=BASE + "/v3/reference/options/contracts?cursor=%d" % calls["n"])
        return httpx.Response(200, json=body)

    client, _ = make_client(Server(handler=handler))
    with pytest.raises(massive.MassiveError) as ei:
        list(client._pages("/v3/reference/options/contracts", {}, max_pages=3))
    assert ei.value.kind == "http" and "past 3 pages" in str(ei.value) and calls["n"] == 3
