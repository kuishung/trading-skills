"""The Hermes Options Screener collector (OPTIONS_SCREENER_DESIGN.md §4; v4.136, first-run
fix v4.137): the always-on loop that keeps the screener database (``app/screener_db.py``,
the ``scr_*`` tables) filled with the whole US options market from Massive (formerly
Polygon.io), so the Options page can screen ~1M contracts without reading anything itself.

Data: Massive Options Starter (the whole-chain snapshot - greeks, IV, open interest, the
day bar; 15 min delayed; NO bid/ask - and option daily bars; the options contracts
reference list) and Stocks Basic (grouped daily bars of every stock, the ticker
reference list; ~5 requests a minute and about two years of history, the client paces
itself). Earnings dates come from Nasdaq's free calendar (``calendars.earnings_for``).
Every write goes through ``scr_store``.

The loop
--------
One ``tick()`` every 15 s never blocks for long: the work runs in executors and the tick
only starts jobs and files their results.

* ``WORKERS`` threads (``TST_SCREENER_WORKERS``, 8) share ONE ``massive.Client``
  (``concurrency=WORKERS``, ``max_rps=TST_SCREENER_MAX_RPS`` 40) - the chain reads of a
  market pass and the IV-history reads; each job opens its own DB session.
* the "universe" thread - the walk of Massive's options contracts list (below).
* the "stocks lane" thread - identities, grouped stock days, per-symbol bar fills and the
  technicals, one job at a time, in that priority.
* the "earnings" thread - Nasdaq's calendar (no Massive request), so a slow Nasdaq never
  holds the stock bars back.

1. **Universe** - daily from 07:30 ET (and at start when older than 20 h, or none):
   ``option_underlyings(exp_lte=today+60d, contract_type=call)`` walks ~250-800 pages of
   1,000 contracts. It is VISIBLE (the page and the tray show the page count) and
   RESUMABLE: a failure keeps the cursor of the page that failed and the walk carries on
   from there 60 s later (a cursor over 2 h old, or from another ET day, is dropped; a
   4xx on a resumed cursor starts again from page 1). On a first start (no complete list
   yet - ``universe_done`` unset) the walk is STREAMED: the stocks read so far are filed
   after the first page and every 25 pages / 30 s (``upsert_universe(deactivate=False)`` -
   a partial save never deactivates anything) and the first market pass starts on them at
   once and GROWS with the list. A complete walk files the whole list
   (``upsert_universe(deactivate=True, now=<completion>)``: a symbol absent from it goes
   inactive and stays in passes 10 more days), prunes, and stamps ``universe_done``. An
   empty list is a failure, not a refresh. With a complete list on file a failed refresh
   only warns (``warn``; yesterday's list keeps feeding the passes); without one it is an
   error - until the retried walk gets a page back (its progress is shown again; a new
   failure raises the error again). A pass that ends on a partial list is stamped
   ``scr_pass.partial``: a restart never takes such an EOD pass as the session's full one
   (the full pass follows the complete list). Identities may run as soon as some stocks
   are filed; earnings, stock days, bar fills and technicals wait for a complete list.
2. **Market passes** - on trading days a ``cycle`` pass every ``TST_SCREENER_CYCLE_MIN``
   (30) minutes from 09:45 to 16:00 ET, then one ``eod`` pass after 16:20 ET (a missed
   evening is caught up before the next open; on a weekend / holiday, the last trading
   day's). Every pass symbol, most contracts first: the whole chain
   (``chain_snapshot(exp today..today+TST_SCREENER_MAX_DTE)``, no strike window) ->
   ``opt_massive.standard_rows`` -> ``estimate_spot`` -> ``contract_rows`` (kept: open
   interest or volume; price = ``opt_massive.model_price`` from the contract's own IV,
   else the last trade; ``as_of`` = read time - 15 min) -> ``scr_store.replace_contracts``
   -> ``update_underlying_pass`` (spot, IV30, volumes / OI, expected move; the EOD pass
   files IV30 as the day's reading). An empty answer keeps a symbol's stored rows while
   they are from the last 2 sessions (a chain listed with ONLY adjusted series - a cash
   merger, a delisting - is final and clears them); an index read bare (``SPX``) that
   comes back empty is tried once as ``I:SPX``. A pass's percent / ETA weigh each symbol
   by its contract count in the stored universe (so they survive a restart).
   No single symbol stalls a pass: one failing (an HTTP 403 for its chain once any chain
   has been read, a 5xx, a bad reply) is counted and skipped (a refused symbol is left out
   of passes for 24 h); a Massive error that is not about the symbol (no key, a rejected
   key, Massive not reachable, or a 403 from 25 different symbols before any chain worked)
   pauses the pass and the rest is read when the pause ends; a symbol requeued twice
   counts as failed. A pass that stores no contract is not "done": it ends unfinished
   (early once 20 underlyings failed and none worked) and the passes pause 10 min doubling
   to 2 h; a mostly failed EOD pass is finished (the page reloads) but read again. An EOD
   pass still open when the session's cycle passes are due (or a newer EOD is due) closes
   unfinished; any pass over 12 h old does too.
3. **Stock days** - every session of the last 2 years (never before Stocks Basic's window,
   ``plan_start``) up to the last PUBLISHED one (``opt_massive.published_session``: a
   day's bars are out at 20:00 ET) that is not on file: ``grouped_daily`` adjusted (OHLCV)
   and unadjusted (``close_raw``), newest first, filed for the universe symbols only. A
   day Massive refuses (403) is retired when it is at the window's edge, else asked again
   in 1 h. A filed day that shows a > 40 % jump for a symbol (a split after its older bars
   were filed) re-reads that symbol's adjusted bars; a universe symbol with under 20 bars
   once the days are complete gets its own bars (``stock_daily``, at most once a day; not
   an index). The technicals run once the days are complete - and hourly while older days
   are still being filed, as soon as the newest 260 sessions are on file. Weekly: the
   ticker reference lists (stocks + indices) -> name / security type / exchange. Daily:
   the earnings dates of the next 70 days (the day of the last read survives a restart).
4. **IV history** - between passes, once the last 260 sessions' unadjusted closes are on
   file: underlyings without it (not the indices - there are no index closes to price
   against), most option volume first, up to ``WORKERS`` at a time ->
   ``opt_massive.iv30_history`` -> daily IV30 -> ``recompute_iv``; done at >= 20 points,
   else retried after 30 min doubling to 24 h (``scr_store.mark_history``).
5. **Status** - ``scr_status`` (row 1) and ``state/screener_collector.json`` (the Hermes
   tray reads it) at the start and the end of every tick: the state, a plain-English
   ``detail`` (jobs running at once are joined with " · "), the active error's kind and
   next try, a non-pausing ``warn``, ``universe_done``, ``earnings_on`` and ``progress``
   (``PROGRESS_KEYS``: the walk's pages, the pass's weighted percent and ETA, stock days
   to go and their ETA, the IV history left / waiting / ETA).

Errors: ``config`` / ``auth`` -> everything paused, looked at again every 5 min - ``app/.env``
is re-read then (``TST_MASSIVE_API_KEY`` from the file replaces the one in the process: a
key added or corrected there is used without a restart; an unchanged rejected key is
tried again at most every 30 min); ``network`` -> everything paused 60 s doubling to 5
min; ``plan`` -> only the part that needs the endpoint (``universe``, ``chain``,
``history`` or ``stocks``) paused 5 min. The key is never logged or written anywhere.
"""
from __future__ import annotations

import collections
import concurrent.futures as cf
import datetime as _dt
import json
import logging
import math
import os
import re
import threading
import time
from pathlib import Path

from . import calendars, clock, massive, opt_massive, option_metrics, scr_store
from .massive import MassiveError

COLLECTOR_VERSION = "1.1"
SOURCE = "massive"

TICK_S = 15.0                    # one tick every 15 s
HEARTBEAT_EVERY_S = 15.0         # one-off runs heartbeat this often while they wait
CYCLE_MIN_DEFAULT, CYCLE_MIN_LO, CYCLE_MIN_HI = 30, 5, 240
WORKERS_DEFAULT, WORKERS_LO, WORKERS_HI = 8, 1, 32
MAX_RPS_DEFAULT, MAX_RPS_LO, MAX_RPS_HI = 40.0, 1.0, 200.0
MAX_DTE_DEFAULT, MAX_DTE_LO, MAX_DTE_HI = 1100, 30, 1500

RTH_START = _dt.time(9, 45)      # the first cycle pass of a session
RTH_END = _dt.time(16, 0)        # no cycle pass starts at or after this
EOD_AFTER = _dt.time(16, 20)     # the end-of-day pass
UNIVERSE_AT = _dt.time(7, 30)    # the daily universe refresh
UNIVERSE_MAX_AGE_H = 20.0        # at start: refresh when older than this
UNIVERSE_EXP_DAYS = 60           # the universe = underlyings with an expiry in the next 60 days
UNIVERSE_RETRY_S = 900.0         # a failed walk that cannot resume: again in 15 min
UNIVERSE_RESUME_S = 60.0         # a failed walk with a cursor: resumed after 60 s ...
UNIVERSE_RESUME_MAX_S = 2 * 3600.0   # ... while the cursor is under 2 h old (and the ET day the same)
UNIVERSE_SAVE_PAGES = 25         # a first-start walk files what it read after page 1, then every 25 pages ...
UNIVERSE_SAVE_S = 30.0           # ... or 30 s
STOCK_YEARS = 2                  # grouped daily bars kept for the last 2 years (inside the plan's window)
STOCK_DAYS_RELOAD_S = 6 * 3600.0  # the on-file days are re-read from the DB this often
EMPTY_DAY_RETRY_S = 6 * 3600.0   # a session Massive has no bars for: asked again in 6 h
PLAN_DAY_MARGIN_DAYS = 7         # a refused day this close to the plan's window edge is retired ...
PLAN_DAY_RETRY_S = 3600.0        # ... a newer refused day is asked again in 1 h
RACE_RETRY_S = 30.0              # a stock day that hit a write race: again in 30 s
STOCKS_RETRY_S = 600.0           # a failed stocks-lane job: again in 10 min
TECH_EVERY_S = 3600.0            # technicals while days are still being filed: at most hourly
IDENTITY_EVERY_S = 7 * 86400.0   # names / types / exchanges weekly
IDENTITY_REQUESTS = 15           # ~ the reference-list pages an identity read takes (for its ETA)
EARNINGS_DAYS = 70               # earnings dates for the next 70 days ...
EARNINGS_RETRY_S = 3600.0        # ... re-read daily; a failed read again in 1 h
FILL_MIN_BARS = 20               # a universe symbol under this many bars gets its own read ...
FILL_RETRY_S = 24 * 3600.0       # ... at most once a day
FILL_EMPTY_RETRY_S = 7 * 86400.0  # a symbol Massive has no stock bars for: once a week
FILL_BATCH = 50                  # symbols queued for a bar fill at a time
SPLIT_LO, SPLIT_HI = 0.6, 1.6    # a day-over-day close ratio outside this re-reads the bars
STOCK_REQ_S = 60.5               # one stock request takes this many seconds / (requests a minute)
HISTORY_SESSIONS = 260           # the IV history covers the last 260 sessions
HISTORY_MIN_POINTS = scr_store.HISTORY_MIN_POINTS
HISTORY_PROGRESS_EVERY_S = 60.0  # the history left / waiting counts are re-read this often
HISTORY_RATE_MIN_S = 1800.0      # the history ETA: completions over at least 30 min ...
HISTORY_RATE_MAX_S = 3600.0      # ... and at most the last 60 min
PLAN_WIDE_403 = 25               # a chain 403 is plan-wide once this many symbols refused and none worked
REQUEUE_MAX = 2                  # a symbol requeued twice by a pause counts as failed the third time
REFUSED_KEEP_S = 24 * 3600.0     # a symbol whose chain was refused is left out of passes for 24 h
EMPTY_ABORT_N = 20               # a pass stops early once this many failed and none worked
EMPTY_RETRY_S = 600.0            # a pass that read no data: passes paused 10 min ...
EMPTY_RETRY_MAX_S = 7200.0       # ... doubling to 2 h
RETRY_ROUND_S = 300.0            # a pass gone bad: its failed reads are tried once more 5 min later
EMPTY_GUARD_SESSIONS = 2         # an empty answer keeps a symbol's stored rows while they are this recent
PASS_MAX_AGE_S = 12 * 3600.0     # any pass older than this closes unfinished
PASS_ETA_MIN = 0.02              # a pass's ETA is shown once 2 % of its contracts are read
KEY_RETRY_S = 300.0              # no key / a rejected key: looked at again every 5 min
KEY_SAME_RETRY_S = 1800.0        # a rejected key still unchanged in app/.env: tried again every 30 min
PLAN_RETRY_S = 300.0             # an endpoint the plan lacks: tried again every 5 min
NETWORK_RETRY_S = 60.0           # Massive unreachable: 60 s ...
NETWORK_RETRY_MAX_S = 300.0      # ... doubling to 5 min
STATE_LOG_EVERY_S = 1800.0       # a persisting error is logged when it begins, then every 30 min
FAIL_LOG_PER_PASS = 20           # per-symbol failures logged at WARNING per pass (then a summary)
DETAIL_JOIN = " · "              # jobs running at once in the detail

NO_KEY_TEXT = "%s is not set on this PC" % massive.ENV_KEY
UNIVERSE_EMPTY_TEXT = "Massive returned an empty options list (/v3/reference/options/contracts)"
NO_UNIVERSE_SUFFIX = " - nothing can be screened until the list of optionable stocks is read"
WAITING_FOR_LIST = "Waiting for the list of optionable stocks."
ALL = "all"                       # an error scope: every Massive request
OPS = ("universe", "chain", "history", "stocks")
_OP_WORDS = {"universe": "the universe refresh", "chain": "market passes",
             "history": "IV-history reads", "stocks": "stock bars and reference reads"}
_FATAL = ("config", "auth", "network")    # MassiveError kinds that pause everything
_PAUSING = _FATAL + ("plan",)
_RETRYABLE = ("http", "rate", "error")    # a pass gone bad tries these once more
_BG_OP = {"universe": "universe", "identity": "stocks", "stocks": "stocks", "fill": "stocks"}
PROGRESS_KEYS = ("universe_pages", "universe_symbols", "universe_started", "pass_kind", "pass_session",
                 "pass_pct", "pass_eta_s", "pass_paused_at", "stock_days_pending", "stock_eta_s",
                 "history_left", "history_waiting", "history_eta_s")

STATE_PATH = Path(__file__).resolve().parents[2] / "state" / "screener_collector.json"
APP_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"

classify_trend = scr_store.classify_trend      # the MATP rule (ported in scr_store)


# ────────────────────────────────── settings ──────────────────────────────────

def _env_num(name: str, default, lo, hi, cast=int):
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        v = cast(raw)
    except ValueError:
        return default
    return v if lo <= v <= hi else default


def env_cycle_min() -> int:
    """``TST_SCREENER_CYCLE_MIN``: minutes between cycle passes, 5-240, else 30."""
    return _env_num("TST_SCREENER_CYCLE_MIN", CYCLE_MIN_DEFAULT, CYCLE_MIN_LO, CYCLE_MIN_HI)


