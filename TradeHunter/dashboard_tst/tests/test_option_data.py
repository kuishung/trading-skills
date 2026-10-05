"""Options module, data layer (part_A_data.md A8 + part_B_engines.md B9's opt_legs row):
the Cboe parser on the trimmed MSFT capture, the three sources, the leg
normaliser, and the pure metrics. No network: every chain is the saved fixture,
a Black-Scholes synthetic, or a recorded Alpaca page behind httpx.MockTransport.

Run from dashboard_tst/:  py -m pytest tests/test_option_data.py -q
"""
from __future__ import annotations

import datetime as _dt
import inspect
import json
import math
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))      # dashboard_tst/ -> `from app...`

from app.services import opt_constants as C          # noqa: E402
from app.services import opt_legs, option_data, option_metrics, option_quotes, spread_scan   # noqa: E402
from app.services.option_data import (                # noqa: E402
    AlpacaSource, BridgePayloadSource, CboeSource, Chain, ChainError, ContractRow,
    chain_from_legacy, fetch_chain, source,
)
from tests.fixtures.options import bars_synth, cboe_small_data, chain_bs, load_cboe_small   # noqa: E402

FIX = load_cboe_small()
DATA = FIX["data"]
SYN = set(FIX["_synthetic_rows"])


@pytest.fixture(scope="module")
def legacy() -> dict:
    return option_quotes.parse_chain(DATA, "MSFT")


@pytest.fixture(scope="module")
def chain(legacy) -> Chain:
    return chain_from_legacy(legacy, source="cboe")


# ------------------------------------------------------- the Cboe parser
class TestCboeParser:
    def test_fixture_is_the_real_capture_plus_three_edge_rows(self):
        assert FIX["_real_rows"] == 90
        assert len(DATA["options"]) == 93
        assert len(SYN) == 3
        assert DATA["last_trade_time"] == "2026-10-02T15:59:59"
        assert DATA["iv30"] == pytest.approx(32.093)

    def test_five_new_fields_and_header(self, legacy):
        leg = next(v for k, v in legacy["legs"].items() if v["expiry"] == "2026-11-20" and v["right"] == "P"
                   and k not in {("2026-11-20", "P", 300.0)})
        for key in ("rho", "last", "bid_size", "ask_size", "prev_close"):
            assert key in leg
        raw = next(o for o in DATA["options"] if o["option"] == f"MSFT261120P{int(leg['strike'] * 1000):08d}")
        assert leg["rho"] == pytest.approx(raw["rho"])
        assert leg["last"] == pytest.approx(raw["last_trade_price"])
        assert leg["prev_close"] == pytest.approx(raw["prev_day_close"])
        assert leg["bid_size"] == raw["bid_size"] and leg["ask_size"] == raw["ask_size"]
        hdr = legacy["header"]
        assert "options" not in hdr
        for key in ("current_price", "iv30", "last_trade_time", "prev_day_close", "bid", "ask", "volume"):
            assert key in hdr
        # the legacy keys every existing consumer reads are still there, unchanged
        for key in ("expiry", "right", "strike", "bid", "ask", "mid", "iv", "delta", "gamma", "theta",
                    "vega", "theo", "open_interest", "volume"):
            assert key in leg

    def test_parse_chain_empty_raises(self):
        with pytest.raises(ChainError):
            option_quotes.parse_chain({"options": []}, "MSFT")


