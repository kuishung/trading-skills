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


def test_the_coloured_surface_sits_above_the_table_with_the_price_and_matp_lines():
    """v4.230 (user: "switch the table to be bottom and the colour up ... make the colour table width to fit ... the
    MATP and MBP and Current price to be marked like the vol surface")."""
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "app" / "templates" / "options.html").read_text(encoding="utf-8")
    assert html.index('id="ofMapBox"') < html.index('id="ofWrap"') < html.index('id="ofMore"')     # colour first
    assert "#volPanelFind .of-mapwrap { width: fit-content; max-width: 100%; }" in html
    fm = html[html.index("function paintFindMap("):html.index("function rkKey(")]
    for needle in ("var lj = {spot: j.spot, levels: j.levels, strikes: ks}", "levelState(lj)", "levelTags(lj, idx)",
                   "h += levelsBefore(lst, j.spot, nc) + spotRow(j, nc); spotDone = true;", "tagHtml(ltags[ri])",
                   "h += levelsRest(lst, nc);"):
        assert needle in fm, needle
    assert "&middot; price</span>" not in fm                       # the old marker gave way to the price line
    for css in (".of-map tr.vol-spot th", ".of-map tr.vol-lvl.vol-lvl-matp th", ".of-map tr.vol-lvl.vol-lvl-mbp th"):
        assert css in html, css


def test_the_finder_api_carries_the_tickers_matp_and_mbp(client, sdb, monkeypatch):
    from app.routes import options_page
    monkeypatch.setattr(options_page, "_levels_for", lambda db, sym: {"matp": 112.4, "mbp": 93.6} if sym.upper() == "ACME" else {})
    j = client.get("/options/api/finder", params={"symbol": "ACME", "target": "120", "worst": "92"}).json()
    assert j["ok"] is True and j["levels"] == {"matp": 112.4, "mbp": 93.6}


def test_a_tick_hides_the_thinly_listed_contracts_from_the_chart():
    """v4.231 (user: "a button ticker to tick to hide the thin contract so that the gaps will disappear in the colour
    chart"): on by default and remembered; the chart drops thin expiries then every strike with a gap (the surface's
    tidy()); the table keeps every call; the title says what was hidden and whether the #1 call is among it."""
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "app" / "templates" / "options.html").read_text(encoding="utf-8")
    assert '<input type="checkbox" id="ofClean" checked> Hide thinly listed contracts (no gaps in the chart)' in html
    assert html.index('id="ofMapTitle"') < html.index('id="ofClean"') < html.index('id="ofMap"')
    fm = html[html.index("function paintFindMap("):html.index("function rkKey(")]
    for needle in ("if ($('ofClean') && $('ofClean').checked)", "cnt.map(function (n) { return n * 2 < most; })",
                   "var tv = tidy(grid, exps.length,", "topHidden = !(exps.indexOf(rows[0].expiry) >= 0",
                   "Hidden as thinly listed: ", "the #1 call is among them (it is in the table)"):
        assert needle in fm, needle
    assert "localStorage.setItem('th.of.clean'" in html and "paintFindMap(ofSorted(OF.last.rows))" in html


def test_a_user_entry_price_reprices_every_call(sdb):
    """v4.234 (user: "another radio button will be user defined entry price, the table will be recalculated based on
    user price"): the calls are priced and every figure worked from the member's stock price; blank = the last price."""
    last = opt_finder.find(sdb, "ACME", target=120, worst=92, today=TODAY)
    assert (last["entry"], last["entry_src"]) == (100.0, "last")
    mine = opt_finder.find(sdb, "ACME", target=120, worst=92, today=TODAY, entry="96")
    assert (mine["entry"], mine["entry_src"], mine["spot"]) == (96.0, "user", 100.0)
    a = next(x for x in last["rows"] if x["strike"] == 100.0 and x["expiry"] == MONTHLIES[1].isoformat())
    b = next(x for x in mine["rows"] if x["strike"] == 100.0 and x["expiry"] == MONTHLIES[1].isoformat())
    want = evaluate([Leg("c", 100.0, b["dte"], b["iv"], 1)], Scenario(96.0, 120, 92, b["dte"], r=mine["rate"]), with_curves=False)
    assert b["cost"] == pytest.approx(want.entry_value, abs=1e-4) and b["cost"] < a["cost"]     # cheaper with the stock lower
    assert b["be_pct"] == pytest.approx((b["breakeven"] - 96.0) / 96.0, abs=1e-5)
    assert all(96 * 0.85 <= x["strike"] <= 96 * 1.15 for x in mine["rows"])                # the band follows the entry
    with pytest.raises(opt_finder.FinderError, match="above your entry price"):
        opt_finder.find(sdb, "ACME", target=95, worst=90, today=TODAY, entry="96")
    with pytest.raises(opt_finder.FinderError, match="within 50%"):
        opt_finder.find(sdb, "ACME", target=200, worst=90, today=TODAY, entry="160")
    with pytest.raises(opt_finder.FinderError, match="entry price must be a positive number"):
        opt_finder.find(sdb, "ACME", target=120, worst=90, today=TODAY, entry="-3")


def test_the_entry_price_radio_and_column():
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "app" / "templates" / "options.html").read_text(encoding="utf-8")
    for needle in ('<input type="checkbox" id="ofEntryLock" checked> fixed at the last price',     # v4.236: a tick, no radios
                   '<input id="ofEntryPx" type="hidden"><input id="ofTarget" type="hidden"><input id="ofWorst" type="hidden">', "q.set('entry', ep);", "'\">Entry price</th>'",
                   "priced from your entry price ", "if (j.entry_src === 'user') q.set('entry', String(j.entry));",
                   "if (p.entry) q.set('entry', p.entry);", "entry: q.get('entry') || ''"):
        assert needle in html, needle


