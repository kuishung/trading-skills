"""Derived option metrics - PURE: HV, the ATM IV per expiry, IV30 constant
maturity, IV rank / percentile with its `state`, term structure, 25-delta skew,
expected move, days to earnings (design/options/part_A_data.md A3).

Every function takes plain lists / dicts (or Part A's ``ContractRow``) and
returns None rather than raising, so the whole layer is unit-testable on
synthetic data with no network - the ``spread_scan.build_candidates`` discipline.
Nothing here reads a database or a feed; ``all_for()`` is the one call the
nightly job and a card read make, and its dict is what ``iv_daily`` stores.

Units (OPTIONS_MODULE_DESIGN.md II.2.12): per-contract ``iv`` comes IN as a
FRACTION; every per-day statistic goes OUT in PERCENT (``hv20``, ``atm_iv``,
``iv30``, ``iv_front`` ...), the ``iv_history`` unit; ``term_ratio``,
``iv_hv_premium`` and ``skew_norm`` are unitless ratios; ``skew25`` is in vol
points. The IV-rank window is the last 252 daily readings with TODAY included
(the bridge's own min/max convention), oldest first.
"""
from __future__ import annotations

import datetime as _dt
import math
import statistics

from .opt_constants import (
    ATM_MAX_DIST_PCT,
    BACK_MIN_DTE,
    BACK_TARGET_DTE,
    CM_MIN_DTE,
    CM_TARGET_DAYS,
    EXPECTED_MOVE_DAYS,
    FRONT_MIN_DTE,
    FRONT_TARGET_DTE,
    HV_LONG,
    HV_SHORT,
    IV_FULL_OBS,
    IV_MIN_OBS,
    IV_RANK_MIN_OBS,
    IV_SANITY_HI,
    IV_SANITY_LO,
    SKEW_DELTA,
    SKEW_DELTA_TOL,
    TRADING_DAYS,
)

STATES = ("none", "forming", "pct_only", "rank_ok", "ok")
BASES = ("rank", "percentile", "provisional", "unknown")


# ---------------------------------------------------------------- helpers
def _num(v):
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def _g(row, key, default=None):
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _date(s) -> _dt.date | None:
    if isinstance(s, _dt.datetime):
        return s.date()
    if isinstance(s, _dt.date):
        return s
    try:
        return _dt.date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def closes_from_bars(bars) -> list[float]:
    """The closes of COMPLETED sessions, oldest first. A bar carrying
    ``session_frac`` (the session is still open - prices.fetch_daily_ohlc marks
    it) is excluded: an intraday Refresh must not compute HV on a half-day close.
    Accepts ``{"close"}`` (prices) or ``{"c"}`` (bars_store) bars."""
    out: list[float] = []
    for b in bars or ():
        if not isinstance(b, dict):
            continue
        if b.get("session_frac") is not None:
            continue
        c = _num(b.get("close", b.get("c")))
        if c is not None:
            out.append(c)
    return out


# --------------------------------------------------------------- A3.1 HV
def hv(closes: list[float], n: int) -> float | None:
    """Close-to-close historical volatility, annualised, PERCENT.

    r_i = ln(C_i / C_{i-1}) over the last ``n`` returns (needs n + 1 closes);
    HV = stdev(r, ddof=1) * sqrt(252) * 100. None if fewer than n + 1 valid closes
    or any close in the window <= 0.
    """
    try:
        n = int(n)
    except (TypeError, ValueError):
        return None
    if n < 2:
        return None
    vals = [_num(c) for c in (closes or ())]
    vals = [c for c in vals if c is not None]
    if len(vals) < n + 1:
        return None
    window = vals[-(n + 1):]
    if any(c <= 0 for c in window):
        return None
    rets = [math.log(window[i] / window[i - 1]) for i in range(1, len(window))]
    try:
        sd = statistics.stdev(rets)          # ddof = 1
    except statistics.StatisticsError:
        return None
    return sd * math.sqrt(TRADING_DAYS) * 100.0


