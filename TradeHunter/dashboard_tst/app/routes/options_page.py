"""Options v2 - browse by rules (OPTIONS_V2_DESIGN.md §9, on the Massive data path of §13).

One page: a status strip (the Hermes collector line - it reads Massive - and the plain
data-plan note), the member's basket (left; every row has a "Refresh now" control), a
strategy dropdown with the rules panel ALWAYS visible, and the list of trades that pass
every rule over the whole basket. A click on a trade opens its legs (each with its data
time, its source, and whether its price is the bid/ask mid or a model price from its IV)
and the payoff chart.

Data (§13): every option figure and the stock price come from Massive (formerly
Polygon.io). Options Starter is the whole-chain snapshot with greeks, IV, open interest
and the day bar, 15 minutes delayed and WITHOUT bid/ask quotes, so a leg's price is
estimated from its own IV (``opt_massive.model_price``); Stocks Basic gives the daily
bars. The Hermes collector (TST-Options-Collector) writes it into ``services/opt_store``
every 15 min during the US session; "Refresh now" (``POST /options/refresh/<sym>``)
reads one ticker on demand, server-side, through the same ``opt_massive.ingest_symbol``.
The earnings date is the one free-source (Yahoo) figure. TradeHunter only FINDS the
trade: the member checks the live price and enters it in IBKR TWS.

No member writes any data any more: the v4.133 connector pill, its download, the help
about it and the contribution endpoints are gone. The basket's optional "Run my TWS
scanner" import still talks to the member's own IBKR bridge on 127.0.0.1 (from the
page's script), unchanged.

This router is included BEFORE ``routes/options.py`` in ``main.py`` so its fixed
paths win over that module's ``/options/{symbol}`` catch-all, and it carries the
``require_menu("options")`` gate there (a member without the grant is redirected).

Nothing here places, modifies or cancels an order.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import math
import os
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field as PField
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as _models
from ..db import get_db
from ..models import OptionBasket, OptQuote, User
from ..security import require_user
from ..services import clock, massive, opt_massive, opt_rules, opt_screen, opt_store, payoff
from ..services import user_watchlist as uwl
from ..services.opt_constants import MAX_BASKET
from . import options as legacy_options            # BRIDGE_PORT only
from .ivscan import DEFAULT_CRITERIA, UNIVERSE_PREF, _clean_symbols

log = logging.getLogger(__name__)

router = APIRouter(prefix="/options", tags=["options-page"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

BRIDGE_PORT = legacy_options.BRIDGE_PORT            # 9224: the member's own IBKR bridge (the TWS scanner import)

SOURCES = ("typed", "paste", "watchlist", "ivscan_list", "ivscan_scan", "scanner",
           "screener", "sector", "positions", "system")
IMPORT_SOURCES = ("paste", "watchlist", "ivscan_list", "ivscan_scan", "scanner",
                  "screener", "positions")
BASKET_SORTS = ("added", "symbol", "fresh", "iv")

FRESH_MIN = 60              # data younger than this is fresh; older is amber during RTH (§9)
STALE_HEARTBEAT_MIN = 5     # the collector line goes stale after this many minutes without a heartbeat
REASON_MAX = 300            # a ticker's no-trade reason in options:counts (shown verbatim; a cap, not a cut)
CHAIN_AGE_PAD_H = 96        # the results load quotes up to max_age_h + this (WALL clock: a weekend never
                            # empties the chain; the screener applies the exact market-time age)
REFRESH_EVERY_S = 60        # "Refresh now": at most once per ticker per member per minute (in-process)
REFRESH_MAX_WAIT_S = 5.0    # ... and the read never waits longer than this in one go inside the web request
                            # (a 429 back-off of 15 s+ answers "Massive is busy" instead of holding it)
CYCLE_MIN_DEFAULT = 15      # the collector's pass interval in the session (TST_OPTIONS_CYCLE_MIN, §13.4)
ERROR_TEXT_MAX = 160        # a collector error in the strip line (the whole text is in the tooltip)

NO_QUOTES_NOTE = ("prices are estimated from IV (no bid/ask on this plan) - check live in TWS "
                  "before entering")
NO_QUOTES_RULE_NOTE = "not used - the current data plan (Massive Starter) has no bid/ask"
KEY_MISSING = "TST_MASSIVE_API_KEY is not set on the server"
PRICE_MID = "bid/ask mid"
PRICE_MODEL = "model (from IV)"

MDT_WORDS = {"live": "live", "frozen": "frozen", "delayed": "delayed",
             "delayed_frozen": "delayed frozen", "eod": "end of day"}
SOURCE_NAMES = {"massive": "Massive", "hermes": "Hermes"}
_ON = {"on", "true", "1", "yes", "y", "t"}

# the results table's server-side sort keys (default: the screener's own score order)
RESULT_SORTS = ("score", "symbol", "expiry", "net", "max_profit", "max_loss", "ror", "pop",
                "delta", "iv_rank", "liquidity", "age")

# the collector's own state words (opt_collector writes the state column) -> the strip's words
COLLECTOR_WORDS = {"starting": "starting", "cycle": "running", "running": "running",
                   "history": "reading history", "eod": "end-of-day pass"}


class BasketImport(BaseModel):
    source: str = "paste"            # one of IMPORT_SOURCES
    text: str = ""                   # paste: commas / spaces / newlines
    symbols: list[str] = PField(default_factory=list)   # scanner / screener: what the browser chose
    note: str = ""


# ────────────────────────────────── small helpers ──────────────────────────────────

def _clean_symbol(symbol: str) -> str:
    """The single-ticker form of ivscan._clean_symbols."""
    out = _clean_symbols([symbol or ""], cap=1)
    return out[0] if out else ""


def _owner(user: User) -> str:
    return f"u{user.id}"


def _strategy(s: str | None) -> str:
    return s if s in opt_rules.STRATEGIES else opt_rules.DEFAULT_STRATEGY


def _trigger(resp: Response, events: dict, header: str = "HX-Trigger") -> Response:
    resp.headers[header] = json.dumps(events)        # ASCII-escaped: header-safe
    return resp


def _toast(msg: str, kind: str = "info") -> dict:
    return {"options:toast": {"kind": kind, "msg": msg}}


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


def _num(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _naive(ts) -> _dt.datetime | None:
    if isinstance(ts, str):
        try:
            ts = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(ts, _dt.datetime):
        return None
    if ts.tzinfo is not None:
        ts = ts.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return ts


def _age_min(ts, now: _dt.datetime | None = None) -> float | None:
    t = _naive(ts)
    if t is None:
        return None
    return max(0.0, ((now or _utcnow()) - t).total_seconds() / 60.0)


def _age_text(minutes) -> str:
    """'now' / '12 min' / '5 h' / '3 d'."""
    m = _num(minutes)
    if m is None:
        return "?"
    if m < 1:
        return "now"
    if m < 60:
        return f"{int(round(m))} min"
    if m < 48 * 60:
        return f"{int(round(m / 60))} h"
    return f"{int(round(m / 1440))} d"


def _age_phrase(minutes) -> str:
    """An age inside a sentence: 'under a minute' / '12 min' / ... / 'unknown'."""
    m = _num(minutes)
    if m is None:
        return "unknown"
    return "under a minute" if m < 1 else _age_text(m)


def _k(v) -> str:
    try:
        return f"{float(v):g}"
    except (TypeError, ValueError):
        return "?"


def _money(v) -> str:
    f = _num(v)
    return "-" if f is None else f"${f:,.0f}"


def _exp_label(expiry: str) -> str:
    """'Nov 20' (this year) / "Jan 15 '27" (another year)."""
    try:
        d = _dt.date.fromisoformat(str(expiry)[:10])
    except ValueError:
        return str(expiry or "?")
    lbl = f"{d.strftime('%b')} {d.day}"
    return lbl if d.year == clock.et_date().year else f"{lbl} '{d.strftime('%y')}"


def _who(source, name=None, uid=None) -> str:
    """'Massive' / 'Hermes' / a member's display name (the last two only on rows written
    before v4.134, which age out within 7 days)."""
    if source == "member":
        return name or (f"member #{uid}" if uid else "a member")
    return SOURCE_NAMES.get(source or "", str(source or "unknown"))


def _source_words(source, name=None, uid=None, mdt=None) -> str:
    """'Massive (delayed)' / 'Hermes live' / 'Kui (live)' - who supplied it and how fresh."""
    m = MDT_WORDS.get(mdt or "", mdt or "unknown")
    who = _who(source, name, uid)
    return f"{who} ({m})" if source in ("member", "massive") else f"{who} {m}"


def _sources_words(sources) -> str:
    """opt_screen's ``data.sources`` ('massive·delayed', 'member:Kui·live') in words."""
    out = []
    for s in sources or ():
        head, sep, mdt = str(s).rpartition("·")
        if not sep:
            head, mdt = str(s), ""
        if head.startswith("member:"):
            w = _source_words("member", head[len("member:"):] or None, None, mdt)
        else:
            w = _source_words(head, None, None, mdt)
        if w not in out:
            out.append(w)
    return " + ".join(out) if out else "unknown source"


