"""The Options Screener page (v4.136) - a Barchart-style options screener on Massive data.

OPTIONS_SCREENER_DESIGN.md is the contract: §1 (what Barchart's screener does), §5 (the
engine API this module calls), §7 (saved screeners), §8 (this page and its endpoints).

* ``GET /options`` - the page: header (title, GO TO, the screen's one-liner, the data
  line with the collector's status dot), tabs SET FILTERS | RESULTS. ``?screen=`` picks
  the screener (unknown -> the Options Screener). The member's default saved screener
  for that screen loads first; otherwise the screen's built-in defaults.
* ``/options/api/*`` - the JSON the page talks to (screens, spec, run, csv, status,
  saved screeners). The member's saved screeners live in the main DB
  (``models.OptionScreen``); members see and change only their own.

The screening engine (``services/screener``) needs numpy, so it is imported LAZILY inside
the handlers: a server that has not run ``pip install -r app/requirements.txt`` yet still
boots, the page renders with a plain notice, and the API answers 503 with the reason.

This router is included BEFORE ``routes/options.py`` in ``main.py`` so its fixed paths
(``/options``, ``/options/api/...``) win over that module's ``/options/{symbol}``
catch-all, and it carries the ``require_menu("options")`` gate there.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import logging
import math
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .. import menus
from ..db import get_db
from ..models import OptionScreen, User
from ..security import require_user
from ..services import clock
from ..services.opt_constants import RISK_FREE

log = logging.getLogger(__name__)

router = APIRouter(prefix="/options", tags=["options-page"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

DEFAULT_SCREEN = "options-screener"
PER_PAGE = 100                 # rows a page (Barchart's 100)
MAX_PER_PAGE = 500
CSV_LIMIT = 1000               # rows in a download (§8)
MAX_FILTERS = 60               # filter cards in one payload
MAX_NAME = 80                  # a saved screener's name
MAX_SAVED_PER_SCREEN = 50      # saved screeners per member per screen
MAX_PAYLOAD_BYTES = 32_000     # the cleaned payload, as JSON
MAX_LIST_VALUES = 200          # values in one choice filter
STALE_ACTIVE_S = 10 * 60       # heartbeat older than this in the collector's working hours -> amber
STALE_IDLE_S = 3 * 60 * 60     # ...and outside them (nights, weekends, holidays)
ACTIVE_FROM = _dt.time(7, 30)  # the collector's working day, ET (universe at 07:30 ...
ACTIVE_TO = _dt.time(21, 0)    # ... stock days after 20:00)

VIEW_LABELS = {"main": "Main view", "filter": "Filter view", "greeks": "Greeks view",
               "vol": "Volatility view"}
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_STATES = {
    "starting": "starting up",
    "universe": "refreshing the list of optionable stocks",
    "pass": "reading the option market",
    "stocks": "filing the day's stock prices",
    "history": "building IV history",
    "idle": "idle, waiting for the next market pass",
    "error": "error",
    "stopped": "stopped",
}


# ─────────────────────────────────────── helpers ───────────────────────────────────────

def _err(msg: str, status: int = 400, code: str = "bad_request") -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg, "code": code}, status_code=status)


def _as_dict(x) -> dict:
    """A plain dict view of an engine object (dict, dataclass, namedtuple, simple object)."""
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    if dataclasses.is_dataclass(x) and not isinstance(x, type):
        return dataclasses.asdict(x)
    if hasattr(x, "_asdict"):
        return dict(x._asdict())
    if hasattr(x, "__dict__"):
        return {k: v for k, v in vars(x).items() if not k.startswith("_")}
    return {}


def _jsonable(x):
    """Engine output made safe for JSON: numpy scalars / arrays to Python, NaN and inf to
    None, dates to ISO, dataclasses to dicts."""
    if x is None or isinstance(x, (str, bool, int)):
        return x
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in x]
    if isinstance(x, (_dt.datetime, _dt.date)):
        return x.isoformat()
    tolist = getattr(x, "tolist", None)            # numpy scalar or array
    if callable(tolist):
        try:
            return _jsonable(tolist())
        except Exception:  # noqa: BLE001
            pass
    if dataclasses.is_dataclass(x) and not isinstance(x, type):
        return _jsonable(dataclasses.asdict(x))
    return str(x)


def _screen_key(v) -> str:
    s = str(v or "").strip().lower()
    return s if _SLUG.match(s) else ""


def _int(v, default: int, lo: int, hi: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _scalar(x):
    """One filter value: a finite number, a bool, a short string, or None."""
    if x is None or isinstance(x, bool):
        return x
    if isinstance(x, int):
        return x
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, str):
        return x.strip()[:40]
    return None


def _clean_payload(p) -> tuple[dict, str | None]:
    """The §5 payload, validated and trimmed to the keys the engine reads. Returns
    (payload, None) or ({}, a plain-words problem)."""
    if p is None:
        p = {}
    if not isinstance(p, dict):
        return {}, "The screener settings were not in the expected form."
    filters = p.get("filters") or []
    if not isinstance(filters, list):
        return {}, "The screener settings were not in the expected form (filters)."
    if len(filters) > MAX_FILTERS:
        return {}, f"Too many filters: {len(filters)}. A screener holds at most {MAX_FILTERS}."
    out = []
    for f in filters:
        if not isinstance(f, dict):
            continue
        key = f.get("f")
        if not isinstance(key, str) or not key.strip() or len(key.strip()) > 60:
            continue
        item: dict[str, Any] = {"f": key.strip()}
        op = f.get("op")
        if isinstance(op, str) and 0 < len(op) <= 12:
            item["op"] = op.strip()
        for b in ("lo", "hi"):
            if b in f:
                item[b] = _scalar(f[b])
        if "v" in f:
            v = f["v"]
            item["v"] = [_scalar(x) for x in v[:MAX_LIST_VALUES]] if isinstance(v, list) else _scalar(v)
        out.append(item)
    # no "filters" key = the screen's defaults (engine §5); an empty list = no filters
    clean: dict[str, Any] = {"filters": out} if "filters" in p else {}
    sort = p.get("sort")
    if isinstance(sort, dict) and isinstance(sort.get("col"), str) and sort["col"].strip():
        clean["sort"] = {"col": sort["col"].strip()[:60], "dir": "asc" if sort.get("dir") == "asc" else "desc"}
    view = p.get("view")
    clean["view"] = view.strip() if isinstance(view, str) and 0 < len(view.strip()) <= 20 else "main"
    clean["flag_earnings"] = bool(p.get("flag_earnings"))
    if len(json.dumps(clean)) > MAX_PAYLOAD_BYTES:
        return {}, "The screener settings are too large to save or run."
    return clean, None


# ─────────────────────────────────────── the engine ───────────────────────────────────────

def _missing_reason(exc: BaseException) -> str:
    text = str(exc)
    if "numpy" in text.lower():
        return ("The screener cannot run on this server yet: the numpy package is not installed. "
                "An administrator runs  pip install -r app\\requirements.txt  on the server and "
                "restarts the app.")
    return ("The screener engine is not installed on this server yet "
            f"({text or type(exc).__name__}). An administrator updates the server and restarts the app.")


def _load_engine():
    """``(engine, None)`` or ``(None, reason)``. Imported here, not at module top, so the
    app boots without numpy. Tests replace this function with a fake engine."""
    try:
        from ..services.screener import engine as eng
    except ImportError as exc:
        return None, _missing_reason(exc)
    except Exception as exc:  # noqa: BLE001 - a broken engine must not take the page down
        log.exception("options screener: the engine failed to import")
        return None, f"The screener engine failed to start on this server ({type(exc).__name__}: {exc})."
    return eng, None


def _screen_row(d: dict, family: str = "") -> dict:
    key = _screen_key(d.get("key") or d.get("slug") or d.get("id"))
    legs = d.get("legs")
    return {
        "key": key,
        "label": str(d.get("label") or d.get("name") or d.get("title") or key),
        "family": str(d.get("family") or family or "Screeners"),
        "desc": str(d.get("desc") or d.get("description") or d.get("blurb") or ""),
        "strategy": bool(d.get("strategy")) or (isinstance(legs, (list, tuple)) and len(legs) > 1),
    }


def _norm_screens(raw) -> list[dict]:
    """``engine.screens()`` as a flat ordered list of ``{key, label, family, desc,
    strategy}``. Accepts a flat list (each with ``family``) or a grouped one
    (``[{family|label, screens|items: [...]}]``)."""
    out: list[dict] = []
    if isinstance(raw, dict):            # {family: [screens]}
        raw = [{"family": k, "screens": v} for k, v in raw.items()]
    for item in raw or []:
        d = _as_dict(item)
        kids = d.get("screens", d.get("items"))
        if isinstance(kids, (list, tuple)):
            fam = str(d.get("family") or d.get("label") or d.get("name") or "")
            out.extend(_screen_row(_as_dict(k), fam) for k in kids)
        else:
            out.append(_screen_row(d))
    seen, rows = set(), []
    for r in out:
        if r["key"] and r["key"] not in seen:
            seen.add(r["key"])
            rows.append(r)
    return rows


def _families(screens: list[dict]) -> list[dict]:
    fams: dict[str, list[dict]] = {}
    for s in screens:
        fams.setdefault(s["family"], []).append(s)
    return [{"label": k, "screens": v} for k, v in fams.items()]


def _norm_choice(c):
    if isinstance(c, (list, tuple)) and c:
        return {"v": _jsonable(c[0]), "label": str(c[1] if len(c) > 1 else c[0])}
    d = _as_dict(c) if not isinstance(c, (str, int, float, bool)) else None
    if d:
        v = d.get("v", d.get("value", d.get("key")))
        return {"v": _jsonable(v), "label": str(d.get("label") or d.get("name") or v)}
    return {"v": _jsonable(c), "label": str(c)}


def _norm_preset(p, kind: str = "range"):
    """A preset chip as ``{label, op, lo?, hi?, v?}``. Without an explicit op: lo/hi ->
    between / gte / lte; a single ``v`` -> ``within`` (date: "within N days"), ``is``
    (bool), ``in`` (choice / a list) or ``eq``."""
    if isinstance(p, (list, tuple)) and p:
        if len(p) >= 3:
            out = {"label": str(p[0]), "lo": _jsonable(p[1]), "hi": _jsonable(p[2])}
        else:
            out = {"label": str(p[0]), "v": _jsonable(p[1] if len(p) > 1 else p[0])}
    elif isinstance(p, str):
        out = {"label": p, "v": [p]}
    else:
        d = _as_dict(p)
        if not d:
            return None
        out = {"label": str(d.get("label") or d.get("name") or "")}
        for src, dst in (("lo", "lo"), ("min", "lo"), ("hi", "hi"), ("max", "hi"), ("v", "v"),
                         ("value", "v"), ("op", "op")):
            if src in d and dst not in out:
                out[dst] = _jsonable(d[src])
    if "op" not in out:
        lo, hi = out.get("lo"), out.get("hi")
        if lo is not None and hi is not None:
            out["op"] = "between"
        elif lo is not None:
            out["op"] = "gte"
        elif hi is not None:
            out["op"] = "lte"
        elif "v" in out:
            if isinstance(out["v"], list) or kind == "choice":
                out["op"] = "in"
                out["v"] = out["v"] if isinstance(out["v"], list) else [out["v"]]
            else:
                out["op"] = {"date": "within", "bool": "is"}.get(kind, "eq")
    return out if out.get("label") else None


def _norm_field(d: dict, group: str | None = None) -> dict | None:
    key = d.get("key") or d.get("f") or d.get("name")
    if not key:
        return None
    choices = d.get("choices") or []
    if isinstance(choices, dict):
        choices = list(choices.items())
    kind = str(d.get("kind") or ("choice" if choices else "range")).lower()
    ops = [str(o) for o in (d.get("ops") or []) if isinstance(o, str)]
    out = {
        "key": str(key),
        "label": str(d.get("label") or key),
        "group": str(d.get("group") or group or "Other"),
        "level": str(d.get("level") or ""),
        "kind": kind,
        "unit": str(d.get("unit") or ""),
        "fmt": str(d.get("fmt") or ""),
        "help": str(d.get("help") or ""),
        "presets": [p for p in (_norm_preset(x, kind) for x in (d.get("presets") or [])) if p],
        "choices": [_norm_choice(c) for c in choices],
        "ops": ops,
    }
    if d.get("within"):
        out["within"] = str(d["within"])
    return out


def _norm_col(c) -> dict | None:
    if isinstance(c, str):
        return {"key": c, "label": c, "unit": "", "fmt": ""}
    d = _as_dict(c)
    key = d.get("key") or d.get("col")
    if not key:
        return None
    out = {"key": str(key), "label": str(d.get("label") or key), "unit": str(d.get("unit") or ""),
           "fmt": str(d.get("fmt") or "")}
    if d.get("help"):
        out["help"] = str(d["help"])
    return out


def _norm_sort(s):
    if isinstance(s, str) and s.strip():
        s = s.strip()
        return {"col": s.lstrip("-+"), "dir": "desc" if s.startswith("-") else "asc"}
    if isinstance(s, (list, tuple)) and s:
        return {"col": str(s[0]), "dir": "asc" if (len(s) > 1 and str(s[1]).lower() == "asc") else "desc"}
    d = _as_dict(s) if s is not None else {}
    if d.get("col") or d.get("key"):
        return {"col": str(d.get("col") or d.get("key")), "dir": "asc" if d.get("dir") == "asc" else "desc"}
    return None


def _norm_default_filter(f) -> dict | None:
    if isinstance(f, str):
        return {"f": f}
    d = _as_dict(f)
    key = d.get("f") or d.get("field") or d.get("key")
    if not key:
        return None
    out = {"f": str(key)}
    for k in ("op", "lo", "hi", "v"):
        if k in d:
            out[k] = _jsonable(d[k])
    return out


def _norm_spec(raw, key: str, screens: list[dict]) -> dict:
    """``engine.spec(key)`` in the one shape the page reads:

    ``{screen: {key, label, family, desc, strategy, legs}, defaults: {filters, sort,
    view, flag_earnings}, fields: [{key, label, group, level, kind, unit, fmt, help,
    presets: [{label, op, lo, hi, v}], choices: [{v, label}]}], groups: [names in
    order], views: [{key, label, columns: [{key, label, unit, fmt}]}]}`` plus any other
    keys the engine sends (passed through)."""
    raw = _as_dict(raw)
    meta = _as_dict(raw.get("screen")) if not isinstance(raw.get("screen"), str) else {}
    base = next((s for s in screens if s["key"] == key), {})
    legs = _jsonable(meta.get("legs", raw.get("legs")) or [])
    screen = {
        "key": key,
        "label": str(meta.get("label") or raw.get("label") or base.get("label") or key),
        "family": str(meta.get("family") or raw.get("family") or base.get("family") or ""),
        "desc": str(meta.get("desc") or meta.get("description") or raw.get("desc")
                    or raw.get("description") or base.get("desc") or ""),
        "legs": legs,
    }
    screen["strategy"] = bool(meta.get("strategy", raw.get("strategy", base.get("strategy")))) \
        or (isinstance(legs, list) and len(legs) > 1)

    # fields (Add a Filter) - a list, {group: [fields]}, {key: field}, or groups=[{label, fields}]
    fields: list[dict] = []
    order: list[str] = []

    def take(items, group=None):
        for f in items or []:
            d = {"key": f} if isinstance(f, str) else _as_dict(f)
            fd = _norm_field(d, group)
            if fd:
                fields.append(fd)

    fr = raw.get("fields")
    if isinstance(fr, dict):
        for k, v in fr.items():
            if isinstance(v, (list, tuple)):
                order.append(str(k))
                take(v, str(k))
            else:
                take([dict(_as_dict(v), key=_as_dict(v).get("key") or k)])
    elif isinstance(fr, (list, tuple)):
        take(fr)
    gr = raw.get("groups") or raw.get("filter_groups")
    if not gr and isinstance(raw.get("filters"), (list, tuple)) and any(
            isinstance(_as_dict(g).get("fields"), (list, tuple)) for g in raw["filters"]):
        gr = raw["filters"]                        # the engine: filters = [{group, fields}]
    if isinstance(gr, (list, tuple)):
        for g in gr:
            if isinstance(g, str):
                order.append(g)
                continue
            gd = _as_dict(g)
            name = str(gd.get("label") or gd.get("name") or gd.get("group") or gd.get("key") or "")
            if name:
                order.append(name)
            if isinstance(gd.get("fields"), (list, tuple)):
                take(gd["fields"], name or None)
    take([c for c in (raw.get("cards") or []) if c])   # the default cards' own field specs
    seen, uniq = set(), []
    for f in fields:
        if f["key"] not in seen:
            seen.add(f["key"])
            uniq.append(f)
    groups: list[str] = []
    for g in order + [f["group"] for f in uniq]:
        if g and g not in groups and any(f["group"] == g for f in uniq):
            groups.append(g)

    # defaults - a payload dict or a list of filters
    dflt = raw.get("defaults", raw.get("default_payload", raw.get("default_filters")))
    filters_src, sort, view, flag = [], None, "main", False
    if isinstance(dflt, dict):
        filters_src = dflt.get("filters") or []
        sort = dflt.get("sort")
        view = dflt.get("view") or "main"
        flag = bool(dflt.get("flag_earnings"))
    elif isinstance(dflt, (list, tuple)):
        filters_src = dflt
    if sort is None:
        sort = raw.get("sort", raw.get("default_sort", meta.get("sort", meta.get("default_sort"))))
    defaults = {"filters": [f for f in (_norm_default_filter(x) for x in filters_src) if f],
                "sort": _norm_sort(sort), "view": str(view), "flag_earnings": flag}

    # views - {key: [cols]} | {key: {label, columns}} | [{key, label, columns}]
    views: list[dict] = []
    vr = raw.get("views")
    if isinstance(vr, dict):
        vr = [dict({"key": k}, **(_as_dict(v) if not isinstance(v, (list, tuple)) else {"columns": v}))
              for k, v in vr.items()]
    for v in vr or []:
        vd = {"key": v} if isinstance(v, str) else _as_dict(v)
        k = str(vd.get("key") or "")
        if not k:
            continue
        cols = [c for c in (_norm_col(x) for x in (vd.get("columns") or vd.get("cols") or [])) if c]
        views.append({"key": k, "label": str(vd.get("label") or VIEW_LABELS.get(k, k.title())), "columns": cols})
    if not views:
        views = [{"key": k, "label": lbl, "columns": []} for k, lbl in VIEW_LABELS.items()]

    known = {"screen", "fields", "groups", "filter_groups", "filters", "cards", "defaults", "default_payload",
             "default_filters", "views", "sort", "default_sort", "label", "family", "desc",
             "description", "legs", "strategy", "key"}
    extra = {k: _jsonable(v) for k, v in raw.items() if k not in known}
    return {**extra, "screen": screen, "defaults": defaults, "fields": uniq, "groups": groups,
            "views": views}


# ─────────────────────────────────────── status ───────────────────────────────────────

def _naive_utc(ts) -> _dt.datetime | None:
    if not isinstance(ts, _dt.datetime):
        return None
    if ts.tzinfo is not None:
        ts = ts.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return ts


def _iso(ts) -> str | None:
    ts = _naive_utc(ts)
    return ts.isoformat(timespec="seconds") + "Z" if ts else None


def _read_collector() -> dict:
    """The screener collector's heartbeat (``scr_status`` row 1) and its newest passes,
    read from the screener DB. Never raises; never creates a missing SQLite file."""
    try:
        from .. import screener_db
        from ..screener_models import ScrPass, ScrStatus
    except Exception as exc:  # noqa: BLE001 - the data part is not deployed yet
        return {"available": False, "why": f"screener tables not installed ({type(exc).__name__})"}
    try:
        url = screener_db.database_url()
        if url.startswith("sqlite"):
            path = url.split(":///", 1)[-1] if ":///" in url else ""
            if path and not path.startswith(":memory:") and not Path(path).exists():
                return {"available": False, "why": "no screener database yet"}
        with screener_db.session() as s:
            st = s.get(ScrStatus, 1)
            last = (s.query(ScrPass).filter(ScrPass.finished.isnot(None))
                    .order_by(ScrPass.id.desc()).first())
            running = (s.query(ScrPass).filter(ScrPass.finished.is_(None))
                       .order_by(ScrPass.id.desc()).first())

            def row(o, cols):
                return {c: getattr(o, c, None) for c in cols} if o is not None else None

            return {
                "available": True,
                "status": row(st, ("state", "detail", "heartbeat", "pid", "version", "pass_id",
                                   "symbols_total", "symbols_done", "universe_n", "universe_on",
                                   "history_done_n", "history_total", "last_error", "api_ok")),
                "last_pass": row(last, ("id", "kind", "session", "started", "finished", "n_symbols",
                                        "n_ok", "n_failed", "n_contracts", "ms")),
                "running_pass": row(running, ("id", "kind", "session", "started", "n_symbols")),
            }
    except Exception as exc:  # noqa: BLE001 - missing tables, locked file, bad URL
        log.debug("options screener: status read failed: %s", exc)
        return {"available": False, "why": "the screener database is not ready yet"}


def _frame_meta(eng) -> dict | None:
    """The engine's loaded-data summary (§5 ``frame.meta``), or None."""
    if eng is None:
        return None
    try:
        for name in ("frame_meta", "meta", "data_meta"):
            fn = getattr(eng, name, None)
            if callable(fn):
                return _jsonable(_as_dict(fn())) or None
        fr = getattr(eng, "frame", None)
        if not callable(getattr(fr, "current", None)):
            from ..services.screener import frame as fr     # the engine package's loader
        return _jsonable(_as_dict(getattr(fr.current(), "meta", None))) or None
    except Exception as exc:  # noqa: BLE001
        log.debug("options screener: frame meta failed: %s", exc)
    return None


