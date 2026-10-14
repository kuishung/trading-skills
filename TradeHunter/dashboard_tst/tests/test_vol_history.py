"""Each contract's own IV history behind the Volatility surface (v4.141): ``app/services/vol_history.py``
- the numpy-only solver against the scalar one, the per-cell figures (rank in its own range, share of
days below, the sparkline points), and the grid payload end to end on a synthetic 30-day contract
history whose IVs rise day by day (so the ranks are known). A temp screener DB and a temp Parquet
folder; no network."""
from __future__ import annotations

import datetime as _dt
import gzip
import json

import numpy as np
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.screener_models import ScrBase, ScrContract, ScrUnderlying, ScrUnderlyingDaily
from app.services import clock, flat_files, opt_history, vol_history, vol_surface
from app.services.black_scholes import black_scholes, implied_vol
from app.services.opt_constants import RISK_FREE

pytest.importorskip("pyarrow")

ON = _dt.date(2026, 10, 7)                       # the surface's day (Wednesday)
NOV, DEC = _dt.date(2026, 11, 20), _dt.date(2026, 12, 18)
SPOT = 100.0
N_DAYS = 30
STRIKES = (90.0, 95.0, 100.0, 105.0, 110.0)


def bs(S, k, exp, right, sig, day):
    return black_scholes(S, k, (exp - day).days / 365.0, RISK_FREE, sig, "call" if right == "C" else "put").price


def sessions_before(day: _dt.date, n: int) -> list[_dt.date]:
    out, d = [], day
    while len(out) < n:
        d = clock.prev_trading_day(d)
        out.append(d)
    return list(reversed(out))


def iv_on(i: int) -> float:
    """Day i of the history (0 = oldest): 20 % rising 1 point a day."""
    return 0.20 + 0.01 * i


# ───────────────────────────────────────── the solver ─────────────────────────────────────────

def test_the_fast_solver_matches_the_scalar_one_to_the_fourth_decimal():
    import math

    rng = np.random.default_rng(3)
    n = 400
    S = np.full(n, 100.0)
    K = rng.uniform(70, 130, n).round(1)
    days = rng.integers(3, 400, n)
    T = days / 365.0
    call = rng.random(n) < 0.5
    sig = rng.uniform(0.12, 0.9, n)
    p = np.array([bs(S[i], K[i], ON + _dt.timedelta(days=int(days[i])), "C" if call[i] else "P", sig[i], ON)
                  for i in range(n)])
    got = vol_history.implied_vols_fast(p, S, K, T, call)
    ok = ~np.isnan(got)
    assert ok.mean() > 0.9
    # the solved vol reprices every contract to within a hundredth of a cent ...
    for i in np.nonzero(ok)[0]:
        again = bs(S[i], K[i], ON + _dt.timedelta(days=int(days[i])), "C" if call[i] else "P", got[i], ON)
        assert abs(again - p[i]) < 1e-4, (i, p[i], again)
    # ... and where the price moves with the vol (vega >= 0.5 per unit vol) the input vol comes back
    d1 = (np.log(S / K) + (RISK_FREE + 0.5 * sig * sig) * T) / (sig * np.sqrt(T))
    vega = S * np.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi) * np.sqrt(T)
    live = ok & (vega >= 0.5)
    assert live.sum() > 200
    assert np.max(np.abs(got[live] - sig[live])) < 2e-4
    for i in np.nonzero(live)[0][:40]:                               # and agrees with the scalar solver
        ref = implied_vol(p[i], S[i], K[i], T[i], "call" if call[i] else "put")
        assert ref is not None and abs(ref - got[i]) < 2e-4
    # no price / no time / an impossible price: NaN, never an exception
    bad = vol_history.implied_vols_fast([0.0, 5.0, 1000.0], [100, 100, 100], [100, 100, 100], [0.1, 0.0, 0.1],
                                        [True, True, True])
    assert np.isnan(bad).all()
    assert vol_history.implied_vols_fast([], [], [], [], []).size == 0