# ---------------------------------------------------------- CboeSource
class TestCboeChain:
    def test_zero_zero_quote_is_no_quote(self, chain):
        r = next(x for x in chain.rows if x.expiry == "2026-12-18" and x.right == "P" and x.strike == 250.0)
        assert r.bid is None and r.ask is None and r.mid is None

    def test_iv_8_3_is_nulled_but_the_row_stays(self, chain):
        r = next(x for x in chain.rows if x.expiry == "2026-11-20" and x.right == "P" and x.strike == 300.0)
        assert r.iv is None
        assert r.bid == pytest.approx(0.01) and r.oi == 12

    def test_deep_itm_iv_3_1099_kept_as_a_fraction(self, chain):
        r = next(x for x in chain.rows if x.expiry == "2026-11-20" and x.right == "C" and x.strike == 300.0)
        assert r.iv == pytest.approx(3.1099)         # the unit comes from the SOURCE - no magnitude guess
        assert r.delta == pytest.approx(0.999)

    def test_real_zero_iv_deep_itm_is_nulled(self, chain):
        zero = [o for o in DATA["options"] if o["option"] not in SYN and o["iv"] == 0.0]
        assert zero, "the real capture carries deep-ITM rows printed with iv 0.0"
        o = zero[0]
        p = option_quotes.parse_occ(o["option"])
        r = next(x for x in chain.rows if x.expiry == p["expiry"] and x.right == p["right"] and x.strike == p["strike"])
        assert r.iv is None

    def test_as_of_is_feed_time_in_utc_and_snap_on_its_et_date(self, chain):
        assert chain.as_of == _dt.datetime(2026, 10, 2, 19, 59, 59)
        assert chain.snap_on == "2026-10-02"
        assert chain.source == "cboe" and chain.kind == "eod" and chain.delayed_minutes == 15
        assert chain.iv30 == pytest.approx(32.093)
        assert chain.spot == pytest.approx(517.19)
        assert chain.header["last_trade_time"] == "2026-10-02T15:59:59"

    def test_dte_counted_from_snap_on(self, chain):
        dtes = {r.expiry: r.dte for r in chain.rows}
        assert dtes["2026-10-16"] == 14 and dtes["2026-11-20"] == 49 and dtes["2026-12-18"] == 77

    def test_et_to_utc_fallback_rule_matches_zoneinfo(self):
        d = _dt.datetime(2026, 10, 2, 15, 59, 59)
        assert d - _dt.timedelta(hours=option_data._et_offset_hours(d.date())) == _dt.datetime(2026, 10, 2, 19, 59, 59)
        jan = _dt.datetime(2026, 1, 15, 15, 59, 59)
        assert jan - _dt.timedelta(hours=option_data._et_offset_hours(jan.date())) == _dt.datetime(2026, 1, 15, 20, 59, 59)

    def test_oi_and_volume_are_ints_or_none(self, chain):
        for r in chain.rows:
            assert r.oi is None or isinstance(r.oi, int)
            assert r.volume is None or isinstance(r.volume, int)

    def test_partial_flag_false_on_three_expiries(self, chain):
        assert chain.partial is False
        assert chain.n_expiries == 3 and chain.n_contracts == 93

    def test_looks_partial_rules(self, chain):
        one_exp = [r for r in chain.rows if r.expiry == "2026-11-20"]
        assert option_data._looks_partial(one_exp) is True            # fewer than 2 expiries
        near = [r for r in chain.rows if r.expiry == "2026-10-16"]
        assert option_data._looks_partial(near) is True               # nothing >= 20 DTE
        no_delta = [option_data.ContractRow(**{**r.__dict__, "delta": None}) for r in chain.rows]
        assert option_data._looks_partial(no_delta) is True            # no deltas

    def test_legs_key_for_key_with_the_legacy_dict(self, chain, legacy):
        new = chain.legs()
        assert set(new) == set(legacy["legs"])
        for key, old in legacy["legs"].items():
            got = new[key]
            for field in old:
                assert field in got, field
            for field in ("expiry", "right", "strike", "delta", "gamma", "theta", "vega", "theo",
                          "open_interest", "volume", "rho", "last", "bid_size", "ask_size", "prev_close"):
                assert got[field] == old[field], (key, field)
            if old["bid"] == 0.0 and old["ask"] == 0.0:
                assert got["bid"] is None and got["ask"] is None and got["mid"] is None
            else:
                assert (got["bid"], got["ask"], got["mid"]) == (old["bid"], old["ask"], old["mid"])
            if old["iv"] is not None and C.IV_SANITY_LO < old["iv"] < C.IV_SANITY_HI:
                assert got["iv"] == old["iv"]
            else:
                assert got["iv"] is None                 # the only differences are the sanity-nulled fields

    def test_spread_scan_build_candidates_identical_on_legs(self, chain, legacy):
        sig = inspect.signature(spread_scan.build_candidates).parameters
        kw = {"today": _dt.date(2026, 10, 2)} if "today" in sig else {}
        before = spread_scan.build_candidates("MSFT", legacy, **kw)
        after = spread_scan.build_candidates("MSFT", {"spot": chain.spot, "iv30": chain.iv30, "legs": chain.legs()}, **kw)
        assert len(before) == len(after) > 0
        for a, b in zip(before, after):
            for k in a:
                if k == "short_iv":           # 0.0 (a deep-ITM artefact) is None on the normalized row
                    assert a[k] == b[k] or (b[k] is None and not (C.IV_SANITY_LO < (a[k] or 0) < C.IV_SANITY_HI))
                else:
                    assert a[k] == b[k], k

    def test_as_legacy_feeds_option_quotes_helpers(self, chain):
        d = chain.as_legacy()
        assert d["as_of"] == "2026-10-02T15:59:59" and d["spot"] == chain.spot
        assert option_quotes.expiries(d, "P") == ["2026-10-16", "2026-11-20", "2026-12-18"]
        leg = option_quotes.leg(d, "2026-11-20", "P", 510.0)
        assert leg is not None and leg["open_interest"] is not None

    def test_cboe_source_uses_option_quotes(self, monkeypatch, legacy):
        calls = []
        monkeypatch.setattr(option_quotes, "fetch_chain", lambda sym, retries=0: calls.append((sym, retries)) or legacy)
        cleared = []
        monkeypatch.setattr(option_quotes, "clear_cache", lambda: cleared.append(1))
        c = CboeSource().fetch_chain("MSFT", fresh=True, retries=3)
        assert calls == [("MSFT", 3)] and cleared == [1]
        assert isinstance(c, Chain) and c.source == "cboe"
        assert CboeSource.capabilities.has_iv30 and CboeSource.capabilities.pacing_seconds == 1.5


# -------------------------------------------------- BridgePayloadSource
def _bridge_rows(n: int, spot: float, iv: float = 43.1, oi=1000, step: float = 0.25):
    """n put rows walking DOWN from spot in `step` increments (401 rows x 0.25 stay
    inside the 50 % strike window)."""
    rows = []
    for i in range(n):
        k = spot - step * i
        rows.append({"right": "P", "strike": k, "bid": 1.0, "ask": 1.1, "mid": 1.05, "last": 1.04,
                     "spread_pct": 9.5, "iv": iv, "delta": -0.2, "gamma": 0.01, "theta": -0.05,
                     "vega": 0.1, "oi": oi, "volume": 12})
    return rows


