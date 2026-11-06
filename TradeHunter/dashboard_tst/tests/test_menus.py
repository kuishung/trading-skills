"""The menu bar's registry (``app/menus.py``)."""
from types import SimpleNamespace

from app import menus


def test_insights_is_a_dropdown_after_the_calendar():
    """v4.242 (user: "I need to build a menu call Insights" with "Macro, Industrial and Company as submenu", "after
    calendar"; the label "Industry"): one dropdown right after the Calendar; Macro and Company open the existing
    pages under their old keys, Industry has its own (new) page; "the sector and industry menu change it to Sector
    Rotation" - the same key and page, a new label, still in the bar."""
    admin = SimpleNamespace(role="admin", menu_access=None)
    nav = menus.nav_for(admin)
    assert nav[0] == {"kind": "item", "href": "/calendar/month", "label": "Calendar"}
    assert nav[1] == {"kind": "group", "label": "Insights",
                      "items": [("/macro", "Macro"), ("/insights/industry", "Industry"), ("/company-analysis", "Company")]}
    assert nav[2] == {"kind": "item", "href": "/sector", "label": "Sector Rotation"}
    assert {"macro", "industry", "sector", "company_analysis"} <= set(menus.ALL_KEYS)
    assert "macro" not in menus.HIDDEN_KEYS and "company_analysis" not in menus.HIDDEN_KEYS
    assert len(menus.ALL_KEYS) == len(set(menus.ALL_KEYS))
    # a member granted only Company sees Insights holding just that entry
    member = SimpleNamespace(role="member", menu_access=["company_analysis"])
    assert menus.nav_for(member) == [{"kind": "group", "label": "Insights", "items": [("/company-analysis", "Company")]}]


def test_the_industry_page_opens(engine, user):
    from tests.test_options_page import _client
    from app.main import app
    try:
        r = _client(engine, user.id).get("/insights/industry")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 200 and ">Industry</h1>" in r.text and "Sector Rotation" in r.text
