"""The volatility surface and the contract view (v4.139): ``app/services/vol_surface.py`` (the
strike x expiry grid, from the last market pass or from a past day of the contract history)
and ``app/services/contract_view.py`` (the contract pickers and one contract's life with its
IV). The past-day numbers are a ROUND TRIP: closes priced by Black-Scholes from a known
volatility must come back as that volatility. A temp screener DB (the models' tables) and a
temp Parquet folder; no network."""
from __future__ import annotations

import datetime as _dt
import gzip
import json
import math

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.screener_models import ScrBase, ScrContract, ScrUnderlying, ScrUnderlyingDaily
from app.services import contract_view, flat_files, opt_history, vol_surface
from app.services.black_scholes import black_scholes
from app.services.opt_constants import RISK_FREE
from app.services.vol_surface import SurfaceError

pytest.importorskip("pyarrow")

ON = _dt.date(2026, 10, 7)                       # Wednesday
NOV = _dt.date(2026, 11, 20)                     # monthly, 44 days
NOV_WK = _dt.date(2026, 11, 13)                  # weekly, 37 days
DEC = _dt.date(2026, 12, 18)                     # monthly, 72 days
FAR = _dt.date(2027, 6, 17)                      # monthly (Juneteenth Friday -> Thursday), 253 days
SPOT = 100.0


def row(exp, right, k, iv):
    return {"expiry": exp, "right": right, "strike": float(k), "iv": iv}


def grid_rows():
    """Calls IV 0.30 + k/1000, puts 0.40 + k/1000 (so a cell says which side filled it)."""
    out = []
    for exp in (NOV, NOV_WK, DEC, FAR, ON):          # ON = expires today: left out
        for k in range(70, 135, 5):
            out.append(row(exp, "C", k, 0.30 + k / 1000.0))
            out.append(row(exp, "P", k, 0.40 + k / 1000.0))
    out.append(row(NOV, "C", 200, 0.5))              # outside every band but "every strike"
    out.append(row(DEC, "C", 125, 9.0))              # an absurd IV: left out
    return out


def test_otm_cells_take_the_put_below_the_price_and_the_call_above():
    g = vol_surface.build(grid_rows(), SPOT, ON)
    assert [e["date"] for e in g["expiries"]] == [NOV_WK.isoformat(), NOV.isoformat(), DEC.isoformat(), FAR.isoformat()]
    assert [e["monthly"] for e in g["expiries"]] == [False, True, True, True]
    assert g["expiries"][1]["dte"] == 44 and g["expiries"][1]["years"] == round(44 / 365, 4)
    assert g["strikes"] == [float(k) for k in range(80, 125, 5)]        # within 20 %
    by = dict(zip(g["strikes"], g["cells"]))
    assert by[95.0][1] == pytest.approx((0.40 + 0.095) * 100)          # below the price: the put
    assert by[100.0][1] == pytest.approx((0.30 + 0.100) * 100)         # at the price: the call
    assert by[105.0][1] == pytest.approx((0.30 + 0.105) * 100)
    assert g["spot_row"] == 4 and g["spot"] == 100.0                   # 80 85 90 95 below
    assert g["atm_strikes"][1] == 100.0
    assert g["atm"][1] == pytest.approx(((0.30 + 0.1) + (0.40 + 0.1)) / 2 * 100)
    assert g["scale"][0] <= g["scale"][1] and g["n_cells"] == 9 * 4
    assert g["side"] == "otm" and g["band"] == 20 and g["max_dte"] == 365


def test_the_options_choose_the_cells():
    rows = grid_rows()
    calls = vol_surface.build(rows, SPOT, ON, side="call")
    assert dict(zip(calls["strikes"], calls["cells"]))[90.0][1] == pytest.approx(39.0)
    puts = vol_surface.build(rows, SPOT, ON, side="put")
    assert dict(zip(puts["strikes"], puts["cells"]))[110.0][1] == pytest.approx(51.0)
    mid = vol_surface.build(rows, SPOT, ON, side="mid")
    assert dict(zip(mid["strikes"], mid["cells"]))[110.0][1] == pytest.approx(46.0)
    narrow = vol_surface.build(rows, SPOT, ON, band=10)
    assert narrow["strikes"] == [90.0, 95.0, 100.0, 105.0, 110.0]
    every = vol_surface.build(rows, SPOT, ON, band=0, common=False)
    assert every["strikes"][0] == 70.0 and every["strikes"][-1] == 200.0
    common = vol_surface.build(rows, SPOT, ON, band=0)              # 200 is in 1 expiry of 4: hidden
    assert common["strikes"][-1] == 130.0 and common["hidden_strikes"] == 1 and common["common"] is True
    short = vol_surface.build(rows, SPOT, ON, max_dte=60)
    assert [e["date"] for e in short["expiries"]] == [NOV_WK.isoformat(), NOV.isoformat()]
    monthly = vol_surface.build(rows, SPOT, ON, expiries="monthly")
    assert NOV_WK.isoformat() not in [e["date"] for e in monthly["expiries"]]
    junk = vol_surface.build(rows, SPOT, ON, side="nope", band="x", max_dte=17, expiries="?")
    assert (junk["side"], junk["band"], junk["max_dte"], junk["exp_mode"]) == ("otm", 20, 365, "all")
    assert monthly["exp_mode"] == "monthly"


