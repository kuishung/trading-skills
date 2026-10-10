"""Massive -> the Options data layer (``app/services/opt_massive.py``, OPTIONS_V2_DESIGN.md
§13.3): the model price, the spot estimate, the window, the chain ingest into
``opt_store``, the IV30 history backfill and the daily bar top-up.

No network: the Massive client runs on an ``httpx.MockTransport`` fake server built from
the Black-Scholes fixtures (``chain_bs`` / ``bs_greeks`` / ``bars_synth``) and a fake
clock. DB tests run on a fresh SQLite file migrated to head (conftest).
"""
from __future__ import annotations

import copy
import datetime as _dt
import importlib.util
import math
import re
from pathlib import Path

import httpx
import pytest

from app import models
from app.services import massive, opt_massive, opt_store
from app.services.black_scholes import black_scholes
from app.services.opt_constants import RISK_FREE

from .fixtures.options import bars_synth, bs_greeks, chain_bs

KEY = "unit-test-key-0123456789abcdef"     # not a real key
BASE = "https://api.massive.test"

TODAY = "2026-10-05"                          # a Monday
NOW = _dt.datetime(2026, 10, 5, 18, 0)         # 14:00 ET, in session (naive UTC)
FEED_T = _dt.datetime(2026, 10, 5, 17, 45)     # the delayed feed's time
UND_T = _dt.datetime(2026, 10, 5, 17, 44, 30)
EXPIRIES = ["2026-10-09", "2026-10-16", "2026-10-23", "2026-11-20", "2026-12-11",
            "2026-12-18", "2027-01-15"]
STRIKES = [60.0 + 2.5 * i for i in range(33)]          # 60 .. 140
SPOT = 100.0
IV = 0.30

BRIDGE = Path(__file__).resolve().parent.parent / "bridge" / "th_ibkr.py"


