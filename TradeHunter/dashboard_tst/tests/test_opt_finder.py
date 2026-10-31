"""The Option Finder (v4.219, ``services/opt_finder.py`` + ``/options/api/finder`` + the tab on the Volatility
Surface page): the user's Excel calculator run over every call of the surface, valued at each call's own expiry or
on one date, ranked. The figures come from ``option_calc.evaluate`` - the engine that IS the workbook."""
from __future__ import annotations

import datetime as _dt
import math

import pytest

from app.services import opt_finder
from app.services.calc_chain import ChainError
from app.services.option_calc import Leg, Scenario, evaluate
from tests.test_calc_sheet_api import MONTHLIES, TODAY, client, sdb  # noqa: F401 - the ACME chain (spot 100, IV 30 %)


def test_each_call_valued_at_its_own_expiry_is_the_sheet(sdb):
    j = opt_finder.find(sdb, "acme", target=120, worst=92, mode="expiry", today=TODAY)
    assert j["symbol"] == "ACME" and j["spot"] == 100.0 and j["mode"] == "expiry" and j["n"] == len(j["rows"]) > 0
    r = next(x for x in j["rows"] if x["strike"] == 100.0 and x["expiry"] == MONTHLIES[1].isoformat())
    want = evaluate([Leg("c", 100.0, r["dte"], 0.30, 1)], Scenario(100.0, 120, 92, r["dte"], r=j["rate"]), with_curves=False)
    assert r["decay"] == r["dte"] and r["valued_on"] == r["expiry"]
    assert r["cost"] == pytest.approx(want.entry_value, abs=1e-4) and r["at_target"] == pytest.approx(20.0, abs=1e-3)   # intrinsic at expiry
    assert r["at_worst"] == pytest.approx(0.0, abs=1e-4) and r["breakeven"] == pytest.approx(100 + want.entry_value, abs=0.01)
    assert r["rr"] == pytest.approx(want.reward_over_risk, rel=1e-4) and r["roi"] == pytest.approx(want.roi, rel=1e-4)
    assert 0 < r["prob"] < 1 and r["be_pct"] == pytest.approx((r["breakeven"] - 100) / 100, abs=1e-5)
    # only calls, within 15 % of the price, a week or more out, open interest 100+, worth 5 cents+
    assert all(85 <= x["strike"] <= 115 and x["dte"] >= opt_finder.MIN_DTE and x["oi"] >= 100 and x["cost"] >= 0.05 for x in j["rows"])
    # ranked by reward over risk, best first
    rr = [x["rr"] for x in j["rows"] if x["rr"] is not None]
    assert rr == sorted(rr, reverse=True)


def test_every_call_valued_on_one_date(sdb):
    on = (TODAY + _dt.timedelta(days=40)).isoformat()
    j = opt_finder.find(sdb, "ACME", target=120, worst=92, mode="date", on=on, today=TODAY)
    assert j["mode"] == "date" and j["on"] == on and j["rows"]
    assert all(x["decay"] == 40 and x["valued_on"] == on and x["dte"] >= 40 for x in j["rows"])     # earlier expiries left out
    r = j["rows"][0]
    want = evaluate([Leg("c", r["strike"], r["dte"], r["iv"], 1)], Scenario(100.0, 120, 92, 40, r=j["rate"]), with_curves=False)
    assert r["at_target"] == pytest.approx(want.target_value, abs=1e-4) and r["at_worst"] >= 0
    # time value left on the valuation date: the at-the-money call of the farthest expiry (the top-ranked one can be
    # a far strike whose next expiry is only days after the date - worth ~0 at the worst case, by the calendar)
    far = max((x for x in j["rows"] if x["strike"] == 100.0), key=lambda x: x["dte"])
    assert far["dte"] > 40 and far["at_worst"] > 0


def test_inputs_are_refused_in_words(sdb):
    with pytest.raises(opt_finder.FinderError, match="target above today's price"):
        opt_finder.find(sdb, "ACME", target=95, worst=90, today=TODAY)
    with pytest.raises(opt_finder.FinderError, match="below the target"):
        opt_finder.find(sdb, "ACME", target=120, worst=125, today=TODAY)
    with pytest.raises(opt_finder.FinderError, match="coming soon"):
        opt_finder.find(sdb, "ACME", target=120, worst=92, strategy="bull_call_spread", today=TODAY)
    with pytest.raises(opt_finder.FinderError, match="after today"):
        opt_finder.find(sdb, "ACME", target=120, worst=92, mode="date", on=TODAY.isoformat(), today=TODAY)
    with pytest.raises(opt_finder.FinderError, match="number"):
        opt_finder.find(sdb, "ACME", target="x", worst=92, today=TODAY)
    with pytest.raises(ChainError):
        opt_finder.find(sdb, "ZZZZ", target=120, worst=92, today=TODAY)


def test_prob_above_is_n_of_d2():
    p = opt_finder.prob_above(100, 110, 0.30, 30, r=0.04)
    T = 30 / 365
    d2 = (math.log(100 / 110) + (0.04 - 0.045) * T) / (0.30 * math.sqrt(T))
    assert p == pytest.approx(0.5 * (1 + math.erf(d2 / math.sqrt(2))))
    assert opt_finder.prob_above(100, 100, 0.30, 0) is None


def test_the_api_and_the_tab(client, sdb, monkeypatch):
    from app.services import clock
    monkeypatch.setattr(clock, "et_today", lambda: TODAY.isoformat())
    j = client.get("/options/api/finder", params={"symbol": "acme", "target": "120", "worst": "92"}).json()
    assert j["ok"] is True and j["rows"] and j["mode"] == "expiry" and j["strategy"] == "long_call"
    need = client.get("/options/api/finder", params={"symbol": "ACME"}).json()
    assert need["ok"] is False and need["need_prices"] is True and need["spot"] == 100.0
    bad = client.get("/options/api/finder", params={"symbol": "ACME", "target": "90", "worst": "80"}).json()
    assert bad["ok"] is False and "target above" in bad["error"]
    d = client.get("/options/api/finder", params={"symbol": "ACME", "target": "120", "worst": "92", "mode": "date",
                                                  "date": (TODAY + _dt.timedelta(days=40)).isoformat()}).json()
    assert d["ok"] and d["on"] == (TODAY + _dt.timedelta(days=40)).isoformat()


def test_rows_say_which_expiries_are_the_standard_monthlies(sdb):
    """v4.229 (user: "the strike and expiry column need the expiry horizon like the vol surface"): each row carries
    ``monthly`` so the finder's headings show the surface's M beside the date, with the days under it."""
    j = opt_finder.find(sdb, "ACME", target=120, worst=92, mode="expiry", today=TODAY)
    from app.services import vol_surface
    assert j["rows"] and all(x["monthly"] == vol_surface.is_monthly(x["expiry"]) for x in j["rows"])
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "app" / "templates" / "options.html").read_text(encoding="utf-8")
    fm = html[html.index("function paintFindMap("):html.index("function rkKey(")]
    assert "meta[r.expiry] = {dte: r.dte, monthly: !!r.monthly};" in fm
    assert "(m.monthly ? '<span class=\"vol-m\">M</span>' : '')" in fm and "'<div class=\"vol-sub\">' + m.dte + 'd</div>'" in fm
    assert "Strike " + "\\" * 2 + " Expiry" in fm and ".of-map .vol-sub" in html
