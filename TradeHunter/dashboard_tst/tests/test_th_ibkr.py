"""bridge/th_ibkr.py - the IBKR fetch library (Options v2, design section 3).

Everything runs against a FAKE ``ib_insync`` module (Stock / Option) and a FakeIB:
qualification against a listed-strike table, reqSecDefOptParams rows, streaming
reqMktData whose ticker is filled a few ms later by Black-Scholes (or never, when
the current market data type is "not entitled"), historical bars and the account
summary. Timing constants are shrunk per test so a wave takes milliseconds.
"""
from __future__ import annotations

import ast
import asyncio
import datetime as _dt
import importlib.util
import math
import sys
import types
from pathlib import Path

import pytest

from tests.fixtures.options import bs_greeks

BRIDGE = Path(__file__).resolve().parent.parent / "bridge" / "th_ibkr.py"


def _load(name="th_ibkr_under_test"):
    spec = importlib.util.spec_from_file_location(name, BRIDGE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


th = _load()
NAN = float("nan")
TODAY = "2026-10-09"          # a Friday


# ───────────────────────────────────────────────────────────── fake ib_insync
class _Contract:
    secType = ""

    def __init__(self, **kw):
        self.conId = 0
        self.symbol = self.exchange = self.currency = self.tradingClass = self.multiplier = ""
        self.lastTradeDateOrContractMonth = self.right = ""
        self.strike = 0.0
        for k, v in kw.items():
            setattr(self, k, v)


class Stock(_Contract):
    secType = "STK"

    def __init__(self, symbol="", exchange="", currency="", **kw):
        super().__init__(symbol=symbol, exchange=exchange, currency=currency, **kw)


class Option(_Contract):
    secType = "OPT"

    def __init__(self, symbol="", lastTradeDateOrContractMonth="", strike=0.0, right="",
                 exchange="", multiplier="", currency="", **kw):
        super().__init__(symbol=symbol, lastTradeDateOrContractMonth=lastTradeDateOrContractMonth,
                         strike=strike, right=right, exchange=exchange, multiplier=multiplier,
                         currency=currency, **kw)


def fake_ib_insync():
    m = types.ModuleType("ib_insync")
    m.Stock, m.Option = Stock, Option
    return m


class FakeTicker:
    def __init__(self, contract):
        self.contract = contract
        self.bid = self.ask = self.last = self.close = self.volume = NAN
        self.bidSize = self.askSize = self.lastSize = NAN
        self.putOpenInterest = self.callOpenInterest = NAN
        self.modelGreeks = self.bidGreeks = self.askGreeks = self.lastGreeks = None
        self.time = None
        self.marketDataType = 1            # ib_insync's default before any callback

    def marketPrice(self):               # ib_insync's rule
        has = self.bid == self.bid and self.ask == self.ask and self.bid > 0 and self.ask > 0
        if has:
            return self.last if self.bid <= self.last <= self.ask else (self.bid + self.ask) / 2
        return self.last


def _chain_row(exchange, tc, mult, exps, strikes):
    return types.SimpleNamespace(exchange=exchange, underlyingConId=1, tradingClass=tc,
                                 multiplier=mult, expirations=set(exps), strikes=set(strikes))


UNION = [float(k) for k in range(40, 161)] + [97.5, 102.5]
EXPS = ["20261002", "20261009", "20261016", "20261023", "20261030", "20261106", "20261113",
        "20261120", "20261127", "20261204", "20261211", "20261218", "20270115", "20270122",
        "20270319", "20270617", "20280121", "20290119", "20291221"]


class FakeIB:
    """Market data types in ``prices_on`` deliver a two-sided quote, those in
    ``close_only_on`` only the previous close, those in ``greeks_on`` model greeks;
    ``report_as`` maps a requested type to the type TWS reports back."""

    def __init__(self, *, spot=100.0, iv=0.35, prices_on=(1, 2, 3, 4), close_only_on=(),
                 greeks_on=None, stock_on=(1, 2, 3, 4), stock_close_only=False, delay=0.004,
                 listed=None, dead=(), no_greeks=(), bump=None, report_as=None, known=True,
                 chains=None, bars=(), ivbars=(), summary=(), greeks_once=False):
        self.spot, self.iv = spot, iv
        self.prices_on, self.close_only_on = set(prices_on), set(close_only_on)
        self.greeks_on = set(prices_on if greeks_on is None else greeks_on)
        self.stock_on, self.stock_close_only = set(stock_on), stock_close_only
        self.delay, self.dead, self.no_greeks = delay, set(dead), set(no_greeks)
        self.bump, self.report_as = dict(bump or {}), dict(report_as or {})
        self.known = known
        self.greeks_once, self._greeks_sent = greeks_once, set()   # TWS: no resend on re-subscribe
        self.listed = listed if listed is not None else {e: set(UNION) for e in EXPS}
        self.chains = chains if chains is not None else [_chain_row("SMART", "XYZ", "100", EXPS, UNION)]
        self.bars, self.ivbars, self.summary = list(bars), list(ivbars), list(summary)
        self.mdt = 1
        self.mdt_calls, self.subs, self.qualify_sizes, self.hist_calls = [], [], [], []
        self.active, self.max_active = set(), 0
        self._tk, self._h, self._next = {}, {}, 100

    # -- contracts
    async def qualifyContractsAsync(self, *cs):
        self.qualify_sizes.append(len(cs))
        out = []
        for c in cs:
            if c.secType == "STK":
                if not self.known:
                    continue
                c.conId = 7
            else:
                if float(c.strike) not in self.listed.get(c.lastTradeDateOrContractMonth, ()):
                    continue
                c.conId, self._next = self._next, self._next + 1
            out.append(c)
        return out

    async def reqSecDefOptParamsAsync(self, sym, fut, sectype, con_id):
        assert sectype == "STK" and con_id == 7
        return list(self.chains)

    # -- market data
    def reqMarketDataType(self, t):
        self.mdt = t
        self.mdt_calls.append(t)

    def ticker(self, c):
        return self._tk.get(id(c))

    def reqMktData(self, c, ticks="", snapshot=False, regulatory=False):
        assert snapshot is False and regulatory is False      # streaming only
        self.subs.append((c, ticks, self.mdt))
        t = self._tk.setdefault(id(c), FakeTicker(c))
        self.active.add(id(c))
        self.max_active = max(self.max_active, len(self.active))
        self._h[id(c)] = asyncio.get_running_loop().call_later(self.delay, self._fill, c, t, self.mdt)
        return t

    def cancelMktData(self, c):
        self.active.discard(id(c))
        h = self._h.pop(id(c), None)
        if h:
            h.cancel()

    def _fill(self, c, t, mdt):
        if id(c) not in self.active:
            return
        if c.secType == "STK":
            if mdt in self.stock_on:
                if not self.stock_close_only:
                    t.bid, t.ask, t.last = self.spot - 0.1, self.spot + 0.1, self.spot
                t.close = self.spot - 1.0
                t.marketDataType = self.report_as.get(mdt, mdt)
            return
        k = float(c.strike)
        if k in self.dead:
            return
        exp = _dt.date(int(c.lastTradeDateOrContractMonth[:4]), int(c.lastTradeDateOrContractMonth[4:6]),
                       int(c.lastTradeDateOrContractMonth[6:]))
        T = max((exp - _dt.date.fromisoformat(TODAY)).days, 1) / 365.0
        g = bs_greeks(self.spot, k, T, self.iv, c.right)
        if mdt in self.prices_on or mdt in self.close_only_on:
            mid = max(0.05, g["price"]) + self.bump.get(mdt, 0.0)
            if mdt in self.prices_on:
                t.bid, t.ask = round(mid - 0.05, 2), round(mid + 0.05, 2)
                t.bidSize, t.askSize = 12.0, 30.0
                t.volume = 250.0
            t.close = round(mid, 2)
            if c.right == "C":
                t.callOpenInterest = 1500.0
            else:
                t.putOpenInterest = 900.0
            t.marketDataType = self.report_as.get(mdt, mdt)
        if mdt in self.greeks_on and k not in self.no_greeks:
            if self.greeks_once and id(c) in self._greeks_sent:
                return
            self._greeks_sent.add(id(c))
            t.modelGreeks = types.SimpleNamespace(impliedVol=self.iv, delta=g["delta"], gamma=g["gamma"],
                                                  theta=g["theta"], vega=g["vega"], undPrice=self.spot)

    # -- history / account
    async def reqHistoricalDataAsync(self, contract, endDateTime="", durationStr="", barSizeSetting="",
                                     whatToShow="", useRTH=True, formatDate=1, **kw):
        self.hist_calls.append(dict(contract=contract, endDateTime=endDateTime, durationStr=durationStr,
                                    barSizeSetting=barSizeSetting, whatToShow=whatToShow,
                                    useRTH=useRTH, formatDate=formatDate))
        return list(self.bars if whatToShow == "TRADES" else self.ivbars)

    async def accountSummaryAsync(self, account=""):
        return list(self.summary)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    """A fake ib_insync, fresh module state and millisecond timings for every test."""
    monkeypatch.setitem(sys.modules, "ib_insync", fake_ib_insync())
    for name, value in {"QUOTE_POLL": 0.002, "QUOTE_MIN": 0.02, "QUOTE_QUIET": 0.01,
                        "QUOTE_STALL": 0.04, "SPOT_POLL": 0.002, "SPOT_WAIT": 0.06,
                        "SPOT_SETTLE": 0.02, "MSG_RATE": 0, "HIST_GAP": 0.0}.items():
        monkeypatch.setattr(th, name, value)
    th.reset_mdt()
    th.clear_cache()
    th._bucket.reset()
    th._hist_next[0] = 0.0
    yield
    th.reset_mdt()
    th.clear_cache()


def run(coro):
    return asyncio.run(coro)


def defs_for(ib, symbol="XYZ"):
    return run(th.chain_defs(ib, symbol))


ROW_KEYS = {"expiry", "right", "strike", "bid", "ask", "mid", "last", "bid_size", "ask_size",
            "volume", "oi", "iv", "delta", "gamma", "theta", "vega", "und_price"}


# ───────────────────────────────────────────────────────────── import hygiene
def test_import_does_not_import_ib_insync(monkeypatch):
    # None in sys.modules makes any "import ib_insync" raise - the load must not try
    monkeypatch.setitem(sys.modules, "ib_insync", None)
    mod = _load("th_ibkr_import_probe")
    assert mod.VERSION == "2.0"
    assert mod.MDT_NAMES == {1: "live", 2: "frozen", 3: "delayed", 4: "delayed_frozen"}
    w = mod.plan({"expiries": ["2026-10-16"], "strikes": [95, 100, 105]}, spot=100, today=TODAY)
    assert w[0]["expiry"] == "2026-10-16"


def test_module_imports_only_stdlib_at_top_level_and_ib_insync_lazily():
    tree = ast.parse(BRIDGE.read_text(encoding="utf-8"))
    top, inner = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            (top if node in tree.body else inner).update(n.split(".")[0] for n in names)
    assert top <= set(sys.stdlib_module_names) | {"__future__"}, top
    assert inner <= {"ib_insync", "zoneinfo"}, inner
    assert not any(n == "app" or n.startswith("app.") for n in top | inner)


# ───────────────────────────────────────────────────────────── dates
def test_third_friday_and_monthly_detection():
    assert th.third_friday(2026, 11) == _dt.date(2026, 11, 20)
    assert th.third_friday(2027, 1) == _dt.date(2027, 1, 15)       # the 1st is a Friday
    assert th.is_monthly(_dt.date(2026, 12, 18))
    assert not th.is_monthly(_dt.date(2026, 12, 11))
    # Juneteenth observed on Friday 2027-06-18: that monthly expires on the Thursday
    assert th.is_monthly(_dt.date(2027, 6, 17), {_dt.date(2027, 6, 17)})
    assert not th.is_monthly(_dt.date(2027, 6, 17), {_dt.date(2027, 6, 17), _dt.date(2027, 6, 18)})
    assert not th.is_monthly(_dt.date(2027, 6, 17))                 # no listing to tell


def test_us_eastern_offset_and_et_today_fallback(monkeypatch):
    off = th._us_eastern_offset
    assert off(_dt.datetime(2026, 7, 1, 12)) == 4 and off(_dt.datetime(2026, 1, 15, 12)) == 5
    assert off(_dt.datetime(2026, 3, 8, 6, 59)) == 5 and off(_dt.datetime(2026, 3, 8, 7, 0)) == 4
    assert off(_dt.datetime(2026, 11, 1, 5, 59)) == 4 and off(_dt.datetime(2026, 11, 1, 6, 0)) == 5
    utc = _dt.datetime(2026, 10, 9, 2, 0, tzinfo=_dt.timezone.utc)   # 22:00 ET the day before
    assert th.et_today(utc) == _dt.date(2026, 10, 8)
    # without a tz database the hand-written DST rule gives the same answer
    monkeypatch.setitem(sys.modules, "zoneinfo", None)
    assert th.et_today(utc) == _dt.date(2026, 10, 8)
    assert th.et_today(_dt.datetime(2026, 1, 9, 4, 30)) == _dt.date(2026, 1, 8)   # naive = UTC, EST


# ───────────────────────────────────────────────────────────── plan (pure)
DEFS = {"symbol": "XYZ", "con_id": 7, "exchange": "SMART", "trading_class": "XYZ", "multiplier": 100,
        "expiries": [f"{e[:4]}-{e[4:6]}-{e[6:]}" for e in EXPS], "strikes": sorted(UNION)}


def _days(iso):
    return (_dt.date.fromisoformat(iso) - _dt.date.fromisoformat(TODAY)).days


def test_plan_expiries_weeklies_monthlies_and_limits():
    w = th.plan(DEFS, spot=100, today=TODAY)
    exps = [e["expiry"] for e in w]
    assert exps == sorted(exps)
    assert "2026-10-02" not in exps                                  # past
    assert "2026-10-09" in exps and w[0]["dte"] == 0                 # expiring today
    for e in ("2026-10-16", "2026-10-23", "2026-11-27", "2026-12-04", "2026-12-11"):
        assert e in exps                                             # weeklies <= 63 DTE
    assert _days("2026-12-11") == 63 and _days("2026-12-18") == 70
    assert "2026-12-18" in exps and "2027-01-15" in exps             # monthlies past 63
    assert "2027-01-22" not in exps                                  # a weekly past 63
    assert "2027-06-17" in exps                                      # holiday-Thursday monthly
    assert "2029-01-19" in exps and _days("2029-01-19") <= 1100
    assert "2029-12-21" not in exps and _days("2029-12-21") > 1100   # past max_dte
    assert all(e["dte"] == _days(e["expiry"]) for e in w)
    assert all(e["trading_class"] == "XYZ" and e["multiplier"] == 100 for e in w)

    short = th.plan(DEFS, spot=100, today=TODAY, max_weekly_dte=14, max_dte=45)
    assert [e["expiry"] for e in short] == ["2026-10-09", "2026-10-16", "2026-10-23", "2026-11-20"]


def test_plan_explicit_expiries_override():
    w = th.plan(DEFS, spot=100, today=TODAY,
                expiries=["20270122", "2026-10-30", "2026-10-02", "2026-10-31", "2030-01-18"])
    # listed + not past only; the weekly beyond max_weekly_dte is kept because it was asked for
    assert [e["expiry"] for e in w] == ["2026-10-30", "2027-01-22"]


def test_plan_strike_window_is_one_scaled_expected_move():
    spot, iv = 100.0, 0.40
    w = {e["expiry"]: e for e in th.plan(DEFS, spot=spot, iv_hint=iv, today=TODAY)}
    for exp in ("2026-10-16", "2026-10-30", "2026-11-20"):
        e = w[exp]
        half = 2.5 * spot * iv * math.sqrt(e["dte"] / 365.0)
        inside = [k for k in UNION if spot - half <= k <= spot + half]
        assert e["strikes"] == sorted(inside), exp
        assert e["strikes"] == sorted(e["strikes"])
    # a wider IV hint gives a wider window on the same expiry
    wide = {e["expiry"]: e for e in th.plan(DEFS, spot=spot, iv_hint=0.80, today=TODAY)}
    assert len(wide["2026-10-16"]["strikes"]) > len(w["2026-10-16"]["strikes"])


def test_plan_min_side_takes_the_nearest_listed_strikes():
    w = th.plan(DEFS, spot=100, iv_hint=0.01, today=TODAY, expiries=["2026-10-16"], min_side=6)
    assert w[0]["strikes"] == [95.0, 96.0, 97.0, 97.5, 98.0, 99.0,
                               100.0, 101.0, 102.0, 102.5, 103.0, 104.0]


def test_plan_max_side_caps_each_side_nearest_first():
    w = th.plan(DEFS, spot=100.5, iv_hint=1.5, today=TODAY, expiries=["2029-01-19"], max_side=10)
    ks = w[0]["strikes"]
    below, above = [k for k in ks if k < 100.5], [k for k in ks if k >= 100.5]
    assert len(below) == 10 and len(above) == 10
    assert below == sorted(k for k in UNION if k < 100.5)[-10:]
    assert above == sorted(k for k in UNION if k >= 100.5)[:10]


def test_plan_side_with_fewer_listed_strikes_than_min_side():
    d = {"expiries": ["2026-10-16"], "strikes": [90, 95, 100, 105, 110, 115, 120, 125, 130]}
    w = th.plan(d, spot=92, iv_hint=0.01, today=TODAY, min_side=4)
    assert w[0]["strikes"] == [90.0, 95.0, 100.0, 105.0, 110.0]       # one exists below


def test_plan_iv_hint_default_and_percent():
    base = th.plan(DEFS, spot=100, today=TODAY, expiries=["2026-11-20"])
    forty = th.plan(DEFS, spot=100, iv_hint=0.40, today=TODAY, expiries=["2026-11-20"])
    assert base == forty                                             # None -> 0.40
    pct = th.plan(DEFS, spot=100, iv_hint=46.0, today=TODAY, expiries=["2026-11-20"])
    frac = th.plan(DEFS, spot=100, iv_hint=0.46, today=TODAY, expiries=["2026-11-20"])
    assert pct == frac                                               # a percent slipped in


def test_plan_today_accepts_date_and_validates_spot():
    assert th.plan(DEFS, spot=100, today=_dt.date(2026, 10, 9)) == th.plan(DEFS, spot=100, today=TODAY)
    for bad in (None, 0, -5, "x"):
        with pytest.raises(ValueError):
            th.plan(DEFS, spot=bad, today=TODAY)
    assert th.plan({"expiries": ["2026-10-16"], "strikes": []}, spot=100, today=TODAY) == []


def test_plan_spec_reads_a_json_spec():
    spec = {"symbol": "XYZ", "spot": 100.0, "iv_hint": "0.46", "expiries": None,
            "max_weekly_dte": "21", "max_dte": 60, "sigma_k": 2.5, "min_side": "6", "max_side": 40}
    want = th.plan(DEFS, spot=100.0, iv_hint=0.46, today=TODAY, max_weekly_dte=21, max_dte=60)
    assert th.plan_spec(DEFS, spec, today=TODAY) == want
    assert th.plan_spec(DEFS, spec, spot=110.0, today=TODAY) == th.plan(
        DEFS, spot=110.0, iv_hint=0.46, today=TODAY, max_weekly_dte=21, max_dte=60)
    # a member chunk: the spec's max_expiries / max_side reach plan
    chunk = dict(spec, max_weekly_dte=63, max_dte=1100, max_expiries="3", max_side=25)
    got = th.plan_spec(DEFS, chunk, today=TODAY)
    assert got == th.plan(DEFS, spot=100.0, iv_hint=0.46, today=TODAY, max_expiries=3, max_side=25)
    assert len(got) == 3


def test_plan_max_expiries_keeps_the_nearest_eligible():
    """#16/#19: a member's read is chunked - with no explicit list, the nearest N
    eligible expiries; an explicit list is never cut; 0 / None = no cap."""
    full = th.plan(DEFS, spot=100, today=TODAY)
    six = th.plan(DEFS, spot=100, today=TODAY, max_expiries=6)
    assert [e["expiry"] for e in six] == [e["expiry"] for e in full][:6]
    assert six == full[:6]
    assert th.plan(DEFS, spot=100, today=TODAY, max_expiries=0) == full
    assert th.plan(DEFS, spot=100, today=TODAY, max_expiries=None) == full
    asked = ["2026-10-16", "2026-10-23", "2026-10-30", "2026-11-06"]
    assert [e["expiry"] for e in th.plan(DEFS, spot=100, today=TODAY, expiries=asked,
                                         max_expiries=2)] == asked


# ───────────────────────────────────────────────────────────── chain_defs
def test_chain_defs_prefers_the_standard_smart_row():
    chains = [
        _chain_row("CBOE", "XYZ", "100", EXPS[:3], [100, 105]),
        _chain_row("SMART", "2XYZ", "100", EXPS[:2], [50, 60]),             # adjusted class
        _chain_row("SMART", "XYZ", "100", ["20261120", "20261016", "20261009"], [105, 95.5, 100]),
        _chain_row("AMEX", "XYZ", "100", EXPS, UNION),
    ]
    ib = FakeIB(chains=chains)
    d = defs_for(ib, "xyz")
    assert d == {"symbol": "XYZ", "con_id": 7, "exchange": "SMART", "trading_class": "XYZ",
                 "expiries": ["2026-10-09", "2026-10-16", "2026-11-20"],
                 "strikes": [95.5, 100.0, 105.0], "multiplier": 100}


def test_chain_defs_falls_back_to_another_exchange_and_raises_on_nothing():
    ib = FakeIB(chains=[_chain_row("CBOE", "XYZ", "100", ["20261016"], [100])])
    assert defs_for(ib)["exchange"] == "CBOE"
    with pytest.raises(RuntimeError, match="no option chain"):
        defs_for(FakeIB(chains=[]))
    with pytest.raises(RuntimeError, match="does not recognise"):
        defs_for(FakeIB(known=False), "NOPE")


# ───────────────────────────────────────────────────────────── quote
def small_window(ib, **kw):
    kw = {"today": TODAY, "expiries": ["2026-10-16", "2026-11-20"], "iv_hint": 0.01,
          "min_side": 4, "max_side": 4, **kw}
    return th.plan(defs_for(ib), spot=100, **kw)


def test_quote_rows_shape_and_units():
    ib = FakeIB()
    w = small_window(ib)
    out = run(th.quote(ib, "XYZ", w, max_lines=60, wait=1.0))
    assert set(out) >= {"symbol", "spot", "mdt", "rows", "requested", "filled", "ms"}
    assert out["symbol"] == "XYZ" and out["mdt"] == "live" and out["mdt_id"] == 1
    assert out["requested"] == 2 * 8 * 2 == out["filled"] == len(out["rows"])
    assert out["spot"] == 100.0 and isinstance(out["ms"], int)
    assert out["partial"] is False and out["attempted"] == 32         # no deadline: always there
    for r in out["rows"]:
        assert set(r) == ROW_KEYS
        assert r["iv"] == pytest.approx(0.35)                         # FRACTION
        assert (r["delta"] > 0) if r["right"] == "C" else (r["delta"] < 0)
        assert r["mid"] == pytest.approx((r["bid"] + r["ask"]) / 2)
        assert r["oi"] == (1500 if r["right"] == "C" else 900)        # its own side's tick
        assert r["volume"] == 250 and r["bid_size"] == 12 and r["ask_size"] == 30
        assert r["und_price"] == 100.0 and r["gamma"] > 0 and r["vega"] > 0
        assert r["expiry"] in ("2026-10-16", "2026-11-20")
    keys = [(r["expiry"], r["right"], r["strike"]) for r in out["rows"]]
    assert keys == sorted(keys)
    # streaming subscriptions with the one option generic tick the 1.x bridge proved
    # (101 = open interest); "100" / "106" are underlying ticks and risk error 321
    assert th.OPT_TICKS == "101"
    assert {ticks for c, ticks, _ in ib.subs if c.secType == "OPT"} == {"101"}
    assert not ib.active                                               # every line released


def test_quote_waves_never_exceed_max_lines():
    ib = FakeIB()
    w = small_window(ib)                                               # 32 contracts
    seen = []
    out = run(th.quote(ib, "XYZ", w, max_lines=7, wait=1.0, progress=lambda d, n: seen.append((d, n))))
    assert out["requested"] == 32 and out["waves"] == math.ceil(32 / 7)
    assert ib.max_active == 7                                          # full waves, never more
    assert seen == [(7, 32), (14, 32), (21, 32), (28, 32), (32, 32)]
    ib2 = FakeIB()
    run(th.quote(ib2, "XYZ", small_window(ib2), max_lines=1, wait=1.0))
    assert ib2.max_active == 1


def test_quote_qualifies_in_batches_of_50_drops_unknown_and_caches():
    listed = {e: set(UNION) for e in EXPS}
    listed["20261120"] = {k for k in UNION if k % 5 == 0}             # a monthly: $5 strikes only
    ib = FakeIB(listed=listed)
    w = th.plan(defs_for(ib), spot=100, iv_hint=0.01, today=TODAY,
                expiries=["2026-10-16", "2026-10-23", "2026-11-20"], min_side=10, max_side=10)
    planned = sum(len(e["strikes"]) * 2 for e in w)                   # 3 x 20 x 2 = 120
    ib.qualify_sizes.clear()
    out = run(th.quote(ib, "XYZ", w, wait=1.0))
    assert ib.qualify_sizes == [50, 50, 20]
    real_monthly = [k for k in w[2]["strikes"] if k % 5 == 0]
    assert out["planned"] == planned
    assert out["unknown"] == (len(w[2]["strikes"]) - len(real_monthly)) * 2
    assert out["requested"] == planned - out["unknown"]
    assert sorted({r["strike"] for r in out["rows"] if r["expiry"] == "2026-11-20"}) == real_monthly

    ib.qualify_sizes.clear()
    again = run(th.quote(ib, "XYZ", w, wait=1.0))
    assert ib.qualify_sizes == []                                      # known AND unknown cached
    assert again["requested"] == out["requested"] and again["unknown"] == out["unknown"]


def test_quote_none_where_ibkr_sent_nothing():
    ib = FakeIB(dead={99.0}, no_greeks={102.0})
    out = run(th.quote(ib, "XYZ", small_window(ib), wait=1.0, spot=100.25))
    strikes = {r["strike"] for r in out["rows"]}
    assert 99.0 not in strikes                                         # nothing at all -> left out
    assert out["requested"] == 32 and out["filled"] == 28
    ng = [r for r in out["rows"] if r["strike"] == 102.0]
    assert len(ng) == 4 and all(r["bid"] is not None for r in ng)
    assert all(r[k] is None for r in ng for k in ("iv", "delta", "gamma", "theta", "vega"))
    assert all(r["und_price"] == 100.25 for r in ng)                   # the spot fills in
    assert out["spot"] == 100.25


def test_quote_spot_from_model_underlying_when_not_given():
    ib = FakeIB(spot=101.0)
    out = run(th.quote(ib, "XYZ", small_window(ib), wait=1.0))
    assert out["spot"] == 101.0


def test_quote_market_data_type_fallback_when_live_yields_nothing():
    ib = FakeIB(prices_on=(3, 4))
    out = run(th.quote(ib, "XYZ", small_window(ib), wait=1.0, max_lines=10))
    assert ib.mdt_calls[:3] == [1, 2, 3]                               # probed in preference order
    assert 4 not in ib.mdt_calls
    assert out["mdt"] == "delayed" and out["filled"] == 32
    assert all(r["bid"] is not None and r["delta"] is not None for r in out["rows"])
    assert ib.max_active <= 10

    ib.mdt_calls.clear()                                               # the verdict is remembered
    run(th.quote(ib, "XYZ", small_window(ib), wait=1.0, max_lines=10))
    assert set(ib.mdt_calls) == {3}

    for key, (t, at) in list(th._STICKY.items()):                     # ... until MDT_RECHECK
        th._STICKY[key] = (t, at - th.MDT_RECHECK - 1)
    ib.mdt_calls.clear()
    run(th.quote(ib, "XYZ", small_window(ib), wait=1.0, max_lines=10))
    assert ib.mdt_calls[0] == 1


def test_quote_fallback_keeps_what_the_first_type_delivered():
    # greeks only arrive under live, prices only under delayed: the re-subscription
    # must not wipe the greeks (TWS does not resend them - greeks_once)
    ib = FakeIB(prices_on=(3,), greeks_on=(1,), greeks_once=True)
    out = run(th.quote(ib, "XYZ", small_window(ib), wait=1.0))
    assert out["mdt"] == "delayed" and out["filled"] == 32
    assert all(r["bid"] is not None and r["delta"] is not None for r in out["rows"])


def test_quote_close_only_is_not_a_quote_after_the_close():
    # after the close a live feed shows only the close; the frozen feed has bid/ask
    ib = FakeIB(prices_on=(2,), close_only_on=(1,), greeks_on=(1, 2))
    out = run(th.quote(ib, "XYZ", small_window(ib), wait=1.0))
    assert out["mdt"] == "frozen" and ib.mdt_calls[:2] == [1, 2]
    assert all(r["bid"] is not None for r in out["rows"])


def test_quote_eod_preference_order_and_reported_type():
    # EOD asks frozen first; in hours TWS answers a frozen request with live data
    ib = FakeIB(report_as={2: 1})
    out = run(th.quote(ib, "XYZ", small_window(ib), wait=1.0, mdt_pref=(2, 1, 4, 3)))
    assert ib.mdt_calls[0] == 2 and out["mdt"] == "live"
    ib2 = FakeIB(prices_on=(4,))
    out2 = run(th.quote(ib2, "XYZ", small_window(ib2), wait=1.0, mdt_pref=("frozen", "live", "delayed_frozen")))
    assert out2["mdt"] == "delayed_frozen" and ib2.mdt_calls[:3] == [2, 1, 4]


def test_quote_reprobes_once_when_the_remembered_type_goes_dead():
    ib = FakeIB(prices_on=(3,))
    th._sticky_set(("opt", 1, 2, 3, 4), 1)                            # remembered: live
    out = run(th.quote(ib, "XYZ", small_window(ib), wait=1.0))
    assert out["mdt"] == "delayed" and out["filled"] == 32
    assert th._sticky_get(("opt", 1, 2, 3, 4)) == 3


def test_quote_greeks_from_the_other_family_prices_kept():
    # live prices, no live greeks; delayed-frozen has greeks and (different) prices
    ib = FakeIB(prices_on=(1, 4), greeks_on=(4,), bump={4: 0.5})
    out = run(th.quote(ib, "XYZ", small_window(ib), wait=1.0))
    assert out["mdt"] == "live" and 4 in ib.mdt_calls
    for r in out["rows"]:
        assert r["delta"] is not None and r["iv"] == pytest.approx(0.35)
        g = bs_greeks(100.0, r["strike"], max(_days(r["expiry"]), 1) / 365.0, 0.35, r["right"])
        assert r["bid"] == round(max(0.05, g["price"]) - 0.05, 2)     # the live bid, not the bumped one


def test_quote_dead_feed_returns_no_rows_and_forgets_stale_tickers():
    ib = FakeIB()
    w = small_window(ib)
    first = run(th.quote(ib, "XYZ", w, wait=1.0))
    assert first["filled"] == 32
    ib.prices_on, ib.greeks_on, ib.close_only_on = set(), set(), set()
    th.reset_mdt()
    dead = run(th.quote(ib, "XYZ", w, wait=0.3))
    assert dead["requested"] == 32 and dead["filled"] == 0 and dead["rows"] == []
    assert dead["mdt"] in th.MDT_IDS and dead["spot"] is None
    assert not ib.active


def test_quote_empty_window():
    ib = FakeIB()
    out = run(th.quote(ib, "XYZ", [], wait=0.2))
    assert out["rows"] == [] and out["requested"] == 0 and out["waves"] == 0 and out["mdt"] == "live"


# ───────────────────────────────────────────────────────────── spot / fetch
def test_spot_streaming_quote():
    ib = FakeIB(spot=250.0)
    s = run(th.spot(ib, "xyz"))
    assert set(s) == {"spot", "bid", "ask", "last", "close", "mdt"} and s["mdt"] == "live"
    assert (s["spot"], s["bid"], s["ask"], s["last"], s["close"]) == pytest.approx(
        (250.0, 249.9, 250.1, 250.0, 249.0))
    assert [t for c, t, _ in ib.subs] == [""] and not ib.active


def test_spot_falls_back_to_delayed_frozen_and_remembers():
    ib = FakeIB(stock_on=(4,))
    s = run(th.spot(ib, "XYZ"))
    assert s["spot"] == 100.0 and s["mdt"] == "delayed_frozen" and ib.mdt_calls == [1, 4]
    ib.mdt_calls.clear()
    run(th.spot(ib, "XYZ"))
    assert ib.mdt_calls == [4]


def test_spot_close_only_and_no_price():
    ib = FakeIB(stock_close_only=True)
    assert run(th.spot(ib, "XYZ"))["spot"] == 99.0
    with pytest.raises(RuntimeError, match="No price"):
        run(th.spot(FakeIB(stock_on=()), "XYZ"))
    with pytest.raises(RuntimeError, match="does not recognise"):
        run(th.spot(FakeIB(known=False), "NOPE"))


def test_a_cancelled_read_releases_its_lines(monkeypatch):
    # the connector wraps reads in asyncio.wait_for; a timeout must not leak lines
    monkeypatch.setattr(th, "SPOT_WAIT", 5.0)
    ib = FakeIB(stock_on=(), prices_on=())

    async def go(coro):
        with pytest.raises((asyncio.TimeoutError, TimeoutError)):
            await asyncio.wait_for(coro, 0.1)

    run(go(th.spot(ib, "XYZ")))
    assert not ib.active
    ib2 = FakeIB(prices_on=())
    w = small_window(ib2)
    monkeypatch.setattr(th, "QUOTE_STALL", 5.0)
    run(go(th.quote(ib2, "XYZ", w, wait=5.0, max_lines=10)))
    assert ib2.max_active == 10 and not ib2.active


def test_fetch_end_to_end_from_a_spec():
    ib = FakeIB(spot=100.0)
    spec = {"symbol": "XYZ", "spot": 97.0, "iv_hint": 0.01, "expiries": ["2026-10-16"],
            "max_weekly_dte": 63, "max_dte": 1100, "sigma_k": 2.5, "min_side": 3, "max_side": 3}
    out = run(th.fetch(ib, "XYZ", spec, wait=1.0, today=TODAY))
    assert out["spot"] == 100.0 and out["spot_mdt"] == "live" and out["n_expiries"] == 1
    # planned around the FRESH spot (100), not the spec's stored 97
    assert sorted({r["strike"] for r in out["rows"]}) == [97.5, 98.0, 99.0, 100.0, 101.0, 102.0]
    assert out["filled"] == 12 and out["partial"] is False


# ───────────────────────────────────────────────────────────── deadline + release (#16/#19, #20)
def test_quote_deadline_returns_the_waves_read_so_far(monkeypatch):
    """#16/#19: once the deadline passes no new wave starts and the rows already read
    come back (partial) - the connector used to be cancelled and return nothing."""
    import time as _time

    offset = [0.0]
    monkeypatch.setattr(th, "_now", lambda: _time.monotonic() + offset[0])
    ib = FakeIB()
    w = small_window(ib)                                               # 32 contracts

    def jump(done, total):
        if done == 8:                                                  # two waves of 4 read ...
            offset[0] += 1000.0                                        # ... then time is up

    out = run(th.quote(ib, "XYZ", w, max_lines=4, wait=1.0, deadline=60.0, progress=jump))
    assert out["partial"] is True and out["attempted"] == 8 and out["requested"] == 32
    assert out["filled"] == 8 == len(out["rows"]) and out["waves"] == 8
    assert all(r["bid"] is not None and r["delta"] is not None for r in out["rows"])
    assert not ib.active                                               # every line released

    ib2 = FakeIB()
    full = run(th.quote(ib2, "XYZ", small_window(ib2), max_lines=4, wait=1.0, deadline=60.0))
    assert full["partial"] is False and full["filled"] == 32           # time enough: complete


def test_quote_deadline_cuts_the_open_window_and_releases_it(monkeypatch):
    # a feed that never answers: without a deadline the wave would wait 5 s per type
    monkeypatch.setattr(th, "QUOTE_STALL", 10.0)
    ib = FakeIB(prices_on=(), greeks_on=())
    w = small_window(ib)
    import time as _time
    t0 = _time.monotonic()
    out = run(th.quote(ib, "XYZ", w, max_lines=10, wait=5.0, deadline=0.15))
    assert _time.monotonic() - t0 < 2.0
    assert out["partial"] is True and out["rows"] == [] and out["attempted"] == 10
    assert len(ib.mdt_calls) == 1                                      # no further type probed
    assert ib.max_active == 10 and not ib.active


def test_quote_deadline_zero_reads_nothing_and_fetch_passes_what_is_left():
    ib = FakeIB()
    out = run(th.quote(ib, "XYZ", small_window(ib), deadline=0))
    assert out["partial"] is True and out["rows"] == [] and out["requested"] == 0
    assert not ib.subs                                                 # not one line taken
    ib2 = FakeIB(spot=100.0)
    spec = {"iv_hint": 0.01, "expiries": ["2026-10-16"], "min_side": 3, "max_side": 3}
    got = run(th.fetch(ib2, "XYZ", spec, wait=1.0, today=TODAY, deadline=0))
    assert got["partial"] is True and got["rows"] == [] and got["spot"] == 100.0
    assert run(th.fetch(FakeIB(), "XYZ", spec, wait=1.0, today=TODAY, deadline=60))["partial"] is False


def test_a_cancellation_during_the_release_cannot_leak_lines(monkeypatch):
    """#20: the release used to pace each cancel - an ``await``, so a cancellation (the
    connector's timeout) landing there left the rest of the wave subscribed for good.
    Here every ``_pace`` after the data has arrived raises CancelledError: the reads
    must still finish with every line released, i.e. the release never awaits."""
    armed = {"on": False}
    real_pace, real_fill, real_price = th._pace, th._await_fill, th._market_price

    async def pace(n=1):
        if armed["on"]:
            raise asyncio.CancelledError()
        await real_pace(n)

    async def fill(tickers, wait):
        await real_fill(tickers, wait)
        armed["on"] = True

    def price(t):
        p = real_price(t)
        if p is not None:
            armed["on"] = True
        return p

    monkeypatch.setattr(th, "_pace", pace)
    monkeypatch.setattr(th, "_await_fill", fill)
    monkeypatch.setattr(th, "_market_price", price)

    ib = FakeIB()
    w = small_window(ib)
    out = run(th.quote(ib, "XYZ", w, max_lines=60, wait=1.0, spot=100.0))
    assert out["filled"] == 32 and not ib.active

    armed["on"] = False
    ib2 = FakeIB(spot=250.0)
    assert run(th.spot(ib2, "XYZ"))["spot"] == 250.0
    assert not ib2.active


def test_released_messages_are_charged_to_the_bucket(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(th, "_now", clock.now)
    monkeypatch.setattr(th, "_sleep", clock.sleep)
    monkeypatch.setattr(th, "MSG_RATE", 40)
    th._bucket.reset()
    th._bucket.charge(60)                          # 60 cancels sent at once, no wait
    assert clock.slept == []
    run(th._pace(1))                               # the next paced message waits for them
    assert clock.slept == [pytest.approx(21 / 40)]


def test_a_cancelled_history_wait_gives_its_slot_back(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(th, "_now", clock.now)
    monkeypatch.setattr(th, "HIST_GAP", 11.0)
    run(th._hist_slot())                           # the first goes at once
    assert th._hist_next[0] == pytest.approx(1011.0)

    async def cancelled(s):
        raise asyncio.CancelledError()

    monkeypatch.setattr(th, "_sleep", cancelled)
    with pytest.raises(asyncio.CancelledError):
        run(th._hist_slot())                       # booked 1011, cancelled while waiting
    assert th._hist_next[0] == pytest.approx(1011.0)   # ... and handed back


def test_unknown_strikes_are_remembered_for_three_days(monkeypatch):
    assert th.NEG_TTL == 3 * 86400.0
    offset = [0.0]
    import time as _time
    monkeypatch.setattr(th, "_now", lambda: _time.monotonic() + offset[0])
    listed = {e: set(UNION) for e in EXPS}
    listed["20261120"] = {k for k in UNION if k % 5 == 0}
    ib = FakeIB(listed=listed)
    w = th.plan(defs_for(ib), spot=100, iv_hint=0.01, today=TODAY, expiries=["2026-11-20"],
                min_side=10, max_side=10)
    first = run(th.quote(ib, "XYZ", w, wait=1.0))
    assert first["unknown"] > 0
    ib.qualify_sizes.clear()
    offset[0] = 2 * 86400.0                        # two days later: still remembered
    run(th.quote(ib, "XYZ", w, wait=1.0))
    assert ib.qualify_sizes == []
    offset[0] = 3 * 86400.0 + 1                    # past three days: asked again
    again = run(th.quote(ib, "XYZ", w, wait=1.0))
    assert sum(ib.qualify_sizes) == first["unknown"] and again["unknown"] == first["unknown"]


# ───────────────────────────────────────────────────────────── history / account
class _Bar:
    def __init__(self, date, close, o=None, h=None, low=None, v=1000.0):
        self.date, self.close, self.open, self.high, self.low, self.volume = date, close, o, h, low, v


def test_daily_bars_shape_order_and_request():
    bars = [_Bar(_dt.date(2026, 10, 8), 101.0, 100.0, 102.0, 99.5, 2_000_000.0),
            _Bar("20261006", 99.0, 98.0, 99.5, 97.5, 1_500_000.0),
            _Bar(_dt.date(2026, 10, 7), 100.0, 99.0, 100.5, 98.5, NAN),
            _Bar(_dt.date(2026, 10, 5), 0.0)]                          # no close: skipped
    ib = FakeIB(bars=bars)
    out = run(th.daily_bars(ib, "XYZ"))
    assert [b["on"] for b in out] == ["2026-10-06", "2026-10-07", "2026-10-08"]
    assert all(set(b) == {"on", "open", "high", "low", "close", "volume"} for b in out)
    assert out[0] == {"on": "2026-10-06", "open": 98.0, "high": 99.5, "low": 97.5, "close": 99.0,
                      "volume": 1_500_000.0}
    assert out[1]["volume"] is None
    call = ib.hist_calls[0]
    assert (call["durationStr"], call["barSizeSetting"], call["whatToShow"], call["useRTH"],
            call["formatDate"], call["endDateTime"]) == ("2 Y", "1 day", "TRADES", True, 1, "")
    run(th.daily_bars(ib, "XYZ", "5 D"))
    assert ib.hist_calls[-1]["durationStr"] == "5 D"


def test_iv_history_percent_oldest_first_positive_only():
    ivbars = [_Bar(_dt.date(2026, 10, 8), 0.4612), _Bar(_dt.date(2026, 10, 6), 0.45),
              _Bar(_dt.date(2026, 10, 7), 0.0), _Bar(_dt.date(2026, 10, 2), -1.0)]
    ib = FakeIB(ivbars=ivbars)
    out = run(th.iv_history(ib, "XYZ"))
    assert out == [{"on": "2026-10-06", "iv": 45.0}, {"on": "2026-10-08", "iv": 46.12}]
    call = ib.hist_calls[0]
    assert (call["durationStr"], call["whatToShow"], call["useRTH"]) == ("1 Y", "OPTION_IMPLIED_VOLATILITY", True)


class _Clock:
    """A fake monotonic clock whose sleep advances time without waiting."""

    def __init__(self):
        self.t, self.slept = 1000.0, []

    def now(self):
        return self.t

    async def sleep(self, s):
        self.slept.append(s)
        self.t += s


def test_historical_requests_are_spaced_hist_gap_apart(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(th, "_now", clock.now)
    monkeypatch.setattr(th, "_sleep", clock.sleep)
    monkeypatch.setattr(th, "HIST_GAP", 11.0)
    ib = FakeIB(bars=[_Bar(_dt.date(2026, 10, 8), 1.0)], ivbars=[_Bar(_dt.date(2026, 10, 8), 0.3)])
    run(th.daily_bars(ib, "XYZ"))
    assert clock.slept == []                                           # the first goes at once
    clock.t += 3.0
    run(th.iv_history(ib, "XYZ"))
    assert clock.slept == [pytest.approx(8.0)]                         # 11 s after the first
    clock.t += 30.0
    run(th.daily_bars(ib, "XYZ"))
    assert len(clock.slept) == 1


def test_message_pacing_bucket(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(th, "_now", clock.now)
    monkeypatch.setattr(th, "_sleep", clock.sleep)
    monkeypatch.setattr(th, "MSG_RATE", 40)
    th._bucket.reset()

    async def burst(n):
        for _ in range(n):
            await th._pace(1)

    run(burst(100))                                    # 40 at once, then 40 per second
    assert sum(clock.slept) == pytest.approx(60 / 40)
    assert (clock.t - 1000.0) == pytest.approx(1.5)
    monkeypatch.setattr(th, "MSG_RATE", 0)
    clock.slept.clear()
    run(burst(500))
    assert clock.slept == []


def test_account_net_liquidation():
    av = types.SimpleNamespace
    ib = FakeIB(summary=[av(account="U1", tag="TotalCashValue", value="5000", currency="USD"),
                         av(account="U1", tag="NetLiquidation", value="123456.78", currency="USD")])
    assert run(th.account(ib)) == {"net_liquidation": 123456.78, "currency": "USD"}
    assert run(th.account(FakeIB(summary=[]))) == {"net_liquidation": None, "currency": None}


# ───────────────────────────────────────────────────────────── row edge cases
def test_row_drops_sentinels_and_crossed_quotes():
    t = FakeTicker(None)
    t.bid, t.ask, t.last, t.close = 2.10, 2.00, NAN, 1.95             # crossed
    t.modelGreeks = types.SimpleNamespace(impliedVol=-1, delta=-2, gamma=-2, theta=-2.0, vega=-2,
                                          undPrice=-1)
    r = th._row(t, "2026-10-16", "P", 100)
    assert r["bid"] is None and r["ask"] is None and r["mid"] is None and r["last"] == 1.95
    assert all(r[k] is None for k in ("iv", "delta", "gamma", "theta", "vega", "und_price"))
    t2 = FakeTicker(None)
    t2.bid, t2.ask, t2.callOpenInterest, t2.putOpenInterest = 0.0, 0.05, 40.0, NAN
    r2 = th._row(t2, "2026-10-16", "P", 60)
    assert r2["bid"] is None and r2["ask"] == 0.05 and r2["oi"] == 40   # own side missing -> other