def _load_th_ibkr():
    spec = importlib.util.spec_from_file_location("th_ibkr_for_opt_massive", BRIDGE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _clean_state():
    opt_store.reset_state()
    yield
    opt_store.reset_state()


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def ns(dt: _dt.datetime) -> int:
    return int(dt.replace(tzinfo=_dt.timezone.utc).timestamp()) * 1_000_000_000


def make_client(handler, **kw):
    clock = FakeClock()
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return massive.Client(api_key=KEY, base_url=BASE, http=http, sleep=clock.sleep, now=clock.now, **kw)


# ───────────────────────────────────────────── a Massive snapshot from chain_bs

def massive_results(*, und_price=None, quotes=None, traded=True, spot=SPOT, iv=IV):
    """Options Starter results for a Black-Scholes chain: day close = model price, IV and
    greeks from the model (no greeks on deep ITM), NO last_quote unless ``quotes`` adds
    one, a distinct feed stamp per contract. Returns (results, {key: stamp})."""
    ch = chain_bs(spot, iv, EXPIRIES, STRIKES, today=TODAY, symbol="SYN")
    out, stamps = [], {}
    for i, ((exp, right, k), leg) in enumerate(sorted(ch["legs"].items())):
        stamp = FEED_T - _dt.timedelta(seconds=i)
        price = round(leg["theo"], 2)
        r = {"details": {"contract_type": "call" if right == "C" else "put",
                         "exercise_style": "american", "expiration_date": exp,
                         "shares_per_contract": 100, "strike_price": k,
                         "ticker": massive.option_ticker("SYN", exp, right, k)},
             "implied_volatility": leg["iv"],
             "open_interest": int(leg["open_interest"]),
             "day": {"close": price, "open": price, "high": price, "low": price, "vwap": price,
                     "previous_close": price, "change": 0.0, "change_percent": 0.0,
                     "volume": int(leg["volume"]) if traded else 0, "last_updated": ns(stamp)},
             "underlying_asset": {"ticker": "SYN", "change_to_break_even": 0.0, "timeframe": "DELAYED"}}
        if abs(leg["delta"]) < 0.97:
            r["greeks"] = {"delta": leg["delta"], "gamma": leg["gamma"], "theta": leg["theta"],
                           "vega": leg["vega"]}
        if und_price is not None:
            r["underlying_asset"].update(price=und_price, last_updated=ns(UND_T))
        q = (quotes or {}).get((exp, right, k))
        if q is not None:
            r["last_quote"] = q
            stamp = max(stamp, massive.ts_to_utc(q["last_updated"]))
        out.append(r)
        stamps[(exp, right, k)] = stamp
    return out, stamps


def snapshot_server(results, *, page=250, seen=None):
    seen = seen if seen is not None else []

    def handler(request):
        seen.append(request)
        start = int(dict(request.url.params).get("cursor", "0"))
        chunk = results[start:start + page]
        body = {"status": "OK", "results": chunk}
        if start + page < len(results):
            body["next_url"] = "%s/v3/snapshot/options/SYN?cursor=%d" % (BASE, start + page)
        return httpx.Response(200, json=body)

    return handler, seen


def parsed_rows(results):
    return [r for r in (massive.parse_snapshot_result(x) for x in results) if r is not None]


# ───────────────────────────────────────────── model price

def test_model_price_is_black_scholes_at_the_house_rate():
    for S, K, dte, iv, right in [(100, 100, 30, 0.30, "C"), (100, 90, 45, 0.25, "P"),
                                 (350.5, 400, 200, 0.46, "call"), (20, 17.5, 10, 0.8, "put")]:
        kind = "call" if str(right).upper().startswith("C") else "put"
        want = round(black_scholes(S, K, dte / 365.0, RISK_FREE, iv, kind).price, 2)
        assert opt_massive.model_price(S, K, dte, iv, right) == want
    # DTE 0 is priced at half a day, not at intrinsic
    half = round(black_scholes(100, 100, 0.5 / 365.0, RISK_FREE, 0.3, "call").price, 2)
    assert opt_massive.model_price(100, 100, 0, 0.3, "C") == half > 0
    assert opt_massive.model_price(None, 100, 30, 0.3, "C") is None
    assert opt_massive.model_price(100, 100, 30, None, "C") is None
    assert opt_massive.model_price(100, 100, 30, 0.3, "X") is None
    # agrees with the fixture's own Black-Scholes
    g = bs_greeks(100, 105, 30 / 365.0, 0.3, "P", RISK_FREE)
    assert opt_massive.model_price(100, 105, 30, 0.3, "P") == round(g["price"], 2)


# ───────────────────────────────────────────── spot

def test_estimate_spot_three_paths():
    res, _ = massive_results(und_price=100.37)
    snap = {"rows": parsed_rows(res), "underlying_price": 100.37}
    assert opt_massive.estimate_spot(snap, stored_close=98.0, today=TODAY) == (100.37, "massive")

    res, _ = massive_results()
    snap = {"rows": parsed_rows(res), "underlying_price": None}
    spot, kind = opt_massive.estimate_spot(snap, stored_close=98.0, today=TODAY)
    assert kind == "parity" and spot == pytest.approx(SPOT, abs=0.03)

    # no deltas at all: the 50-delta strike is found from the smallest |call - put|
    rows = [dict(r, delta=None) for r in parsed_rows(res)]
    spot, kind = opt_massive.estimate_spot({"rows": rows}, today=TODAY)
    assert kind == "parity" and spot == pytest.approx(SPOT, abs=0.03)

    # nothing traded today: parity has no prices -> the stored close
    res, _ = massive_results(traded=False)
    snap = {"rows": parsed_rows(res)}
    assert opt_massive.estimate_spot(snap, stored_close=98.0, today=TODAY) == (98.0, "close")
    assert opt_massive.estimate_spot(snap, stored_close=None, today=TODAY) == (None, "none")
    assert opt_massive.estimate_spot({}, today=TODAY) == (None, "none")


FRI_T = _dt.datetime(2026, 10, 2, 19, 30)      # Friday 15:30 ET - the last session's bars


def test_parity_pairs_only_legs_from_the_newest_session():
    # Monday: the stock is at 104 (Friday close 100). Massive's ``day`` is a contract's most
    # recent bar, so a put that has not printed today still carries Friday's close
    monday = parsed_rows(massive_results(spot=104.0)[0])
    friday = {(r["expiry"], r["right"], r["strike"]): dict(r, last_updated=FRI_T)
              for r in parsed_rows(massive_results(spot=100.0)[0])}

    def mix(stale):
        return [friday[(r["expiry"], r["right"], r["strike"])] if stale(r) else r for r in monday]

    # every call has printed today, no put has: no pair is from one session -> the stored close
    rows = mix(lambda r: r["right"] == "P")
    assert opt_massive.estimate_spot({"rows": rows}, stored_close=100.0, today=TODAY) == (100.0, "close")
    # the nearest expiry's puts are still Friday's; the next expiry has both legs today
    rows = mix(lambda r: r["right"] == "P" and r["expiry"] == "2026-10-16")
    spot, kind = opt_massive.estimate_spot({"rows": rows}, stored_close=100.0, today=TODAY)
    assert kind == "parity" and spot == pytest.approx(104.0, abs=0.03)
    # before the first print of the day every bar is Friday's: one session, parity reads it
    spot, kind = opt_massive.estimate_spot({"rows": list(friday.values())}, stored_close=99.0, today=TODAY)
    assert kind == "parity" and spot == pytest.approx(100.0, abs=0.03)
    # no stamps: the session cannot be told -> the stored close
    rows = [dict(r, last_updated=None) for r in monday]
    assert opt_massive.estimate_spot({"rows": rows}, stored_close=100.0, today=TODAY) == (100.0, "close")


def test_parity_uses_an_expiry_at_least_a_week_out():
    res, _ = massive_results(spot=100.0)
    rows = parsed_rows(res)
    # wreck the closes of the 4-DTE weekly: parity must not read it
    for r in rows:
        if r["expiry"] == "2026-10-09":
            r["day_close"] = 50.0 if r["right"] == "C" else 0.05
    spot, kind = opt_massive.estimate_spot({"rows": rows}, today=TODAY)
    assert kind == "parity" and spot == pytest.approx(100.0, abs=0.03)


# ───────────────────────────────────────────── the window

@pytest.mark.parametrize("spot,iv_hint", [(100.0, 0.30), (103.7, 45.0), (88.2, None), (100.0, 0.05)])
def test_window_matches_th_ibkr_plan(spot, iv_hint):
    th = _load_th_ibkr()
    res, _ = massive_results()
    rows = parsed_rows(res)
    defs = {"expiries": EXPIRIES, "strikes": STRIKES}
    plan = th.plan(defs, spot=spot, iv_hint=iv_hint, today=TODAY)
    want = {(p["expiry"], k) for p in plan for k in p["strikes"]}
    kept = opt_massive.window(rows, spot, today=TODAY, iv_hint=iv_hint)
    got = {(r["expiry"], r["strike"]) for r in kept}
    assert got == want
    # both rights of every kept strike, nothing else
    assert len(kept) == 2 * len(want)
    assert "2026-12-11" not in {e for e, _ in got}          # a weekly past 63 DTE
    assert "2027-01-15" in {e for e, _ in got}              # a monthly far out


def test_window_without_spot_keeps_the_expiry_rule_only():
    res, _ = massive_results()
    rows = parsed_rows(res)
    kept = opt_massive.window(rows, None, today=TODAY)
    assert {r["expiry"] for r in kept} == set(EXPIRIES) - {"2026-12-11"}
    assert len(kept) == len([r for r in rows if r["expiry"] != "2026-12-11"])


def test_window_drops_expired():
    res, _ = massive_results()
    rows = parsed_rows(res)
    kept = opt_massive.window(rows, SPOT, today="2026-10-12", iv_hint=IV)
    assert "2026-10-09" not in {r["expiry"] for r in kept}


# ───────────────────────────────────────────── ingest

def _quote(bid, ask, t):
    return {"bid": bid, "ask": ask, "bid_size": 5, "ask_size": 9, "midpoint": (bid + ask) / 2,
            "last_updated": ns(t), "timeframe": "DELAYED"}


def _quotes(db, sym="SYN"):
    return {(q.expiry, q.right, q.strike): q
            for q in db.query(models.OptQuote).filter(models.OptQuote.symbol == sym)}


def test_ingest_symbol_end_to_end_into_opt_store(db):
    qkey = ("2026-11-20", "P", 95.0)
    q_t = FEED_T + _dt.timedelta(seconds=30)
    res, stamps = massive_results(quotes={qkey: _quote(1.10, 1.30, q_t)})
    handler, seen = snapshot_server(res, page=250)
    client = make_client(handler)

    out = opt_massive.ingest_symbol(db, client, "syn", today=TODAY, now=NOW)

    # the read: every page, the date range, no strike range (no spot known yet)
    assert out["pages"] == len(seen) == math.ceil(len(res) / 250) > 1
    p0 = dict(seen[0].url.params)
    assert p0["expiration_date.gte"] == TODAY and p0["expiration_date.lte"] == "2029-10-09"
    assert "strike_price.gte" not in p0
    assert all(KEY not in str(r.url) for r in seen)

    # the spot: no underlying price on the plan -> put-call parity
    assert out["spot_kind"] == "parity" and out["spot"] == pytest.approx(SPOT, abs=0.03)
    spot = out["spot"]

    # what was stored = the window, every row Massive / delayed, stamped by the feed
    want = opt_massive.window(parsed_rows(res), spot, today=TODAY, iv_hint=IV)
    stored = _quotes(db)
    assert out["stored"] == len(stored) == len(want)
    assert out["rows"] == len(res)
    assert {q.source for q in stored.values()} == {"massive"}
    assert {q.mdt for q in stored.values()} == {"delayed"}
    assert not any(k[0] == "2026-12-11" for k in stored)
    assert ("2026-10-09", "C", 60.0) not in stored
    assert out["expiries"] == len({k[0] for k in stored}) == 6

    # no quote (Starter): bid / ask empty, mid = the model price from the contract's IV
    k = ("2026-11-20", "C", 100.0)
    q = stored[k]
    assert q.bid is None and q.ask is None
    assert q.mid == opt_massive.model_price(spot, 100.0, 46, q.iv, "C")
    assert q.iv == pytest.approx(IV) and q.oi > 0 and q.last is not None
    # stamped with the READ time minus the 15-min delay, not the contract's own (last-trade)
    # day.last_updated - a thin strike must not look hours old when its IV is current
    assert q.as_of == NOW - _dt.timedelta(seconds=opt_massive.DELAY_S) != stamps[k]
    assert q.und_price == pytest.approx(spot)

    # a contract the feed quoted: bid / ask kept, mid = their midpoint, stamped by the quote
    q = stored[qkey]
    assert (q.bid, q.ask, q.bid_size, q.ask_size) == (1.10, 1.30, 5, 9)
    assert q.mid == pytest.approx(1.20)
    assert q.as_of == q_t

    # the spot and today's IV30
    u = opt_store.underlying(db, "SYN")
    assert u["spot"] == pytest.approx(spot)
    assert u["spot_source"] == "massive" and u["spot_mdt"] == "delayed"
    assert u["spot_as_of"] == NOW - _dt.timedelta(seconds=opt_massive.DELAY_S)   # a parity spot: the read time - 15 min
    assert out["iv30"] == pytest.approx(IV * 100, abs=0.05)
    day = (db.query(models.OptUnderlyingDaily)
             .filter(models.OptUnderlyingDaily.symbol == "SYN", models.OptUnderlyingDaily.on == TODAY)
             .one())
    assert day.iv30 == pytest.approx(out["iv30"]) and day.source == "massive"
    assert u["iv30"] == pytest.approx(out["iv30"])

    # the refresh log row the freshness badge reads
    log = db.query(models.OptRefreshLog).filter(models.OptRefreshLog.symbol == "SYN").one()
    assert log.kind == "cycle" and log.source == "massive" and log.n_contracts == out["stored"]
    assert log.as_of == max(stamps[k] for k in stored)


def test_ingest_with_underlying_price_then_a_known_spot_narrows_the_read(db):
    res, _ = massive_results(und_price=100.0)
    handler, seen = snapshot_server(res, page=1000)
    client = make_client(handler)
    out = opt_massive.ingest_symbol(db, client, "SYN", today=TODAY, now=NOW, kind="manual")
    assert out["spot_kind"] == "massive" and out["spot"] == 100.0 and out["pages"] == 1
    u = opt_store.underlying(db, "SYN")
    assert u["spot"] == 100.0 and u["spot_as_of"] == UND_T and u["spot_mdt"] == "delayed"
    assert db.query(models.OptRefreshLog).one().kind == "manual"

    # second read: the stored spot bounds the snapshot's strikes at 0.3x - 3x
    opt_massive.ingest_symbol(db, client, "SYN", today=TODAY, now=NOW + _dt.timedelta(minutes=15))
    p = dict(seen[-1].url.params)
    assert p["strike_price.gte"] == "30" and p["strike_price.lte"] == "300"


def test_ingest_falls_back_to_the_stored_close(db):
    opt_store.upsert_daily(db, "SYN", bars=[{"on": "2026-10-02", "open": 99, "high": 100.5,
                                             "low": 98.5, "close": 99.5, "volume": 1e6}],
                           today=TODAY, now=NOW)
    res, _ = massive_results(traded=False)
    handler, seen = snapshot_server(res, page=1000)
    out = opt_massive.ingest_symbol(db, make_client(handler), "SYN", today=TODAY, now=NOW)
    assert out["spot_kind"] == "close" and out["spot"] == 99.5
    p = dict(seen[0].url.params)
    assert p["strike_price.gte"] == "29.85" and p["strike_price.lte"] == "298.5"
    u = opt_store.underlying(db, "SYN")
    assert u["spot"] == 99.5 and u["spot_mdt"] == "eod"
    assert u["spot_as_of"] == _dt.datetime(2026, 10, 2, 20, 0)         # 16:00 EDT
    # the model mids use that spot
    q = _quotes(db)[("2026-11-20", "C", 100.0)]
    assert q.mid == opt_massive.model_price(99.5, 100.0, 46, q.iv, "C")


def test_ingest_before_the_open_files_iv_under_the_last_session(db):
    res, _ = massive_results(und_price=100.0)
    handler, _ = snapshot_server(res, page=1000)
    pre_open = _dt.datetime(2026, 10, 5, 12, 0)                        # 08:00 ET Monday
    out = opt_massive.ingest_symbol(db, make_client(handler), "SYN", today=TODAY, now=pre_open)
    days = [r.on for r in db.query(models.OptUnderlyingDaily).filter(
        models.OptUnderlyingDaily.symbol == "SYN", models.OptUnderlyingDaily.iv30.isnot(None))]
    assert out["iv30"] is not None and days == ["2026-10-02"]           # Friday's session


def test_standard_rows_drops_non_standard_contracts():
    adj = {"expiry": "2026-11-20", "right": "C", "strike": 100.0, "iv": 0.9, "multiplier": 100,
           "ticker": "O:BRKB1261120C00100000"}                       # an adjusted root
    std = dict(adj, iv=0.3, ticker=massive.option_ticker("BRK-B", "2026-11-20", "C", 100.0))
    mini = dict(adj, strike=105.0, multiplier=10, ticker=None)       # 10 shares
    plain = dict(adj, strike=110.0, multiplier=None, ticker=None)    # nothing said: kept
    assert opt_massive.standard_rows("BRK-B", [adj, std, mini, plain]) == [std, plain]
    assert opt_massive.standard_rows("BRK-B", [std, adj]) == [std]
    assert opt_massive.standard_rows("BRK-B", [adj, dict(adj, iv=0.5)]) == [adj]    # neither: the first


def test_ingest_keeps_only_standard_contracts(db):
    res, _ = massive_results()
    by_key = {(r["details"]["expiration_date"], r["details"]["contract_type"][0].upper(),
               r["details"]["strike_price"]): r for r in res}
    extra = []
    # an adjusted root (after a spin-off) at the same expiry / right / strike as standard
    # contracts, later in the pages, with another deliverable's IV and prices
    for exp, k in (("2026-10-16", 100.0), ("2026-10-16", 102.5), ("2026-11-20", 100.0)):
        for rt in ("C", "P"):
            x = copy.deepcopy(by_key[(exp, rt, k)])
            x["details"]["ticker"] = massive.option_ticker("SYN1", exp, rt, k)
            x["implied_volatility"] = 0.9
            x["day"]["close"] = round(x["day"]["close"] + (6.0 if rt == "C" else 0.5), 2)
            extra.append(x)
    # a mini (10 shares) at a strike the standard chain does not list
    mini = copy.deepcopy(by_key[("2026-11-20", "C", 100.0)])
    mini["details"].update(strike_price=101.0, shares_per_contract=10, ticker="O:SYN7261120C00101000")
    extra.append(mini)
    handler, _ = snapshot_server(res + extra, page=1000)

    out = opt_massive.ingest_symbol(db, make_client(handler), "SYN", today=TODAY, now=NOW)

    assert out["rows"] == len(res) + len(extra)
    assert out["spot_kind"] == "parity" and out["spot"] == pytest.approx(SPOT, abs=0.03)
    stored = _quotes(db)
    for k in (("2026-10-16", "C", 100.0), ("2026-10-16", "P", 102.5), ("2026-11-20", "C", 100.0)):
        assert stored[k].iv == pytest.approx(IV), k
    assert ("2026-11-20", "C", 101.0) not in stored


def test_ingest_read_failure_writes_nothing(db):
    def handler(request):
        return httpx.Response(403, json={"status": "NOT_AUTHORIZED"})

    with pytest.raises(massive.MassiveError) as ei:
        opt_massive.ingest_symbol(db, make_client(handler), "SYN", today=TODAY, now=NOW)
    assert ei.value.kind == "plan"
    assert db.query(models.OptQuote).count() == 0
    assert db.query(models.OptRefreshLog).count() == 0


def test_ingest_empty_chain(db):
    handler, _ = snapshot_server([], page=250)
    out = opt_massive.ingest_symbol(db, make_client(handler), "NOPT", today=TODAY, now=NOW)
    assert out["stored"] == 0 and out["spot"] is None and out["spot_kind"] == "none"
    assert out["iv30"] is None


# ───────────────────────────────────────────── history

HIST_DAY = "2026-10-02"                         # a Friday


def _sigma(i: int) -> float:
    return 0.25 + 0.10 * math.sin(2 * math.pi * i / 90.0)


def history_server(bars, listed, seen, expiry_ok=None, split=None):
    """Stock bars from ``bars``; option bars priced by Black-Scholes at the day's known
    IV (``_sigma`` of the session index) on the stock's close, for listed strikes (and
    expiries, when ``expiry_ok`` says which). ``split`` = ``(date, ratio)``: ``bars`` are
    split-adjusted and before ``date`` the stock really traded at ``ratio`` x them - the
    stock bars come back that way when a request says ``adjusted=false``, and the option
    contracts (listed and traded at the real price) are priced off it."""
    def factor(d: str) -> float:
        return split[1] if split is not None and d < split[0] else 1.0

    closes = {b["time"]: b["close"] * factor(b["time"]) for b in bars}     # the real (unadjusted) closes
    days = sorted(closes)
    index = {d: i for i, d in enumerate(days)}
    by_day = {b["time"]: b for b in bars}

    def t_ms(d: str) -> int:
        x = _dt.date.fromisoformat(d)
        return int(_dt.datetime(x.year, x.month, x.day, 12, 0, tzinfo=_dt.timezone.utc).timestamp() * 1000)

    def handler(request):
        seen.append(request)
        parts = request.url.path.split("/")
        ticker, frm, to = parts[4], parts[8], parts[9]
        out = []
        m = re.match(r"^O:([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$", ticker)
        if m is None:
            raw = dict(request.url.params).get("adjusted") == "false"
            for d in days:
                if frm <= d <= to:
                    b = by_day[d]
                    f = factor(d) if raw else 1.0
                    out.append({"t": t_ms(d), "o": round(b["open"] * f, 2), "h": round(b["high"] * f, 2),
                                "l": round(b["low"] * f, 2), "c": round(b["close"] * f, 2),
                                "v": b["volume"]})
        else:
            exp = _dt.date(2000 + int(m.group(2)), int(m.group(3)), int(m.group(4)))
            k = int(m.group(6)) / 1000.0
            kind = "call" if m.group(5) == "C" else "put"
            if listed(k) and (expiry_ok is None or expiry_ok(exp)):
                for d in days:
                    dte = (exp - _dt.date.fromisoformat(d)).days
                    if not (frm <= d <= to) or dte <= 0:
                        continue
                    p = round(black_scholes(closes[d], k, dte / 365.0, RISK_FREE, _sigma(index[d]),
                                            kind).price, 2)
                    if p >= 0.01:
                        out.append({"t": t_ms(d), "o": p, "h": p, "l": p, "c": p, "v": 10})
        return httpx.Response(200, json={"status": "OK", "results": out})

    return handler, index


@pytest.mark.parametrize("listed_name,listed", [
    ("every 2.5", lambda k: abs(k / 2.5 - round(k / 2.5)) < 1e-9),
    ("only every 5", lambda k: abs(k / 5.0 - round(k / 5.0)) < 1e-9),
])
def test_backfill_recovers_the_iv30_series(db, listed_name, listed):
    bars = bars_synth("range", n=520, start=150.0, seed=11, end=HIST_DAY)
    seen: list = []
    handler, index = history_server(bars, listed, seen)
    client = make_client(handler)

    out = opt_massive.backfill_history(db, client, "SYN", today=HIST_DAY, now=NOW)

    assert out["bars"] == 520
    assert out["requests"] == len(seen)
    # ~13 monthly expiries x (1-6 strikes x call + put) + the stock: inside the
    # contract's budget of ~150-300 per ticker for 260 sessions, never above it
    assert 2 * 13 < out["requests"] <= 300, (listed_name, out["requests"])
    assert out["iv_points"] >= 230                       # 260 sessions, a few calendar gaps
    assert out["history_done"] is True
    pts = (db.query(models.OptUnderlyingDaily)
             .filter(models.OptUnderlyingDaily.symbol == "SYN",
                     models.OptUnderlyingDaily.iv30.isnot(None))
             .order_by(models.OptUnderlyingDaily.on).all())
    assert len(pts) == out["iv_points"]
    sessions = sorted(index)[-260:]
    assert pts[0].on >= sessions[0] and pts[-1].on <= sessions[-1]
    worst = max(abs(p.iv30 - _sigma(index[p.on]) * 100.0) for p in pts)
    assert worst < 1.0, worst                            # within a vol point, every day
    u = opt_store.underlying(db, "SYN")
    assert u["history_done"] is True
    assert u["iv_n"] == min(252, len(pts)) and u["iv_rank"] is not None
    assert u["hv20"] is not None and u["atr14"] is not None
    # the stock twice (adjusted bars to file, then the IV window's unadjusted closes), then
    # only option contracts of monthly expiries
    tickers = [r.url.path.split("/")[4] for r in seen]
    assert tickers[:2] == ["SYN", "SYN"] and all(t.startswith("O:SYN") for t in tickers[2:])
    assert [dict(r.url.params).get("adjusted") for r in seen[:2]] == ["true", "false"]
    for t in tickers[2:]:
        m = re.match(r"^O:SYN(\d{2})(\d{2})(\d{2})", t)
        e = _dt.date(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3)))
        assert opt_massive.is_monthly(e) or e == opt_massive.monthly_expiry(e.year, e.month)


