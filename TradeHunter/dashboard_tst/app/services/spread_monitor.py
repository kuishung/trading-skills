"""Turn a tracked spread + a live chain into today's verdict.

This is the I/O half of the monitor. ``bull_put.spread_math`` and
``bull_put.monitor`` are pure and hold the rules; everything that touches the
network or the database lives here, so the rules stay unit-testable against
synthetic numbers and this module stays the only place that can be slow or fail.

Marking a credit spread
-----------------------
A bull put spread is CLOSED by paying a debit: buy back the short put, sell the
long one. So::

    cost to close (per share) = short_put_price - long_put_price
    unrealised P/L per share  = credit_received - cost_to_close
    unrealised P/L ($)        = that x 100 x contracts

Two marks are computed from the same chain:

* **mid** — mid of each leg. This is what a broker marks the position at and what
  the loss line is graded on. Using the worst case here would fire the 20% stop
  on wide quotes rather than on an actual loss, which on an illiquid chain is a
  meaningful difference.
* **worst** — pay the short's ask, hit the long's bid. What an immediate exit
  would really cost, shown alongside so the number is never a surprise.

Nothing here places, modifies or cancels an order.
"""
from __future__ import annotations

import datetime as _dt

from . import bull_put, option_quotes


def _dte(expiry: str, today: _dt.date | None = None) -> int:
    try:
        return (_dt.date.fromisoformat(expiry) - (today or _dt.date.today())).days
    except (TypeError, ValueError):
        return 0


def snapshot(*, symbol: str, expiry: str, short_strike: float, long_strike: float,
             credit: float, contracts: int = 1, roll_delta: float | None = None,
             loss_fraction: float | None = None, chain: dict | None = None,
             today: _dt.date | None = None,
             profit_target: float | None = None,
             dte_floor: int | None = None) -> dict:
    """Everything the Portfolio row needs for one spread, quotes included.

    ``chain`` may be passed in so a sweep fetches each underlying once for all of
    that member's positions on it. Never raises: a failure becomes ``error`` on
    the result and the geometry (max loss, breakeven, DTE) is still returned,
    because a position you cannot price is still a position you must see.
    """
    rd = bull_put.ROLL_DELTA if roll_delta is None else float(roll_delta)
    lf = bull_put.LOSS_STOP_FRACTION if loss_fraction is None else float(loss_fraction)
    # Winning-side lines: None = the playbook default; 0 = switched off.
    pt = bull_put.PROFIT_TARGET_FRACTION if profit_target is None else float(profit_target)
    pt = pt if pt > 0 else None
    df = bull_put.DTE_FLOOR if dte_floor is None else int(dte_floor)
    df = df if df > 0 else None
    grade = dict(max_loss=None, roll_delta=rd, loss_fraction=lf,
                 profit_target=pt, dte_floor=df)

    math = bull_put.spread_math(short_strike=short_strike, long_strike=long_strike,
                                credit=credit, contracts=contracts)
    dte = _dte(expiry, today)

    out = {
        "symbol": (symbol or "").upper(), "expiry": expiry, "dte": dte,
        "short_strike": float(short_strike), "long_strike": float(long_strike),
        **math,
        "spot": None, "short_delta": None, "short_iv": None, "iv30": None,
        "long_delta": None, "long_iv": None, "net_delta": None, "theta": None,
        "profit_pct": None,
        "mark": None, "mark_worst": None, "pl": None, "pl_worst": None,
        "short_leg": None, "long_leg": None,
        "lines": {"roll_delta": rd, "loss_fraction": lf,
                  "profit_target": pt, "dte_floor": df},
        "source": "cboe", "as_of": None, "error": None,
        "verdict": None,
    }

    # Expired: no chain will carry it, and grading it against a delta line is
    # meaningless. Say so plainly instead of rendering a broken row.
    if dte < 0:
        out["error"] = "expired"
        out["verdict"] = {
            "state": "EXPIRED", "urgent": False, "reasons": [], "loss_pct": None,
            "profit_pct": None, "delta_breach": False, "loss_breach": False,
            "profit_breach": False, "dte_breach": False,
            "action": f"Expired {abs(dte)}d ago. Mark it closed to take it off the board.",
        }
        return out
    grade["max_loss"] = math["max_loss"]
    grade["max_profit"] = math["max_profit"]

    try:
        ch = chain if chain is not None else option_quotes.fetch_chain(symbol)
    except option_quotes.ChainError as exc:
        out["error"] = str(exc)
        out["verdict"] = bull_put.monitor(short_delta=None, dte=dte, pl=None, **grade)
        return out

    out["spot"] = ch.get("spot")
    out["iv30"] = ch.get("iv30")
    out["as_of"] = ch.get("as_of")

    s = option_quotes.leg(ch, expiry, "P", short_strike)
    l = option_quotes.leg(ch, expiry, "P", long_strike)
    if s is None or l is None:
        missing = []
        if s is None:
            missing.append(f"{short_strike:g}P")
        if l is None:
            missing.append(f"{long_strike:g}P")
        # Naming the listed expiries turns "it's broken" into "you typed 09-18
        # but the chain has 09-19", which is the actual mistake nine times in ten.
        exps = option_quotes.expiries(ch, "P")
        hint = ""
        if expiry not in exps:
            near = [e for e in exps if e >= _dt.date.today().isoformat()][:4]
            hint = f" — {expiry} is not a listed expiry; nearest: {', '.join(near)}"
        out["error"] = f"not on the chain: {', '.join(missing)}{hint}"
        out["verdict"] = bull_put.monitor(short_delta=None, dte=dte, pl=None, **grade)
        return out

    out["short_leg"], out["long_leg"] = s, l
    out["short_delta"] = None if s.get("delta") is None else abs(s["delta"])
    out["short_iv"] = s.get("iv")
    out["long_delta"] = None if l.get("delta") is None else abs(l["delta"])
    out["long_iv"] = l.get("iv")

    n = math["contracts"]
    # Position greeks, in the units a trader reads them in. The chain's delta is
    # signed per share held LONG (a put's is negative); we are short the near put
    # and long the far one, so the position is -short + long. A bull put spread
    # therefore always comes out positive delta ("behaves like N long shares")
    # and, on a normal day, positive theta ("earns $X a day").
    if s.get("delta") is not None and l.get("delta") is not None:
        out["net_delta"] = (l["delta"] - s["delta"]) * 100.0 * n
    if s.get("theta") is not None and l.get("theta") is not None:
        out["theta"] = (l["theta"] - s["theta"]) * 100.0 * n
    if s.get("mid") is not None and l.get("mid") is not None:
        out["mark"] = s["mid"] - l["mid"]
        out["pl"] = (math["credit"] - out["mark"]) * 100.0 * n
        if math["credit"] > 0:
            out["profit_pct"] = (math["credit"] - out["mark"]) / math["credit"]
    if s.get("ask") is not None and l.get("bid") is not None:
        out["mark_worst"] = s["ask"] - l["bid"]
        out["pl_worst"] = (math["credit"] - out["mark_worst"]) * 100.0 * n

    out["verdict"] = bull_put.monitor(short_delta=out["short_delta"], dte=dte,
                                      pl=out["pl"], **grade)
    return out


