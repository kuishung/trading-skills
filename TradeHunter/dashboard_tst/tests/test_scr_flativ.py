"""IV history from Massive's options day files (v4.138): ``app/services/scr_flativ.py``.

The point of these tests is EQUIVALENCE: one day file -> every stock's IV30 must be the
number the per-stock REST history (``opt_massive._day_iv`` + ``iv30_constant_maturity``,
and ``iv30_history`` end to end) gives for the same contracts. The day is synthetic:
closes are Black-Scholes prices from known IVs, with BRK-B (root BRKB), an adjusted root
(GME1), index roots (SPX / SPXW), a split-like day, weeklies and far expiries that must be
ignored, and the edge cases of the strike pick. Plus the speed on a 400k-row day file.

No network, no database.
"""
from __future__ import annotations

import csv
import datetime as _dt
import gzip
import math
import random
import statistics
import time

import numpy as np
import pytest

from app.services import option_metrics, opt_massive, scr_flativ
from app.services.black_scholes import black_scholes, implied_vol
from app.services.opt_constants import ATM_MAX_DIST_PCT, RISK_FREE

DAY = _dt.date(2026, 11, 3)                 # Tuesday: Nov 20 is 17 DTE, Dec 18 is 45 DTE
NOV, DEC = _dt.date(2026, 11, 20), _dt.date(2026, 12, 18)
NEAR_WK = _dt.date(2026, 11, 13)            # weekly, 10 DTE: ignored
MID_WK = _dt.date(2026, 12, 4)              # weekly, 31 DTE: ignored (not a monthly)
FAR_M = _dt.date(2027, 1, 15)               # monthly, 73 DTE: ignored
HEADER = ["ticker", "volume", "open", "close", "high", "low", "window_start", "transactions"]


def tick(root: str, exp: _dt.date, right: str, k: float) -> str:
    return "O:%s%s%s%08d" % (root, exp.strftime("%y%m%d"), right, int(round(k * 1000)))


def bs(S, K, exp, right, sig, day=DAY) -> float:
    return black_scholes(S, K, (exp - day).days / 365.0, RISK_FREE, sig,
                         "call" if right == "C" else "put").price


# ───────────────────────────────── the synthetic day ─────────────────────────────────

class Market:
    """Contracts of one day as records ``(symbol, root, expiry, right, strike, close,
    volume)`` - the reference computes from these records, the builder from the rows."""

    def __init__(self):
        self.recs: list[tuple] = []

    def add(self, sym, root, exp, right, k, close, vol=25):
        self.recs.append((sym, root, exp, right, float(k), close, vol))

    def chain(self, sym, root, S, exps_sig: dict, strikes, *, rights="CP", dp=4, vol=25, skip=()):
        for exp, sig in exps_sig.items():
            for k in strikes:
                for rt in rights:
                    if (exp, rt, k) in skip:
                        continue
                    smile = sig + 0.08 * math.log(k / S) ** 2       # a little smile
                    self.add(sym, root, exp, rt, k, round(bs(S, k, exp, rt, smile), dp), vol)

    def rows(self):
        return [(tick(root, exp, rt, k), close, vol) for _, root, exp, rt, k, close, vol in self.recs]

    def write(self, path, *, gz=True):
        opener = gzip.open if gz else open
        with opener(path, "wt", newline="") as f:
            w = csv.writer(f)
            w.writerow(HEADER)
            for t, close, vol in self.rows():
                w.writerow([t, vol, close, close, close, close, 1793682000000000000, 3])
        return path


def frange(a, b, step):
    out, x = [], a
    while x <= b + 1e-9:
        out.append(round(x, 4))
        x += step
    return out


SPOTS = {"AAA": 101.3, "BRK-B": 485.0, "GME": 23.1, "SPX": 5800.0, "SPLT": 200.4, "ONEC": 60.7,
         "NOSV": 75.2, "FARS": 40.0, "ZVOL": 88.8}


