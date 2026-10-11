"""The US-session clock the Options module shares: "is the market open right now?",
"what was the last session?", "step this date back over a weekend or a holiday?".

Why one module
--------------
Three places need the same answer and must never disagree: the order ticket's
"Refresh first" banner (is the US session open while the quotes on screen are
from before the last close?), the nightly job's health pill (is ``run_on`` the last
ET trading day, stepped back over weekends and NYSE holidays?), and the signal
store's ``stale`` flag (is the snapshot older than the previous ET trading day?).
Each one used to be a weekday check somewhere; a Thanksgiving Thursday would then
read as "stale" and "job missed" on a day nothing could have run.

Conventions (the ones ``spread_monitor.et_today`` set)
-------------------------------------------------------
* The exchange clock is **New York**; the member trades from Malaysia (UTC+8), so
  for most of their working day the local date is already tomorrow in New York.
  Every date here is an ET date; ``et_today()`` returns it as ``YYYY-MM-DD`` like
  ``spread_monitor.et_today()`` does.
* ``zoneinfo`` needs the ``tzdata`` package on Windows. The fallback applies US
  daylight-saving by rule (second Sunday of March to first Sunday of November,
  02:00 local), the same fallback ``spread_monitor._et_now`` carries, so the clock
  is right even without the package.
* Holidays: the calendar service is asked first (``calendars.nyse_holidays(year)``
  when a later version grows it); until then the static NYSE rules below apply, plus
  the one-off closures no rule produces (``_SPECIAL_CLOSURES`` - national days of
  mourning, Hurricane Sandy), which are added to either list.
  Early closes (the day after Thanksgiving, Christmas Eve) are treated as full
  sessions - the one place that would notice is the ticket's "Refresh first"
  banner, and showing it for an extra afternoon is harmless.

Every function takes an optional ``now`` so the tests (and a "what if" caller)
can pin the clock; ``now`` may be aware (any zone) or naive, in which case it is
read as UTC like every other naive stamp in this code base.
"""
from __future__ import annotations

import datetime as _dt

try:
    from zoneinfo import ZoneInfo

    _ET = ZoneInfo("America/New_York")
except Exception:  # noqa: BLE001 - ZoneInfoNotFoundError, or no zoneinfo at all
    _ET = None

try:
    from zoneinfo import ZoneInfo as _ZI

    _MYT = _ZI("Asia/Kuala_Lumpur")
except Exception:  # noqa: BLE001
    _MYT = None

# Malaysia has no daylight saving, so a fixed offset is an exact fallback.
_MYT_FIXED = _dt.timezone(_dt.timedelta(hours=8), "MYT")

SESSION_OPEN = _dt.time(9, 30)
SESSION_CLOSE = _dt.time(16, 0)


# ────────────────────────────────── now, in a zone ──────────────────────────────────

def _as_utc(now: _dt.datetime | None) -> _dt.datetime:
    if now is None:
        return _dt.datetime.now(_dt.timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=_dt.timezone.utc)
    return now.astimezone(_dt.timezone.utc)


def _nth_sunday(year: int, month: int, n: int) -> _dt.date:
    d = _dt.date(year, month, 1)
    d += _dt.timedelta(days=(6 - d.weekday()) % 7)      # first Sunday
    return d + _dt.timedelta(weeks=n - 1)


def _fallback_et(utc: _dt.datetime) -> _dt.datetime:
    """US Eastern by rule when no zone database is installed: DST runs from the
    second Sunday of March to the first Sunday of November, switching at 02:00
    local (07:00 UTC going in under EST, 06:00 UTC going out under EDT)."""
    y = utc.year
    start = _dt.datetime.combine(_nth_sunday(y, 3, 2), _dt.time(7), _dt.timezone.utc)
    end = _dt.datetime.combine(_nth_sunday(y, 11, 1), _dt.time(6), _dt.timezone.utc)
    offset = -4 if start <= utc < end else -5
    return utc.astimezone(_dt.timezone(_dt.timedelta(hours=offset), "ET"))