def _has_quote(leg: dict) -> bool:
    """True when the leg's price is a real bid/ask midpoint (the plan returned quotes):
    opt_screen's own ``priced`` ("quotes" | "model") when the leg carries it, else
    whether it has both a bid and an ask."""
    priced = leg.get("priced")
    if priced in ("quotes", "model"):
        return priced == "quotes"
    return _num(leg.get("bid")) is not None and _num(leg.get("ask")) is not None


def _cycle_min() -> int:
    """The collector's pass interval in the session (TST_OPTIONS_CYCLE_MIN, default 15)."""
    try:
        v = int(str(os.environ.get("TST_OPTIONS_CYCLE_MIN") or CYCLE_MIN_DEFAULT).strip())
    except ValueError:
        return CYCLE_MIN_DEFAULT
    return v if 1 <= v <= 240 else CYCLE_MIN_DEFAULT


def _scrub(text) -> str:
    """A text shown on the page with the Massive key blanked out - the client never puts
    the key into an error, this is only a second lock."""
    s = " ".join(str(text or "").split())
    key = massive.api_key()
    if key and key in s:
        s = s.replace(key, "***")
    return s


# ────────────────────────────────── basket ──────────────────────────────────

def _basket_rows(db: Session, user: User) -> list[OptionBasket]:
    return (db.query(OptionBasket)
              .filter(OptionBasket.owner_key == _owner(user), OptionBasket.active.is_(True))
              .order_by(OptionBasket.pos, OptionBasket.symbol).all())


def _ivscan_universe(user: User) -> list[str]:
    raw = (getattr(user, "prefs", None) or {}).get(UNIVERSE_PREF)
    return _clean_symbols(raw if isinstance(raw, list) else [])


def _ivscan_scan_symbols(db: Session, user: User) -> list[str]:
    model = getattr(_models, "IVScanItem", None)
    if model is None:
        return []
    try:
        rows = db.query(model.symbol).filter(model.user_id == user.id).order_by(model.pos).all()
    except Exception as exc:  # noqa: BLE001 - an import source, never the page
        log.warning("options basket: ivscan items: %s", exc)
        db.rollback()
        return []
    return [s for (s,) in rows]


def _open_trade_symbols(db: Session, user: User) -> list[str]:
    """Symbols of this member's tracked trades still open in the (v1, now unused)
    option_trades table - an import source only."""
    model = getattr(_models, "OptionTrade", None)
    if model is None:
        return []
    try:
        rows = (db.query(model.symbol)
                  .filter(model.user_id == user.id, model.status == "open").distinct().all())
    except Exception as exc:  # noqa: BLE001
        log.warning("options basket: open trades: %s", exc)
        db.rollback()
        return []
    return sorted(s for (s,) in rows)


def _add_symbols(db: Session, user: User, syms: list[str], source: str, note: str = "") -> dict:
    """The one basket writer: duplicates are skipped, the cap is enforced, the rest
    get pos = max + 1. Commits. Returns {added, skipped, over_cap, total, new}. A new
    symbol has no data until the Hermes collector's next pass or a "Refresh now"."""
    source = source if source in SOURCES else "typed"
    existing = {r.symbol: r for r in db.query(OptionBasket).filter(OptionBasket.owner_key == _owner(user)).all()}
    n_active = sum(1 for r in existing.values() if r.active)
    pos = max((r.pos for r in existing.values()), default=-1) + 1
    added = skipped = over_cap = 0
    new: list[str] = []
    today = clock.et_today()
    seen: set[str] = set()
    for sym in syms:
        if not sym or sym in seen:
            skipped += 1
            continue
        seen.add(sym)
        row = existing.get(sym)
        if row is not None and row.active:
            skipped += 1
            continue
        if n_active >= MAX_BASKET:
            over_cap += 1
            continue
        if row is not None:                      # kept-but-off row: switch it back on
            row.active = True
            row.added_on = today
            row.source = source
            row.pos = pos
        else:
            db.add(OptionBasket(user_id=user.id, owner_key=_owner(user), symbol=sym,
                                source=source, note=(note or None), active=True,
                                added_on=today, pos=pos))
        pos += 1
        n_active += 1
        added += 1
        new.append(sym)
    db.commit()
    return {"added": added, "skipped": skipped, "over_cap": over_cap, "total": n_active, "new": new}


def _in_own_basket(db: Session, user: User, sym: str) -> bool:
    return (db.query(OptionBasket.id)
              .filter(OptionBasket.owner_key == _owner(user), OptionBasket.symbol == sym,
                      OptionBasket.active.is_(True)).first()) is not None


def _dot(f: dict | None, rth: bool) -> str:
    """The basket's data-age dot: emerald = fresh, amber = getting old, rose = stale,
    slate = no data yet."""
    if not f:
        return "slate"
    age = _num(f.get("age_min"))
    if age is None:
        return "slate"
    if age <= FRESH_MIN or (not rth and not clock.older_than_last_close(f.get("as_of"))):
        return "emerald"
    if age <= 24 * 60:
        return "amber"
    return "rose"


def _basket_item(r: OptionBasket, f: dict | None, u: dict | None, rth: bool) -> dict:
    """One basket row's display values (``f``: opt_store.freshness, ``u``: the stored
    underlying)."""
    u = u or {}
    src = _source_words(f.get("source"), f.get("source_name"), f.get("source_user_id"), f.get("mdt")) if f else None
    if f is None:
        tip = (f"{r.symbol}: waiting for its first read - the Hermes collector reads it from Massive on its "
               f"next pass (every {_cycle_min()} min while the US market is open); the refresh button reads it now")
    else:
        tip = (f"{r.symbol}: data from {_age_text(f.get('age_min'))} ago · {src}, "
               f"{f.get('n') or 0} contracts. The refresh button reads it again now.")
    return {"row": r, "sym": r.symbol, "fresh": f, "waiting": f is None,
            "dot": _dot(f, rth), "age_text": _age_text(f.get("age_min")) if f else None,
            "src_text": src, "iv_rank": _num(u.get("iv_rank")),
            "history_done": bool(u.get("history_done")), "tip": tip}


def _basket_context(db: Session, user: User, *, sort: str = "added") -> dict:
    """Every basket row with its freshness (opt_store.freshness: the newest read of that
    ticker) and the stored IV rank - one query each, never a market call."""
    rows = _basket_rows(db, user)
    syms = [r.symbol for r in rows]
    fr = opt_store.freshness(db, syms) if syms else {}
    unds = opt_store.underlyings(db, syms) if syms else {}
    rth = clock.us_session_open()
    items = [_basket_item(r, fr.get(r.symbol), unds.get(r.symbol), rth) for r in rows]
    sort = sort if sort in BASKET_SORTS else "added"
    keys = {
        "added": lambda it: (it["row"].pos, it["sym"]),
        "symbol": lambda it: it["sym"],
        "fresh": lambda it: (0 if it["fresh"] is None else 1,
                             -(_num((it["fresh"] or {}).get("age_min")) or 0.0), it["sym"]),
        "iv": lambda it: (it["iv_rank"] is None, -(it["iv_rank"] or 0.0), it["sym"]),
    }
    items.sort(key=keys[sort])
    return {"user": user, "items": items, "sort": sort, "n": len(rows), "max_basket": MAX_BASKET,
            "n_waiting": sum(1 for it in items if it["waiting"]),
            "n_watchlist": len(uwl.symbol_set(db, user)),
            "n_ivscan_list": len(_ivscan_universe(user)),
            "n_ivscan_scan": len(_ivscan_scan_symbols(db, user)),
            "n_positions": len(_open_trade_symbols(db, user)),
            "criteria": DEFAULT_CRITERIA, "cycle_min": _cycle_min()}