def build_market() -> Market:
    m = Market()
    # AAA: two monthlies -> interpolated; weeklies / far monthly at a crazy IV must not count
    m.chain("AAA", "AAA", SPOTS["AAA"], {NOV: 0.32, DEC: 0.35}, frange(90, 112.5, 2.5))
    m.chain("AAA", "AAA", SPOTS["AAA"], {NEAR_WK: 1.9, MID_WK: 1.9, FAR_M: 1.9}, frange(95, 107.5, 2.5))
    # BRK-B: root BRKB; 485 sits exactly between 480 and 490 -> the lower one
    m.chain("BRK-B", "BRKB", SPOTS["BRK-B"], {NOV: 0.18, DEC: 0.20}, frange(460, 510, 10))
    # GME: standard strikes, plus the ADJUSTED series GME1 nearer the close at a crazy price
    m.chain("GME", "GME", SPOTS["GME"], {NOV: 0.80, DEC: 0.75}, [20.0, 22.5, 25.0, 27.5])
    for rt in "CP":
        m.add("GME", "GME1", NOV, rt, 23.0, 9.99)
        m.add("GME", "GME1", DEC, rt, 23.0, 9.99)
    # SPX / SPXW: index roots never map, whatever the closes say
    m.chain("SPX", "SPX", SPOTS["SPX"], {NOV: 0.15, DEC: 0.16}, frange(5700, 5900, 50))
    m.chain("SPX", "SPXW", SPOTS["SPX"], {NOV: 0.15, MID_WK: 0.15}, frange(5700, 5900, 50))
    # SPLT: a day BEFORE a 2:1 split - listed at the pre-split strikes, the raw close 200.4
    m.chain("SPLT", "SPLT", SPOTS["SPLT"], {NOV: 0.45, DEC: 0.47}, frange(180, 220, 5))
    # ONEC: only the call traded at the nearest strike in November
    m.chain("ONEC", "ONEC", SPOTS["ONEC"], {NOV: 0.40, DEC: 0.42}, frange(55, 66, 1), skip={(NOV, "P", 61.0)})
    # NOSV: November's nearest strike has only a call priced under intrinsic -> November says
    # nothing (no fall-back to the next strike), December carries the day
    m.chain("NOSV", "NOSV", SPOTS["NOSV"], {NOV: 0.30, DEC: 0.33}, frange(70, 80, 2.5),
            skip={(NOV, "C", 75.0), (NOV, "P", 75.0)})
    m.add("NOSV", "NOSV", NOV, "C", 75.0, 0.05)
    # FARS: every listed strike more than 15 % from the close
    m.chain("FARS", "FARS", SPOTS["FARS"], {NOV: 0.5, DEC: 0.5}, [30.0, 50.0])
    # ZVOL: the nearest strike printed no trade (volume 0) -> the next one is used
    m.chain("ZVOL", "ZVOL", SPOTS["ZVOL"], {NOV: 0.28, DEC: 0.29}, [85.0, 87.5, 90.0, 92.5])
    for rt in "CP":
        m.add("ZVOL", "ZVOL", NOV, rt, 89.0, 4.44, vol=0)
        m.add("ZVOL", "ZVOL", DEC, rt, 89.0, 4.44, vol=0)
    # NOCL: options but no raw close for the day -> nothing
    m.chain("NOCL", "NOCL", 50.0, {NOV: 0.3, DEC: 0.3}, [45.0, 50.0, 55.0])
    return m


UNIVERSE = ["AAA", "BRK-B", "GME", "SPX", "SPLT", "ONEC", "NOSV", "FARS", "ZVOL", "NOCL", "I:NDX"]


