r"""Check the Options page's Volatility surface against its sources (v4.139) - READ-ONLY.

Run on Hermes (where the screener DB and the contract history live):

    cd C:\trading-skills\TradeHunter\dashboard_tst
    .\.venv\Scripts\python.exe deploy\check_vol_surface.py AAPL MSFT SPY
    .\.venv\Scripts\python.exe deploy\check_vol_surface.py AAPL --day 2026-10-09

Five checks per symbol, each ending in PASS / AGREES / CHECK / FAIL with its numbers:

1. DISPLAY       - every cell of the "Latest" surface (and its ATM row) equals the implied
                   volatility stored for that contract from Massive (scr_contract), for each of the
                   four "Option in each cell" choices: nothing lost, moved or altered on the way.
2. MASSIVE/PRICE - Massive's IV against the IV solved HERE from the same contract's last trade and
                   the underlying price: does Massive's number fit the contract's own price?
3. CALL/PUT      - Massive's call and put IV at the same strike and expiry (in theory nearly equal).
4. SAME SESSION  - Massive's end-of-day IV against the IV solved from Massive's daily file for that
                   same session (two independent routes to one number; once that day is converted).
5. PAST DAY      - every cell of a past day's surface (TradeHunter's own calculation) against the
                   solve here from the Parquet closes.
6. IBKR          - with --ibkr: Massive's IV against IBKR's own option model for the same contracts,
                   from a read-only snapshot taken on the laptop (deploy/ibkr_iv_snapshot.py, synced
                   here by Resilio into <MarketData>\iv_check). A source outside Massive entirely.

Plus a short list of liquid contracts to compare by eye with the Implied Vol column in TWS.

The solver here is SEPARATE from the app's (app/services/black_scholes.py, scr_flativ): its own
Black-Scholes with statistics.NormalDist for the normal CDF (the app uses math.erf) and its own
bisection, at the platform's rate (opt_constants.RISK_FREE), no dividend, T = calendar days / 365.
Massive does not publish its own model, rate, dividend or which price it solves from, so small
differences in check 2 / 4 are expected; what matters is that liquid near-the-money contracts agree.
Nothing is written anywhere.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import os
import statistics
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

DASH = Path(__file__).resolve().parent.parent
if str(DASH) not in sys.path:
    sys.path.insert(0, str(DASH))

NEAR = 0.10              # "near the money": strike within 10 % of the price
MIN_DTE = 7              # ... at least a week to expiry (shorter = tiny vega, noisy IV)
MIN_VOL = 100            # ... and at least 100 contracts traded that day
FRESH_MIN = 30           # check 2: the last trade within 30 min of the close
OK_PTS = 2.0             # "agrees" = within 2 volatility points ...
OK_SHARE = 0.90          # ... for 90 % of the liquid near-the-money contracts
_N = statistics.NormalDist()


# ------------------------------ an independent Black-Scholes ------------------------------

def bs_price(S: float, K: float, T: float, r: float, sig: float, call: bool) -> float:
    st = sig * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / st
    d2 = d1 - st
    disc = K * math.exp(-r * T)
    if call:
        return S * _N.cdf(d1) - disc * _N.cdf(d2)
    return disc * _N.cdf(-d2) - S * _N.cdf(-d1)


def solve_iv(price, S, K, T, r, call) -> float | None:
    """The sigma that prices ``price`` (FRACTION), or None (no price, no time, outside 0.01 % - 1000 %)."""
    try:
        price, S, K, T = float(price), float(S), float(K), float(T)
    except (TypeError, ValueError):
        return None
    if not (price > 0 and S > 0 and K > 0 and T > 0):
        return None
    lo, hi = 1e-4, 10.0
    if not (bs_price(S, K, T, r, lo, call) < price < bs_price(S, K, T, r, hi, call)):
        return None
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if bs_price(S, K, T, r, mid, call) > price:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-12:
            break
    return 0.5 * (lo + hi)


# ------------------------------ small helpers ------------------------------

def _d(x) -> _dt.date | None:
    if isinstance(x, _dt.datetime):
        return x.date()
    if isinstance(x, _dt.date):
        return x
    try:
        return _dt.date.fromisoformat(str(x)[:10])
    except ValueError:
        return None


def _f(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def stats_line(diffs: list[float]) -> str:
    if not diffs:
        return "no contracts to compare"
    a = sorted(abs(x) for x in diffs)
    n = len(a)
    p90 = a[min(n - 1, int(round(0.9 * (n - 1))))]
    w1 = sum(1 for x in a if x <= 1.0) / n
    w2 = sum(1 for x in a if x <= OK_PTS) / n
    return ("%d contracts: median gap %.2f pts, 90%% within %.2f pts, %.0f%% within 1 pt, %.0f%% within 2 pts, "
            "average direction %+.2f pts" % (n, statistics.median(a), p90, 100 * w1, 100 * w2, statistics.fmean(diffs)))


def share_ok(diffs: list[float]) -> float:
    return sum(1 for x in diffs if abs(x) <= OK_PTS) / len(diffs) if diffs else 0.0


def verdict(ok: bool, good="AGREES", bad="CHECK") -> str:
    return good if ok else bad


def tag(e, right, k) -> str:
    return "%s %s %s" % (_d(e).isoformat(), "C" if right == "C" else "P", ("%g" % k))


# ------------------------------ the checks ------------------------------

def load_latest(s, sym):
    from app.screener_models import ScrContract as C, ScrUnderlying as U

    u = s.query(U).filter(U.symbol == sym).first()
    rows = (s.query(C.expiry, C.right, C.strike, C.iv, C.last, C.volume, C.oi, C.last_trade, C.session, C.as_of)
             .filter(C.symbol == sym).all())
    return u, rows


def check_display(s, sym, u, rows, vs) -> bool:
    """1. The Latest surface shows exactly the stored IVs."""
    print("\n1. DISPLAY - the 'Latest' grid against the stored Massive IVs (every strike, every expiry)")
    if not rows or u is None or not u.spot:
        print("   SKIP - no stored chain / price for %s" % sym)
        return True
    spot = float(u.spot)
    session = max(r.session for r in rows)
    on = _d(session)
    stored = {}
    for r in rows:
        e, iv, k = _d(r.expiry), _f(r.iv), _f(r.strike)
        if e is None or iv is None or k is None or not (vs.IV_LO <= iv <= vs.IV_HI) or (e - on).days < 1:
            continue
        stored[(k, e, r.right)] = iv
    all_ok = True
    for side in ("otm", "call", "put", "mid"):
        g = vs.surface(s, sym, "latest", side=side, band=0, max_dte=0)
        exps = [_d(x["date"]) for x in g["expiries"]]
        bad = shown = 0
        for i, k in enumerate(g["strikes"]):
            for j, e in enumerate(exps):
                c, p = stored.get((k, e, "C")), stored.get((k, e, "P"))
                if side == "call":
                    want = c
                elif side == "put":
                    want = p
                elif side == "mid":
                    vals = [v for v in (c, p) if v is not None]
                    want = sum(vals) / len(vals) if vals else None
                else:
                    want = p if k < spot else c
                want = round(want * 100, 2) if want is not None else None
                got = g["cells"][i][j]
                if got is not None:
                    shown += 1
                if (got is None) != (want is None) or (got is not None and abs(got - want) > 0.005):
                    bad += 1
                    if bad <= 3:
                        print("   mismatch %s strike %g %s: shown %s, stored %s" % (side, k, e, got, want))
        # every stored contract inside the grid's rows / columns is shown
        print("   %-4s %s - %d cells shown, %d differ from the stored IV" % (side, "PASS" if not bad else "FAIL", shown, bad))
        all_ok = all_ok and not bad
    g = vs.surface(s, sym, "latest", side="otm", band=0, max_dte=0)
    bad_atm = 0
    for j, x in enumerate(g["expiries"]):
        k = g["atm_strikes"][j]
        e = _d(x["date"])
        vals = [v for v in (stored.get((k, e, "C")), stored.get((k, e, "P"))) if v is not None]
        want = round(sum(vals) / len(vals) * 100, 2) if vals else None
        if (want is None) != (g["atm"][j] is None) or (want is not None and abs(want - g["atm"][j]) > 0.005):
            bad_atm += 1
    print("   ATM  %s - %d expiries, %d differ" % ("PASS" if not bad_atm else "FAIL", len(g["expiries"]), bad_atm))
    return all_ok and not bad_atm


def check_massive_vs_price(sym, u, rows, r, clock) -> list:
    """2. Massive's IV against the IV solved here from the contract's last trade."""
    print("\n2. MASSIVE/PRICE - Massive's IV vs the IV solved here from each contract's last trade")
    if not rows or u is None or not u.spot:
        print("   SKIP - no stored chain / price")
        return []
    spot = float(u.spot)
    session = max(r_.session for r_ in rows)
    on = _d(session)
    close_et = _dt.datetime.combine(on, _dt.time(16, 0), tzinfo=clock.et_now().tzinfo)
    close_utc = close_et.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    print("   underlying price %.2f (source: %s, %s) - session %s" % (
        spot, u.spot_src or "?", {"massive": "Massive's snapshot", "parity": "estimated by TradeHunter from "
                                  "the option prices", "close": "the last close"}.get(u.spot_src or "", "?"), session))
    liquid, other, rows_out = [], [], []
    for x in rows:
        e, k, iv, last = _d(x.expiry), _f(x.strike), _f(x.iv), _f(x.last)
        if e is None or k is None or iv is None or last is None or last <= 0:
            continue
        dte = (e - on).days
        if dte < 1:
            continue
        mine = solve_iv(last, spot, k, dte / 365.0, r, x.right == "C")
        if mine is None:
            continue
        diff = (iv - mine) * 100.0
        fresh = x.last_trade is not None and x.last_trade >= close_utc - _dt.timedelta(minutes=FRESH_MIN)
        is_liquid = abs(k / spot - 1) <= NEAR and dte >= MIN_DTE and (x.volume or 0) >= MIN_VOL and fresh
        (liquid if is_liquid else other).append(diff)
        if is_liquid:
            rows_out.append((abs(diff), x, mine, dte))
    ok = share_ok(liquid) >= OK_SHARE
    print("   liquid near the money (strike within 10%%, 7+ days, 100+ traded, last trade in the last 30 min): %s"
          % stats_line(liquid))
    print("   everything else with a price: %s" % stats_line(other))
    print("   %s - %.0f%% of the liquid contracts within %.0f pts (needs %.0f%%)" % (
        verdict(ok), 100 * share_ok(liquid), OK_PTS, 100 * OK_SHARE))
    worst = sorted(rows_out, key=lambda t: -t[0])[:6]
    if worst:
        print("   largest gaps among the liquid ones:")
        for _g, x, mine, dte in worst:
            print("     %-22s last %7.2f  Massive %6.1f%%  solved %6.1f%%  gap %+5.1f  vol %6d  last trade %s ET"
                  % (tag(x.expiry, x.right, x.strike), x.last, x.iv * 100, mine * 100, (x.iv - mine) * 100,
                     x.volume or 0, clock.et_now(x.last_trade.replace(tzinfo=_dt.timezone.utc)).strftime("%H:%M")
                     if x.last_trade else "?"))
    return sorted(rows_out, key=lambda t: (t[3], abs(_f(t[1].strike) / spot - 1)))


def check_call_put(u, rows) -> None:
    """3. Massive's call and put IV at the same strike and expiry."""
    print("\n3. CALL/PUT - Massive's call IV minus put IV at the same strike (strikes within 10% of the price)")
    if not rows or u is None or not u.spot:
        print("   SKIP")
        return
    spot = float(u.spot)
    on = _d(max(r.session for r in rows))
    by = {}
    for x in rows:
        e, k, iv = _d(x.expiry), _f(x.strike), _f(x.iv)
        if e is None or k is None or iv is None or abs(k / spot - 1) > NEAR or (e - on).days < 1:
            continue
        by.setdefault((e, k), {})[x.right] = iv
    buckets = {"1-7 days": [], "8-30 days": [], "31-90 days": [], "91+ days": []}
    for (e, k), legs in by.items():
        if "C" in legs and "P" in legs:
            dte = (e - on).days
            key = "1-7 days" if dte <= 7 else "8-30 days" if dte <= 30 else "31-90 days" if dte <= 90 else "91+ days"
            buckets[key].append((legs["C"] - legs["P"]) * 100)
    for key, v in buckets.items():
        if v:
            print("   %-10s %4d pairs: median gap %.2f pts, average call-minus-put %+.2f pts"
                  % (key, len(v), statistics.median(abs(x) for x in v), statistics.fmean(v)))
    print("   (a steady call-minus-put offset = a different forward price - dividend, rate or the price timing -"
          " not a wrong number; it is what the OTM view and the ATM average absorb)")


