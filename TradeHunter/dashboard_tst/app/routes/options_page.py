"""Options v2 - browse by rules (OPTIONS_V2_DESIGN.md §9, with §5.4 and §6).

One page: a status strip (the member's own IBKR connector pill, the Hermes collector
line), the member's basket (left), a strategy dropdown with the rules panel ALWAYS
visible, and the list of trades that pass every rule over the whole basket. A click on
a trade opens its legs (each with its own as_of / source / market data type) and the
payoff chart.

Data: every option and stock figure is read from the shared IBKR pool in
``services/opt_store`` (the Hermes collector and members' connectors write it); the
earnings date is the one free-source figure. The web app never connects to IBKR: the
browser relays the member's own connector on 127.0.0.1 through ``/options/data/*``,
which validates (§2.4), rate-limits and stores it as ``source="member"`` so every
other member benefits.

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
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field as PField
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as _models
from ..db import get_db
from ..models import OptionBasket, OptQuote, User
from ..security import require_user
from ..services import clock, opt_connector_pkg, opt_rules, opt_screen, opt_store, payoff
from ..services import user_watchlist as uwl
from ..services.opt_constants import MAX_BASKET
from . import options as legacy_options            # BRIDGE_PORT only
from .ivscan import DEFAULT_CRITERIA, UNIVERSE_PREF, _clean_symbols

log = logging.getLogger(__name__)

router = APIRouter(prefix="/options", tags=["options-page"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

BRIDGE_PORT = legacy_options.BRIDGE_PORT            # 9224: the member's connector on 127.0.0.1

SOURCES = ("typed", "paste", "watchlist", "ivscan_list", "ivscan_scan", "scanner",
           "screener", "sector", "positions", "system")
IMPORT_SOURCES = ("paste", "watchlist", "ivscan_list", "ivscan_scan", "scanner",
                  "screener", "positions")
BASKET_SORTS = ("added", "symbol", "fresh", "iv")

FRESH_MIN = 60              # a quote younger than this is fresh; older is amber during RTH (§9)
STALE_HEARTBEAT_MIN = 5     # the collector line goes stale after this many minutes without a heartbeat (§4.3)
NEXT_WAIT_S = 30            # /data/next with nothing to read: the page asks again after this
HISTORY_MAX_POINTS = 800    # a member's history post: at most this many bars / IV points each
MEMBER_HISTORY_KEEP_D = 7   # a member's history on file younger than this is not re-filed by another member
MAX_BODY_BYTES = 5 * 1024 * 1024   # a contribution body over this is refused before it is parsed
FAILED_ERROR_MAX = 300      # /data/failed keeps this many characters of the connector's error
REASON_MAX = 300            # a ticker's no-trade reason in options:counts (shown verbatim; a cap, not a cut)
CHAIN_AGE_PAD_H = 96        # the results load quotes up to max_age_h + this (WALL clock: a weekend never
                            # empties the chain; the screener applies the exact market-time age)
REFRESH_SIDE_MIN = 4        # "Refresh these legs live": at least this many strikes each side of spot
REFRESH_SIDE_MAX = 40       # ... and at most this many (th_ibkr.plan's max_side)
REFRESH_SIDE_PAD = 2        # ... beyond the farthest leg
REFRESH_SIGMA_K_MIN = 0.05  # the refresh window's sigma_k bounds (the connector accepts 0 < k <= 10)
REFRESH_SIGMA_K_MAX = 10.0
REFRESH_DEFAULT_IV = 0.40   # th_ibkr.plan's own default when the stock's IV30 is unknown

MDT_WORDS = {"live": "live", "frozen": "frozen", "delayed": "delayed",
             "delayed_frozen": "delayed frozen"}
_ON = {"on", "true", "1", "yes", "y", "t"}

# the results table's server-side sort keys (default: the screener's own score order)
RESULT_SORTS = ("score", "symbol", "expiry", "net", "max_profit", "max_loss", "ror", "pop",
                "delta", "iv_rank", "liquidity", "age")


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


def _trigger(resp: Response, events: dict) -> Response:
    resp.headers["HX-Trigger"] = json.dumps(events)        # ASCII-escaped: header-safe
    return resp


def _toast(msg: str, kind: str = "info") -> dict:
    return {"options:toast": {"kind": kind, "msg": msg}}


def _jerr(status: int, error: str, **extra) -> JSONResponse:
    return JSONResponse({"ok": False, "error": error, **extra}, status_code=status)


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
    """'Hermes' / the contributing member's display name."""
    if source == "member":
        return name or (f"member #{uid}" if uid else "a member")
    if source == "hermes":
        return "Hermes"
    return str(source or "unknown")