def reference(m: Market, day, spots: dict) -> dict[str, float]:
    """The REST method on the same contracts: per stock, the legs of each standard monthly
    expiry 15-45 DTE (``{(right, strike): {on: close}}``, the shape ``_fetch_expiry``
    returns), ``opt_massive._day_iv`` per expiry, ``iv30_constant_maturity`` - UNROUNDED.
    Only the stock's own plain root (``massive.option_ticker``'s root) and traded rows."""
    on = day.isoformat()
    monthly = {opt_massive.monthly_expiry(y, mo) for y in (day.year, day.year + 1) for mo in range(1, 13)}
    out = {}
    for sym, S in spots.items():
        if sym in ("SPX",) or sym.startswith("I:"):
            continue
        root = sym.replace("-", "")
        legs_by: dict[_dt.date, dict] = {}
        for s, r, exp, rt, k, close, vol in m.recs:
            if s != sym or r != root or exp not in monthly or close is None or close <= 0 or vol <= 0:
                continue
            if not (opt_massive.IV_DTE_LO <= (exp - day).days <= opt_massive.IV_DTE_HI):
                continue
            legs_by.setdefault(exp, {})[(rt, k)] = {on: close}
        by_exp = {}
        for exp, legs in legs_by.items():
            dte = (exp - day).days
            v = opt_massive._day_iv(legs, on, S, dte)
            if v is not None:
                by_exp[exp.isoformat()] = {"dte": dte, "atm_iv": v * 100.0}
        iv30 = option_metrics.iv30_constant_maturity(by_exp, on)
        if iv30 is not None and iv30 > 0:
            out[sym] = iv30
    return out


# ───────────────────────────────── tickers + symbols ─────────────────────────────────

def test_parse_ticker():
    assert scr_flativ.parse_ticker("O:AAPL261016C00150000") == ("AAPL", _dt.date(2026, 10, 16), "C", 150.0)
    assert scr_flativ.parse_ticker("O:BRKB261120P00482500") == ("BRKB", NOV, "P", 482.5)
    assert scr_flativ.parse_ticker("o:gme1261120c00023000") == ("GME1", NOV, "C", 23.0)    # adjusted kept as such
    assert scr_flativ.parse_ticker("O:SPXW261204C05800000")[0] == "SPXW"
    for bad in ("", None, "AAPL261016C00150000", "O:AAPL261016X00150000", "O:AAPL261332C00150000",
                "O:AAPL26101AC00150000", "O:261016C00150000", "O:AAPL261016C00000000", "O:1A261016C00150000"):
        assert scr_flativ.parse_ticker(bad) is None, bad


def test_symbol_map_share_classes_adjusted_and_index_roots():
    got = scr_flativ.symbol_map(["aapl", "BRK-B", "BF.B", "SPX", "SPXW", "NDX", "VIX", "RUT", "XSP", "DJX",
                                 "I:SPX", "GME", "ABC1", ""])
    assert got == {"AAPL": "AAPL", "BRKB": "BRK-B", "BFB": "BF.B", "GME": "GME"}
    # one root, two spellings: the one spelt like the root wins (order does not matter)
    assert scr_flativ.symbol_map(["BRK-B", "BRKB"]) == {"BRKB": "BRKB"}
    assert scr_flativ.symbol_map(["BRKB", "BRK-B"]) == {"BRKB": "BRKB"}
    # a {symbol: sec_type} dict: an index never maps
    assert scr_flativ.symbol_map({"QQQ": "etf", "NDX100": "index", "FOO": "index", "BAR": None}) == \
        {"QQQ": "QQQ", "BAR": "BAR"}
    assert scr_flativ.option_root("BRK-B") == "BRKB" and scr_flativ.option_root("RUTW") is None


def test_monthly_window_is_the_rest_window():
    assert scr_flativ.monthly_window(DAY) == {"261120": (NOV, 17), "261218": (DEC, 45)}
    assert scr_flativ.monthly_window("2026-11-02") == {"261120": (NOV, 18)}     # Dec 18 is 46 DTE
    # Good Friday 2025-04-18: April's monthly expires Thursday the 17th
    assert scr_flativ.monthly_window(_dt.date(2025, 3, 24)) == {"250417": (_dt.date(2025, 4, 17), 24)}
    assert scr_flativ.monthly_window(None) == {}


# ───────────────────────────────── the day file ─────────────────────────────────