def test_backfill_history_done_needs_enough_points(db):
    # a young listing: 15 bars -> filed, not done
    bars = bars_synth("range", n=15, start=150.0, seed=3, end=HIST_DAY)
    seen: list = []
    handler, _ = history_server(bars, lambda k: True, seen)
    out = opt_massive.backfill_history(db, make_client(handler), "YNG", today=HIST_DAY, now=NOW)
    assert out["bars"] == 15 and out["iv_points"] < 20
    assert out["history_done"] is False
    assert opt_store.underlying(db, "YNG")["history_done"] is False

    # plenty of bars but no option bars at all (nothing listed) -> not done either
    bars = bars_synth("range", n=300, start=150.0, seed=4, end=HIST_DAY)
    seen = []
    handler, _ = history_server(bars, lambda k: False, seen)
    out = opt_massive.backfill_history(db, make_client(handler), "NOO", today=HIST_DAY, now=NOW)
    assert out["bars"] == 300 and out["iv_points"] == 0 and out["history_done"] is False
    u = opt_store.underlying(db, "NOO")
    assert u["history_done"] is False and u["hv20"] is not None     # the bars still count
    # the stock twice (adjusted + the IV window unadjusted); then newest expiry first, each
    # empty one costing the grid probe + 2 strikes x 2 legs (the bracketing standard strikes
    # are those two already), and three empty in a row end it
    assert out["requests"] <= 2 + 3 * (1 + 2 * 2), out["requests"]