def test_common_strikes_only_hides_the_rarely_listed_strikes_and_unticked_shows_all():
    """v4.140: the $2.50 strikes only the near weeklies list are rows that are empty for every other
    expiry - hidden by default (fewer than half of the expiries have a cell there)."""
    rows = grid_rows()
    for k in (97.5, 102.5):                                   # listed for the near weekly only
        rows.append(row(NOV_WK, "P", k, 0.41))
        rows.append(row(NOV_WK, "C", k, 0.31))
    g = vol_surface.build(rows, SPOT, ON)
    assert 97.5 not in g["strikes"] and 102.5 not in g["strikes"] and g["hidden_strikes"] == 2
    assert g["strikes"] == [float(k) for k in range(80, 125, 5)]
    assert g["spot_row"] == 4 and len(g["cells"]) == len(g["strikes"])
    every = vol_surface.build(rows, SPOT, ON, common=False)
    assert 97.5 in every["strikes"] and 102.5 in every["strikes"] and every["hidden_strikes"] == 0
    assert every["spot_row"] == 5 and every["common"] is False
    row_975 = dict(zip(every["strikes"], every["cells"]))[97.5]
    assert row_975[0] == pytest.approx(41.0) and row_975[1:] == [None, None, None]
    # a strike in exactly half of the expiries stays (2 of 4)
    half = grid_rows() + [row(NOV_WK, "C", 101.0, 0.3), row(NOV, "C", 101.0, 0.3)]
    assert 101.0 in vol_surface.build(half, SPOT, ON)["strikes"]
    # a grid where no strike is common keeps every strike rather than going empty
    lone = [row(NOV, "C", 100.0, 0.3), row(DEC, "C", 105.0, 0.3), row(FAR, "C", 110.0, 0.3)]
    assert vol_surface.build(lone, SPOT, ON)["strikes"] == [100.0, 105.0, 110.0]


def test_an_empty_cell_stays_empty_and_no_price_is_an_error():
    rows = [r for r in grid_rows() if not (r["expiry"] == DEC and r["strike"] == 110.0)]
    g = vol_surface.build(rows, SPOT, ON)
    assert dict(zip(g["strikes"], g["cells"]))[110.0][2] is None       # nothing filled in
    with pytest.raises(SurfaceError):
        vol_surface.build(rows, None, ON)
    assert vol_surface.build([], SPOT, ON)["n_cells"] == 0


def fig_rows():
    """grid_rows() with the tile figures: delta = +k/1000 on calls, -k/1000 on puts (so the
    figures say which contract a cell's IV came from), the rest derived from the strike."""
    out = []
    for r in grid_rows():
        k, sign = r["strike"], (1.0 if r["right"] == "C" else -1.0)
        out.append({**r, "delta": sign * k / 1000.0, "gamma": 0.01, "theta": -0.05, "vega": 0.15,
                    "last": k / 100.0, "price": k / 100.0 + 0.01, "chg_pct": 1.5, "oi": int(k) * 10, "volume": int(k)})
    return out


