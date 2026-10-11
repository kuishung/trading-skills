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
* v4.137 - the collector's status (``_status_view``): a heartbeat older than 5 min is amber
  at any hour; the label is the collector's own plain-words detail; errors are told by kind
  in plain words (admins also get the fix); the status file
  ``state/screener_collector.json`` is read when the DB has nothing newer (a collector that
  could not start writes only that); ``empty`` = the results area's panel saying why there
  is nothing to screen yet. The first paint carries the same status (``boot.status``).

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
STALE_S = 5 * 60               # no heartbeat for 5 min -> amber, at ANY hour: the collector beats
                               # every 15 s around the clock (the Hermes tray uses the same 5 min)
LABEL_MAX = 240                # the collector's detail in the page label
# the collector's own status file - read when the screener DB has no status row, cannot be
# read, or holds an older heartbeat (a collector that crashed at start writes only this)
STATE_PATH = Path(__file__).resolve().parents[2] / "state" / "screener_collector.json"
CRASH_WRITER = "deploy/screener_collector.py"

VIEW_LABELS = {"main": "Main view", "filter": "Filter view", "greeks": "Greeks view",
               "vol": "Volatility view"}
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_STATES = {                    # the label when the collector sent no detail
    "starting": "starting up",
    "universe": "refreshing the list of optionable stocks",
    "pass": "reading the option market",
    "stocks": "filing the day's stock prices",
    "history": "building IV history",
    "idle": "idle, waiting for the next market pass",
    "error": "error",
    "stopped": "stopped",
}
_DOING = {                     # "(it was ...)" in the stale texts
    "starting": "starting up",
    "universe": "reading the list of optionable stocks",
    "pass": "reading the option market",
    "stocks": "loading daily stock prices",
    "history": "building IV history",
    "idle": "idle",
    "error": "reporting an error",
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
    """A datetime (aware, or naive = UTC) or ISO text (``...Z`` / ``+00:00``) -> naive UTC."""
    if isinstance(ts, str):
        s = ts.strip()
        if not s:
            return None
        if s[-1] in "Zz":
            s = s[:-1] + "+00:00"
        try:
            ts = _dt.datetime.fromisoformat(s)
        except ValueError:
            return None
    if not isinstance(ts, _dt.datetime):
        return None
    if ts.tzinfo is not None:
        ts = ts.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return ts


def _iso(ts) -> str | None:
    ts = _naive_utc(ts)
    return ts.isoformat(timespec="seconds") + "Z" if ts else None


# scr_status columns the page reads. The v4.137 ones (migration 8d2f4b6a1c37, error_kind on)
# are None while an older collector still writes the row - every reader accepts that.
_STATUS_COLS = ("state", "detail", "heartbeat", "pid", "version", "pass_id", "symbols_total",
                "symbols_done", "universe_n", "universe_on", "history_done_n", "history_total",
                "last_error", "api_ok",
                "error_kind", "next_try", "warn", "universe_done", "earnings_on", "progress")
_STATUS_TIMES = ("heartbeat", "next_try", "universe_done")


def _read_collector_db() -> dict:
    """``scr_status`` row 1 and the newest finished / unfinished passes from the screener DB.
    Never raises; never creates a missing SQLite file."""
    try:
        from sqlalchemy import or_

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
            # the data line's source: the newest finished pass that STORED contracts (a pass
            # that read nothing - e.g. v4.136's empty one - is not where the data comes from)
            real = (s.query(ScrPass).filter(ScrPass.finished.isnot(None),
                                            or_(ScrPass.n_contracts.is_(None), ScrPass.n_contracts > 0))
                    .order_by(ScrPass.id.desc()).first())

            def row(o, cols):
                return {c: getattr(o, c, None) for c in cols} if o is not None else None

            pass_cols = ("id", "kind", "session", "started", "finished", "n_symbols", "n_ok", "n_failed",
                         "n_contracts", "ms")
            return {
                "available": True,
                "source": "db",
                "status": row(st, _STATUS_COLS),
                "last_pass": row(last, pass_cols),
                "real_pass": row(real, pass_cols),
                "running_pass": row(running, ("id", "kind", "session", "started", "n_symbols")),
            }
    except Exception as exc:  # noqa: BLE001 - missing tables, locked file, bad URL
        log.debug("options screener: status read failed: %s", exc)
        return {"available": False, "why": "the screener database is not ready yet"}


def _read_state_file() -> dict | None:
    """The collector's ``state/screener_collector.json`` (None when absent or unreadable)."""
    try:
        doc = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def _file_view(doc: dict) -> tuple[dict, dict | None, dict | None]:
    """The state file in the DB's shapes: (status, last_pass, running_pass)."""
    st = {c: doc.get(c) for c in _STATUS_COLS}
    for c in _STATUS_TIMES:
        st[c] = _naive_utc(st.get(c))
    if not isinstance(st.get("progress"), dict):
        st["progress"] = None
    st["written_by"] = str(doc.get("written_by") or "") or None
    last = None
    if doc.get("last_pass_id") is not None or doc.get("last_pass_finished"):
        last = {"id": doc.get("last_pass_id"), "kind": doc.get("last_pass_kind"),
                "session": doc.get("last_pass_session"),
                "finished": _naive_utc(doc.get("last_pass_finished")),
                "n_contracts": doc.get("last_pass_contracts")}
    running = None
    if doc.get("pass_kind") and st.get("pass_id") is not None:
        running = {"id": st.get("pass_id"), "kind": doc.get("pass_kind"),
                   "session": doc.get("pass_session"), "started": None,
                   "n_symbols": st.get("symbols_total")}
    return st, last, running


def _read_collector() -> dict:
    """The screener collector's heartbeat (``scr_status`` row 1) and its newest passes.

    Falls back to ``state/screener_collector.json`` when the screener DB has no status row,
    cannot be read, or holds an older heartbeat than the file (a collector that crashed at
    start - ``deploy/screener_collector.py`` - writes only the file). ``source`` says which
    was used. Never raises; never creates a missing SQLite file."""
    col = _read_collector_db()
    doc = _read_state_file()
    if doc is None:
        return col
    fst, flast, frun = _file_view(doc)
    dst = col.get("status") if col.get("available") else None
    fhb, dhb = fst.get("heartbeat"), _naive_utc((dst or {}).get("heartbeat"))
    newer = fhb is not None and (dhb is None or (fhb - dhb).total_seconds() > 1.0)
    if dst is not None and not newer:
        return col
    return {"available": True, "source": "file", "status": fst,
            "last_pass": col.get("last_pass") or flast,
            # the file's last pass is the collector's own: the newest that stored contracts
            "real_pass": col.get("real_pass") or flast,
            "running_pass": col.get("running_pass") if col.get("available") else frun}


def _frame_meta(eng) -> dict | None:
    """The engine's loaded-data summary (§5 ``frame.meta``) plus ``reloading``, or None. The
    flag goes into this COPY, never into the shared frame's meta."""
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
        m = dict(_as_dict(getattr(fr.current(), "meta", None)))
        if not m:
            return None
        rl = getattr(fr, "reloading", None)
        m["reloading"] = bool(rl()) if callable(rl) else False
        return _jsonable(m)
    except Exception as exc:  # noqa: BLE001
        log.debug("options screener: frame meta failed: %s", exc)
    return None


def _no_data_text() -> str:
    """The engine's "no data" warning (frame.NO_DATA) - the page hides it while the empty
    panel explains why. A plain fallback when the engine does not import."""
    try:
        from ..services.screener.frame import NO_DATA
        return str(NO_DATA)
    except Exception:  # noqa: BLE001 - numpy missing: the page shows the engine error instead
        return "No option data loaded yet."


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


def _num(v) -> int | None:
    try:
        return None if v is None or isinstance(v, bool) else int(v)
    except (TypeError, ValueError):
        return None


# ───────────────────────── status: words (fix plan 2.T, T-40 to T-68) ─────────────────────────

def _dur(seconds, *, up: bool = False) -> str:
    """A duration in plain words: ``4 min`` · ``2 h`` · ``2 h 05 min`` · ``3 d 4 h``
    (``up``: round up, for a countdown; an age rounds down)."""
    s = max(0.0, float(seconds or 0))
    mins = int(math.ceil(s / 60.0)) if up else int(s // 60)
    if mins < 60:
        return f"{mins} min"
    h, m = divmod(mins, 60)
    if h < 48:
        return f"{h} h" if not m else f"{h} h {m:02d} min"
    d, h = divmod(h, 24)
    return f"{d} d" if not h else f"{d} d {h} h"


def _et(ts) -> _dt.datetime | None:
    ts = _naive_utc(ts)
    return clock.et_now(ts) if ts else None


def _hm(ts) -> str:
    et = _et(ts)
    return et.strftime("%H:%M") if et else "-"


def _day_words(d) -> str:
    """A date (or ``YYYY-MM-DD``) as ``Fri Oct 9``."""
    if isinstance(d, _dt.datetime):
        d = d.date()
    if not isinstance(d, _dt.date):
        try:
            d = _dt.date.fromisoformat(str(d)[:10])
        except ValueError:
            return str(d or "")
    return f"{d.strftime('%a %b')} {d.day}"


def _pass_words(kind, session) -> str:
    """Which market a pass reads (T-03): ``Fri Oct 9 close`` for an end-of-day pass,
    ``live, 15-min delayed`` for a cycle (or manual) pass."""
    if str(kind or "") == "eod":
        return f"{_day_words(session)} close" if session else "end-of-day"
    return "live, 15-min delayed"


def _data_when(src: dict) -> str:
    """The data line's pass part: T-51 (eod) / T-52 (cycle)."""
    kind = src.get("kind") or src.get("pass_kind")
    fin = _et(src.get("finished"))
    if kind == "eod" and src.get("session"):
        read = f" (read {fin.strftime('%a %H:%M')} ET)" if fin else ""
        return f"data: {_day_words(src['session'])} close{read}"
    if fin:
        return f"data: {_day_words(fin.date())} {fin.strftime('%H:%M')} ET"
    return f"data: {_day_words(src['session'])}" if src.get("session") else "no market pass yet"


_KIND_TEXT = (                 # an older collector's row carries no error_kind: read its text
    ("startup", ("could not start",)),
    ("config", ("is not set on this pc", "could not set up the massive client")),
    ("auth", ("rejected the api key", "http 401")),
    ("network", ("could not reach massive",)),
    ("plan", ("plan does not include", "http 403", "not_authorized")),
    ("rate", ("too many requests", "http 429")),
    ("empty", ("read no option data", "empty options list")),
)
_MEMBER_TEXT = {               # T-42 .. T-47
    "config": "The server is not connected to the market data feed yet.",
    "auth": "The market data feed rejected the server's access key.",
    "network": "The server cannot reach the market data feed right now.",
    "plan": "The server's data subscription does not cover part of the feed.",
    "empty": "The last market pass read no option data.",
}
_MEMBER_OTHER = "The market collector hit an unexpected problem."
# an EMPTY list of optionable stocks (the universe walk), not an empty market pass (T-47)
_UNIVERSE_EMPTY = "The market data feed returned an empty list of optionable stocks."
_ALL_KINDS = ("config", "auth", "network")     # the collector pauses EVERYTHING for these (its _FATAL)
_UNI_WORDS = ("universe", "massive returned an empty options list")   # how a universe alert starts
_PLAN_ONE = "Part of the data ({what}) is paused; the rest keeps updating."        # T-46
_ADMIN_HINT = {                # T-49
    "config": "Admin: add TST_MASSIVE_API_KEY to app\\.env on Hermes (re-read within 5 min).",
    "auth": "Admin: fix the key in app\\.env on Hermes (re-read within 5 min).",
    "plan": "Admin: check the Massive subscription.",
    "network": "Admin: check Hermes' internet / DNS.",
}
_ADMIN_OTHER = "Admin: see logs\\screener_collector.log on Hermes."
_NEXT_RE = re.compile(r";?\s*next try (\d{1,2}:\d{2})(?:\s*ET)?", re.I)
# keyed on scr_collector._OP_WORDS' values (a test pins the two together)
_SCOPE_WORDS = {"the universe refresh": "the list of optionable stocks",
                "market passes": "the option market reads",
                "IV-history reads": "IV history",
                "stock bars and reference reads": "stock prices and names"}
# only the collector's own op words: a " - " inside Massive's reason must not be read as the scope
_SCOPE_RE = re.compile(r"\s+-\s+(" + "|".join(map(re.escape, _SCOPE_WORDS)) + r") paused, the rest carries on")
_CRASH_PREFIX = "The collector could not start on the server: "
_START_TASK = "Hermes: Start-ScheduledTask -TaskName TST-Options-Screener."
_SETUP_TASK = ("Hermes: powershell -ExecutionPolicy Bypass -File deploy\\setup_screener_task.ps1 -StartNow, "
               "then read logs\\screener_collector.log.")


def _error_kind(st: dict) -> str:
    """The active alert's kind: ``error_kind`` when the collector sent it, else read from
    the detail first, then ``last_error`` (an older collector)."""
    k = str(st.get("error_kind") or "").strip().lower()
    if k:
        return k
    for text in (st.get("detail"), st.get("last_error")):
        t = str(text or "").lower()
        for kind, hints in _KIND_TEXT:
            if any(h in t for h in hints):
                return kind
    return "other"


def _scope(detail) -> str | None:
    """The one paused part of a single-scope alert (the collector's "<op> paused, the rest
    carries on"), else None = everything is paused."""
    m = _SCOPE_RE.search(str(detail or ""))
    return m.group(1).strip() if m else None


def _uni_alert(detail, last_error, scope) -> bool:
    """Is the active alert about the list of optionable stocks (the universe walk) - read
    from the alert's own words, never from how many stocks are filed?"""
    if scope == "the universe refresh":
        return True
    return any(str(t or "").strip().lower().startswith(_UNI_WORDS) for t in (detail, last_error))


def _pass_paused(in_pass: bool, total, prog: dict, kind, scope) -> bool:
    """Is the running pass really paused? The collector says so (``progress.pass_paused_at``
    - a TIME, set once the pass stopped submitting), or the alert pauses the passes: the
    market-pass scope, or one of the kinds that pause everything. A universe alert during a
    first start's pass is NOT a pause - the pass keeps reading."""
    if not (in_pass and total):
        return False
    return (bool((prog or {}).get("pass_paused_at")) or scope == "market passes"
            or (scope is None and kind in _ALL_KINDS))


def _raw_reason(text, cap: int = 200) -> str:
    """The collector's reason without its scope / next-try tails."""
    t = _NEXT_RE.sub("", _SCOPE_RE.sub("", str(text or ""))).strip().rstrip(";").strip()
    return t[:cap]


def _next_try(st: dict, now: _dt.datetime) -> tuple[str, str] | None:
    """(``HH:MM``, ``in N min`` words) of the collector's next try - ``now`` for a time
    already passed, ``""`` when only the HH:MM is known (an older collector's detail)."""
    nt = _naive_utc(st.get("next_try"))
    if nt is not None:
        left = (nt - now).total_seconds()
        return _hm(nt), ("now" if left <= 30 else _dur(left, up=True))
    m = _NEXT_RE.search(str(st.get("detail") or ""))
    return (m.group(1), "") if m else None


def _retry_text(nt) -> str:
    """T-48."""
    if not nt:
        return ""
    hm, left = nt
    if left == "now":
        return " Retrying now."
    return f" Retrying at {hm} ET" + (f" (in {left})." if left else ".")


def _tries_again(nt, lead: str = "The collector tries again") -> str:
    """The panels' "... at HH:MM ET (in N min)" sentence tail (no full stop)."""
    if not nt:
        return f"{lead} by itself"
    hm, left = nt
    if left == "now":
        return f"{lead} now"
    return f"{lead} at {hm} ET" + (f" (in {left})" if left else "")


def _sentence(text: str) -> str:
    """text ending in a full stop (before more words are appended)."""
    text = text.rstrip()
    return text if (not text or text[-1] in ".!?") else text + "."


def _cap(text: str, n: int = LABEL_MAX) -> str:
    """``text`` cut to at most ``n`` characters, at a word when one ends near the cut, with
    "…" - the collector joins the jobs running at once (" · "), so a detail often runs past
    the cap and must not stop mid-word."""
    if len(text) <= n:
        return text
    cut = text[:n - 1]
    sp = cut.rfind(" ")
    if sp >= n - 40:
        cut = cut[:sp]
    return cut.rstrip(" ·;,-") + "…"


def _crash_reason(st: dict) -> str:
    text = str(st.get("last_error") or st.get("detail") or "").strip()
    if text.startswith(_CRASH_PREFIX):
        text = text[len(_CRASH_PREFIX):]
    text = re.sub(r"\.?\s*-\s*see logs\\+screener_collector\.log\s*$", "", text).strip().rstrip(".")
    return text[:160] or "unknown reason"


def _is_crash(st: dict) -> bool:
    if (st.get("state") or "") != "error":
        return False
    return CRASH_WRITER in str(st.get("written_by") or "") or str(st.get("error_kind") or "") == "startup"


def _reload_eta_s(meta: dict, last: dict | None) -> int:
    """About how long loading the latest data into the screener takes (~10 s per 1M rows)."""
    n = (last or {}).get("n_contracts") or meta.get("pass_contracts") or 0
    try:
        s = float(n) / 1_000_000 * 10.0
    except (TypeError, ValueError):
        s = 10.0
    return int(max(5, round(s / 5.0) * 5)) if n else 10


def _status_view(col: dict, meta: dict | None = None, now: _dt.datetime | None = None,
                 engine_error: str | None = None, is_admin: bool = False) -> dict:
    """The data line, the status dot, the alert and the empty-results panel (§8;
    dashboard-visibility rule; fix plan v4.137 step 9).

    dot: ``green`` working / idle · ``amber`` no heartbeat for ``STALE_S`` (any hour), stopped,
    a warning, a plan pause on one part, an idle collector with no universe and an error, or a
    last pass that stored nothing · ``rose`` error / could not start · ``slate`` never reported.
    ``label`` = ``alert`` (+ the engine's problem); members get plain words, admins also the
    fix (``is_admin``). ``empty`` = ``{title, body, admin}`` while the frame has no contracts
    or no pass has finished (None otherwise)."""
    now = _naive_utc(now) or _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    meta = meta or {}
    col = col or {}
    st = col.get("status") or None
    last = col.get("last_pass") or None
    running = col.get("running_pass") or None
    s = st or {}
    prog = s.get("progress") if isinstance(s.get("progress"), dict) else {}
    state = s.get("state") or ""
    detail = " ".join(str(s.get("detail") or "").split())
    last_error = " ".join(str(s.get("last_error") or "").split())
    warn = " ".join(str(s.get("warn") or "").split())
    universe_n = _num(s.get("universe_n")) or 0
    hb = _naive_utc(s.get("heartbeat"))
    age = (now - hb).total_seconds() if hb else None

    # ---- what the screener holds ----
    finished = last if (last and last.get("finished")) else None
    # the newest finished pass that STORED contracts - where the data comes from (a newer
    # pass that read nothing keeps the amber dot and T-22, never the data line)
    if "real_pass" in col:
        real = col.get("real_pass") or None
        real = real if (real and real.get("finished")) else None
    else:
        real = finished if (finished and finished.get("n_contracts") != 0) else None
    n_und, n_con = meta.get("n_underlyings"), meta.get("n_contracts")
    if not n_con and real and real.get("n_contracts"):
        # the frame is empty but a pass stored rows (its reload is due): the pass's counts
        n_und = real.get("n_ok") if real.get("n_ok") is not None else real.get("n_symbols")
        n_con = real.get("n_contracts")
    if meta:
        frame_empty = bool(meta.get("empty")) or not meta.get("n_contracts")
    else:
        frame_empty = not (real and real.get("n_contracts"))
    has_rows = not frame_empty
    # a pass in flight (running, or paused by an alert): the collector's pass is the newest
    # unfinished one (an idle collector reports its LAST pass's id and counts instead)
    in_pass = state == "pass" or bool(running and s.get("pass_id") is not None
                                      and s.get("pass_id") == running.get("id"))
    done, total = _num(s.get("symbols_done")), _num(s.get("symbols_total"))
    if not in_pass:
        done = total = None
    elif total is None and running:
        total = _num(running.get("n_symbols"))

    # ---- the data line (T-51 .. T-55) ----
    parts = ["Massive", "15-min delayed", "prices estimated from IV"]
    if "real_pass_id" in meta:
        # the pass the loaded data comes from (what the table shows): the frame's newest real
        # pass - none while only a pass that read nothing has finished (the first pass runs)
        src = ({"kind": meta.get("real_pass_kind"), "session": meta.get("real_session"),
                "finished": meta.get("real_finished")} if meta.get("real_pass_id") is not None else None)
    elif (meta.get("pass_id") is not None and (meta.get("finished") or meta.get("session"))
          and meta.get("pass_contracts") != 0):
        src = {"kind": meta.get("pass_kind"), "session": meta.get("session"), "finished": meta.get("finished")}
    else:
        src = real                        # ... else the collector's last pass that stored contracts
    if src:
        parts.append(_data_when(src))
    elif in_pass and total:
        parts.append(f"first pass: {_count(done or 0)} / {_count(total)} read")
    elif in_pass:
        parts.append("first pass running")
    elif universe_n and frame_empty:
        parts.append(f"list of optionable stocks {_count(universe_n)} · none read yet")
    else:
        parts.append("no market pass yet")
    if n_con:
        parts.append(f"{_count(n_und or 0)} underlying" + ("" if n_und == 1 else "s"))
        parts.append(f"{_compact(n_con)} contracts")
    h_total, h_done = _num(s.get("history_total")), _num(s.get("history_done_n"))
    if not h_total and meta.get("iv_history_total"):
        h_total, h_done = _num(meta.get("iv_history_total")), _num(meta.get("iv_history_done"))
    if h_total:
        h_done = h_done or 0
        if h_done >= h_total:
            parts.append(f"IV history {_count(h_done)} / {_count(h_total)}")
        elif not h_done and (_num(prog.get("stock_days_pending")) or state == "stocks"):
            parts.append("IV history starts after the stock prices")
        else:
            parts.append(f"IV rank: building ({_count(h_done)} / {_count(h_total)})")

    # ---- the dot and the label ----
    kind = _error_kind(s) if state == "error" else None
    nt = _next_try(s, now) if st else None
    admin_hint = ""
    if not col.get("available") or not st:
        dot, label = "slate", "The market collector has not started yet - no option data to screen."
    elif _is_crash(s):
        dot, label = "rose", f"{_CRASH_PREFIX}{_crash_reason(s)}."                     # T-41
        admin_hint = _ADMIN_OTHER
    elif state == "stopped":
        dot, label = "amber", "The market collector is stopped - the data is not being refreshed."
        admin_hint = "Admin: " + _START_TASK[len("Hermes: "):]
    elif age is None or age > STALE_S:
        dot = "amber"
        label = ((f"The collector has not reported for {_dur(age)}" if age is not None
                  else "The collector has never reported a heartbeat")
                 + f" (it was {_DOING.get(state, state or 'running')}) - it has probably stopped, "
                   "so the data is not being refreshed.")                               # T-40
        admin_hint = "Admin: " + _START_TASK[len("Hermes: "):]
    elif state == "error":
        scope = _scope(detail)
        if kind == "plan" and scope:
            dot, label = "amber", _PLAN_ONE.format(what=_SCOPE_WORDS.get(scope, scope))  # T-46
        elif kind == "empty" and _uni_alert(detail, last_error, scope):
            dot, label = "rose", _UNIVERSE_EMPTY        # the list of optionable stocks, not a pass
        else:
            dot, label = "rose", _MEMBER_TEXT.get(kind, _MEMBER_OTHER)                  # T-42..T-47
        label += _retry_text(nt)                                                        # T-48
        if kind != "empty" and _pass_paused(in_pass, total, prog, kind, scope):
            label += (f" Pass paused at {_count(done or 0)} / {_count(total)} underlyings"   # T-50
                      + ("; results already loaded stay on screen." if has_rows else "."))
        admin_hint = _ADMIN_HINT.get(kind, _ADMIN_OTHER)
    else:
        dot = "green"
        label = _cap(f"Collector: {detail}") if detail else \
            "Collector " + _STATES.get(state, state or "running") + "."
        if warn:
            dot = "amber"
            label = _sentence(label) + " " + warn[:LABEL_MAX]
        elif state == "idle" and not universe_n and last_error:
            dot = "amber"
            label = _sentence(label) + f" The last attempt failed: {_raw_reason(last_error, 160)}."
        elif state == "idle" and finished and finished.get("n_contracts") == 0 and frame_empty:
            dot = "amber"
            label = _sentence(label) + (f" The last market pass ({_pass_words(finished.get('kind'), finished.get('session'))})"
                      " read no option data.")
    if is_admin and admin_hint and dot != "green":
        raw = detail or last_error
        label += f" {admin_hint}" + (f" Details: {raw[:300]}" if raw else "")
    alert = label

    empty = None
    if frame_empty or not finished:
        empty = _empty_view(col, st, meta, now, kind=kind, nt=nt, age=age, frame_empty=frame_empty,
                            finished=finished, running=running, in_pass=in_pass, done=done,
                            total=total, universe_n=universe_n, detail=detail, is_admin=is_admin)

    if engine_error:
        label += " " + engine_error

    return {
        "ok": True,
        "dot": dot,
        "label": label,
        "alert": alert,
        "line": " · ".join(parts),
        "parts": parts,
        "heartbeat": _iso(hb),
        "heartbeat_age_s": int(age) if age is not None else None,
        "state": state or None,
        "error_kind": kind,
        "next_try": _iso(s.get("next_try")),
        "warn": warn or None,
        "source": col.get("source"),
        "collector": _jsonable({k: (_iso(v) if isinstance(v, _dt.datetime) else v)
                                for k, v in s.items()}) if st else None,
        "last_pass": _jsonable({k: (_iso(v) if isinstance(v, _dt.datetime) else v)
                                for k, v in (last or {}).items()}) if last else None,
        "running_pass": _jsonable({k: (_iso(v) if isinstance(v, _dt.datetime) else v)
                                   for k, v in (running or {}).items()}) if running else None,
        "frame": meta or None,
        "empty": empty,
        "reload_eta_s": _reload_eta_s(meta, finished),
        "engine_ok": engine_error is None,
        "engine_error": engine_error,
    }


def _empty_view(col, st, meta, now, *, kind, nt, age, frame_empty, finished, running, in_pass, done,
                total, universe_n, detail, is_admin) -> dict:
    """The results area's panel while there is nothing to screen (T-60 .. T-68):
    ``{title, body, admin}`` - ``admin`` is the fix for an administrator, "" for members."""
    s = st or {}
    prog = s.get("progress") if isinstance(s.get("progress"), dict) else {}
    state = s.get("state") or ""

    def out(title, body="", admin=""):
        return {"title": title, "body": body, "admin": admin if is_admin else ""}

    def plain(k):
        return (_MEMBER_TEXT.get(k, _MEMBER_OTHER)).rstrip(".")

    if not col.get("available") or not st:                                            # T-60
        return out("No option data yet - the market data collector is not running.",
                   "TradeHunter reads the whole US option market from Massive with a collector on the "
                   "server. It has never reported in, so there is nothing to screen yet.", _SETUP_TASK)
    if _is_crash(s):                                                                   # T-61
        return out("No option data yet - the market data collector could not start.",
                   f"{_CRASH_PREFIX}{_crash_reason(s)}.",
                   "Hermes: read logs\\screener_collector.log, then run powershell -ExecutionPolicy "
                   "Bypass -File deploy\\setup_screener_task.ps1 -StartNow.")
    if frame_empty and meta.get("reloading"):                                         # T-68 = T-25
        return out(f"New market data is loading into the screener (about {_reload_eta_s(meta, finished)} "
                   "seconds) - the results refresh by themselves.")
    if state == "stopped":
        return out("No option data yet - the market data collector is stopped.",
                   "Nothing new is read until it runs again.", _START_TASK)
    if age is None or age > STALE_S:                                                  # T-62
        when = f"{_dur(age)} ago" if age is not None else "a while ago"
        return out(f"No option data yet - the collector stopped reporting {when} "
                   f"(it was {_DOING.get(state, state or 'running')}).",
                   "Nothing new is read until it runs again.", _START_TASK)
    hint = _ADMIN_HINT.get(kind or "", _ADMIN_OTHER)
    raw = detail or str(s.get("last_error") or "")
    admin = f"{hint} Details: {raw[:300]}" if raw else hint

    def pass_panel():                                                                  # T-65
        src = running or {}
        what = _pass_words(src.get("kind") or prog.get("pass_kind"), src.get("session") or prog.get("pass_session"))
        counts = f"{_count(done or 0)} of {_count(total)} underlyings read. " if total else ""
        return out(f"Step 2 of 2: reading the option market ({what}).",
                   counts + "Results appear within a minute and grow every couple of minutes as more "
                   "are read; this page refreshes by itself.")

    if state == "error":
        scope = _scope(detail)
        uni_alert = _uni_alert(detail, s.get("last_error"), scope)
        if (not universe_n or uni_alert) and not in_pass:                              # T-64
            reason = "Massive returned an empty options list" if kind == "empty" else plain(kind)
            pages = _num(prog.get("universe_pages"))
            cont = f", continuing from page {pages + 1:,}" if pages else ""
            return out("No option data yet - the list of optionable stocks could not be read.",
                       f"{reason}. {_tries_again(nt)}{cont}.", admin)
        if ((kind == "empty" and not uni_alert)                                        # T-67: a pass's empty
                or (finished and finished.get("n_contracts") == 0 and not in_pass)):
            src = finished or running or {}
            reason = _raw_reason(detail or s.get("last_error")) or plain("empty")
            return out(f"The last market pass ({_pass_words(src.get('kind'), src.get('session'))}) "
                       "read no option data.", f"{reason.rstrip('.')}. {_tries_again(nt)}.", admin)
        if _pass_paused(in_pass, total, prog, kind, scope):                            # T-66
            return out(f"Reading the option market is paused at {_count(done or 0)} of {_count(total)} "
                       "underlyings.", f"{plain(kind)}. {_tries_again(nt, 'It resumes where it stopped')}.", admin)
        if in_pass:
            return pass_panel()           # the pass keeps reading under another part's alert
        reason = (_PLAN_ONE.format(what=_SCOPE_WORDS.get(scope, scope)).rstrip(".")
                  if (kind == "plan" and scope) else plain(kind))
        return out("No option data yet.", f"{reason}. {_tries_again(nt)}.", admin)
    if state == "universe":                                                            # T-63
        pages, syms = _num(prog.get("universe_pages")), _num(prog.get("universe_symbols"))
        started = _naive_utc(prog.get("universe_started"))
        if pages:
            mins = int(max(0.0, (now - started).total_seconds()) // 60) if started else None
            body = (f"Page {pages:,} of Massive's option contract list ({syms or 0:,} stocks so far"
                    + (f", started {mins} min ago" if mins is not None else "") + "). ")
        else:
            body = "Reading Massive's option contract list (this can take several minutes). "
        return out("Step 1 of 2: reading the list of optionable stocks.",
                   body + "Results appear here as soon as the first stocks' option chains are read; "
                   "this page refreshes by itself.")
    if in_pass or state == "pass":                                                     # T-65
        return pass_panel()
    if finished and finished.get("n_contracts") == 0:                                  # T-67 (idle)
        reason = _raw_reason(s.get("last_error")) or plain("empty")
        return out(f"The last market pass ({_pass_words(finished.get('kind'), finished.get('session'))}) "
                   "read no option data.", f"{reason.rstrip('.')}. {_tries_again(nt)}.", admin)
    body = _sentence(f"Collector: {detail}") if detail else "Collector " + _STATES.get(state, state or "running") + "."
    if state == "idle" and not universe_n and s.get("last_error"):
        body += f" The last attempt failed: {_raw_reason(s.get('last_error'), 160)}."
    return out("No option data yet.", _cap(body, LABEL_MAX + 60))


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
    # the same inputs /api/status uses, so the first paint and the first poll say the same.
    # The frame summary comes from the spec the engine just built (``data`` = frame.meta +
    # reloading): a second frame read would wait on an empty frame's reload once more.
    d = spec.get("data") if spec else None
    if isinstance(d, dict):
        meta = d
    elif engine_error is None:
        meta = _frame_meta(eng)                   # an engine whose spec carries no data
    else:
        meta = None                               # the engine failed: its error is shown instead
    status = _status_view(_read_collector(), meta, engine_error=None,
                          is_admin=bool(getattr(user, "is_admin", False)))
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
        "status": status,                     # the empty panel + alert on first paint
        "no_data": _no_data_text(),           # hidden from the warnings while the panel shows
        "is_admin": bool(getattr(user, "is_admin", False)),
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
    return _status_view(_read_collector(), _frame_meta(eng), engine_error=missing,
                        is_admin=bool(getattr(user, "is_admin", False)))


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