def test_read_day_gz_and_plain_any_column_order(tmp_path):
    m = build_market()
    rows = m.rows()
    p = m.write(tmp_path / "2026-11-03.csv.gz")
    got = list(scr_flativ.read_day(p))
    assert len(got) == len(rows)
    assert got[0] == (rows[0][0], pytest.approx(rows[0][1], abs=1e-12), 25.0)
    # a plain CSV, columns in another order, junk lines skipped
    q = tmp_path / "plain.csv"
    q.write_text("close,ticker,volume\n1.25,O:AAA261120C00100000,7\nnot,a\nx,O:AAA261120P00100000,\n",
                 encoding="ascii")
    assert list(scr_flativ.read_day(q)) == [("O:AAA261120C00100000", 1.25, 7.0),
                                            ("O:AAA261120P00100000", None, None)]
    # a byte-order mark before the header does not hide the ticker column
    bom = tmp_path / "bom.csv"
    bom.write_bytes("﻿ticker,close\nO:AAA261120C00100000,2.5\n".encode("utf-8"))
    assert list(scr_flativ.read_day(bom)) == [("O:AAA261120C00100000", 2.5, None)]
    # the expiry pre-filter, and the tally of every data line read
    tally = [0]
    only = list(scr_flativ.read_day(p, expiries={"261218"}, tally=tally))
    assert tally == [len(rows)] and 0 < len(only) < len(rows)
    assert only and all(t[-15:-9] == "261218" for t, _, _ in only)
    bad = tmp_path / "bad.csv"
    bad.write_text("symbol,price\nX,1\n", encoding="ascii")
    with pytest.raises(ValueError):
        list(scr_flativ.read_day(bad))


# ───────────────────────────────── the vectorised solve ─────────────────────────────────

def test_implied_vols_are_the_scalar_solver_bit_for_bit():
    rnd = random.Random(7)
    P, S, K, T, C = [], [], [], [], []
    for _ in range(400):
        s = rnd.uniform(5, 900)
        k = s * rnd.uniform(0.7, 1.3)
        t = rnd.randint(1, 400) / 365.0
        right = rnd.choice("CP")
        sig = rnd.uniform(0.05, 2.5)
        price = round(black_scholes(s, k, t, RISK_FREE, sig, "call" if right == "C" else "put").price, 2)
        for arr, v in ((P, price), (S, s), (K, k), (T, t), (C, right == "C")):
            arr.append(v)
    # the cases the scalar solver refuses: no price, under intrinsic, over the ceiling, no time
    P += [0.0, -1.0, 0.01, 10_000.0, 2.0]
    S += [100.0, 100.0, 100.0, 100.0, 100.0]
    K += [100.0, 100.0, 50.0, 100.0, 100.0]
    T += [0.1, 0.1, 0.1, 0.1, 0.0]
    C += [True, True, True, False, True]
    got = scr_flativ.implied_vols(P, S, K, T, C)
    for i in range(len(P)):
        want = implied_vol(P[i], S[i], K[i], T[i], "call" if C[i] else "put")
        if want is None:
            assert math.isnan(got[i]), i
        else:
            assert got[i] == want, (i, got[i], want)          # the same number to the last bit
    assert scr_flativ.implied_vols([], [], [], [], []).shape == (0,)


# ───────────────────────────────── one day -> every stock ─────────────────────────────────

def test_day_iv30_equals_the_rest_method_on_the_same_contracts(tmp_path):
    m = build_market()
    roots = scr_flativ.symbol_map(UNIVERSE)
    assert "SPX" not in roots and "SPXW" not in roots and roots["BRKB"] == "BRK-B"
    closes = dict(SPOTS)                                      # NOCL has no raw close
    ref = reference(m, DAY, closes)
    stats: dict = {}
    raw = scr_flativ.day_iv30(m.rows(), DAY, closes, roots, raw=True, stats=stats)
    assert set(raw) == set(ref) == {"AAA", "BRK-B", "GME", "SPLT", "ONEC", "NOSV", "ZVOL"}
    for sym, v in ref.items():
        assert raw[sym] == pytest.approx(v, abs=1e-9), sym
    # the stored figure: rounded like iv30_history's points; and the file path gives the same
    rounded = scr_flativ.day_iv30(m.rows(), DAY, closes, roots)
    from_file = scr_flativ.day_iv30(str(m.write(tmp_path / "d.csv.gz")), DAY, closes, roots)
    for sym, v in ref.items():
        assert abs(rounded[sym] - round(v, 4)) <= 1e-6 and from_file[sym] == rounded[sym], sym
    assert stats["symbols"] == len(ref) and stats["contracts"] > 0 and stats["rows"] == len(m.recs)
    # the known IVs come back: BRK-B's 18 % (17 DTE) and 20 % (45 DTE) blended to 30 days in
    # variance-time (closes at 4 decimals: within a few hundredths of a vol point)
    blend = math.sqrt((0.18 ** 2 * 17 * (45 - 30) + 0.20 ** 2 * 45 * (30 - 17)) / ((45 - 17) * 30)) * 100
    assert raw["BRK-B"] == pytest.approx(blend, abs=0.05)


