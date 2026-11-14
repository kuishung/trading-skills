"""Which economic releases a member's Calendar shows (v4.263).

The TradingView feed lists every release of every picked country - far more than a
reader wants on a month grid (user, 2026-11-14: "the economic calendar is set to have
all event and it is so many. I want user to be able to select which event to show in
his calendar so it is a per user setting; by default the calendar for the item in the
macro insight will be default calendar events").

So each member keeps ONE of three choices in ``User.prefs[PREF_KEY]``:

  * absent            -> MACRO: the releases the Macro Insights topics read (CPI, PCE, PPI,
                         the Fed decision, unemployment, payrolls, GDP) - the default
  * ``"all"``         -> ALL: every release the feed has (what the page showed before)
  * ``{"titles": []}`` -> CUSTOM: the member's own list of release titles, as the feed
                         names them ("Inflation Rate YoY", "Non Farm Payrolls" ...)

A title is matched across every picked country, so "Inflation Rate YoY" ticked once
shows the US, EU and UK prints alike when those countries are on. The Macro set is
derived from ``macro_topics`` (each topic's ``release`` pattern and the three price
indexes), so a topic added there joins the default calendar by itself.
"""
from __future__ import annotations

import datetime as _dt
import re

from . import calendars as cal

PREF_KEY = "cal_econ_events"
MODE_MACRO, MODE_ALL, MODE_CUSTOM = "macro", "all", "custom"
MODES = (MODE_MACRO, MODE_ALL, MODE_CUSTOM)
MODE_LABELS = {MODE_MACRO: "Macro Insights", MODE_ALL: "All events", MODE_CUSTOM: "My own list"}

# The catalogue the picker lists: every title the feed has within this many days either
# side of today (monthly prints recur, so ten weeks each way sees each of them at least
# twice; quarterly GDP at least once).
CATALOGUE_DAYS = 70

_macro_cache: list[re.Pattern] | None = None


def macro_patterns() -> list[re.Pattern]:
    """The release-title patterns of the Macro Insights topics (compiled once)."""
    global _macro_cache
    if _macro_cache is None:
        from .macro_topics import _PRICE_INDEX, TOPICS

        pats = [t["release"] for t in TOPICS if t.get("release")]
        pats += [p for _, p, _ in _PRICE_INDEX.values()]
        _macro_cache = [re.compile(p) for p in dict.fromkeys(pats)]
    return _macro_cache


def is_macro(title: str) -> bool:
    t = (title or "").strip()
    return any(p.match(t) for p in macro_patterns())


def selection(user) -> tuple[str, set[str]]:
    """``(mode, titles)`` for this member: ``titles`` is only meaningful for CUSTOM."""
    prefs = getattr(user, "prefs", None) or {}
    v = prefs.get(PREF_KEY)
    if v == MODE_ALL:
        return MODE_ALL, set()
    if isinstance(v, dict) and isinstance(v.get("titles"), list):
        return MODE_CUSTOM, {str(t).strip() for t in v["titles"] if str(t).strip()}
    return MODE_MACRO, set()


def keep(title: str, mode: str, titles: set[str]) -> bool:
    if mode == MODE_ALL:
        return True
    if mode == MODE_CUSTOM:
        return (title or "").strip() in titles
    return is_macro(title)


def filter_events(events: list[dict], mode: str, titles: set[str]) -> list[dict]:
    if mode == MODE_ALL:
        return list(events)
    return [e for e in events if keep(e.get("title", ""), mode, titles)]


def save(db, user, mode: str, titles) -> tuple[str, set[str]]:
    """Store the member's choice (ORM, portable). CUSTOM with no title is kept as an empty
    list - "show none" is a real choice; MACRO clears the key so the default applies."""
    from ..models import User

    u = db.get(User, user.id)
    if u is None:
        return selection(user)
    prefs = dict(u.prefs or {})
    if mode == MODE_ALL:
        prefs[PREF_KEY] = MODE_ALL
    elif mode == MODE_CUSTOM:
        seen: list[str] = []
        for t in titles or []:
            t = str(t).strip()
            if t and t not in seen:
                seen.append(t)
        prefs[PREF_KEY] = {"titles": seen}
    else:
        prefs.pop(PREF_KEY, None)
    u.prefs = prefs
    db.commit()
    return selection(u)


def catalogue(countries, mode: str, titles: set[str], today: _dt.date | None = None) -> list[dict]:
    """Every release title the feed has around today for these countries, one row each:
    ``{title, importance, n, macro, on}`` - ``on`` = shown under the member's current
    choice. Macro titles first (loudest first), then the rest alphabetically."""
    today = today or cal.today_et()
    start, end = today - _dt.timedelta(days=CATALOGUE_DAYS), today + _dt.timedelta(days=CATALOGUE_DAYS)
    try:
        events = cal.economic_events(start, end, list(countries or ["US"]), -1)
    except Exception:  # noqa: BLE001 - the service soft-fails already; never break the picker
        events = []
    rows: dict[str, dict] = {}
    for e in events:
        t = (e.get("title") or "").strip()
        if not t:
            continue
        r = rows.get(t)
        if r is None:
            r = rows[t] = {"title": t, "importance": -1, "n": 0, "macro": is_macro(t), "on": keep(t, mode, titles)}
        r["n"] += 1
        r["importance"] = max(r["importance"], int(e.get("importance", -1)))
    # a custom title the feed does not list right now still belongs on the member's list
    if mode == MODE_CUSTOM:
        for t in sorted(titles):
            rows.setdefault(t, {"title": t, "importance": -1, "n": 0, "macro": is_macro(t), "on": True})
    return sorted(rows.values(), key=lambda r: (not r["macro"], -r["importance"] if r["macro"] else 0, r["title"].lower()))