def snapshot_rows(rows, prefs: dict | None = None,
                  today: _dt.date | None = None) -> list[dict]:
    """Snapshot a list of ``OptionSpread`` rows, one chain fetch per underlying.

    Returns ``[{row, snap}, ...]`` in the order given. Per-row thresholds win over
    the member's defaults, which win over the playbook's.
    """
    prefs = prefs or {}
    rd_default = prefs.get("roll_delta", bull_put.ROLL_DELTA)
    lf_default = prefs.get("loss_fraction", bull_put.LOSS_STOP_FRACTION)
    pt_default = (prefs["profit_target_pct"] / 100.0 if "profit_target_pct" in prefs
                  else bull_put.PROFIT_TARGET_FRACTION)
    df_default = prefs.get("dte_floor", bull_put.DTE_FLOOR)

    chains: dict[str, dict | None] = {}
    out = []
    for r in rows:
        sym = (r.symbol or "").upper()
        if sym not in chains:
            try:
                chains[sym] = option_quotes.fetch_chain(sym)
            except option_quotes.ChainError:
                chains[sym] = None
        out.append({"row": r, "snap": snapshot(
            symbol=sym, expiry=r.expiry, short_strike=r.short_strike,
            long_strike=r.long_strike, credit=r.credit or 0.0,
            contracts=r.contracts or 1,
            roll_delta=(r.roll_delta if getattr(r, "roll_delta", None) else rd_default),
            loss_fraction=(r.loss_stop_pct / 100.0
                           if getattr(r, "loss_stop_pct", None) else lf_default),
            profit_target=(r.profit_target_pct / 100.0
                           if getattr(r, "profit_target_pct", None) is not None
                           else pt_default),
            dte_floor=(r.dte_floor if getattr(r, "dte_floor", None) is not None
                       else df_default),
            chain=chains[sym], today=today,
        )})
    return out


# ------------------------------------------------------------------ recording
# Snapshots are worth keeping. The daily sweep writes one row per spread per day
# so the page can draw a series; a manual refresh overwrites that day's row
# rather than appending, so opening the page ten times does not invent ten
# observations.

def et_today() -> str:
    """Today in EXCHANGE time, as YYYY-MM-DD.

    The member trades US options from Malaysia (UTC+8), so for most of their
    working day the local date is already tomorrow in New York. Keying the daily
    check on the local date would file two checks under one trading day and none
    under the next.
    """
    return _et_now().date().isoformat()