def _one_item(db: Session, user: User, sym: str) -> dict | None:
    """The basket row of one ticker (after a "Refresh now"), or None when it is gone."""
    row = (db.query(OptionBasket)
             .filter(OptionBasket.owner_key == _owner(user), OptionBasket.symbol == sym,
                     OptionBasket.active.is_(True)).first())
    if row is None:
        return None
    return _basket_item(row, opt_store.freshness(db, [sym]).get(sym), opt_store.underlying(db, sym),
                        clock.us_session_open())


def _part(part: str | None) -> str:
    """'rows' = only the rows container of the basket (the Add / Import footer and its
    inputs stay as the member left them); anything else = the whole fragment."""
    return "rows" if part == "rows" else "all"


def _basket_response(request: Request, db: Session, user: User, *, sort: str = "added",
                     events: dict | None = None, part: str = "all") -> Response:
    ctx = _basket_context(db, user, sort=sort)
    ctx["part"] = _part(part)
    resp = templates.TemplateResponse(request, "_opt_basket.html", ctx)
    # the basket itself is in this response; the results re-screen on universe-changed
    ev: dict = {"options:universe-changed": {"n": ctx["n"]}}
    ev.update(events or {})
    return _trigger(resp, ev)


# ────────────────────────────────── rules ──────────────────────────────────

def _input_text(v, f: opt_rules.Field) -> str:
    if f.kind == "bool":
        return "on" if v else ""
    if f.kind == "int":
        try:
            return str(int(v))
        except (TypeError, ValueError):
            return ""
    if f.kind == "num":
        try:
            return f"{float(v):g}"
        except (TypeError, ValueError):
            return ""
    return str(v if v is not None else "")


def _input_step(name: str, f: opt_rules.Field) -> float | None:
    """The number input's step, on the same grid as its min AND its house default.

    A browser counts steps from ``min``, so with min 0.01 and step 0.05 the arrows
    move the house default 0.50 to 0.51; the panel's own step (``opt_rules.step``) is
    refined (/2, /5, /10, ...) until ``default - min`` is a whole number of steps."""
    st = opt_rules.step(name, f)
    if not st or f.kind != "num" or f.lo is None or not isinstance(f.default, (int, float)):
        return st
    gap = float(f.default) - float(f.lo)
    for div in (1, 2, 4, 5, 10, 20, 25, 50, 100):
        cand = st / div
        n = gap / cand
        if abs(n - round(n)) < 1e-6:
            return float(f"{cand:.6g}")
    return st


def _unused_rules() -> dict[str, str]:
    """{rule name: note} of the rules the data plan cannot apply: the bid/ask rules while
    Massive returns no quotes (opt_rules.UNUSED_WITHOUT_QUOTES); empty with quotes. The
    panel greys those fields out with the note (their values stay stored)."""
    if opt_rules.quotes_available():
        return {}
    raw = opt_rules.UNUSED_WITHOUT_QUOTES
    if isinstance(raw, dict):
        return {str(k): str(v or NO_QUOTES_RULE_NOTE) for k, v in raw.items()}
    return {str(k): NO_QUOTES_RULE_NOTE for k in (raw or ())}


def _rules_context(db: Session, user: User, strategy: str, *, msg: str = "", msg_kind: str = "ok",
                   field_errs: dict | None = None, errors: list | None = None, part: str = "panel",
                   posted: set | None = None) -> dict:
    """The panel: the shared block, then the strategy's own block - every field with its
    label, help (tooltip), unit, bounds, current value, whether it differs from the
    house default, and the note when the data plan cannot apply it. ``errors`` (all of
    them) feed the message line; ``field_errs`` the slot under each field. ``posted`` (a
    save): the 'block.name' keys of the fields that request carried - only their error
    slots and changed-dots are re-sent, so a warning on another field (a clamped value)
    stays until THAT field is saved again."""
    prefs = opt_rules.read(db, user)
    view = opt_rules.for_strategy(prefs, strategy)
    field_errs = field_errs or {}
    unused = _unused_rules()
    blocks = []
    n_changed = 0
    for block, title in (("shared", "Every strategy"), (strategy, opt_rules.LABELS[strategy])):
        vals = view["shared"] if block == "shared" else view["rules"]
        fields = []
        for name, f in opt_rules.SCHEMA[block].items():
            v = vals.get(name, f.default)
            changed = v != f.default
            n_changed += 1 if changed else 0
            words = opt_rules.CHOICE_LABELS.get(name, {})
            fields.append({
                "block": block, "name": name, "key": f"{block}.{name}", "id": f"{block}-{name}",
                "label": f.label, "help": f.help, "unit": f.unit, "kind": f.kind,
                "value": v, "text": _input_text(v, f),
                "default_text": (words.get(f.default, f.default) if f.kind == "choice" else
                                 ("on" if f.default else "off") if f.kind == "bool" else _input_text(f.default, f)),
                "lo": f.lo, "hi": f.hi, "step": _input_step(name, f),
                "choices": [(c, words.get(c, c)) for c in (f.choices or ())],
                "changed": changed, "err": field_errs.get(f"{block}.{name}"),
                "posted": posted is None or f"{block}.{name}" in posted,
                "unused": unused.get(name),
            })
        blocks.append({"block": block, "title": title, "fields": fields})
    band = opt_rules.band_errors(prefs, strategy)
    return {"user": user, "strategy": strategy, "label": opt_rules.LABELS[strategy], "blocks": blocks,
            "msg": msg, "msg_kind": msg_kind, "errors": list(errors or []), "band": band,
            "n_changed": n_changed, "part": part}


def _field_index(strategy: str) -> dict[str, tuple[str, str, opt_rules.Field]]:
    """Posted form key -> (block, name, Field): 'block.name', 'block__name', or a bare
    name (shared first - no name is in both blocks)."""
    idx: dict = {}
    for block, name, f in opt_rules.fields(strategy):
        for key in (f"{block}.{name}", f"{block}__{name}", name):
            idx.setdefault(key, (block, name, f))
    return idx


async def _form_dict(request: Request) -> dict:
    """The posted form as {key: value}. A key posted more than once (a checkbox and its
    hidden 'off' twin) reads 'on' when any copy is on, else the last copy."""
    form = await request.form()
    out: dict = {}
    for k in form.keys():
        vals = [v for v in form.getlist(k) if isinstance(v, str)]
        if not vals:
            continue
        if len(vals) > 1 and any(v.strip().lower() in _ON for v in vals):
            out[k] = "on"
        else:
            out[k] = vals[-1]
    return out


def _rules_changed(resp: Response, strategy: str) -> Response:
    return _trigger(resp, {"options:rules-changed": {"strategy": strategy}})


# ────────────────────────────────── results ──────────────────────────────────

def _legs_text(c: dict) -> str:
    """'-94P / +92P' (one expiry) or '+360C Jan 15 '27 / -380C Nov 20' (two)."""
    legs = c.get("legs") or []
    exps = {l.get("expiry") for l in legs}
    parts = []
    for l in legs:
        sign = "−" if l.get("side") == "sell" else "+"
        s = f"{sign}{_k(l.get('strike'))}{l.get('right') or ''}"
        if len(exps) > 1:
            s += f" {_exp_label(l.get('expiry'))}"
        parts.append(s)
    return " / ".join(parts)


def _expiry_text(c: dict) -> str:
    exps = sorted({l.get("expiry") for l in (c.get("legs") or []) if l.get("expiry")})
    lbl = " / ".join(_exp_label(e) for e in exps) or "?"
    return f"{lbl} ({c.get('dte')}d)"


