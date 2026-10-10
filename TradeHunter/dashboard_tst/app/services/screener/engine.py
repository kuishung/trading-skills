"""The screener API the routes call (OPTIONS_SCREENER_DESIGN.md §5).

* ``screens()``   -> the GO TO list: ``[{"family", "screens": [{key, label, description, bias,
                     strategy}]}]`` in Barchart's order.
* ``spec(key)``   -> one screen: meta, legs, its default payload (+ the cards' field specs),
                     every field it can filter on grouped for Add-a-Filter, its views and their
                     columns, the engine limits and the data line (``frame.meta``).
* ``run(key, payload, *, page=1, per_page=100)`` -> ``{"screen", "label", "total", "kept",
                     "page", "pages", "per_page", "rows", "columns", "truncated", "ms", "data",
                     "warnings", "sort", "view", "flag_earnings", "limits"}``.
* ``csv(key, payload, *, limit=1000)`` -> CSV text of the same rows (header = column labels).

Payload (what the page sends and a saved screener stores)::

    {"filters": [{"f": "<field>", "op": "gte|lte|eq|between|in|is|within",
                  "lo": n, "hi": n, "v": [...]}],
     "sort": {"col": "<field>", "dir": "asc|desc"},
     "view": "main|filter|greeks|vol",
     "flag_earnings": bool}

No ``filters`` key -> the screen's defaults; ``"filters": []`` -> no filters at all. An ``in``
with an empty ``v`` or a range with neither bound is an empty card and does nothing. Unknown
fields, operators or values are skipped with a warning - never an exception.
"""
from __future__ import annotations

import csv as _csv
import io
import logging
import math
import time
from dataclasses import dataclass

import numpy as np

from . import frame as _frame
from . import single, strategies
from .fields import (GROUPS, LEG_FILTER_BASES, LEG_TERM_BASES, Field, Filt, Plan, Table, all_fields,
                     display, get, parse_filters, values)
from .screens import FAMILIES, SCREENS, Screen

log = logging.getLogger("tst.screener.engine")

MAX_ROWS = 5000            # sorted rows kept per run; beyond it truncated=True
PER_PAGE_MAX = 1000
CSV_MAX = 1000
VIEWS = ("main", "filter", "greeks", "vol")
_COMBO_CONTRACT = ("expiry", "dte", "expiry_type", "exp_before_earnings", "earnings_before_exp")
_TERM_CONTRACT = ("exp_before_earnings", "earnings_before_exp")


def limits() -> dict:
    return {"max_rows": MAX_ROWS, "max_apart": strategies.MAX_APART, "combo_cap": strategies.COMBO_CAP,
            "per_page_max": PER_PAGE_MAX, "csv_max": CSV_MAX}


# ─────────────────────────────────── which fields a screen offers ───────────────────────────────────

def filter_fields(screen: Screen) -> list[Field]:
    """Every field a member can add as a filter on ``screen`` (Add-a-Filter)."""
    fs: list[Field] = list(all_fields("underlying"))
    if not screen.strategy:
        fs += [f for f in all_fields("contract") if not (f.key == "option_type" and screen.right)]
    else:
        fs += [get(k) for k in (_TERM_CONTRACT if screen.horizontal else _COMBO_CONTRACT)]
        bases = LEG_FILTER_BASES + (LEG_TERM_BASES if screen.horizontal else ())
        for n in range(1, len(screen.legs) + 1):
            fs += [get(f"leg{n}.{b}") for b in bases]
    fs += [get(k) for k in screen.metrics]
    seen, out = set(), []
    for f in fs:
        if f is not None and f.key not in seen:
            seen.add(f.key)
            out.append(f)
    return out


def _grouped(fields: list[Field], frame) -> list[dict]:
    by: dict[str, list] = {g: [] for g in GROUPS}
    for f in fields:
        by.setdefault(f.group, []).append(f.public(frame))
    return [{"group": g, "fields": fs} for g, fs in by.items() if fs]


def _column_ok(screen: Screen, f: Field | None) -> bool:
    """May ``f`` be a column / sort key on ``screen``?"""
    if f is None:
        return False
    if f.level == "leg":
        return f.leg <= len(screen.legs)
    if f.level == "strategy":
        return f.key in screen.metrics or any(f.key == k for k, _ in screen.columns)
    return True


# ─────────────────────────────────── views ───────────────────────────────────

