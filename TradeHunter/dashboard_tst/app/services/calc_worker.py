"""The option calculator's Hermes lane (OPTION_CALCULATOR_DESIGN.md step 2-3): after each
Massive market pass, recompute every member's saved setups and store the results.

A SEPARATE process from the screener collector (``deploy/option_calc_worker.py``, task
``TST-Option-Calc``): it needs both databases (the setups and results live in ``tst.db``
beside the member's other saved objects, the chain in ``screener.db``), and the collector
is left untouched. Every ``TICK_S`` it looks at the collector's newest finished pass
(``scr_store.last_pass``); every active request not yet computed on that pass (or
changed since) is computed and its rows written with the pass stamp. A request the chain
cannot serve (a leg no longer listed, no IV, an unknown ticker) keeps the reason in
``last_error`` and is tried again on the next pass or when the member changes it.

The web page's "compute now" calls :func:`compute_and_store` inline - the same code, no
waiting for a pass.

Status: a state file ``state/option_calc_worker.json`` (``STATE_KEYS``), written on every
look, read by the Hermes tray (``dashboard_intraday/tray_status.py``) and by Settings ->
Data Ingest -> Option Calculator. Timestamps are ISO with offset.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import threading
import time
from pathlib import Path

from ..models import User
from . import calc_pair, calc_store, clock, scr_store
from .calc_chain import ChainError, load_chain, resolve_legs
from .calc_store import CalcInputError, add_base, curves_from, figures_from
from .opt_constants import RISK_FREE
from .option_calc import Scenario, evaluate

log = logging.getLogger(__name__)

WORKER_VERSION = "1.0"
TICK_S = 60.0                     # one look a minute (a pass is every 30 min; a changed request waits at most this)
THIN_EVERY_S = 6 * 3600.0         # the retention pass
STATE_PATH = Path(__file__).resolve().parents[2] / "state" / "option_calc_worker.json"
STATES = ("starting", "idle", "computing", "error", "stopped")
STATE_KEYS = ("version", "pid", "heartbeat", "state", "detail", "last_look", "next_look", "last_pass_id",
              "last_pass_session", "last_pass_finished", "requests_active", "requests_due", "done",
              "failed", "last_run_ms", "last_error", "computed_at", "written_by")


def _iso(t: _dt.datetime | None) -> str | None:
    if t is None:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=_dt.timezone.utc)
    return t.isoformat(timespec="seconds")


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


# ────────────────────────────────── computing one request ──────────────────────────────────

def compute_request(sdb, req, user, *, today: _dt.date, r: float = RISK_FREE) -> tuple[list[dict], object]:
    """The rows for ``req`` from the chain's last pass (``calc_store.write_results`` shape)
    and the chain they came from. Raises ChainError / CalcInputError with a message for
    the member."""
    chain = load_chain(sdb, req.symbol)
    prefs = calc_store.criteria_for(user, req)
    if not chain.spot or chain.spot <= 0:
        raise ChainError("the last market pass has no spot price for %s" % chain.symbol)
    if req.kind == "manual":
        legs, resolved = resolve_legs(chain, req.legs, today)
        days = int(req.horizon_days or prefs["horizon_days"])
        shift = float(getattr(req, "iv_shift", None) or 0.0)     # v4.217: vol points on the target date
        res = evaluate(legs, Scenario(chain.spot, req.target, req.worst, days, sets=req.sets, r=r, iv_shift=shift / 100.0))
        figs = figures_from(res)
        curves = curves_from(res)
        if shift:
            add_base(figs, curves, evaluate(legs, Scenario(chain.spot, req.target, req.worst, days, sets=req.sets, r=r)))
        figs["iv_shift"] = shift
        for rl in resolved:
            rl["iv_target"] = None if rl.get("iv") is None else round(max(rl["iv"] + shift / 100.0, 0.0), 6)
        figs.update({"direction": "bull" if req.target > chain.spot else "bear" if req.target < chain.spot else "flat",
                     "family_label": "Your legs", "atm_iv": round(calc_pair._atm_iv(chain, resolved[0]["expiry"], chain.spot), 4)})
        p = calc_pair._prob_profit(res, chain.spot, figs["atm_iv"], days / 365.0, r)
        figs["prob_profit"] = None if p != p else round(p, 4)
        rows = [{"family": "manual", "rank": 1, "horizon_days": days,
                 "target_date": (today + _dt.timedelta(days=days)).isoformat(), "legs": resolved,
                 "figures": figs, "curves": curves, "score": None}]
        return rows, chain
    rows, summary = calc_pair.search(chain, req.target, req.worst, prefs, today, horizon_days=req.horizon_days,
                                     r=r, sets=req.sets)
    rows.append({"family": "_summary", "rank": 0, "horizon_days": None, "target_date": None, "legs": [],
                 "figures": summary, "curves": None, "score": None})
    return rows, chain


def compute_and_store(db, sdb, req, user, *, now: _dt.datetime | None = None, r: float = RISK_FREE) -> int:
    """Compute ``req`` against the newest finished pass and store the rows (the page's
    "compute now" and the lane share this). Returns the rows written; on a member-facing
    failure the reason is stored on the request and ChainError is re-raised."""
    now = now or _utcnow()
    today = clock.et_date(now)
    lp = scr_store.last_pass(sdb, min_contracts=1) or {}
    pass_id = lp.get("id")
    try:
        rows, chain = compute_request(sdb, req, user, today=today, r=r)
    except (ChainError, CalcInputError, ValueError) as exc:
        calc_store.mark_error(db, req, str(exc), pass_id=pass_id, now=now.replace(tzinfo=None))
        raise ChainError(str(exc)) from exc
    return calc_store.write_results(db, req, rows, pass_id=pass_id, session=chain.session or lp.get("session"),
                                    as_of=chain.as_of, spot=chain.spot, now=now.replace(tzinfo=None))


# ────────────────────────────────── the lane ──────────────────────────────────

class CalcWorker:
    """``look()`` once a tick; ``run_forever()`` until ``stop()``."""

    def __init__(self, *, tst_sessions, scr_sessions, state_path=None, tick_s: float = TICK_S,
                 r: float = RISK_FREE):
        self.tst_sessions = tst_sessions          # callables returning a Session each
        self.scr_sessions = scr_sessions
        self.state_path = Path(state_path) if state_path else STATE_PATH
        self.tick_s = float(tick_s)
        self.r = r
        self._stopping = threading.Event()
        self._last_thin: float = 0.0
        self.fields: dict = dict.fromkeys(STATE_KEYS)
        self.fields.update(version=WORKER_VERSION, pid=os.getpid(), state="starting", done=0, failed=0,
                           written_by="option_calc_worker")

    # ---- status -------------------------------------------------------------
    def _write_state(self, now: _dt.datetime) -> None:
        doc = dict(self.fields)
        doc["heartbeat"] = _iso(now)
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            gi = self.state_path.parent / ".gitignore"
            if not gi.exists():
                gi.write_text("*\n", encoding="utf-8")
            tmp = self.state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(doc, indent=1, default=str), encoding="utf-8")
            try:
                os.replace(tmp, self.state_path)
            except PermissionError:
                self.state_path.write_text(json.dumps(doc, indent=1, default=str), encoding="utf-8")
        except OSError as exc:                     # the status must never kill the lane
            log.warning("state file %s not written: %s", self.state_path, exc)

    def _set(self, now: _dt.datetime, **fields) -> None:
        self.fields.update(fields)
        self._write_state(now)

    # ---- one look -----------------------------------------------------------
    def look(self, now: _dt.datetime | None = None) -> dict:
        """Compute what is due on the newest finished pass. Returns a small summary."""
        now = now or _utcnow()
        t0 = time.perf_counter()
        out = {"pass_id": None, "due": 0, "done": 0, "failed": 0}
        db, sdb = self.tst_sessions(), self.scr_sessions()
        try:
            lp = scr_store.last_pass(sdb, min_contracts=1)
            if not lp:
                self._set(now, state="idle", detail="waiting for the screener collector's first market pass",
                          last_look=_iso(now), next_look=_iso(now + _dt.timedelta(seconds=self.tick_s)),
                          requests_active=len(calc_store.due_requests(db, None)), requests_due=0)
                return out
            pass_id = lp["id"]
            out["pass_id"] = pass_id
            due = calc_store.due_requests(db, pass_id)
            active = len(calc_store.due_requests(db, None))
            out["due"] = len(due)
            self._set(now, state="computing" if due else "idle", last_pass_id=pass_id,
                      last_pass_session=lp.get("session"), last_pass_finished=_iso(lp.get("finished")),
                      requests_active=active, requests_due=len(due), last_look=_iso(now),
                      detail=("%d setup%s to compute on pass %d" % (len(due), "" if len(due) == 1 else "s", pass_id))
                      if due else "up to date with pass %d · %d active setup%s" % (pass_id, active, "" if active == 1 else "s"))
            today = clock.et_date(now)
            last_err = None
            for req in due:
                if self._stopping.is_set():
                    break
                user = db.get(User, req.user_id)
                if user is None:
                    calc_store.mark_error(db, req, "the member no longer exists", pass_id=pass_id)
                    out["failed"] += 1
                    continue
                try:
                    rows, chain = compute_request(sdb, req, user, today=today, r=self.r)
                    calc_store.write_results(db, req, rows, pass_id=pass_id, session=chain.session or lp.get("session"),
                                             as_of=chain.as_of, spot=chain.spot, now=now.replace(tzinfo=None))
                    out["done"] += 1
                except (ChainError, CalcInputError, ValueError) as exc:
                    calc_store.mark_error(db, req, str(exc), pass_id=pass_id, now=now.replace(tzinfo=None))
                    out["failed"] += 1
                    last_err = "%s #%d: %s" % (req.symbol, req.id, exc)
                    log.info("request %d (%s) not computed: %s", req.id, req.symbol, exc)
                except Exception as exc:          # a bug: keep the lane alive, say so
                    db.rollback()
                    calc_store.mark_error(db, req, "could not be computed (%s)" % type(exc).__name__, pass_id=pass_id,
                                          now=now.replace(tzinfo=None))
                    out["failed"] += 1
                    last_err = "%s #%d: %s" % (req.symbol, req.id, type(exc).__name__)
                    log.exception("request %d (%s) crashed", req.id, req.symbol)
            if time.monotonic() - self._last_thin > THIN_EVERY_S:
                try:
                    n = calc_store.thin_results(db)
                    if n:
                        log.info("retention: %d old result rows removed", n)
                finally:
                    self._last_thin = time.monotonic()
            ms = int((time.perf_counter() - t0) * 1000)
            done_total = (self.fields.get("done") or 0) + out["done"]
            failed_total = (self.fields.get("failed") or 0) + out["failed"]
            self._set(_utcnow(), state="idle", done=done_total, failed=failed_total, last_run_ms=ms,
                      requests_due=0, last_error=last_err if out["failed"] else None,
                      computed_at=_iso(now) if due else self.fields.get("computed_at"),
                      next_look=_iso(now + _dt.timedelta(seconds=self.tick_s)),
                      detail="pass %d: %d computed, %d not%s · %d active setup%s · %d ms"
                             % (pass_id, out["done"], out["failed"], "" if not out["failed"] else " (see the setups)",
                                active, "" if active == 1 else "s", ms) if due
                      else "up to date with pass %d · %d active setup%s" % (pass_id, active, "" if active == 1 else "s"))
            return out
        except Exception as exc:
            log.exception("look failed")
            self._set(_utcnow(), state="error", last_error="%s: %s" % (type(exc).__name__, exc),
                      detail="the look failed; again in %d s" % int(self.tick_s),
                      next_look=_iso(now + _dt.timedelta(seconds=self.tick_s)))
            return out
        finally:
            db.close()
            sdb.close()

    # ---- the loop -----------------------------------------------------------
    def run_forever(self) -> None:
        log.info("option calc worker %s started (tick %.0f s, state %s)", WORKER_VERSION, self.tick_s, self.state_path)
        try:
            while not self._stopping.is_set():
                self.look()
                self._stopping.wait(self.tick_s)
        finally:
            self._set(_utcnow(), state="stopped", detail="stopped")
            log.info("option calc worker stopped")

    def stop(self) -> None:
        self._stopping.set()


def read_state(path=None) -> dict | None:
    """The state file as a dict, or None when there is none / it cannot be read."""
    p = Path(path) if path else STATE_PATH
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