def _delta_view(strategy: str, c: dict) -> tuple[str, str, float | None]:
    """(text, tooltip, sort value) of the Δ column - the leg that defines the trade."""
    fam = opt_rules.FAMILY.get(strategy)
    m = c.get("metrics") or {}

    def d(v):
        f = _num(v)
        return "-" if f is None else f"{f:.2f}"

    if fam in ("single", "leaps"):
        return d(m.get("delta")), "Delta of the option you buy (without its sign).", _num(m.get("delta"))
    if fam == "credit_vertical":
        return (d(m.get("short_delta")), f"Delta of the option you sell; the one you buy is {d(m.get('long_delta'))}.",
                _num(m.get("short_delta")))
    if fam in ("debit_vertical", "diagonal"):
        return (f"{d(m.get('long_delta'))}/{d(m.get('short_delta'))}", "Delta of the option you buy / the one you sell.",
                _num(m.get("long_delta")))
    if fam == "condor":
        return (f"{d(m.get('short_put_delta'))}/{d(m.get('short_call_delta'))}",
                "Delta of the put you sell / the call you sell.", _num(m.get("short_put_delta")))
    if fam == "calendar":
        return d(m.get("front_delta")), "Delta of the near-term call (the one you sell).", _num(m.get("front_delta"))
    return "-", "", None


def _net_view(c: dict) -> tuple[str, str, bool]:
    net = _num(c.get("net"))
    nat = _num(c.get("net_natural"))
    if net is None:
        return "-", "", False
    credit = net > 0
    word = "credit" if credit else "debit"
    legs = c.get("legs") or []
    priced = (c.get("data") or {}).get("priced")
    modelled = (priced == "model") if priced in ("quotes", "model") else \
        (bool(legs) and not all(_has_quote(l) for l in legs))
    tip = (f"{word.capitalize()} {_money(abs(net) * 100)} per contract at the leg prices"
           + (" (estimated from IV - this data has no bid/ask; check the live price in TWS)" if modelled else
              " (the bid/ask mids)"))
    if nat is not None:
        tip += f"; {_money(abs(nat) * 100)} {'credit' if nat > 0 else 'debit'} at the bid / ask"
    return f"${abs(net):.2f} {word}", tip + ".", credit


def _row_view(strategy: str, c: dict, rth: bool) -> dict:
    """The display strings of one result row (the candidate itself is untouched)."""
    data = c.get("data") or {}
    liq = c.get("liquidity") or {}
    und = c.get("underlying") or {}
    age = _num(data.get("age_min"))
    net_text, net_tip, credit = _net_view(c)
    d_text, d_tip, d_sort = _delta_view(strategy, c)
    mp = c.get("max_profit")
    oi, sp = _num(liq.get("oi_min")), _num(liq.get("spread_max"))
    spp, vmin = _num(liq.get("spread_pct_max")), _num(liq.get("volume_min"))
    liq_tip = ("Smallest open interest across the legs: " + (f"{oi:,.0f}" if oi is not None else "not reported")
               + "; widest bid/ask: " + (f"${sp:.2f}" if sp is not None else "not known (no bid/ask on this data)")
               + (f" ({spp:.0f}% of its mid)" if spp is not None else "")
               + "; least traded today: " + (f"{vmin:,.0f}" if vmin is not None else "not reported") + ".")
    liq_text = f"OI {oi:,.0f}" if oi is not None else "OI ?"
    if sp is not None:
        liq_text += f" · ${sp:.2f}"
    elif vmin is not None:
        liq_text += f" · vol {vmin:,.0f}"
    srcs = _sources_words(data.get("sources"))
    oldest = data.get("as_of_oldest")
    oldest_dt = _naive(oldest)
    # age_min is the MARKET-time age (the column, and what max_age_h filters on: the
    # clock stops while the market is closed); wall_age_min is the plain clock age
    wall = _num(data.get("wall_age_min"))
    data_tip = (f"Market-time age {_age_phrase(age)} (the age clock stops while the market is closed, so data "
                f"from the last close stays current until the next open) · on the clock, data from "
                f"{_age_phrase(wall if wall is not None else age)} ago"
                + (f" ({oldest_dt.strftime('%Y-%m-%d %H:%M')} UTC)" if oldest_dt else "")
                + f" · {srcs}." + (" Legs come from more than one source." if data.get("mixed") else ""))
    return {
        "c": c, "id": c.get("id"), "sym": c.get("symbol"),
        "legs_text": _legs_text(c), "expiry_text": _expiry_text(c),
        "net_text": net_text, "net_tip": net_tip, "is_credit": credit,
        "max_profit_text": (_money(mp) if mp is not None else
                            ("unlimited" if strategy in ("buy_call", "leaps_call") else "-")),
        "max_loss_text": _money(c.get("max_loss")),
        "ror_text": "-" if _num(c.get("ror")) is None else f"{_num(c.get('ror')) * 100:.0f}%",
        "pop_text": "-" if _num(c.get("pop")) is None else f"{_num(c.get('pop')) * 100:.0f}%",
        "delta_text": d_text, "delta_tip": d_tip, "delta_sort": d_sort,
        "ivr_text": "-" if _num(und.get("iv_rank")) is None else f"{_num(und.get('iv_rank')):.0f}",
        "liq_text": liq_text, "liq_tip": liq_tip,
        "data_text": f"{_age_text(age)} · {srcs}",
        "data_tip": data_tip,
        "data_amber": bool(rth and age is not None and age > FRESH_MIN),
    }


def _sort_rows(views: list[dict], sort: str, direction: str) -> list[dict]:
    """Server-side sort; a missing value always sorts last."""
    if sort == "score":
        out = list(views)                      # the screener's order: best score first
        return out if direction == "desc" else out[::-1]
    getters = {
        "symbol": lambda v: v["sym"],
        "expiry": lambda v: v["c"].get("dte"),
        "net": lambda v: _num(v["c"].get("net")),
        "max_profit": lambda v: _num(v["c"].get("max_profit")),
        "max_loss": lambda v: _num(v["c"].get("max_loss")),
        "ror": lambda v: _num(v["c"].get("ror")),
        "pop": lambda v: _num(v["c"].get("pop")),
        "delta": lambda v: v["delta_sort"],
        "iv_rank": lambda v: _num((v["c"].get("underlying") or {}).get("iv_rank")),
        "liquidity": lambda v: _num((v["c"].get("liquidity") or {}).get("oi_min")),
        "age": lambda v: _num((v["c"].get("data") or {}).get("age_min")),
    }
    get = getters.get(sort)
    if get is None:
        return list(views)
    have = [v for v in views if get(v) is not None]
    none = [v for v in views if get(v) is None]
    have.sort(key=get, reverse=(direction == "desc"))
    return have + none


def _summary(n_passed: int, n_tickers: int) -> str:
    return (f"{n_passed} trade{'' if n_passed == 1 else 's'} pass · "
            f"{n_tickers} ticker{'' if n_tickers == 1 else 's'}")


def _dte_window(r: dict) -> tuple[int, int]:
    """The union of the strategy's expiry windows in days - every ``*dte_lo`` /
    ``*dte_hi`` rule, and LEAPS ``months_lo`` / ``months_hi`` x 30.44 - widened to whole
    days. Only these expiries are loaded for the results (the screener drops the rest
    under its "dte" rule anyway)."""
    los = [float(v) for k, v in r.items() if k.endswith("dte_lo") and _num(v) is not None]
    his = [float(v) for k, v in r.items() if k.endswith("dte_hi") and _num(v) is not None]
    if _num(r.get("months_lo")) is not None and _num(r.get("months_hi")) is not None:
        los.append(float(r["months_lo"]) * opt_screen.DAYS_PER_MONTH)
        his.append(float(r["months_hi"]) * opt_screen.DAYS_PER_MONTH)
    if not los or not his:
        return 0, 3650
    lo, hi = min(los + his), max(los + his)      # a "from" above its "to" still loads its days
    return max(0, int(math.floor(lo))), int(math.ceil(hi))