def _view_cols(screen: Screen, view: str, filts: list[Filt]) -> list[tuple[str, str]]:
    if view == "main":
        return list(screen.columns)
    nl = len(screen.legs)
    head = [("symbol", "Symbol"), ("stock_price", "Price")]
    if screen.strategy:
        exps = ([(f"leg{n}.expiry", f"Exp Leg{n}") for n in range(1, nl + 1)] if screen.horizontal
                else [("expiry", "Exp Date")])
        strikes = [(f"leg{n}.strike", f"Leg{n} strike") for n in range(1, nl + 1)]
    else:
        if screen.kind == "options":
            head.append(("option_type", "Type"))
        exps = [("expiry", "Exp Date")]
        strikes = [("strike", "Strike")]
    if view == "filter":
        cols = head + exps + strikes
        have = {k for k, _ in cols}
        for flt in filts:
            if not flt.noop and flt.field.key not in have:
                cols.append((flt.field.key, flt.field.label))
                have.add(flt.field.key)
        return cols
    if view == "greeks":
        if not screen.strategy:
            return head + exps + strikes + [("price", "Opt. price (est.)"), ("delta", "Delta"),
                                             ("gamma", "Gamma"), ("theta", "Theta"), ("vega", "Vega"),
                                             ("iv", "IV")]
        cols = head + exps
        for n in range(1, nl + 1):
            cols += [(f"leg{n}.strike", f"Leg{n} strike"), (f"leg{n}.delta", f"Delta L{n}"),
                     (f"leg{n}.gamma", f"Gamma L{n}"), (f"leg{n}.theta", f"Theta L{n}"),
                     (f"leg{n}.vega", f"Vega L{n}"), (f"leg{n}.iv", f"IV L{n}")]
        return cols + [("net_delta", "Net Delta"), ("net_gamma", "Net Gamma"), ("net_theta", "Net Theta"),
                       ("net_vega", "Net Vega")]
    # vol
    if not screen.strategy:
        return head + exps + strikes + [("iv", "IV"), ("iv_rank", "IV Rank"), ("iv_pctl", "IV Pctl"),
                                         ("iv30", "IV30"), ("hv20", "HV20"), ("iv_hv", "IV/HV"),
                                         ("exp_move", "Exp Move"), ("exp_move_pct", "Exp Move %")]
    cols = head + exps + strikes + [(f"leg{n}.iv", f"Leg{n} IV") for n in range(1, nl + 1)]
    if screen.horizontal:
        cols.append(("iv_skew", "IV Skew"))
    return cols + [("iv_rank", "IV Rank"), ("iv_pctl", "IV Pctl"), ("iv30", "IV30"), ("hv20", "HV20"),
                   ("avg_iv_hv", "IV/HV"), ("exp_move", "Exp Move"), ("exp_move30", "Exp Move 30d %")]


def _col_dict(f: Field, label: str) -> dict:
    return {"key": f.key, "label": label or f.label, "unit": f.unit, "fmt": f.fmt, "level": f.level,
            "help": f.help}


def _resolve_cols(screen: Screen, pairs: list[tuple[str, str]]) -> list[tuple[Field, str]]:
    out = []
    for key, label in pairs:
        f = get(key)
        if f is not None and _column_ok(screen, f):
            out.append((f, label))
    return out


# ─────────────────────────────────── screens() / spec() ───────────────────────────────────

def screens() -> list[dict]:
    """The GO TO list, grouped by family in Barchart's order."""
    out = []
    for fam in FAMILIES:
        items = [{"key": s.key, "label": s.label, "description": s.description, "bias": s.bias,
                  "strategy": s.strategy} for s in SCREENS.values() if s.family == fam]
        if items:
            out.append({"family": fam, "screens": items})
    return out


def _defaults(screen: Screen) -> dict:
    return {"filters": [dict(d) for d in screen.defaults],
            "sort": {"col": screen.sort[0], "dir": screen.sort[1]}, "view": "main", "flag_earnings": False}


def spec(key: str) -> dict:
    """One screen's full description for the page (unknown key -> ``{"key", "error"}``)."""
    screen = SCREENS.get(key)
    if screen is None:
        return {"key": key, "error": f"Unknown screen '{key}'.", "warnings": [f"Unknown screen '{key}'."]}
    fr = _frame.current()
    fields = filter_fields(screen)
    cards = []
    for d in screen.defaults:
        f = get(d["f"])
        cards.append(f.public(fr) if f is not None else None)
    views = {v: [_col_dict(f, lbl) for f, lbl in _resolve_cols(screen, _view_cols(screen, v, []))]
             for v in VIEWS}
    return {
        "key": screen.key, "label": screen.label, "family": screen.family,
        "description": screen.description, "bias": screen.bias, "kind": screen.kind,
        "strategy": screen.strategy, "credit": screen.credit, "stock": screen.stock,
        "right": screen.right or None,
        "legs": [leg.public(n) for n, leg in enumerate(screen.legs, 1)],
        "defaults": _defaults(screen),
        "cards": cards,
        "filters": _grouped(fields, fr),
        "views": views,
        "view_notes": {"filter": "symbol, expiration and strikes plus one column per active filter"},
        "limits": limits(),
        "data": _meta(fr),
    }