def _source_words(source, name=None, uid=None, mdt=None) -> str:
    """'Hermes live' / 'Kui (live)' - who quoted it and on what market data type."""
    m = MDT_WORDS.get(mdt or "", mdt or "unknown")
    who = _who(source, name, uid)
    return f"{who} ({m})" if source == "member" else f"{who} {m}"


def _sources_words(sources) -> str:
    """opt_screen's ``data.sources`` ('hermes·live', 'member:Kui·live') in words."""
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
    symbol simply has no data until Hermes or a member's connector reads it."""
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


def _basket_context(db: Session, user: User, *, sort: str = "added") -> dict:
    """Every basket row with its freshness (opt_store.freshness: the newest refresh-log
    row) and the stored IV rank - one query each, never a market call."""
    rows = _basket_rows(db, user)
    syms = [r.symbol for r in rows]
    fr = opt_store.freshness(db, syms) if syms else {}
    unds = opt_store.underlyings(db, syms) if syms else {}
    rth = clock.us_session_open()
    items = []
    for r in rows:
        f = fr.get(r.symbol)
        u = unds.get(r.symbol) or {}
        src = _source_words(f.get("source"), f.get("source_name"), f.get("source_user_id"), f.get("mdt")) if f else None
        items.append({"row": r, "sym": r.symbol, "fresh": f, "waiting": f is None,
                      "dot": _dot(f, rth), "age_text": _age_text(f.get("age_min")) if f else None,
                      "src_text": src, "iv_rank": _num(u.get("iv_rank")),
                      "history_done": bool(u.get("history_done")),
                      "tip": (f"{r.symbol}: waiting for first read - a member's IBKR connector reads it "
                              f"while its pill is green; Hermes reads new tickers after 20:10 ET and at the "
                              f"weekend" if f is None else
                              f"{r.symbol}: last read {_age_text(f.get('age_min'))} ago by {src}, "
                              f"{f.get('n') or 0} contracts")})
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
            "criteria": DEFAULT_CRITERIA}


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


def _rules_context(db: Session, user: User, strategy: str, *, msg: str = "", msg_kind: str = "ok",
                   field_errs: dict | None = None, errors: list | None = None, part: str = "panel",
                   posted: set | None = None) -> dict:
    """The panel: the shared block, then the strategy's own block - every field with its
    label, help (tooltip), unit, bounds, current value and whether it differs from the
    house default. ``errors`` (all of them) feed the message line; ``field_errs`` the
    slot under each field. ``posted`` (a save): the 'block.name' keys of the fields that
    request carried - only their error slots and changed-dots are re-sent, so a warning
    on another field (a clamped value) stays until THAT field is saved again."""
    prefs = opt_rules.read(db, user)
    view = opt_rules.for_strategy(prefs, strategy)
    field_errs = field_errs or {}
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
    tip = f"{word.capitalize()} {_money(abs(net) * 100)} per contract at the mid prices"
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
               + "; widest bid/ask: " + (f"${sp:.2f}" if sp is not None else "unknown")
               + (f" ({spp:.0f}% of its mid)" if spp is not None else "")
               + "; least traded today: " + (f"{vmin:,.0f}" if vmin is not None else "not reported") + ".")
    srcs = _sources_words(data.get("sources"))
    oldest = data.get("as_of_oldest")
    oldest_dt = _naive(oldest)
    # age_min is the MARKET-time age (the column, and what max_age_h filters on: the
    # clock stops while the market is closed); wall_age_min is the plain clock age
    wall = _num(data.get("wall_age_min"))
    data_tip = (f"Market-time age {_age_phrase(age)} (the age clock stops while the market is closed, so a "
                f"quote from the last close stays current until the next open) · on the clock, read "
                f"{_age_phrase(wall if wall is not None else age)} ago"
                + (f" ({oldest_dt.strftime('%Y-%m-%d %H:%M')} UTC)" if oldest_dt else "")
                + f" by {srcs}." + (" Legs come from more than one source." if data.get("mixed") else ""))
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
        "liq_text": (f"OI {oi:,.0f}" if oi is not None else "OI ?") + (f" · ${sp:.2f}" if sp is not None else ""),
        "liq_tip": liq_tip,
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
        # a contribution for any basket ticker re-screens the list (the page debounces it)
        "syms_watch": sorted(syms),
        "no_trade": sorted(((s, (t or {}).get("reason") or "no trade passes") for s, t in tickers.items()
                            if not (t or {}).get("passed")), key=lambda p: p[0]),
        "rules_note": (opt_rules.band_errors(prefs, strategy) or [None])[0],
    })
    return ctx