def et_now(now: _dt.datetime | None = None) -> _dt.datetime:
    """Now (or ``now``) in New York, as an aware datetime."""
    utc = _as_utc(now)
    if _ET is not None:
        return utc.astimezone(_ET)
    return _fallback_et(utc)


def et_date(now: _dt.datetime | None = None) -> _dt.date:
    """Today's ET date as a ``date``."""
    return et_now(now).date()


def et_today(now: _dt.datetime | None = None) -> str:
    """Today's ET date as ``YYYY-MM-DD`` - the key every daily table uses
    (``spread_monitor.et_today`` returns the same string)."""
    return et_date(now).isoformat()


def myt_now(now: _dt.datetime | None = None) -> _dt.datetime:
    """Now (or ``now``) in Malaysia, as an aware datetime."""
    utc = _as_utc(now)
    return utc.astimezone(_MYT or _MYT_FIXED)


# ────────────────────────────────── NYSE holidays ──────────────────────────────────

def _easter(year: int) -> _dt.date:
    """Gregorian Easter Sunday (anonymous algorithm) - Good Friday is two days before."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7       # noqa: E741 - the algorithm's own letter
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return _dt.date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> _dt.date:
    """The n-th ``weekday`` (Mon=0) of a month; n = -1 for the last one."""
    if n > 0:
        d = _dt.date(year, month, 1)
        d += _dt.timedelta(days=(weekday - d.weekday()) % 7)
        return d + _dt.timedelta(weeks=n - 1)
    nxt = _dt.date(year + (month == 12), (month % 12) + 1, 1)
    d = nxt - _dt.timedelta(days=1)
    d -= _dt.timedelta(days=(d.weekday() - weekday) % 7)
    return d


def _observed(d: _dt.date) -> _dt.date | None:
    """A fixed-date holiday falling on Saturday is observed on the Friday, on a
    Sunday the Monday. New Year's Day on a Saturday is NOT observed on Dec 31 by
    the NYSE (it stays open), so the caller handles that one."""
    if d.weekday() == 5:
        return d - _dt.timedelta(days=1)
    if d.weekday() == 6:
        return d + _dt.timedelta(days=1)
    return d


# Full-day NYSE closures no yearly rule produces: national days of mourning for a former
# president (Ford 2007-01-02, G. H. W. Bush 2018-12-05, Carter 2025-01-09) and Hurricane
# Sandy (2012-10-29 / 30). A future one-off closure goes here too - without it the
# screener's stock-day list waits forever for a grouped day Massive never publishes.
_SPECIAL_CLOSURES: frozenset[_dt.date] = frozenset({
    _dt.date(2007, 1, 2),
    _dt.date(2012, 10, 29),
    _dt.date(2012, 10, 30),
    _dt.date(2018, 12, 5),
    _dt.date(2025, 1, 9),
})


def _special_closures(year: int) -> set[_dt.date]:
    return {d for d in _SPECIAL_CLOSURES if d.year == year}


def _static_nyse_holidays(year: int) -> set[_dt.date]:
    out: set[_dt.date] = set(_special_closures(year))
    ny = _dt.date(year, 1, 1)
    if ny.weekday() == 6:                      # Sunday -> Monday; Saturday -> no closure
        out.add(ny + _dt.timedelta(days=1))
    elif ny.weekday() < 5:
        out.add(ny)
    out.add(_nth_weekday(year, 1, 0, 3))       # Martin Luther King Jr. Day - third Monday of January
    out.add(_nth_weekday(year, 2, 0, 3))       # Presidents' Day - third Monday of February
    out.add(_easter(year) - _dt.timedelta(days=2))   # Good Friday
    out.add(_nth_weekday(year, 5, 0, -1))      # Memorial Day - last Monday of May
    if year >= 2022:
        obs = _observed(_dt.date(year, 6, 19))     # Juneteenth (observed by the NYSE since 2022)
        if obs is not None:
            out.add(obs)
    obs = _observed(_dt.date(year, 7, 4))      # Independence Day
    if obs is not None:
        out.add(obs)
    out.add(_nth_weekday(year, 9, 0, 1))       # Labor Day - first Monday of September
    out.add(_nth_weekday(year, 11, 3, 4))      # Thanksgiving - fourth Thursday of November
    obs = _observed(_dt.date(year, 12, 25))    # Christmas
    if obs is not None:
        out.add(obs)
    return out


_HOLIDAY_CACHE: dict[int, frozenset[_dt.date]] = {}


def nyse_holidays(year: int) -> frozenset[_dt.date]:
    """Full-day NYSE closures in ``year``. The calendar service is asked first
    (``calendars.nyse_holidays(year)``) so a feed-backed list wins the day it
    exists; the static rules above are the fallback. The one-off closures
    (``_SPECIAL_CLOSURES``) are in the answer either way."""
    hit = _HOLIDAY_CACHE.get(year)
    if hit is not None:
        return hit
    days: set[_dt.date] | None = None
    try:
        from . import calendars

        fn = getattr(calendars, "nyse_holidays", None)
        if callable(fn):
            got = fn(year)
            if got:
                days = {d if isinstance(d, _dt.date) else _dt.date.fromisoformat(str(d)) for d in got}
    except Exception:  # noqa: BLE001 - the service is optional here
        days = None
    if not days:
        days = _static_nyse_holidays(year)
    out = frozenset(set(days) | _special_closures(year))
    _HOLIDAY_CACHE[year] = out
    return out


def is_trading_day(d: _dt.date | str) -> bool:
    """A weekday that is not a full-day NYSE closure."""
    if isinstance(d, str):
        d = _dt.date.fromisoformat(d)
    return d.weekday() < 5 and d not in nyse_holidays(d.year)


def last_trading_day(d: _dt.date | str) -> _dt.date:
    """``d`` itself when it is a trading day, else the nearest trading day before it."""
    if isinstance(d, str):
        d = _dt.date.fromisoformat(d)
    while not is_trading_day(d):
        d -= _dt.timedelta(days=1)
    return d


def prev_trading_day(d: _dt.date | str, n: int = 1) -> _dt.date:
    """The ``n``-th trading day strictly before ``d``."""
    if isinstance(d, str):
        d = _dt.date.fromisoformat(d)
    while n > 0:
        d -= _dt.timedelta(days=1)
        if is_trading_day(d):
            n -= 1
    return d


def next_trading_day(d: _dt.date | str) -> _dt.date:
    """The first trading day strictly after ``d``."""
    if isinstance(d, str):
        d = _dt.date.fromisoformat(d)
    d += _dt.timedelta(days=1)
    while not is_trading_day(d):
        d += _dt.timedelta(days=1)
    return d


# ────────────────────────────────── the session ──────────────────────────────────

def _us_session_open(now: _dt.datetime | None = None) -> bool:
    """True while the US regular session is open: a trading day, 09:30 <= ET time
    < 16:00. Used by the ticket's "Refresh first" banner and the refresh cooldown
    exemption. The leading underscore is the contract's own spelling (II.2.5)."""
    et = et_now(now)
    if not is_trading_day(et.date()):
        return False
    return SESSION_OPEN <= et.time() < SESSION_CLOSE


us_session_open = _us_session_open      # the public alias; same function


def last_session_close(now: _dt.datetime | None = None) -> _dt.datetime:
    """The most recent 16:00 ET close at or before ``now``, as an aware ET datetime.

    During a session (and before the open) this is the PREVIOUS trading day's
    close: quotes stamped before it are "from before the last close" and the
    ticket asks for a Refresh when the market is open again."""
    et = et_now(now)
    d = et.date()
    if is_trading_day(d) and et.time() >= SESSION_CLOSE:
        day = d
    else:
        day = prev_trading_day(d) if is_trading_day(d) else last_trading_day(d)
    return _dt.datetime.combine(day, SESSION_CLOSE, tzinfo=et.tzinfo)


def older_than_last_close(as_of: _dt.datetime | None, now: _dt.datetime | None = None) -> bool:
    """Is a feed stamp (naive UTC, the ``as_of`` convention) older than the last
    session close? None reads as older (nothing is known about it)."""
    if as_of is None:
        return True
    return _as_utc(as_of) < last_session_close(now).astimezone(_dt.timezone.utc)
