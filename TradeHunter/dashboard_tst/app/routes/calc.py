"""The option calculator page (OPTION_CALCULATOR_DESIGN.md steps 2-3) - the member's view.

* ``GET /calc`` - the page: the member's saved setups (left / top), the selected setup's
  result (right / below), a "New setup" form (two prices: expected and worst case;
  "find the legs for me" = a pair request, "I pick the legs" = a manual one) and the
  member's defaults. ``?id=`` selects a setup, ``?symbol=`` fills the ticker,
  ``?view=sheet|setups`` picks the view (v4.157: the Excel-style "Sheet" with the
  volatility surface as the leg picker, or the saved "Setups").
* ``/calc/api/*`` - the JSON the page talks to:

  - ``GET  /api/chain?symbol=``          the last market pass's chain for the leg picker
  - ``GET  /api/requests``               the member's setups
  - ``POST /api/requests``               create (JSON body)
  - ``GET  /api/requests/{id}``          one setup + its newest results + a short history
  - ``POST /api/requests/{id}``          change a setup's inputs
  - ``POST /api/requests/{id}/active``   ``{active: bool}`` pause / resume
  - ``DELETE /api/requests/{id}``        delete (results go with it)
  - ``POST /api/requests/{id}/compute``  "compute now": the Hermes lane's own code, inline
  - ``GET/POST /api/prefs``              the member's defaults
  - ``GET  /api/status``                 the Hermes lane's state file + the screener's last pass

Expected problems (the chain cannot serve a request, no market pass yet) answer HTTP 200
``{ok: false, error}`` with a message for the member; bad input answers 400 with the same
shape; another member's setup answers 404. Everything is scoped by ``user_id`` in
``calc_store``.

The calculator services import numpy, so they are imported LAZILY inside the handlers (a
server without numpy still boots; the page says why it cannot calculate and the API
answers 503).
"""
from __future__ import annotations

import datetime as _dt
import logging
import math
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..db import get_db
from ..models import User
from ..security import require_user
from ..services import clock

log = logging.getLogger(__name__)

router = APIRouter(prefix="/calc", tags=["calc"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

HISTORY_LIMIT = 30            # passes shown under a setup
STALE_S = 5 * 60              # the lane looks every minute; no heartbeat for 5 min = not running
MAX_LEGS = 4
REQUEST_FIELDS = ("symbol", "target", "worst", "kind", "name", "horizon_days", "sets", "legs", "criteria")


# ─────────────────────────────────────── helpers ───────────────────────────────────────

class _Missing(RuntimeError):
    """The calculator services could not be imported (numpy missing on this server)."""


def _services():
    """The calculator modules, imported on first use."""
    try:
        from ..services import calc_chain, calc_pair, calc_store, calc_worker  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - only on a server without numpy
        raise _Missing("The calculator needs numpy on this server (pip install -r app\\requirements.txt): %s"
                       % exc) from exc
    return calc_store, calc_chain, calc_pair, calc_worker


def _err(msg: str, status: int = 400, **extra) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg, **extra}, status_code=status)


def _utc(t: _dt.datetime | None) -> _dt.datetime | None:
    if t is None:
        return None
    return t.replace(tzinfo=_dt.timezone.utc) if t.tzinfo is None else t.astimezone(_dt.timezone.utc)


def _iso(t) -> str | None:
    """A stored (naive = UTC) datetime as ISO with its offset, so the browser reads it right."""
    if t is None:
        return None
    if isinstance(t, str):
        return t
    return _utc(t).isoformat(timespec="seconds")


def _et(t) -> str | None:
    """``Oct 19, 10:45 ET`` - every time on the page is New York time."""
    if t is None:
        return None
    if isinstance(t, str):
        try:
            t = _dt.datetime.fromisoformat(t)
        except ValueError:
            return t
    e = clock.et_now(_utc(t))
    return "%s %d, %s ET" % (e.strftime("%b"), e.day, e.strftime("%H:%M"))