def test_backfill_young_option_listing_keeps_its_recent_history(db):
    bars = bars_synth("range", n=520, start=150.0, seed=11, end=HIST_DAY)
    seen: list = []
    first = _dt.date(2026, 5, 15)                  # options listed from the May 2026 monthly on
    handler, index = history_server(bars, lambda k: True, seen, expiry_ok=lambda e: e >= first)
    out = opt_massive.backfill_history(db, make_client(handler), "SYN", today=HIST_DAY, now=NOW)
    pts = (db.query(models.OptUnderlyingDaily)
             .filter(models.OptUnderlyingDaily.symbol == "SYN",
                     models.OptUnderlyingDaily.iv30.isnot(None))
             .order_by(models.OptUnderlyingDaily.on).all())
    assert out["iv_points"] == len(pts) >= 100
    assert pts[0].on >= (first - _dt.timedelta(days=45)).isoformat()
    assert max(abs(p.iv30 - _sigma(index[p.on]) * 100.0) for p in pts) < 1.0
    assert out["history_done"] is True
    assert out["requests"] <= 300


def test_strike_grid_by_magnitude():
    # (finer, coarser, standard): the standard step is the base listing increment
    assert opt_massive.strike_grid(12.3) == (0.5, 1.0, 2.5)
    assert opt_massive.strike_grid(42.0) == (1.0, 2.5, 5.0)
    assert opt_massive.strike_grid(150.0) == (2.5, 5.0, 5.0)
    assert opt_massive.strike_grid(349.2) == (2.5, 5.0, 10.0)
    assert opt_massive.strike_grid(1200.0) == (5.0, 10.0, 10.0)