def env_workers() -> int:
    """``TST_SCREENER_WORKERS``: reader threads (and the client's concurrency), 1-32, else 8."""
    return _env_num("TST_SCREENER_WORKERS", WORKERS_DEFAULT, WORKERS_LO, WORKERS_HI)


def env_max_rps() -> float:
    """``TST_SCREENER_MAX_RPS``: Massive requests a second at most, 1-200, else 40."""
    return _env_num("TST_SCREENER_MAX_RPS", MAX_RPS_DEFAULT, MAX_RPS_LO, MAX_RPS_HI, cast=float)


def env_max_dte() -> int:
    """``TST_SCREENER_MAX_DTE``: the farthest expiry read, in days, 30-1500, else 1100."""
    return _env_num("TST_SCREENER_MAX_DTE", MAX_DTE_DEFAULT, MAX_DTE_LO, MAX_DTE_HI)


def _load_env() -> None:
    """Re-read ``app/.env``: every variable not already set is loaded, and the Massive key
    is taken FROM THE FILE whatever the process holds - a key added or corrected there is
    picked up without a restart; a blank ``TST_MASSIVE_API_KEY=`` removes it. A missing
    file (or python-dotenv) leaves the environment as it is."""
    try:
        from dotenv import dotenv_values, load_dotenv  # noqa: PLC0415

        load_dotenv(APP_ENV_PATH, override=False)
        if not Path(APP_ENV_PATH).is_file():
            return
        vals = dotenv_values(APP_ENV_PATH)
        if massive.ENV_KEY in vals:
            v = str(vals.get(massive.ENV_KEY) or "").strip()
            if v:
                os.environ[massive.ENV_KEY] = v
            else:
                os.environ.pop(massive.ENV_KEY, None)
    except Exception:  # noqa: BLE001 - python-dotenv or the file unreadable: the environment as is
        pass


def default_client(workers: int | None = None, max_rps: float | None = None):
    """ONE ``massive.Client`` for the whole collector (``concurrency`` = the worker threads,
    ``max_rps`` the request cap), the key from the environment; ``app/.env`` is re-read
    first (``_load_env``) so a key added or corrected there is used at the next 5-minute
    look."""
    _load_env()
    w = int(workers or env_workers())
    return massive.Client(max_rps=float(max_rps or env_max_rps()), concurrency=w)


# ────────────────────────────────── pure helpers ──────────────────────────────────

def sec_type_of(code, market: str = "stocks") -> str:
    """Massive's ticker type -> the screener's security type: CS -> stock; ETF / ETN / ETV
    / ETS -> etf; an index (or the indices market) -> index; anything else (ADRC, PFD,
    WARRANT, UNIT, ...) -> other."""
    if str(market or "").lower() == "indices":
        return "index"
    c = str(code or "").strip().upper()
    if c == "CS":
        return "stock"
    if c in ("ETF", "ETN", "ETV", "ETS"):
        return "etf"
    if c == "INDEX":
        return "index"
    return "other"


def exchange_of(mic, market: str = "stocks") -> str:
    """A primary-exchange MIC -> the screener's exchange: XNYS -> NYSE; XNAS -> NASDAQ;
    XASE / ARCX / BATS -> AMEX; an index -> INDEX; anything else -> OTHER."""
    if str(market or "").lower() == "indices":
        return "INDEX"
    return {"XNYS": "NYSE", "XNAS": "NASDAQ", "XASE": "AMEX", "ARCX": "AMEX",
            "BATS": "AMEX"}.get(str(mic or "").strip().upper(), "OTHER")


def _to_date(x) -> _dt.date | None:
    if isinstance(x, _dt.datetime):
        return x.date()
    if isinstance(x, _dt.date):
        return x
    try:
        return _dt.date.fromisoformat(str(x or "")[:10])
    except ValueError:
        return None


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def contract_rows(std_rows, spot, *, day, session) -> tuple[list[dict], dict]:
    """The rows a pass keeps from one chain's standard contracts, and the underlying's
    option-wide figures. Pure.

    Kept (§3): open interest > 0 or volume > 0 in the contract's latest day bar; expiry on
    or after ``day`` (the ET date of the read). Per row: ``weekly`` (not the standard
    monthly - ``opt_massive.is_monthly`` against the chain's expiries), ``price`` = the
    Black-Scholes price from its own IV (``opt_massive.model_price``) when the spot is
    known, else the last trade; ``last`` = the day bar close; ``volume`` / ``chg_pct`` =
    the bar's when the bar is from ``session``, else 0 / None (Massive's ``day`` is the
    contract's most recent bar, which for a contract not traded yet in ``session`` is an
    older one); ``last_trade`` = the bar's time.

    Figures: ``iv30`` (PERCENT, ``option_metrics.atm_iv_by_expiry`` without the quote
    condition + ``iv30_constant_maturity``), ``exp_move30`` (= iv30 x sqrt(30/365)),
    ``call_vol`` / ``put_vol`` / ``call_oi`` / ``put_oi``, ``n_contracts``."""
    d0 = _to_date(day)
    ses = str(session)[:10]
    listed = {d for d in (_to_date(r.get("expiry")) for r in std_rows or ()) if d is not None}
    s = _num(spot) if (_num(spot) or 0) > 0 else None
    out: list[dict] = []
    for r in std_rows or ():
        d = _to_date(r.get("expiry"))
        right = str(r.get("right") or "").upper()[:1]
        if d is None or d < d0 or right not in ("C", "P"):
            continue
        vol, oi = r.get("volume"), r.get("oi")
        if not ((oi or 0) > 0 or (vol or 0) > 0):
            continue
        t = r.get("last_updated")
        bar_ses = clock.et_date(t).isoformat() if isinstance(t, _dt.datetime) else None
        fresh = bar_ses is None or bar_ses >= ses
        last = r.get("day_close")
        price = opt_massive.model_price(s, r.get("strike"), (d - d0).days, r.get("iv"), right) if s else None
        out.append({
            "expiry": d.isoformat(), "right": right, "strike": r.get("strike"),
            "weekly": not opt_massive.is_monthly(d, listed),
            "price": price if price is not None else last, "last": last,
            "chg_pct": r.get("day_change_pct") if fresh else None,
            "volume": (vol or 0) if fresh else 0, "oi": oi,
            "iv": r.get("iv"), "delta": r.get("delta"), "gamma": r.get("gamma"),
            "theta": r.get("theta"), "vega": r.get("vega"), "last_trade": t,
        })
    fig = {"iv30": None, "exp_move30": None, "n_contracts": len(out),
           "call_vol": sum(int(x["volume"] or 0) for x in out if x["right"] == "C"),
           "put_vol": sum(int(x["volume"] or 0) for x in out if x["right"] == "P"),
           "call_oi": sum(int(x["oi"] or 0) for x in out if x["right"] == "C"),
           "put_oi": sum(int(x["oi"] or 0) for x in out if x["right"] == "P")}
    if s:
        day_s = d0.isoformat()
        by_exp = option_metrics.atm_iv_by_expiry(
            [{"expiry": x["expiry"], "strike": x["strike"], "iv": x["iv"],
              "dte": (_to_date(x["expiry"]) - d0).days} for x in out],
            s, day_s, require_quote=False)
        v = option_metrics.iv30_constant_maturity(by_exp, day_s)
        if v is not None and v > 0:
            fig["iv30"] = round(v, 4)
            fig["exp_move30"] = round(v * math.sqrt(30.0 / 365.0), 4)
    return out, fig


def snapshot_session(now: _dt.datetime) -> str:
    """The session a chain read at ``now`` describes: today once the session has opened
    on a trading day; before the open the previous trading day; on a weekend / holiday the
    last trading day."""
    et = clock.et_now(now)
    d = et.date()
    if clock.is_trading_day(d):
        return (d if et.time() >= clock.SESSION_OPEN else clock.prev_trading_day(d)).isoformat()
    return clock.last_trading_day(d).isoformat()


def eod_day_for(now: _dt.datetime) -> str | None:
    """The trading day whose end-of-day pass may run at ``now``: today after 16:20 ET,
    the previous trading day before today's open (a missed evening caught up), the last
    trading day on a weekend / holiday; None from the open to 16:20 ET."""
    et = clock.et_now(now)
    d, t = et.date(), et.time()
    if clock.is_trading_day(d):
        if t >= EOD_AFTER:
            return d.isoformat()
        if t < clock.SESSION_OPEN:
            return clock.prev_trading_day(d).isoformat()
        return None
    return clock.last_trading_day(d).isoformat()


def plan_start(now: _dt.datetime) -> _dt.date:
    """The oldest session worth asking Stocks Basic for at ``now``: the ET date minus
    ``massive.STOCKS_PLAN_DAYS`` (725) - the plan keeps about two years; an older day is
    refused (HTTP 403)."""
    return massive.stocks_plan_start(clock.et_date(now))


def stock_days(now: _dt.datetime, years: float | None = None) -> list[str]:
    """The trading days of the last ``years`` years (default ``STOCK_YEARS``) up to the
    last published session (``opt_massive.published_session``), never before
    ``plan_start(now)``, oldest first."""
    years = STOCK_YEARS if years is None else years
    end = opt_massive.published_session(clock.et_date(now), now)
    start = max(end - _dt.timedelta(days=int(years * 365.25) + 1), plan_start(now))
    out, d = [], start
    while d <= end:
        if clock.is_trading_day(d):
            out.append(d.isoformat())
        d += _dt.timedelta(days=1)
    return out


def _iso(t) -> str | None:
    if t is None:
        return None
    if isinstance(t, _dt.datetime):
        if t.tzinfo is None:
            t = t.replace(tzinfo=_dt.timezone.utc)
        return t.isoformat()
    return str(t)


def _et_hm(t: _dt.datetime) -> str:
    return clock.et_now(t).strftime("%H:%M") + " ET"


def _day_words(d) -> str:
    """``Fri Oct 9`` (no leading zero, on any OS)."""
    d = _to_date(d)
    if d is None:
        return "?"
    return "%s %d" % (d.strftime("%a %b"), d.day)


def _when_words(t_et: _dt.datetime) -> str:
    """``Mon Oct 12 09:45`` for an ET datetime."""
    return "%s %s" % (_day_words(t_et.date()), t_et.strftime("%H:%M"))


def _dur(seconds) -> str:
    """A duration in plain words: ``7 min``, ``2 h 05 min``, ``1 d 21 h``."""
    f = _num(seconds)
    if f is None:
        return "?"
    m = max(1, int(math.ceil(f / 60.0)))
    if m < 60:
        return "%d min" % m
    h, mm = divmod(m, 60)
    if h < 24:
        return ("%d h %02d min" % (h, mm)) if mm else "%d h" % h
    d, hh = divmod(h, 24)
    return ("%d d %d h" % (d, hh)) if hh else "%d d" % d


def _has_key(client) -> bool:
    return client is not None and bool(getattr(client, "has_key", True))


def _plural(n: int, word: str) -> str:
    return "%s %s%s" % (format(int(n), ","), word, "" if n == 1 else "s")


def _strip_sym(text: str, sym: str) -> str:
    """A failure text without its symbol, so equal reasons count together."""
    if not sym:
        return text
    return re.sub(r"(?<![A-Z0-9])%s(?![A-Z0-9])" % re.escape(sym), "<sym>", text)


def _is_integrity(exc) -> bool:
    """A unique-key write race (SQLAlchemy's IntegrityError), told by its class name - this
    module never imports the database layer."""
    return any(c.__name__ == "IntegrityError" for c in type(exc).__mro__)


class InlineExecutor:
    """A ``concurrent.futures`` executor that runs every job at once in the caller's
    thread - deterministic ticks for tests and by-hand debugging."""

    def submit(self, fn, *args, **kw):
        f: cf.Future = cf.Future()
        try:
            f.set_result(fn(*args, **kw))
        except BaseException as exc:  # noqa: BLE001
            f.set_exception(exc)
        return f

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:  # noqa: ARG002
        return None


def _thread_pool(n: int):
    return cf.ThreadPoolExecutor(max_workers=max(1, int(n)), thread_name_prefix="scr")


# ────────────────────────────────── a market pass ──────────────────────────────────

class _Pass:
    """One market pass in progress: the symbols still to read (``queue``), the jobs in
    flight, the counts, the halt flag the workers check when Massive pauses it, and the
    bookkeeping of a pass that grows with a streamed universe (``grow``), retries its bad
    reads once (``retry``), stops early (``aborted``) or is closed (``closing``)."""

    def __init__(self, pid: int, kind: str, session: str, day: _dt.date, symbols: list[str],
                 started: _dt.datetime, weights: dict | None = None):
        self.id, self.kind, self.session, self.day = pid, kind, session, day
        self.total = len(symbols)
        self.queue: list[str] = list(symbols)
        self.seen: set[str] = set(symbols)
        self.w: dict[str, int] = {s: int((weights or {}).get(s, 1) or 1) for s in symbols}
        self.total_w = sum(self.w.values())
        self.done_w = 0
        self.futures: dict[cf.Future, str] = {}
        self.ok = self.failed = self.contracts = self.pages = 0
        self.started = started
        self.t0 = time.monotonic()
        self.halt = threading.Event()
        self.lock = threading.Lock()
        self.logged_fails = 0
        self.grow = False                # started on a streamed list still being read
        self.complete_list = True        # the pass covers a complete universe
        self.one_off = False
        self.requeues: collections.Counter = collections.Counter()
        self.r403: set[str] = set()      # symbols whose chain answered 403
        self.reasons: collections.Counter = collections.Counter()   # (kind, text) of the failures
        self.retry: list[str] = []       # failed with http / rate / error
        self.retried = False
        self.retry_at: _dt.datetime | None = None
        self.aborted = False
        self.closing = False
        self.paused_at: _dt.datetime | None = None
        self.empty_syms: list[str] = []  # read fine, no contract kept

    @property
    def done(self) -> int:
        return self.ok + self.failed

    def add(self, symbols, weights: dict) -> int:
        new = [s for s in symbols if s not in self.seen]
        for s in new:
            self.seen.add(s)
            w = int(weights.get(s, 1) or 1)
            self.w[s] = w
            self.total_w += w
        self.queue.extend(new)
        self.total += len(new)
        return len(new)


# ────────────────────────────────── the collector ──────────────────────────────────

