"""One-off NYSE closures in the session clock (``app/services/clock.py``, v4.137).

No yearly rule produces a national day of mourning or a storm closure, so they are listed
(``clock._SPECIAL_CLOSURES``). Without 2025-01-09 (President Carter) the screener
collector's two-year stock-day list held a weekday Massive never publishes a grouped day
for, and its "stock days pending" never reached zero. No network.
"""
from __future__ import annotations

import datetime as _dt

import pytest

from app.services import calendars, clock, scr_collector

CLOSURES = ("2007-01-02", "2012-10-29", "2012-10-30", "2018-12-05", "2025-01-09")


@pytest.mark.parametrize("day", CLOSURES)
def test_special_closures_are_not_trading_days(day):
    d = _dt.date.fromisoformat(day)
    assert d.weekday() < 5                                   # a weekday a rule would have kept
    assert clock.is_trading_day(day) is False
    assert d in clock.nyse_holidays(d.year)


def test_carter_day_of_mourning_steps_the_calendar():
    assert clock.is_trading_day("2025-01-09") is False
    assert clock.is_trading_day("2025-01-08") and clock.is_trading_day("2025-01-10")
    assert clock.prev_trading_day("2025-01-10") == _dt.date(2025, 1, 8)
    assert clock.next_trading_day("2025-01-08") == _dt.date(2025, 1, 10)
    assert clock.last_trading_day("2025-01-09") == _dt.date(2025, 1, 8)
    # the rule-based holidays of that year are all still there
    h = clock.nyse_holidays(2025)
    for day in ("2025-01-01", "2025-01-20", "2025-04-18", "2025-07-04", "2025-12-25"):
        assert _dt.date.fromisoformat(day) in h, day
    assert len(h) == 11                                      # 10 rule holidays + the closure


def test_screener_stock_days_skip_the_closure():
    sat = _dt.datetime(2026, 10, 10, 16, 0)                  # Saturday 12:00 ET (naive UTC)
    days = scr_collector.stock_days(sat)
    assert "2025-01-09" not in days
    assert "2025-01-08" in days and "2025-01-10" in days
    assert days[-1] == "2026-10-09"                          # Friday, the last published session


def test_a_calendar_service_list_still_gets_the_closures(monkeypatch):
    """When ``calendars.nyse_holidays`` exists (a feed-backed list) its answer is used -
    and a one-off closure it lacks is still added."""
    monkeypatch.setattr(clock, "_HOLIDAY_CACHE", {})
    monkeypatch.setattr(calendars, "nyse_holidays", lambda year: ["%d-12-25" % year], raising=False)
    h = clock.nyse_holidays(2025)
    assert h == frozenset({_dt.date(2025, 12, 25), _dt.date(2025, 1, 9)})
    assert clock.nyse_holidays(2024) == frozenset({_dt.date(2024, 12, 25)})