# ---------------------------------------------- A3.2 ATM IV per expiry, IV30
def atm_iv_by_expiry(rows, spot: float, snap_on: str | None = None, *,
                     require_quote: bool = True) -> dict[str, dict]:
    """``{expiry: {"dte", "atm_iv" (PERCENT), "n_legs", "em_1sd"}}``.

    Per expiry: K1 = the highest strike <= spot, K2 = the lowest strike > spot
    (both must exist and sit within ATM_MAX_DIST_PCT of spot). Legs = the call
    and put at K1 and K2 with a sane iv and bid > 0 (a quote, not a stale print);
    at least two legs in all. ``require_quote=False`` drops the bid condition (a
    leg counts on a sane iv alone): Massive Options Starter has no bid/ask, its
    IV comes from the feed's own model (OPTIONS_V2_DESIGN.md §13.3). iv(K) = the mean of the call and put iv present at
    K; atm_iv = iv(K1) + (iv(K2) - iv(K1)) * (spot - K1) / (K2 - K1), then * 100.
    When spot sits exactly on K1, iv(K1) alone decides. ``em_1sd`` = spot *
    atm_iv / 100 * sqrt(dte / 365), the one-sigma move to that expiry.
    An expiry that cannot be read is simply absent from the result.
    """
    spot = _num(spot)
    if not spot or spot <= 0:
        return {}
    by_exp: dict[str, list] = {}
    for r in rows or ():
        exp = _g(r, "expiry")
        if exp:
            by_exp.setdefault(str(exp)[:10], []).append(r)
    out: dict[str, dict] = {}
    for exp in sorted(by_exp):
        group = by_exp[exp]
        strikes = sorted({_num(_g(r, "strike")) for r in group if _num(_g(r, "strike"))})
        below = [k for k in strikes if k <= spot]
        above = [k for k in strikes if k > spot]
        if not below or not above:
            continue
        k1, k2 = max(below), min(above)
        if k1 < spot * (1 - ATM_MAX_DIST_PCT) or k2 > spot * (1 + ATM_MAX_DIST_PCT):
            continue
        ivs: dict[float, list[float]] = {k1: [], k2: []}
        for r in group:
            k = _num(_g(r, "strike"))
            if k not in ivs:
                continue
            iv = _num(_g(r, "iv"))
            bid = _num(_g(r, "bid"))
            if iv is None or not (IV_SANITY_LO < iv < IV_SANITY_HI):
                continue
            if require_quote and (bid is None or bid <= 0):
                continue
            ivs[k].append(iv)
        n_legs = len(ivs[k1]) + len(ivs[k2])
        if n_legs < 2:
            continue
        iv1 = statistics.fmean(ivs[k1]) if ivs[k1] else None
        iv2 = statistics.fmean(ivs[k2]) if ivs[k2] else None
        w = (spot - k1) / (k2 - k1)
        if iv1 is not None and iv2 is not None:
            atm = iv1 + (iv2 - iv1) * w
        elif iv1 is not None and w == 0.0:
            atm = iv1
        else:
            continue
        dte = _g(group[0], "dte", None)
        if dte is None:
            d_exp, d_on = _date(exp), _date(snap_on)
            dte = (d_exp - d_on).days if (d_exp and d_on) else None
        atm_pct = atm * 100.0
        em = spot * atm * math.sqrt(dte / 365.0) if (dte is not None and dte > 0) else None
        out[exp] = {"dte": dte, "atm_iv": round(atm_pct, 4), "n_legs": n_legs,
                    "em_1sd": round(em, 4) if em is not None else None}
    return out


def iv30_constant_maturity(by_expiry: dict, snap_on: str | None = None) -> float | None:
    """Our own 30-day constant-maturity ATM IV (PERCENT) by interpolating in
    VARIANCE-TIME between the two expiries bracketing 30 calendar days (T = dte /
    365), the standard formula:
        sig30^2 = [sig1^2 T1 (T2 - T30) + sig2^2 T2 (T30 - T1)] / [(T2 - T1) T30]
    Expiries under CM_MIN_DTE are ignored. With nothing beyond 30 days the nearest
    at or under is used; with nothing under, the nearest above. None when no
    expiry qualifies. (``snap_on`` is accepted for the A3.2 signature; the dte on
    each entry already carries it.)"""
    items = []
    for exp, d in (by_expiry or {}).items():
        dte, sig = _num((d or {}).get("dte")), _num((d or {}).get("atm_iv"))
        if dte is None or sig is None or dte < CM_MIN_DTE or sig <= 0:
            continue
        items.append((dte, sig, exp))
    if not items:
        return None
    items.sort()
    for dte, sig, _ in items:
        if dte == CM_TARGET_DAYS:
            return sig
    under = [x for x in items if x[0] < CM_TARGET_DAYS]
    over = [x for x in items if x[0] > CM_TARGET_DAYS]
    if under and over:
        d1, s1, _ = under[-1]
        d2, s2, _ = over[0]
        t1, t2, t30 = d1 / 365.0, d2 / 365.0, CM_TARGET_DAYS / 365.0
        var = (s1 * s1 * t1 * (t2 - t30) + s2 * s2 * t2 * (t30 - t1)) / ((t2 - t1) * t30)
        return math.sqrt(var) if var > 0 else None
    if under:
        return under[-1][1]
    return over[0][1]