def test_day_iv30_strike_rules_and_drops():
    m = build_market()
    roots = scr_flativ.symbol_map(UNIVERSE)
    on = DAY.isoformat()

    def one(sym, exp):
        legs = {}
        for s, r, e, rt, k, close, vol in m.recs:
            if s == sym and r == sym.replace("-", "") and e == exp and vol > 0:
                legs[(rt, k)] = {on: close}
        return legs

    def solve(close, S, k, dte, rt):
        return implied_vol(close, S, k, dte / 365.0, "call" if rt == "C" else "put")

    # BRK-B: 485 sits exactly between 480 and 490 -> the LOWER strike; November alone (one
    # expiry is used as it is) = the mean of the call and put IVs solved at 480
    nov_rows = [r for r in m.rows() if r[0].startswith("O:BRKB261120")]
    at480 = [solve(c, 485.0, 480.0, 17, rt) for s, r, e, rt, k, c, v in m.recs
             if r == "BRKB" and e == NOV and k == 480.0]
    got = scr_flativ.day_iv30(nov_rows, DAY, {"BRK-B": 485.0}, roots, raw=True)["BRK-B"]
    assert len(at480) == 2 and got == pytest.approx(statistics.fmean(at480) * 100.0, abs=1e-12)
    # GME: the adjusted GME1 rows sit nearer the close at a crazy price - never used, even when
    # a caller maps the adjusted root by hand
    base = scr_flativ.day_iv30(m.rows(), DAY, {"GME": SPOTS["GME"]}, roots, raw=True)
    hand = scr_flativ.day_iv30(m.rows(), DAY, {"GME": SPOTS["GME"]}, dict(roots, GME1="GME"), raw=True)
    assert base == hand and 70 < base["GME"] < 85
    # SPX: an index root never counts, even mapped by hand
    assert scr_flativ.day_iv30(m.rows(), DAY, {"SPX": 5800.0}, {"SPX": "SPX", "SPXW": "SPX"}) == {}
    # ZVOL: the 89 strike (nearest the 88.8 close) printed no trade (volume 0) -> as if it
    # were not in the file at all
    z = scr_flativ.day_iv30(m.rows(), DAY, {"ZVOL": SPOTS["ZVOL"]}, roots, raw=True)
    without = [r for r in m.rows() if not r[0].startswith(("O:ZVOL261120C00089000", "O:ZVOL261120P00089000",
                                                           "O:ZVOL261218C00089000", "O:ZVOL261218P00089000"))]
    assert z == scr_flativ.day_iv30(without, DAY, {"ZVOL": SPOTS["ZVOL"]}, roots, raw=True)
    assert 27 < z["ZVOL"] < 30
    # FARS: nothing within 15 % -> nothing; a missing / zero / junk raw close -> nothing
    assert scr_flativ.day_iv30(m.rows(), DAY, {"FARS": 40.0}, roots) == {}
    assert scr_flativ.day_iv30(m.rows(), DAY, {"AAA": 0.0, "GME": None, "ONEC": "x"}, roots) == {}
    # NOSV: November's nearest strike cannot be solved -> no fall-back to the next strike;
    # December alone carries the day (no interpolation)
    n = scr_flativ.day_iv30(m.rows(), DAY, {"NOSV": SPOTS["NOSV"]}, roots, raw=True)["NOSV"]
    assert opt_massive._day_iv(one("NOSV", NOV), on, SPOTS["NOSV"], 17) is None
    assert n == pytest.approx(opt_massive._day_iv(one("NOSV", DEC), on, SPOTS["NOSV"], 45) * 100.0, abs=1e-12)
    # ONEC: only the call traded at November's nearest strike (61) -> November = that call's IV
    nov_call = [(c, rt) for s, r, e, rt, k, c, v in m.recs if r == "ONEC" and e == NOV and k == 61.0]
    assert [rt for _, rt in nov_call] == ["C"]
    assert opt_massive._day_iv(one("ONEC", NOV), on, SPOTS["ONEC"], 17) == solve(nov_call[0][0], 60.7, 61.0, 17, "C")
    # nothing to map, or no date -> nothing / an error
    assert scr_flativ.day_iv30(m.rows(), DAY, SPOTS, {}) == {}
    with pytest.raises(ValueError):
        scr_flativ.day_iv30(m.rows(), "not a day", SPOTS, roots)
    assert ATM_MAX_DIST_PCT == 0.15