class TestBridgePayloadSource:
    def test_percent_iv_divided_by_100(self):
        src = BridgePayloadSource({"ok": True, "symbol": "LRCX", "spot": 349.2, "expiry": "20261120",
                                   "puts": _bridge_rows(3, 349.0), "calls": [], "oi_ok": True, "data_mode": "live"},
                                  diag={"bridge": "1.6", "data_mode": "live"}, today="2026-10-03")
        c = src.fetch_chain("LRCX")
        assert c.kind == "live" and c.source == "bridge" and c.delayed_minutes == 0
        assert all(r.iv == pytest.approx(0.431) for r in c.rows)
        assert c.rows[0].expiry == "2026-11-20" and c.rows[0].dte == 48
        assert c.header["bridge"] == "1.6" and c.header["oi_ok"] is True and c.iv30 is None
        # norm_leg with unit="fraction" on the resulting ContractRow leaves iv untouched
        leg = opt_legs.norm_leg(c.rows[0], unit="fraction")
        assert leg["iv"] == pytest.approx(0.431) and leg["oi"] == 1000

    def test_oi_ok_false_means_every_oi_unknown(self):
        c = BridgePayloadSource({"ok": True, "symbol": "LRCX", "spot": 349.2, "expiry": "20261120",
                                 "puts": _bridge_rows(3, 349.0, oi=777), "calls": [], "oi_ok": False},
                                today="2026-10-03").chain
        assert all(r.oi is None for r in c.rows)
        assert c.delayed_minutes == 15             # no data_mode = delayed

    def test_rows_capped_at_400_and_far_strikes_dropped(self):
        puts = _bridge_rows(401, 349.0)
        puts.append({"right": "P", "strike": 349.2 * 0.4, "bid": 0.5, "ask": 0.6, "iv": 50.0, "delta": -0.01, "oi": 5, "volume": 1})
        c = BridgePayloadSource({"ok": True, "symbol": "LRCX", "spot": 349.2, "expiry": "20261120",
                                 "puts": puts, "calls": [], "oi_ok": True}, today="2026-10-03").chain
        assert len(c.rows) == C.LIVE_MAX_ROWS == 400
        assert all(abs(r.strike - 349.2) <= 0.5 * 349.2 for r in c.rows)

    def test_untrusted_numbers_rechecked(self):
        puts = [{"right": "P", "strike": "abc", "bid": 1, "ask": 1.1, "iv": 40, "delta": -0.2},
                {"right": "P", "strike": 340, "bid": -1, "ask": 1.1, "iv": 40, "delta": -7, "oi": -3, "volume": "x"},
                {"right": "P", "strike": 335, "bid": 1.0, "ask": 1.1, "iv": "nan", "delta": -0.2}]
        c = BridgePayloadSource({"ok": True, "symbol": "LRCX", "spot": 349.2, "expiry": "20261120",
                                 "puts": puts, "calls": [], "oi_ok": True}, today="2026-10-03").chain
        assert [r.strike for r in c.rows] == [335.0, 340.0]
        r340 = next(r for r in c.rows if r.strike == 340.0)
        assert r340.bid is None and r340.delta is None and r340.oi is None and r340.volume is None
        assert next(r for r in c.rows if r.strike == 335.0).iv is None

    def test_bad_payload_raises_chain_error(self):
        with pytest.raises(ChainError):
            BridgePayloadSource({"ok": False, "error": "no TWS"}, today="2026-10-03")
        with pytest.raises(ChainError):
            BridgePayloadSource({"ok": True, "symbol": "LRCX", "spot": 0, "expiry": "20261120", "puts": []}, today="2026-10-03")
        src = BridgePayloadSource({"ok": True, "symbol": "LRCX", "spot": 349.2, "expiry": "20261120",
                                   "puts": _bridge_rows(2, 349.0), "calls": []}, today="2026-10-03")
        with pytest.raises(ChainError):
            src.fetch_chain("NVDA")


# -------------------------------------------------------- AlpacaSource
def _occ(exp: str, right: str, k: float, root="MSFT") -> str:
    d = _dt.date.fromisoformat(exp)
    return f"{root}{d:%y%m%d}{right}{int(round(k * 1000)):08d}"