def _stock_prefail(sh: dict, r: dict, und: dict | None) -> bool:
    """True when the ticker certainly fails one of the screener's first stock filters
    - price, 20-day stock volume, IV rank range (opt_screen._stock_fail's order and
    tolerance) - from its stored stock facts alone, so its chain need not be loaded.
    Needs a stored spot; without one nothing is decided here."""
    u = und or {}
    spot = _num(u.get("spot"))
    if not spot or spot <= 0:
        return False
    eps = opt_screen.EPS
    if sh.get("price_min", 0) > 0 and spot < sh["price_min"] - eps:
        return True
    if sh.get("stock_vol_min", 0) > 0:
        v = _num(u.get("avg_vol20"))
        if v is None or v < sh["stock_vol_min"]:
            return True
    lo, hi = r.get("iv_rank_min"), r.get("iv_rank_max")
    if lo is not None and hi is not None and (lo > 0 or hi < 100):
        ivr = _num(u.get("iv_rank"))
        if ivr is None or not (lo - eps <= ivr <= hi + eps):
            return True
    return False


def _stored_expiries(db: Session, syms: list[str], day: str) -> dict[str, list[str]]:
    """{symbol: [expiry...]} of every stored expiry >= ``day`` (one DISTINCT over the
    (symbol, expiry) index - never the rows). Feeds the expiry stubs below."""
    if not syms:
        return {}
    stmt = (select(OptQuote.symbol, OptQuote.expiry)
            .where(OptQuote.symbol.in_(syms), OptQuote.expiry >= day)
            .distinct())
    out: dict[str, list[str]] = {}
    for s, e in db.execute(stmt).all():
        out.setdefault(s, []).append(e)
    return {s: sorted(v) for s, v in out.items()}


def _stub(expiry: str, day: _dt.date) -> dict:
    try:
        dte = (_dt.date.fromisoformat(expiry) - day).days
    except ValueError:
        dte = None
    return {"expiry": expiry, "dte": dte, "calls": [], "puts": []}


def _with_stubs(chain: dict, stored: list[str], day: _dt.date) -> tuple[dict, int]:
    """``chain`` plus an empty entry for every stored expiry the windowed load left out
    (outside the window, or every quote older than the age pre-filter), so the
    screener still counts those expiries under its own rules ("dte", "monthly",
    "earnings") and never reads a ticker WITH data as "no option data yet". Returns
    (chain, number of stub entries)."""
    have = {e.get("expiry") for e in chain.get("expiries") or ()}
    extra = [_stub(e, day) for e in stored if e not in have]
    if not extra:
        return chain, 0
    out = dict(chain)
    out["expiries"] = sorted(list(chain.get("expiries") or []) + extra, key=lambda e: str(e.get("expiry")))
    return out, len(extra)


def _results_context(db: Session, user: User, strategy: str, sort: str, direction: str) -> dict:
    """Screen the member's basket. The heavy part - the chains - is loaded only where
    it can matter: a ticker that already fails the stock filters (price, 20-day volume,
    IV rank range) on its stored facts is not loaded at all, and the others load only
    the expiries inside the strategy's window and quotes younger than max_age_h + 96 h
    (wall clock; the screener then applies the exact market-time age). Every stored
    expiry the load left out is passed as an empty stub, so the funnel's ticker and
    expiry counts and every "why" stay what a full load would give."""
    rows = _basket_rows(db, user)
    syms = [r.symbol for r in rows]
    prefs = opt_rules.read(db, user)
    rules = opt_rules.for_strategy(prefs, strategy)
    ctx = {"user": user, "strategy": strategy, "label": opt_rules.LABELS[strategy], "sort": sort,
           "dir": direction, "n_tickers": len(syms), "views": [], "funnel": [], "tickers": {},
           "n_passed": 0, "n_considered": 0, "error": None, "syms_watch": list(syms), "no_trade": [],
           "rules_note": None}
    if not syms:
        return ctx
    sh, r = rules["shared"], rules["rules"]
    day = clock.et_date()
    dte_min, dte_max = _dte_window(r)
    max_age = float(sh.get("max_age_h") or 24) + CHAIN_AGE_PAD_H
    stale_only: set[str] = set()
    try:
        unds = opt_store.underlyings(db, syms)
        stored = _stored_expiries(db, syms, day.isoformat())
        chains: dict[str, dict] = {}
        for s in syms:
            u = unds.get(s) or {}
            exps = stored.get(s) or []
            if not exps:                                   # nothing stored: the screener says "no option data yet"
                chains[s] = {"symbol": s, "spot": _num(u.get("spot")), "expiries": []}
            elif _stock_prefail(sh, r, u):                 # fails on its stock facts: the chain is never read
                chains[s] = {"symbol": s, "spot": _num(u.get("spot")),
                             "expiries": [_stub(e, day) for e in exps]}
            else:
                ch = opt_store.chain_view(db, s, today=day, dte_min=dte_min, dte_max=dte_max, max_age_h=max_age)
                if not any((e.get("calls") or e.get("puts")) for e in ch.get("expiries") or ()) \
                        and any(dte_min <= (_stub(e, day)["dte"] or -1) <= dte_max for e in exps):
                    stale_only.add(s)                      # expiries in the window, but no quote young enough
                chains[s], _ = _with_stubs(ch, exps, day)
        res = opt_screen.screen(strategy, chains, unds, rules)
    except Exception as exc:  # noqa: BLE001 - the page says what went wrong, never a blank box
        log.warning("options results %s: %s", strategy, exc, exc_info=True)
        db.rollback()
        ctx["error"] = f"The list could not be built: {type(exc).__name__}: {str(exc)[:160]}"
        return ctx
    rth = clock.us_session_open()
    views = [_row_view(strategy, c, rth) for c in res.get("rows") or []]
    tickers = res.get("tickers") or {}
    for s in stale_only:
        t = tickers.get(s)
        # only a ticker that got past the stock filters (its reason is about expiries or
        # trades): "ATR not known yet" / "earnings date unknown" stay the real blocker
        if t is not None and not t.get("passed") and str(t.get("reason") or "").startswith(("no trade", "no expir")):
            t["reason"] = (f"its stored quotes in the {dte_min}-{dte_max} day window are all too old "
                           f"- waiting for a fresh read")
    ctx.update({
        "views": _sort_rows(views, sort, direction),
        "funnel": res.get("funnel") or [], "tickers": tickers,
        "n_passed": int(res.get("n_passed") or 0), "n_considered": int(res.get("n_considered") or 0),
        "syms_watch": sorted(syms),
        "no_trade": sorted(((s, (t or {}).get("reason") or "no trade passes") for s, t in tickers.items()
                            if not (t or {}).get("passed")), key=lambda p: p[0]),
        "rules_note": (opt_rules.band_errors(prefs, strategy) or [None])[0],
    })
    return ctx


# ────────────────────────────────── trade detail ──────────────────────────────────

def _leg_view(l: dict, now: _dt.datetime) -> dict:
    """One leg of the opened trade: its price and where that price comes from - the
    bid/ask midpoint when the data has quotes, else a model price from the leg's own IV
    (Massive Starter has no bid/ask)."""
    iv = _num(l.get("iv"))
    mid = _num(l.get("mid"))
    quoted = _has_quote(l)
    return {**l, "side_word": "Sell" if l.get("side") == "sell" else "Buy",
            "right_word": "call" if l.get("right") == "C" else "put",
            "strike_text": _k(l.get("strike")), "expiry_text": _exp_label(l.get("expiry")),
            "iv_text": "-" if iv is None else f"{iv * 100:.1f}%",
            "price_text": "-" if mid is None else f"{mid:.2f}",
            "price_kind": PRICE_MID if quoted else (PRICE_MODEL if mid is not None else "no price"),
            "quoted": quoted,
            "age_text": _age_text(_age_min(l.get("as_of"), now)),
            "as_of_dt": _naive(l.get("as_of")),
            "src_text": _source_words(l.get("source"), l.get("source_name"), l.get("source_user_id"), l.get("mdt")),
            "mdt_text": MDT_WORDS.get(l.get("mdt") or "", l.get("mdt") or "unknown")}