# ─────────────────────────────────── run() ───────────────────────────────────

@dataclass
class _Exec:
    screen: Screen
    frame: object
    table: Table
    total: int
    stopped: bool
    columns: list
    view: str
    flag: bool
    sort: dict
    warnings: list


def _meta(fr) -> dict:
    m = dict(fr.meta)
    m["warnings"] = list(m.get("warnings") or [])
    return m


def _execute(key: str, payload) -> _Exec | None:
    screen = SCREENS.get(key)
    if screen is None:
        return None
    fr = _frame.current()
    warnings: list[str] = []
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        warnings.append("The payload must be an object - the screen's defaults were used.")
        payload = {}
    raw = payload.get("filters") if "filters" in payload else [dict(d) for d in screen.defaults]
    allowed = {f.key for f in filter_fields(screen)}
    filts = parse_filters(raw, allowed, fr, warnings, screen.label)

    sort_in = payload.get("sort") if isinstance(payload.get("sort"), dict) else {}
    col, dirn = screen.sort
    if sort_in.get("col") is not None:
        f = get(str(sort_in.get("col")))
        if _column_ok(screen, f):
            col = f.key
        else:
            warnings.append(f"Unknown sort column '{sort_in.get('col')}' - sorted by {get(col).label}.")
        dirn = "asc"
    d_in = str(sort_in.get("dir") or "").lower()
    if d_in in ("asc", "desc"):
        dirn = d_in
    elif d_in:
        warnings.append(f"Unknown sort direction '{sort_in.get('dir')}' - used {dirn}.")

    view = str(payload.get("view") or "main").lower()
    if view not in VIEWS:
        warnings.append(f"Unknown view '{payload.get('view')}' - showing the main view.")
        view = "main"
    flag = bool(payload.get("flag_earnings"))

    if any(f.field.key == "sec_type" and not f.noop for f in filts) and fr.meta.get("n_sec_type_unknown"):
        warnings.append(f"{fr.meta['n_sec_type_unknown']:,} underlyings have no security type yet and are "
                        "left out by the Security Type filter.")

    plan = Plan.build(filts, get(col), dirn == "desc", MAX_ROWS)
    cols = _resolve_cols(screen, _view_cols(screen, view, filts))
    if flag:
        cols.append((get("earnings_date"), "Earnings"))

    if fr.empty:
        table = Table(fr, [np.zeros(0, dtype=np.int64) for _ in screen.legs])
        total, stopped = 0, False
        if not fr.meta.get("warnings"):
            warnings.append(_frame.NO_DATA)
    elif screen.strategy:
        table, total, w, stopped = strategies.run(fr, screen, plan)
        warnings += w
    else:
        table, total, w, stopped = single.run(fr, screen, plan)
        warnings += w
    return _Exec(screen, fr, table, total, stopped, cols, view, flag, {"col": col, "dir": dirn}, warnings)


def _iso_days(arr) -> list:
    return display(get("expiry"), arr, None)


def _rows(ex: _Exec, sel: np.ndarray, *, with_legs: bool = True) -> list[dict]:
    t = ex.table.take(sel)
    fr = ex.frame
    n = t.n
    rows: list[dict] = [{} for _ in range(n)]
    if n == 0:
        return rows
    for r, s in zip(rows, display(get("symbol"), values("symbol", t), fr)):
        r["symbol"] = s
    for f, _ in ex.columns:
        for r, v in zip(rows, display(f, values(f, t), fr)):
            r[f.key] = v
    c = fr.c
    if ex.flag:
        eb = np.zeros(n, dtype=bool)
        for leg_idx in t.legs:
            eb |= c["earn_before"][leg_idx] > 0
        for r, v in zip(rows, eb.tolist()):
            r["earnings_flag"] = bool(v)
    # per-leg facts for the id and the profit/loss chart
    per = []
    for k, leg in enumerate(ex.screen.legs):
        i = t.legs[k]
        per.append({
            "exp": _iso_days(c["exp_f"][i]),
            "right": ["P" if p else "C" for p in c["is_put"][i].tolist()],
            "strike": display(get("strike"), c["strike"][i], fr),
            "price": display(get("price"), c["price"][i], fr),
            "iv": display(get("iv"), c["iv_pct"][i], fr),
            "delta": display(get("delta"), c["delta"][i], fr),
        })
    spot = display(get("stock_price"), fr.u["spot"][t.sym], fr) if ex.screen.stock else None
    for j, r in enumerate(rows):
        parts = [f"{p['exp'][j]}{p['right'][j]}{p['strike'][j]}" for p in per]
        r["id"] = f"{r['symbol']}|" + "|".join(parts)
        if not with_legs:
            continue
        legs = []
        if ex.screen.stock:
            legs.append({"action": "buy", "qty": 1, "right": "S", "expiry": None, "strike": None,
                         "price": spot[j], "iv": None, "delta": 1.0})
        for k, leg in enumerate(ex.screen.legs):
            p = per[k]
            legs.append({"action": leg.action, "qty": leg.qty, "right": p["right"][j],
                         "expiry": p["exp"][j], "strike": p["strike"][j], "price": p["price"][j],
                         "iv": p["iv"][j], "delta": p["delta"][j]})
        r["legs"] = legs
    return rows