def test_split_day_needs_the_raw_close():
    """SPLT before a 2:1 split: its options are listed at the pre-split strikes. With the raw
    close (200.4) the day has an IV30; with the split-ADJUSTED close (100.2) the nearest
    strike is far off and the day is skipped - never a wrong number."""
    m = build_market()
    roots = scr_flativ.symbol_map(UNIVERSE)
    got = scr_flativ.day_iv30(m.rows(), DAY, {"SPLT": 200.4}, roots, raw=True)
    assert got["SPLT"] == pytest.approx(reference(m, DAY, {"SPLT": 200.4})["SPLT"], abs=1e-9)
    assert 44 < got["SPLT"] < 48
    assert scr_flativ.day_iv30(m.rows(), DAY, {"SPLT": 100.2}, roots) == {}


def test_flat_constant_iv_comes_back_exactly():
    """Unrounded closes priced at one IV for every strike and expiry -> that IV, to 1e-6."""
    m = Market()
    for sym, S, sig in (("AAA", 101.3, 0.27), ("BRK-B", 487.0, 0.19), ("XYZ", 9.4, 0.95)):
        root = sym.replace("-", "")
        for exp in (NOV, DEC):
            for k in frange(S * 0.8, S * 1.2, max(0.5, round(S * 0.025, 1))):
                for rt in "CP":
                    m.add(sym, root, exp, rt, k, bs(S, k, exp, rt, sig))
    got = scr_flativ.day_iv30(m.rows(), DAY, {"AAA": 101.3, "BRK-B": 487.0, "XYZ": 9.4},
                              scr_flativ.symbol_map(["AAA", "BRK-B", "XYZ"]), raw=True)
    assert got == pytest.approx({"AAA": 27.0, "BRK-B": 19.0, "XYZ": 95.0}, abs=1e-6)


# ───────────────────────────────── end to end vs iv30_history ─────────────────────────────────

SESSIONS = [_dt.date(2026, 10, d) for d in (26, 27, 28, 29, 30)] + \
           [_dt.date(2026, 11, d) for d in (2, 3, 4, 5, 6)]
CLOSES = [100.37, 101.82, 99.61, 100.94, 102.21, 101.13, 100.42, 99.18, 100.66, 101.57]
GRID = frange(80, 125, 2.5)


def _sig(exp, k, day):
    base = 0.30 + 0.002 * (day - SESSIONS[0]).days + (0.03 if exp == DEC else 0.0)
    return base + 0.10 * math.log(k / 100.0) ** 2


def _px(day, exp, right, k):
    S = CLOSES[SESSIONS.index(day)]
    return round(bs(S, k, exp, right, _sig(exp, k, day), day=day), 2)


class FakeRest:
    """Massive's per-contract daily bars over the same synthetic market."""

    def __init__(self):
        self.calls = 0

    def option_daily(self, ticker, start, end):
        self.calls += 1
        root, exp, right, k = scr_flativ.parse_ticker(ticker)
        if root != "AAA" or exp not in (NOV, DEC) or k not in GRID:
            return []
        return [{"on": d.isoformat(), "close": _px(d, exp, right, k)} for d in SESSIONS if start <= d <= end
                and (exp - d).days >= 0]


