"""The Watchlist grid (`_wl_macros.html` ticker_grid, v4.158): the price-to-MATP gap in $ and %
where the Trend and Sig columns were."""
from __future__ import annotations

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app import models
from app.db import get_db
from app.main import app
from app.routes import matp as matp_routes
from app.security import current_user


@pytest.fixture
def client(engine, user):
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    def _user(s=Depends(get_db)):
        return s.get(models.User, user.id)

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[current_user] = _user
    try:
        yield TestClient(app, follow_redirects=False)
    finally:
        app.dependency_overrides.clear()


def test_grid_shows_the_gap_to_matp_and_no_trend_or_sig(client, db, monkeypatch):
    db.add(models.MATPLevel(symbol="NVDA", exchange="NASDAQ", matp=200.0, mbp=173.9, trend="uptrend", signal="HOT"))
    db.add(models.MATPLevel(symbol="ABOVE", exchange="NYSE", matp=100.0, mbp=87.0, trend="uptrend", signal=None))
    db.add(models.MATPLevel(symbol="NOMATP", exchange="NYSE", matp=None, mbp=None, trend=None, signal=None))
    db.commit()
    live = {"NVDA": {"current": 182.0, "trend": "uptrend", "signal": "HOT"},
            "ABOVE": {"current": 104.0, "trend": "uptrend", "signal": None},
            "NOMATP": {"current": 50.0, "trend": None, "signal": None}}
    monkeypatch.setattr(matp_routes, "_watchlist_signals_fast", lambda levels: live)
    # v4.186: the option-liquidity verdict per ticker (Opt liq column) - Liquid / Fair / Thin against the
    # member's own lines, the figures in the hover; a ticker the screener does not list reads "-"
    def figs(oi, vol, chain, lv):
        return [{"key": "oi", "name": "Near OI (sum)", "value_text": oi, "fair_text": "5k", "liquid_text": "20k", "level": lv[0]},
                {"key": "vol", "name": "Near volume today", "value_text": vol, "fair_text": "1k", "liquid_text": "5k", "level": lv[1]},
                {"key": "chain", "name": "Chain OI", "value_text": chain, "fair_text": "20k", "liquid_text": "100k", "level": lv[2]}]
    liq = {"NVDA": {"level": "liquid", "label": "Liquid", "num": "12.4k", "near_words": "within 10% of 182.00 in 2026-11-20 and 2026-12-18, 20 contracts",
                    "figs": figs("12.4k", "31k", "2.1M", ("liquid", "liquid", "liquid")), "text": "NVDA option liquidity: Liquid."},
           "ABOVE": {"level": "fair", "label": "Fair", "num": "410", "near_words": "within 10% of 104.00 in 2026-11-20 and 2026-12-18, 8 contracts",
                     "figs": figs("410", "2.3k", "48k", ("fair", "fair", "fair")), "text": "ABOVE option liquidity: Fair."}}
    monkeypatch.setattr(matp_routes, "_watchlist_liquidity", lambda levels, user: liq)
    r = client.get("/matp/watchlist", params={"wl": "all"})
    assert r.status_code == 200
    html = r.text
    # the headers
    assert "&Delta;$" in html or "Δ$" in html
    assert ">Trend<" not in html and ">Sig<" not in html
    assert ">Opt liq</span>" in html
    # v4.187 (user: "show the liquidity in numbers and then the fair, thin, liquid next to it ... a table"):
    # the near OI, the verdict beside it, and a hidden table the delegated handler shows as a popover
    i = html.index('data-level="liquid"')
    cell = html[i:i + 2500]
    assert '<b class="text-slate-200 tabular-nums font-medium">12.4k</b> <span class="text-emerald-300">Liquid</span>' in cell
    assert '<div class="wl-liq-tip hidden">' in cell and "NVDA option liquidity</b>" in cell
    assert "<th>Fair &ge;</th><th>Liquid &ge;</th>" in cell and "Near OI (sum)" in cell and ">2.1M</td>" in cell
    assert "Near the money = within 10% of 182.00 in 2026-11-20 and 2026-12-18, 20 contracts." in cell
    j = html.index('data-level="fair"')
    assert '<b class="text-slate-200 tabular-nums font-medium">410</b> <span class="text-amber-300">Fair</span>' in html[j:j + 400]
    assert 'class="text-amber-300">410</td>' in html[j:j + 2500]          # the value coloured by the line it clears
    assert 'data-level=""' in html and "No option data for NOMATP in the screener" in html
    assert "window.__wlLiqPop" in html and "document.addEventListener('mouseover'" in html
    # v4.188 (user: "the black wording cannot see ... follow the setting of the toggle dark/light"): theme variables
    assert "#wlLiqPop{position:fixed;z-index:60;width:19rem;padding:.5rem .6rem;border:1px solid var(--tv-border-2);border-radius:.5rem;background:var(--tv-panel);color:var(--tv-text);" in html
    assert "#wlLiqPop .wl-liq-cap{margin-top:.3rem;color:var(--tv-muted);font-size:10px}" in html
    assert 'class="text-left wl-liq-name">Near OI (sum)</td>' in cell and 'class="wl-liq-line">20k</td>' in cell
    assert "text-slate-400" not in cell[cell.index('<div class="wl-liq-tip hidden">'):] .split("</div>\n    </div>")[0] if "wl-liq-tip" in cell else True
    # the member's lines under the list, with the defaults
    assert "Option liquidity settings" in html and 'hx-post="/matp/liq-thresholds"' in html
    assert 'name="liquid_oi" type="number" min="0" step="1" value="20000"' in html and ">Near OI (sum)</span>" in html
    # v4.214: the band and the number of monthlies, the member's own
    assert 'name="band_pct" type="number" min="1" max="50" step="1" value="10"' in html
    assert 'name="monthlies" type="number" min="1" max="6" step="1" value="2"' in html
    assert "Near the money = within 10% of the price in the first 2 monthly expiries." in html
    assert 'name="fair_chain" type="number" min="0" step="1" value="20000"' in html
    # NVDA: 182 - 200 = -18.00, -9.0 % of the MATP, green (below the target)
    assert "-18.00" in html and "-9.0%" in html
    # ABOVE: 104 - 100 = +4.00, +4.0 %, red (past the target)
    assert "+4.00" in html and "+4.0%" in html
    i_nvda, i_above = html.index(">NVDA<"), html.index(">ABOVE<")
    assert "text-emerald-300" in html[i_nvda:i_nvda + 1500]
    assert "text-rose-300" in html[i_above:i_above + 1500]
    # no MATP -> dashes, no crash
    i_no = html.index(">NOMATP<")
    assert html[i_no:i_no + 900].count(">-<") >= 2
    # Price, MATP and MBP are still there
    assert ">MATP<" in html and ">MBP<" in html and "173.90" in html
    # v4.196 (user): Last, MATP, MBP, then the gap in $ and % after the MBP, then Opt liq; no plot column
    head = html[html.index('title="My Watchlist"'):html.index(">Opt liq</span>")]
    assert ">Price<" not in head and ">Last</span>" in head
    assert head.index(">Last</span>") < head.index(">MATP<") < head.index(">MBP<") < head.index("&Delta;$") < head.index("&Delta;%")
    assert "&#9639;" not in html and "tv-plot" not in html
    row = html[html.index(">NVDA<"):html.index(">ABOVE<")]
    assert row.index(">182.00<") < row.index(">200.00<") < row.index(">173.90<") < row.index(">-18.00<") < row.index(">-9.0%<")


