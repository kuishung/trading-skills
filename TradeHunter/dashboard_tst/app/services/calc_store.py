"""Store for the option calculator (OPTION_CALCULATOR_DESIGN.md steps 2-3): a member's
defaults, their saved requests, and the results the Hermes lane writes per Massive pass.

Portable ORM only (platform data-handling rule): query-then-update upserts, JSON columns,
no dialect features. The member's defaults live in ``User.prefs`` under ``calc_*`` keys,
like ``trade_prefs`` - UI preferences, one row per user already exists.
"""
from __future__ import annotations

import datetime as _dt
import math

from sqlalchemy import func

from ..models import OptionCalcRequest, OptionCalcResult
from .calc_chain import ChainError, normalise_legs

KINDS = ("manual", "pair")
CREDIT_PREFS = ("any", "debit", "credit")

# the member's defaults (design §5 decisions 6-7): a 45-day horizon for the manual page,
# three monthly expiries searched by the pairing, criteria as discussed 2026-10-17
DEFAULTS = {
    "horizon_days": 45,
    "horizons_n": 3,
    "defined_risk": True,
    "max_loss": 0.0,          # $ per set; 0 = no cap
    "min_rr": 1.0,
    "min_oi": 100,
    "avoid_earnings": True,
    "credit_pref": "any",
    "alternatives": 3,        # alternatives kept under each family's best
}
LIMITS = {
    "horizon_days": (1, 730), "horizons_n": (1, 6), "max_loss": (0.0, 1e7), "min_rr": (0.0, 100.0),
    "min_oi": (0, 1e6), "alternatives": (0, 10),
}
MAX_SETS = 10_000
KEEP_SESSIONS = 2             # every pass kept for this many newest sessions; older: last pass per session


class CalcInputError(ValueError):
    """Bad member input; the message is for the member."""


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


def _num(v, default, lo, hi):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    if math.isnan(x) or math.isinf(x):
        return default
    return x if lo <= x <= hi else default