def _et_now() -> _dt.datetime:
    """Now in New York. ``zoneinfo`` needs the ``tzdata`` package on Windows
    (there is no system zone database), and on the box that lacked it this call
    took the whole Portfolio board down with a 500 (v4.62-v4.65, Hermes). The
    fallback applies US daylight-saving by rule - second Sunday of March to first
    Sunday of November, 02:00 local - so the date is right even without the
    package; tzdata is in requirements.txt as well so the fallback stays unused.
    """
    try:
        from zoneinfo import ZoneInfo

        return _dt.datetime.now(ZoneInfo("America/New_York"))
    except Exception:  # noqa: BLE001 - ZoneInfoNotFoundError, or no zoneinfo at all
        pass
    utc = _dt.datetime.now(_dt.timezone.utc)
    y = utc.year

    def _nth_sunday(month: int, n: int) -> _dt.date:
        d = _dt.date(y, month, 1)
        d += _dt.timedelta(days=(6 - d.weekday()) % 7)      # first Sunday
        return d + _dt.timedelta(weeks=n - 1)

    # DST switches at 02:00 local = 07:00 UTC (EST) going in, 06:00 UTC (EDT) going out
    start = _dt.datetime.combine(_nth_sunday(3, 2), _dt.time(7), _dt.timezone.utc)
    end = _dt.datetime.combine(_nth_sunday(11, 1), _dt.time(6), _dt.timezone.utc)
    offset = -4 if start <= utc < end else -5
    return utc.astimezone(_dt.timezone(_dt.timedelta(hours=offset)))


def record_check(db, spread, snap: dict, *, source: str = "cboe",
                 on: str | None = None):
    """Upsert today's ``SpreadCheck`` for one spread. Returns the row.

    Portable upsert (query-then-update/insert), not ``INSERT OR REPLACE`` — the
    platform data-handling rule is that the SQLite -> Postgres swap stays a
    config change.
    """
    from ..models import SpreadCheck

    day = on or et_today()
    v = snap.get("verdict") or {}
    row = (db.query(SpreadCheck)
             .filter(SpreadCheck.spread_id == spread.id,
                     SpreadCheck.checked_on == day)
             .one_or_none())
    if row is None:
        row = SpreadCheck(spread_id=spread.id, checked_on=day)
        db.add(row)

    row.spot = snap.get("spot")
    row.short_delta = snap.get("short_delta")
    row.short_iv = snap.get("short_iv")
    row.long_delta = snap.get("long_delta")
    row.long_iv = snap.get("long_iv")
    row.net_delta = snap.get("net_delta")
    row.theta = snap.get("theta")
    row.profit_pct = snap.get("profit_pct")
    row.mark = snap.get("mark")
    row.pl = snap.get("pl")
    row.loss_pct = v.get("loss_pct")
    row.dte = snap.get("dte")
    row.state = v.get("state") or "UNKNOWN"
    row.action = v.get("action")
    row.source = source
    row.error = snap.get("error")
    return row


def sweep(db, *, user_id: int | None = None, on: str | None = None,
          fresh: bool = True) -> dict:
    """Check every OPEN tracked spread and record the result.

    ``user_id`` limits the sweep to one member (the page's manual refresh);
    omitted, it covers everyone (the nightly job). ``fresh`` drops the quote
    cache first, which is what the nightly job wants and a page refresh does not.

    Returns a summary — counts by state plus the rows that need action — so the
    caller can log one line instead of the whole board.
    """
    from ..models import OptionSpread

    if fresh:
        option_quotes.clear_cache()

    q = db.query(OptionSpread).filter(OptionSpread.status == "open")
    if user_id is not None:
        q = q.filter(OptionSpread.user_id == user_id)
    rows = q.order_by(OptionSpread.symbol).all()

    day = on or et_today()
    states: dict[str, int] = {}
    actionable = []
    # Each member's own defaults apply to their rows, exactly as on the page.
    # Grouped by user so the chain cache still serves one fetch per underlying.
    from . import trade_prefs as _tp

    items = []
    by_user: dict[int, list] = {}
    for r in rows:
        by_user.setdefault(r.user_id, []).append(r)
    for uid, urows in by_user.items():
        items.extend(snapshot_rows(urows, prefs=_tp.read(urows[0].user)))

    for item in items:
        r, snap = item["row"], item["snap"]
        # The sweep records EVERY row, un-priceable ones included. It is the
        # authoritative daily job, so "we tried this position today and could not
        # price it" is a fact worth keeping — it is how a permanently broken
        # ticker becomes visible instead of just missing.
        #
        # The page-load path (routes/portfolio._list_context) deliberately does
        # the opposite and skips those, because it must not overwrite the night's
        # good reading with an UNKNOWN caused by a transient Cboe hiccup while
        # somebody happened to be browsing.
        record_check(db, r, snap, source="cboe", on=day)
        st = (snap.get("verdict") or {}).get("state") or "UNKNOWN"
        states[st] = states.get(st, 0) + 1
        if (snap.get("verdict") or {}).get("urgent"):
            actionable.append({
                "user_id": r.user_id, "symbol": r.symbol, "expiry": r.expiry,
                "short_strike": r.short_strike, "long_strike": r.long_strike,
                "contracts": r.contracts, "dte": snap.get("dte"),
                "short_delta": snap.get("short_delta"), "pl": snap.get("pl"),
                "profit_pct": snap.get("profit_pct"),
                "loss_pct": (snap.get("verdict") or {}).get("loss_pct"),
                "state": st, "action": (snap.get("verdict") or {}).get("action"),
            })
    db.commit()
    return {"checked_on": day, "spreads": len(rows), "states": states,
            "actionable": actionable}