def _dedupe(ws: list) -> list:
    seen, out = set(), []
    for w in ws:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


def _empty_result(key, warnings, t0, page=1, per_page=100) -> dict:
    return {"screen": key, "label": None, "total": 0, "kept": 0, "page": 1, "pages": 0,
            "per_page": per_page, "rows": [], "columns": [], "truncated": False,
            "ms": int((time.perf_counter() - t0) * 1000), "data": {}, "warnings": warnings,
            "sort": None, "view": "main", "flag_earnings": False, "limits": limits()}


def run(key: str, payload: dict | None = None, *, page: int = 1, per_page: int = 100) -> dict:
    """Screen the market with ``payload`` and return one page of sorted rows (§5)."""
    t0 = time.perf_counter()
    try:
        per_page = max(1, min(int(per_page or 100), PER_PAGE_MAX))
    except (TypeError, ValueError):
        per_page = 100
    try:
        page = max(1, int(page or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        ex = _execute(key, payload)
        if ex is None:
            return _empty_result(key, [f"Unknown screen '{key}'."], t0, per_page=per_page)
        kept = ex.table.n
        pages = int(math.ceil(kept / per_page)) if kept else 0
        if pages and page > pages:
            page = pages
        lo = (page - 1) * per_page
        sel = np.arange(lo, min(lo + per_page, kept))
        rows = _rows(ex, sel)
        meta = _meta(ex.frame)
        return {
            "screen": key, "label": ex.screen.label, "total": int(ex.total), "kept": int(kept),
            "page": page, "pages": pages, "per_page": per_page, "rows": rows,
            "columns": [_col_dict(f, lbl) for f, lbl in ex.columns],
            "truncated": bool(ex.total > kept or ex.stopped),
            "ms": int((time.perf_counter() - t0) * 1000), "data": meta,
            "warnings": _dedupe(meta["warnings"] + ex.warnings), "sort": ex.sort, "view": ex.view,
            "flag_earnings": ex.flag, "limits": limits(),
        }
    except Exception as exc:  # noqa: BLE001 - the page gets a warning, never a 500
        log.exception("screener run failed: %s", key)
        return _empty_result(key, [f"The screener could not run this request ({type(exc).__name__}: {exc})."],
                             t0, per_page=per_page)


def csv(key: str, payload: dict | None = None, *, limit: int = CSV_MAX) -> str:
    """The run's rows (best first, at most ``limit``, never more than ``CSV_MAX``) as CSV."""
    try:
        limit = max(1, min(int(limit or CSV_MAX), CSV_MAX, MAX_ROWS))
    except (TypeError, ValueError):
        limit = CSV_MAX
    buf = io.StringIO()
    w = _csv.writer(buf, lineterminator="\n")
    try:
        ex = _execute(key, payload)
    except Exception:  # noqa: BLE001
        log.exception("screener csv failed: %s", key)
        ex = None
    if ex is None:
        w.writerow(["Symbol"])
        return buf.getvalue()
    header = [lbl for _, lbl in ex.columns]
    keys = [f.key for f, _ in ex.columns]
    if "symbol" not in keys:
        header, keys = ["Symbol"] + header, ["symbol"] + keys
    if ex.flag:
        header.append("Earnings Before Expiration")
        keys.append("earnings_flag")
    w.writerow(header)
    for r in _rows(ex, np.arange(min(limit, ex.table.n)), with_legs=False):
        w.writerow(["" if r.get(k) is None else r.get(k) for k in keys])
    return buf.getvalue()