# ------------------------------------------------- A3.3 IV rank / percentile
def iv_rank_pct(series, current: float | None = None, *, hv20: float | None = None) -> dict:
    """``{"iv_rank", "iv_pct", "iv_n", "n", "lo", "hi", "state", "basis", "provisional"}``.

    ``series`` = the window of daily iv30 readings (PERCENT), oldest first, TODAY
    INCLUDED (A3.3; the bridge's min/max convention); ``current`` = today's value
    (default: the last element). rank = (current - lo) / (hi - lo) * 100 (None
    under IV_RANK_MIN_OBS readings or when hi == lo); pct = the share of readings
    strictly below current (None under IV_MIN_OBS).

    state by n: none (0) · forming (< 20) · pct_only (< 60) · rank_ok (< 252) · ok.
    basis: rank (ok) · percentile (rank_ok, pct_only) · provisional (forming WITH
    an HV20 - the verdict can rest on IV vs HV alone) · unknown (none, or forming
    without HV). provisional = basis in (provisional, unknown).
    """
    vals = [v for v in (_num(x) for x in (series or ())) if v is not None]
    n = len(vals)
    cur = _num(current)
    if cur is None and vals:
        cur = vals[-1]
    state = ("none" if n == 0 else "forming" if n < IV_MIN_OBS else "pct_only" if n < IV_RANK_MIN_OBS
             else "rank_ok" if n < IV_FULL_OBS else "ok")
    lo = min(vals) if vals else None
    hi = max(vals) if vals else None
    rank = pct = None
    if cur is not None and n >= IV_MIN_OBS:
        pct = round(sum(1 for v in vals if v < cur) / n * 100.0, 1)
    if cur is not None and n >= IV_RANK_MIN_OBS and hi is not None and hi > lo:
        rank = round((cur - lo) / (hi - lo) * 100.0, 1)
    if state == "ok":
        basis = "rank"
    elif state in ("rank_ok", "pct_only"):
        basis = "percentile"
    elif state == "forming" and _num(hv20) is not None:
        basis = "provisional"
    else:
        basis = "unknown"
    return {"iv_rank": rank, "iv_pct": pct, "iv_n": n, "n": n, "lo": lo, "hi": hi,
            "state": state, "basis": basis, "provisional": basis in ("provisional", "unknown")}


# --------------------------------------------------- A3.4 term structure
def _nearest(items, target: int, min_dte: int):
    cands = [(exp, d) for exp, d in items if d.get("dte") is not None and d["dte"] >= min_dte
             and _num(d.get("atm_iv")) is not None]
    if not cands:
        return None
    return min(cands, key=lambda x: (abs(x[1]["dte"] - target), x[1]["dte"]))


def term_structure(by_expiry: dict) -> dict:
    """front = the listed expiry nearest FRONT_TARGET_DTE (30) with dte >= 7; back
    = nearest BACK_TARGET_DTE (75) with dte >= 45 (both must exist, else None).
    ``term_ratio = iv_front / iv_back`` - unitless, the ONE term figure (>= TERM_EVENT
    1.05: an event is priced in the front; <= TERM_CONTANGO 0.95: contango).
    -> ``{"iv_front", "iv_back", "term_ratio", "front_expiry", "back_expiry",
    "front_dte", "back_dte"}``."""
    items = list((by_expiry or {}).items())
    front = _nearest(items, FRONT_TARGET_DTE, FRONT_MIN_DTE)
    back = _nearest(items, BACK_TARGET_DTE, BACK_MIN_DTE)
    out = {"iv_front": None, "iv_back": None, "term_ratio": None,
           "front_expiry": None, "back_expiry": None, "front_dte": None, "back_dte": None}
    if front:
        out.update(iv_front=float(front[1]["atm_iv"]), front_expiry=front[0], front_dte=front[1]["dte"])
    if back:
        out.update(iv_back=float(back[1]["atm_iv"]), back_expiry=back[0], back_dte=back[1]["dte"])
    if front and back and out["iv_back"]:
        out["term_ratio"] = round(out["iv_front"] / out["iv_back"], 4)
    return out