# ────────────────────────────────── trade detail ──────────────────────────────────

def _leg_view(l: dict, now: _dt.datetime) -> dict:
    iv = _num(l.get("iv"))
    return {**l, "side_word": "Sell" if l.get("side") == "sell" else "Buy",
            "right_word": "call" if l.get("right") == "C" else "put",
            "strike_text": _k(l.get("strike")), "expiry_text": _exp_label(l.get("expiry")),
            "iv_text": "-" if iv is None else f"{iv * 100:.1f}%",
            "age_text": _age_text(_age_min(l.get("as_of"), now)),
            "as_of_dt": _naive(l.get("as_of")),
            "src_text": _who(l.get("source"), l.get("source_name"), l.get("source_user_id")),
            "mdt_text": MDT_WORDS.get(l.get("mdt") or "", l.get("mdt") or "unknown")}


def _refresh_spec(c: dict, chain: dict, und: dict | None) -> dict:
    """The narrow fetch window behind "Refresh these legs live" (th_ibkr.plan's spec):
    only the trade's expiries; on each side of the spot at least as many strikes as
    the stored chain lists out to the farthest leg (+2), and a sigma_k whose expected
    move reaches that leg's distance (x1.15) at the nearest expiry - so a leg is
    covered even when IBKR lists more strikes than were stored. Ticker-relative: the
    distance is measured in the stock's own IV."""
    legs = c.get("legs") or []
    exps = sorted({l.get("expiry") for l in legs if l.get("expiry")})
    spot = _num(chain.get("spot")) or _num((und or {}).get("spot")) or _num((c.get("underlying") or {}).get("spot"))
    iv30 = _num((und or {}).get("iv30"))
    iv_hint = round(iv30 / 100.0, 4) if iv30 else None
    need, dist = REFRESH_SIDE_MIN, 0.0
    by_exp = {e.get("expiry"): e for e in chain.get("expiries") or ()}
    for l in legs:
        e = by_exp.get(l.get("expiry")) or {}
        ks = sorted({_num(r.get("strike")) for r in (e.get("calls") or []) + (e.get("puts") or [])} - {None})
        k = _num(l.get("strike"))
        if spot is None or k is None:
            continue
        n = (sum(1 for x in ks if k <= x < spot) if k < spot else sum(1 for x in ks if spot <= x <= k))
        need = max(need, n + REFRESH_SIDE_PAD)
        dist = max(dist, abs(k - spot))
    dtes = [int(l["dte"]) for l in legs if isinstance(l.get("dte"), (int, float))]
    sigma_k = REFRESH_SIGMA_K_MIN
    if spot and dist > 0:
        move_1k = spot * (iv_hint or REFRESH_DEFAULT_IV) * math.sqrt(max(1, min(dtes) if dtes else 30) / 365.0)
        sigma_k = min(REFRESH_SIGMA_K_MAX, max(REFRESH_SIGMA_K_MIN, round(dist * 1.15 / move_1k, 3)))
    return {"symbol": c.get("symbol"), "spot": spot, "iv_hint": iv_hint, "expiries": exps,
            "sigma_k": sigma_k, "min_side": min(REFRESH_SIDE_MAX, need), "max_side": REFRESH_SIDE_MAX}


