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
    every = vol_surface.build(rows, SPOT, ON, band=0)
    assert every["strikes"][0] == 70.0 and every["strikes"][-1] == 200.0
    short = vol_surface.build(rows, SPOT, ON, max_dte=60)
    assert [e["date"] for e in short["expiries"]] == [NOV_WK.isoformat(), NOV.isoformat()]
    monthly = vol_surface.build(rows, SPOT, ON, expiries="monthly")
    assert NOV_WK.isoformat() not in [e["date"] for e in monthly["expiries"]]
    junk = vol_surface.build(rows, SPOT, ON, side="nope", band="x", max_dte=17, expiries="?")
    assert (junk["side"], junk["band"], junk["max_dte"], junk["exp_mode"]) == ("otm", 20, 365, "all")
    assert monthly["exp_mode"] == "monthly"


def test_an_empty_cell_stays_empty_and_no_price_is_an_error():
    rows = [r for r in grid_rows() if not (r["expiry"] == DEC and r["strike"] == 110.0)]
    g = vol_surface.build(rows, SPOT, ON)
    assert dict(zip(g["strikes"], g["cells"]))[110.0][2] is None       # nothing filled in
    with pytest.raises(SurfaceError):
        vol_surface.build(rows, None, ON)
    assert vol_surface.build([], SPOT, ON)["n_cells"] == 0


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
                s.add(ScrContract(symbol="AAPL", expiry=exp.isoformat(), right=rt, strike=k, weekly=False,
                                  iv=0.25 + (0.05 if rt == "P" else 0.0), session="2026-10-07", as_of=asof))
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


def test_the_latest_surface_is_massives_iv_from_the_last_pass(sdb):
    g = vol_surface.surface(sdb, "aapl")
    assert g["source"] == "latest" and g["symbol"] == "AAPL" and g["name"] == "Apple Inc."
    assert g["on"] == "2026-10-07" and g["spot"] == 101.0 and g["as_of"] == "2026-10-07T19:30:00Z"
    by = dict(zip(g["strikes"], g["cells"]))
    assert by[90.0] == [30.0, 30.0] and by[110.0] == [25.0, 25.0]      # puts below 101, calls above
    assert "Massive" in g["method"]
    with pytest.raises(SurfaceError, match="ZZZZ"):
        vol_surface.surface(sdb, "ZZZZ")
    with pytest.raises(SurfaceError, match="symbol"):
        vol_surface.surface(sdb, "")


def test_a_past_days_surface_solves_the_closes_back_to_their_volatility(sdb, hist):
    g = vol_surface.surface(sdb, "AAPL", ON.isoformat(), root=hist, side="call")
    assert g["source"] == "history" and g["spot"] == SPOT and g["traded"] == 20
    for row_ in g["cells"]:
        for v in row_:
            assert v == pytest.approx(30.0, abs=0.05)
    assert "Black-Scholes" in g["method"]
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