# ------------------------------------------------------------- A3.5 skew
def skew25(rows, front_expiry: str | None, atm_iv_front: float | None = None) -> dict:
    """At the front expiry: the put whose |delta| is nearest 0.25 and the call whose
    delta is nearest 0.25, each within SKEW_DELTA_TOL of it and with a sane iv.
    ``skew25 = (iv_put25 - iv_call25) * 100`` (vol points, positive = puts richer,
    the normal equity shape); ``skew_norm = skew25 / atm_iv_front`` (unitless).
    Both None if either leg is missing."""
    out = {"skew25": None, "skew_norm": None, "put25": None, "call25": None}
    if not front_expiry:
        return out
    best = {"P": None, "C": None}
    for r in rows or ():
        if str(_g(r, "expiry") or "")[:10] != front_expiry:
            continue
        rt = (_g(r, "right") or "").upper()[:1]
        d = _num(_g(r, "delta"))
        iv = _num(_g(r, "iv"))
        if rt not in best or d is None or iv is None or not (IV_SANITY_LO < iv < IV_SANITY_HI):
            continue
        dist = abs(abs(d) - SKEW_DELTA)
        if dist > SKEW_DELTA_TOL:
            continue
        if best[rt] is None or dist < best[rt][0]:
            best[rt] = (dist, iv, _num(_g(r, "strike")))
    if best["P"] is None or best["C"] is None:
        return out
    sk = (best["P"][1] - best["C"][1]) * 100.0
    out["skew25"] = round(sk, 4)
    out["put25"], out["call25"] = best["P"][2], best["C"][2]
    atm = _num(atm_iv_front)
    if atm:
        out["skew_norm"] = round(sk / atm, 4)
    return out


# ---------------------------------------------- A3.6 / A3.7 move, earnings
def expected_move(spot: float, iv_pct: float | None, days: int = EXPECTED_MOVE_DAYS) -> float | None:
    """The one-sigma expected move in $ over ``days`` calendar days from an IV in
    PERCENT: spot * iv / 100 * sqrt(days / 365)."""
    s, iv = _num(spot), _num(iv_pct)
    if not s or s <= 0 or iv is None or iv <= 0 or not days or days <= 0:
        return None
    return round(s * iv / 100.0 * math.sqrt(days / 365.0), 4)


def days_to_earnings(earnings, today) -> tuple[str | None, int | None]:
    """``(earnings_date, days)`` from prices.fetch_next_earnings' ``{"date",
    "days"}``, a bare ISO string, or None (= unknown: ``(None, None)``, displayed as
    "earnings date unknown", never as "no earnings"). ``days`` is recounted from
    ``today`` so a cached figure never goes stale."""
    if not earnings:
        return None, None
    raw = earnings.get("date") if isinstance(earnings, dict) else earnings
    d = _date(raw)
    t = _date(today) or _dt.date.today()
    if d is None:
        return None, None
    return d.isoformat(), (d - t).days


def earnings_inside(earnings_date, expiry, today) -> bool | None:
    """``today <= earnings_date <= expiry`` - the rule spread_scan.build_candidates
    applies. None when the date is unknown."""
    e, x, t = _date(earnings_date), _date(expiry), _date(today)
    if e is None or x is None or t is None:
        return None
    return t <= e <= x


# ------------------------------------------------------------------ all_for
def _window(iv_series, iv30: float | None, snap_on: str | None) -> list[float]:
    """The rank window from the stored series + today's reading. Items may be
    bare floats, ``(on, iv30)`` pairs or ``{"on", "iv30"}`` dicts (oldest first).
    A dated series has any ``on == snap_on`` entry replaced by today's figure; a
    bare-float series gets today's figure appended unless its last value already
    equals it (a caller who included today)."""
    dated: list[tuple[str | None, float]] = []
    for item in iv_series or ():
        if isinstance(item, dict):
            on, v = item.get("on"), _num(item.get("iv30", item.get("iv")))
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            on, v = item[0], _num(item[1])
        else:
            on, v = None, _num(item)
        if v is not None:
            dated.append((str(on)[:10] if on else None, v))
    # Always the documented window: IV_FULL_OBS readings INCLUDING today. A caller
    # that hands in 252 PRIOR readings (the nightly, before today's row is stored)
    # would otherwise rank on 253, one more than the stored rank and the chart use.
    if iv30 is None:
        return [v for _, v in dated][-IV_FULL_OBS:]
    if any(on is not None for on, _ in dated):
        kept = [(on, v) for on, v in dated if on != str(snap_on or "")[:10]]
        return ([v for _, v in kept] + [iv30])[-IV_FULL_OBS:]
    vals = [v for _, v in dated]
    if vals and vals[-1] == iv30:
        return vals[-IV_FULL_OBS:]
    return (vals + [iv30])[-IV_FULL_OBS:]