def test_extra_carries_the_tile_figures_of_the_cells_own_contract():
    """v4.166: ``extra`` has the shape of ``cells``, null where the cell is null, and the figures
    of the contract the cell's IV came from (out of the money: the put below the price, the call
    at / above it)."""
    rows = [r for r in fig_rows() if not (r["expiry"] == DEC and r["strike"] == 110.0)]
    g = vol_surface.build(rows, SPOT, ON)
    assert len(g["extra"]) == len(g["cells"])
    for crow, xrow in zip(g["cells"], g["extra"]):
        assert len(xrow) == len(crow)
        for v, x in zip(crow, xrow):
            assert (v is None) == (x is None)
    by = dict(zip(g["strikes"], g["extra"]))
    assert by[95.0][1]["d"] == pytest.approx(-0.095)                  # below the price: the put's figures
    assert by[105.0][1]["d"] == pytest.approx(0.105)                  # above: the call's
    assert by[110.0][2] is None                                       # the empty cell
    x = by[105.0][1]
    assert x == {"d": pytest.approx(0.105), "g": 0.01, "t": -0.05, "v": 0.15, "last": 1.05, "price": pytest.approx(1.06),
                 "chg": 1.5, "oi": 1050, "vol": 105, "liq": None}     # liq: build() alone does not rank (latest_rows does)
    assert "mixed" not in x
    calls = vol_surface.build(rows, SPOT, ON, side="call")
    assert dict(zip(calls["strikes"], calls["extra"]))[90.0][1]["d"] == pytest.approx(0.09)
    puts = vol_surface.build(rows, SPOT, ON, side="put")
    assert dict(zip(puts["strikes"], puts["extra"]))[110.0][1]["d"] == pytest.approx(-0.11)
    mid = vol_surface.build(rows, SPOT, ON, side="mid")               # the call's figures, marked mixed
    xm = dict(zip(mid["strikes"], mid["extra"]))[90.0][1]
    assert xm["d"] == pytest.approx(0.09) and xm["mixed"] is True
    # a missing figure is None, never dropped; open interest / volume are whole numbers
    one = [{**rows[0], "delta": None, "oi": 12.0, "volume": "7"}]
    x1 = vol_surface.build(one, SPOT, ON, side="call", band=0, common=False)["extra"][0][0]
    assert x1["d"] is None and x1["oi"] == 12 and isinstance(x1["oi"], int) and x1["vol"] == 7


def test_rows_without_figures_give_null_extra_cells():
    """A past day's rows carry only the IV: every ``extra`` cell is null, the grid unchanged."""
    g = vol_surface.build(grid_rows(), SPOT, ON)
    assert g["n_cells"] == 36 and all(x is None for row_ in g["extra"] for x in row_)
    assert len(g["extra"]) == len(g["cells"]) and len(g["extra"][0]) == len(g["cells"][0])


# ───────────────────────────────── the reads: a screener DB + the Parquet files ─────────────────────────────────

def bs(S, k, exp, right, sig, day=ON):
    return black_scholes(S, k, (exp - day).days / 365.0, RISK_FREE, sig, "call" if right == "C" else "put").price


@pytest.fixture
def sdb(tmp_path):
    eng = sa.create_engine("sqlite:///" + (tmp_path / "screener.db").as_posix(), future=True)
    ScrBase.metadata.create_all(eng)
    s = sessionmaker(bind=eng, future=True)()
    s.add_all([ScrUnderlying(symbol="AAPL", name="Apple Inc.", sec_type="stock", spot=101.0, spot_src="massive",
                             history_done=True, history_tries=0),
               ScrUnderlying(symbol="SPX", name="S&P 500", sec_type="index", spot=5000.0, history_done=False,
                             history_tries=0)])
    asof = _dt.datetime(2026, 10, 7, 19, 30)
    for exp in (NOV, DEC):
        for k in (90.0, 100.0, 110.0):
            for rt in "CP":
                full = k != 110.0                     # the 110 contracts carry no greeks / prices (all None)
                s.add(ScrContract(symbol="AAPL", expiry=exp.isoformat(), right=rt, strike=k, weekly=False,
                                  iv=0.25 + (0.05 if rt == "P" else 0.0), session="2026-10-07", as_of=asof,
                                  delta=((0.4 if rt == "C" else -0.6) if full else None),
                                  gamma=(0.02 if full else None), theta=(-0.04 if full else None),
                                  vega=(0.11 if full else None), last=(k / 50.0 if full else None),
                                  price=(k / 50.0 + 0.05 if full else None), chg_pct=(-2.0 if full else None),
                                  oi=(1200 if full else None), volume=(34 if full else None)))
    for day, c in ((_dt.date(2026, 10, 5), 98.0), (_dt.date(2026, 10, 6), 99.0), (ON, SPOT)):
        s.add(ScrUnderlyingDaily(symbol="AAPL", on=day.isoformat(), close=c, close_raw=c))
    s.commit()
    yield s
    s.close()
    eng.dispose()