def _on(step):
    return lambda k: abs(k / step - round(k / step)) < 1e-9


def _iv_points(db, sym="SYN"):
    return (db.query(models.OptUnderlyingDaily)
              .filter(models.OptUnderlyingDaily.symbol == sym,
                      models.OptUnderlyingDaily.iv30.isnot(None))
              .order_by(models.OptUnderlyingDaily.on).all())


def calm_bars(mid: float, amp: float, n: int = 520, end: str = HIST_DAY) -> list[dict]:
    """A calm stock: weekday closes on a slow wave mid +/- amp (period 37 sessions)."""
    days, d = [], _dt.date.fromisoformat(end)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= _dt.timedelta(days=1)
    out, prev = [], mid
    for i, day in enumerate(reversed(days)):
        c = round(mid + amp * math.sin(2 * math.pi * i / 37.0), 2)
        out.append({"time": day.isoformat(), "open": prev, "high": round(max(prev, c) * 1.003, 2),
                    "low": round(min(prev, c) * 0.997, 2), "close": c, "volume": 1_000_000})
        prev = c
    return out


@pytest.mark.parametrize("name,mid,amp,step", [
    # closes 18.6-19.4: the $0.50 and $1 strikes it sits on are not listed; 17.5 / 20 are
    ("a ~$19 stock with only $2.50 strikes", 19.0, 0.4, 2.5),
    # closes 61.4-63.2: 61 / 62 / 63 and 62.5 are not listed; 60 / 65 are
    ("a ~$62 stock with only $5 strikes", 62.3, 0.9, 5.0),
    # closes 303-307: 302.5 / 305 / 307.5 are not listed; 300 / 310 are
    ("a ~$305 stock with only $10 strikes", 305.0, 2.0, 10.0),
    # closes 18.9-19.1 all snap to 19 on every finer grid - no probe strike, nothing to
    # step down on; only the standard strikes that bracket the closes (17.5 / 20) are listed
    ("a $19 stock pinned between two $2.50 strikes", 19.0, 0.1, 2.5),
])
def test_backfill_finds_strikes_listed_only_on_the_standard_grid(db, name, mid, amp, step):
    # the finer grids are not listed at all: the reading must step down to the standard
    # grid and the strikes that bracket the closes, not give up on the expiry
    bars = calm_bars(mid, amp)
    seen: list = []
    handler, index = history_server(bars, _on(step), seen)
    out = opt_massive.backfill_history(db, make_client(handler), "SYN", today=HIST_DAY, now=NOW)
    assert out["requests"] == len(seen) <= 300, (name, out["requests"])
    assert out["iv_points"] >= 230, (name, out["iv_points"])
    assert out["history_done"] is True
    pts = _iv_points(db)
    # the newest point is the newest session an expiry 15-45 days out speaks for - the
    # latest month is not lost
    sessions = sorted(index)[-260:]
    covered = [d for d in sessions
               if any(15 <= (opt_massive.monthly_expiry(y, m) - _dt.date.fromisoformat(d)).days <= 45
                      for y, m in ((2026, 9), (2026, 10), (2026, 11)))]
    assert pts[-1].on == covered[-1]
    errs = sorted(abs(p.iv30 - _sigma(index[p.on]) * 100.0) for p in pts)
    assert errs[len(errs) // 2] < 0.5 and errs[-1] < 2.0, (name, errs[len(errs) // 2], errs[-1])


def test_backfill_reads_iv_from_unadjusted_closes_across_a_split(db):
    # a 4:1 split three weeks ago: the expired contracts were listed and traded at the
    # pre-split price, so the IV history must be rebuilt from the unadjusted closes
    bars = bars_synth("range", n=520, start=150.0, seed=11, end=HIST_DAY)
    split = ("2026-09-14", 4.0)
    seen: list = []
    handler, index = history_server(bars, _on(2.5), seen, split=split)
    # Monday in session: Friday is the last published session
    out = opt_massive.backfill_history(db, make_client(handler), "SYN", today=TODAY, now=NOW)
    stock = [r for r in seen if not r.url.path.split("/")[4].startswith("O:")]
    assert [dict(r.url.params).get("adjusted") for r in stock] == ["true", "false"]
    assert all(r.url.path.endswith("/" + HIST_DAY) for r in stock)
    assert out["requests"] == len(seen) <= 300
    assert out["iv_points"] >= 230 and out["history_done"] is True
    pts = _iv_points(db)
    assert sum(1 for p in pts if p.on < split[0]) >= 200            # the history does not start at the split
    assert max(abs(p.iv30 - _sigma(index[p.on]) * 100.0) for p in pts) < 1.0
    # the filed bars stay split-adjusted - HV and ATR read them
    adj = {b["time"]: b["close"] for b in bars}
    for on in ("2026-08-03", "2026-09-11", "2026-09-14", HIST_DAY):
        row = (db.query(models.OptUnderlyingDaily)
                 .filter(models.OptUnderlyingDaily.symbol == "SYN", models.OptUnderlyingDaily.on == on).one())
        assert row.close == pytest.approx(adj[on])
    u = opt_store.underlying(db, "SYN")
    assert u["hv20"] is not None and u["hv20"] < 50                 # no fake 4x jump in the HV


def test_monthly_expiry_moves_to_thursday_on_a_holiday():
    assert opt_massive.monthly_expiry(2026, 10) == _dt.date(2026, 10, 16)
    assert opt_massive.monthly_expiry(2025, 4) == _dt.date(2025, 4, 17)    # Good Friday 2025-04-18
    assert opt_massive.monthly_expiry(2027, 6) == _dt.date(2027, 6, 17)    # Juneteenth observed


# ───────────────────────────────────────────── daily top-up

def test_daily_update_files_recent_bars(db):
    bars = bars_synth("range", n=40, start=150.0, seed=5, end=HIST_DAY)
    seen: list = []
    handler, _ = history_server(bars, lambda k: True, seen)
    out = opt_massive.daily_update(db, make_client(handler), "SYN", today=HIST_DAY, now=NOW)
    assert len(seen) == 1
    assert seen[0].url.path == "/v2/aggs/ticker/SYN/range/1/day/2026-09-22/2026-10-02"
    assert out["bars"] == 9                                  # business days 09-22 .. 10-02
    rows = db.query(models.OptUnderlyingDaily).filter(models.OptUnderlyingDaily.symbol == "SYN").all()
    assert len(rows) == 9 and all(r.source == "massive" for r in rows)
    assert opt_store.underlying(db, "SYN")["bars_as_of"] is not None
    assert out["complete"] is True


def test_published_session():
    ps = opt_massive.published_session
    mon = _dt.date(2026, 10, 5)
    assert ps(mon, _dt.datetime(2026, 10, 5, 18, 0)) == _dt.date(2026, 10, 2)     # 14:00 ET: Friday
    assert ps(mon, _dt.datetime(2026, 10, 5, 23, 59)) == _dt.date(2026, 10, 2)    # 19:59 ET
    assert ps(mon, _dt.datetime(2026, 10, 6, 0, 0)) == mon                        # 20:00 ET: Monday is out
    assert ps("2026-10-03", _dt.datetime(2026, 10, 3, 16, 0)) == _dt.date(2026, 10, 2)    # Saturday
    assert ps("2026-09-08", _dt.datetime(2026, 9, 8, 14, 0)) == _dt.date(2026, 9, 4)      # after Labor Day
    assert ps("2026-10-02", NOW) == _dt.date(2026, 10, 2)                         # a day already past


def _daily_rows(db, sym="SYN"):
    return {r.on: r for r in db.query(models.OptUnderlyingDaily).filter(models.OptUnderlyingDaily.symbol == sym)}


def test_daily_update_ends_at_the_last_published_session(db):
    # the feed would hand out Monday's part-day bar to a read that asks for Monday
    bars = bars_synth("range", n=41, start=150.0, seed=5, end=TODAY)
    seen: list = []
    handler, _ = history_server(bars, lambda k: True, seen)
    client = make_client(handler)

    # Monday 14:00 ET, in session: the range ends on Friday - no part-day bar is filed
    out = opt_massive.daily_update(db, client, "SYN", today=TODAY, now=NOW)
    assert seen[-1].url.path == "/v2/aggs/ticker/SYN/range/1/day/2026-09-22/2026-10-02"
    assert out["complete"] is True and out["bars"] == 9
    assert TODAY not in _daily_rows(db) and HIST_DAY in _daily_rows(db)

    # Monday 20:30 ET: Monday's bar is published -> read and filed
    out = opt_massive.daily_update(db, client, "SYN", today=TODAY, now=_dt.datetime(2026, 10, 6, 0, 30))
    assert seen[-1].url.path == "/v2/aggs/ticker/SYN/range/1/day/2026-09-25/2026-10-05"
    assert out["complete"] is True and TODAY in _daily_rows(db)


def test_daily_update_not_complete_until_the_session_is_on_the_feed(db):
    bars = bars_synth("range", n=40, start=150.0, seed=5, end=HIST_DAY)     # nothing for Monday yet
    seen: list = []
    handler, _ = history_server(bars, lambda k: True, seen)
    out = opt_massive.daily_update(db, make_client(handler), "SYN", today=TODAY,
                                   now=_dt.datetime(2026, 10, 6, 0, 30))     # Monday 20:30 ET
    assert seen[-1].url.path.endswith("/2026-09-25/2026-10-05")
    assert out["bars"] == 6 and out["complete"] is False