def _detail(db: Session, user: User, strategy: str, cid: str) -> tuple[dict | None, dict, dict | None, str]:
    """(opt_screen.detail result or None, chain, underlying, symbol) for a candidate id."""
    parsed = opt_screen.parse_id(cid)
    sym = _clean_symbol(parsed[0]) if parsed else ""
    if not sym:
        return None, {}, None, ""
    prefs = opt_rules.read(db, user)
    rules = opt_rules.for_strategy(prefs, strategy)
    # only the trade's own expiries are read (a day either side); no age cut - an
    # opened trade shows its legs however old their quotes are
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
    """The Hermes collector line of the strip (§4.3): state running / error / stale /
    stopped / none, the words, the tooltip and a tone."""
    now = now or _utcnow()
    st = opt_store.collector_status(db)
    if st is None:
        return {"state": "none", "tone": "slate", "text": "Hermes collector: no heartbeat yet",
                "tip": "The Hermes options collector has not reported yet. Until it runs, only members' "
                       "connectors fill the shared data."}
    age = _age_min(st.get("heartbeat"), now)
    gw = st.get("gateway") or "IB Gateway"
    detail = st.get("phase_detail") or ""
    if age is None or age > STALE_HEARTBEAT_MIN:
        return {"state": "stale", "tone": "amber",
                "text": f"Hermes collector: no heartbeat for {_age_text(age) if age is not None else 'a while'}",
                "tip": f"The collector on Hermes last reported {_age_text(age)} ago (state {st.get('state') or '?'}). "
                       f"It may have stopped - check the TST-Options-Collector task on Hermes."}
    if st.get("state") == "waiting":
        # The ingest supervisor keeps the Hermes Gateway down BY DESIGN (the weekday
        # 08:00-20:10 ET manual-trading blackout, its start-up, closed after the nightly
        # top-up): neutral, not an error - members' connectors carry the session.
        eod = st.get("last_eod_on")
        return {"state": "waiting", "tone": "slate", "text": "Hermes: waiting · Gateway off by design",
                "tip": (detail or "The Hermes Gateway is off by design.")
                       + (f" · last end-of-day pass {eod}" if eod else "")}
    if st.get("state") == "error" or st.get("gateway_ok") is False:
        last_ok = _naive(st.get("cycle_finished"))
        since = f" (last full pass {last_ok.strftime('%H:%M')} UTC)" if last_ok else ""
        return {"state": "error", "tone": "rose",
                "text": f"Hermes: gateway down{since}" if st.get("gateway_ok") is False else "Hermes: error",
                "tip": f"{gw}: {st.get('last_error') or detail or 'not reachable'}. The collector retries every minute."}
    if st.get("state") == "stopped":
        return {"state": "stopped", "tone": "slate", "text": "Hermes collector: stopped",
                "tip": detail or "The collector was stopped on Hermes."}
    words = {"starting": "starting", "history": "reading history", "cycle": "running",
             "eod": "end-of-day pass", "idle": "idle"}
    parts = [f"Hermes: {words.get(st.get('state') or '', st.get('state') or 'running')}"]
    if st.get("cycle_n"):
        parts.append(f"cycle {st['cycle_n']}")
    if st.get("symbols_total"):
        parts.append(f"{st.get('symbols_done') or 0}/{st['symbols_total']} tickers this pass")
    if st.get("mdt"):
        parts.append(f"{MDT_WORDS.get(st['mdt'], st['mdt'])} data")
    tip = (f"{gw} · heartbeat {_age_text(age)} ago" + (f" · {detail}" if detail else "")
           + (f" · last end-of-day pass {st['last_eod_on']}" if st.get("last_eod_on") else ""))
    return {"state": "running", "tone": "emerald", "text": " · ".join(parts), "tip": tip}


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
    """The basket fragment; ``part=rows`` = only the rows container (the page's
    refreshes after a contribution, a sort click), so the Add / Import footer keeps
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
    msg = (f"{sym} added - waiting for its first read (a member's IBKR connector, or Hermes after "
           f"20:10 ET / at the weekend)" if res["added"] else
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
    """One trade, re-derived from the current pool: the legs with each one's as_of /
    source / market data type, the payoff chart, breakevens, max profit / loss, POP and
    the "Refresh these legs live" window."""
    strategy = _strategy(strategy)
    d, chain, und, sym = _detail(db, user, strategy, id)
    ctx: dict[str, Any] = {"user": user, "strategy": strategy, "label": opt_rules.LABELS[strategy],
                           "cid": id, "sym": sym, "d": d}
    if d is None:
        ctx["gone"] = ("This trade is no longer in the data (a leg expired, or its quote is gone). "
                       "The list refreshes as new quotes arrive.")
        return templates.TemplateResponse(request, "_opt_trade.html", ctx)
    c = d["candidate"]
    now = _utcnow()
    rth = clock.us_session_open()
    ctx.update({
        "c": c, "v": _row_view(strategy, c, rth), "po": d.get("payoff") or {}, "fails": d.get("fails"),
        "legs": [_leg_view(l, now) for l in c.get("legs") or []],
        "pane_url": _payoff_url(strategy, id),
        "spec_json": json.dumps(_refresh_spec(c, chain, und)),
        "metrics": c.get("metrics") or {}, "family": opt_rules.FAMILY.get(strategy),
        "breakevens_text": ", ".join(_k(b) for b in c.get("breakevens") or []) or "-",
        "earnings": (und or {}).get("earnings_date"), "spot": _num(chain.get("spot")),
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


# ────────────────────────────────── routes: strip, connector ──────────────────────────────────

@router.get("/status", response_class=HTMLResponse)
def status(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """The Hermes collector line of the strip (polled every 60 s). The connector pill
    is the browser's own probe of 127.0.0.1 and is not part of this fragment."""
    return templates.TemplateResponse(request, "_opt_status.html",
                                      {"user": user, "col": _collector_view(db)})