@pytest.fixture
def hist(tmp_path):
    """Three sessions of AAPL contracts priced at 30 % vol (and an index root), as Parquet."""
    flat = tmp_path / "flat"
    man = {}
    spots = {_dt.date(2026, 10, 5): 98.0, _dt.date(2026, 10, 6): 99.0, ON: SPOT}
    for day, S in spots.items():
        lines = ["ticker,volume,open,close,high,low,window_start,transactions"]
        for exp in (NOV, DEC):
            for k in (90.0, 95.0, 100.0, 105.0, 110.0):
                for rt in "CP":
                    p = round(bs(S, k, exp, rt, 0.30, day), 4)
                    lines.append("O:AAPL%s%s%08d,10,%s,%s,%s,%s,1,2" % (exp.strftime("%y%m%d"), rt, int(k * 1000),
                                                                       p, p, p, p))
        lines.append("O:SPX%sP%08d,10,5,5,5,5,1,2" % (NOV.strftime("%y%m%d"), 4_000_000))
        key = flat_files.key_for(day)
        path = flat.joinpath(*key.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
            f.write("\n".join(lines) + "\n")
        man[key] = {"day": day.isoformat(), "size": path.stat().st_size, "etag": "x", "downloaded_at": None}
    (flat / "manifest.json").write_text(json.dumps(man), encoding="utf-8")
    st = opt_history.Store(root=tmp_path / "pq", flat_root=flat)
    assert st.run()["converted"] == 3
    return tmp_path / "pq"


def test_latest_rows_carry_the_tile_figures(sdb):
    """v4.166: each row of the last pass carries delta / gamma / theta / vega, last, model price,
    day change, open interest and volume (None when the pass stored none)."""
    rows, meta = vol_surface.latest_rows(sdb, "AAPL")
    assert meta["source"] == "latest" and len(rows) == 12
    keys = {"expiry", "right", "strike", "iv", "delta", "gamma", "theta", "vega", "last", "price", "chg_pct", "oi", "volume",
            "liq"}
    assert all(set(r) == keys for r in rows)
    put90 = next(r for r in rows if r["strike"] == 90.0 and r["right"] == "P" and r["expiry"] == NOV.isoformat())
    assert put90["delta"] == -0.6 and put90["gamma"] == 0.02 and put90["theta"] == -0.04 and put90["vega"] == 0.11
    assert put90["last"] == 1.8 and put90["price"] == pytest.approx(1.85) and put90["chg_pct"] == -2.0
    assert put90["oi"] == 1200 and put90["volume"] == 34
    bare = next(r for r in rows if r["strike"] == 110.0)
    assert all(bare[k] is None for k in ("delta", "gamma", "theta", "vega", "last", "price", "chg_pct", "oi", "volume", "liq"))
    assert put90["liq"] == 0.5                     # v4.181: the eight contracts with figures tie


def test_liquidity_ranks_place_each_contract_among_its_chain():
    """v4.181: depth = log(1 + OI) + 0.5 log(1 + volume), ranked 0..1 within the chain (ties share
    the mid rank); a contract with neither figure gets None; a chain of one is 1.0."""
    rows = [{"oi": 10000, "volume": 500}, {"oi": 10, "volume": 0}, {"oi": 300, "volume": 20},
            {"oi": None, "volume": None}, {"oi": 300, "volume": 20}]
    vol_surface.liquidity_ranks(rows)
    assert rows[0]["liq"] == 1.0 and rows[1]["liq"] == 0.0 and rows[3]["liq"] is None
    assert rows[2]["liq"] == rows[4]["liq"] == 0.5                        # a tie of the two middle ranks (1 and 2 of 0..3)
    one = [{"oi": 5, "volume": None}]
    vol_surface.liquidity_ranks(one)
    assert one[0]["liq"] == 1.0
    vol_surface.liquidity_ranks([])                                       # nothing to rank, no error


def test_the_latest_surface_is_massives_iv_from_the_last_pass(sdb):
    g = vol_surface.surface(sdb, "aapl")
    assert g["source"] == "latest" and g["symbol"] == "AAPL" and g["name"] == "Apple Inc."
    assert g["on"] == "2026-10-07" and g["spot"] == 101.0 and g["as_of"] == "2026-10-07T19:30:00Z"
    by = dict(zip(g["strikes"], g["cells"]))
    assert by[90.0] == [30.0, 30.0] and by[110.0] == [25.0, 25.0]      # puts below 101, calls above
    assert "Massive" in g["method"]
    # v4.166: the tile figures of each cell's own contract; the latest pass has greeks
    assert g["has_greeks"] is True
    xb = dict(zip(g["strikes"], g["extra"]))
    assert xb[90.0][0]["d"] == -0.6 and xb[90.0][0]["oi"] == 1200 and xb[90.0][0]["last"] == 1.8   # the put
    assert xb[100.0][0]["d"] == -0.6                                       # 100 < the 101 spot: the put too
    assert xb[110.0][0] == {"d": None, "g": None, "t": None, "v": None, "last": None, "price": None,
                            "chg": None, "oi": None, "vol": None, "liq": None}
    # v4.181: liquidity rank among the chain - the eight full contracts tie (same OI and volume) at the mid rank
    assert xb[90.0][0]["liq"] == 0.5 and xb[100.0][0]["liq"] == 0.5
    with pytest.raises(SurfaceError, match="ZZZZ"):
        vol_surface.surface(sdb, "ZZZZ")
    with pytest.raises(SurfaceError, match="symbol"):
        vol_surface.surface(sdb, "")
    # v4.176 (the user typed MFST for MSFT): an unknown symbol names the closest listed tickers
    assert vol_surface.unknown_symbol_text(sdb, "ZZZZ") == "ZZZZ is not in the screener's list of optionable stocks"
    assert vol_surface.unknown_symbol_text(sdb, "apal") == \
        "APAL is not in the screener's list of optionable stocks - did you mean AAPL?"
    with pytest.raises(SurfaceError, match="did you mean AAPL"):
        vol_surface.surface(sdb, "AAPLE")


def test_a_past_days_surface_solves_the_closes_back_to_their_volatility(sdb, hist):
    g = vol_surface.surface(sdb, "AAPL", ON.isoformat(), root=hist, side="call")
    assert g["source"] == "history" and g["spot"] == SPOT and g["traded"] == 20
    for row_ in g["cells"]:
        for v in row_:
            assert v == pytest.approx(30.0, abs=0.05)
    assert "Black-Scholes" in g["method"]
    # v4.166: a past day has no greeks or prices - every tile figure cell is null
    assert g["has_greeks"] is False
    assert len(g["extra"]) == len(g["cells"]) and all(x is None for row_ in g["extra"] for x in row_)
    with pytest.raises(SurfaceError, match="index"):
        vol_surface.surface(sdb, "SPX", ON.isoformat(), root=hist)
    with pytest.raises(SurfaceError, match="not in the contract history"):
        vol_surface.surface(sdb, "AAPL", "2026-10-02", root=hist)
    sdb.query(ScrUnderlyingDaily).filter(ScrUnderlyingDaily.on == "2026-10-06").delete()
    sdb.commit()
    with pytest.raises(SurfaceError, match="stock close"):
        vol_surface.surface(sdb, "AAPL", "2026-10-06", root=hist)


def test_the_contract_pickers_and_one_contracts_life(sdb, hist):
    c = contract_view.day_contracts(sdb, "AAPL", ON.isoformat(), root=hist)
    assert c["root"] == "AAPL" and c["spot"] == SPOT
    assert [(e["expiry"], e["dte"]) for e in c["expiries"]] == [(NOV.isoformat(), 44), (DEC.isoformat(), 72)]
    assert c["expiries"][0]["calls"] == [90.0, 95.0, 100.0, 105.0, 110.0]
    life = contract_view.contract_series(sdb, "AAPL", NOV.isoformat(), "C", "100", root=hist)
    assert life["ticker"] == "O:AAPL261120C00100000"
    assert [r["on"] for r in life["rows"]] == ["2026-10-05", "2026-10-06", "2026-10-07"]
    assert [r["spot"] for r in life["rows"]] == [98.0, 99.0, 100.0]
    assert all(r["iv"] == pytest.approx(30.0, abs=0.05) for r in life["rows"])
    assert life["rows"][-1]["dte"] == 44 and life["rows"][-1]["close"] == pytest.approx(bs(SPOT, 100.0, NOV, "C", 0.30), abs=1e-4)
    with pytest.raises(SurfaceError):
        contract_view.contract_series(sdb, "AAPL", NOV.isoformat(), "X", "100", root=hist)
    with pytest.raises(SurfaceError, match="index"):
        contract_view.day_contracts(sdb, "SPX", ON.isoformat(), root=hist)
    assert contract_view.history_days(hist) == ["2026-10-07", "2026-10-06", "2026-10-05"]
    assert not math.isnan(life["rows"][0]["iv"])


def test_the_hermes_check_script_solves_independently_and_reports(sdb, hist, tmp_path, monkeypatch, capsys):
    """deploy/check_vol_surface.py: its own Black-Scholes (statistics.NormalDist) agrees with the
    app's solver, and a run over the fixtures reports every check (display and past day PASS)."""
    import importlib.util
    from pathlib import Path

    from app import screener_db
    from app.services.black_scholes import implied_vol

    path = Path(__file__).resolve().parent.parent / "deploy" / "check_vol_surface.py"
    spec = importlib.util.spec_from_file_location("check_vol_surface", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    T = (NOV - ON).days / 365.0
    for k, rt in ((90.0, "P"), (100.0, "C"), (110.0, "C"), (100.0, "P")):
        p = bs(SPOT, k, NOV, rt, 0.30)
        mine = mod.solve_iv(p, SPOT, k, T, RISK_FREE, rt == "C")
        assert mine == pytest.approx(implied_vol(p, SPOT, k, T, "call" if rt == "C" else "put"), abs=1e-9)
        assert mine == pytest.approx(0.30, abs=1e-6)
    assert mod.solve_iv(0, SPOT, 100.0, T, RISK_FREE, True) is None
    screener_db.configure("sqlite:///" + (tmp_path / "screener.db").as_posix())
    monkeypatch.setenv("TST_OPTION_HISTORY_DIR", str(hist))
    try:
        rc = mod.main(["AAPL"])
    finally:
        screener_db.configure(None)
    out = capsys.readouterr().out
    assert rc == 0, out
    for needle in ("1. DISPLAY", "otm  PASS", "ATM  PASS", "2. MASSIVE/PRICE", "3. CALL/PUT", "4. SAME SESSION",
                   "5. PAST DAY", "nothing was written"):
        assert needle in out, needle
    assert "differ by more than 0.01" in out and "FAIL" not in out


def test_the_check_compares_massive_with_an_ibkr_snapshot(sdb, tmp_path, monkeypatch, capsys):
    """check 6 (--ibkr): Massive's stored IV against IBKR's model IV, matched by expiry / right /
    strike; tight IBKR quotes near the money are the 'liquid' set."""
    import importlib.util
    import json as _json
    from pathlib import Path

    from app import screener_db

    path = Path(__file__).resolve().parent.parent / "deploy" / "check_vol_surface.py"
    spec = importlib.util.spec_from_file_location("check_vol_surface", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rows = []
    for exp in (NOV, DEC):
        for k in (90.0, 100.0, 110.0):
            for rt in "CP":
                iv = 0.25 + (0.05 if rt == "P" else 0.0) + (0.004 if k == 100.0 else 0.0)   # Massive +- 0.4 pt
                p = bs(101.0, k, exp, rt, iv)
                rows.append({"expiry": exp.isoformat(), "right": rt, "strike": k, "bid": round(p * 0.99, 2),
                             "ask": round(p * 1.01, 2), "last": None, "close": None, "volume": 10,
                             "iv_model": iv, "und_price": 101.0, "iv_bid": None, "iv_ask": None})
    snap = {"taken_utc": "2026-10-07T19:40:00+00:00", "taken_et": "2026-10-07 15:40", "source": "test",
            "symbols": {"AAPL": {"spot": 101.0, "spot_src": "last", "contracts": rows}}}
    f = tmp_path / "iv_check" / "ibkr_2026-10-07_1540ET.json"
    f.parent.mkdir()
    f.write_text(_json.dumps(snap), encoding="utf-8")
    screener_db.configure("sqlite:///" + (tmp_path / "screener.db").as_posix())
    monkeypatch.setenv("TST_OPTION_HISTORY_DIR", str(tmp_path / "option_history"))
    try:
        rc = mod.main(["AAPL", "--ibkr"])
    finally:
        screener_db.configure(None)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "IBKR snapshot: " in out and "6. IBKR" in out and "time gap" in out
    six = out[out.index("6. IBKR"):]
    assert "AGREES - 100% of the liquid contracts within 2 pts of IBKR" in six, six
    assert "price: Massive 101.00" in six and "IBKR 101.00" in six
    assert mod.find_snapshot(str(f), None) == f