def _detail(db: Session, user: User, strategy: str, cid: str) -> tuple[dict | None, dict, dict | None, str]:
    """(opt_screen.detail result or None, chain, underlying, symbol) for a candidate id."""
    parsed = opt_screen.parse_id(cid)
    sym = _clean_symbol(parsed[0]) if parsed else ""
    if not sym:
        return None, {}, None, ""
    prefs = opt_rules.read(db, user)
    rules = opt_rules.for_strategy(prefs, strategy)
    # only the trade's own expiries are read (a day either side); no age cut - an
    # opened trade shows its legs however old their data is
    day = clock.et_date()
    dtes = []
    for exp, _r, _k in parsed[2]:
        try:
            dtes.append((_dt.date.fromisoformat(exp) - day).days)
        except ValueError:
            pass
    if dtes:
        chain = opt_store.chain_view(db, sym, today=day, dte_min=max(0, min(dtes) - 1), dte_max=max(dtes) + 1)
    else:
        chain = opt_store.chain_view(db, sym, today=day)
    und = opt_store.underlying(db, sym)
    try:
        d = opt_screen.detail(strategy, cid, chain, und, rules)
    except Exception as exc:  # noqa: BLE001
        log.warning("options trade %s: %s", cid, exc, exc_info=True)
        d = None
    return d, chain, und, sym


def _sigma_fallback(und: dict | None) -> float | None:
    u = und or {}
    v = _num(u.get("iv30")) or _num(u.get("hv20"))
    return v / 100.0 if v else None


def _payoff_url(strategy: str, cid: str) -> str:
    return f"/options/payoff?strategy={quote(strategy)}&id={quote(cid, safe='')}"


# ────────────────────────────────── collector line ──────────────────────────────────

def _collector_view(db: Session, now: _dt.datetime | None = None) -> dict:
    """The Hermes collector line of the strip (§13.4, §13.6): state running / idle /
    history / eod / error / stale / none (and stopped), the words, the tooltip, a tone,
    and ``pass_key`` - it changes when a pass finishes, and the page then re-screens."""
    now = now or _utcnow()
    st = opt_store.collector_status(db)
    if st is None:
        return {"state": "none", "tone": "slate", "pass_key": "",
                "text": "Massive collector: no heartbeat yet",
                "tip": "The Hermes options collector (TST-Options-Collector) has not reported yet. Until it "
                       "runs, a ticker is read only when a member presses its refresh button."}
    age = _age_min(st.get("heartbeat"), now)
    raw = str(st.get("state") or "").strip().lower()
    detail = _scrub(st.get("phase_detail") or "")
    eod = st.get("last_eod_on")
    finished = _naive(st.get("cycle_finished"))
    pass_key = "|".join(str(x or "") for x in (st.get("cycle_n"), finished and finished.isoformat(), eod))
    tail = ((f" · last pass finished {finished.strftime('%Y-%m-%d %H:%M')} UTC" if finished else "")
            + (f" · last end-of-day pass {eod}" if eod else ""))
    if age is None or age > STALE_HEARTBEAT_MIN:
        return {"state": "stale", "tone": "amber", "pass_key": pass_key,
                "text": f"Massive collector: no heartbeat for {_age_text(age) if age is not None else 'a while'}",
                "tip": f"The collector on Hermes last reported {_age_phrase(age)} ago (state {raw or '?'}). "
                       f"It may have stopped - an administrator can check the TST-Options-Collector task on "
                       f"Hermes. The refresh button on a ticker still reads it now." + tail}
    if raw == "error":
        # the reported detail is the pause's own reason + next try (the tray reads it first
        # too); last_error is only the newest one-ticker failure
        reason = _scrub(detail or st.get("last_error") or "unknown error")
        short = reason if len(reason) <= ERROR_TEXT_MAX else reason[:ERROR_TEXT_MAX - 1].rstrip() + "…"
        return {"state": "error", "tone": "rose", "pass_key": pass_key,
                "text": f"Collector error: {short}",
                "tip": f"The Hermes collector cannot read Massive: {reason}. It keeps trying on its own; the "
                       f"data on the page stays as it was last read." + tail}
    if raw == "stopped":
        return {"state": "stopped", "tone": "slate", "pass_key": pass_key,
                "text": "Massive collector: stopped",
                "tip": (detail or "The collector was stopped on Hermes.") + tail}
    mdt = st.get("mdt")
    feed = f"{MDT_WORDS.get(mdt, mdt)} data" if mdt and mdt not in ("delayed", "eod") else "data 15 min delayed"
    head = COLLECTOR_WORDS.get(raw)
    if head is None:                                       # idle (between passes, out of hours) or unknown
        parts = ["Massive: idle"]
        if st.get("cycle_n"):
            parts.append(f"pass {st['cycle_n']} done")
        parts.append(feed)
        return {"state": "idle", "tone": "slate", "pass_key": pass_key, "text": " · ".join(parts),
                "tip": (f"Between passes: the collector reads every basket from Massive every {_cycle_min()} min "
                        f"while the US market is open, and once more after the close"
                        + (f" · {detail}" if detail else "") + f" · heartbeat {_age_phrase(age)} ago" + tail)}
    parts = [f"Massive: {head}"]
    if raw in ("cycle", "running") and st.get("cycle_n"):
        parts.append(f"pass {st['cycle_n']}")
    if st.get("symbols_total"):
        parts.append(f"{st.get('symbols_done') or 0}/{st['symbols_total']} tickers")
    if raw != "history":
        parts.append(feed)
    state = "running" if raw in ("cycle", "running", "starting") else raw
    return {"state": state, "tone": "emerald", "pass_key": pass_key, "text": " · ".join(parts),
            "tip": (f"The Hermes collector reads Massive (Options Starter, 15 min delayed) · heartbeat "
                    f"{_age_phrase(age)} ago" + (f" · {detail}" if detail else "") + tail)}


# ────────────────────────────────── "Refresh now" ──────────────────────────────────

_refresh_lock = threading.Lock()
_refresh_last: dict[tuple[int, str], float] = {}      # (user id, symbol) -> monotonic time of the last read
_now_s = time.monotonic                                # the limiter's clock (a test replaces it)


def reset_refresh_limits() -> None:
    """Forget every member's last "Refresh now" (tests, a restart)."""
    with _refresh_lock:
        _refresh_last.clear()


def _refresh_slot(user_id: int, sym: str) -> int:
    """0 and the slot taken when this member may read ``sym`` now; else the whole
    seconds left before the next read (one per ticker per member per minute)."""
    now = _now_s()
    with _refresh_lock:
        last = _refresh_last.get((user_id, sym))
        if last is not None and now - last < REFRESH_EVERY_S:
            return max(1, int(math.ceil(REFRESH_EVERY_S - (now - last))))
        if len(_refresh_last) > 5000:                  # never grows without bound
            for k in [k for k, t in _refresh_last.items() if now - t >= REFRESH_EVERY_S]:
                _refresh_last.pop(k, None)
        _refresh_last[(user_id, sym)] = now
        return 0


def _refresh_free(user_id: int, sym: str) -> None:
    """Give the slot back (nothing was asked of Massive)."""
    with _refresh_lock:
        _refresh_last.pop((user_id, sym), None)


def _refresh_sleep(seconds: float) -> None:
    """The refresh client's wait: the short pacing and network-retry pauses are kept;
    a longer one (the 429 back-off: 15 s doubling to 2 min) ends the read as "rate"
    rather than holding the member's request for minutes."""
    if seconds > REFRESH_MAX_WAIT_S:
        raise massive.MassiveError("rate", "Massive asked to wait %.0f s (too many requests)" % seconds, 429)
    if seconds > 0:
        time.sleep(seconds)


def _massive_client():
    """The Massive client of one "Refresh now" (the key from TST_MASSIVE_API_KEY)."""
    return massive.Client(sleep=_refresh_sleep)


def _massive_problem(exc: Exception) -> tuple[int, str]:
    """(HTTP status, plain words) for a failed Massive read (MassiveError.kind)."""
    kind = getattr(exc, "kind", None)
    if kind == "config":
        return 503, "Massive is not set up on the server"
    if kind in ("auth", "plan"):
        return 502, _scrub(str(exc)) or ("Massive rejected the API key" if kind == "auth"
                                         else "the Massive plan does not include this data")
    if kind == "rate":
        return 503, "Massive is busy, try again in a minute"
    if kind == "network":
        return 503, "Massive could not be reached - try again in a minute"
    status = getattr(exc, "status", None)
    return 502, f"Massive answered with an error{f' (HTTP {status})' if status else ''} - try again in a minute"


