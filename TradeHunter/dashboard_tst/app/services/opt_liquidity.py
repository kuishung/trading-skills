"""Option liquidity per TICKER - "is this name's option market liquid enough?" (v4.186).

The user (2026-10-21): *"I do not want to compare against each other on the watchlist. I want
to know the ticker is liquid enough."* So this is an ABSOLUTE verdict with fixed, member-editable
thresholds - not a rank (the per-contract rank among a chain lives in ``vol_surface.liquidity_ranks``).

Judged where a trade would happen, not across the whole chain:

- **near-the-money open interest**: the median open interest of the contracts within ``BAND``
  (10 %) of the price in the first ``N_MONTHLIES`` (2) standard monthly expiries - the figure that
  decides whether an order fills at size;
- **near-the-money volume today**: the day's contracts traded across those same contracts; before
  the open (nothing traded yet) the previous session's final volume stands in, and the verdict says so;
- **chain open interest**: calls plus puts over every contract of the name, a sanity check.

Three levels - ``liquid`` / ``fair`` / ``thin`` - and a name must clear ALL THREE figures for a
level. The verdict names the figure that held it back from the level above. The defaults are
practitioner rules of thumb, not a standard, so each member can change them (``User.prefs``,
keys ``liq_*``). Data: the screener's last market pass (Massive Options Starter - open interest is
yesterday's, volume 15 min delayed, no bid/ask so the spread stays unknown).
"""
from __future__ import annotations

import datetime as _dt
import statistics

BAND = 0.10                  # near the money = strike within 10 % of the price
N_MONTHLIES = 2              # the first two standard monthly expiries from today

DEFAULTS = {
    "liquid_oi": 1000,       # near-the-money median open interest
    "liquid_vol": 5000,      # near-the-money volume today
    "liquid_chain": 100_000, # chain open interest, calls + puts
    "fair_oi": 250,
    "fair_vol": 1000,
    "fair_chain": 20_000,
}
LIMITS = (0, 1_000_000_000)
LABELS = {"liquid": "Liquid", "fair": "Fair", "thin": "Thin"}
FIGURES = (("oi", "near-the-money open interest"), ("vol", "near-the-money volume"), ("chain", "chain open interest"))


def _int(v, default: int) -> int:
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return default
    return min(LIMITS[1], max(LIMITS[0], n))


def read_thresholds(user) -> dict:
    """This member's thresholds, always complete and in bounds (defaults where unset)."""
    p = getattr(user, "prefs", None) or {}
    return {k: _int(p.get("liq_" + k), d) for k, d in DEFAULTS.items()}


def write_thresholds(db, user, **changes) -> tuple[dict, str]:
    """Update the supplied thresholds. Returns (thresholds, error); a bad value is reported,
    nothing is written. A "fair" line may not sit above its "liquid" line."""
    cur = read_thresholds(user)
    new = dict(cur)
    for key, value in changes.items():
        if key not in DEFAULTS or value is None or value == "":
            continue
        try:
            n = int(float(value))
        except (TypeError, ValueError):
            return cur, "%s must be a whole number" % key.replace("_", " ")
        if n < LIMITS[0] or n > LIMITS[1]:
            return cur, "%s must be between %d and %d" % (key.replace("_", " "), LIMITS[0], LIMITS[1])
        new[key] = n
    for fig, _words in FIGURES:
        if new["fair_" + fig] > new["liquid_" + fig]:
            return cur, "the Fair line for %s cannot be above the Liquid line" % dict(FIGURES)[fig]
    p = dict(getattr(user, "prefs", None) or {})
    for k, v in new.items():
        p["liq_" + k] = v
    user.prefs = p
    db.add(user)
    db.commit()
    return new, ""


def _to_date(x) -> _dt.date | None:
    if x is None:
        return None
    if isinstance(x, _dt.datetime):
        return x.date()
    if isinstance(x, _dt.date):
        return x
    try:
        return _dt.date.fromisoformat(str(x)[:10])
    except ValueError:
        return None


def _fmt(n) -> str:
    if n is None:
        return "-"
    n = float(n)
    if abs(n) >= 1e6:
        return ("%.1fM" % (n / 1e6)).replace(".0M", "M")
    if abs(n) >= 1e4:
        return "%dk" % round(n / 1e3)
    if abs(n) >= 1e3:
        return ("%.1fk" % (n / 1e3)).replace(".0k", "k")
    return "%d" % round(n)


def grade(near_oi, near_vol, chain_oi, thresholds: dict | None = None) -> tuple[str, str | None]:
    """``(level, held_back_by)`` from the three figures. None for a figure counts as zero."""
    t = {**DEFAULTS, **(thresholds or {})}
    figs = {"oi": near_oi or 0, "vol": near_vol or 0, "chain": chain_oi or 0}

    def fails(level: str) -> str | None:
        for fig, words in FIGURES:
            if figs[fig] < t[level + "_" + fig]:
                return words
        return None

    miss = fails("liquid")
    if miss is None:
        return "liquid", None
    if fails("fair") is None:
        return "fair", miss
    return "thin", fails("fair")