def test_day_files_match_iv30_history_end_to_end():
    """For a stock listed on the standard strike grid, the day files give every session the
    very IV30 the REST history stores (``opt_massive.iv30_history`` with a fake client)."""
    bars = [{"on": d.isoformat(), "close": c} for d, c in zip(SESSIONS, CLOSES)]
    fake = FakeRest()
    points, n_req = opt_massive.iv30_history(fake, "AAA", bars, days_iv=len(bars))
    assert n_req > 0 and len(points) == len(SESSIONS)
    rest = {p["on"]: p["iv"] for p in points}
    roots = scr_flativ.symbol_map(["AAA"])
    for d, c in zip(SESSIONS, CLOSES):
        rows = [(tick("AAA", exp, rt, k), _px(d, exp, rt, k), 10)
                for exp in (NOV, DEC, MID_WK) for k in GRID for rt in "CP" if (exp - d).days >= 0]
        flat = scr_flativ.day_iv30(rows, d, {"AAA": c}, roots)
        assert abs(flat["AAA"] - rest[d.isoformat()]) <= 1e-6, d


# ───────────────────────────────── speed ─────────────────────────────────

def _root(i: int) -> str:
    s = ""
    for _ in range(4):
        s = chr(65 + i % 26) + s
        i //= 26
    return s


def test_speed_on_a_400k_row_day(tmp_path):
    """~400k rows (4,000 stocks x 100 contracts, 40 of them the 15-45 DTE monthlies) in
    under 5 seconds (the v4.138 target); the timing is printed (-s) for the record."""
    rnd = np.random.default_rng(11)
    n_sym = 4000
    syms = [_root(i + 1000) for i in range(n_sym)]
    spots = rnd.uniform(8, 600, n_sym)
    other = (NEAR_WK, MID_WK, FAR_M)          # 10 strikes x (2 + 3 expiries) x 2 rights = 100 a stock
    path = tmp_path / "2026-11-03.csv.gz"
    n = 0
    with gzip.open(path, "wt", compresslevel=1, newline="") as f:
        f.write(",".join(HEADER) + "\n")
        for sym, S in zip(syms, spots.tolist()):
            step = 0.5 if S < 25 else 2.5 if S < 200 else 5.0
            base = round(S / step) * step
            ks = [round(base + step * j, 2) for j in range(-5, 5)]
            for exp in (NOV, DEC):
                T = (exp - DAY).days / 365.0
                for k in ks:
                    if k <= 0:
                        continue
                    tv = 0.4 * S * 0.35 * math.sqrt(T) * math.exp(-(math.log(k / S) ** 2) / (2 * 0.35 ** 2 * T))
                    for rt in "CP":
                        intrinsic = max(S - k, 0.0) if rt == "C" else max(k - S, 0.0)
                        f.write("%s,12,1,%.2f,1,1,1793682000000000000,3\n" % (tick(sym, exp, rt, k),
                                                                             intrinsic + tv + 0.05))
                        n += 1
            for exp in other:
                for k in ks:
                    if k <= 0:
                        continue
                    for rt in "CP":
                        f.write("%s,5,1,1.23,1,1,1793682000000000000,1\n" % tick(sym, exp, rt, k))
                        n += 1
    assert n == 400_000
    closes = dict(zip(syms, spots.tolist()))
    roots = scr_flativ.symbol_map(syms)
    stats: dict = {}
    t0 = time.perf_counter()
    got = scr_flativ.day_iv30(str(path), DAY, closes, roots, stats=stats)
    secs = time.perf_counter() - t0
    print("\nscr_flativ.day_iv30: %d rows, %d kept, %d IVs solved, %d stocks in %.2f s"
          % (stats["rows"], stats["kept"], stats["contracts"], stats["symbols"], secs))
    assert stats["rows"] == n                    # every row of the file was looked at ...
    assert 150_000 <= stats["kept"] < n          # ... and only the 15-45 DTE monthlies kept (4,000 x 2 x 10 x 2)
    assert len(got) >= 0.99 * n_sym
    assert secs < 5.0, secs