def test_saving_the_liquidity_lines_rerenders_the_panel_and_reports_a_bad_line(client, db, user, monkeypatch):
    db.add(models.MATPLevel(symbol="NVDA", exchange="NASDAQ", matp=200.0, mbp=173.9, trend="uptrend", signal="HOT"))
    db.commit()
    monkeypatch.setattr(matp_routes, "_watchlist_signals_fast", lambda levels: {"NVDA": {"current": 182.0}})
    monkeypatch.setattr(matp_routes, "_watchlist_liquidity", lambda levels, user: {})
    r = client.post("/matp/liq-thresholds", data={"wl": "all", "sym": "", "liquid_oi": "8000", "fair_vol": "500",
                                                  "band_pct": "15", "monthlies": "3"})
    assert r.status_code == 200 and 'value="8000"' in r.text and 'value="500"' in r.text
    assert 'name="band_pct" type="number" min="1" max="50" step="1" value="15"' in r.text
    assert 'name="monthlies" type="number" min="1" max="6" step="1" value="3"' in r.text
    db.expire_all()
    p = db.get(models.User, user.id).prefs
    assert p["liq_liquid_oi"] == 8000 and p["liq_band_pct"] == 15 and p["liq_monthlies"] == 3 and p["liq_rule"] == "sum"
    r = client.post("/matp/liq-thresholds", data={"wl": "all", "sym": "", "fair_oi": "9000"})
    assert r.status_code == 200 and "cannot be above the Liquid line" in r.text and 'value="8000"' in r.text
    r = client.post("/matp/liq-thresholds", data={"wl": "all", "sym": "", "monthlies": "9"})
    assert r.status_code == 200 and "the number of monthly expiries must be between 1 and 6" in r.text