def verdict(sdb, symbol: str, thresholds: dict | None = None, today: _dt.date | None = None) -> dict | None:
    """The verdict for one ticker from the screener's last pass, or None when the screener does
    not list it. ``{symbol, level, label, near_oi, near_vol, vol_src ('today' | 'prev'), chain_oi,
    n_near, expiries, held, text, short}`` - ``text`` is the hover sentence, ``short`` the figure
    shown beside the label ("1.2k OI")."""
    from ..screener_models import ScrContract as C, ScrUnderlying as U  # noqa: PLC0415

    sym = str(symbol or "").strip().upper()
    u = sdb.query(U).filter(U.symbol == sym).first()
    if u is None:
        return None
    t = {**DEFAULTS, **(thresholds or {})}
    d = today or _dt.date.today()
    spot = float(u.spot) if u.spot else None
    chain_oi = (int(u.call_oi or 0) + int(u.put_oi or 0)) or None
    if chain_oi is None:
        chain_oi = sdb.query(C.oi).filter(C.symbol == sym).all()
        chain_oi = sum(int(x or 0) for (x,) in chain_oi) or None
    exps = [e for (e,) in sdb.query(C.expiry).filter(C.symbol == sym, C.weekly.is_(False))
            .distinct().order_by(C.expiry).all() if e and _to_date(e) and _to_date(e) >= d][:N_MONTHLIES]
    near_oi = near_vol = None
    vol_src = "today"
    n_near = 0
    if spot and exps:
        lo, hi = spot * (1 - BAND), spot * (1 + BAND)
        rows = (sdb.query(C.oi, C.volume, C.vol_prev)
                   .filter(C.symbol == sym, C.expiry.in_(exps), C.strike >= lo, C.strike <= hi).all())
        n_near = len(rows)
        if rows:
            near_oi = int(statistics.median([int(o or 0) for (o, _v, _p) in rows]))
            v_today = sum(int(v or 0) for (_o, v, _p) in rows)
            v_prev = sum(int(p or 0) for (_o, _v, p) in rows)
            if v_today > 0 or v_prev == 0:
                near_vol = v_today
            else:
                near_vol, vol_src = v_prev, "prev"
    level, held = grade(near_oi, near_vol, chain_oi, t)
    label = LABELS[level]
    when = "today" if vol_src == "today" else "the previous session (nothing traded yet today)"
    parts = ["%s option liquidity: %s." % (sym, label)]
    if n_near:
        parts.append("Near the money (within 10%% of %.2f, %s): median open interest %s, volume %s %s."
                     % (spot, " and ".join(exps), _fmt(near_oi), _fmt(near_vol), when))
    elif spot and exps:
        parts.append("No contract within 10%% of %.2f in %s." % (spot, " and ".join(exps)))
    elif not spot:
        parts.append("No price for the name in the last pass.")
    else:
        parts.append("No standard monthly expiry ahead in the last pass.")
    parts.append("Chain open interest %s." % _fmt(chain_oi))
    if held:
        nxt = "Liquid" if level == "fair" else "Fair"
        parts.append("Held back from %s by the %s." % (nxt, held))
    parts.append("Lines: Liquid >= %s / %s / %s, Fair >= %s / %s / %s (near OI / near volume / chain OI); change them under the watchlist."
                 % (_fmt(t["liquid_oi"]), _fmt(t["liquid_vol"]), _fmt(t["liquid_chain"]),
                    _fmt(t["fair_oi"]), _fmt(t["fair_vol"]), _fmt(t["fair_chain"])))
    parts.append("Open interest is yesterday's; volume is 15 min delayed; the spread is not in this data.")
    return {"symbol": sym, "level": level, "label": label, "near_oi": near_oi, "near_vol": near_vol,
            "vol_src": vol_src, "chain_oi": chain_oi, "n_near": n_near, "expiries": exps, "held": held,
            "text": " ".join(parts), "short": (_fmt(near_oi) + " OI") if near_oi is not None else ""}


def verdicts(sdb, symbols, thresholds: dict | None = None, today: _dt.date | None = None) -> dict[str, dict]:
    """``{symbol: verdict}`` for the symbols the screener lists (the others are left out)."""
    out: dict[str, dict] = {}
    for s in symbols or ():
        sym = str(s or "").strip().upper()
        if not sym or sym in out:
            continue
        v = verdict(sdb, sym, thresholds, today)
        if v is not None:
            out[sym] = v
    return out


__all__ = ["BAND", "N_MONTHLIES", "DEFAULTS", "LIMITS", "LABELS", "FIGURES", "read_thresholds",
           "write_thresholds", "grade", "verdict", "verdicts"]