class Collector:
    """The §4 loop. ``tick()`` does one non-blocking step; ``run_forever()`` ticks every
    15 s; ``run_once`` / ``run_universe`` / ``run_eod`` / ``run_history`` are the CLI's
    one-off runs.

    Injectable (tests): ``session_factory()`` -> a session on the screener DB (default
    ``screener_db.SessionLocal``); ``client`` (a ``massive.Client`` or a fake with
    ``chain_snapshot``, ``option_daily``, ``stock_daily``, ``grouped_daily``,
    ``option_underlyings``, ``reference_tickers``, ``has_key``) or ``client_factory()``;
    ``clock()`` -> now (naive = UTC); ``sleep(s)``; ``executor_factory(n)`` -> an executor
    (default a thread pool; ``InlineExecutor`` runs jobs at once) and
    ``universe_executor_factory(n)`` for the universe thread (default the same);
    ``earnings_fetch(day)`` -> Nasdaq-shaped rows (default ``calendars.earnings_for``)."""

    def __init__(self, session_factory=None, *, client=None, client_factory=None, clock=None,
                 sleep=None, log=None, state_path=None, executor_factory=None, earnings_fetch=None,
                 tick_s: float = TICK_S, cycle_min=None, workers=None, max_rps=None, max_dte=None,
                 stock_years: float | None = None, universe_executor_factory=None):
        if session_factory is None:
            from .. import screener_db  # noqa: PLC0415

            session_factory = screener_db.SessionLocal
        self._sf = session_factory
        self.workers = int(workers or env_workers())
        self.max_rps = float(max_rps or env_max_rps())
        self.max_dte = int(max_dte or env_max_dte())
        self.cycle_min = int(cycle_min or env_cycle_min())
        self.stock_years = float(stock_years or STOCK_YEARS)
        self.client = client
        if client_factory is None and client is None:
            client_factory = lambda: default_client(self.workers, self.max_rps)  # noqa: E731
        self._client_factory = client_factory
        self._clock = clock
        self._sleep = sleep or time.sleep
        self.log = log or logging.getLogger(__name__)
        self.state_path = Path(state_path) if state_path else STATE_PATH
        self._executor_factory = executor_factory or _thread_pool
        self._uni_executor_factory = universe_executor_factory or self._executor_factory
        self._earnings_fetch = earnings_fetch or calendars.earnings_for
        self.tick_s = float(tick_s)
        self.version = COLLECTOR_VERSION
        self.pid = os.getpid()
        # status
        self.state = "starting"
        self.detail = "starting"
        self.api_ok: bool | None = None
        self.last_error: str | None = None
        self.last_failure: str | None = None           # a per-symbol failure while an alert is shown
        self.universe_n = 0
        self.universe_on: str | None = None
        self.history_done_n = 0
        self.history_total = 0
        self.last_pass: dict | None = None            # the newest FINISHED pass that stored contracts
        self._warn: str | None = None                  # a non-pausing warning (a failed day-2 universe)
        # errors that pause work: scope (ALL | an op) -> (kind, reason) / next try
        self._alerts: dict[str, tuple[str, str]] = {}
        self._retry_at: dict[str, _dt.datetime] = {}
        self._net_fails = 0
        self._state_log: tuple | None = None
        self._auth_tried_at: _dt.datetime | None = None
        self._retired: list = []                       # clients replaced while jobs may hold them
        # work
        self._pool = None
        self._bgpool = None
        self._unipool = None
        self._earnpool = None
        self._pass: _Pass | None = None
        self._last_cycle: _dt.datetime | None = None
        self._last_cycle_session: str | None = None
        self._cycle_retry = False
        self._last_eod: str | None = None
        self._partial_eod: str | None = None          # an EOD session read on a partial list
        self._empty_fails = 0
        self._chain_ok_seen = False                    # a chain read worked (this client)
        self._hist_ok_seen = False                     # an IV-history read worked (this client)
        self._stock_ok_seen = False                    # a grouped stock day came back (this client)
        self._refused: dict[str, _dt.datetime] = {}    # symbol -> when its chain was refused (403)
        self._spelling: dict[str, str] = {}            # symbol -> the spelling its chain answers to
        self._bg: tuple[str, cf.Future] | None = None
        self._bg_day: str | None = None
        self._bg_fails: dict[str, int] = {}
        self._hist: dict[cf.Future, str] = {}
        self._hist_gave_up: set[str] = set()
        self._hist_left: int | None = None
        self._hist_waiting: int | None = None
        self._hist_prog_at: _dt.datetime | None = None
        self._hist_samples: collections.deque = collections.deque()
        # the universe
        self._uni_future: cf.Future | None = None
        self._uni_stream = False
        self._uni_progress: dict | None = None
        self._uni_resume: dict | None = None
        self._uni_counts: dict[str, int] = {}
        self._pass_w: dict[str, int] = {}              # symbol -> contracts in the STORED universe (pass weights)
        self._universe_done: _dt.datetime | None = None
        self._universe_retry_at: _dt.datetime | None = None
        self._universe_checked = False
        self._ident_records: list[dict] | None = None
        self._ident_pending: set[str] = set()
        self._index_syms: set[str] = set()
        self._identity_at: _dt.datetime | None = None
        self._identity_retry_at: _dt.datetime | None = None
        self._earn_future: cf.Future | None = None
        self._earnings_on: str | None = None
        self._earnings_retry_at: _dt.datetime | None = None
        self._days: dict[str, list[bool]] = {}
        self._days_loaded_at: _dt.datetime | None = None
        self._day_retry: dict[str, _dt.datetime] = {}
        self._refused_days: set[str] = set()
        self._stocks_retry_at: _dt.datetime | None = None
        self._tech_dirty = True
        self._tech_at: _dt.datetime | None = None
        self._tech_retry_at: _dt.datetime | None = None
        self._fill: dict[str, str] = {}                 # symbol -> "new" | "split"
        self._fill_tried: dict[str, _dt.datetime] = {}
        self._restored = False
        self._last_beat: _dt.datetime | None = None
        self._db_warned = False
        self._file_warned = False
        self._stopping = threading.Event()

    # ── clock + text ──

    def _now(self) -> _dt.datetime:
        t = self._clock() if self._clock is not None else _dt.datetime.now(_dt.timezone.utc)
        if t.tzinfo is not None:
            t = t.astimezone(_dt.timezone.utc).replace(tzinfo=None)
        return t

    def _scrub(self, text) -> str:
        """Text safe for a log line or the status: the key masked, capped at 500 chars."""
        s = str(text or "")
        keys = [massive.api_key(), getattr(self.client, "_key", None)]
        keys += [getattr(c, "_key", None) for c in self._retired]
        for key in keys:
            if isinstance(key, str) and len(key) >= 4:
                s = s.replace(key, "***")
        return massive._scrub(s)[:500]   # noqa: SLF001 - the client's own masking rules

    def _err_text(self, exc) -> str:
        return self._scrub(str(exc) or type(exc).__name__)

    def _rollback(self, db) -> None:
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass

    # ── executors ──

    def _workers_pool(self):
        if self._pool is None:
            self._pool = self._executor_factory(self.workers)
        return self._pool

    def _lane(self):
        if self._bgpool is None:
            self._bgpool = self._executor_factory(1)
        return self._bgpool

    def _uni_lane(self):
        if self._unipool is None:
            self._unipool = self._uni_executor_factory(1)
        return self._unipool

    def _earn_lane(self):
        if self._earnpool is None:
            self._earnpool = self._executor_factory(1)
        return self._earnpool

    # ── errors that pause work ──

    def _alert_scope(self) -> str | None:
        if ALL in self._alerts:
            return ALL
        for op in OPS:
            if op in self._alerts:
                return op
        return None

    def error_kind(self) -> str | None:
        scope = self._alert_scope()
        return self._alerts[scope][0] if scope else None

    def _log_state(self, key: tuple, level: int, msg: str, *args) -> None:
        now = self._now()
        last = self._state_log
        if last is not None and last[0] == key and (now - last[1]).total_seconds() < STATE_LOG_EVERY_S:
            return
        self._state_log = (key, now)
        self.log.log(level, msg, *args)

    def _alert(self, scope: str, kind: str, reason: str, wait_s: float, now: _dt.datetime) -> None:
        self._alerts[scope] = (kind, reason)
        self._retry_at[scope] = now + _dt.timedelta(seconds=wait_s)
        self.last_error = reason[:500]
        what = "every Massive request" if scope == ALL else _OP_WORDS[scope]
        self._log_state(("alert", scope, kind, reason), logging.WARNING,
                        "Massive %s error - %s paused: %s (next try %s)", kind, what, reason,
                        _et_hm(self._retry_at[scope]))

    def _clear(self, scope: str) -> None:
        """Drop the alert of ``scope``. ``last_error`` then names the alert still active, or
        is cleared when it was this alert's reason."""
        got = self._alerts.pop(scope, None)
        self._retry_at.pop(scope, None)
        if got is None:
            return
        self._state_log = None
        self.log.info("Massive works again (%s error cleared: %s)", got[0], got[1])
        active = self._alert_scope()
        if active is not None:
            self.last_error = self._alerts[active][1][:500]
        elif self.last_error == got[1][:500]:
            self.last_error = None

    def _allowed(self, op: str, now: _dt.datetime | None = None) -> bool:
        now = now or self._now()
        for scope in (ALL, op):
            until = self._retry_at.get(scope)
            if until is not None and now < until:
                return False
        return True

    def _success(self, op: str) -> None:
        """A request of ``op`` worked: Massive is reachable and the plan covers it. A
        ``chain`` alert of kind ``empty`` (a pass that read nothing) stays until a pass
        stores contracts (``_finish_pass``)."""
        self.api_ok = True
        self._net_fails = 0
        for scope in (ALL, op):
            got = self._alerts.get(scope)
            if got is None or (scope == "chain" and got[0] == "empty"):
                continue
            self._clear(scope)

    def _massive_failure(self, op: str, kind: str, msg: str) -> None:
        """A Massive error that pauses work: ``config`` / ``auth`` / ``network`` ->
        everything, ``plan`` -> ``op``."""
        now = self._now()
        if kind in ("config", "auth"):
            self.api_ok = False
            self._alert(ALL, kind, msg, KEY_RETRY_S, now)
        elif kind == "network":
            self.api_ok = False
            until = self._retry_at.get(ALL)
            if until is not None and now < until and self._alerts.get(ALL, ("",))[0] == "network":
                return                     # jobs in flight failing in the same outage: one back-off step
            self._net_fails += 1
            wait = min(NETWORK_RETRY_S * 2 ** (self._net_fails - 1), NETWORK_RETRY_MAX_S)
            self._alert(ALL, "network", msg, wait, now)
        elif kind == "plan":
            self._alert(op, "plan", msg, PLAN_RETRY_S, now)

    def _note_failure(self, line: str) -> None:
        """A failure that pauses nothing: ``last_error`` - unless an alert is shown, whose
        reason stays there (the failure goes to ``last_failure``)."""
        if self._alert_scope() is not None:
            self.last_failure = line[:500]
        else:
            self.last_error = line[:500]

    def _ready(self, now: _dt.datetime, *, force: bool = False) -> bool:
        """A client with a key, and Massive not paused (``force``: look now). No key ->
        state error ``NO_KEY_TEXT``, looked at again in 5 min. A rejected key: ``app/.env``
        is re-read at each look - a different key there gets a fresh client at once; the
        same key is tried again at most every 30 min."""
        until = self._retry_at.get(ALL)
        if not force and until is not None and now < until:
            return False
        c = self.client
        if self._alerts.get(ALL, ("",))[0] == "auth" and self._client_factory is not None and _has_key(c):
            _load_env()
            if massive.api_key() != getattr(c, "_key", None):
                if not self._swap_client(now, "the Massive key in app/.env changed - a new client uses it"):
                    return False
                c = self.client
            elif not force and self._auth_tried_at is not None and \
                    (now - self._auth_tried_at).total_seconds() < KEY_SAME_RETRY_S:
                self._retry_at[ALL] = now + _dt.timedelta(seconds=KEY_RETRY_S)
                return False
            else:
                self._auth_tried_at = now
        if not _has_key(c) and self._client_factory is not None:
            try:
                fresh = self._client_factory()
            except Exception as exc:  # noqa: BLE001
                self._alert(ALL, "config", "could not set up the Massive client: %s"
                            % self._err_text(exc), KEY_RETRY_S, now)
                return False
            if c is not None and fresh is not c:
                self._close_client(c)          # it had no key: nothing in flight on it
            self.client = c = fresh
            self._seen_reset()
        if not _has_key(c):
            self._alert(ALL, "config", NO_KEY_TEXT, KEY_RETRY_S, now)
            return False
        if self._alerts.get(ALL, ("",))[0] == "config":
            self._clear(ALL)
        return True

    def _seen_reset(self) -> None:
        self._chain_ok_seen = self._hist_ok_seen = self._stock_ok_seen = False
        self._auth_tried_at = None

    def _swap_client(self, now: _dt.datetime, why: str) -> bool:
        """A fresh client from the factory; the old one is retired (closed once no job can
        hold it)."""
        try:
            fresh = self._client_factory()
        except Exception as exc:  # noqa: BLE001
            self._alert(ALL, "config", "could not set up the Massive client: %s" % self._err_text(exc),
                        KEY_RETRY_S, now)
            return False
        old = self.client
        if old is not None and fresh is not old:
            self._retired.append(old)
        self.client = fresh
        self._seen_reset()
        self.log.info(why)
        return True

    def _close_client(self, c) -> None:
        close = getattr(c, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass

    def _busy(self) -> bool:
        return bool((self._pass is not None and self._pass.futures) or self._bg is not None
                    or self._uni_future is not None or self._earn_future is not None or self._hist)

    def _close_retired(self, *, force: bool = False) -> None:
        if not self._retired or (self._busy() and not force):
            return
        for c in self._retired:
            self._close_client(c)
        self._retired = []

    # ── status ──

    def _shown(self) -> tuple[str, str]:
        scope = self._alert_scope()
        if scope is None or self.state == "stopped":
            return self.state, self.detail
        kind, reason = self._alerts[scope]
        text = reason
        if scope == "universe" and self._universe_done is None and not self.universe_n:
            text += NO_UNIVERSE_SUFFIX          # T-09: only while no stock at all is filed
        elif scope != ALL:
            text += " - %s paused, the rest carries on" % _OP_WORDS[scope]
        nxt = self._retry_at.get(scope)
        if nxt is not None and nxt > self._now():
            text += "; next try %s" % _et_hm(nxt)
        return "error", text

    @property
    def shown_state(self) -> str:
        return self._shown()[0]

    def _pending_days(self) -> int:
        return sum(1 for v in self._days.values() if not (v[0] and v[1]))

    def _next_try(self, now: _dt.datetime) -> _dt.datetime | None:
        """When the paused / failed work is tried again: the active alert's retry, else the
        nearest retry of a failed universe / identity / stocks-lane job."""
        scope = self._alert_scope()
        if scope is not None:
            t = self._retry_at.get(scope)
            return t if (t is not None and t > now) else None
        cands = [t for t in (self._universe_retry_at, self._identity_retry_at, self._stocks_retry_at)
                 if t is not None and t > now]
        return min(cands) if cands else None

    def _spm(self) -> int:
        """Stock requests a minute the client allows (Stocks Basic: 5)."""
        c = self.client
        v = getattr(c, "stocks_per_min", None) or getattr(getattr(c, "_stocks", None), "n", None)
        try:
            v = int(v)
        except (TypeError, ValueError):
            v = 0
        return v if v > 0 else 5

    def _stock_eta_s(self) -> float:
        """At least this long for the stock days still missing and the fills queued."""
        adj = sum(1 for v in self._days.values() if not v[0])
        raw = sum(1 for v in self._days.values() if not v[1])
        return (adj + raw + 2 * len(self._fill)) * STOCK_REQ_S / self._spm()

    def _weight(self, sym: str) -> int:
        """A symbol's share of a pass's percent / ETA: its contract count in the STORED
        universe (``_pass_w``, re-read every tick - so it survives a restart), else the
        running walk's own count, else 1."""
        for src in (self._pass_w, self._uni_counts):
            try:
                v = int(src.get(sym) or 0)
            except (TypeError, ValueError):
                v = 0
            if v > 0:
                return v
        return 1

    @staticmethod
    def _pass_pct(p: _Pass) -> float:
        return min(1.0, p.done_w / p.total_w) if p.total_w else 0.0

    @staticmethod
    def _pass_eta_s(p: _Pass, now: _dt.datetime, pct: float) -> float | None:
        if pct < PASS_ETA_MIN or pct >= 1.0:
            return None
        elapsed = max(0.0, (now - p.started).total_seconds())
        return elapsed * (1.0 - pct) / pct

    def _history_eta_s(self, now: _dt.datetime) -> float | None:
        left = (self.history_total or 0) - (self.history_done_n or 0)
        if left <= 0 or not self._hist_samples:
            return None
        t0, n0 = self._hist_samples[0]
        span = (now - t0).total_seconds()
        if span < HISTORY_RATE_MIN_S:
            return None
        rate = (self.history_done_n - n0) / span
        return left / rate if rate > 0 else None

    def _progress(self, now: _dt.datetime) -> dict:
        """The ``progress`` the page and the tray show (``PROGRESS_KEYS``; None = unknown)."""
        out = dict.fromkeys(PROGRESS_KEYS)
        pr = self._uni_progress if self._uni_future is not None else None
        rs = self._uni_resume
        if pr:
            out.update(universe_pages=int(pr.get("pages") or 0), universe_symbols=int(pr.get("symbols") or 0),
                       universe_started=_iso(pr.get("started")))
        elif rs:
            out.update(universe_pages=int(rs.get("pages") or 0), universe_symbols=len(rs.get("counts") or {}),
                       universe_started=_iso(rs.get("started")))
        p = self._pass
        if p is not None:
            pct = self._pass_pct(p)
            eta = self._pass_eta_s(p, now, pct)
            out.update(pass_kind=p.kind, pass_session=p.session, pass_pct=round(pct * 100.0, 1),
                       pass_eta_s=int(eta) if eta is not None else None, pass_paused_at=_iso(p.paused_at))
        if self._days:
            pend = self._pending_days()
            out["stock_days_pending"] = pend
            out["stock_eta_s"] = int(self._stock_eta_s()) if (pend or self._fill) else 0
        if self.history_total:
            out["history_left"] = self._hist_left
            out["history_waiting"] = self._hist_waiting
            eta = self._history_eta_s(now)
            out["history_eta_s"] = int(eta) if eta is not None else None
        return out

    def _fields(self) -> dict:
        """The ``scr_status`` columns (the state file carries them too)."""
        now = self._now()
        state, detail = self._shown()
        p = self._pass
        if p is not None:
            pass_id, total, done = p.id, p.total, p.done
        else:
            lp = self.last_pass or {}
            pass_id, total, done = lp.get("id"), lp.get("n_symbols"), (lp.get("n_ok") or 0) + (lp.get("n_failed") or 0)
        return {"state": state, "detail": detail, "pid": self.pid, "version": self.version,
                "pass_id": pass_id, "symbols_total": total, "symbols_done": done,
                "universe_n": self.universe_n, "universe_on": self.universe_on,
                "history_done_n": self.history_done_n, "history_total": self.history_total,
                "last_error": self.last_error, "api_ok": self.api_ok,
                "error_kind": self.error_kind(), "next_try": self._next_try(now), "warn": self._warn,
                "universe_done": self._universe_done, "earnings_on": self._earnings_on,
                "progress": self._progress(now)}

    def _beat(self, db, *, force: bool = True) -> None:
        now = self._now()
        if not force and self._last_beat is not None and \
                (now - self._last_beat).total_seconds() < HEARTBEAT_EVERY_S:
            return
        self._last_beat = now
        fields = self._fields()
        if db is not None:
            try:
                scr_store.set_status(db, heartbeat=now, **fields)
                self._db_warned = False
            except Exception as exc:  # noqa: BLE001 - the file still carries the heartbeat
                self._rollback(db)
                if not self._db_warned:
                    self.log.warning("could not write the screener status row: %s", exc)
                    self._db_warned = True
        self._write_state(fields, now)

    def _write_state(self, fields: dict, now: _dt.datetime) -> None:
        """``state/screener_collector.json`` for the Hermes tray, replaced atomically (a
        ``.gitignore`` is dropped into a fresh ``state`` folder)."""
        doc = dict(fields)
        doc["next_try"] = _iso(fields.get("next_try"))
        doc["universe_done"] = _iso(fields.get("universe_done"))
        p = self._pass
        lp = self.last_pass or {}
        doc.update(
            heartbeat=_iso(now), source=SOURCE,
            pass_kind=p.kind if p is not None else None,
            pass_session=p.session if p is not None else None,
            symbols_failed=p.failed if p is not None else None,
            last_pass_id=lp.get("id"), last_pass_kind=lp.get("kind"),
            last_pass_session=lp.get("session"), last_pass_finished=_iso(lp.get("finished")),
            last_pass_et=_et_hm(lp["finished"]) if lp.get("finished") else None,
            last_pass_contracts=lp.get("n_contracts"), last_eod_session=self._last_eod,
            stock_days_pending=self._pending_days() if self._days else None,
            cycle_min=self.cycle_min, workers=self.workers,
            written_by="dashboard_tst/app/services/scr_collector.py")
        path = self.state_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.parent.name == "state":
                gi = path.parent / ".gitignore"
                if not gi.exists():
                    gi.write_text("# runtime state (collector heartbeats) - never committed\n*\n",
                                  encoding="utf-8")
            text = json.dumps(doc, indent=1, sort_keys=True, default=str)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            try:
                os.replace(tmp, path)
            except PermissionError:          # the tray had it open this instant (Windows)
                path.write_text(text, encoding="utf-8")
                try:
                    tmp.unlink()
                except OSError:
                    pass
            self._file_warned = False
        except Exception as exc:  # noqa: BLE001
            if not self._file_warned:
                self.log.warning("could not write %s: %s", path, exc)
                self._file_warned = True

    def _restore(self, db) -> None:
        """What survives a restart: the last cycle pass's start, the last EOD session that
        stored contracts (a finished pass that read nothing, or mostly failed, is not
        "done"; one read on a PARTIAL list of optionable stocks - ``scr_pass.partial`` - is
        not the session's full pass: the full one follows the complete list), the last
        pass that stored contracts, the universe's completion stamp and the day of the last
        earnings read. Tried again at the next tick when the reads fail."""
        if self._restored:
            return
        try:
            cyc = scr_store.last_pass(db, kind="cycle", finished=None)
            eod = scr_store.last_pass(db, kind="eod", min_contracts=1)
            full = eod
            if eod and eod.get("partial"):        # read on a partial list: not the session's full pass
                full = scr_store.last_pass(db, kind="eod", min_contracts=1, partial=False)
            last = scr_store.last_pass(db, min_contracts=1)
            st = scr_store.status(db) or {}
        except Exception as exc:  # noqa: BLE001 - e.g. the tables missing before a migration
            self._rollback(db)
            self._log_state(("restore", type(exc).__name__), logging.WARNING,
                            "could not read the screener's last passes: %s", self._err_text(exc))
            return
        self._restored = True
        if cyc:
            self._last_cycle, self._last_cycle_session = cyc.get("started"), cyc.get("session")
        if eod and eod.get("partial"):
            # the full-list EOD pass of that session follows the complete list (_pass_due)
            self._partial_eod = eod.get("session")
        if full:
            n_ok, n_failed = int(full.get("n_ok") or 0), int(full.get("n_failed") or 0)
            if n_failed * 2 <= n_ok + n_failed:
                self._last_eod = full.get("session")
        self.last_pass = last
        self.universe_on = st.get("universe_on") or None
        ud = st.get("universe_done")
        if isinstance(ud, _dt.datetime):
            self._universe_done = ud if ud.tzinfo is None else ud.astimezone(_dt.timezone.utc).replace(tzinfo=None)
        eo = st.get("earnings_on")
        if eo:
            self._earnings_on = str(eo)[:10]

    def _stored_symbols(self, db, now: _dt.datetime) -> list[str]:
        """The pass symbols in order; their stored contract counts become the pass weights
        (``_pass_w``)."""
        pairs = scr_store.pass_symbols_weighted(db, now=now)
        self._pass_w = {s: n for s, n in pairs}
        return [s for s, _ in pairs]

    def _refresh_counts(self, db, now: _dt.datetime) -> list[str]:
        syms = self._stored_symbols(db, now)
        self.universe_n = len(syms)
        self.history_done_n, self.history_total = scr_store.history_counts(db, syms)
        self._hist_progress(db, now, syms)
        return syms

    def _hist_progress(self, db, now: _dt.datetime, syms: list[str]) -> None:
        """The IV history's left / waiting counts and the completion samples of its ETA,
        once a minute."""
        if self._hist_prog_at is not None and (now - self._hist_prog_at).total_seconds() < HISTORY_PROGRESS_EVERY_S:
            return
        self._hist_prog_at = now
        not_done = max(0, (self.history_total or 0) - (self.history_done_n or 0))
        if not_done:
            due = scr_store.history_queue(db, now=now, symbols=syms, limit=len(syms) + 1)
            self._hist_left = min(len(due), not_done)
        else:
            self._hist_left = 0
        self._hist_waiting = max(0, not_done - self._hist_left)
        s = self._hist_samples
        s.append((now, self.history_done_n))
        while s and (now - s[0][0]).total_seconds() > HISTORY_RATE_MAX_S:
            s.popleft()

    # ── one chain (a worker thread) ──

    def _snapshot(self, spelling: str, day: _dt.date) -> dict:
        return self.client.chain_snapshot(spelling, exp_gte=day, exp_lte=day + _dt.timedelta(days=self.max_dte))

    def read_chain(self, db, sym: str, *, kind: str, session: str, day: _dt.date,
                   pass_id: int | None = None) -> dict:
        """Read ``sym``'s whole chain and file it (§4.2). Raises ``MassiveError`` when the
        read fails - nothing is written then. An index read bare (``SPX``, how the contracts
        list names it) that comes back empty is read once as ``I:SPX`` (the spelling that
        answered is kept). An EMPTY answer - or standard contracts that keep nothing (no
        open interest, no volume) - while the symbol has stored rows from the last
        ``EMPTY_GUARD_SESSIONS`` sessions writes NOTHING (``empty`` True): good data is
        never wiped by a transient empty answer. A chain Massive lists with no standard
        contract at all (only adjusted series - a cash merger, a delisting) is final: the
        stored rows are replaced by none and the underlying counts 0 contracts.
        Returns ``{"symbol", "contracts", "pages", "spot", "spot_src", "iv30", "rows"}``."""
        t_read = self._now()
        spelling = self._spelling.get(sym, sym)
        snap = self._snapshot(spelling, day)
        pages = int(snap.get("pages") or 0)
        raw = snap.get("rows") or []
        und = None
        if not raw and not spelling.startswith("I:"):
            und = scr_store.underlying(db, sym) or {}
            if str(und.get("sec_type") or "").lower() == "index" and int(self._uni_counts.get(sym, 1) or 0) > 0:
                alt = "I:" + sym
                snap2 = self._snapshot(alt, day)
                pages += int(snap2.get("pages") or 0)
                if snap2.get("rows"):
                    snap, raw = snap2, snap2.get("rows") or []
                    self._spelling[sym] = alt
                    self.log.info("%s: its option chain answers as %s - read that way from now on", sym, alt)
        std = opt_massive.standard_rows(sym, raw)
        stored_close, close_on = scr_store.last_close(db, sym)
        spot, src = opt_massive.estimate_spot(dict(snap, rows=std), stored_close=stored_close, today=day)
        rows, fig = contract_rows(std, spot, day=day, session=session)
        if not rows and (not raw or std):
            # nothing came back, or the standard contracts keep nothing: the stored rows stay
            # while they are recent (raw rows that are ALL non-standard fall through: final)
            if und is None:
                und = scr_store.underlying(db, sym) or {}
            if int(und.get("n_contracts") or 0) > 0 and self._stored_recent(db, sym, session):
                return {"symbol": sym, "contracts": 0, "pages": pages, "spot": None, "spot_src": None,
                        "iv30": None, "rows": len(raw), "empty": True}
        as_of = t_read - _dt.timedelta(seconds=opt_massive.DELAY_S)
        n = scr_store.replace_contracts(db, sym, rows, session_day=session, as_of=as_of)
        if not spot:
            spot, src, s_as_of = None, None, None
        elif src == "massive":
            s_as_of = min(snap.get("underlying_as_of") or as_of, t_read)
        elif src == "close":
            s_as_of = scr_store.close_time_utc(close_on) or as_of
        else:
            s_as_of = as_of
        scr_store.update_underlying_pass(
            db, sym, session_day=session, pass_id=pass_id, spot=spot, spot_src=src, spot_as_of=s_as_of,
            iv30=fig["iv30"], exp_move30=fig["exp_move30"], call_vol=fig["call_vol"],
            put_vol=fig["put_vol"], call_oi=fig["call_oi"], put_oi=fig["put_oi"],
            n_contracts=fig["n_contracts"], file_iv=(kind == "eod"), now=t_read)
        return {"symbol": sym, "contracts": n, "pages": pages, "spot": spot,
                "spot_src": src, "iv30": fig["iv30"], "rows": len(raw)}

    @staticmethod
    def _stored_recent(db, sym: str, session: str) -> bool:
        """Do ``sym``'s stored contract rows describe one of the last
        ``EMPTY_GUARD_SESSIONS`` sessions before ``session`` (or a newer one)?"""
        newest = scr_store.newest_session(db, sym)
        if not newest:
            return False
        try:
            edge = clock.prev_trading_day(str(session)[:10], EMPTY_GUARD_SESSIONS).isoformat()
        except (TypeError, ValueError):
            return True
        return newest >= edge

    def _plan_wide_n(self, p: _Pass) -> int:
        """How many different symbols must answer 403 (with no chain read yet) before the
        403 is taken as the plan lacking the chain endpoint."""
        if p.grow or not p.complete_list:
            return PLAN_WIDE_403
        return max(1, min(PLAN_WIDE_403, p.total))

    def _chain_job(self, p: _Pass, sym: str) -> dict:
        if p.halt.is_set() or self._stopping.is_set():
            return {"symbol": sym, "skipped": True}
        db = self._sf()
        try:
            res = self.read_chain(db, sym, kind=p.kind, session=p.session, day=p.day, pass_id=p.id)
            self._chain_ok_seen = True
            return dict(res, ok=True)
        except MassiveError as exc:
            self._rollback(db)
            if exc.kind in _FATAL:
                p.halt.set()
            elif exc.kind == "plan":
                with p.lock:
                    p.r403.add(sym)
                    wide = (not self._chain_ok_seen) and len(p.r403) >= self._plan_wide_n(p)
                if wide:
                    p.halt.set()
            return {"symbol": sym, "ok": False, "kind": exc.kind, "error": self._err_text(exc)}
        except Exception as exc:  # noqa: BLE001 - one symbol never stops the pass
            self._rollback(db)
            return {"symbol": sym, "ok": False, "kind": "error", "error": self._err_text(exc)}
        finally:
            db.close()

    # ── passes ──

    def _is_refused(self, sym: str, now: _dt.datetime) -> bool:
        t = self._refused.get(sym)
        return t is not None and (now - t).total_seconds() < REFUSED_KEEP_S

    def _pass_due(self, now: _dt.datetime, syms: list[str]) -> tuple[str, str] | None:
        if not syms or not self._allowed("chain", now):
            return None
        et = clock.et_now(now)
        d, t = et.date(), et.time()
        if clock.is_trading_day(d) and RTH_START <= t < RTH_END:
            today = d.isoformat()
            if (self._cycle_retry or self._last_cycle is None or self._last_cycle_session != today
                    or (now - self._last_cycle).total_seconds() >= self.cycle_min * 60):
                return "cycle", today
            return None
        day = eod_day_for(now)
        if day and (self._last_eod or "") < day:
            if self._universe_done is None and self._partial_eod == day:
                return None                   # read on the partial list; the full pass follows the list
            return "eod", day
        return None

    def _start_pass(self, db, kind: str, session: str, syms: list[str], now: _dt.datetime, *,
                    one_off: bool = False) -> _Pass | None:
        syms = [s for s in syms if not self._is_refused(s, now)]
        if not syms:
            return None
        pid = scr_store.start_pass(db, kind=kind, session=session, n_symbols=len(syms), now=now)
        p = _Pass(pid, kind, session, clock.et_date(now), syms, now, {s: self._weight(s) for s in syms})
        p.one_off = one_off
        p.complete_list = self._universe_done is not None
        p.grow = (not one_off and not p.complete_list and self._uni_future is not None and self._uni_stream)
        self._pass = p
        if kind == "cycle":
            self._last_cycle, self._last_cycle_session, self._cycle_retry = now, session, False
        self.log.info("%s pass %d (session %s): %s, %d workers%s", kind, pid, session,
                      _plural(len(syms), "underlying"), self.workers,
                      " - grows with the list of optionable stocks" if p.grow else "")
        self._submit_pass(p)
        return p

    def _submit_pass(self, p: _Pass) -> None:
        p.halt.clear()
        pool = self._workers_pool()
        queue, p.queue = p.queue, []
        for sym in queue:
            p.futures[pool.submit(self._chain_job, p, sym)] = sym

    def _grow_pass(self, p: _Pass, syms: list[str], now: _dt.datetime, db=None) -> int:
        """Add the symbols filed since the pass began (a streamed first universe) to it;
        they are read at once unless the pass is paused. The pass row's ``n_symbols``
        follows (the page's "results cover n of N" reads it)."""
        new = [s for s in syms if s not in p.seen and not self._is_refused(s, now)]
        if not new:
            return 0
        n = p.add(new, {s: self._weight(s) for s in new})
        if db is not None:
            try:
                scr_store.finish_pass(db, p.id, n_symbols=p.total, finished=False, now=now)
            except Exception as exc:  # noqa: BLE001 - only a count; _finish_pass records it again
                self._rollback(db)
                self.log.debug("could not record the growth of pass %d: %s", p.id, exc)
        if (p.queue and not p.halt.is_set() and p.retry_at is None and p.paused_at is None
                and self._allowed("chain", now) and not self._stopping.is_set()):
            pool = self._workers_pool()
            queue, p.queue = p.queue, []
            for sym in queue:
                p.futures[pool.submit(self._chain_job, p, sym)] = sym
        return n

    def _pass_should_close(self, p: _Pass, now: _dt.datetime) -> bool:
        """A pass that must end unfinished: any pass over 12 h old; a cycle pass paused
        past its session; an EOD pass of the loop (not a by-hand one-off) when the
        session's cycle passes are due or a newer EOD pass is."""
        if (now - p.started).total_seconds() > PASS_MAX_AGE_S:
            return True
        if p.one_off:
            return False
        et = clock.et_now(now)
        if p.kind == "cycle":
            paused = not p.futures and bool(p.queue)
            return paused and (et.date().isoformat() != p.session or et.time() >= EOD_AFTER)
        if p.kind == "eod":
            if clock.is_trading_day(et.date()) and RTH_START <= et.time() < RTH_END:
                return True
            d = eod_day_for(now)
            return bool(d and d > p.session)
        return False

    def _symbol_failed(self, p: _Pass, sym: str, kind: str, err: str, *, refused: bool = False) -> None:
        p.failed += 1
        p.done_w += p.w.get(sym, 1)
        p.reasons[(kind, _strip_sym(err, sym))] += 1
        self._note_failure("%s %s: %s" % (p.kind, sym, err))
        if p.logged_fails < FAIL_LOG_PER_PASS:
            if refused:
                self.log.warning("%s pass %d: Massive refused %s's chain (HTTP 403) - skipped this pass",
                                 p.kind, p.id, sym)
            else:
                self.log.warning("%s pass %d: %s failed: %s", p.kind, p.id, sym, err)
        p.logged_fails += 1

    def _requeue(self, p: _Pass, sym: str, kind: str, err: str, *, pause: bool) -> None:
        if p.requeues[sym] >= REQUEUE_MAX:
            self._symbol_failed(p, sym, kind, err)
        else:
            p.requeues[sym] += 1
            p.queue.append(sym)
        if pause:
            self._massive_failure("chain", kind, err)

    def _pass_result(self, p: _Pass, sym: str, res: dict, now: _dt.datetime) -> None:
        if res.get("skipped"):
            p.queue.append(sym)
            return
        if res.get("ok"):
            p.ok += 1
            p.done_w += p.w.get(sym, 1)
            n = int(res.get("contracts") or 0)
            p.contracts += n
            p.pages += int(res.get("pages") or 0)
            if n == 0:
                p.empty_syms.append(sym)
            self._success("chain")
            return
        kind = str(res.get("kind") or "error")
        err = res.get("error") or ""
        if kind == "plan":
            if self._chain_ok_seen:                 # the plan covers chains: this symbol is refused
                self._refused[sym] = now
                self._symbol_failed(p, sym, kind, err, refused=True)
                return
            wide = len(p.r403) >= self._plan_wide_n(p)
            self._requeue(p, sym, kind, err, pause=wide)    # not wide yet: read again at the end
            return
        if kind in _PAUSING:
            self._requeue(p, sym, kind, err, pause=True)    # read again when the pause ends
            return
        self._symbol_failed(p, sym, kind, err)
        if kind in _RETRYABLE:
            p.retry.append(sym)

    def _collect_pass(self, db) -> None:
        p = self._pass
        if p is None:
            return
        now = self._now()
        for f in [f for f in p.futures if f.done()]:
            sym = p.futures.pop(f)
            try:
                res = f.result()
            except Exception as exc:  # noqa: BLE001
                res = {"symbol": sym, "ok": False, "kind": "error", "error": self._err_text(exc)}
            self._pass_result(p, sym, res, now)
        if not p.closing and not p.aborted and self._pass_should_close(p, now):
            p.closing = True
            p.halt.set()
        if not p.aborted and not p.closing and p.ok == 0 and p.failed >= min(EMPTY_ABORT_N, max(1, p.total)):
            p.aborted = True                        # nothing works: stop early, try again later
            p.halt.set()
        if p.futures:
            return
        if p.aborted:
            self._finish_pass(db, p, finished=False)
            self._pass_alert(p, now, empty=True)
            return
        if p.closing:
            self._finish_pass(db, p, finished=False)
            return
        if p.queue:
            if p.retry_at is not None and now < p.retry_at:
                return
            if self._allowed("chain", now) and not self._stopping.is_set():
                if p.paused_at is not None or p.retry_at is not None:
                    self.log.info("%s pass %d resumes: %s left", p.kind, p.id, _plural(len(p.queue), "underlying"))
                p.paused_at = p.retry_at = None
                self._submit_pass(p)
            elif p.paused_at is None:
                p.paused_at = now
            return
        if p.grow:
            if self._uni_future is not None:
                return                              # every symbol listed so far is read
            p.grow = False
            if self._universe_done is not None:     # the walk completed: the rest of the list
                p.complete_list = True
                if self._grow_pass(p, self._stored_symbols(db, now), now, db):
                    return
        bad = p.contracts == 0 or p.failed * 2 > p.done
        if bad and p.retry and not p.retried and not p.one_off:
            p.retried = True
            for s in p.retry:
                p.done_w -= p.w.get(s, 1)
            p.failed -= len(p.retry)
            p.queue, p.retry = list(p.retry), []
            p.retry_at = now + _dt.timedelta(seconds=RETRY_ROUND_S)
            self.log.info("%s pass %d: %s failed - tried once more at %s", p.kind, p.id,
                          _plural(len(p.queue), "underlying"), _et_hm(p.retry_at))
            return
        self._end_pass(db, p, now)

    def _end_pass(self, db, p: _Pass, now: _dt.datetime) -> None:
        """Every symbol is read or failed: a pass that stored no contract is not done (it
        ends unfinished and the passes pause); a mostly failed EOD pass is finished (the
        page reloads) but read again; anything else is done."""
        if p.total and p.contracts == 0:
            self._finish_pass(db, p, finished=False)
            self._pass_alert(p, now, empty=True)
            return
        if p.kind == "eod" and p.failed * 2 > p.done:
            self._finish_pass(db, p, finished=True, mark_eod=False)
            self._pass_alert(p, now, empty=False)
            return
        self._finish_pass(db, p, finished=True)

    def _pass_alert(self, p: _Pass, now: _dt.datetime, *, empty: bool) -> None:
        """The ``chain`` alert after a pass that read nothing (kind ``empty``) or failed for
        most of its symbols (the commonest failure's kind): the passes wait 10 min, doubling
        to 2 h, then read again."""
        self._empty_fails += 1
        wait = min(EMPTY_RETRY_S * 2 ** (self._empty_fails - 1), EMPTY_RETRY_MAX_S)
        if p.reasons:
            (kind, text), n = p.reasons.most_common(1)[0]
        else:
            kind, text, n = "empty", "Massive returned no contracts", len(p.empty_syms) or p.done
        if empty:
            msg = "%s pass %d read no option data: %s (%d of %d underlyings)" % (p.kind, p.id, text, n, p.total)
            self._alert("chain", "empty", msg, wait, now)
        else:
            msg = "%s pass %d failed for %d of %d underlyings: %s (%d of them)" % (
                p.kind, p.id, p.failed, p.total, text, n)
            self._alert("chain", kind if kind in massive.KINDS else "error", msg, wait, now)
        if p.kind == "cycle":
            self._cycle_retry = True

    def _finish_pass(self, db, p: _Pass, *, finished: bool, mark_eod: bool = True) -> None:
        ms = int((time.monotonic() - p.t0) * 1000)
        failed = p.failed + (0 if finished else len(p.queue))
        try:
            row = scr_store.finish_pass(db, p.id, n_ok=p.ok, n_failed=failed, n_contracts=p.contracts,
                                        requests=p.pages, ms=ms, finished=finished, now=self._now(),
                                        n_symbols=p.total, partial=not p.complete_list)
        except Exception as exc:  # noqa: BLE001
            self._rollback(db)
            row = None
            self.log.warning("could not record the end of pass %d: %s", p.id, exc)
        self._pass = None
        partial = not p.complete_list
        if finished:
            self.last_pass = row or self.last_pass
            if p.kind == "eod":
                if partial:
                    self._partial_eod = p.session
                elif mark_eod:
                    self._last_eod = p.session
            self.log.info("%s pass %d done: %d ok, %d failed, %s, %d requests, %.0f s%s", p.kind, p.id,
                          p.ok, p.failed, _plural(p.contracts, "contract"), p.pages, ms / 1000.0,
                          " (on a partial list of optionable stocks)" if partial else "")
        else:
            if p.kind == "cycle":
                self._cycle_retry = False
            self.log.warning("%s pass %d stopped unfinished: %d ok, %d failed, %d not read", p.kind,
                             p.id, p.ok, p.failed, len(p.queue))
        if p.contracts > 0 and self._alerts.get("chain", ("",))[0] == "empty":
            self._clear("chain")
        if p.contracts > 0 and not p.failed * 2 > p.done:
            self._empty_fails = 0
        if p.empty_syms:
            first = ", ".join(p.empty_syms[:5]) + (", ..." if len(p.empty_syms) > 5 else "")
            self.log.warning("%s listed by Massive returned no contracts (first: %s)",
                             _plural(len(p.empty_syms), "underlying"), first)
        if p.logged_fails > FAIL_LOG_PER_PASS:
            self.log.warning("%s pass %d: %d underlyings failed in all (the first %d logged)", p.kind,
                             p.id, p.logged_fails, FAIL_LOG_PER_PASS)

    # ── IV history (worker threads) ──

    def history_one(self, db, sym: str, *, overwrite: bool = False) -> dict:
        """The IV30 history of ``sym`` (§4.4) from its unadjusted closes; files the series,
        recomputes the IV figures, marks the attempt. Raises ``MassiveError`` (nothing
        marked) when a read fails."""
        bars = scr_store.raw_bars(db, sym, HISTORY_SESSIONS)
        points, n_req = [], 0
        if len(bars) >= HISTORY_MIN_POINTS:
            points, n_req = opt_massive.iv30_history(self.client, sym, bars, days_iv=HISTORY_SESSIONS)
            if points:
                scr_store.upsert_daily(db, sym, iv_series=points, overwrite_iv=overwrite)
            scr_store.recompute_iv(db, sym, now=self._now())
        done = len(points) >= HISTORY_MIN_POINTS
        mark = scr_store.mark_history(db, sym, done=done, now=self._now())
        return {"symbol": sym, "bars": len(bars), "points": len(points), "requests": n_req,
                "done": done, "tries": mark.get("tries")}

    def _history_job(self, sym: str, overwrite: bool = False) -> dict:
        if self._stopping.is_set():
            return {"symbol": sym, "skipped": True}
        db = self._sf()
        try:
            res = self.history_one(db, sym, overwrite=overwrite)
            if res.get("requests"):
                self._hist_ok_seen = True
            return dict(res, ok=True)
        except MassiveError as exc:
            self._rollback(db)
            one = exc.kind == "plan" and self._hist_ok_seen      # the plan covers history: this symbol is refused
            if exc.kind not in _PAUSING or one:
                try:
                    scr_store.mark_history(db, sym, done=False, now=self._now())
                except Exception:  # noqa: BLE001
                    self._rollback(db)
            return {"symbol": sym, "ok": False, "kind": "plan_symbol" if one else exc.kind,
                    "error": self._err_text(exc)}
        except Exception as exc:  # noqa: BLE001
            try:
                db.rollback()
                scr_store.mark_history(db, sym, done=False, now=self._now())
            except Exception:  # noqa: BLE001
                self._rollback(db)
            return {"symbol": sym, "ok": False, "kind": "error", "error": self._err_text(exc)}
        finally:
            db.close()

    def _collect_history(self) -> list[dict]:
        out = []
        for f in [f for f in self._hist if f.done()]:
            sym = self._hist.pop(f)
            try:
                res = f.result()
            except Exception as exc:  # noqa: BLE001
                res = {"symbol": sym, "ok": False, "kind": "error", "error": self._err_text(exc)}
            out.append(res)
            if res.get("skipped"):
                continue
            if res.get("ok"):
                if res.get("requests"):
                    self._success("history")
                first = not res.get("done") and sym not in self._hist_gave_up
                if not res.get("done"):
                    self._hist_gave_up.add(sym)
                lvl = logging.INFO if (res.get("done") or first) else logging.DEBUG
                self.log.log(lvl, "IV history %s: %d IV points from %d closes, %d requests%s", sym,
                             res.get("points", 0), res.get("bars", 0), res.get("requests", 0),
                             "" if res.get("done") else " - too few, retried later")
            elif res.get("kind") in _PAUSING:
                self._massive_failure("history", res["kind"], res.get("error") or "")
            else:
                self._note_failure("IV history %s: %s" % (sym, res.get("error")))
                self.log.warning("IV history %s failed: %s", sym, res.get("error"))
        return out

    def _recent_days_done(self) -> bool:
        if not self._days:
            return False
        recent = sorted(self._days)[-HISTORY_SESSIONS:]
        return all(self._days[d][0] and self._days[d][1] for d in recent)

    def _history_tick(self, db, now: _dt.datetime, syms: list[str]) -> None:
        room = self.workers - len(self._hist)
        if room <= 0 or not self._allowed("history", now) or not self._recent_days_done():
            return
        busy = set(self._hist.values())
        queue = [s for s in scr_store.history_queue(db, now=now, symbols=syms, limit=room + len(busy))
                 if s not in busy][:room]
        pool = self._workers_pool()
        for sym in queue:
            self._hist[pool.submit(self._history_job, sym)] = sym

    # ── the stocks lane (one thread) ──

    def _load_days(self, db, now: _dt.datetime) -> None:
        if self._days_loaded_at is not None and \
                (now - self._days_loaded_at).total_seconds() < STOCK_DAYS_RELOAD_S:
            want = [d for d in stock_days(now, self.stock_years) if d not in self._refused_days]
            if want and want[-1] in self._days:
                return
        days = [d for d in stock_days(now, self.stock_years) if d not in self._refused_days]
        if not days:
            self._days = {}
            return
        have = scr_store.stock_day_status(db, days[0], days[-1])
        self._days = {d: [have.get(d, (0, 0))[0] > 0, have.get(d, (0, 0))[1] > 0] for d in days}
        self._days_loaded_at = now

    def _next_day(self, now: _dt.datetime) -> tuple[str, bool, bool] | None:
        for d in sorted(self._days, reverse=True):
            adj, raw = self._days[d]
            if adj and raw:
                continue
            at = self._day_retry.get(d)
            if at is not None and now < at:
                continue
            return d, not adj, not raw
        return None

    def _valid_resume(self, now: _dt.datetime) -> dict | None:
        """The saved cursor of a failed walk, while it is under 2 h old and from today (ET)."""
        r = self._uni_resume
        if r is None:
            return None
        if r.get("day") != clock.et_today(now) or (now - r["t"]).total_seconds() > UNIVERSE_RESUME_MAX_S:
            self._uni_resume = None
            self.log.info("universe: the saved page cursor is dropped (over 2 h old or a new day) - "
                          "the list is read again from page 1")
            return None
        return r

    def _universe_due(self, now: _dt.datetime) -> bool:
        if not self._allowed("universe", now):
            return False
        if self._universe_retry_at is not None and now < self._universe_retry_at:
            return False
        if self._valid_resume(now) is not None:
            return True
        ref = self._universe_done
        first = not self._universe_checked
        self._universe_checked = True
        if ref is None:
            return True
        et = clock.et_now(now)
        at = _dt.datetime.combine(et.date(), UNIVERSE_AT, tzinfo=et.tzinfo).astimezone(
            _dt.timezone.utc).replace(tzinfo=None)
        if now >= at > ref:
            return True
        return first and (now - ref).total_seconds() > UNIVERSE_MAX_AGE_H * 3600

    def _next_bg_job(self, db, now: _dt.datetime):
        """The stocks lane's next job ``(name, fn)``: identities as soon as some stocks are
        filed; the rest (stock days, fills, technicals) only on a complete universe."""
        if not self.universe_n:
            return None
        if self._universe_done is not None:
            self._load_days(db, now)                 # the IV history waits on these, not on the lane
        stocks_ok = self._allowed("stocks", now) and not (
            self._stocks_retry_at is not None and now < self._stocks_retry_at)
        if stocks_ok and self._identity_due(db, now):
            return "identity", self.job_identity
        if self._universe_done is None:
            return None
        tech_ok = self._tech_dirty and not (self._tech_retry_at is not None and now < self._tech_retry_at)
        tech_age_ok = self._tech_at is None or (now - self._tech_at).total_seconds() >= TECH_EVERY_S
        if stocks_ok:
            if tech_ok and tech_age_ok and self._pending_days() and self._recent_days_done():
                return "technicals", self.job_technicals        # hourly while older days still file
            nxt = self._next_day(now)
            if nxt is not None:
                d, adj, raw = nxt
                self._bg_day = d
                return "stocks", lambda: self.job_stock_day(d, adjusted=adj, raw=raw)
            for sym, why in list(self._fill.items()):
                at = self._fill_tried.get(sym)
                if at is not None and (now - at).total_seconds() < FILL_RETRY_S:
                    self._fill.pop(sym, None)
                    continue
                self._fill.pop(sym, None)
                self._fill_tried[sym] = now
                return "fill", lambda s=sym, w=why: self.job_fill(s, split=(w == "split"))
        if tech_ok and (self._pending_days() == 0 or tech_age_ok):
            return "technicals", self.job_technicals
        return None

    def _identity_due(self, db, now: _dt.datetime) -> bool:
        if self._identity_retry_at is not None and now < self._identity_retry_at:
            return False
        if self._identity_at is not None:
            return (now - self._identity_at).total_seconds() >= IDENTITY_EVERY_S
        return scr_store.identity_missing(db)

    def _bg_wrap(self, name: str, fn) -> dict:
        """Run a background job; never raises. A job's traceback is logged on its first
        failure in a row only - repeats are one WARNING line (at most every 30 min)."""
        try:
            out = fn() or {}
            self._bg_fails.pop(name, None)
            return dict(out, ok=out.get("ok", True), job=name)
        except MassiveError as exc:
            return {"job": name, "ok": False, "kind": exc.kind, "error": self._err_text(exc)}
        except Exception as exc:  # noqa: BLE001
            n = self._bg_fails.get(name, 0) + 1
            self._bg_fails[name] = n
            text = self._err_text(exc)
            if n == 1:
                self.log.exception("lane job %s failed", name)
            else:
                self._log_state(("bg", name, type(exc).__name__), logging.WARNING,
                                "lane job %s failed again (%d in a row): %s", name, n, text)
            return {"job": name, "ok": False, "kind": "error", "error": text, "integrity": _is_integrity(exc)}

    def _bg_tick(self, db, now: _dt.datetime) -> None:
        if self._bg is not None:
            return
        job = self._next_bg_job(db, now)
        if job is None:
            return
        name, fn = job
        self._bg = (name, self._lane().submit(self._bg_wrap, name, fn))

    def _collect_bg(self) -> dict | None:
        if self._bg is None or not self._bg[1].done():
            return None
        name, fut = self._bg
        self._bg = None
        try:
            res = fut.result()
        except Exception as exc:  # noqa: BLE001
            res = {"job": name, "ok": False, "kind": "error", "error": self._err_text(exc)}
        self._bg_result(name, res)
        return res

    def _uni_tick(self, now: _dt.datetime) -> None:
        if self._uni_future is not None or self._stopping.is_set() or not self._universe_due(now):
            return
        self._uni_stream = self._universe_done is None
        rs = self._uni_resume                       # shown until the walk's first page comes back
        base = int(rs.get("pages") or 0) if rs else 0
        self._uni_progress = {"pages": base, "base": base, "symbols": len(rs["counts"]) if rs else 0,
                              "started": rs["started"] if rs else now, "cursor": rs["url"] if rs else None}
        self._uni_future = self._uni_lane().submit(self._bg_wrap, "universe", self.job_universe)

    def _uni_resumed(self) -> None:
        """A walk that failed with no complete list on file raised the ``universe`` alert.
        Once its retry (resumed at the saved cursor, or from page 1) has got a page back,
        the walk works again: the alert is dropped - the page and the tray show the walk's
        progress (T-01), not "nothing can be screened" - and its reason stays in
        ``last_error`` until the walk completes. A new failure raises it again."""
        if self._uni_future is None or "universe" not in self._alerts:
            return
        pr = self._uni_progress or {}
        try:
            pages, base = int(pr.get("pages") or 0), int(pr.get("base") or 0)
        except (TypeError, ValueError):
            return
        if pages <= base:
            return                               # no page back yet (it may still be retrying)
        kind, reason = self._alerts.pop("universe")
        self._retry_at.pop("universe", None)
        if self._state_log is not None and self._state_log[0][:2] == ("alert", "universe"):
            self._state_log = None               # a new failure after this progress is logged again
        active = self._alert_scope()
        if active is not None:
            self.last_error = self._alerts[active][1][:500]
            self.last_failure = reason[:500]
        else:
            self.last_error = reason[:500]
        self.log.info("universe: the walk is reading again (page %s) - the %s error is cleared",
                      format(pages, ","), kind)

    def _collect_uni(self) -> None:
        f = self._uni_future
        if f is None or not f.done():
            return
        self._uni_future = None
        try:
            res = f.result()
        except Exception as exc:  # noqa: BLE001
            res = {"job": "universe", "ok": False, "kind": "error", "error": self._err_text(exc)}
        self._uni_progress = None
        self._bg_result("universe", res)

    def _earnings_due(self, now: _dt.datetime) -> bool:
        if not self.universe_n or self._universe_done is None:
            return False
        if self._earnings_on == clock.et_today(now):
            return False
        if self._earnings_retry_at is not None and now < self._earnings_retry_at:
            return False
        return clock.et_now(now).time() >= UNIVERSE_AT or self._earnings_on is None

    def _earn_tick(self, now: _dt.datetime) -> None:
        if self._earn_future is not None or self._stopping.is_set() or not self._earnings_due(now):
            return
        self._earn_future = self._earn_lane().submit(self._bg_wrap, "earnings", self.job_earnings)

    def _collect_earn(self) -> None:
        f = self._earn_future
        if f is None or not f.done():
            return
        self._earn_future = None
        try:
            res = f.result()
        except Exception as exc:  # noqa: BLE001
            res = {"job": "earnings", "ok": False, "kind": "error", "error": self._err_text(exc)}
        self._bg_result("earnings", res)

    def _bg_result(self, name: str, res: dict) -> None:
        now = self._now()
        if name == "universe":
            if res.get("ok"):
                self._universe_ok(res, now)
            else:
                self._universe_failed(res, now)
            return
        if not res.get("ok"):
            self._bg_failed(name, res, now)
            return
        if name in _BG_OP:
            self._success(_BG_OP[name])
        if name == "identity":
            self._identity_at, self._identity_retry_at = now, None
            self.log.info("identity: %d underlyings named (%d stock tickers, %d indices)",
                          res.get("named", 0), res.get("stocks", 0), res.get("indices", 0))
        elif name == "earnings":
            self._earnings_on, self._earnings_retry_at = clock.et_today(now), None
            self.log.info("earnings: %d dates from %d calendar rows, %d rows changed",
                          res.get("dates", 0), res.get("rows", 0), res.get("changed", 0))
        elif name == "stocks":
            d = res.get("day")
            if res.get("adj_bars") or res.get("raw_bars"):
                self._stock_ok_seen = True
            if d in self._days:
                if res.get("adj_bars"):
                    self._days[d][0] = True
                if res.get("raw_bars"):
                    self._days[d][1] = True
            if (res.get("adjusted") and not res.get("adj_bars")) or (res.get("raw") and not res.get("raw_bars")):
                self._day_retry[d] = now + _dt.timedelta(seconds=EMPTY_DAY_RETRY_S)
                self.log.info("stock bars of %s: Massive has none yet - asked again in %d h", d,
                              EMPTY_DAY_RETRY_S / 3600)
            if res.get("filed"):
                self._tech_dirty = True
            self._queue_fill(res.get("split") or [], "split")
            if self._pending_days() == 0 and res.get("filed"):
                self.log.info("stock bars: every session of the last %d years is on file", STOCK_YEARS)
        elif name == "fill":
            # job_fill recomputed that symbol's technicals itself. Massive had no bars at all
            # for it (not a stock - an index, a foreign listing): ask again in a week.
            if not res.get("days") and res.get("symbol"):
                self._fill_tried[res["symbol"]] = now + _dt.timedelta(seconds=FILL_EMPTY_RETRY_S - FILL_RETRY_S)
        elif name == "technicals":
            self._tech_dirty = False
            self._tech_at = now
            self._tech_retry_at = None
            self._queue_fill(res.get("fill") or [], "new")
            self.log.info("technicals: %s recomputed", _plural(res.get("n", 0), "underlying"))

    def _bg_failed(self, name: str, res: dict, now: _dt.datetime) -> None:
        kind, err = str(res.get("kind") or "error"), res.get("error") or ""
        if name == "stocks" and kind == "plan_day":
            self._stock_day_refused(res, now)
            return
        if name == "fill" and kind == "plan_fill":
            sym = res.get("symbol")
            if sym:
                self._fill_tried[sym] = now
            self.log.info("own stock bars of %s: Massive refused them (HTTP 403) - asked again in 24 h", sym)
            return
        op = _BG_OP.get(name)
        if op and kind in _PAUSING:
            self._massive_failure(op, kind, err)
        else:
            self._note_failure("%s: %s" % (name, err))
            self.log.warning("%s failed: %s", name, err)
        if name == "identity" and kind not in _FATAL:
            self._identity_retry_at = now + _dt.timedelta(seconds=STOCKS_RETRY_S)
        elif name == "earnings":
            self._earnings_retry_at = now + _dt.timedelta(seconds=EARNINGS_RETRY_S)
        elif name == "stocks" and res.get("integrity"):
            d = res.get("day") or self._bg_day
            if d:
                self._day_retry[d] = now + _dt.timedelta(seconds=RACE_RETRY_S)
        elif name in ("stocks", "fill") and kind not in _PAUSING:
            self._stocks_retry_at = now + _dt.timedelta(seconds=STOCKS_RETRY_S)
        elif name == "technicals":
            self._tech_retry_at = now + _dt.timedelta(seconds=STOCKS_RETRY_S)

    def _stock_day_refused(self, res: dict, now: _dt.datetime) -> None:
        """A grouped day Massive refused (403): at the plan window's edge it is retired (never
        asked again), a newer one is asked again in 1 h - the lane is not paused. Only a
        refusal that is not about the date, before any stock day ever came back, is the
        plan lacking the endpoint (the lane pauses as before)."""
        d = res.get("day")
        if d in self._days and res.get("adj_bars"):
            self._days[d][0] = True
        if not res.get("window") and not self._stock_ok_seen:
            self._massive_failure("stocks", "plan", res.get("error") or "")
            return
        if not d:
            return
        edge = (plan_start(now) + _dt.timedelta(days=PLAN_DAY_MARGIN_DAYS)).isoformat()
        if d < edge:
            self._refused_days.add(d)
            self._days.pop(d, None)
            self.log.info("stock bars of %s: outside Massive's stock plan window - not asked again", d)
        else:
            self._day_retry[d] = now + _dt.timedelta(seconds=PLAN_DAY_RETRY_S)
            self.log.info("stock bars of %s: Massive refused them (HTTP 403) - asked again in 1 h", d)

    def _universe_ok(self, res: dict, now: _dt.datetime) -> None:
        self._success("universe")
        self._universe_retry_at = None
        self._uni_resume = None
        self._universe_done = res.get("done") or now
        self.universe_on = clock.et_today(now)
        self._warn = None
        le = self.last_error or ""
        if le.startswith("universe") or le == UNIVERSE_EMPTY_TEXT:
            scope = self._alert_scope()
            self.last_error = self._alerts[scope][1][:500] if scope else None
        self._tech_dirty = True
        self.log.info("universe: %s (%d new, %d gone%s), %s; prune %s", _plural(res.get("n", 0), "underlying"),
                      res.get("new", 0), res.get("inactive", 0),
                      ", partial list - nothing deactivated" if res.get("partial") else "",
                      _plural(res.get("pages") or 0, "page"), res.get("pruned"))
        self._queue_fill(res.get("fill") or [], "new")

    def _universe_failed(self, res: dict, now: _dt.datetime) -> None:
        """A walk that failed. With a cursor it resumes there in 60 s (the network back-off
        for kind network); without one it starts again in 15 min. With no complete list on
        file the failure is an error (the ``universe`` alert - never for the kinds that
        already pause); with one it is only a warning - yesterday's list stays in use."""
        kind, err = str(res.get("kind") or "error"), res.get("error") or ""
        url = res.get("resume_url")
        if url and kind != "empty":
            old = self._uni_resume
            t = old["t"] if (old and old.get("url") == url) else now
            self._uni_resume = {"url": url, "counts": dict(res.get("counts") or {}), "day": clock.et_today(now),
                                "t": t, "pages": int(res.get("pages") or 0), "started": res.get("started") or now}
            wait = UNIVERSE_RESUME_S
        else:
            self._uni_resume = None
            wait = UNIVERSE_RETRY_S
        if kind in _PAUSING:
            self._massive_failure("universe", kind, err)
            self._universe_retry_at = None if kind in _FATAL else now + _dt.timedelta(seconds=wait)
            return
        self._universe_retry_at = now + _dt.timedelta(seconds=wait)
        if self._universe_done is None:
            if kind == "empty":
                self._alert("universe", "empty", UNIVERSE_EMPTY_TEXT, UNIVERSE_RETRY_S, now)
            else:
                self._alert("universe", kind, "universe: " + err, wait, now)
            return
        reason = err[:160]
        self._warn = "Universe refresh failed %s (%s); next try %s - yesterday's list in use." % (
            _et_hm(now), reason, _et_hm(self._universe_retry_at))
        self._note_failure("universe: " + err)
        self.log.warning("universe failed: %s (next try %s; yesterday's list in use)", err,
                         _et_hm(self._universe_retry_at))

    def _queue_fill(self, syms, why: str) -> None:
        now = self._now()
        for s in list(syms)[:FILL_BATCH * 4]:
            at = self._fill_tried.get(s)
            if why == "split" or at is None or (now - at).total_seconds() >= FILL_RETRY_S:
                if why == "split":
                    self._fill_tried.pop(s, None)
                if len(self._fill) < FILL_BATCH or why == "split":
                    self._fill[s] = why

    def _fill_candidates(self, db) -> list[str]:
        """Universe symbols with too few bars once every stock day is on file - not an
        index (``I:`` prefix, an index of the reference list, or a stored index type)."""
        if not self._days or self._pending_days():
            return []
        counts = scr_store.daily_counts(db)
        out = []
        for s in scr_store.pass_symbols(db, now=self._now()):
            if s.startswith("I:") or s in self._index_syms or counts.get(s, 0) >= FILL_MIN_BARS:
                continue
            u = scr_store.underlying(db, s) or {}
            if str(u.get("sec_type") or "").lower() == "index":
                continue
            out.append(s)
            if len(out) >= FILL_BATCH * 4:
                break
        return out

    # ── background jobs (each opens its own session) ──

    def _apply_identity(self, db, syms) -> None:
        """The cached reference records (the last identity read) onto newly filed symbols;
        kept pending until there are records."""
        pend = set(syms) | self._ident_pending
        if not pend:
            return
        recs = self._ident_records
        if recs is None:
            self._ident_pending = pend
            return
        try:
            scr_store.set_identity(db, recs, only=pend)
            self._ident_pending = set()
        except Exception as exc:  # noqa: BLE001
            self._rollback(db)
            self._ident_pending = pend
            self._log_state(("ident", type(exc).__name__), logging.WARNING,
                            "universe: could not name the stocks just filed: %s", self._err_text(exc))

    def _uni_save(self, counts: dict, filed: set) -> None:
        """A streamed save of the stocks read so far (never deactivates anything)."""
        db = self._sf()
        try:
            scr_store.upsert_universe(db, counts, deactivate=False)
            new = set(counts) - filed
            filed.update(counts)
            self._apply_identity(db, new)
        except Exception as exc:  # noqa: BLE001 - the walk goes on; the next save files them
            self._rollback(db)
            self._log_state(("unisave", type(exc).__name__), logging.WARNING,
                            "universe: could not file the stocks read so far: %s", self._err_text(exc))
        finally:
            db.close()

    def job_universe(self) -> dict:
        """Massive's options list -> the universe -> retention. Streamed on a first start,
        resumed at a failed page's cursor; an empty list is a failure (``empty``)."""
        now = self._now()
        today = clock.et_date(now)
        stream = self._universe_done is None
        resume = self._valid_resume(now)
        if resume is not None:
            start_url, base = resume["url"], dict(resume.get("counts") or {})
            base_pages, started = int(resume.get("pages") or 0), resume.get("started") or now
        else:
            start_url, base, base_pages, started = None, {}, 0, now
        self._uni_stream = stream
        self._uni_progress = {"pages": base_pages, "base": base_pages, "symbols": len(base), "started": started,
                              "cursor": start_url}
        saver = {"n": 0, "page": base_pages, "t": time.monotonic(), "filed": set()}

        def on_page(page_no, counts, next_url):
            pages = base_pages + int(page_no)
            if stream:            # a day-2 refresh keeps the full list's counts until it completes
                self._uni_counts = counts
            self._uni_progress = {"pages": pages, "base": base_pages, "symbols": len(counts), "started": started,
                                  "cursor": next_url}
            if stream and (saver["n"] == 0 or pages - saver["page"] >= UNIVERSE_SAVE_PAGES
                           or time.monotonic() - saver["t"] >= UNIVERSE_SAVE_S):
                self._uni_save(counts, saver["filed"])
                saver.update(n=saver["n"] + 1, page=pages, t=time.monotonic())

        try:
            counts = self.client.option_underlyings(
                exp_lte=today + _dt.timedelta(days=UNIVERSE_EXP_DAYS), start_url=start_url,
                counts=base or None, on_page=on_page)
        except MassiveError as exc:
            status = int(exc.status or 0)
            if start_url and exc.kind == "http" and 400 <= status < 500 and not getattr(exc, "pages", 0):
                self.log.info("universe: Massive refused the saved page cursor (HTTP %d) - the list is "
                              "read again from page 1", status)
                self._uni_resume = None
                return self.job_universe()
            got = exc.counts if isinstance(getattr(exc, "counts", None), dict) else base
            if stream and got:
                self._uni_save(got, saver["filed"])
            return {"ok": False, "kind": exc.kind, "error": self._err_text(exc),
                    "resume_url": getattr(exc, "resume_url", None), "counts": got,
                    "pages": base_pages + int(getattr(exc, "pages", 0) or 0), "started": started}
        if not counts:
            return {"ok": False, "kind": "empty", "error": UNIVERSE_EMPTY_TEXT}
        done = self._now()
        self._uni_counts = dict(counts)
        pages = int((self._uni_progress or {}).get("pages") or 0)
        db = self._sf()
        try:
            before = set(scr_store.pass_symbols(db, now=done))
            res = scr_store.upsert_universe(db, counts, now=done)
            res["pruned"] = scr_store.prune(db, now=done, today=clock.et_date(done).isoformat())
            self._apply_identity(db, {str(s).strip().upper() for s in counts} - before)
            res["fill"] = self._fill_candidates(db)
        finally:
            db.close()
        res.update(done=done, pages=pages)
        return res

    def job_identity(self) -> dict:
        """The ticker reference lists -> name / security type / exchange (the records are
        kept for the stocks a streamed universe files later)."""
        stocks = self.client.reference_tickers("stocks")
        try:
            indices = self.client.reference_tickers("indices")
        except MassiveError as exc:
            if exc.kind in _FATAL:
                raise
            self.log.info("index names not read (%s): index underlyings keep no name",
                          self._err_text(exc))
            indices = []
        records = [{"symbol": t["symbol"], "name": t.get("name"),
                    "sec_type": sec_type_of(t.get("type")), "exchange": exchange_of(t.get("primary_exchange"))}
                   for t in stocks]
        seen = {r["symbol"] for r in records}
        for t in indices:
            rec = {"symbol": t["symbol"], "name": t.get("name"), "sec_type": "index", "exchange": "INDEX"}
            records.append(rec)
            bare = t["symbol"][2:] if t["symbol"].startswith("I:") else None
            if bare and bare not in seen:
                records.append(dict(rec, symbol=bare))
        self._index_syms = {r["symbol"] for r in records if r["sec_type"] == "index"}
        self._ident_records = records
        db = self._sf()
        try:
            n = scr_store.set_identity(db, records, only=set(scr_store.pass_symbols(db, now=self._now())))
            self._ident_pending = set()
            return {"named": n, "stocks": len(stocks), "indices": len(indices)}
        finally:
            db.close()

    def job_earnings(self) -> dict:
        """The next earnings date per underlying from Nasdaq's calendar (next 70 days)."""
        now = self._now()
        today = clock.et_date(now)
        dates: dict[str, str] = {}
        n_rows = 0
        for i in range(EARNINGS_DAYS + 1):
            d = today + _dt.timedelta(days=i)
            if d.weekday() >= 5:
                continue
            if self._stopping.is_set():
                return {"ok": False, "kind": "error", "error": "stopping"}
            rows = self._earnings_fetch(d) or []
            n_rows += len(rows)
            for r in rows:
                sym = massive.our_symbol((r or {}).get("symbol"))
                if sym and sym not in dates:
                    dates[sym] = d.isoformat()
        if n_rows == 0:
            return {"ok": False, "kind": "error", "error": "Nasdaq's earnings calendar returned nothing"}
        db = self._sf()
        try:
            changed = scr_store.set_earnings(db, dates, today=today.isoformat(), now=now)
        finally:
            db.close()
        return {"dates": len(dates), "rows": n_rows, "changed": changed}

    def job_stock_day(self, day: str, *, adjusted: bool = True, raw: bool = True) -> dict:
        """One session of grouped daily bars (adjusted and / or unadjusted) for the
        universe; a filed adjusted day is checked for splits against the day before. A
        403 for the day comes back as ``kind`` ``plan_day`` (``window`` when Massive spoke
        of the plan's timeframe) - not raised."""
        out = {"day": day, "adjusted": adjusted, "raw": raw, "adj_bars": 0, "raw_bars": 0, "filed": 0,
               "split": []}
        db = self._sf()
        try:
            syms = set(scr_store.pass_symbols(db, now=self._now()))
            if adjusted:
                bars = self.client.grouped_daily(day, adjusted=True)
                out["adj_bars"] = len(bars)
                out["filed"] += scr_store.file_grouped_day(db, day, bars, adjusted=True, symbols=syms)
            if raw:
                bars = self.client.grouped_daily(day, adjusted=False)
                out["raw_bars"] = len(bars)
                out["filed"] += scr_store.file_grouped_day(db, day, bars, adjusted=False, symbols=syms)
            if adjusted and out["adj_bars"]:
                prev = clock.prev_trading_day(day).isoformat()
                if self._days.get(prev, [False])[0]:
                    out["split"] = scr_store.big_moves(db, day, lo=SPLIT_LO, hi=SPLIT_HI, symbols=syms)
            return out
        except MassiveError as exc:
            if exc.kind != "plan":
                raise
            self._rollback(db)
            return dict(out, ok=False, kind="plan_day", error=self._err_text(exc),
                        window=bool(getattr(exc, "window", False)))
        finally:
            db.close()

    def job_fill(self, sym: str, *, split: bool = False) -> dict:
        """One symbol's own 2 years of bars (never before the plan's window; a new
        universe member; after a split only the adjusted ones), then its technicals. A 403
        comes back as ``kind`` ``plan_fill`` - that symbol only."""
        now = self._now()
        end = opt_massive.published_session(clock.et_date(now), now)
        start = max(end - _dt.timedelta(days=int(self.stock_years * 365.25) + 1), plan_start(now))
        try:
            adj = self.client.stock_daily(sym, start, end)
            raw = [] if split else self.client.stock_daily(sym, start, end, adjusted=False)
        except MassiveError as exc:
            if exc.kind != "plan":
                raise
            return {"symbol": sym, "ok": False, "kind": "plan_fill", "error": self._err_text(exc)}
        db = self._sf()
        try:
            n = scr_store.upsert_daily(db, sym, bars=adj, raw_bars=raw, today=end.isoformat())
            scr_store.recompute_technicals(db, sym, now=now)
            return {"symbol": sym, "days": n, "split": split}
        finally:
            db.close()

    def job_technicals(self) -> dict:
        """The technicals of every pass symbol; then the symbols that need their own bars."""
        db = self._sf()
        try:
            syms = scr_store.pass_symbols(db, now=self._now())
            n = scr_store.recompute_technicals_many(db, syms, now=self._now(),
                                                    stop=self._stopping.is_set)
            return {"n": n, "fill": self._fill_candidates(db)}
        finally:
            db.close()

    # ── the detail (plain words for members) ──

    def _session_words(self, p: _Pass) -> str:
        return ("%s close" % _day_words(p.session)) if p.kind == "eod" else "live, 15-min delayed"

    def _pass_text(self, p: _Pass, now: _dt.datetime) -> str:
        ses = self._session_words(p)
        if p.grow and not p.futures and not p.queue and self._uni_future is not None:
            return ("Reading the option market (%s): all %s underlyings listed so far are read - waiting for "
                    "the rest of the list." % (ses, format(p.done, ",")))
        pct = self._pass_pct(p)
        eta = self._pass_eta_s(p, now, pct)
        text = "Reading the option market (%s): %s of %s underlyings, %d%% of contracts" % (
            ses, format(p.done, ","), format(p.total, ","), int(pct * 100))
        return text + ((", about %s left." % _dur(eta)) if eta is not None else ".")

    def _uni_text(self, now: _dt.datetime) -> str:
        pr = self._uni_progress or {}
        pages = max(1, int(pr.get("pages") or 0))
        n = int(pr.get("symbols") or 0)
        if self._uni_stream:
            started = pr.get("started") or now
            m = max(0, int((now - started).total_seconds() // 60))
            return ("Step 1 of 2: reading Massive's list of optionable stocks - page %s (%s stocks so far, %d min). "
                    "Results start appearing as soon as the first stocks are read."
                    % (format(pages, ","), format(n, ","), m))
        return ("Refreshing the list of optionable stocks - page %s (%s so far); yesterday's list is in use "
                "meanwhile." % (format(pages, ","), format(n, ",")))

    def _identity_minutes(self) -> int:
        return max(1, int(round(IDENTITY_REQUESTS * STOCK_REQ_S / self._spm() / 60.0)))

    def _bg_text(self) -> str:
        parts = []
        name = self._bg[0] if self._bg is not None else None
        if name == "identity":
            parts.append("Loading stock names and types (about %d min) - the Security Type and Exchange "
                         "filters need them." % self._identity_minutes())
        elif name == "stocks":
            parts.append("Loading 2 years of daily stock prices: %s of %s trading days to go, at least %s left "
                         "(Massive's stock plan allows %d requests a minute). Trend, moving-average, RSI and HV "
                         "filters fill in as they load." % (format(self._pending_days(), ","),
                                                            format(len(self._days), ","),
                                                            _dur(self._stock_eta_s()), self._spm()))
        elif name == "fill":
            parts.append("Loading daily prices for %s newly listed underlyings." % format(len(self._fill) + 1, ","))
        elif name == "technicals":
            parts.append("Computing trend, moving averages, RSI and HV for %s underlyings."
                         % format(self.universe_n, ","))
        if self._earn_future is not None:
            parts.append("Loading earnings dates for the next %d days." % EARNINGS_DAYS)
        return DETAIL_JOIN.join(parts)

    def _hist_text(self, now: _dt.datetime) -> str:
        eta = self._history_eta_s(now)
        text = "Building IV history: %s of %s underlyings done" % (format(self.history_done_n, ","),
                                                                  format(self.history_total, ","))
        text += (" (about %s left)." % _dur(eta)) if eta is not None else "."
        return text + " IV Rank shows - for an underlying until its history is in."

    def _next_pass_et(self, now: _dt.datetime) -> _dt.datetime:
        et = clock.et_now(now)
        d, t = et.date(), et.time()

        def at(day, tm):
            return _dt.datetime.combine(day, tm, tzinfo=et.tzinfo)

        if clock.is_trading_day(d):
            if t < RTH_START:
                return at(d, RTH_START)
            if t < RTH_END:
                if self._last_cycle is not None and self._last_cycle_session == d.isoformat():
                    nxt = clock.et_now(self._last_cycle + _dt.timedelta(minutes=self.cycle_min))
                    return nxt if nxt.time() < RTH_END else at(d, EOD_AFTER)
                return et
            if t < EOD_AFTER or (self._last_eod or "") < d.isoformat():
                return at(d, EOD_AFTER) if t < EOD_AFTER else et
        return at(clock.next_trading_day(d), RTH_START)

    def _idle_text(self, now: _dt.datetime) -> str:
        if not self.universe_n:
            return WAITING_FOR_LIST
        lp = self.last_pass or {}
        if lp.get("kind") == "eod" and lp.get("session"):
            up = "Up to date with the %s close" % _day_words(lp["session"])
        elif isinstance(lp.get("started"), _dt.datetime):
            up = "Up to date with the %s ET read" % _when_words(clock.et_now(lp["started"]))
        else:
            up = "No market pass yet"
        return "%s; next market pass %s ET." % (up, _when_words(self._next_pass_et(now)))

    # ── the tick ──

    def _collect(self, db) -> None:
        # the universe first (a pass grows with it); the pass last, so a pause it raises is
        # not cleared by a lane request that was sent before it
        self._collect_uni()
        self._collect_bg()
        self._collect_earn()
        self._collect_history()
        self._collect_pass(db)

    def _set_state(self, now: _dt.datetime) -> None:
        """The state word (the 8 the tray knows) and the plain-English detail; jobs running
        at once are joined with " · " - the pass, then the universe, then the stock lane."""
        parts, state = [], None
        p = self._pass
        if p is not None:
            state = "pass"
            parts.append(self._pass_text(p, now))
        if self._uni_future is not None:
            state = state or "universe"
            parts.append(self._uni_text(now))
        bg = self._bg_text()
        if bg:
            state = state or "stocks"
            parts.append(bg)
        if self._hist:
            state = state or "history"
            parts.append(self._hist_text(now))
        if state is None:
            self.state, self.detail = "idle", self._idle_text(now)
        else:
            self.state, self.detail = state, DETAIL_JOIN.join(parts)

    def _step(self, db, now: _dt.datetime) -> None:
        syms = self._refresh_counts(db, now)
        if not self._ready(now):
            return
        self._uni_resumed()
        p = self._pass
        if p is not None and not p.one_off and not p.complete_list:
            if self._uni_future is not None and self._uni_stream:
                p.grow = True
            if p.grow:
                self._grow_pass(p, syms, now, db)
        if self._pass is None:
            due = self._pass_due(now, syms)
            if due is not None:
                self._start_pass(db, due[0], due[1], syms, now)
        self._uni_tick(now)
        self._bg_tick(db, now)
        self._earn_tick(now)
        if self._pass is None and self._pass_due(now, syms) is None:
            self._history_tick(db, now, syms)

    def tick(self) -> str:
        """One step of the loop; never raises. Returns the state as reported."""
        now = self._now()
        db = self._sf()
        try:
            try:
                self._restore(db)
                self._collect(db)
                self._step(db, now)
                self._collect(db)              # an inline executor finished everything already
                self._close_retired()
                self._set_state(self._now())
            except Exception as exc:  # noqa: BLE001 - a bug or a DB failure must not stop the loop
                self._rollback(db)
                self.state = "error"
                self.last_error = ("tick failed: %s" % self._err_text(exc))[:500]
                self.detail = self.last_error
                self.log.exception("tick failed")
            self._beat(db)
            return self.shown_state
        finally:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass

    def run_forever(self, *, max_ticks: int | None = None) -> None:
        """Tick every ``tick_s`` until Ctrl+C (or ``max_ticks``)."""
        n = 0
        try:
            while True:
                t0 = self._now()
                self.tick()
                n += 1
                if max_ticks is not None and n >= max_ticks:
                    break
                spent = (self._now() - t0).total_seconds()
                self._sleep(max(1.0, self.tick_s - spent))
        except KeyboardInterrupt:
            self.log.info("stopping (interrupted)")
        finally:
            self.stop()

    # ── one-off runs (the CLI) ──

    def _begin_one_off(self, db) -> bool:
        self._retry_at.clear()
        self._restore(db)
        self._refresh_counts(db, self._now())
        self._beat(db)
        return self._ready(self._now(), force=True)

    def _one_off_result(self, out: dict) -> dict:
        scope = self._alert_scope()
        out["ok"] = scope is None
        if scope is not None:
            out["error"] = self._alerts[scope][1]
            out["error_kind"] = self._alerts[scope][0]
        return out

    @staticmethod
    def _failed_result(out: dict, res: dict, default: str) -> dict:
        """A one-off whose job failed without an alert (a warning, an empty list): ok False."""
        out["ok"] = False
        out.setdefault("error", res.get("error") or default)
        out.setdefault("error_kind", res.get("kind") or "error")
        return out

    def _wait(self, db, futures, *, collect) -> None:
        """Wait for ``futures`` while heartbeating every ``HEARTBEAT_EVERY_S``."""
        futures = list(futures)
        while futures:
            cf.wait(futures, timeout=HEARTBEAT_EVERY_S, return_when=cf.ALL_COMPLETED)
            collect()
            futures = [f for f in futures if not f.done()]
            self._set_state(self._now())
            self._beat(db)

    def run_universe(self) -> dict:
        """Refresh the universe now. Returns ``{"ok", "n", "new", "inactive"}`` (+ error);
        ``ok`` is False when the walk failed, alert or not (exit 2)."""
        db = self._sf()
        try:
            if not self._begin_one_off(db):
                return self._one_off_result({"n": 0})
            self.state, self.detail = "universe", "refreshing the list of optionable stocks (one-off)"
            self._beat(db)
            res = self._bg_wrap("universe", self.job_universe)
            self._bg_result("universe", res)
            self._refresh_counts(db, self._now())
            self.state, self.detail = "idle", "universe (one-off): %s" % _plural(self.universe_n, "underlying")
            out = self._one_off_result({"n": res.get("n", 0), "new": res.get("new", 0),
                                        "inactive": res.get("inactive", 0)})
            if not res.get("ok"):
                self._failed_result(out, res, "the list of optionable stocks could not be read")
            return out
        finally:
            self._beat(db)
            db.close()

    def run_once(self, kind: str = "manual") -> dict:
        """One full market pass now, whatever the clock (the universe first when there is
        none). ``kind`` ``manual`` files the session the read describes; ``eod`` files
        the end-of-day pass's session and the day's IV30. Returns ``{"ok", "pass_id",
        "session", "symbols", "read", "failed", "contracts"}`` (+ ``error``); a pass
        stopped by a Massive error is not recorded as finished; a pass that read nothing
        (or a universe read that failed) is ``ok`` False (exit 2)."""
        out = {"pass_id": None, "session": None, "symbols": 0, "read": 0, "failed": 0, "contracts": 0}
        db = self._sf()
        try:
            if not self._begin_one_off(db):
                return self._one_off_result(out)
            now = self._now()
            syms = self._stored_symbols(db, now)
            if not syms:
                res = self._bg_wrap("universe", self.job_universe)
                self._bg_result("universe", res)
                syms = self._refresh_counts(db, self._now())
                if not syms:
                    out = self._one_off_result(out)
                    if out["ok"]:
                        self._failed_result(out, res, "the list of optionable stocks could not be read")
                    return out
            session = (eod_day_for(now) or clock.et_today(now)) if kind == "eod" else snapshot_session(now)
            p = self._start_pass(db, kind, session, syms, now, one_off=True)
            if p is None:
                return self._failed_result(self._one_off_result(out), {"kind": "error"},
                                           "every underlying's chain was refused in the last 24 h")
            out.update(pass_id=p.id, session=session, symbols=p.total)
            while self._pass is p:
                self._wait(db, list(p.futures), collect=lambda: self._collect_pass(db))
                if self._pass is p and not p.futures:
                    # paused (a Massive error): a one-off stops here
                    self._finish_pass(db, p, finished=False)
            out.update(read=p.ok, failed=p.failed + len(p.queue), contracts=p.contracts)
            self.state, self.detail = "idle", "%s pass %d (one-off): %d read, %d failed" % (
                kind, p.id, p.ok, out["failed"])
            out = self._one_off_result(out)
            if out["ok"] and (p.ok == 0 or p.contracts == 0):
                self._failed_result(out, {"kind": "empty"}, "the %s pass read no option data (%d read, %d contracts)"
                                    % (kind, p.ok, p.contracts))
            return out
        finally:
            self._beat(db)
            db.close()

    def run_eod(self) -> dict:
        """The end-of-day pass now (``run_once(kind="eod")``)."""
        return self.run_once(kind="eod")

    def run_history(self, symbols) -> dict:
        """The IV history of ``symbols`` now, even when done before (overwrites the stored
        series). A symbol with under 20 stored closes is skipped (nothing read, nothing
        marked). Returns ``{"ok", "symbols", "done", "failed", "skipped"}`` (+ ``error``);
        ``nothing`` is True when no history came out done (the CLI's exit 3)."""
        syms: list[str] = []
        for s in symbols or ():
            s = str(s or "").strip().upper()
            if s and s not in syms:
                syms.append(s)
        out = {"symbols": len(syms), "done": 0, "failed": 0, "skipped": 0}
        db = self._sf()
        try:
            if not self._begin_one_off(db):
                return self._one_off_result(out)
            try:
                self._load_days(db, self._now())
            except Exception:  # noqa: BLE001 - only the "sessions to go" figure needs it
                self._rollback(db)
            go: list[str] = []
            for s in syms:
                n = len(scr_store.raw_bars(db, s, HISTORY_SESSIONS))
                if n < HISTORY_MIN_POINTS:
                    out["skipped"] += 1
                    self.log.warning("IV history %s skipped: %d stored closes, need %d - the stock bars are not "
                                     "loaded yet (%s to go)", s, n, HISTORY_MIN_POINTS,
                                     _plural(self._pending_days(), "session"))
                    continue
                go.append(s)
            pool = self._workers_pool()
            futs = {pool.submit(self._history_job, s, True): s for s in go}
            self._hist.update(futs)
            results: list[dict] = []
            self._wait(db, futs, collect=lambda: results.extend(self._collect_history()))
            results.extend(self._collect_history())
            for r in results:
                if r.get("ok") and r.get("done"):
                    out["done"] += 1
                else:
                    out["failed"] += 1
                    if r.get("ok"):
                        self.log.warning("IV history %s not done: %d IV points from %d closes", r.get("symbol"),
                                         r.get("points", 0), r.get("bars", 0))
            self.state, self.detail = "idle", "IV history (one-off): %d done, %d failed, %d skipped" % (
                out["done"], out["failed"], out["skipped"])
            out = self._one_off_result(out)
            out["nothing"] = out["done"] == 0
            return out
        finally:
            self._beat(db)
            db.close()

    def wait_idle(self, timeout: float = 60.0) -> None:
        """Wait for every job in flight (tests and one-offs)."""
        futs = list(self._hist)
        if self._pass is not None:
            futs += list(self._pass.futures)
        for f in ((self._bg[1] if self._bg is not None else None), self._uni_future, self._earn_future):
            if f is not None:
                futs.append(f)
        if futs:
            cf.wait(futs, timeout=timeout)

    def stop(self, reason: str = "stopped") -> None:
        """Heartbeat ``stopped``, let the jobs in flight end, close the client this object
        made (and any it replaced)."""
        self._stopping.set()
        if self._pass is not None:
            self._pass.halt.set()
        for pool in (self._pool, self._bgpool, self._unipool, self._earnpool):
            if pool is not None:
                try:
                    pool.shutdown(wait=False, cancel_futures=True)
                except TypeError:     # an executor without cancel_futures
                    pool.shutdown(wait=False)
                except Exception:  # noqa: BLE001
                    pass
        self._pool = self._bgpool = self._unipool = self._earnpool = None
        self.state = "stopped"
        self.detail = reason
        try:
            db = self._sf()
            try:
                self._beat(db)
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            self.log.warning("final heartbeat failed: %s", exc)
            self._write_state(self._fields(), self._now())
        self._close_retired(force=True)
        if self._client_factory is not None and self.client is not None:
            self._close_client(self.client)
            self.client = None


__all__ = ["Collector", "InlineExecutor", "default_client", "contract_rows", "snapshot_session",
           "eod_day_for", "stock_days", "plan_start", "sec_type_of", "exchange_of", "classify_trend",
           "env_cycle_min", "env_workers", "env_max_rps", "env_max_dte", "STATE_PATH",
           "COLLECTOR_VERSION", "NO_KEY_TEXT", "UNIVERSE_EMPTY_TEXT", "PROGRESS_KEYS", "TICK_S",
           "RTH_START", "RTH_END", "EOD_AFTER"]