def check_same_session(s, sym, u, rows, r, vs, oh) -> None:
    """4. Massive's end-of-day IV vs the IV solved from the daily file of the same session."""
    print("\n4. SAME SESSION - Massive's end-of-day IV vs the IV solved from Massive's daily file for that session")
    if not rows:
        print("   SKIP")
        return
    session = max(x.session for x in rows)
    root = vs.option_root_of(s, sym)
    if root is None:
        print("   SKIP - an index: the daily files carry no index close")
        return
    tbl = oh.chain(root, session, columns=["expiry", "right", "strike", "close", "volume"])
    if tbl is None:
        print("   PENDING - the daily file for %s is not converted yet (Massive publishes it ~11:00 ET the next day;"
              " the collector converts it within minutes). Run this again after that." % session)
        return
    spot = vs.raw_closes(s, sym, [session]).get(session)
    if spot is None:
        print("   SKIP - no stock close on file for %s yet" % session)
        return
    mass = {(_d(x.expiry), x.right, _f(x.strike)): _f(x.iv) for x in rows}
    on = _d(session)
    liquid, other = [], []
    for x in tbl.to_pylist():
        e, k, c = x["expiry"], _f(x["strike"]), _f(x["close"])
        iv = mass.get((e, x["right"], k))
        if iv is None or c is None or c <= 0 or (e - on).days < 1:
            continue
        mine = solve_iv(c, spot, k, (e - on).days / 365.0, r, x["right"] == "C")
        if mine is None:
            continue
        diff = (iv - mine) * 100
        if abs(k / spot - 1) <= NEAR and (e - on).days >= MIN_DTE and (x["volume"] or 0) >= MIN_VOL:
            liquid.append(diff)
        else:
            other.append(diff)
    print("   stock close %.2f on %s" % (spot, session))
    print("   liquid near the money: %s" % stats_line(liquid))
    print("   everything else: %s" % stats_line(other))
    print("   %s - %.0f%% of the liquid contracts within %.0f pts" % (verdict(share_ok(liquid) >= OK_SHARE),
                                                                    100 * share_ok(liquid), OK_PTS))