def _clean(x):
    """JSON-safe: NaN / inf to None, recursively."""
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {str(k): _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    return x


def _req_dict(r) -> dict:
    return {
        "id": r.id, "symbol": r.symbol, "kind": r.kind, "name": r.name, "target": r.target,
        "worst": r.worst, "horizon_days": r.horizon_days, "sets": r.sets, "legs": r.legs,
        "criteria": r.criteria, "active": bool(r.active), "last_pass_id": r.last_pass_id,
        "last_computed_at": _iso(r.last_computed_at), "last_computed_et": _et(r.last_computed_at),
        "last_error": r.last_error, "created_at": _iso(r.created_at),
    }


def _res_dict(r) -> dict:
    return _clean({
        "id": r.id, "pass_id": r.pass_id, "session": r.session, "as_of": _iso(r.as_of),
        "as_of_et": _et(r.as_of), "spot": r.spot, "family": r.family, "rank": r.rank,
        "horizon_days": r.horizon_days, "target_date": r.target_date, "legs": r.legs or [],
        "figures": r.figures or {}, "curves": r.curves, "score": r.score,
        "created_at": _iso(r.created_at), "created_et": _et(r.created_at),
    })


def _history(rows, kind: str) -> list[dict]:
    """The rank-1 rows over time, newest first. A pair setup has one per family per pass:
    the best-scoring one of each computation stands for it."""
    out, seen = [], {}
    for r in rows:
        if r.family == "_summary":
            continue
        key = r.created_at
        if kind == "pair":
            if key in seen and (seen[key].score or 0) >= (r.score or 0):
                continue
            seen[key] = r
        else:
            if key in seen:
                continue
            seen[key] = r
    for r in sorted(seen.values(), key=lambda x: (x.created_at or _dt.datetime.min), reverse=True)[:HISTORY_LIMIT]:
        f = r.figures or {}
        out.append(_clean({"pass_id": r.pass_id, "created_at": _iso(r.created_at), "created_et": _et(r.created_at),
                           "as_of_et": _et(r.as_of), "spot": r.spot, "family": r.family,
                           "family_label": f.get("family_label"), "horizon_days": r.horizon_days,
                           "entry_value": f.get("entry_value"), "target_value": f.get("target_value"),
                           "reward_over_risk": f.get("reward_over_risk"), "score": r.score}))
    return out


def _payload(db: Session, store, req) -> dict:
    """The setup, its newest computation (results + the pairing summary) and its history."""
    rows = store.latest_results(db, req)
    summary = next((r for r in rows if r.family == "_summary"), None)
    results = [_res_dict(r) for r in rows if r.family != "_summary"]
    first = rows[0] if rows else None
    computed = None
    if first is not None:
        computed = _clean({"pass_id": first.pass_id, "session": first.session, "as_of": _iso(first.as_of),
                           "as_of_et": _et(first.as_of), "spot": first.spot, "created_at": _iso(first.created_at),
                           "created_et": _et(first.created_at)})
    return {"ok": True, "item": _req_dict(req), "results": results,
            "summary": _clean(summary.figures) if summary is not None else None,
            "computed": computed,
            "history": _history(store.result_history(db, req, limit=HISTORY_LIMIT * 14), req.kind)}


async def _json_body(request: Request) -> dict | None:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - empty or not JSON
        return None
    return body if isinstance(body, dict) else None


# ─────────────────────────────────────── status ───────────────────────────────────────

def _status_payload(now: _dt.datetime | None = None) -> dict:
    """The Hermes lane (its state file) and the screener collector's newest finished pass."""
    from .. import screener_db  # noqa: PLC0415
    from ..services import scr_store  # noqa: PLC0415

    now = _utc(now) or _dt.datetime.now(_dt.timezone.utc)
    try:
        _store, _chain, _pair, worker = _services()
        st = worker.read_state()
    except _Missing:
        st = None
    if not st:
        lane = {"state": "absent", "stale": True, "heartbeat": None, "heartbeat_age_s": None,
                "detail": "The background calculator on Hermes has not reported yet. \"Compute now\" still works from this page.",
                "last_pass_id": None, "computed_at": None, "computed_et": None, "last_error": None,
                "requests_active": None, "requests_due": None}
    else:
        hb = st.get("heartbeat")
        age = None
        try:
            t = _dt.datetime.fromisoformat(hb) if hb else None
            age = None if t is None else max(int((now - _utc(t)).total_seconds()), 0)
        except (TypeError, ValueError):
            age = None
        lane = {"state": st.get("state") or "unknown", "detail": st.get("detail"), "heartbeat": hb,
                "heartbeat_age_s": age, "stale": age is None or age > STALE_S or st.get("state") == "stopped",
                "last_pass_id": st.get("last_pass_id"), "computed_at": st.get("computed_at"),
                "computed_et": _et(st.get("computed_at")), "last_error": st.get("last_error"),
                "requests_active": st.get("requests_active"), "requests_due": st.get("requests_due")}
    last_pass, pass_error = None, None
    try:
        with screener_db.session() as s:
            lp = scr_store.last_pass(s, min_contracts=1)
        if lp:
            last_pass = {"id": lp.get("id"), "session": lp.get("session"), "finished": _iso(lp.get("finished")),
                         "finished_et": _et(lp.get("finished")), "kind": lp.get("kind")}
    except Exception as exc:  # noqa: BLE001 - no screener DB on this PC, or it is busy
        log.info("calc status: the screener DB could not be read: %s", exc)
        pass_error = "The option data could not be read just now."
    return {"ok": True, "lane": lane, "last_pass": last_pass, "last_pass_error": pass_error,
            "now": _iso(now), "now_et": _et(now)}


# ─────────────────────────────────────── the page ───────────────────────────────────────

@router.get("", response_class=HTMLResponse)
def calc_home(request: Request, id: str = "", symbol: str = "", view: str = "",
              user: User = Depends(require_user), db: Session = Depends(get_db)):
    engine_error, requests, prefs, defaults, limits, families = None, [], {}, {}, {}, {}
    try:
        store, _chain, pair, _worker = _services()
        requests = [_req_dict(r) for r in store.list_requests(db, user)]
        prefs, defaults = store.read_prefs(user), dict(store.DEFAULTS)
        limits = {k: list(v) for k, v in store.LIMITS.items()}
        families = {k: {"direction": v[0], "legs": v[1], "defined_risk": v[2], "label": v[3], "debit_credit": v[4]}
                    for k, v in pair.FAMILIES.items()}
    except _Missing as exc:
        engine_error = str(exc)
    try:
        selected = int(id) if id else None
    except ValueError:
        selected = None
    if selected is not None and not any(r["id"] == selected for r in requests):
        selected = None
    sym = str(symbol or "").strip().upper()[:20]
    # v4.157: "sheet" (the Excel-style sheet with the volatility-surface leg picker) or
    # "setups" (the saved setups). Empty = the page decides: ?id= -> setups, else the
    # member's last choice (browser storage), else the sheet.
    view = str(view or "").strip().lower()
    view = view if view in ("sheet", "setups") else ""
    boot = {
        "engine_error": engine_error, "requests": requests, "prefs": prefs, "defaults": defaults,
        "limits": limits, "families": families, "selected": selected, "symbol": sym,
        "status": _status_payload(), "today": clock.et_today(), "max_legs": MAX_LEGS, "view": view,
    }
    return templates.TemplateResponse(request, "calc.html", {"user": user, "boot": boot,
                                                             "engine_error": engine_error})


# ─────────────────────────────────────── the API ───────────────────────────────────────

def _chain_payload(symbol: str) -> dict:
    from .. import screener_db  # noqa: PLC0415

    _store, chain_mod, _pair, _worker = _services()
    today = clock.et_today()
    try:
        with screener_db.session() as s:
            ch = chain_mod.load_chain(s, symbol)
    except chain_mod.ChainError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - no screener DB yet, or it is busy
        log.info("calc chain %s: %s", symbol, exc)
        return {"ok": False, "error": "The option data could not be read just now - the screener collector "
                                      "may not have finished its first market pass yet. Try again in a minute."}
    strikes: dict[str, dict[str, list[float]]] = {}
    for c in ch.contracts:
        if c.expiry < today or not c.iv or c.iv <= 0:
            continue                      # past expiries and contracts without an IV cannot be priced
        strikes.setdefault(c.expiry, {"C": [], "P": []})[c.right].append(c.strike)
    for e in strikes.values():
        e["C"] = sorted(set(e["C"]))
        e["P"] = sorted(set(e["P"]))
    expiries = sorted(strikes)
    d0 = _dt.date.fromisoformat(today)
    return _clean({"ok": True, "symbol": ch.symbol, "name": ch.name, "spot": ch.spot, "spot_src": ch.spot_src,
                   "as_of": _iso(ch.as_of), "as_of_et": _et(ch.as_of), "session": ch.session,
                   "expiries": expiries,
                   "dte": {e: (_dt.date.fromisoformat(e) - d0).days for e in expiries},
                   "strikes": strikes, "earnings_date": ch.earnings_date, "iv_rank": ch.iv_rank})


@router.get("/api/chain")
async def api_chain(symbol: str = "", user: User = Depends(require_user)):
    sym = str(symbol or "").strip().upper()
    if not sym or len(sym) > 20:
        return _err("Type a ticker first.")
    try:
        return JSONResponse(await run_in_threadpool(_chain_payload, sym))
    except _Missing as exc:
        return _err(str(exc), 503)


@router.get("/api/requests")
def api_requests(user: User = Depends(require_user), db: Session = Depends(get_db)):
    try:
        store = _services()[0]
    except _Missing as exc:
        return _err(str(exc), 503)
    return {"ok": True, "items": [_req_dict(r) for r in store.list_requests(db, user)]}


@router.post("/api/requests")
async def api_create(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    body = await _json_body(request)
    if body is None:
        return _err("The request was not in the expected form.")
    try:
        store = _services()[0]
    except _Missing as exc:
        return _err(str(exc), 503)
    fields = {k: body.get(k) for k in REQUEST_FIELDS if k in body}
    fields.setdefault("kind", "pair")
    fields.setdefault("sets", 1)
    if "symbol" not in fields or "target" not in fields or "worst" not in fields:
        return _err("A ticker, an expected price and a worst-case price are needed.")
    try:
        req = store.create_request(db, user, **fields)
    except store.CalcInputError as exc:
        return _err(str(exc))
    return {"ok": True, "item": _req_dict(req)}


@router.get("/api/requests/{rid}")
def api_request(rid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    try:
        store = _services()[0]
    except _Missing as exc:
        return _err(str(exc), 503)
    req = store.get_request(db, user, rid)
    if req is None:
        return _err("That setup was not found.", 404)
    return _payload(db, store, req)


@router.post("/api/requests/{rid}")
async def api_update(rid: int, request: Request, user: User = Depends(require_user),
                     db: Session = Depends(get_db)):
    body = await _json_body(request)
    if body is None:
        return _err("The request was not in the expected form.")
    try:
        store = _services()[0]
    except _Missing as exc:
        return _err(str(exc), 503)
    changes = {k: body[k] for k in REQUEST_FIELDS if k in body}
    try:
        req = store.update_request(db, user, rid, **changes)
    except store.CalcInputError as exc:
        db.rollback()
        return _err(str(exc))
    if req is None:
        return _err("That setup was not found.", 404)
    return {"ok": True, "item": _req_dict(req)}


@router.post("/api/requests/{rid}/active")
def api_active(rid: int, body: Any = Body(None), user: User = Depends(require_user),
               db: Session = Depends(get_db)):
    if not isinstance(body, dict) or "active" not in body:
        return _err("Say whether the setup is active: {\"active\": true} or false.")
    try:
        store = _services()[0]
    except _Missing as exc:
        return _err(str(exc), 503)
    req = store.set_active(db, user, rid, bool(body.get("active")))
    if req is None:
        return _err("That setup was not found.", 404)
    return {"ok": True, "item": _req_dict(req)}


@router.delete("/api/requests/{rid}")
def api_delete(rid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    try:
        store = _services()[0]
    except _Missing as exc:
        return _err(str(exc), 503)
    if not store.delete_request(db, user, rid):
        return _err("That setup was not found.", 404)
    return {"ok": True}


def _compute(db: Session, user: User, rid: int) -> tuple[dict | None, int]:
    from .. import screener_db  # noqa: PLC0415

    store, chain_mod, _pair, worker = _services()
    req = store.get_request(db, user, rid)
    if req is None:
        return None, 404
    error = None
    try:
        with screener_db.session() as s:
            worker.compute_and_store(db, s, req, user)
    except chain_mod.ChainError as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001 - no screener DB yet, or a bug: say so, keep the page alive
        db.rollback()
        log.exception("calc compute %s failed", rid)
        error = ("The option data could not be read just now (%s). Try again in a minute." % type(exc).__name__)
    db.refresh(req)
    out = _payload(db, store, req)
    if error:
        out["ok"] = False
        out["error"] = error
    return out, 200


@router.post("/api/requests/{rid}/compute")
async def api_compute(rid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    try:
        out, status = await run_in_threadpool(_compute, db, user, rid)
    except _Missing as exc:
        return _err(str(exc), 503)
    if out is None:
        return _err("That setup was not found.", status)
    return JSONResponse(out)


@router.get("/api/prefs")
def api_prefs(user: User = Depends(require_user)):
    try:
        store = _services()[0]
    except _Missing as exc:
        return _err(str(exc), 503)
    return {"ok": True, "prefs": store.read_prefs(user), "defaults": dict(store.DEFAULTS),
            "limits": {k: list(v) for k, v in store.LIMITS.items()}}


@router.post("/api/prefs")
async def api_prefs_save(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    body = await _json_body(request)
    if body is None:
        return _err("The request was not in the expected form.")
    try:
        store = _services()[0]
    except _Missing as exc:
        return _err(str(exc), 503)
    prefs, problem = store.write_prefs(db, user, **{k: body[k] for k in store.DEFAULTS if k in body})
    if problem:
        return _err(problem, prefs=prefs)
    return {"ok": True, "prefs": prefs}


@router.get("/api/status")
async def api_status(user: User = Depends(require_user)):
    return JSONResponse(await run_in_threadpool(_status_payload))


# ─────────────────────────────────── the sheet view (v4.157) ───────────────────────────────────
# The user: "I would want a interface to follow the excel sheet and then on the leg user can
# select the leg from the volatility surface. So every leg selected will be calculated with
# the result as per the excel sheet format." Two endpoints: the surface as a leg PICKER (call
# and put IV per strike x expiry, from the last market pass) and an instant PREVIEW (the
# engine's figures for the legs on screen, nothing saved).

SURFACE_BAND = 0.35           # strikes within +/- 35 % of spot
SURFACE_MAX_STRIKES = 40      # nearest the spot
SURFACE_MAX_EXPIRIES = 14


def _surface_payload(symbol: str) -> dict:
    """``{ok, symbol, name, spot, as_of_et, session, earnings_date, iv_rank, expiries:[{date,
    dte}], strikes:[...], calls:[[iv %|null]], puts:[[iv %|null]], oi_c, oi_p, atm:[iv %|null],
    scale:[lo, hi]}`` - rows = strikes ascending, columns = expiries ascending."""
    from .. import screener_db  # noqa: PLC0415

    _store, chain_mod, _pair, _worker = _services()
    with screener_db.session() as s:
        ch = chain_mod.load_chain(s, symbol)
    if not ch.spot or ch.spot <= 0:
        raise chain_mod.ChainError("the last market pass has no spot price for %s" % ch.symbol)
    today = clock.et_date()
    lo, hi = ch.spot * (1 - SURFACE_BAND), ch.spot * (1 + SURFACE_BAND)
    by: dict = {}
    for c in ch.contracts:
        if c.iv is None or c.iv <= 0 or not (lo <= c.strike <= hi):
            continue
        d = chain_mod.dte(c.expiry, today)
        if d <= 0:
            continue
        by.setdefault(c.expiry, {}).setdefault(c.strike, {})[c.right] = c
    exps = sorted(by)[:SURFACE_MAX_EXPIRIES]
    strikes = sorted({k for e in exps for k in by[e]})
    if len(strikes) > SURFACE_MAX_STRIKES:
        strikes = sorted(sorted(strikes, key=lambda k: (abs(k - ch.spot), k))[:SURFACE_MAX_STRIKES])

    def grid(right, attr):
        out = []
        for k in strikes:
            row = []
            for e in exps:
                c = by.get(e, {}).get(k, {}).get(right)
                v = getattr(c, attr) if c is not None else None
                row.append(round(v * 100.0, 2) if (attr == "iv" and v is not None) else v)
            out.append(row)
        return out

    calls, puts = grid("C", "iv"), grid("P", "iv")
    atm = []
    for e in exps:
        ks = sorted(by[e], key=lambda k: (abs(k - ch.spot), k))
        legs = by[e][ks[0]] if ks else {}
        vals = [c.iv for c in legs.values() if c.iv]
        atm.append(round(sum(vals) / len(vals) * 100.0, 2) if vals else None)
    flat = sorted(v for g in (calls, puts) for row in g for v in row if v is not None)
    scale = [flat[int(0.05 * (len(flat) - 1))], flat[int(0.95 * (len(flat) - 1))]] if flat else None
    return _clean({
        "ok": True, "symbol": ch.symbol, "name": ch.name, "spot": ch.spot, "spot_src": ch.spot_src,
        "as_of": _iso(ch.as_of), "as_of_et": _et(ch.as_of), "session": ch.session,
        "earnings_date": ch.earnings_date, "iv_rank": ch.iv_rank, "today": today.isoformat(),
        "expiries": [{"date": e, "dte": chain_mod.dte(e, today)} for e in exps],
        "strikes": strikes, "calls": calls, "puts": puts, "oi_c": grid("C", "oi"), "oi_p": grid("P", "oi"),
        "atm": atm, "scale": scale,
        "spot_row": sum(1 for k in strikes if k < ch.spot),
    })


@router.get("/api/surface")
async def api_surface(symbol: str = "", user: User = Depends(require_user)):
    sym = (symbol or "").strip().upper()
    if not sym:
        return _err("A ticker is needed.")
    try:
        _store, chain_mod, _pair, _worker = _services()
    except _Missing as exc:
        return _err(str(exc), 503)
    try:
        return JSONResponse(await run_in_threadpool(_surface_payload, sym))
    except chain_mod.ChainError as exc:
        return JSONResponse({"ok": False, "error": str(exc)})
    except Exception as exc:  # noqa: BLE001 - no screener DB on this PC
        log.info("calc surface %s: %s", sym, exc)
        return JSONResponse({"ok": False, "error": "The option data could not be read just now (%s)." % type(exc).__name__})


def _preview_payload(user, body: dict) -> dict:
    """The sheet's figures for the legs on screen: ``{ok, spot, as_of_et, session, horizon_days,
    target_date, legs (resolved), figures, curves}``. Nothing is saved."""
    from .. import screener_db  # noqa: PLC0415
    from ..services.option_calc import Scenario, evaluate  # noqa: PLC0415

    store, chain_mod, pair, _worker = _services()
    prefs = store.read_prefs(user)
    sym = str(body.get("symbol") or "").strip().upper()
    if not sym:
        raise chain_mod.ChainError("A ticker is needed.")
    try:
        target, worst = float(body.get("target")), float(body.get("worst"))
    except (TypeError, ValueError):
        raise chain_mod.ChainError("The expected and worst-case prices must be numbers.") from None
    if target <= 0 or worst <= 0:
        raise chain_mod.ChainError("The expected and worst-case prices must be positive.")
    days = body.get("days") or body.get("horizon_days")
    try:
        days = int(days) if days not in (None, "") else int(prefs["horizon_days"])
    except (TypeError, ValueError):
        raise chain_mod.ChainError("Days must be a whole number.") from None
    if not 1 <= days <= 730:
        raise chain_mod.ChainError("Days must be between 1 and 730.")
    try:
        sets = int(body.get("sets") or 1)
    except (TypeError, ValueError):
        sets = 1
    sets = max(1, min(sets, 10_000))
    today = clock.et_date()
    with screener_db.session() as s:
        ch = chain_mod.load_chain(s, sym)
    if not ch.spot or ch.spot <= 0:
        raise chain_mod.ChainError("the last market pass has no spot price for %s" % ch.symbol)
    legs, resolved = chain_mod.resolve_legs(ch, body.get("legs") or [], today)
    res = evaluate(legs, Scenario(ch.spot, target, worst, days, sets=sets))
    figs = store.figures_from(res)
    atm = pair._atm_iv(ch, resolved[0]["expiry"], ch.spot)
    p = pair._prob_profit(res, ch.spot, atm, days / 365.0, res_r(res))
    figs.update({"prob_profit": None if p != p else round(p, 4), "atm_iv": round(atm, 4),
                 "direction": "bull" if target > ch.spot else "bear" if target < ch.spot else "flat",
                 "earnings_in_window": bool(ch.earnings_date and today.isoformat() < ch.earnings_date
                                            <= (today + _dt.timedelta(days=days)).isoformat()),
                 "earnings_date": ch.earnings_date})
    return _clean({"ok": True, "symbol": ch.symbol, "spot": ch.spot, "spot_src": ch.spot_src,
                   "as_of": _iso(ch.as_of), "as_of_et": _et(ch.as_of), "session": ch.session,
                   "horizon_days": days, "target_date": (today + _dt.timedelta(days=days)).isoformat(),
                   "today": today.isoformat(), "sets": sets, "legs": resolved, "figures": figs,
                   "curves": store.curves_from(res)})


def res_r(res) -> float:
    """The rate the engine priced with (the platform's one rate)."""
    from ..services.opt_constants import RISK_FREE  # noqa: PLC0415

    return RISK_FREE


@router.post("/api/preview")
async def api_preview(request: Request, user: User = Depends(require_user)):
    body = await _json_body(request)
    if body is None:
        return _err("The request was not in the expected form.")
    try:
        _store, chain_mod, _pair, _worker = _services()
    except _Missing as exc:
        return _err(str(exc), 503)
    try:
        return JSONResponse(await run_in_threadpool(_preview_payload, user, body))
    except chain_mod.ChainError as exc:
        return JSONResponse({"ok": False, "error": str(exc)})
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)})
    except Exception as exc:  # noqa: BLE001
        log.exception("calc preview failed")
        return JSONResponse({"ok": False, "error": "The option data could not be read just now (%s)." % type(exc).__name__})