@router.get("/connector", response_class=HTMLResponse)
def connector_help(request: Request, user: User = Depends(require_user)):
    """The connector help: download, install, set the port; what the pill colours mean."""
    return templates.TemplateResponse(request, "_opt_connector.html",
                                      {"user": user, "bridge_port": BRIDGE_PORT})


@router.get("/connector/download")
def connector_download(user: User = Depends(require_user)):
    """The connector as a zip, built in memory from bridge/ (opt_connector_pkg)."""
    try:
        data, name = opt_connector_pkg.build_zip()
    except FileNotFoundError as exc:
        log.error("connector download: %s", exc)
        return Response(f"The connector package is not available on this server: {exc}",
                        status_code=500, media_type="text/plain")
    return Response(content=data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"',
                             "Cache-Control": "no-store"})


# ────────────────────────────────── routes: member contributions (§6) ──────────────────────────────────

def _held(db: Session, sym: str) -> bool:
    return (db.query(OptionBasket.id)
              .filter(OptionBasket.symbol == sym, OptionBasket.active.is_(True)).first()) is not None


def _in_own_basket(db: Session, user: User, sym: str) -> bool:
    return (db.query(OptionBasket.id)
              .filter(OptionBasket.owner_key == _owner(user), OptionBasket.symbol == sym,
                      OptionBasket.active.is_(True)).first()) is not None


class _Body(NamedTuple):
    """A member's JSON body, read by ``_json_body``: ``data`` or (``status``, ``error``)."""
    data: Any = None
    status: int = 200
    error: str | None = None


async def _json_body(request: Request, user: User = Depends(require_user)) -> _Body:
    """The JSON body of a contribution, read AFTER the sign-in check (this depends on
    require_user; the router's menu gate runs before both) and refused unread when it
    is over MAX_BODY_BYTES - by Content-Length, or while streaming a body without one.
    Parsed off the event loop."""
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            n = int(cl)
        except ValueError:
            return _Body(None, 400, "bad Content-Length")
        if n > MAX_BODY_BYTES:
            return _Body(None, 413, f"the body is over {MAX_BODY_BYTES // (1024 * 1024)} MB")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return _Body(None, 413, f"the body is over {MAX_BODY_BYTES // (1024 * 1024)} MB")
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw.strip():
        return _Body(None)
    try:
        return _Body(await run_in_threadpool(json.loads, raw))
    except ValueError:
        return _Body(None, 400, "the body is not JSON")