def check_past_day(s, sym, day, r, vs, oh) -> bool:
    """5. A past day's surface (TradeHunter's calculation) vs the independent solve here."""
    days = oh.converted_days()
    if not days:
        print("\n5. PAST DAY - SKIP: no day converted to Parquet yet")
        return True
    day = day or days[-1]
    print("\n5. PAST DAY - TradeHunter's surface for %s vs the solve here from the same Parquet closes" % day)
    root = vs.option_root_of(s, sym)
    if root is None:
        print("   SKIP - an index")
        return True
    try:
        # v4.225: the surface of a past day is the closing snapshot (or blank); the solve from the closes
        # still feeds the cell colours, so that is what is checked here
        rows, meta = vs.trade_rows(s, sym, day, band=0, max_dte=0)
        g = vs.build(rows, meta["spot"], meta["on"], side="otm", band=0, max_dte=0)
    except vs.SurfaceError as exc:
        print("   SKIP - %s" % exc)
        return True
    spot = vs.raw_closes(s, sym, [day]).get(day)
    tbl = oh.chain(root, day, columns=["expiry", "right", "strike", "close"])
    on = _d(day)
    closes = {(x["expiry"], x["right"], _f(x["strike"])): _f(x["close"]) for x in tbl.to_pylist()}
    worst, n, bad = 0.0, 0, 0
    for i, k in enumerate(g["strikes"]):
        for j, x in enumerate(g["expiries"]):
            got = g["cells"][i][j]
            if got is None:
                continue
            e = _d(x["date"])
            right = "P" if k < spot else "C"
            mine = solve_iv(closes.get((e, right, k)), spot, k, (e - on).days / 365.0, r, right == "C")
            n += 1
            if mine is None:
                bad += 1
                continue
            d = abs(got - mine * 100)
            worst = max(worst, d)
            if d > 0.011:
                bad += 1
                if bad <= 3:
                    print("   differs: %s %g: surface %.2f, solved here %.2f" % (e, k, got, mine * 100))
    print("   %s - %d cells, largest difference %.4f pts, %d differ by more than 0.01" % (
        "PASS" if not bad else "FAIL", n, worst, bad))
    return not bad