def all_for(chain, bars, earnings, iv_series, *, today: str | None = None) -> dict:
    """Every derived figure for one chain, in one dict (the ``iv_daily`` columns +
    the informational extras the gauge passes through):

    spot, as_of, snap_on, source, kind, n_contracts, n_expiries, partial,
    iv30 (PERCENT; the feed's own figure when it has one, else atm_iv30),
    iv30_src ("cboe" | "atm" | <source>), atm_iv30, hv20, hv60, iv_hv_premium
    (the RATIO iv30 / hv20), iv_rank, iv_pct, iv_n, iv_state, iv_basis,
    provisional, iv_lo, iv_hi, iv_by_expiry, iv_front, iv_back, term_ratio,
    front_expiry, back_expiry, front_dte, back_dte, skew25, skew_norm,
    expected_move, earnings_date, earnings_days.

    ``chain`` is Part A's Chain (or any object / dict with ``rows``, ``spot``,
    ``iv30``, ``snap_on``, ``source``); ``bars`` the daily history (the open
    session's bar is excluded); ``earnings`` prices.fetch_next_earnings' dict;
    ``iv_series`` the stored daily iv30 readings oldest first (see ``_window``).
    """
    rows = list(_g(chain, "rows", None) or [])
    spot = _num(_g(chain, "spot"))
    snap_on = str(_g(chain, "snap_on") or today or _dt.date.today().isoformat())[:10]
    today = today or snap_on
    source = _g(chain, "source", None)
    closes = closes_from_bars(bars)
    hv20, hv60 = hv(closes, HV_SHORT), hv(closes, HV_LONG)
    by_exp = atm_iv_by_expiry(rows, spot, snap_on) if spot else {}
    atm30 = iv30_constant_maturity(by_exp, snap_on)
    feed_iv30 = _num(_g(chain, "iv30"))
    if feed_iv30 is not None:
        iv30, src = feed_iv30, ("cboe" if source == "cboe" else (source or "feed"))
    elif atm30 is not None:
        iv30, src = round(atm30, 4), "atm"
    else:
        iv30, src = None, None
    window = _window(iv_series, iv30, snap_on)
    rp = iv_rank_pct(window, iv30, hv20=hv20)
    term = term_structure(by_exp)
    sk = skew25(rows, term["front_expiry"], term["iv_front"])
    e_date, e_days = days_to_earnings(earnings, today)
    prem = round(iv30 / hv20, 4) if (iv30 and hv20) else None
    as_of = _g(chain, "as_of", None)
    return {
        "spot": spot, "as_of": as_of, "snap_on": snap_on, "source": source, "kind": _g(chain, "kind", None),
        "n_contracts": len(rows), "n_expiries": len({str(_g(r, "expiry")) for r in rows}),
        "partial": bool(_g(chain, "partial", False)),
        "iv30": iv30, "iv30_src": src, "atm_iv30": round(atm30, 4) if atm30 is not None else None,
        "hv20": round(hv20, 4) if hv20 is not None else None,
        "hv60": round(hv60, 4) if hv60 is not None else None,
        "iv_hv_premium": prem,
        "iv_rank": rp["iv_rank"], "iv_pct": rp["iv_pct"], "iv_n": rp["iv_n"],
        "iv_state": rp["state"], "iv_basis": rp["basis"], "provisional": rp["provisional"],
        "iv_lo": rp["lo"], "iv_hi": rp["hi"],
        "iv_by_expiry": by_exp,
        "iv_front": term["iv_front"], "iv_back": term["iv_back"], "term_ratio": term["term_ratio"],
        "front_expiry": term["front_expiry"], "back_expiry": term["back_expiry"],
        "front_dte": term["front_dte"], "back_dte": term["back_dte"],
        "skew25": sk["skew25"], "skew_norm": sk["skew_norm"],
        "expected_move": expected_move(spot, iv30, EXPECTED_MOVE_DAYS),
        "earnings_date": e_date, "earnings_days": e_days,
    }