def test_cell_stats_rank_share_and_sparkline_points():
    on = np.arange(20000, 20000 + 300)
    iv = np.linspace(20.0, 49.9, 300)                                    # 300 days, rising
    s = vol_history.cell_stats(49.9, on, iv)
    assert s["n"] == 300 and s["lo"] == 20.0 and s["hi"] == 49.9 and s["rank"] == 1.0
    assert s["pct"] == pytest.approx(299 / 300, abs=1e-4)
    assert len(s["series"]) == vol_history.SPARK_POINTS                  # downsampled ...
    assert s["series"][0] == [20000, 20.0] and s["series"][-1] == [20299, 49.9]   # ... first and last kept
    assert s["first"] == "2024-10-04" and s["last"] == "2025-07-30"       # days 20000 and 20299 since 1970
    mid = vol_history.cell_stats(35.0, on, iv)
    assert mid["rank"] == pytest.approx((35 - 20) / 29.9, abs=1e-3)
    assert vol_history.cell_stats(60.0, on, iv)["rank"] == 1.0            # above its range: clamped
    assert vol_history.cell_stats(10.0, on, iv)["rank"] == 0.0
    short = vol_history.cell_stats(25.0, on[:5], iv[:5])
    assert short["n"] == 5 and short["rank"] is None and short["pct"] is None and len(short["series"]) == 5
    flat = vol_history.cell_stats(30.0, on[:20], np.full(20, 30.0))
    assert flat["rank"] == 0.5
    assert vol_history.cell_stats(30.0, on[:0], iv[:0]) is None
    assert vol_history.cell_stats(None, on, iv)["rank"] is None


# ───────────────────────────────────────── end to end ─────────────────────────────────────────

@pytest.fixture
def sdb(tmp_path):
    eng = sa.create_engine("sqlite:///" + (tmp_path / "screener.db").as_posix(), future=True)
    ScrBase.metadata.create_all(eng)
    s = sessionmaker(bind=eng, future=True)()
    s.add(ScrUnderlying(symbol="AAPL", name="Apple Inc.", sec_type="stock", spot=SPOT, spot_src="massive",
                        history_done=True, history_tries=0))
    s.add(ScrUnderlying(symbol="SPX", name="S&P 500", sec_type="index", spot=5000.0, history_done=False,
                        history_tries=0))
    asof = _dt.datetime(2026, 10, 7, 19, 30)
    # the latest chain: the 100 call at the history's top (49 %), the 100 put at its bottom (20 %),
    # the 90 put a third of the way up (29.67 %), everything else 30 %
    for exp in (NOV, DEC):
        for k in STRIKES:
            for rt in "CP":
                iv = 0.30
                if k == 100.0 and rt == "C":
                    iv = iv_on(N_DAYS - 1)
                elif k == 100.0 and rt == "P":
                    iv = iv_on(0)
                elif k == 90.0 and rt == "P":
                    iv = 0.2967
                s.add(ScrContract(symbol="AAPL", expiry=exp.isoformat(), right=rt, strike=k, weekly=False, iv=iv,
                                  session=ON.isoformat(), as_of=asof))
    for d in sessions_before(ON, N_DAYS) + [ON]:
        s.add(ScrUnderlyingDaily(symbol="AAPL", on=d.isoformat(), close=SPOT, close_raw=SPOT))
    s.commit()
    yield s
    s.close()
    eng.dispose()