def _compact(n) -> str:
    if n is None:
        return "-"
    n = float(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 100_000:
        return f"{n / 1_000:.0f}K"
    return f"{n:,.0f}"


def _count(n) -> str:
    return "-" if n is None else f"{int(n):,}"


def _idle_hours(now_utc: _dt.datetime) -> bool:
    et = clock.et_now(now_utc)
    if not clock.is_trading_day(et.date()):
        return True
    return not (ACTIVE_FROM <= et.time() < ACTIVE_TO)


def _status_view(col: dict, meta: dict | None = None, now: _dt.datetime | None = None,
                 engine_error: str | None = None) -> dict:
    """The data line and the status dot (§8; dashboard-visibility rule).

    dot: ``green`` running / idle · ``amber`` heartbeat stale (> 10 min in the collector's
    working hours, > 3 h outside them) or stopped · ``rose`` error · ``slate`` not started."""
    now = _naive_utc(now) or _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    meta = meta or {}
    st = (col or {}).get("status") or None
    last = (col or {}).get("last_pass") or None
    running = (col or {}).get("running_pass") or None

    parts = ["Massive", "15-min delayed", "prices estimated from IV"]
    fin = _naive_utc((last or {}).get("finished"))
    if fin:
        et = clock.et_now(fin)
        today = clock.et_now(now).date()
        when = et.strftime("%H:%M") if et.date() == today else f"{et.strftime('%b')} {et.day} {et.strftime('%H:%M')}"
        parts.append(f"last market pass {when} ET")
    else:
        parts.append("no market pass yet")
    n_und = meta.get("n_underlyings")
    if n_und is None and last:
        n_und = last.get("n_ok") if last.get("n_ok") is not None else last.get("n_symbols")
    if n_und is None and st:
        n_und = st.get("universe_n")
    n_con = meta.get("n_contracts", (last or {}).get("n_contracts"))
    if n_und is not None:
        parts.append(f"{_count(n_und)} underlying" + ("" if n_und == 1 else "s"))
    if n_con is not None:
        parts.append(f"{_compact(n_con)} contracts")
    if st and st.get("history_total"):
        parts.append(f"IV history {_count(st.get('history_done_n') or 0)} / {_count(st.get('history_total'))}")
    elif meta.get("iv_history_total"):
        parts.append(f"IV history {_count(meta.get('iv_history_done') or 0)} / {_count(meta['iv_history_total'])}")

    hb = _naive_utc((st or {}).get("heartbeat"))
    age = (now - hb).total_seconds() if hb else None
    state = (st or {}).get("state") or ""
    if not col or not col.get("available") or not st:
        dot, label = "slate", "The market collector has not started yet - no option data to screen."
    elif state == "error":
        why = (st.get("last_error") or st.get("detail") or "").strip()
        dot, label = "rose", "Collector error" + (f": {why[:160]}" if why else ".")
    elif state == "stopped":
        dot, label = "amber", "The market collector is stopped - the data is not being refreshed."
    elif age is None or age > (STALE_IDLE_S if _idle_hours(now) else STALE_ACTIVE_S):
        mins = int(age // 60) if age is not None else None
        dot = "amber"
        label = ("The collector has not checked in"
                 + (f" for {mins} min" if mins is not None else "") + " - it may have stopped.")
    else:
        dot = "green"
        label = "Collector " + _STATES.get(state, state or "running")
        if state == "pass" and st.get("symbols_total"):
            label += f" ({_count(st.get('symbols_done') or 0)} / {_count(st.get('symbols_total'))})"
        elif st.get("detail") and state not in ("idle",):
            label += f" - {str(st['detail'])[:120]}"
        label += "."
    if engine_error:
        label += " " + engine_error

    return {
        "ok": True,
        "dot": dot,
        "label": label,
        "line": " · ".join(parts),
        "parts": parts,
        "heartbeat": _iso(hb),
        "heartbeat_age_s": int(age) if age is not None else None,
        "state": state or None,
        "collector": _jsonable({k: (_iso(v) if isinstance(v, _dt.datetime) else v)
                                for k, v in (st or {}).items()}) if st else None,
        "last_pass": _jsonable({k: (_iso(v) if isinstance(v, _dt.datetime) else v)
                                for k, v in (last or {}).items()}) if last else None,
        "running_pass": _jsonable({k: (_iso(v) if isinstance(v, _dt.datetime) else v)
                                   for k, v in (running or {}).items()}) if running else None,
        "frame": meta or None,
        "engine_ok": engine_error is None,
        "engine_error": engine_error,
    }


# ─────────────────────────────────────── saved screeners ───────────────────────────────────────

def _item(r: OptionScreen) -> dict:
    return {"id": r.id, "name": r.name, "screen": r.screen_key, "is_default": bool(r.is_default),
            "payload": r.payload or {}, "updated_at": _iso(r.updated_at or r.created_at)}


def _saved_rows(db: Session, user: User, key: str) -> list[OptionScreen]:
    return (db.query(OptionScreen)
            .filter(OptionScreen.user_id == user.id, OptionScreen.screen_key == key)
            .order_by(OptionScreen.name.asc(), OptionScreen.id.asc()).all())


def _saved_list(db: Session, user: User, key: str) -> list[dict]:
    if not key:
        return []
    return [_item(r) for r in _saved_rows(db, user, key)]


def _own(db: Session, user: User, sid: int) -> OptionScreen | None:
    return (db.query(OptionScreen)
            .filter(OptionScreen.id == sid, OptionScreen.user_id == user.id).first())


def _set_default(db: Session, user: User, row: OptionScreen, on: bool) -> None:
    if on:
        for r in _saved_rows(db, user, row.screen_key):
            if r.id != row.id and r.is_default:
                r.is_default = False
    row.is_default = bool(on)


def _chart_url(user: User) -> str:
    """Where a Symbol cell links: the first chart page this member may open
    (``{sym}`` is replaced in the page)."""
    if menus.user_can(user, "sector"):
        return "/sector/chart-window?symbol={sym}"
    if menus.user_can(user, "matp"):
        return "/matp?symbol={sym}"
    if menus.user_can(user, "company_analysis"):
        return "/company-analysis?symbol={sym}"
    return ""


# ─────────────────────────────────────── the page ───────────────────────────────────────

@router.get("", response_class=HTMLResponse)
def options_home(request: Request, screen: str = "", user: User = Depends(require_user),
                 db: Session = Depends(get_db)):
    """The screener page. ``?screen=<key>`` (unknown -> the Options Screener)."""
    eng, engine_error = _load_engine()
    screens: list[dict] = []
    spec: dict | None = None
    if eng is not None:
        try:
            screens = _norm_screens(eng.screens())
        except Exception as exc:  # noqa: BLE001
            log.exception("options screener: screens() failed")
            engine_error = f"The screener list could not be loaded ({type(exc).__name__})."
    keys = {s["key"] for s in screens}
    key = _screen_key(screen)
    if key not in keys:
        key = DEFAULT_SCREEN if (DEFAULT_SCREEN in keys or not screens) else screens[0]["key"]
    if eng is not None and screens:
        try:
            spec = _norm_spec(eng.spec(key), key, screens)
        except Exception as exc:  # noqa: BLE001
            log.exception("options screener: spec(%s) failed", key)
            engine_error = f"This screener could not be loaded ({type(exc).__name__})."
    cur = next((s for s in screens if s["key"] == key),
               {"key": key, "label": "Options Screener", "family": "", "desc": "", "strategy": False})
    if spec:
        cur = {**cur, **{k: spec["screen"][k] for k in ("label", "desc") if spec["screen"].get(k)}}
    saved = _saved_list(db, user, key)
    default = next((i for i in saved if i["is_default"]), None)
    status = _status_view(_read_collector(), None, engine_error=None)
    boot = {
        "screen": key,
        "engine_ok": spec is not None and engine_error is None,
        "engine_error": engine_error,
        "spec": spec,
        "saved": saved,
        "default_id": default["id"] if default else None,
        "chart_url": _chart_url(user),
        "risk_free": RISK_FREE,
        "per_page": PER_PAGE,
        "csv_limit": CSV_LIMIT,
        "max_filters": MAX_FILTERS,
        "max_name": MAX_NAME,
        "today": clock.et_today(),
    }
    return templates.TemplateResponse(request, "options.html", {
        "user": user, "screens": screens, "families": _families(screens), "screen": cur,
        "status": status, "engine_error": engine_error, "boot": boot,
    })


# ─────────────────────────────────────── the API ───────────────────────────────────────

@router.get("/api/screens")
def api_screens(user: User = Depends(require_user)):
    eng, why = _load_engine()
    if eng is None:
        return _err(why, 503, "engine_missing")
    try:
        screens = _norm_screens(eng.screens())
    except Exception:  # noqa: BLE001
        log.exception("options screener: screens() failed")
        return _err("The screener list could not be loaded.", 500, "engine_failed")
    return {"ok": True, "screens": screens, "families": _families(screens)}


@router.get("/api/spec")
def api_spec(screen: str = "", user: User = Depends(require_user), db: Session = Depends(get_db)):
    eng, why = _load_engine()
    if eng is None:
        return _err(why, 503, "engine_missing")
    try:
        screens = _norm_screens(eng.screens())
        key = _screen_key(screen)
        if key not in {s["key"] for s in screens}:
            return _err("There is no screener by that name.", 404, "unknown_screen")
        spec = _norm_spec(eng.spec(key), key, screens)
    except Exception:  # noqa: BLE001
        log.exception("options screener: spec(%s) failed", screen)
        return _err("This screener could not be loaded.", 500, "engine_failed")
    return {"ok": True, **spec, "saved": _saved_list(db, user, key)}


async def _body(request: Request) -> tuple[dict | None, str | None]:
    """The request body as a dict: JSON, or a form whose ``payload`` field is JSON."""
    ctype = (request.headers.get("content-type") or "").lower()
    try:
        if "application/json" in ctype or not ctype:
            raw = await request.body()
            body = json.loads(raw or b"{}")
        else:
            form = await request.form()
            body = {k: form.get(k) for k in ("screen", "page", "per_page", "limit")}
            body["payload"] = json.loads(form.get("payload") or "{}")
    except (ValueError, UnicodeDecodeError):
        return None, "The request was not valid JSON."
    if not isinstance(body, dict):
        return None, "The request was not in the expected form."
    return body, None


def _prepare(eng, body: dict) -> tuple[str, dict, JSONResponse | None]:
    try:
        keys = {s["key"] for s in _norm_screens(eng.screens())}
    except Exception:  # noqa: BLE001
        log.exception("options screener: screens() failed")
        return "", {}, _err("The screener list could not be loaded.", 500, "engine_failed")
    key = _screen_key(body.get("screen"))
    if key not in keys:
        return "", {}, _err("There is no screener by that name.", 404, "unknown_screen")
    payload, problem = _clean_payload(body.get("payload"))
    if problem:
        return "", {}, _err(problem, 400, "bad_payload")
    return key, payload, None


@router.post("/api/run")
async def api_run(request: Request, user: User = Depends(require_user)):
    """``{screen, payload, page, per_page}`` -> ``engine.run()`` (§5) as JSON."""
    body, why = await _body(request)
    if body is None:
        return _err(why, 400, "bad_request")
    eng, missing = _load_engine()
    if eng is None:
        return _err(missing, 503, "engine_missing")
    key, payload, bad = _prepare(eng, body)
    if bad is not None:
        return bad
    page = _int(body.get("page"), 1, 1, 1_000_000)
    per_page = _int(body.get("per_page"), PER_PAGE, 1, MAX_PER_PAGE)
    try:
        res = await run_in_threadpool(eng.run, key, payload, page=page, per_page=per_page)
    except Exception:  # noqa: BLE001
        log.exception("options screener: run(%s) failed", key)
        return _err("The screener failed on this request. Try again, or narrow the filters.", 500,
                    "engine_failed")
    out = _jsonable(_as_dict(res))
    out.setdefault("ok", True)
    out.setdefault("screen", key)
    return JSONResponse(out)


@router.post("/api/csv")
async def api_csv(request: Request, user: User = Depends(require_user)):
    """``{screen, payload[, limit]}`` (JSON, or a form with ``payload`` as JSON text) ->
    a CSV download of up to 1,000 rows."""
    body, why = await _body(request)
    if body is None:
        return _err(why, 400, "bad_request")
    eng, missing = _load_engine()
    if eng is None:
        return _err(missing, 503, "engine_missing")
    key, payload, bad = _prepare(eng, body)
    if bad is not None:
        return bad
    limit = _int(body.get("limit"), CSV_LIMIT, 1, CSV_LIMIT)
    try:
        text = await run_in_threadpool(eng.csv, key, payload, limit=limit)
    except Exception:  # noqa: BLE001
        log.exception("options screener: csv(%s) failed", key)
        return _err("The download failed. Try again, or narrow the filters.", 500, "engine_failed")
    fname = f"tradehunter-{key}-{clock.et_today()}.csv"
    return Response(content=text if isinstance(text, (str, bytes)) else str(text),
                    media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.get("/api/status")
def api_status(user: User = Depends(require_user)):
    """The collector's status (screener DB) + the engine's loaded-data summary. Never
    fails: a missing screener DB or engine is reported in the body."""
    eng, missing = _load_engine()
    return _status_view(_read_collector(), _frame_meta(eng), engine_error=missing)


@router.get("/api/saved")
def api_saved(screen: str = "", user: User = Depends(require_user), db: Session = Depends(get_db)):
    key = _screen_key(screen)
    if not key:
        return _err("There is no screener by that name.", 404, "unknown_screen")
    return {"ok": True, "screen": key, "items": _saved_list(db, user, key)}


@router.post("/api/saved")
def api_saved_upsert(body: Any = Body(None), user: User = Depends(require_user),
                     db: Session = Depends(get_db)):
    """``{screen, name, payload[, is_default]}`` -> create, or update the member's saved
    screener of that name (names match ignoring case)."""
    if not isinstance(body, dict):
        return _err("The request was not in the expected form.")
    key = _screen_key(body.get("screen"))
    if not key:
        return _err("There is no screener by that name.", 404, "unknown_screen")
    name = " ".join(str(body.get("name") or "").split())
    if not name:
        return _err("Give the screener a name.")
    if len(name) > MAX_NAME:
        return _err(f"The name is too long - at most {MAX_NAME} characters.")
    payload, problem = _clean_payload(body.get("payload"))
    if problem:
        return _err(problem, 400, "bad_payload")
    rows = _saved_rows(db, user, key)
    row = next((r for r in rows if r.name.lower() == name.lower()), None)
    created = row is None
    if created:
        if len(rows) >= MAX_SAVED_PER_SCREEN:
            return _err(f"You already have {MAX_SAVED_PER_SCREEN} saved screeners for this screen - "
                        "delete one first.", 400, "limit")
        row = OptionScreen(user_id=user.id, screen_key=key, name=name, payload=payload,
                           is_default=False)
        db.add(row)
    else:
        row.name = name
        row.payload = payload
    if "is_default" in body:
        db.flush()
        _set_default(db, user, row, bool(body.get("is_default")))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _err("A screener with that name was just saved - try again.", 409, "conflict")
    db.refresh(row)
    return {"ok": True, "created": created, "item": _item(row), "items": _saved_list(db, user, key)}


@router.post("/api/saved/{sid}/default")
def api_saved_default(sid: int, body: Any = Body(None), user: User = Depends(require_user),
                      db: Session = Depends(get_db)):
    """Make this saved screener the one that loads when the screen opens (``{"on": false}``
    clears it)."""
    row = _own(db, user, sid)
    if row is None:
        return _err("That saved screener was not found.", 404, "not_found")
    on = not (isinstance(body, dict) and body.get("on") is False)
    _set_default(db, user, row, on)
    db.commit()
    return {"ok": True, "item": _item(row), "items": _saved_list(db, user, row.screen_key)}


@router.post("/api/saved/{sid}/delete")
def api_saved_delete(sid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    row = _own(db, user, sid)
    if row is None:
        return _err("That saved screener was not found.", 404, "not_found")
    key = row.screen_key
    db.delete(row)
    db.commit()
    return {"ok": True, "items": _saved_list(db, user, key)}
