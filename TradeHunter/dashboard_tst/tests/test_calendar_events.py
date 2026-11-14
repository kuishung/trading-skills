"""v4.263: which economic releases a member's Calendar shows - Macro Insights by default, every
event, or the member's own list (services/cal_events.py, routes/calendar.py events_*)."""
from __future__ import annotations

import datetime as _dt

from app.main import app
from app.services import cal_events as ce
from app.services import calendars as cal
from tests.test_options_page import _client


def _ev(title, day, imp=0, country="US"):
    when = _dt.datetime(day.year, day.month, day.day, 12, 30)
    return {"id": title, "when": when, "date": day, "country": country, "country_label": country, "importance": imp,
            "impact": cal.IMPORTANCE.get(imp, "low"), "title": title, "period": "", "actual": None, "forecast": None,
            "previous": None, "source": "", "comment": "", "surprise": None}


FEED = [("Inflation Rate YoY", 1), ("Non Farm Payrolls", 1), ("Fed Interest Rate Decision", 1), ("GDP Growth Rate QoQ Adv", 1),
        ("Core PCE Price Index YoY", 1), ("Building Permits", 0), ("Initial Jobless Claims", 0), ("Baker Hughes Oil Rig Count", -1)]


def test_the_macro_default_keeps_the_macro_topics_releases():
    assert ce.selection(type("U", (), {"prefs": None})()) == (ce.MODE_MACRO, set())
    for t in ("Inflation Rate YoY", "Core Inflation Rate YoY", "PCE Price Index YoY", "PPI MoM", "Fed Interest Rate Decision",
              "Interest Rate", "FOMC Minutes", "Unemployment Rate", "Non Farm Payrolls", "GDP Growth Rate QoQ Adv"):
        assert ce.is_macro(t), t
    for t in ("Building Permits", "Initial Jobless Claims", "Baker Hughes Oil Rig Count", ""):
        assert not ce.is_macro(t), t
    evs = [_ev(t, _dt.date(2026, 11, 20), i) for t, i in FEED]
    assert [e["title"] for e in ce.filter_events(evs, ce.MODE_MACRO, set())] == \
        ["Inflation Rate YoY", "Non Farm Payrolls", "Fed Interest Rate Decision", "GDP Growth Rate QoQ Adv", "Core PCE Price Index YoY"]
    assert len(ce.filter_events(evs, ce.MODE_ALL, set())) == 8
    assert [e["title"] for e in ce.filter_events(evs, ce.MODE_CUSTOM, {"Building Permits"})] == ["Building Permits"]
    assert ce.filter_events(evs, ce.MODE_CUSTOM, set()) == []            # "none" is a real choice


def test_the_catalogue_lists_macro_titles_first_and_marks_what_is_on(monkeypatch):
    d = _dt.date(2026, 11, 20)
    monkeypatch.setattr(cal, "economic_events", lambda s, e, c, m: [_ev(t, d, i) for t, i in FEED] + [_ev("Building Permits", d, 0)])
    rows = ce.catalogue(["US"], ce.MODE_MACRO, set(), today=d)
    assert [r["title"] for r in rows][:5] == ["Core PCE Price Index YoY", "Fed Interest Rate Decision", "GDP Growth Rate QoQ Adv",
                                               "Inflation Rate YoY", "Non Farm Payrolls"]
    assert [r["title"] for r in rows][5:] == ["Baker Hughes Oil Rig Count", "Building Permits", "Initial Jobless Claims"]
    bp = next(r for r in rows if r["title"] == "Building Permits")
    assert bp["n"] == 2 and bp["importance"] == 0 and not bp["macro"] and not bp["on"]
    assert all(r["on"] for r in rows if r["macro"])
    rows = ce.catalogue(["US"], ce.MODE_CUSTOM, {"Building Permits", "Not In Feed"}, today=d)
    assert next(r for r in rows if r["title"] == "Not In Feed")["on"] and next(r for r in rows if r["title"] == "Inflation Rate YoY")["on"] is False


def test_the_month_panel_and_the_picker_follow_the_members_choice(engine, user, monkeypatch):
    d = cal.today_et()
    monkeypatch.setattr(cal, "economic_events", lambda s, e, c, m: [_ev(t, d, i) for t, i in FEED])
    monkeypatch.setattr(cal, "earnings_range", lambda days: [])
    try:
        c = _client(engine, user.id)
        html = c.get("/calendar/month/panel").text
        assert "Events: Macro Insights</button>" in html and 'hx-target="#calEvents"' in html
        assert "Inflation Rate YoY" in html and "Building Permits" not in html          # the default keeps the macro set
        assert 'id="calEvents"' in c.get("/calendar/month").text

        pk = c.get("/calendar/events/panel?countries=US&back=/calendar/month/panel?imp=all").text
        assert 'id="calEventsForm" hx-post="/calendar/events/save"' in pk and 'value="macro" checked' in pk
        assert 'value="Inflation Rate YoY" checked' in pk and 'value="Building Permits" class' in pk
        assert '<input type="hidden" name="back" value="/calendar/month/panel?imp=all">' in pk
        assert pk.index("Macro Insights releases") < pk.index("Other releases")

        r = c.post("/calendar/events/save", data={"mode": "custom", "titles": ["Building Permits", "Non Farm Payrolls"], "back": "https://evil/x"}).text
        assert 'id="calPanel"' in r and 'hx-swap-oob="true"' in r and 'hx-get="/calendar/month/panel"' in r   # a foreign back is dropped
        html = c.get("/calendar/month/panel").text
        assert "Events: My own list (2)</button>" in html and "Building Permits" in html and "Inflation Rate YoY" not in html
        pk = c.get("/calendar/events/panel").text
        assert 'value="custom" checked' in pk and 'value="Building Permits" checked' in pk and 'value="Inflation Rate YoY" class' in pk

        c.post("/calendar/events/save", data={"mode": "all"})
        html = c.get("/calendar/month/panel").text
        assert "Events: All events</button>" in html and "Baker Hughes Oil Rig Count" in html
        c.post("/calendar/events/save", data={"mode": "custom"})                      # none ticked = none shown
        assert "Events: My own list (0)</button>" in c.get("/calendar/month/panel").text
        c.post("/calendar/events/save", data={"mode": "macro"})
        assert "Events: Macro Insights</button>" in c.get("/calendar/month/panel").text
        assert c.get("/calendar/events/close").text == ""
    finally:
        app.dependency_overrides.clear()