@pytest.fixture
def hist(tmp_path, monkeypatch):
    """30 sessions of AAPL day files before ON, every contract priced at ``iv_on(i)`` (rising),
    converted to Parquet; the app aimed at that folder."""
    flat = tmp_path / "flat"
    man = {}
    for i, day in enumerate(sessions_before(ON, N_DAYS)):
        lines = ["ticker,volume,open,close,high,low,window_start,transactions"]
        for exp in (NOV, DEC):
            for k in STRIKES:
                for rt in "CP":
                    p = round(bs(SPOT, k, exp, rt, iv_on(i), day), 4)
                    lines.append("O:AAPL%s%s%08d,10,%s,%s,%s,%s,1,2" % (exp.strftime("%y%m%d"), rt, int(k * 1000),
                                                                       p, p, p, p))
        key = flat_files.key_for(day)
        path = flat.joinpath(*key.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
            f.write("\n".join(lines) + "\n")
        man[key] = {"day": day.isoformat(), "size": path.stat().st_size, "etag": "x", "downloaded_at": None}
    (flat / "manifest.json").write_text(json.dumps(man), encoding="utf-8")
    root = tmp_path / "pq"
    assert opt_history.Store(root=root, flat_root=flat).run()["converted"] == N_DAYS
    monkeypatch.setenv("TST_OPTION_HISTORY_DIR", str(root))
    vol_history._TABLES.clear()
    vol_history._SERIES.clear()
    return root


def test_the_grid_payload_ranks_each_cell_in_its_own_history(sdb, hist):
    grid = vol_surface.surface(sdb, "AAPL", "latest", side="otm", band=20)
    h = vol_history.grid_history(sdb, "AAPL", grid)
    assert h["days"] == N_DAYS and h["window"] == [sessions_before(ON, N_DAYS)[0].isoformat(),
                                                   sessions_before(ON, N_DAYS)[-1].isoformat()]
    assert h["note"] is None and "Black-Scholes" in h["method"]
    rows = {k: i for i, k in enumerate(grid["strikes"])}
    cols = {e["date"]: j for j, e in enumerate(grid["expiries"])}
    c100 = h["cells"]["%d,%d" % (rows[100.0], cols[NOV.isoformat()])]        # the call at its top
    assert c100["n"] == N_DAYS and c100["rank"] == 1.0 and c100["pct"] == pytest.approx(29 / 30, abs=1e-3)
    assert c100["lo"] == pytest.approx(20.0, abs=0.05) and c100["hi"] == pytest.approx(49.0, abs=0.05)
    assert len(c100["series"]) == N_DAYS and c100["series"][-1][1] == pytest.approx(49.0, abs=0.05)
    assert c100["first"] == sessions_before(ON, N_DAYS)[0].isoformat()
    p90 = h["cells"]["%d,%d" % (rows[90.0], cols[DEC.isoformat()])]          # the put a third of the way up
    assert p90["rank"] == pytest.approx((29.67 - 20) / 29.0, abs=0.01)
    assert p90["pct"] == pytest.approx(10 / 30, abs=0.04)
    assert "atm" not in h                                                 # no ATM row (v4.143: not a contract)
    # every shown cell has an entry, the series dates are days since the epoch
    assert len(h["cells"]) == grid["n_cells"] and h["epoch"] == "1970-01-01"
    d0 = _dt.date(1970, 1, 1) + _dt.timedelta(days=c100["series"][0][0])
    assert d0.isoformat() == c100["first"]


def test_the_history_is_cached_and_a_past_day_excludes_itself(sdb, hist):
    grid = vol_surface.surface(sdb, "AAPL", "latest", side="call", band=20)
    vol_history.grid_history(sdb, "AAPL", grid)
    assert len(vol_history._TABLES) == 1 and vol_history._SERIES
    tables_before = dict(vol_history._TABLES)
    h2 = vol_history.grid_history(sdb, "AAPL", grid)              # the second call: no re-read
    assert dict(vol_history._TABLES) == tables_before and h2["days"] == N_DAYS
    # a past day's surface: its history stops the session before it
    last = sessions_before(ON, N_DAYS)[-1]
    g = vol_surface.surface(sdb, "AAPL", last.isoformat(), side="call", band=20)
    h = vol_history.grid_history(sdb, "AAPL", g)
    assert h["days"] == N_DAYS - 1 and h["window"][1] == sessions_before(ON, N_DAYS)[-2].isoformat()
    rows = {k: i for i, k in enumerate(g["strikes"])}
    cols = {e["date"]: j for j, e in enumerate(g["expiries"])}
    c = h["cells"]["%d,%d" % (rows[100.0], cols[NOV.isoformat()])]
    assert c["rank"] == 1.0 and c["n"] == N_DAYS - 1                  # that day's own IV (49 %) is the top so far


def test_two_leg_cells_use_only_the_days_both_legs_traded():
    """The review's finding: a day with one leg only would put that leg's level into the history
    the call + put average is ranked against."""
    on_c, iv_c = np.array([1, 2, 3, 4, 5]), np.array([20.0, 21.0, 22.0, 23.0, 24.0])
    on_p, iv_p = np.array([2, 3, 5, 6]), np.array([30.0, 31.0, 33.0, 60.0])     # 60 on a day the call did not trade
    on, iv = vol_history._merge_mean([(on_c, iv_c), (on_p, iv_p)])
    assert on.tolist() == [2, 3, 5] and iv.tolist() == [25.5, 26.5, 28.5]
    assert vol_history._merge_mean([(on_c, iv_c), None])[0].size == 0            # a leg with no history: none
    assert vol_history._merge_mean([(on_c, iv_c), (np.zeros(0, np.int64), np.zeros(0))])[0].size == 0
    assert vol_history._merge_mean([(on_c, iv_c)]) == (on_c, iv_c)              # one leg: as it is
    assert vol_history._merge_mean([(on_c, iv_c), (np.array([9]), np.array([1.0]))])[0].size == 0   # no common day


def test_the_payload_names_its_grid_and_the_sparkline_keeps_the_extremes(sdb, hist):
    grid = vol_surface.surface(sdb, "AAPL", "latest", side="otm", band=20)
    h = vol_history.grid_history(sdb, "AAPL", grid)
    assert h["strikes"] == grid["strikes"] and h["expiries"] == [e["date"] for e in grid["expiries"]]
    assert h["spot"] == grid["spot"] and h["side"] == "otm" and h["skipped"] == []
    on = np.arange(20000, 20000 + 300)
    iv = np.full(300, 30.0)
    iv[137], iv[211] = 10.0, 55.0                                              # a dip and a spike on odd days
    s = vol_history.cell_stats(30.0, on, iv)
    pts = {p[0]: p[1] for p in s["series"]}
    assert pts[20137] == 10.0 and pts[20211] == 55.0 and len(s["series"]) <= vol_history.SPARK_POINTS + 2


def test_a_window_with_closes_missing_is_not_cached(sdb, hist):
    grid = vol_surface.surface(sdb, "AAPL", "latest", side="call", band=20)
    days = vol_history.history_window(grid["on"])
    key = vol_history.contract_key(NOV, "C", 100.0)
    few = {d: SPOT for d in days[-5:]}                                        # the stock lane behind
    got = vol_history.contract_series("AAPL", [key], few, grid["on"])
    assert len(got[key][0]) == 5
    assert all(key not in st for st in vol_history._SERIES.values())          # not kept ...
    full = vol_history.contract_series("AAPL", [key], {d: SPOT for d in days}, grid["on"])
    assert len(full[key][0]) == N_DAYS                                        # ... so the full solve comes next time
    assert any(key in st for st in vol_history._SERIES.values())


def test_an_index_and_a_missing_history_say_so(sdb, hist, monkeypatch):
    g = vol_surface.surface(sdb, "SPX", "latest", side="call", band=50) if False else None   # (no SPX chain seeded)
    grid = {"on": ON.isoformat(), "spot": 5000.0, "side": "call", "strikes": [5000.0],
            "expiries": [{"date": NOV.isoformat()}], "cells": [[20.0]], "atm": [20.0], "atm_strikes": [5000.0]}
    h = vol_history.grid_history(sdb, "SPX", grid)
    assert h["cells"] == {} and "index" in h["note"]
    monkeypatch.setenv("TST_OPTION_HISTORY_DIR", str(hist.parent / "empty"))
    g2 = vol_surface.surface(sdb, "AAPL", "latest", side="call", band=20)
    h2 = vol_history.grid_history(sdb, "AAPL", g2)
    assert h2["days"] == 0 and h2["cells"] == {} and "converted" in h2["note"]
    assert g is None


def test_the_payload_carries_the_earnings_dates_of_the_window_and_the_next(sdb, hist):
    """v4.143: the earnings dates the collector kept (scr_earnings) inside the history window,
    and the next one after the surface day (scr_earnings first, else the underlying's next date)."""
    from app.services import scr_store

    days = sessions_before(ON, N_DAYS)
    scr_store.file_earnings_day(sdb, days[5], [{"symbol": "AAPL", "when": "amc"}, {"symbol": "MSFT", "when": "bmo"}])
    scr_store.file_earnings_day(sdb, days[20], [{"symbol": "AAPL", "when": "amc"}])
    scr_store.file_earnings_day(sdb, "2025-01-30", [{"symbol": "AAPL", "when": "amc"}])      # before the window
    grid = vol_surface.surface(sdb, "AAPL", "latest", side="otm", band=20)
    h = vol_history.grid_history(sdb, "AAPL", grid)
    assert [e["on"] for e in h["earnings"]] == [days[5].isoformat(), days[20].isoformat()]
    assert h["earnings"][0]["when"] == "amc" and h["next_earnings"] is None
    weekdays = sum(1 for i in range((ON - days[0]).days) if (days[0] + _dt.timedelta(days=i)).weekday() < 5)
    assert h["earnings_coverage"] == {"fetched": 2, "weekdays": weekdays}   # only the two filed days were read
    # the next one: the underlying's next date (re-read daily) unless a kept date comes first
    u = sdb.query(ScrUnderlying).filter(ScrUnderlying.symbol == "AAPL").one()
    u.earnings_date = "2026-10-29"
    sdb.commit()
    assert vol_history.grid_history(sdb, "AAPL", grid)["next_earnings"] == {"on": "2026-10-29", "when": None}
    scr_store.file_earnings_day(sdb, "2026-10-22", [{"symbol": "AAPL", "when": "amc"}])
    assert vol_history.grid_history(sdb, "AAPL", grid)["next_earnings"] == {"on": "2026-10-22", "when": "amc"}
    # earnings ON the surface day: not in the past, and "next" says so (the review's finding)
    scr_store.file_earnings_day(sdb, ON, [{"symbol": "AAPL", "when": "bmo"}])
    h3 = vol_history.grid_history(sdb, "AAPL", grid)
    assert h3["next_earnings"] == {"on": ON.isoformat(), "when": "bmo"}
    assert ON.isoformat() not in [e["on"] for e in h3["earnings"]]
    # a past day's surface: dates before that day, the next one on or after it
    g2 = vol_surface.surface(sdb, "AAPL", days[10].isoformat(), side="call", band=20)
    h2 = vol_history.grid_history(sdb, "AAPL", g2)
    assert [e["on"] for e in h2["earnings"]] == [days[5].isoformat()]
    assert h2["next_earnings"] == {"on": days[20].isoformat(), "when": "amc"}