@router.get("/data/next")
def data_next(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """The next symbol this member's connector should read (stalest first, leased) with
    its fetch window - a CHUNK of the chain (a few expiries, stalest first) so one read
    fits the connector's time limit - or {wait: seconds}."""
    nxt = opt_store.next_for_member(db, user)
    if not nxt:
        return {"wait": NEXT_WAIT_S}
    hd = nxt.get("history_done")
    if hd is None:
        hd = (opt_store.underlying(db, nxt["symbol"]) or {}).get("history_done")
    return {"symbol": nxt["symbol"], "spec": nxt["spec"], "history_done": bool(hd)}


@router.post("/data/failed")
def data_failed(body: _Body = Depends(_json_body), user: User = Depends(require_user),
                db: Session = Depends(get_db)):
    """The page's connector could not read a symbol (an error, a timeout, an empty
    answer, or data the server refused): its lease is released and the symbol backs off
    for members' reads (10 min, doubling, at most 2 h; the next good contribution clears
    it), so one ticker that cannot be read never holds the loop. Only for a ticker in
    the member's own basket - the loop reads nothing else."""
    if body.error:
        return _jerr(body.status, body.error)
    payload = body.data
    if not isinstance(payload, dict):
        return _jerr(400, "the report must be a JSON object")
    sym = _clean_symbol(str(payload.get("symbol") or ""))
    if not sym:
        return _jerr(400, "symbol missing or not valid")
    if not _in_own_basket(db, user, sym):
        return _jerr(400, f"{sym} is not in your basket")
    why = opt_store.check_rate(user.id, sym, bucket="failed")
    if why:
        return _jerr(429, why)
    error = " ".join(str(payload.get("error") or "the read failed").split())[:FAILED_ERROR_MAX]
    try:
        backoff = opt_store.report_failure(sym, user.id, error, db=db)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        log.warning("options read failure %s by user %s: %s", sym, user.id, exc, exc_info=True)
        return _jerr(500, "the failure could not be recorded")
    log.info("options read failure %s by user %s (back-off %s s): %s", sym, user.id, backoff, error)
    return {"ok": True, "symbol": sym, "backoff_s": backoff}


@router.post("/data/contribute")
def data_contribute(body: _Body = Depends(_json_body), user: User = Depends(require_user),
                    db: Session = Depends(get_db)):
    """A chain read by the member's own connector (§6): validated (§2.4), rate-limited,
    stored as source 'member' with the member's id and the SERVER's receive time. The
    background loop's post (not a trade refresh) always frees this member's own lease;
    a refused one (400) of a ticker in the member's basket also counts as a failed read
    (opt_store.report_failure: the ticker backs off; ``backoff_s`` in the answer)."""
    if body.error:
        return _jerr(body.status, body.error)
    payload = body.data
    if not isinstance(payload, dict):
        return _jerr(400, "the contribution must be a JSON object")
    kind = "trade" if payload.get("kind") == "trade" else "member"
    clean, err, dropped = opt_store.validate_contribution(db, payload, user_id=user.id)
    if clean is None:
        err = err or "the contribution was rejected"
        posted = _clean_symbol(str(payload.get("symbol") or ""))
        extra: dict = {"dropped": dropped}
        if kind != "trade" and posted and _in_own_basket(db, user, posted):
            # the background loop's read was refused: exactly a failed read - this
            # member's lease goes and the ticker backs off, so the loop moves on (the
            # page sees backoff_s and does not report it a second time)
            try:
                extra["backoff_s"] = opt_store.report_failure(posted, user.id, f"refused: {err}", db=db)
            except Exception as exc:  # noqa: BLE001 - the refusal itself still answers
                db.rollback()
                opt_store.release_lease(posted, user.id)
                log.warning("options refused contribution %s by user %s: %s", posted, user.id, exc)
        return _jerr(400, err, **extra)
    sym = clean["symbol"]
    why = opt_store.check_rate(user.id, sym, bucket="trade" if kind == "trade" else "chain")
    if why:
        if kind != "trade":
            opt_store.release_lease(sym, user.id)       # not stored: free this member's own lease
        return _jerr(429, why)
    now = _utcnow()
    try:
        res = opt_store.upsert_quotes(db, sym, clean["rows"], source="member", mdt=clean["mdt"],
                                      user_id=user.id, as_of=now, kind=kind, spot=clean["spot"])
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        log.warning("options contribution %s by user %s: %s", sym, user.id, exc, exc_info=True)
        return _jerr(500, "the contribution could not be stored")
    finally:
        if kind != "trade":
            opt_store.release_lease(sym, user.id)       # only this member's own lease
    log.info("options contribution %s by user %s: %s stored, %s older, %s dropped, mdt %s, connector %s, "
             "client clock %s", sym, user.id, res.get("stored"), res.get("skipped_older"), dropped,
             clean["mdt"], str(payload.get("connector_version") or "?")[:16],
             str(payload.get("client_as_of") or "?")[:40])
    return {"ok": True, "symbol": sym, "stored": res.get("stored", 0), "skipped_older": res.get("skipped_older", 0),
            "dropped": dropped, "as_of": now.isoformat(timespec="seconds") + "Z"}


@router.post("/data/contribute_history")
def data_contribute_history(body: _Body = Depends(_json_body), user: User = Depends(require_user),
                            db: Session = Depends(get_db)):
    """A year of daily bars + IBKR's daily 30-day IV from the member's connector, for a
    symbol Hermes has not pulled history for yet: checked by opt_store.validate_history
    (bars required, weekday dates only, every close inside the chain contributions' spot
    band, IV 0.1-1000), filed into opt_underlying_daily (source 'member') and the stock
    statistics recomputed. A no-op once history is on file - Hermes's, or a member's
    from the last 7 days (so one member cannot overwrite another's; Hermes's own pull
    replaces member rows)."""
    if body.error:
        return _jerr(body.status, body.error)
    payload = body.data
    if not isinstance(payload, dict):
        return _jerr(400, "the history must be a JSON object")
    sym = _clean_symbol(str(payload.get("symbol") or ""))
    if not sym:
        return _jerr(400, "symbol missing or not valid")
    if not _held(db, sym):
        return _jerr(400, f"{sym} is not in any member's basket")
    bars, ivs = payload.get("bars"), payload.get("iv_series")
    if not isinstance(bars, list) or not isinstance(ivs or [], list):
        return _jerr(400, "bars and iv_series must be lists")
    if len(bars) > HISTORY_MAX_POINTS or len(ivs or []) > HISTORY_MAX_POINTS:
        return _jerr(400, f"at most {HISTORY_MAX_POINTS} points each")
    und = opt_store.underlying(db, sym) or {}
    if und.get("history_done"):
        return {"ok": True, "symbol": sym, "stored": 0, "note": "history already on file"}
    filed = _age_min(und.get("bars_as_of"))
    if filed is not None and filed < MEMBER_HISTORY_KEEP_D * 1440:
        return {"ok": True, "symbol": sym, "stored": 0,
                "note": "a member's history is already on file; Hermes replaces it with its own pull"}
    clean, err = opt_store.validate_history(db, payload, user_id=user.id)
    if clean is None:
        return _jerr(400, err or "the history was rejected")
    why = opt_store.check_rate(user.id, sym, bucket="history")
    if why:
        return _jerr(429, why)
    try:
        n = opt_store.upsert_daily(db, sym, clean.get("bars") or [], clean.get("iv_series") or [],
                                   source="member", user_id=user.id)
        u = opt_store.recompute_underlying(db, sym) if n else (opt_store.underlying(db, sym) or {})
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        log.warning("options history %s by user %s: %s", sym, user.id, exc, exc_info=True)
        return _jerr(500, "the history could not be stored")
    return {"ok": True, "symbol": sym, "stored": n, "iv_rank": u.get("iv_rank"), "iv_n": u.get("iv_n"),
            "atr14": u.get("atr14")}