def test_the_api_passes_the_entry_price(client, sdb):
    j = client.get("/options/api/finder", params={"symbol": "ACME", "target": "120", "worst": "92", "entry": "97.5"}).json()
    assert j["ok"] is True and j["entry"] == 97.5 and j["entry_src"] == "user"
    j = client.get("/options/api/finder", params={"symbol": "ACME", "target": "120", "worst": "92"}).json()
    assert j["entry_src"] == "last" and j["entry"] == j["spot"]


def test_the_price_bar_drags_the_entry_target_and_worst_case():
    """v4.235 (user: "combine these into a bar where the bar will indicate 10% to 30% plus minus range and user will be
    able to drag a line in the bar rather than typing the price"): the current price fixed, three handles; a handle
    writes its box and fires its input (so the finder re-ranks); dragging the entry switches to "my entry price"."""
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "app" / "templates" / "options.html").read_text(encoding="utf-8")
    for needle in ('<div id="ofBar" class="of-bar hidden"', 'data-ofrange="10"', 'data-ofrange="20"', 'data-ofrange="30"',
                   'data-ofh="worst"', 'data-ofh="entry"', 'data-ofh="target"', 'id="ofBarNow"',
                   "var OFB_BOX = {entry: 'ofEntryPx', target: 'ofTarget', worst: 'ofWorst'};",
                   "box.dispatchEvent(new Event('input'));",
                   "if (h.dataset.ofh === 'entry' && !ofEntryUser()) return;      // v4.236: fixed at the last price",
                   "$('ofBarTrack').addEventListener('pointerdown'", "$('ofBarTrack').addEventListener('keydown'",
                   "localStorage.setItem('th.of.range', String(OFB.range));", "ofbPaint();                                           // v4.235"):
        assert needle in html, needle
    assert html.index('id="ofBar"') < html.index('id="ofMsg"')


def test_the_entry_is_fixed_by_a_tick_and_the_top_panel_stays_put():
    """v4.236 (user: "we do not need the target price column, worst case column since we have the bar; we also do not
    need the radio button ... add the button to make the fix entry price as last price so the entry bar cannot be
    moved, untick it make it movable"; "the top selection panel in option finder make it fixed and cannot be
    scrolled")."""
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "app" / "templates" / "options.html").read_text(encoding="utf-8")
    assert 'name="ofEntry"' not in html and "Target price\n        <input" not in html
    assert "function ofEntryUser() { return !$('ofEntryLock').checked; }" in html
    assert "localStorage.setItem('th.of.lock'" in html
    assert html.index('<div id="ofHead" class="of-head">') < html.index('id="ofBar"')
    assert ".of-head { position: sticky; top: 0; z-index: 6; background: var(--tv-page);" in html
    # a ticker with no saved prices starts from target +10 % / worst -5 % on the bar, then ranks
    assert "if (j && j.need_prices && j.spot && ofbDefaults(j.spot)) { runFind(); return; }" in html
    assert "if ($('ofTarget').value || $('ofWorst').value) return false;" in html


def test_the_finder_settings_sit_in_the_left_panel_and_the_bar_spans_the_top():
    """v4.237 (user: "I want to make this panel height slimmer"; "i think this we put in left panel add a new one";
    "so the bar will occupy the whole top of the table"): the strategy / entry / time decay / ranking move to a new box
    in the left panel (under the ticker, on the Option Finder tab only); the fixed head holds only the slim, full-width
    bar."""
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "app" / "templates" / "options.html").read_text(encoding="utf-8")
    side = html.index('<section id="ofSide" class="hidden of-side"')
    assert html.index('id="volSym"') < side < html.index('id="volDay"')          # under the ticker, in the left panel
    assert side < html.index('<div class="of-inputs">') < html.index('id="ofEntryLock"') < html.index('id="volDay"')
    head = html[html.index('<div id="ofHead" class="of-head">'):html.index('id="ofMsg"')]
    assert 'class="of-inputs"' not in head and 'id="ofBar"' in head
    assert "show($('ofSide'), TAB === 'find');" in html
    assert ".of-bar { width: 100%;" in html and ".of-bar-track { position: relative; flex: 1 1 auto; height: 2.75rem;" in html
    assert "function ofbSpread(items)" in html                                   # v4.238: labels spread, never cut off
    assert "if (q !== 0 && (q % (step * 2) === 0" in html                       # the "Now" label takes the 0 % spot


def test_the_bar_marks_the_matp_and_mbp():
    """v4.238 (user: "i need the MATP and MBP to be shown in the bar also"): fixed dashed marks from the finder's
    ``levels`` for this ticker, coloured as on the Volatility Surface, pinned to an end (with an arrow) when outside the
    range; the labels on each row are spread so none overlaps."""
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "app" / "templates" / "options.html").read_text(encoding="utf-8")
    for needle in ('<div class="of-bar-lvl of-bar-mbp hidden" data-lvl="mbp"><span></span></div>',
                   '<div class="of-bar-lvl of-bar-matp hidden" data-lvl="matp"><span></span></div>',
                   "var lv = (OF.last && OF.last.symbol === OF.sym && OF.last.levels) || {};",
                   "sp.textContent = k.toUpperCase() + ' ' + money(+v);",
                   ".of-bar-matp { color: #fb923c; } .of-bar-mbp { color: #22c55e; }",
                   "ofbSpread(low);", "if (TAB === 'find') ofbPaint();"):
        assert needle in html, needle
    bar = html[html.index('id="ofBarTrack"'):html.index('id="ofMsg"')]
    assert 'data-lvl="matp"' in bar and 'data-ofh="target"' in bar
