"""Sector Rotation -> the Sector Chart tab (v4.250): the periods, the ETF table and chart
series, the holdings table with sector / industry / returns, the page and its API."""
from __future__ import annotations

import pytest

from app.main import app
from app.services import sector_chart as sc
from tests.test_options_page import _client


def _bars(n, start=100.0, step=1.0):
    """n daily bars, close rising by `step` a day, dated 2026-01-01 onwards (weekday-blind)."""
    import datetime as _dt
    d0 = _dt.date(2026, 1, 1)
    return [{"time": (d0 + _dt.timedelta(days=i)).isoformat(), "close": start + i * step} for i in range(n)]


def test_the_26_periods_are_trading_days():
    keys = [k for k, _, _ in sc.PERIODS]
    assert keys[:3] == ["1d", "2d", "3d"] and keys[9] == "10d" and keys[10] == "1w" and keys[19] == "10w"
    assert keys[20] == "1m" and keys[-1] == "6m" and len(keys) == 26
    assert sc.PERIOD_DAYS["1w"] == 5 and sc.PERIOD_DAYS["10w"] == 50 and sc.PERIOD_DAYS["1m"] == 21 and sc.PERIOD_DAYS["6m"] == 126
    assert sc.ret([100, 110], 1) == pytest.approx(10.0) and sc.ret([100, 110], 2) is None
    assert sc.returns([100.0] * 200)["6m"] == pytest.approx(0.0)


def test_the_etf_table_carries_returns_series_and_a_sparkline_tail(monkeypatch):
    def fake(symbols, rng):
        return {s: (lambda b: ([x["time"] for x in b], [x["close"] for x in b]))(_bars(600, 100.0, 0.5 if s == "XLK" else 0.1))
                for s in symbols}
    monkeypatch.setattr(sc, "_fetch_many", fake)
    sc._etf_cache.clear()
    t = sc.etf_table()
    assert [e["symbol"] for e in t["etfs"]] == sc.ETF_SYMBOLS and len(t["etfs"]) == 13
    xlk = next(e for e in t["etfs"] if e["symbol"] == "XLK")
    assert xlk["color"] == sc.ETF_COLORS["XLK"] and xlk["full_name"].startswith("Technology")
    assert xlk["last"] == 100 + 599 * 0.5 and len(xlk["tail"]) == sc.MAX_DAYS + 1
    assert xlk["ret"]["1d"] == pytest.approx(0.5 / (xlk["last"] - 0.5) * 100, abs=0.01)
    assert set(xlk["ret"]) == set(sc.PERIOD_DAYS)
    assert len(t["series"]["XLK"]) == 2 * 252 + 5 and t["series"]["XLK"][-1][1] == xlk["last"]
    assert t["default"] == "1m" and [p["key"] for p in t["periods"]][-1] == "6m"
    sc._etf_cache.clear()


def test_the_holdings_table_joins_weights_sector_industry_and_returns(monkeypatch):
    from app.services import etf_holdings as eh

    monkeypatch.setattr(eh, "holdings", lambda s: {"symbol": s, "as_of": "2026-11-06", "source": "ssga", "stale": False, "error": None,
                                                   "rows": [{"symbol": "AAPL", "name": "APPLE INC", "weight": 12.5},
                                                            {"symbol": "BRK.B", "name": "BERKSHIRE", "weight": 1.0},
                                                            {"symbol": "NVDA", "name": "NVIDIA CORP", "weight": 14.0}]})
    monkeypatch.setattr(sc, "industry_map", lambda **kw: ({"AAPL": {"sector": "Technology", "industry": "Consumer Electronics", "company": "Apple Inc"},
                                                          "NVDA": {"sector": "Technology", "industry": "Semiconductors", "company": "NVIDIA Corp"}}, "ready"))
    asked = {}

    def fake(symbols, rng):
        asked["symbols"] = list(symbols)
        return {s: (lambda b: ([x["time"] for x in b], [x["close"] for x in b]))(_bars(300, 50.0, 1.0)) for s in symbols}
    monkeypatch.setattr(sc, "_fetch_many", fake)
    t = sc.holdings_table("XLK", cap=2)
    assert t["etf"] == "XLK" and t["n"] == 3 and t["capped"] == 2 and t["industry_state"] == "ready"
    assert [r["symbol"] for r in t["rows"]] == ["NVDA", "AAPL", "BRK.B"]          # weight order
    assert asked["symbols"] == ["NVDA", "AAPL"]                                      # the heaviest `cap` are priced
    nvda, aapl, brk = t["rows"]
    assert nvda["sector"] == "Technology" and nvda["industry"] == "Semiconductors" and nvda["name"] == "NVIDIA Corp"
    assert nvda["last"] == 349.0 and nvda["ret"]["1d"] == pytest.approx(1 / 348 * 100, abs=0.01)
    assert brk["chart_symbol"] == "BRK-B" and brk["ret"] is None and brk["sector"] is None and brk["name"] == "Berkshire"
    assert aapl["ret"]["6m"] == pytest.approx(126 / (349 - 126) * 100, abs=0.01)