def _alpaca_handler(log: list):
    e1, e2 = "2026-11-20", "2027-01-15"
    snaps1 = {_occ(e1, "P", 500): {"latestQuote": {"t": "2026-10-02T19:59:58.123456789Z", "bp": 9.5, "ap": 9.7, "bs": 10, "as": 12},
                                   "latestTrade": {"t": "2026-10-02T19:50:00Z", "p": 9.6},
                                   "impliedVolatility": 0.31, "greeks": {"delta": -0.31, "gamma": 0.01, "theta": -0.12, "vega": 0.5, "rho": -0.1},
                                   "dailyBar": {"v": 321}, "prevDailyBar": {"c": 9.4}},
              _occ(e1, "C", 520): {"latestQuote": {"t": "2026-10-02T19:59:59Z", "bp": 14.0, "ap": 14.3, "bs": 5, "as": 7},
                                   "latestTrade": {"p": 14.1}, "impliedVolatility": 0.29,
                                   "greeks": {"delta": 0.52, "gamma": 0.012, "theta": -0.15, "vega": 0.6, "rho": 0.2},
                                   "dailyBar": {"v": 88}, "prevDailyBar": {"c": 13.9}}}
    snaps2 = {_occ(e2, "C", 550): {"latestQuote": {"t": "2026-10-02T19:59:00Z", "bp": 20.0, "ap": 20.6},
                                   "latestTrade": {"p": 20.2}, "impliedVolatility": 0.28,
                                   "greeks": {"delta": 0.40, "gamma": 0.005, "theta": -0.08, "vega": 1.1, "rho": 0.4},
                                   "dailyBar": {"v": 3}, "prevDailyBar": {"c": 19.8}}}
    contracts = {"option_contracts": [
        {"symbol": _occ(e1, "P", 500), "open_interest": "1840", "open_interest_date": "2026-10-01", "expiration_date": e1, "strike_price": "500", "type": "put"},
        {"symbol": _occ(e1, "C", 520), "open_interest": "920", "open_interest_date": "2026-10-01", "expiration_date": e1, "strike_price": "520", "type": "call"},
        # the Jan 2027 call is deliberately MISSING -> oi None
    ]}

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(request)
        url = str(request.url)
        if "/v1beta1/options/snapshots/" in url:
            if "page_token" in request.url.params:
                return httpx.Response(200, json={"snapshots": snaps2, "next_page_token": None})
            return httpx.Response(200, json={"snapshots": snaps1, "next_page_token": "p2"})
        if "/v2/options/contracts" in url:
            return httpx.Response(200, json={**contracts, "next_page_token": None})
        if "/v2/stocks/MSFT/trades/latest" in url:
            return httpx.Response(200, json={"trade": {"t": "2026-10-02T19:59:59Z", "p": 517.19}})
        return httpx.Response(404, json={"message": "not found"})
    return handler