def _bool(v, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on") if v.strip() else default
    if isinstance(v, (int, float)):
        return bool(v)
    return default


# ────────────────────────────────── member defaults ──────────────────────────────────

def read_prefs(user) -> dict:
    """This member's calculator defaults, always complete and in bounds."""
    p = getattr(user, "prefs", None) or {}
    out = {
        "horizon_days": int(_num(p.get("calc_horizon_days"), DEFAULTS["horizon_days"], *LIMITS["horizon_days"])),
        "horizons_n": int(_num(p.get("calc_horizons_n"), DEFAULTS["horizons_n"], *LIMITS["horizons_n"])),
        "defined_risk": _bool(p.get("calc_defined_risk"), DEFAULTS["defined_risk"]),
        "max_loss": _num(p.get("calc_max_loss"), DEFAULTS["max_loss"], *LIMITS["max_loss"]),
        "min_rr": _num(p.get("calc_min_rr"), DEFAULTS["min_rr"], *LIMITS["min_rr"]),
        "min_oi": int(_num(p.get("calc_min_oi"), DEFAULTS["min_oi"], *LIMITS["min_oi"])),
        "avoid_earnings": _bool(p.get("calc_avoid_earnings"), DEFAULTS["avoid_earnings"]),
        "credit_pref": p.get("calc_credit_pref") if p.get("calc_credit_pref") in CREDIT_PREFS else DEFAULTS["credit_pref"],
        "alternatives": int(_num(p.get("calc_alternatives"), DEFAULTS["alternatives"], *LIMITS["alternatives"])),
    }
    return out


def write_prefs(db, user, **changes) -> tuple[dict, str]:
    """Update the supplied defaults. Out-of-range input is reported, not clamped."""
    p = dict(getattr(user, "prefs", None) or {})
    for key, value in changes.items():
        if value is None or value == "" or key not in DEFAULTS:
            continue
        if key in ("defined_risk", "avoid_earnings"):
            p["calc_" + key] = _bool(value, DEFAULTS[key])
        elif key == "credit_pref":
            if value not in CREDIT_PREFS:
                return read_prefs(user), "Debit or credit preference must be any, debit or credit."
            p["calc_" + key] = value
        else:
            lo, hi = LIMITS[key]
            try:
                x = float(value)
            except (TypeError, ValueError):
                return read_prefs(user), "%s must be a number." % key.replace("_", " ").capitalize()
            if not lo <= x <= hi:
                return read_prefs(user), "%s must be between %g and %g." % (key.replace("_", " ").capitalize(), lo, hi)
            p["calc_" + key] = int(x) if key in ("horizon_days", "horizons_n", "min_oi", "alternatives") else x
    user.prefs = p          # reassign: SQLAlchemy won't see an in-place mutation
    db.commit()
    return read_prefs(user), ""


def criteria_for(user, request: OptionCalcRequest | None = None) -> dict:
    """The pairing criteria for a request: the member's defaults, the request's
    ``criteria`` overrides on top (same keys, same bounds)."""
    base = read_prefs(user)
    over = dict(getattr(request, "criteria", None) or {})
    if not over:
        return base
    fake = type("P", (), {"prefs": {"calc_" + k: v for k, v in {**{k: base[k] for k in base}, **over}.items()}})()
    return read_prefs(fake)


# ────────────────────────────────── requests ──────────────────────────────────

def _clean(symbol, target, worst, kind, name, horizon_days, sets, legs, criteria) -> dict:
    sym = str(symbol or "").strip().upper().replace(".", "-")
    if not sym or len(sym) > 20:
        raise CalcInputError("A ticker is needed.")
    if kind not in KINDS:
        raise CalcInputError("kind must be manual or pair")
    t, w = _num(target, None, 0.0001, 1e7), _num(worst, None, 0.0001, 1e7)
    if t is None:
        raise CalcInputError("The expected price must be a positive number.")
    if w is None:
        raise CalcInputError("The worst-case price must be a positive number.")
    if abs(t - w) < 1e-9:
        raise CalcInputError("The expected and worst-case prices must differ.")
    h = None
    if horizon_days not in (None, ""):
        h = _num(horizon_days, None, *LIMITS["horizon_days"])
        if h is None:
            raise CalcInputError("Days must be between %d and %d." % LIMITS["horizon_days"])
        h = int(h)
    s = _num(sets, None, 1, MAX_SETS)
    if s is None:
        raise CalcInputError("Sets must be between 1 and %d." % MAX_SETS)
    clean_legs = None
    if kind == "manual":
        try:
            clean_legs = normalise_legs(legs)
        except ChainError as e:
            raise CalcInputError(str(e)) from e
    crit = None
    if kind == "pair" and criteria:
        if not isinstance(criteria, dict):
            raise CalcInputError("criteria must be an object")
        crit = {k: v for k, v in criteria.items() if k in DEFAULTS}
    nm = (str(name).strip()[:80] or None) if name is not None else None
    return {"symbol": sym, "kind": kind, "name": nm, "target": t, "worst": w, "horizon_days": h,
            "sets": int(s), "legs": clean_legs, "criteria": crit}


def create_request(db, user, *, symbol, target, worst, kind="manual", name=None, horizon_days=None,
                   sets=1, legs=None, criteria=None) -> OptionCalcRequest:
    fields = _clean(symbol, target, worst, kind, name, horizon_days, sets, legs, criteria)
    req = OptionCalcRequest(user_id=user.id, active=True, **fields)
    db.add(req)
    db.commit()
    db.refresh(req)
    return req


def get_request(db, user, rid) -> OptionCalcRequest | None:
    try:
        rid = int(rid)
    except (TypeError, ValueError):
        return None
    return (db.query(OptionCalcRequest)
              .filter(OptionCalcRequest.id == rid, OptionCalcRequest.user_id == user.id).first())


def list_requests(db, user, *, active=None, kind=None) -> list[OptionCalcRequest]:
    q = db.query(OptionCalcRequest).filter(OptionCalcRequest.user_id == user.id)
    if active is not None:
        q = q.filter(OptionCalcRequest.active.is_(bool(active)))
    if kind:
        q = q.filter(OptionCalcRequest.kind == kind)
    return q.order_by(OptionCalcRequest.created_at.desc(), OptionCalcRequest.id.desc()).all()


def update_request(db, user, rid, **changes) -> OptionCalcRequest | None:
    """Change a request's inputs. The lane recomputes it on its next look (the pass
    stamp is cleared) and the old results stay as history."""
    req = get_request(db, user, rid)
    if req is None:
        return None
    cur = {"symbol": req.symbol, "target": req.target, "worst": req.worst, "kind": req.kind,
           "name": req.name, "horizon_days": req.horizon_days, "sets": req.sets, "legs": req.legs,
           "criteria": req.criteria}
    cur.update({k: v for k, v in changes.items() if k in cur})
    fields = _clean(**cur)
    for k, v in fields.items():
        setattr(req, k, v)
    req.last_pass_id = None
    req.last_error = None
    req.updated_at = _utcnow()
    db.commit()
    db.refresh(req)
    return req


def set_active(db, user, rid, active: bool) -> OptionCalcRequest | None:
    req = get_request(db, user, rid)
    if req is None:
        return None
    req.active = bool(active)
    if req.active:
        req.last_pass_id = None       # recompute on the next look
    db.commit()
    return req


def delete_request(db, user, rid) -> bool:
    req = get_request(db, user, rid)
    if req is None:
        return False
    db.delete(req)
    db.commit()
    return True


# ────────────────────────────────── the lane's side ──────────────────────────────────

def due_requests(db, pass_id) -> list[OptionCalcRequest]:
    """Every member's active requests not yet computed on ``pass_id`` (NULL stamp = never
    or changed since)."""
    q = db.query(OptionCalcRequest).filter(OptionCalcRequest.active.is_(True))
    if pass_id is not None:
        q = q.filter((OptionCalcRequest.last_pass_id.is_(None)) | (OptionCalcRequest.last_pass_id != int(pass_id)))
    return q.order_by(OptionCalcRequest.id).all()


def write_results(db, req: OptionCalcRequest, rows: list[dict], *, pass_id, session, as_of, spot,
                  now=None) -> int:
    """Replace the request's rows for ``pass_id`` with ``rows`` (dicts with family, rank,
    horizon_days, target_date, legs, figures, curves, score) and stamp the request."""
    now = now or _utcnow()
    q = db.query(OptionCalcResult).filter(OptionCalcResult.request_id == req.id)
    q = q.filter(OptionCalcResult.pass_id.is_(None)) if pass_id is None else q.filter(OptionCalcResult.pass_id == int(pass_id))
    for old in q.all():
        db.delete(old)
    n = 0
    for r in rows:
        db.add(OptionCalcResult(
            request_id=req.id, pass_id=pass_id, session=session, as_of=as_of, spot=spot,
            family=str(r.get("family") or "manual")[:24], rank=int(r.get("rank") or 1),
            horizon_days=r.get("horizon_days"), target_date=r.get("target_date"),
            legs=r.get("legs") or [], figures=r.get("figures") or {}, curves=r.get("curves"),
            score=r.get("score"), created_at=now))
        n += 1
    req.last_pass_id = pass_id
    req.last_computed_at = now
    req.last_error = None
    db.commit()
    return n


def mark_error(db, req: OptionCalcRequest, message: str, *, pass_id, now=None) -> None:
    """The lane could not compute this request on ``pass_id``: keep the reason, stamp the
    pass so it is not retried until the next pass (or the member changes the request)."""
    req.last_pass_id = pass_id
    req.last_computed_at = now or _utcnow()
    req.last_error = str(message)[:2000]
    db.commit()


def latest_pass_id(db, req: OptionCalcRequest) -> int | None:
    row = (db.query(OptionCalcResult.pass_id, OptionCalcResult.created_at)
             .filter(OptionCalcResult.request_id == req.id)
             .order_by(OptionCalcResult.created_at.desc(), OptionCalcResult.id.desc()).first())
    return None if row is None else row[0]


def latest_results(db, req: OptionCalcRequest) -> list[OptionCalcResult]:
    """The rows of the request's newest computation, best first within each family."""
    newest = (db.query(OptionCalcResult.created_at)
                .filter(OptionCalcResult.request_id == req.id)
                .order_by(OptionCalcResult.created_at.desc()).first())
    if newest is None:
        return []
    return (db.query(OptionCalcResult)
              .filter(OptionCalcResult.request_id == req.id, OptionCalcResult.created_at == newest[0])
              .order_by(OptionCalcResult.score.desc().nullslast(), OptionCalcResult.family,
                        OptionCalcResult.rank).all())


def result_history(db, req: OptionCalcRequest, *, limit: int = 200) -> list[OptionCalcResult]:
    """Rank-1 rows over time (newest first): how the setup's figures drifted pass by pass."""
    return (db.query(OptionCalcResult)
              .filter(OptionCalcResult.request_id == req.id, OptionCalcResult.rank == 1)
              .order_by(OptionCalcResult.created_at.desc(), OptionCalcResult.id.desc())
              .limit(int(limit)).all())


def thin_results(db, *, keep_sessions: int = KEEP_SESSIONS) -> int:
    """Retention (design §5 decision 11): every pass is kept for the newest
    ``keep_sessions`` sessions; for older sessions only each request's LAST pass of the
    session stays. Returns the rows removed."""
    sessions = [s for (s,) in db.query(OptionCalcResult.session).distinct().all() if s]
    old = sorted(sessions)[:-keep_sessions] if len(sessions) > keep_sessions else []
    removed = 0
    for sess in old:
        keep = dict(db.query(OptionCalcResult.request_id, func.max(OptionCalcResult.created_at))
                      .filter(OptionCalcResult.session == sess)
                      .group_by(OptionCalcResult.request_id).all())
        for row in db.query(OptionCalcResult).filter(OptionCalcResult.session == sess).all():
            if row.created_at != keep.get(row.request_id):
                db.delete(row)
                removed += 1
    if removed:
        db.commit()
    return removed


# ────────────────────────────────── serialising the engine's result ──────────────────────────────────

_FIGURE_KEYS = ("entry_value", "decayed_value", "target_value", "worst_value", "gain_at_target",
                "loss_at_worst", "reward_over_risk", "roi", "roi_on_max_loss", "total_decay",
                "net_delta_entry", "net_delta_target", "breakevens", "max_profit", "max_loss",
                "total_gains", "total_risk", "total_outlay")


def _r(x, nd=6):
    if isinstance(x, float):
        return None if (math.isnan(x) or math.isinf(x)) else round(x, nd)
    return x


def figures_from(res) -> dict:
    """The engine's indicators as a JSON-safe dict (no curves)."""
    out = {k: _r(getattr(res, k)) for k in _FIGURE_KEYS}
    out["breakevens"] = [_r(b) for b in res.breakevens]
    out["legs"] = [{k: _r(v) for k, v in leg.__dict__.items()} for leg in res.legs]
    return out


def curves_from(res, *, step: int = 1) -> dict:
    """The payoff curves as a JSON-safe dict (every ``step``-th grid point)."""
    return {"grid": [_r(x, 4) for x in res.grid[::step]],
            "net_target": [_r(x, 4) for x in res.net_curve_target[::step]],
            "net_entry": [_r(x, 4) for x in res.net_curve_entry[::step]],
            "legs": [[_r(x, 4) for x in c[::step]] for c in res.leg_curves_target]}