def test_the_page_has_two_side_tabs_and_the_api_answers(engine, user, monkeypatch):
    monkeypatch.setattr(sc, "_fetch_many", lambda symbols, rng: {s: ([], []) for s in symbols})
    sc._etf_cache.clear()
    try:
        c = _client(engine, user.id)
        html = c.get("/sector").text
        html2 = c.get("/sector?tab=chart").text
        api = c.get("/sector/api/etfs").json()
        bad = c.get("/sector/api/holdings?symbol=NOPE").json()
    finally:
        app.dependency_overrides.clear()
    assert html.count('class="sc-tab ') == 2 and "RRG Chart" in html and "Sector Chart" in html
    assert 'data-tab="rrg" title="RRG Chart" class="sc-tab block px-3 py-3 border-l-2 transition on"' in html
    assert 'data-tab="chart" title="Sector Chart" class="sc-tab block px-3 py-3 border-l-2 border-t border-t-slate-800/60 transition on"' in html2
    assert 'id="scRailBtn"' in html and 'th_sector_rail_folded' in html and "title: e.name" in html       # v4.253 / v4.255
    assert "<h1" not in html and "ETF history" not in html                                                  # v4.255: no headings
    assert 'id="scTags"' in html and "function placeTags()" in html                                         # v4.254
    assert "#scGrid.sc-folded { grid-template-columns: 3rem minmax(0, 1fr); }" in html and "order: 2" not in html   # v4.256: the strip stays left
    assert 'id="scTicks" class="w-full lg:w-56' in html and html.index('id="scChart"') < html.index('id="scTicks"')  # the tick boxes right of the chart
    assert html.count('class="sc-fold ') == 2 and 'id="scFold-perf"' in html and 'id="scFold-chart"' in html         # v4.257: the cards collapse
    assert html.count('class="sc-tick"') == 13 and 'rrg.optuma.com' in html
    # v4.251: a group selector (All days / All weeks / All months) in place of 26 period pills;
    # the 26 periods travel to the page as JSON and become the tables' columns
    assert html.count('class="sc-grp ') == 3 and 'data-group="months" title="1m to 6m - 21 sessions each">All months' in html
    assert 'hover:text-slate-200 on"\n                    data-group="months"' in html      # the default group is on
    assert html.count('class="sc-per ') == 0 and '"key": "10w"' in html and "var group = 'months'" in html
    assert "Holdings" in html and "Industry" in html and "lightweight-charts" in html
    assert api["ok"] and len(api["etfs"]) == 13 and api["etfs"][0]["last"] is None
    assert bad == {"ok": False, "error": "unknown fund"}


def test_the_ticked_funds_are_remembered_per_member(engine, user, monkeypatch):
    """v4.258: the tick boxes open on the member's saved choice (User.prefs), SPY until they
    have ticked anything; an empty choice is kept as 'none ticked'; unknown symbols are dropped."""
    monkeypatch.setattr(sc, "_fetch_many", lambda symbols, rng: {s: ([], []) for s in symbols})
    sc._etf_cache.clear()

    def checked(html):
        import re
        return re.findall(r'value="([A-Z]+)" checked', html)

    try:
        c = _client(engine, user.id)
        assert checked(c.get("/sector?tab=chart").text) == ["SPY"]
        r = c.post("/sector/api/ticks", json={"symbols": ["xlk", "XLV", "NOPE", "XLK"]}).json()
        assert r == {"ok": True, "symbols": ["XLK", "XLV"]}
        assert checked(c.get("/sector?tab=chart").text) == ["XLK", "XLV"]        # page order, saved set
        assert c.post("/sector/api/ticks", json={"symbols": []}).json()["symbols"] == []
        assert checked(c.get("/sector?tab=chart").text) == []
        assert c.post("/sector/api/ticks", json={"symbols": None}).json()["symbols"] is None
        assert checked(c.get("/sector?tab=chart").text) == ["SPY"]              # cleared -> the default again
        assert "function saveTicks()" in c.get("/sector").text
    finally:
        app.dependency_overrides.clear()