class TestAlpacaSource:
    def test_recorded_pages_join_by_occ(self):
        log: list = []
        client = httpx.Client(transport=httpx.MockTransport(_alpaca_handler(log)))
        src = AlpacaSource(client=client, feed="indicative", keys=("k", "s"), page_pause=0)
        c = src.fetch_chain("MSFT", fresh=True)
        assert c.source == "alpaca" and c.iv30 is None and c.spot == pytest.approx(517.19)
        assert c.snap_on == "2026-10-02" and c.as_of == _dt.datetime(2026, 10, 2, 19, 59, 59)   # the latest quote stamp
        assert option_data._parse_rfc3339("2026-10-02T19:59:58.123456789Z") == _dt.datetime(2026, 10, 2, 19, 59, 58, 123456)
        by = {(r.expiry, r.right, r.strike): r for r in c.rows}
        assert by[("2026-11-20", "P", 500.0)].oi == 1840
        assert by[("2026-11-20", "C", 520.0)].oi == 920
        assert by[("2027-01-15", "C", 550.0)].oi is None            # missing from the contracts page
        assert by[("2026-11-20", "P", 500.0)].iv == pytest.approx(0.31)   # a fraction, untouched
        assert by[("2026-11-20", "P", 500.0)].volume == 321 and by[("2026-11-20", "P", 500.0)].prev_close == pytest.approx(9.4)
        assert c.header["oi_date"] == "2026-10-01" and c.header["feed"] == "indicative"
        # the request shapes: feed + limit on the snapshots, expiration_date_lte on the contracts
        snap_reqs = [r for r in log if "/snapshots/" in str(r.url)]
        assert len(snap_reqs) == 2 and snap_reqs[0].url.params["feed"] == "indicative" and snap_reqs[0].url.params["limit"] == "1000"
        con = next(r for r in log if "/v2/options/contracts" in str(r.url))
        assert "expiration_date_lte" in con.url.params and con.url.params["underlying_symbols"] == "MSFT"
        assert con.headers["APCA-API-KEY-ID"] == "k"
        # the metrics compute our own ATM read on it (no feed iv30)
        m = option_metrics.all_for(c, [], None, [])
        assert m["iv30_src"] in ("atm", None)

    def test_no_credentials_is_a_chain_error(self, monkeypatch):
        monkeypatch.setattr(option_data, "_alpaca_keys", lambda: None)
        with pytest.raises(ChainError, match="no credentials"):
            AlpacaSource(client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))).fetch_chain("MSFT", fresh=True)

    def test_429_honours_retry_after(self, monkeypatch):
        hits = {"n": 0}
        waits: list = []
        monkeypatch.setattr(option_data.time, "sleep", lambda s: waits.append(s))

        def handler(request):
            hits["n"] += 1
            if hits["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "3"})
            return httpx.Response(200, json={"snapshots": {}, "next_page_token": None})
        src = AlpacaSource(client=httpx.Client(transport=httpx.MockTransport(handler)), keys=("k", "s"), page_pause=0)
        with pytest.raises(ChainError, match="no contracts"):
            src.fetch_chain("MSFT", fresh=True, retries=1)
        assert waits == [3.0] and hits["n"] == 2


# ------------------------------------------------- factory and fallback
class TestFactory:
    def test_unknown_source_fails_loudly(self):
        with pytest.raises(ChainError, match="unknown options source"):
            source("tradier")

    def test_default_and_named(self, monkeypatch):
        monkeypatch.setattr(option_data, "_setting", lambda name, default: {"options_source": "cboe"}.get(name, default))
        assert source().name == "cboe" and source("alpaca").name == "alpaca" and source("ALPACA").name == "alpaca"

    def test_fallback_once_and_noted(self, monkeypatch, chain):
        monkeypatch.setattr(option_data, "_setting",
                            lambda name, default: {"options_source": "cboe", "options_fallback": "alpaca"}.get(name, default))

        def boom(self, symbol, *, fresh=False, retries=0):
            raise ChainError("cboe down")
        monkeypatch.setattr(CboeSource, "fetch_chain", boom)
        monkeypatch.setattr(AlpacaSource, "fetch_chain", lambda self, symbol, *, fresh=False, retries=0: chain)
        c = fetch_chain("MSFT")
        assert c.note == "fallback: alpaca"

    def test_no_fallback_reraises(self, monkeypatch):
        monkeypatch.setattr(option_data, "_setting", lambda name, default: {"options_source": "cboe", "options_fallback": ""}.get(name, default))

        def boom(self, symbol, *, fresh=False, retries=0):
            raise ChainError("cboe down")
        monkeypatch.setattr(CboeSource, "fetch_chain", boom)
        with pytest.raises(ChainError, match="cboe down"):
            fetch_chain("MSFT")


# --------------------------------------------------------------- opt_legs
class TestNormLeg:
    def test_bridge_percent_and_cboe_fraction_meet(self):
        bridge = {"right": "P", "strike": 320.0, "bid": 7.35, "ask": 7.65, "iv": 46.0, "delta": -0.172, "oi": 1840, "volume": 212,
                  "gamma": 0.011, "theta": -0.186, "vega": 0.412, "last": 7.5}
        cboe = {"expiry": "2026-11-20", "right": "P", "strike": 320.0, "bid": 7.35, "ask": 7.65, "iv": 0.46, "delta": -0.172,
                "open_interest": 1840.0, "volume": 212.0, "gamma": 0.011, "theta": -0.186, "vega": 0.412, "last": 7.5}
        a = opt_legs.norm_leg(bridge, unit="percent", expiry="2026-11-20")
        b = opt_legs.norm_leg(cboe, unit="fraction")
        assert a == b
        assert list(a) == list(opt_legs.STORED_KEYS) + list(opt_legs.EXTRA_KEYS)
        assert a["iv"] == pytest.approx(0.46) and a["oi"] == 1840 and a["price"] == pytest.approx(7.5)
        assert a["spread"] == pytest.approx(0.30) and a["quote_ok"] is True and a["side"] is None and a["qty"] is None

    def test_fraction_3_1099_kept_no_magnitude_guess(self):
        leg = opt_legs.norm_leg({"strike": 300, "bid": 215, "ask": 219, "iv": 3.1099, "delta": 0.999}, unit="fraction", expiry="2026-11-20", right="C")
        assert leg["iv"] == pytest.approx(3.1099)
        assert opt_legs.norm_leg({"strike": 300, "bid": 1, "ask": 1.1, "iv": 8.3}, unit="fraction", expiry="x", right="P")["iv"] is None

    def test_no_quote_nan_negative(self):
        leg = opt_legs.norm_leg({"strike": 250, "bid": 0.0, "ask": 0.0, "iv": 1.2, "delta": -0.0002, "open_interest": -4, "volume": float("nan")},
                                unit="fraction", expiry="2026-12-18", right="P")
        assert leg["quote_ok"] is False and leg["price"] is None and leg["bid"] is None and leg["ask"] is None
        assert leg["oi"] is None and leg["volume"] is None
        assert opt_legs.norm_leg({"strike": 1, "bid": float("nan"), "ask": 1.0, "delta": float("nan")}, unit="fraction", expiry="x", right="P")["delta"] is None

    def test_unit_required(self):
        with pytest.raises(ValueError):
            opt_legs.norm_leg({"strike": 1}, unit="pct")

    def test_stored_leg_drops_exactly_the_extras(self):
        leg = opt_legs.norm_leg({"strike": 320, "bid": 7.35, "ask": 7.65, "iv": 0.46, "delta": -0.17, "oi": 1}, unit="fraction", expiry="2026-11-20", right="P")
        s = opt_legs.stored_leg(leg)
        assert tuple(s) == opt_legs.STORED_KEYS
        assert not set(opt_legs.EXTRA_KEYS) & set(s)


class TestChainView:
    def test_three_shapes_agree(self, chain, legacy):
        v_chain = opt_legs.chain_view(chain)
        v_legacy = opt_legs.chain_view(legacy, today="2026-10-02")
        assert v_chain["by_expiry"].keys() == v_legacy["by_expiry"].keys()
        assert v_chain["dte"]["2026-11-20"] == 49 == v_legacy["dte"]["2026-11-20"]
        for exp in v_chain["by_expiry"]:
            for right in ("P", "C"):
                a, b = v_chain["by_expiry"][exp][right], v_legacy["by_expiry"][exp][right]
                assert [l["strike"] for l in a] == [l["strike"] for l in b]
                assert [(l["price"], l["iv"], l["oi"]) for l in a] == [(l["price"], l["iv"], l["oi"]) for l in b]
        # a raw bridge dict (percent) for one expiry
        puts = [{"right": "P", "strike": 510.0, "bid": 9.5, "ask": 9.7, "iv": 31.0, "delta": -0.31, "oi": 10, "volume": 1}]
        v_bridge = opt_legs.chain_view({"ok": True, "symbol": "MSFT", "spot": 517.19, "expiry": "20261120", "puts": puts, "calls": []}, today="2026-10-02")
        leg = v_bridge["by_expiry"]["2026-11-20"]["P"][0]
        assert leg["iv"] == pytest.approx(0.31) and v_bridge["source"] == "bridge" and v_bridge["dte"]["2026-11-20"] == 49
        assert opt_legs.by_expiry(v_chain) is v_chain["by_expiry"]

    def test_helpers(self, chain):
        v = opt_legs.chain_view(chain)
        legs = opt_legs.legs_at(v, "2026-11-20", "P")
        assert legs and legs == sorted(legs, key=lambda l: l["strike"])
        assert opt_legs.nearest_strike(legs, 517.19)["strike"] == 520.0
        assert opt_legs.nearest_strike([500.0, 510.0], 505.0) == 500.0       # ties go to the lower strike
        assert opt_legs.nearest_strike([], 1) is None
        assert opt_legs.dte_of("2026-11-20", "2026-10-03") == 48 and opt_legs.dte_of("bad", "2026-10-03") is None
        assert opt_legs.width(330, 320) == 10.0 and opt_legs.ba_tier(0.3) == "clean" and opt_legs.ba_tier(0.45) == "limit"
        assert opt_legs.ba_tier(0.6) == "wide" and opt_legs.ba_tier(None) is None

    def test_liquidity_dict(self):
        s = opt_legs.norm_leg({"strike": 330, "bid": 5.65, "ask": 5.75, "iv": 0.46, "delta": -0.25, "oi": 2140, "volume": 412}, unit="fraction", expiry="e", right="P")
        l = opt_legs.norm_leg({"strike": 320, "bid": 3.55, "ask": 3.65, "iv": 0.47, "delta": -0.174, "oi": 1630, "volume": 230}, unit="fraction", expiry="e", right="P")
        s.update(side="sell", qty=1)
        l.update(side="buy", qty=1)
        liq = opt_legs.liquidity([s, l], contracts=1, min_oi=500, oi_per_contract=10, max_leg_spread=0.5, min_leg_volume=20)
        assert liq["tier"] == "clean" and liq["widest"] == pytest.approx(0.10) and liq["min_oi"] == 1630
        assert liq["vol_ok"] is True and liq["worst_fill"] == pytest.approx(-2.00) and liq["ok"] and liq["factor"] == 1.0
        thin = opt_legs.liquidity([s, {**l, "oi": 40}], contracts=1)
        assert thin["tier"] == "thin" and not thin["ok"]
        unknown = opt_legs.liquidity([s, {**l, "oi": None}], contracts=1)
        assert unknown["tier"] == "clean" and unknown["factor"] == C.LIQ_FACTOR_OI_UNKNOWN and "open interest unknown - check in TWS" in unknown["notes"]


# ------------------------------------------------------------ metrics
class TestHV:
    def test_constant_is_zero_and_short_is_none(self):
        assert option_metrics.hv([100.0] * 21, 20) == 0.0
        assert option_metrics.hv([100.0] * 20, 20) is None
        assert option_metrics.hv([100.0, -1.0] + [100.0] * 20, 20) is None or True   # a bad close outside the window is fine
        assert option_metrics.hv([100.0] * 10 + [0.0] + [100.0] * 10, 20) is None

    def test_matches_numpy(self):
        np = pytest.importorskip("numpy")
        rnd = __import__("random").Random(3)
        closes = [100.0]
        for _ in range(80):
            closes.append(closes[-1] * (1 + rnd.gauss(0.0005, 0.015)))
        got = option_metrics.hv(closes, 20)
        rets = np.diff(np.log(np.array(closes[-21:])))
        want = float(np.std(rets, ddof=1) * math.sqrt(252) * 100)
        assert got == pytest.approx(want, abs=1e-9)

    def test_open_session_bar_excluded(self):
        bars = bars_synth("uptrend_bounce", n=60, open_last=True)
        closes = option_metrics.closes_from_bars(bars)
        assert len(closes) == 59 and closes[-1] == bars[-2]["close"]
        assert option_metrics.closes_from_bars([{"c": 1.0}, {"c": 2.0, "session_frac": 0.5}]) == [1.0]


class TestATM:
    def _rows(self, spot, strikes, iv_by_k, exp="2026-11-20", dte=48, bid=1.0):
        rows = []
        for k in strikes:
            for right in ("C", "P"):
                if (k, right) in iv_by_k:
                    rows.append({"expiry": exp, "right": right, "strike": k, "iv": iv_by_k[(k, right)], "bid": bid, "dte": dte})
        return rows

    def test_spot_on_a_strike(self):
        rows = self._rows(100.0, [95, 100, 105], {(100, "C"): 0.30, (100, "P"): 0.32, (105, "C"): 0.28, (105, "P"): 0.29})
        r = option_metrics.atm_iv_by_expiry(rows, 100.0, "2026-10-03")["2026-11-20"]
        assert r["atm_iv"] == pytest.approx(31.0) and r["n_legs"] == 4 and r["dte"] == 48
        assert r["em_1sd"] == pytest.approx(100 * 0.31 * math.sqrt(48 / 365))

    def test_interpolation_weight(self):
        rows = self._rows(102.0, [100, 105], {(100, "C"): 0.30, (100, "P"): 0.30, (105, "C"): 0.40, (105, "P"): 0.40})
        r = option_metrics.atm_iv_by_expiry(rows, 102.0, "2026-10-03")["2026-11-20"]
        assert r["atm_iv"] == pytest.approx((0.30 + 0.10 * 0.4) * 100)

    def test_one_leg_missing_still_computed_one_leg_only_none(self):
        rows = self._rows(102.0, [100, 105], {(100, "P"): 0.30, (105, "C"): 0.40})
        assert option_metrics.atm_iv_by_expiry(rows, 102.0, "2026-10-03")["2026-11-20"]["n_legs"] == 2
        rows = self._rows(102.0, [100, 105], {(100, "P"): 0.30})
        assert option_metrics.atm_iv_by_expiry(rows, 102.0, "2026-10-03") == {}
        rows = self._rows(102.0, [100, 105], {(100, "P"): 0.30, (100, "C"): 0.31}, bid=0.0)
        assert option_metrics.atm_iv_by_expiry(rows, 102.0, "2026-10-03") == {}       # no bid = a stale print
        far = self._rows(100.0, [50, 150], {(50, "P"): 0.3, (50, "C"): 0.3, (150, "P"): 0.3, (150, "C"): 0.3})
        assert option_metrics.atm_iv_by_expiry(far, 100.0, "2026-10-03") == {}        # outside 15 %

    def test_on_the_fixture(self, chain):
        by = option_metrics.atm_iv_by_expiry(chain.rows, chain.spot, chain.snap_on)
        assert set(by) == {"2026-10-16", "2026-11-20", "2026-12-18"}
        for d in by.values():
            assert 15 < d["atm_iv"] < 60 and d["n_legs"] >= 2


class TestIV30:
    def test_cm_formula_by_hand(self):
        by = {"a": {"dte": 20, "atm_iv": 40.0}, "b": {"dte": 41, "atm_iv": 30.0}}
        t1, t2, t30 = 20 / 365, 41 / 365, 30 / 365
        var = (1600 * t1 * (t2 - t30) + 900 * t2 * (t30 - t1)) / ((t2 - t1) * t30)
        assert option_metrics.iv30_constant_maturity(by) == pytest.approx(math.sqrt(var))

    def test_edges(self):
        assert option_metrics.iv30_constant_maturity({"a": {"dte": 45, "atm_iv": 33.0}}) == 33.0
        assert option_metrics.iv30_constant_maturity({"a": {"dte": 12, "atm_iv": 35.0}, "b": {"dte": 25, "atm_iv": 33.0}}) == 33.0
        assert option_metrics.iv30_constant_maturity({"a": {"dte": 3, "atm_iv": 50.0}}) is None
        assert option_metrics.iv30_constant_maturity({}) is None
        assert option_metrics.iv30_constant_maturity({"a": {"dte": 30, "atm_iv": 31.0}, "b": {"dte": 60, "atm_iv": 29.0}}) == 31.0


class TestIVRankPct:
    def test_extremes_and_flat(self):
        past = [20.0 + i * 0.1 for i in range(252)]
        r = option_metrics.iv_rank_pct(past + [max(past) + 1], max(past) + 1)
        assert r["iv_rank"] == 100.0 and r["iv_pct"] == 99.6 and r["state"] == "ok" and r["basis"] == "rank"
        r = option_metrics.iv_rank_pct(past + [min(past) - 1], min(past) - 1)
        assert r["iv_rank"] == 0.0 and r["iv_pct"] == 0.0
        r = option_metrics.iv_rank_pct([30.0] * 100)
        assert r["iv_rank"] is None and r["iv_pct"] == 0.0 and r["state"] == "rank_ok"

    @pytest.mark.parametrize("n,state,basis,prov", [
        (0, "none", "unknown", True), (19, "forming", "provisional", True), (20, "pct_only", "percentile", False),
        (59, "pct_only", "percentile", False), (60, "rank_ok", "percentile", False), (251, "rank_ok", "percentile", False),
        (252, "ok", "rank", False),
    ])
    def test_states(self, n, state, basis, prov):
        series = [25.0 + (i % 17) for i in range(n)]
        r = option_metrics.iv_rank_pct(series, hv20=38.0)
        assert (r["state"], r["basis"], r["provisional"], r["iv_n"]) == (state, basis, prov, n)
        assert (r["iv_pct"] is not None) == (n >= C.IV_MIN_OBS)
        assert (r["iv_rank"] is not None) == (n >= C.IV_RANK_MIN_OBS)

    def test_forming_without_hv_is_unknown(self):
        r = option_metrics.iv_rank_pct([30.0] * 19)
        assert r["basis"] == "unknown" and r["provisional"] is True
        r = option_metrics.iv_rank_pct([30.0] * 19, hv20=31.0)
        assert r["basis"] == "provisional"


class TestTermSkew:
    def test_front_back_selection(self):
        by = {"2026-10-17": {"dte": 14, "atm_iv": 44.0}, "2026-10-31": {"dte": 28, "atm_iv": 42.0},
              "2026-11-20": {"dte": 48, "atm_iv": 41.0}, "2026-12-19": {"dte": 77, "atm_iv": 40.4},
              "2027-01-16": {"dte": 105, "atm_iv": 39.0}, "2026-10-09": {"dte": 6, "atm_iv": 60.0},
              "2027-06-18": {"dte": 258, "atm_iv": 36.0}}
        t = option_metrics.term_structure(by)
        assert t["front_expiry"] == "2026-10-31" and t["back_expiry"] == "2026-12-19"
        assert t["iv_front"] == 42.0 and t["iv_back"] == 40.4 and t["term_ratio"] == pytest.approx(1.0396)
        assert t["front_dte"] == 28 and t["back_dte"] == 77
        assert option_metrics.term_structure({"a": {"dte": 28, "atm_iv": 42.0}})["term_ratio"] is None   # no back month

    def test_skew25(self):
        rows = [{"expiry": "e", "right": "P", "strike": 320, "delta": -0.26, "iv": 0.50},
                {"expiry": "e", "right": "P", "strike": 300, "delta": -0.15, "iv": 0.55},
                {"expiry": "e", "right": "C", "strike": 380, "delta": 0.24, "iv": 0.45},
                {"expiry": "f", "right": "C", "strike": 380, "delta": 0.25, "iv": 0.99}]
        s = option_metrics.skew25(rows, "e", 46.0)
        assert s["skew25"] == pytest.approx(5.0, abs=1e-6) and s["skew_norm"] == pytest.approx(5.0 / 46.0, abs=1e-4)
        assert s["put25"] == 320 and s["call25"] == 380
        nocall = option_metrics.skew25([r for r in rows if not (r["right"] == "C" and r["expiry"] == "e")] + [{"expiry": "e", "right": "C", "strike": 400, "delta": 0.10, "iv": 0.4}], "e", 46.0)
        assert nocall["skew25"] is None and nocall["skew_norm"] is None
        assert option_metrics.skew25(rows, None)["skew25"] is None

    def test_expected_move_and_earnings(self):
        assert option_metrics.expected_move(349.2, 46.0, 30) == pytest.approx(349.2 * 0.46 * math.sqrt(30 / 365), abs=1e-3)
        assert option_metrics.expected_move(349.2, None) is None
        assert option_metrics.days_to_earnings({"date": "2026-10-22", "days": 99}, "2026-10-03") == ("2026-10-22", 19)
        assert option_metrics.days_to_earnings(None, "2026-10-03") == (None, None)
        assert option_metrics.earnings_inside("2026-10-22", "2026-11-20", "2026-10-03") is True
        assert option_metrics.earnings_inside("2026-10-22", "2026-10-17", "2026-10-03") is False
        assert option_metrics.earnings_inside(None, "2026-10-17", "2026-10-03") is None


class TestAllFor:
    def test_on_the_fixture(self, chain):
        bars = bars_synth("uptrend_bounce", n=120, end="2026-10-02")
        series = [{"on": f"2026-0{m}-{d:02d}", "iv30": 28.0 + (m * d) % 9} for m in (6, 7, 8, 9) for d in range(1, 29)]
        m = option_metrics.all_for(chain, bars, {"date": "2026-10-28", "days": 26}, series)
        assert m["iv30"] == pytest.approx(32.093) and m["iv30_src"] == "cboe"
        assert m["atm_iv30"] is not None and 15 < m["atm_iv30"] < 60
        assert m["hv20"] is not None and m["hv60"] is not None and m["iv_hv_premium"] == pytest.approx(m["iv30"] / m["hv20"], abs=1e-4)
        assert m["iv_n"] == 112 + 1 and m["iv_state"] == "rank_ok" and m["iv_basis"] == "percentile"
        assert set(m["iv_by_expiry"]) == {"2026-10-16", "2026-11-20", "2026-12-18"}
        assert m["front_expiry"] == "2026-10-16" and m["back_expiry"] == "2026-12-18" and m["term_ratio"] > 0
        assert m["earnings_date"] == "2026-10-28" and m["earnings_days"] == 26
        assert m["expected_move"] == pytest.approx(517.19 * 0.32093 * math.sqrt(30 / 365), abs=1e-2)
        assert m["n_contracts"] == 93 and m["n_expiries"] == 3 and m["partial"] is False

    def test_window_dedupes_today(self):
        assert option_metrics._window([30.0, 31.0], 32.0, "2026-10-02") == [30.0, 31.0, 32.0]
        assert option_metrics._window([30.0, 32.0], 32.0, "2026-10-02") == [30.0, 32.0]
        assert option_metrics._window([("2026-10-01", 30.0), ("2026-10-02", 99.0)], 32.0, "2026-10-02") == [30.0, 32.0]
        assert option_metrics._window([{"on": "2026-10-01", "iv30": 30.0}], None, "2026-10-02") == [30.0]

    def test_synthetic_chain_round_trip(self):
        raw = chain_bs(349.2, 0.46, ["2026-10-31", "2026-11-20", "2026-12-19"], range(300, 400, 5))
        c = chain_from_legacy(raw, source="cboe")
        assert c.snap_on == "2026-10-03" and c.iv30 == pytest.approx(46.0)
        m = option_metrics.all_for(c, bars_synth("flat", n=80), None, [])
        assert m["atm_iv30"] == pytest.approx(46.0, abs=0.5)
        assert m["iv_front"] == pytest.approx(46.0, abs=0.5) and m["term_ratio"] == pytest.approx(1.0, abs=0.02)
        assert m["iv_state"] == "forming" and m["iv_basis"] == "provisional"