def _refresh_fail(status: int, msg: str, *, retry_after: int | None = None) -> Response:
    """A refresh that did not happen: the plain reason as the body and as a toast; the
    basket row stays as it is (no swap)."""
    resp = Response(msg, status_code=status, media_type="text/plain")
    resp.headers["HX-Reswap"] = "none"
    if retry_after:
        resp.headers["Retry-After"] = str(int(retry_after))
    return _trigger(resp, _toast(msg, "err"))


# ────────────────────────────────── routes: page + basket ──────────────────────────────────

@router.get("", response_class=HTMLResponse)
def options_home(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """The shell: every panel is an HTMX fragment loaded by the page script."""
    return templates.TemplateResponse(request, "options.html", {
        "user": user, "n_basket": len(_basket_rows(db, user)), "max_basket": MAX_BASKET,
        "bridge_port": BRIDGE_PORT, "strategies": [(k, opt_rules.LABELS[k]) for k in opt_rules.STRATEGIES],
        "default_strategy": opt_rules.DEFAULT_STRATEGY, "criteria": DEFAULT_CRITERIA,
        "fresh_min": FRESH_MIN})


@router.get("/basket", response_class=HTMLResponse)
def basket(request: Request, sort: str = "added", part: str = "", user: User = Depends(require_user),
           db: Session = Depends(get_db)):
    """The basket fragment; ``part=rows`` = only the rows container (a sort click, add /
    remove, the page's refresh after a collector pass), so the Add / Import footer keeps
    what the member typed and which panel is open."""
    ctx = _basket_context(db, user, sort=sort)
    ctx["part"] = _part(part)
    return templates.TemplateResponse(request, "_opt_basket.html", ctx)


@router.post("/basket/add", response_class=HTMLResponse)
def basket_add(request: Request, symbol: str = Form(""), note: str = Form(""), sort: str = Form("added"),
               part: str = Form(""), user: User = Depends(require_user), db: Session = Depends(get_db)):
    """One typed ticker (form-encoded) = an import with source 'typed'."""
    sym = _clean_symbol(symbol)
    res = _add_symbols(db, user, [sym] if sym else [], "typed", note)
    msg = (f"{sym} added - press its refresh button to read it from Massive now, or the collector reads it "
           f"on its next pass" if res["added"] else
           ("Basket is full (%d tickers)" % MAX_BASKET if res["over_cap"] else
            (f"{sym} is already in your basket" if sym else "That is not a ticker")))
    return _basket_response(request, db, user, sort=sort, part=part,
                            events=_toast(msg, "ok" if res["added"] else "err"))


@router.post("/basket/remove", response_class=HTMLResponse)
def basket_remove(request: Request, symbol: str = Form(""), sort: str = Form("added"), part: str = Form(""),
                  user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Deletes the member's row only; the shared data stays for everyone else."""
    sym = _clean_symbol(symbol)
    (db.query(OptionBasket).filter(OptionBasket.owner_key == _owner(user), OptionBasket.symbol == sym)
       .delete(synchronize_session=False))
    db.commit()
    return _basket_response(request, db, user, sort=sort, part=part,
                            events=_toast(f"{sym} removed from your basket.", "ok"))


@router.post("/basket/import")
def basket_import(payload: BasketImport, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Bulk add (a JSON body). Returns {added, skipped, over_cap, total}; the page then
    fires options:basket-changed so the basket and the list re-render."""
    src = payload.source if payload.source in IMPORT_SOURCES else "paste"
    if src == "paste":
        raw = payload.text.replace(",", " ").replace(";", " ").replace("\n", " ").split()
        syms = _clean_symbols(raw, cap=1000)
    elif src == "watchlist":
        syms = uwl.symbols(db, user)
    elif src == "ivscan_list":
        syms = _ivscan_universe(user)
    elif src == "ivscan_scan":
        syms = _ivscan_scan_symbols(db, user)
    elif src == "positions":
        syms = _open_trade_symbols(db, user)
    else:                                                    # scanner / screener: what the browser chose
        syms = _clean_symbols(payload.symbols, cap=1000)
    raw_n = len(payload.text.replace(",", " ").replace(";", " ").split()) if src == "paste" else len(syms)
    res = _add_symbols(db, user, syms, src, payload.note)
    if src == "paste":
        res["skipped"] += max(0, raw_n - len(syms))         # junk the cleaner dropped counts as skipped
    new = res.pop("new", [])                                 # the JSON shape stays {added, skipped, over_cap, total}
    resp = JSONResponse(res)
    return _trigger(resp, {"options:basket-changed": {"n": res["total"], "new": new},
                           **_toast(f"{res['added']} added, {res['skipped']} skipped"
                                    + (f", {res['over_cap']} over the {MAX_BASKET} cap" if res["over_cap"] else ""),
                                    "ok" if res["added"] else "info")})


@router.post("/refresh/{symbol}", response_class=HTMLResponse)
def refresh_symbol(request: Request, symbol: str, src: str = Form(""), user: User = Depends(require_user),
                   db: Session = Depends(get_db)):
    """"Refresh now": read one ticker of the member's basket from Massive, server-side
    (``opt_massive.ingest_symbol(kind="manual")``, the collector's own read - a few
    seconds), at most once per ticker per member per minute. Answers the refreshed
    basket row; ``HX-Trigger-After-Settle: options:refreshed`` makes the page re-screen
    the list (and reload an open trade of that ticker). A read that fails answers the
    plain reason (as the body and a toast) and swaps nothing. ``src=trade`` = pressed
    in the trade detail (the page then reloads the basket rows itself)."""
    sym = _clean_symbol(symbol)
    if not sym or not _in_own_basket(db, user, sym):
        return _refresh_fail(400, f"{sym or 'That ticker'} is not in your basket")
    wait = _refresh_slot(user.id, sym)
    if wait:
        return _refresh_fail(429, f"{sym} was just refreshed - try again in {wait} s", retry_after=wait)
    t0 = time.monotonic()
    client = None
    try:
        client = _massive_client()
        res = opt_massive.ingest_symbol(db, client, sym, kind="manual") or {}
    except massive.MassiveError as exc:
        db.rollback()
        kind = getattr(exc, "kind", None)
        if kind == "config":
            _refresh_free(user.id, sym)                  # nothing was asked of Massive
        status, why = _massive_problem(exc)
        log.warning("options refresh %s by user %s: Massive %s (HTTP %s)", sym, user.id, kind,
                    getattr(exc, "status", None))
        return _refresh_fail(status, f"{sym}: {why}", retry_after=60 if kind == "rate" else None)
    except Exception as exc:  # noqa: BLE001 - the member sees a plain reason, the log has the rest
        db.rollback()
        log.warning("options refresh %s by user %s: %s", sym, user.id, type(exc).__name__, exc_info=True)
        return _refresh_fail(500, f"{sym}: the refresh failed ({type(exc).__name__}) - try again in a minute")
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                pass
    stored = int(_num(res.get("stored")) or 0)
    secs = (time.monotonic() - t0)
    log.info("options refresh %s by user %s: %s contracts in %.1f s", sym, user.id, stored, secs)
    msg = (f"{sym} read from Massive: {stored:,} contracts ({secs:.1f} s) - the list is re-screened"
           if stored else f"Massive returned no option contracts for {sym}")
    it = _one_item(db, user, sym)
    if it is None:                                       # removed meanwhile: nothing to swap in
        resp = Response("", status_code=200, media_type="text/html")
        resp.headers["HX-Reswap"] = "none"
    else:
        resp = templates.TemplateResponse(request, "_opt_basket.html", {"user": user, "part": "row", "it": it})
    _trigger(resp, _toast(msg, "ok" if stored else "info"))
    return _trigger(resp, {"options:refreshed": {"symbol": sym, "stored": stored,
                                                 "src": "trade" if src == "trade" else "basket"}},
                    header="HX-Trigger-After-Settle")


# ────────────────────────────────── routes: rules ──────────────────────────────────

@router.get("/rules", response_class=HTMLResponse)
def rules(request: Request, strategy: str = "", user: User = Depends(require_user), db: Session = Depends(get_db)):
    """The rules panel for one strategy: shared fields + the strategy's own."""
    return templates.TemplateResponse(request, "_opt_rules.html", _rules_context(db, user, _strategy(strategy)))


@router.post("/rules", response_class=HTMLResponse)
async def rules_save(request: Request, strategy: str = "", user: User = Depends(require_user),
                     db: Session = Depends(get_db)):
    """Autosave of one (or more) fields. Answers the panel's message line plus an
    out-of-band error slot and changed-dot per field (the inputs are never re-rendered,
    so typing is never interrupted) and fires options:rules-changed so the list
    re-screens."""
    strategy = _strategy(strategy)
    form = await _form_dict(request)
    idx = _field_index(strategy)
    field_errs: dict[str, str] = {}
    posted: set[str] = set()
    clamped: dict[str, dict] = {}
    for key, raw in form.items():
        hit = idx.get(key)
        if hit is None:
            continue
        block, name, f = hit
        posted.add(f"{block}.{name}")
        val, err = opt_rules.parse(raw, f, name=name)
        if err:
            field_errs[f"{block}.{name}"] = err
            if f.kind in ("int", "num") and isinstance(val, (int, float)) and not isinstance(val, bool):
                # stored clamped: the page writes the value in force back into the box
                clamped[f"rf-{block}-{name}"] = {"sent": str(raw), "value": _input_text(val, f)}
    _, errors = opt_rules.write(db, user, strategy, form)
    band = [e for e in errors if e not in field_errs.values()]
    if band:
        msg, kind = "Saved, but: " + " ".join(band), "warn"
    elif field_errs:
        msg, kind = "Saved - a value was adjusted, see the field.", "warn"
    else:
        msg, kind = "Saved - the list below is updated.", "ok"
    ctx = _rules_context(db, user, strategy, msg=msg, msg_kind=kind, field_errs=field_errs,
                         errors=errors, part="msg", posted=posted)
    resp = templates.TemplateResponse(request, "_opt_rules.html", ctx)
    events: dict = {"options:rules-changed": {"strategy": strategy}}
    if clamped:
        events["options:rules-clamped"] = clamped
    return _trigger(resp, events)


@router.post("/rules/reset", response_class=HTMLResponse)
def rules_reset(request: Request, strategy: str = "", user: User = Depends(require_user),
                db: Session = Depends(get_db)):
    """Back to the house defaults for what the panel shows (shared + this strategy)."""
    strategy = _strategy(strategy)
    opt_rules.reset(db, user, strategy)
    ctx = _rules_context(db, user, strategy,
                         msg=f"Every rule for {opt_rules.LABELS[strategy]} (and the shared ones) is back to the house default.")
    return _rules_changed(templates.TemplateResponse(request, "_opt_rules.html", ctx), strategy)


# ────────────────────────────────── routes: results + trade ──────────────────────────────────

@router.get("/results", response_class=HTMLResponse)
def results(request: Request, strategy: str = "", sort: str = "score", dir: str = "desc",  # noqa: A002
            user: User = Depends(require_user), db: Session = Depends(get_db)):
    """The trades that pass every rule over the member's basket (opt_screen.screen),
    sorted on the server. The per-ticker passing counts ride along as the
    options:counts event the page paints into the basket."""
    strategy = _strategy(strategy)
    sort = sort if sort in RESULT_SORTS else "score"
    direction = "asc" if dir == "asc" else "desc"
    ctx = _results_context(db, user, strategy, sort, direction)
    resp = templates.TemplateResponse(request, "_opt_results.html", ctx)
    counts = {s: int((t or {}).get("passed") or 0) for s, t in ctx["tickers"].items()}
    # the screener's reason verbatim (e.g. "no trade passes: the last rule in the way is ..."),
    # capped only against a runaway string
    reasons = {s: str((t or {}).get("reason") or "")[:REASON_MAX] for s, t in ctx["tickers"].items()
               if not (t or {}).get("passed")}
    return _trigger(resp, {"options:counts": {"strategy": strategy, "counts": counts, "reasons": reasons,
                                              "summary": _summary(ctx["n_passed"], ctx["n_tickers"])}})


@router.get("/trade", response_class=HTMLResponse)
def trade(request: Request, strategy: str = "", id: str = "",  # noqa: A002
          user: User = Depends(require_user), db: Session = Depends(get_db)):
    """One trade, re-derived from the current data: the legs with each one's price and
    where it comes from (bid/ask mid, or a model price from its IV), its data time and
    source, the payoff chart, breakevens, max profit / loss, POP, the "Refresh now"
    control and the reminder to check the live price in TWS."""
    strategy = _strategy(strategy)
    d, chain, und, sym = _detail(db, user, strategy, id)
    ctx: dict[str, Any] = {"user": user, "strategy": strategy, "label": opt_rules.LABELS[strategy],
                           "cid": id, "sym": sym, "d": d}
    if d is None:
        ctx["gone"] = ("This trade is no longer in the data (a leg expired, or its data is gone). "
                       "The list refreshes as new data arrives.")
        return templates.TemplateResponse(request, "_opt_trade.html", ctx)
    c = d["candidate"]
    now = _utcnow()
    rth = clock.us_session_open()
    legs = [_leg_view(l, now) for l in c.get("legs") or []]
    ctx.update({
        "c": c, "v": _row_view(strategy, c, rth), "po": d.get("payoff") or {}, "fails": d.get("fails"),
        "legs": legs, "any_quotes": any(l["quoted"] for l in legs),
        "modelled": any(not l["quoted"] for l in legs),
        "pane_url": _payoff_url(strategy, id),
        "metrics": c.get("metrics") or {}, "family": opt_rules.FAMILY.get(strategy),
        "breakevens_text": ", ".join(_k(b) for b in c.get("breakevens") or []) or "-",
        "earnings": (und or {}).get("earnings_date"), "spot": _num(chain.get("spot")),
        "in_basket": _in_own_basket(db, user, sym),
    })
    return templates.TemplateResponse(request, "_opt_trade.html", ctx)


@router.get("/payoff", response_class=HTMLResponse)
def payoff_pane(request: Request, strategy: str = "", id: str = "", units: str = "$",  # noqa: A002
                user: User = Depends(require_user), db: Session = Depends(get_db)):
    """The payoff pane alone (_payoff_chart.html's $ | R toggle re-requests this)."""
    strategy = _strategy(strategy)
    d, chain, und, sym = _detail(db, user, strategy, id)
    if d is None:
        return HTMLResponse('<div class="po-pane min-h-[300px] flex items-center justify-center text-[11px] '
                            'text-amber-300">This trade is no longer in the data.</div>')
    po = d.get("payoff") or {}
    if str(units).upper() == "R":
        c = d["candidate"]
        try:
            po = payoff.build(c["legs"], strategy=strategy, spot=_num(chain.get("spot")) or c["underlying"]["spot"],
                              atr=_num((und or {}).get("atr14")), as_of=clock.et_date(),
                              sigma_fallback=_sigma_fallback(und), units="R", symbol=sym)
        except Exception as exc:  # noqa: BLE001 - the $ pane is still right
            log.warning("options payoff R %s: %s", id, exc)
    return templates.TemplateResponse(request, "_payoff_chart.html",
                                      {"po": po, "pane_url": _payoff_url(strategy, id), "user": user})


# ────────────────────────────────── routes: strip + help ──────────────────────────────────

@router.get("/status", response_class=HTMLResponse)
def status(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """The status strip (polled every 60 s): the Hermes collector line, the plain note
    that prices are estimated from IV when the plan has no bid/ask, and - for an
    administrator only - a missing Massive key."""
    return templates.TemplateResponse(request, "_opt_status.html", {
        "user": user, "col": _collector_view(db), "quotes": opt_rules.quotes_available(),
        "no_quotes_note": NO_QUOTES_NOTE,
        "key_missing": bool(getattr(user, "is_admin", False)) and massive.api_key() is None,
        "key_missing_text": KEY_MISSING})


@router.get("/help", response_class=HTMLResponse)
def data_help(request: Request, user: User = Depends(require_user)):
    """The "?" panel: where the data comes from (Massive Options Starter, 15 min
    delayed, refreshed by Hermes in the session), how prices are estimated, and that
    the entry is made in TWS."""
    return templates.TemplateResponse(request, "_opt_help.html", {
        "user": user, "quotes": opt_rules.quotes_available(), "cycle_min": _cycle_min(),
        "refresh_every_s": REFRESH_EVERY_S, "fresh_min": FRESH_MIN})