def find_snapshot(arg, oh) -> Path | None:
    """The IBKR snapshot: the path given, or the newest ibkr_*.json in <MarketData>/iv_check."""
    if arg and arg != "newest":
        return Path(arg)
    roots = []
    hr = oh.history_root()
    if hr.name == "option_history":
        roots.append(hr.parent / "iv_check")
    ph = (os.environ.get("TST_PRICE_HISTORY_DIR") or "").strip()
    if ph:
        roots.append(Path(ph).parent / "iv_check")
    roots += [Path("C:/HermesSync/MarketData/iv_check"), Path("D:/HermesSync/MarketData/iv_check")]
    for r in roots:
        files = sorted(r.glob("ibkr_*.json")) if r.is_dir() else []
        if files:
            return files[-1]
    return None


def check_ibkr(sym, u, rows, snap, r, clock) -> None:
    """6. Massive's IV against IBKR's own model IV for the same contracts."""
    print("\n6. IBKR - Massive's IV vs IBKR's own option model (snapshot %s ET, %s)"
          % (snap.get("taken_et"), snap.get("source", "")))
    s = (snap.get("symbols") or {}).get(sym)
    if not s:
        print("   SKIP - %s is not in the IBKR snapshot" % sym)
        return
    if not rows or u is None or not u.spot:
        print("   SKIP - no stored Massive chain")
        return
    taken = _dt.datetime.fromisoformat(snap["taken_utc"]).astimezone(_dt.timezone.utc).replace(tzinfo=None)
    mass = {(_d(x.expiry), x.right, _f(x.strike)): x for x in rows}
    spot_m, spot_i = float(u.spot), float(s["spot"])
    print("   price: Massive %.2f (%s, as of %s ET) vs IBKR %.2f (%s) - %+.2f%%" % (
        spot_m, u.spot_src or "?", clock.et_now(u.spot_as_of.replace(tzinfo=_dt.timezone.utc)).strftime("%H:%M")
        if u.spot_as_of else "?", spot_i, s.get("spot_src"), 100 * (spot_m / spot_i - 1)))
    liquid, other, gaps, worst, model = [], [], [], [], []
    for c in s.get("contracts") or ():
        e, k, ib_iv = _d(c.get("expiry")), _f(c.get("strike")), _f(c.get("iv_model"))
        x = mass.get((e, c.get("right"), k))
        if x is None or ib_iv is None or ib_iv <= 0 or _f(x.iv) is None:
            continue
        diff = (x.iv - ib_iv) * 100
        if x.as_of is not None:
            gaps.append(abs((x.as_of - taken).total_seconds()) / 60.0)
        bid, ask = _f(c.get("bid")), _f(c.get("ask"))
        mid = (bid + ask) / 2 if (bid and ask and ask >= bid) else None
        tight = mid is not None and (ask - bid) <= 0.10 * mid
        dte = (e - taken.date()).days
        und = _f(c.get("und_price")) or spot_i
        if mid is not None and dte >= 1:              # IBKR's model vs the plain one here, on IBKR's own mid
            mine = solve_iv(mid, und, k, dte / 365.0, r, c.get("right") == "C")
            if mine is not None:
                model.append((ib_iv - mine) * 100)
        if abs(k / spot_i - 1) <= NEAR and dte >= MIN_DTE and tight:
            liquid.append(diff)
            worst.append((abs(diff), c, x, ib_iv))
        else:
            other.append(diff)
    if gaps:
        print("   time gap between Massive's read (15 min delayed) and the IBKR snapshot: median %.0f min" %
              statistics.median(gaps))
    print("   liquid near the money (strike within 10%%, 7+ days, IBKR bid/ask spread under 10%%): %s"
          % stats_line(liquid))
    print("   everything else matched: %s" % stats_line(other))
    if model:
        print("   (for scale: IBKR's model vs the plain Black-Scholes here on the same IBKR mid price: median gap %.2f pts,"
              " average %+.2f - dividends, rates and early exercise)"
              % (statistics.median(abs(x) for x in model), statistics.fmean(model)))
    print("   %s - %.0f%% of the liquid contracts within %.0f pts of IBKR (needs %.0f%%)" % (
        verdict(share_ok(liquid) >= OK_SHARE), 100 * share_ok(liquid), OK_PTS, 100 * OK_SHARE))
    for _g, c, x, ib_iv in sorted(worst, key=lambda t: -t[0])[:6]:
        print("     %-22s Massive %6.1f%%  IBKR %6.1f%%  gap %+5.1f   IBKR bid/ask %s / %s" % (
            tag(c["expiry"], c["right"], c["strike"]), x.iv * 100, ib_iv * 100, (x.iv - ib_iv) * 100,
            c.get("bid"), c.get("ask")))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Check the Volatility surface against its sources (read-only).")
    ap.add_argument("symbols", nargs="+", help="underlyings, e.g. AAPL MSFT SPY")
    ap.add_argument("--day", default=None, help="the past day for check 5 (default: the newest converted)")
    ap.add_argument("--ibkr", nargs="?", const="newest", default=None,
                    help="check 6: compare with an IBKR snapshot (a path, or the newest in <MarketData>\\iv_check)")
    a = ap.parse_args(argv)
    from app import screener_db
    from app.services import clock, opt_history as oh, vol_surface as vs
    from app.services.opt_constants import RISK_FREE

    print("Volatility surface check - %s ET - rate %.1f%%, no dividend, T = days / 365"
          % (clock.et_now().strftime("%Y-%m-%d %H:%M"), RISK_FREE * 100))
    print("Contract history: %s (%d days converted)" % (oh.history_root(), len(oh.converted_days())))
    snap = None
    if a.ibkr:
        path = find_snapshot(a.ibkr, oh)
        if path is None or not path.is_file():
            print("IBKR snapshot: none found (run deploy\\ibkr_iv_snapshot.py on the laptop; Resilio brings it here)")
        else:
            snap = json.loads(path.read_text(encoding="utf-8"))
            print("IBKR snapshot: %s (taken %s ET)" % (path, snap.get("taken_et")))
    failed = False
    with screener_db.session() as s:
        for sym in [x.strip().upper() for x in a.symbols if x.strip()]:
            print("\n" + "=" * 100 + "\n%s" % sym + "\n" + "=" * 100)
            u, rows = load_latest(s, sym)
            if u is None and not rows:
                print("not in the screener")
                continue
            print("%s - %d contracts in the last market pass (session %s, read %s UTC)" % (
                u.name if u else sym, len(rows), max((x.session for x in rows), default="-"),
                max((x.as_of for x in rows), default=None)))
            try:
                failed |= not check_display(s, sym, u, rows, vs)
                liquid = check_massive_vs_price(sym, u, rows, RISK_FREE, clock)
                check_call_put(u, rows)
                check_same_session(s, sym, u, rows, RISK_FREE, vs, oh)
                failed |= not check_past_day(s, sym, a.day, RISK_FREE, vs, oh)
                if snap is not None:
                    check_ibkr(sym, u, rows, snap, RISK_FREE, clock)
            except Exception as exc:  # noqa: BLE001 - report and go on with the next symbol
                print("   ERROR - %s: %s" % (type(exc).__name__, exc))
                failed = True
                continue
            if liquid:
                print("\nCompare in TWS (Option Chain, Implied Vol column; best during US market hours):")
                seen = set()
                for _g, x, mine, dte in liquid:
                    key = (x.expiry, x.right)
                    if key in seen or len(seen) >= 6:
                        continue
                    seen.add(key)
                    print("   %-22s Massive %6.1f%%   solved here %6.1f%%   (%d days)"
                          % (tag(x.expiry, x.right, x.strike), x.iv * 100, mine * 100, dte))
    print("\nDone - nothing was written.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
