"""Options - one page: basket (left), ticker card (centre), My rules (bottom).

Read path = DB only (option_signal through option_store.card_for / basket_rows_for,
option_chain_snapshot, iv_daily): nothing on this page waits on a market call.
On-demand Refresh (Cboe, ~1-2 s) and Live (the member's own IBKR bridge, posted from
the browser and graded in-request, never stored) are explicit buttons.

Replaces IV Rank (/ivscan), Spread (/spreads) and Positions (/portfolio); those
routers stay registered (menus.HIDDEN_KEYS) until a later release. This router is
included BEFORE routes/options.py in main.py so its fixed paths win over that
module's /{symbol} catch-all; the legacy POST /options/track is not touched (the new
one is POST /options/track-idea).

Nothing here places, modifies or cancels an order (DESIGN.md security posture).

Engines that have not landed on a checkout (option_words, option_sizing, payoff,
order_ticket, strike_picker, option_nightly, option_exits, telegram) are imported
lazily through ``_svc``; every route still answers, says what is missing in plain
words, and never invents a number in their place. The member strings those modules
own are repeated at the bottom of this file ONLY as the fallback for a checkout
without them (``_W``); once ``option_words`` exists its text wins.
"""
from __future__ import annotations

import datetime as _dt
import importlib
import json
import logging
import secrets
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (IVScanItem, OptionBasket, OptionIdeaPush, OptionTrade,
                      OptionTradeCheck, SpreadCandidate, User, UserOptionPrefs, _utcnow)
from ..security import require_user
from ..services import clock, job_runs, option_prefs, option_store, strategy_rules
from ..services import trade_prefs as tp
from ..services import user_watchlist as uwl
from ..services.opt_constants import (BRIDGE_MIN_VERSION, CHAIN_STRIKES_EACH_SIDE,
                                      LEVEL_PAD_ATR, MAX_BASKET)
from . import options as legacy_options            # BRIDGE_PORT / BRIDGE_SETUP_PATH, nothing else
from .ivscan import DEFAULT_CRITERIA, UNIVERSE_PREF, _clean_symbols
from .sector import _chart_ctx

log = logging.getLogger(__name__)

router = APIRouter(prefix="/options", tags=["options-page"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

BRIDGE_PORT = legacy_options.BRIDGE_PORT            # 9224, one constant
BRIDGE_SETUP_PATH = legacy_options.BRIDGE_SETUP_PATH
REFRESH_COOLDOWN_S = 60     # per (member, ticker); never blocks the ticket's first "Refresh first"
FRESH_HOURS = 20            # the per-ticker age dot: younger than this = emerald
STALE_HOURS = 72            # ... older than this = rose
SEEN_PREF = "options_seen_at"       # user.prefs key the badge's ideas_new counts against
CODE_TTL_MIN = 10           # the Telegram handshake code lives this long

SOURCES = ("typed", "paste", "watchlist", "ivscan_list", "ivscan_scan", "scanner",
           "screener", "sector", "positions", "system")
IMPORT_SOURCES = ("paste", "watchlist", "ivscan_list", "ivscan_scan", "scanner",
                  "screener", "positions")
SORTS = ("idea", "iv", "trend", "added")
IDEA_WORDS = {"bull_put": "sell put", "bear_call": "sell call", "buy_call": "buy call",
              "buy_put": "buy put", "bull_call": "call sprd", "bear_put": "put sprd",
              "leaps_call": "LEAPS", "iron_condor": "condor", "calendar": "calendar",
              "diagonal_call": "diagonal"}
DEBIT_FAMILIES = ("debit_vertical", "long", "leaps")
LEVEL_FAMILIES = ("credit_vertical", "condor", "time")
TAB_OF_FAMILY = {"credit_vertical": "credit", "debit_vertical": "debit", "long": "debit",
                 "leaps": "debit", "condor": "condor", "time": "time"}

_PKG = __name__.rsplit(".", 2)[0]       # "app"
_cooldown: dict[tuple[int, str], float] = {}

# First-time reads in flight (v4.131; user, 2026-10-06: a freshly added ticker gets ALL
# its data at once - IV history, today's chain, the engines). Per process, like
# _cooldown, under one lock (the reading thread and the request handlers both touch it):
#   _first_reads        symbol -> (monotonic start, owner user id)   while the read runs
#   _first_reads_done   owner user id -> symbols finished, not yet announced to THAT
#                       member's basket (another member's tab must not drain them)
#   _first_reads_failed symbol -> why the last first read failed (the card says so)
_reg_lock = threading.Lock()
_first_reads: dict[str, tuple[float, int | None]] = {}
_first_reads_done: dict[int | None, set[str]] = {}
_first_reads_failed: dict[str, str] = {}
FIRST_READ_MAX_S = 20 * 60          # a thread that vanished must not spin a card forever


def _reading(sym: str) -> bool:
    with _reg_lock:
        t = _first_reads.get(sym)
        if t is None:
            return False
        if time.monotonic() - t[0] > FIRST_READ_MAX_S:
            _first_reads.pop(sym, None)
            return False
        return True


def _reading_symbols(user_id: int | None = None) -> list[str]:
    """Symbols being read - all of them, or only those a given member added."""
    now = time.monotonic()
    with _reg_lock:
        return sorted(s for s, (t, owner) in _first_reads.items()
                      if now - t <= FIRST_READ_MAX_S and (user_id is None or owner == user_id))


def _mark_reading(syms: list[str], user_id: int | None, started: float) -> None:
    with _reg_lock:
        for s in syms:
            _first_reads[s] = (started, user_id)
            _first_reads_failed.pop(s, None)


def _mark_done(sym: str, user_id: int | None, err: str | None = None, started: float | None = None) -> None:
    """The read of ``sym`` finished (``err`` when it failed). ``started`` guards a stale
    thread from clearing a NEWER read's marker."""
    with _reg_lock:
        cur = _first_reads.get(sym)
        if cur is not None and (started is None or cur[0] == started):
            _first_reads.pop(sym, None)
        _first_reads_done.setdefault(user_id, set()).add(sym)
        if err:
            _first_reads_failed[sym] = str(err)[:300]
        else:
            _first_reads_failed.pop(sym, None)


def _take_done(user_id: int | None) -> set[str]:
    with _reg_lock:
        return _first_reads_done.pop(user_id, set())


def _pop_done(sym: str, user_id: int | None) -> bool:
    with _reg_lock:
        s = _first_reads_done.get(user_id)
        if s and sym in s:
            s.discard(sym)
            return True
        return False


def _read_error(sym: str) -> str | None:
    with _reg_lock:
        return _first_reads_failed.get(sym)


def _clear_read_error(sym: str) -> None:
    with _reg_lock:
        _first_reads_failed.pop(sym, None)


class LiveIn(BaseModel):
    chain: dict = Field(default_factory=dict)   # bridge /chain response, untrusted
    iv: dict = Field(default_factory=dict)      # bridge /iv response (+ series when the bridge is >= 1.6)
    nlv: float | None = None                    # /account net_liquidation - sizes THIS request only
    diag: dict | None = None                    # what the browser saw when the loopback fetch failed


class BasketImport(BaseModel):
    source: str = "paste"            # paste | watchlist | ivscan_list | ivscan_scan | scanner | screener | positions
    text: str = ""                   # paste: commas / spaces / newlines
    symbols: list[str] = Field(default_factory=list)   # scanner / screener: what the browser chose
    note: str = ""


# ────────────────────────────────── small helpers ──────────────────────────────────

def _svc(name: str):
    """A sibling service module, or None when it has not landed on this checkout."""
    try:
        return importlib.import_module(f"{_PKG}.services.{name}")
    except ImportError:
        return None
    except Exception as exc:  # noqa: BLE001 - a broken module must not take the page down
        log.warning("options page: %s failed to import: %s", name, exc)
        return None


def _clean_symbol(symbol: str) -> str:
    """The single-ticker form of ivscan._clean_symbols."""
    out = _clean_symbols([symbol or ""], cap=1)
    return out[0] if out else ""


def _owner(user: User) -> str:
    return f"u{user.id}"


def _trigger(resp: Response, events: dict) -> Response:
    resp.headers["HX-Trigger"] = json.dumps(events)
    return resp


def _toast(msg: str, kind: str = "info") -> dict:
    return {"options:toast": {"kind": kind, "msg": msg}}


def _g(v, nd: int = 2) -> str:
    """A price as a member reads it: 340 / 336.2 / 327.9."""
    try:
        return f"{round(float(v), nd):g}"
    except (TypeError, ValueError):
        return "?"


def _money(v) -> str:
    try:
        return f"${round(float(v)):,.0f}"
    except (TypeError, ValueError):
        return "$?"


def _fmt_as_of(ts) -> str:
    """'Oct 2, 16:00 ET' from a naive-UTC feed stamp (the as_of convention)."""
    if ts is None:
        return "no data yet"
    if isinstance(ts, str):
        try:
            ts = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return ts
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_dt.timezone.utc)
    et = clock.et_now(ts)
    return f"{et.strftime('%b')} {et.day}, {et.strftime('%H:%M')} ET"


def _recommended(strategies) -> dict | None:
    return next((s for s in (strategies or []) if isinstance(s, dict) and s.get("fit") == "recommended"), None)


def _rule_allows_earnings(strategy: str, prefs: dict) -> bool:
    """defined_risk_only on a defined-risk strategy admits an earnings-inside trade."""
    rule = ((prefs or {}).get("shared") or {}).get("earnings_rule")
    return rule == "defined_risk_only" and option_prefs.defined_risk(strategy)


def _earnings_blocked(chosen: dict | None, strategy: str, prefs: dict) -> bool:
    """The one gate that hides strikes, refuses the ticket and refuses Track."""
    return bool(chosen and chosen.get("fit") == "rejected"
                and chosen.get("reason_key") == "earnings_inside"
                and not _rule_allows_earnings(strategy, prefs))


def _real_picks(lst) -> list[dict]:
    return [p for p in (lst or []) if isinstance(p, dict) and p.get("status", "ok") == "ok"]


def _degenerate_of(lst) -> dict | None:
    for p in (lst or []):
        if isinstance(p, dict) and p.get("status", "ok") != "ok":
            d = p.get("degenerate")
            if isinstance(d, dict):
                return d
            return {"reason_key": None, "text": None, "nearest": p if p.get("status") == "nearest" else None, "fix": None}
    return None


def _pick_label(pick: dict) -> str:
    """'Nov 20 · 330/320 put' - the legs cell of the picks table."""
    legs = [l for l in (pick.get("legs") or []) if isinstance(l, dict)]
    if not legs:
        return pick.get("label") or "-"
    word = {"P": "put", "C": "call"}
    expiries = sorted({l.get("expiry") for l in legs if l.get("expiry")})
    rights = {l.get("right") for l in legs}
    el = strategy_rules.expiry_label
    if len(expiries) == 1 and len(legs) == 2 and len(rights) == 1:
        short = next((l for l in legs if l.get("side") == "sell"), legs[0])
        long_ = next((l for l in legs if l.get("side") == "buy"), legs[-1])
        return f"{el(expiries[0])} · {_g(short.get('strike'))}/{_g(long_.get('strike'))} {word.get(short.get('right'), '')}".strip()
    if len(expiries) == 1 and len(legs) == 4:
        puts = sorted((l for l in legs if l.get("right") == "P"), key=lambda l: -float(l.get("strike") or 0))
        calls = sorted((l for l in legs if l.get("right") == "C"), key=lambda l: float(l.get("strike") or 0))
        return (f"{el(expiries[0])} · {'/'.join(_g(l.get('strike')) for l in puts)} put + "
                f"{'/'.join(_g(l.get('strike')) for l in calls)} call")
    if len(expiries) == 2:
        front = min(legs, key=lambda l: l.get("expiry") or "")
        back = max(legs, key=lambda l: l.get("expiry") or "")
        if front.get("strike") == back.get("strike"):
            return f"{el(front['expiry'])} → {el(back['expiry'])} · {_g(front.get('strike'))} {word.get(front.get('right'), '')}"
        return (f"{el(front['expiry'])} {_g(front.get('strike'))}{front.get('right', '')} → "
                f"{el(back['expiry'])} {_g(back.get('strike'))}{back.get('right', '')}")
    l = legs[0]
    return f"{el(l.get('expiry'))} · {_g(l.get('strike'))} {word.get(l.get('right'), '')}".strip()


def _chart_legs(pick: dict) -> list[dict]:
    out = []
    for l in (pick.get("legs") or []):
        if not isinstance(l, dict) or l.get("strike") is None:
            continue
        side = l.get("side") or "buy"
        out.append({"strike": l.get("strike"), "right": l.get("right"), "side": side,
                    "label": ("Short " if side == "sell" else "Long ") + _g(l.get("strike")) + str(l.get("right") or "")})
    return out


def _chart_spec(pick: dict) -> dict:
    """The strike overlay spec thChartSetStrikes takes (II.5 #33); the legacy
    {short, long, breakeven} keys ride along so today's _price_chart.html paints a
    two-leg spread until the generalised painter lands."""
    legs = _chart_legs(pick)
    bes = [b for b in (pick.get("breakevens") or []) if b is not None]
    spec = {"legs": legs, "breakevens": bes, "expiry": pick.get("expiry"), "label": _pick_label(pick)}
    sells = [l["strike"] for l in legs if l["side"] == "sell"]
    buys = [l["strike"] for l in legs if l["side"] == "buy"]
    if len(legs) == 2 and sells and buys:
        spec.update({"short": sells[0], "long": buys[0], "breakeven": bes[0] if bes else None})
    return spec


def _decorate_pick(pick: dict, family: str | None) -> dict:
    """Read-time presentation keys on a pick dict (never stored): label, chart_spec,
    the money words and the sizing line."""
    pick["label"] = _pick_label(pick)
    pick["chart_legs"] = _chart_legs(pick)
    pick["chart_spec"] = _chart_spec(pick)
    net = pick.get("net")
    words = pick.get("words") if isinstance(pick.get("words"), dict) else {}
    credit = family in ("credit_vertical", "condor")
    pick["is_credit"] = credit
    pick["collect_usd"] = round(-net * 100) if (credit and net is not None) else None
    pick["pay_usd"] = round(net * 100) if (not credit and net is not None) else None
    pick["collect_words"] = words.get("collect")
    pick["risk_words"] = words.get("risk")
    pick["pop_pct"] = round(float(pick["pop"]) * 100) if pick.get("pop") is not None else None
    pick["pop_words"] = _W.pop_words(pick.get("pop"), pick.get("pop_kind") or ("keep" if credit else "profit")) if pick.get("pop") is not None else None
    pick["pop_model_pct"] = round(float(pick["pop_model"]) * 100) if pick.get("pop_model") is not None else None
    ml = pick.get("max_loss")
    if credit and ml:
        pick["return_words"] = f"{round(-net * 100 / ml * 100)}% on risk" if net is not None else None
    elif ml and pick.get("max_profit") is not None and not credit:
        pick["return_words"] = f"{round(pick['max_profit'] / ml, 1)}x reward/cost"
    else:
        pick["return_words"] = None
    pick["sizing_line"] = _W.sizing_line(pick.get("sizing"))
    liq = pick.get("liquidity") if isinstance(pick.get("liquidity"), dict) else {}
    pick["liq_tier"] = liq.get("tier") or "unknown"
    pick["liq_word"] = {"clean": "clean", "limit": "at the limit", "wide": "wide", "thin": "thin",
                        "unknown": "unknown"}.get(pick["liq_tier"], "unknown")
    return pick


def _resize(picks: list[dict], nlv, nlv_source, prefs: dict) -> None:
    """Read-time sizing for a NLV that is not the stored one (the Live figure)."""
    sz = _svc("option_sizing")
    if sz is None:
        return
    prefs2 = dict(prefs)
    acct = dict(prefs.get("account") or {})
    acct.update({"nlv": nlv, "nlv_source": nlv_source})
    prefs2["account"] = acct
    for p in picks:
        if not isinstance(p, dict) or p.get("status", "ok") != "ok":
            continue
        try:
            p["sizing"] = sz.size(p, nlv, prefs2)
        except Exception as exc:  # noqa: BLE001
            log.warning("option_sizing.size failed: %s", exc)


# ────────────────────────────────── basket ──────────────────────────────────

def _basket_rows(db: Session, user: User) -> list[OptionBasket]:
    return (db.query(OptionBasket)
              .filter(OptionBasket.owner_key == _owner(user), OptionBasket.active.is_(True))
              .order_by(OptionBasket.pos, OptionBasket.symbol).all())


def _ivscan_universe(user: User) -> list[str]:
    raw = (getattr(user, "prefs", None) or {}).get(UNIVERSE_PREF)
    return _clean_symbols(raw if isinstance(raw, list) else [])


def _ivscan_scan_symbols(db: Session, user: User) -> list[str]:
    rows = (db.query(IVScanItem.symbol).filter(IVScanItem.user_id == user.id)
              .order_by(IVScanItem.pos).all())
    return [s for (s,) in rows]


def _open_trade_symbols(db: Session, user: User) -> list[str]:
    rows = (db.query(OptionTrade.symbol)
              .filter(OptionTrade.user_id == user.id, OptionTrade.status == "open")
              .distinct().all())
    return sorted(s for (s,) in rows)


def _add_symbols(db: Session, user: User, syms: list[str], source: str, note: str = "") -> dict:
    """The one basket writer: duplicates are skipped, the cap is enforced, the rest
    get pos = max + 1. Commits. Returns {added, skipped, over_cap, total}."""
    source = source if source in SOURCES else "typed"
    existing = {r.symbol: r for r in db.query(OptionBasket).filter(OptionBasket.owner_key == _owner(user)).all()}
    n_active = sum(1 for r in existing.values() if r.active)
    pos = max((r.pos for r in existing.values()), default=-1) + 1
    added = skipped = over_cap = 0
    new: list[str] = []                      # the symbols actually added, in order (the first-time read)
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


def _screener_suggestions(db: Session, user: User, exclude: set[str], prefs: dict) -> list[dict]:
    """Up to 8 of last night's spread_candidates not in the basket, passing the
    member's shared liquidity rules (D5.2). None when the scan table is empty."""
    latest = (db.query(SpreadCandidate.scan_on).order_by(SpreadCandidate.scan_on.desc()).first())
    if latest is None:
        return []
    shared = prefs.get("shared") or {}
    min_oi = shared.get("min_oi") or 0
    max_spread = shared.get("max_leg_spread") or 9e9
    rows = (db.query(SpreadCandidate).filter(SpreadCandidate.scan_on == latest[0])
              .order_by(SpreadCandidate.credit_pct.desc().nullslast()).limit(200).all())
    out, seen = [], set()
    for c in rows:
        if c.symbol in exclude or c.symbol in seen:
            continue
        if (c.short_oi or 0) < min_oi or (c.long_oi or 0) < min_oi:
            continue
        if None not in (c.short_bid, c.short_ask) and (c.short_ask - c.short_bid) > max_spread:
            continue
        if None not in (c.long_bid, c.long_ask) and (c.long_ask - c.long_bid) > max_spread:
            continue
        seen.add(c.symbol)
        out.append({"symbol": c.symbol, "credit_pct": c.credit_pct, "iv_pct": c.iv_pct,
                    "expiry": c.expiry, "short": c.short_strike, "long": c.long_strike})
        if len(out) >= 8:
            break
    return out


def _basket_context(db: Session, user: User, *, sort: str = "idea", selected: str = "",
                    compact: bool = False) -> dict:
    """Every basket row with the three numbers the column shows (trend arrow, IV
    rank, the recommended idea) read from option_signal through ONE batched query -
    never a market call, never a per-row lookup. pick_state comes from
    basket_rows_for and is never recomputed here."""
    rows = _basket_rows(db, user)
    prefs = option_prefs.read(db, user)
    sigs = option_store.basket_rows_for(db, user, prefs=prefs) if rows else {}
    items = []
    for r in rows:
        s = sigs.get(r.symbol)
        has_sig = bool(s and s.get("snap_on"))
        idea = s.get("idea") if s else None
        iv = (s or {}).get("iv") or {}
        pick_state = s.get("pick_state") if (has_sig and idea) else None
        age_h = s.get("age_h") if s else None
        stale = (not has_sig) or bool(s.get("stale"))
        if age_h is None:
            dot = "rose"
        elif age_h < FRESH_HOURS:
            dot = "emerald"
        elif age_h <= STALE_HOURS:
            dot = "amber"
        else:
            dot = "rose"
        items.append({"row": r, "sig": s if has_sig else None, "iv": iv, "rec": idea,
                      "pick_state": pick_state, "age_h": age_h, "stale": stale, "dot": dot,
                      "idea_word": _W.idea_short(idea) if idea else None,
                      "iv_words": _W.iv_rank_words(iv) if has_sig else None,
                      "trend": s.get("trend") if has_sig else None,
                      "headline": s.get("headline") if has_sig else None,
                      "no_setup": bool(has_sig and s.get("status") in ("no_setup",)),
                      "reading": _reading(r.symbol),
                      "read_err": None if has_sig else _read_error(r.symbol),
                      "as_of_text": None})
    rank = {"has_picks": 0, "not_checked": 1, "no_strike_passes": 2}
    keys = {
        "idea": lambda it: (it["rec"] is None, rank.get(it["pick_state"], 3), -((it["iv"] or {}).get("iv_rank") or 0)),
        "iv": lambda it: ((it["iv"] or {}).get("iv_rank") is None, -((it["iv"] or {}).get("iv_rank") or 0)),
        "trend": lambda it: (it["sig"] is None, {"up": 0, "down": 1, "sideways": 2, "unclear": 3}.get(it["trend"], 9)),
        "added": lambda it: it["row"].pos,
    }
    sort = sort if sort in SORTS else "idea"
    items.sort(key=keys[sort])
    return {"user": user, "items": items, "sort": sort, "selected": selected, "compact": compact,
            "n": len(rows), "max_basket": MAX_BASKET,
            "screener": _screener_suggestions(db, user, {r.symbol for r in rows}, prefs),
            "n_watchlist": len(uwl.symbol_set(db, user)),
            "n_ivscan_list": len(_ivscan_universe(user)),
            "n_ivscan_scan": len(_ivscan_scan_symbols(db, user)),
            "n_positions": len(_open_trade_symbols(db, user)),
            "criteria": DEFAULT_CRITERIA}


def _basket_response(request: Request, db: Session, user: User, *, sort="idea",
                     selected="", compact=False, events: dict | None = None) -> Response:
    ctx = _basket_context(db, user, sort=sort, selected=selected, compact=compact)
    resp = templates.TemplateResponse(request, "_options_basket.html", ctx)
    ev: dict = {"options:basket-changed": {"n": ctx["n"]}}
    for k, v in (events or {}).items():
        if k == "options:basket-changed" and isinstance(v, dict):
            ev[k] = {**ev[k], **v}            # keep n, add e.g. the "new" symbols (v4.131)
        else:
            ev[k] = v
    return _trigger(resp, ev)


# ────────────────────────────────── the card ──────────────────────────────────

def _nlv_for(user: User, live: dict | None) -> tuple[float | None, str | None]:
    """B5.3 order: the Live figure for THIS request -> stored trade_prefs nlv (> 0)
    -> None; nlv_source in {live, prefs, None}. Never written here."""
    if live and live.get("nlv"):
        try:
            return float(live["nlv"]), "live"
        except (TypeError, ValueError):
            pass
    stored = tp.read(user)["nlv"]
    return (stored, "prefs") if stored and stored > 0 else (None, None)


def _earnings_state(card: dict | None, chosen: dict | None, strategy: str, picks: list | None,
                    prefs: dict) -> dict:
    """{date, days, inside, rule, allowed, hide_strikes}: the earnings line of the card
    and the one gate that hides the strike table."""
    iv = (card or {}).get("iv") or {}
    date = iv.get("earnings_date")
    days = iv.get("earnings_days")
    expiry = None
    for p in _real_picks(picks):
        expiry = p.get("expiry") or p.get("back_expiry")
        break
    inside = bool(date and expiry and str(date) <= str(expiry))
    if chosen and chosen.get("reason_key") == "earnings_inside":
        inside = True
    allowed = _rule_allows_earnings(strategy, prefs) if strategy else False
    rule = ((prefs or {}).get("shared") or {}).get("earnings_rule") or "none_inside"
    return {"date": date, "label": strategy_rules.expiry_label(date) if date else None,
            "days": days, "inside": inside, "rule": rule, "allowed": allowed,
            "hide_strikes": _earnings_blocked(chosen, strategy, prefs),
            "line": _W.earnings_words(date, inside, expiry)}


def _age_badge(card: dict | None, live: dict | None) -> dict:
    """{text, cls, title} - 'as of Oct 2, 16:00 ET · delayed' or 'live · TWS 21:42 ET'."""
    if live and live.get("at"):
        return {"text": f"live · TWS {live['at']}", "cls": "text-emerald-300", "title": "Quotes from your own TWS, this moment, for the live expiry only. Nothing from it is stored."}
    if not card:
        return {"text": "no read yet", "cls": "text-slate-500", "title": "No stored chain for this ticker yet."}
    when = _fmt_as_of(card.get("as_of"))
    age = card.get("age_h")
    if card.get("stale") or (age is not None and age > STALE_HOURS):
        return {"text": f"as of {when} · delayed", "cls": "text-rose-300",
                "title": f"as of {when} - the nightly job has not run since. Refresh reads today's delayed chain."}
    if age is not None and age >= FRESH_HOURS:
        return {"text": f"as of {when} · delayed", "cls": "text-amber-300",
                "title": f"as of {when} - the nightly job hasn't run since. Refresh reads today's delayed chain."}
    return {"text": f"as of {when} · delayed", "cls": "text-emerald-300",
            "title": "Cboe's delayed feed, read by the nightly job (or your last Refresh)."}


def _card_context(db: Session, user: User, symbol: str, *, strategy: str = "", pick: int = 0,
                  note: str = "", note_kind: str = "ok", live: dict | None = None,
                  diag: dict | None = None, units: str = "$") -> dict:
    """Everything the card renders. card_for() is the ONLY thing the headline, chips,
    overlays and picks read; a hash miss is filled inside it from the stored chain.
    `live` is the in-request grade of a bridge payload: its picks replace the stored
    ones for the live expiry only, and nothing from it is stored."""
    sym = _clean_symbol(symbol)
    prefs = option_prefs.read(db, user)
    card = option_store.card_for(db, sym, user, prefs=prefs) if sym else None
    in_basket = bool(sym) and (db.query(OptionBasket)
                               .filter(OptionBasket.owner_key == _owner(user),
                                       OptionBasket.symbol == sym, OptionBasket.active.is_(True))
                               .first() is not None)
    strategies = list(card.get("strategies") or []) if card else []
    rec = _recommended(strategies)
    strategy = strategy if strategy in strategy_rules.STRATEGY_KEYS else (rec["key"] if rec else "")
    chosen = next((s for s in strategies if s.get("key") == strategy), None)
    family = option_prefs.family_of(strategy) if strategy else None
    stored_picks = list(((card or {}).get("picks") or {}).get(strategy) or []) if card else []
    live_picks = list(live.get("picks") or []) if live else []
    picks_src = live_picks if (live and live.get("picks") is not None) else stored_picks
    picks = _real_picks(picks_src)
    degenerate = _degenerate_of(picks_src) if not picks else None
    nlv, nlv_source = _nlv_for(user, live)
    if live and nlv_source == "live":
        _resize(picks, nlv, nlv_source, prefs)
    for p in picks:
        _decorate_pick(p, family)
    pick_i = max(0, min(pick, len(picks) - 1)) if picks else 0
    chips = _W.chip_row(strategies)
    earnings = _earnings_state(card, chosen, strategy, picks, prefs)
    rules_line = (picks[0].get("rules_line") if picks else
                  (degenerate or {}).get("rules_line") or (picks_src[0].get("rules_line") if picks_src else None))
    considered = picks[0].get("considered") if picks else (picks_src[0].get("considered") if picks_src else None)
    setup = (card or {}).get("setup") or {}
    if not rules_line and strategy:
        rules_line = _W.rules_line(prefs, strategy, setup)
    chosen_blocked = _earnings_blocked(chosen, strategy, prefs)
    gauge_text = _W.gauge((card or {}).get("iv")) if card else None
    # v4.131: a first-time read in flight, or why the last one failed (cleared once a card exists)
    reading = _reading(sym) if sym else False
    read_err = None if (reading or not sym) else _read_error(sym)
    if read_err and card:
        _clear_read_error(sym)
        read_err = None
    return {"user": user, "sym": sym, "card": card, "prefs": prefs, "in_basket": in_basket,
            "strategy": strategy, "chosen": chosen, "rec": rec, "family": family,
            "label": strategy_rules.LABELS.get(strategy, strategy) if strategy else None,
            "chips": chips, "picks": picks, "pick_i": pick_i, "live": live, "degenerate": degenerate,
            "rules_line": rules_line, "considered": considered,
            "headline": card.get("headline") if card else None,
            "gauge": gauge_text, "iv": (card or {}).get("iv") or {},
            "iv_words": _W.iv_rank_words((card or {}).get("iv")) if card else None,
            "iv_hv_words": _W.iv_hv_words((card or {}).get("iv")) if card else None,
            "setup": setup, "spot": setup.get("close"), "atr": setup.get("atr"),
            "must_happen": chosen.get("must_happen") if chosen else None,
            "earnings": earnings, "hide_strikes": chosen_blocked,
            "not_available": bool(chosen and chosen.get("reason_key") == "not_available_yet"),
            "rejected": bool(chosen and chosen.get("fit") == "rejected"),
            "rejection": (chosen.get("reasons") or [None])[0] if chosen else None,
            "age": _age_badge(card, live), "no_setup": bool(card and card.get("status") == "no_setup"),
            "reading": reading, "read_err": read_err,
            "bridge_port": BRIDGE_PORT, "bridge_setup_path": BRIDGE_SETUP_PATH,
            "bridge_min_version": BRIDGE_MIN_VERSION, "note": note, "note_kind": note_kind,
            "diag": diag, "nlv": nlv, "nlv_source": nlv_source, "trade_prefs": tp.read(user),
            "sizing_error": (card or {}).get("sizing_error"), "units": units if units in ("$", "R") else "$",
            "tab_label": option_prefs.TAB_LABELS.get(TAB_OF_FAMILY.get(family or ""), "Shared"),
            "job_missed": job_runs.missed(db, "nightly"),
            "last_run": job_runs.latest(db, "nightly")}


def _picks_context(db: Session, user: User, symbol: str, *, strategy: str = "", pick: int = 0,
                   live: dict | None = None, units: str = "$") -> dict:
    """_card_context minus the headline / gauge / chips / age: a chip click or a rule
    change must never recompose the sentence."""
    ctx = _card_context(db, user, symbol, strategy=strategy, pick=pick, live=live, units=units)
    for k in ("headline", "gauge", "chips", "age"):
        ctx.pop(k, None)
    return ctx


# ────────────────────────────────── chart overlays ──────────────────────────────────

def _bounce_from_setup(sup: dict | None) -> dict | None:
    """The BOUNCE dict _price_chart.html paints (sector._bounce_overlay's shape),
    built from the STORED setup.sup - never from a detector run."""
    if not isinstance(sup, dict) or sup.get("level") is None:
        return None
    return {"level": sup["level"], "zone": sup.get("zone"), "touches": sup.get("touches") or [],
            "bounce": sup.get("bounce"), "d_ema": sup.get("d_ema"), "w_ema": sup.get("w_ema"),
            "vol_ratio": sup.get("vol_ratio"),
            "label": "Support" + (f" ≈ {sup['d_ema']}" if sup.get("d_ema") else "")}


def _chart_overlays(setup: dict, expiry: str | None = None) -> dict:
    """chart_bounce / chart_trendline / chart_range from the stored setup. The
    trend line and range modules land in later releases; until then they read None."""
    setup = setup or {}
    out = {"bounce": _bounce_from_setup(setup.get("sup")), "trendline": None, "range": None}
    tl_mod, rb_mod = _svc("trend_line"), _svc("range_box")
    try:
        if tl_mod is not None and setup.get("tl"):
            out["trendline"] = tl_mod.overlay(setup.get("tl"), setup.get("tl_bounce"))
        if rb_mod is not None and setup.get("rng"):
            out["range"] = rb_mod.overlay(setup.get("rng"))
    except Exception as exc:  # noqa: BLE001 - an overlay is decoration, never the page
        log.warning("chart overlays: %s", exc)
    return out


def _levels_at(setup: dict, expiry: str | None) -> tuple:
    """payoff.build's levels= tuple: support / resistance from setup.levels, the trend
    line at the expiry, the range edges; kind in {support, resistance, trend_line, target}."""
    setup = setup or {}
    out = []
    lv = setup.get("levels") or {}
    if lv.get("support") is not None:
        out.append({"x": lv["support"], "label": f"support {_g(lv['support'])}", "kind": "support"})
    if lv.get("resistance") is not None:
        out.append({"x": lv["resistance"], "label": f"resistance {_g(lv['resistance'])}", "kind": "resistance"})
    tl = setup.get("tl") or {}
    va = (tl.get("value_at") or {}) if isinstance(tl, dict) else {}
    if expiry and va.get(expiry) is not None:
        out.append({"x": va[expiry], "label": f"trend line at expiry {_g(va[expiry])}", "kind": "trend_line"})
    rng = setup.get("rng") or {}
    if isinstance(rng, dict) and rng.get("low") is not None and rng.get("high") is not None:
        out.append({"x": rng["low"], "label": f"range low {_g(rng['low'])}", "kind": "support"})
        out.append({"x": rng["high"], "label": f"range high {_g(rng['high'])}", "kind": "resistance"})
    return tuple(out)


def _premium_stop_pct(strategy: str, prefs: dict, trade: OptionTrade | None = None):
    """R1: the leaps block's figure for leaps_call / diagonal_call, the long block's for
    buy_call / buy_put / bull_call / bear_put / calendar, None for the credit families."""
    fam = option_prefs.family_of(strategy) if strategy in strategy_rules.FAMILY_OF else None
    if fam in ("credit_vertical", "condor"):
        return None
    if trade is not None and getattr(trade, "loss_stop_pct", None):
        return trade.loss_stop_pct
    block = "leaps" if strategy in ("leaps_call", "diagonal_call") else "long"
    return (prefs.get(block) or option_prefs.HOUSE[block]).get("premium_stop_pct")


def _loss_fraction(prefs_tp: dict, trade: OptionTrade | None = None) -> float:
    if trade is not None and getattr(trade, "loss_stop_pct", None):
        return float(trade.loss_stop_pct) / 100.0
    return float(prefs_tp.get("loss_stop_pct") or 20.0) / 100.0


# ────────────────────────────────── positions / badge ──────────────────────────────────

def _latest_checks(db: Session, ids: list[int]) -> dict[int, OptionTradeCheck]:
    """One query: the newest OptionTradeCheck per trade."""
    if not ids:
        return {}
    rows = (db.query(OptionTradeCheck).filter(OptionTradeCheck.trade_id.in_(ids))
              .order_by(OptionTradeCheck.checked_on.asc(), OptionTradeCheck.id.asc()).all())
    out: dict[int, OptionTradeCheck] = {}
    for r in rows:
        out[r.trade_id] = r
    return out


def _verdict(trade: OptionTrade, check: OptionTradeCheck | None, cards: dict, db: Session,
             user: User, prefs: dict) -> dict:
    """The check's verdict plus the read-time 'earnings now inside' row (II.2.14)."""
    v = {"state": check.state if check else "UNKNOWN", "action": check.action if check else None,
         "reasons": list(check.reasons or []) if check else [], "urgent": bool(check.urgent) if check else False,
         "earnings_now_inside": None}
    sym = trade.symbol
    if sym not in cards:
        try:
            cards[sym] = option_store.card_for(db, sym, user, prefs=prefs)
        except Exception as exc:  # noqa: BLE001
            log.warning("positions: card_for %s: %s", sym, exc)
            cards[sym] = None
    card = cards.get(sym) or {}
    date = ((card.get("iv") or {}).get("earnings_date")) if card else None
    if trade.strategy == "calendar":
        life_end = trade.back_expiry or trade.front_expiry
    else:
        life_end = trade.front_expiry
    if (date and life_end and str(date) <= str(life_end)
            and not _rule_allows_earnings(trade.strategy, prefs)
            and (trade.earnings_date_at_entry is None or str(trade.earnings_date_at_entry) > str(date))):
        text = (f"Earnings {strategy_rules.expiry_label(date)} now fall inside this trade (the date was "
                f"unknown or later when you entered). Decide before the close that day.")
        v["earnings_now_inside"] = text
        if v["state"] in ("OK", "UNKNOWN", "WATCH"):
            v["state"] = "WATCH"
        v["urgent"] = True
        v["reasons"] = [text] + v["reasons"]
    return v


def _positions_context(db: Session, user: User, *, status: str = "open", focus: int = 0) -> dict:
    status = status if status in ("open", "closed") else "open"
    trades = (db.query(OptionTrade).filter(OptionTrade.user_id == user.id, OptionTrade.status == status)
                .order_by(OptionTrade.front_expiry, OptionTrade.symbol).all())
    latest = _latest_checks(db, [t.id for t in trades])
    prefs = option_prefs.read(db, user)
    cards: dict = {}
    rows = []
    for t in trades:
        chk = latest.get(t.id)
        rows.append({"trade": t, "check": chk, "verdict": _verdict(t, chk, cards, db, user, prefs),
                     "label": _pick_label({"legs": t.legs or []}),
                     "family_word": "credit" if t.family in ("credit_vertical", "condor") else "debit",
                     "history": sorted(t.checks, key=lambda c: c.checked_on)[-20:],
                     "dte": (_dt.date.fromisoformat(t.front_expiry) - clock.et_date()).days if t.front_expiry else None})
    urgent = [r for r in rows if r["verdict"]["urgent"]]
    watch = [r for r in rows if r["verdict"]["state"] == "WATCH" and not r["verdict"]["urgent"]]
    ids = [r["trade"].id for r in rows]
    focus_id = focus if focus in ids else (urgent[0]["trade"].id if urgent else (rows[0]["trade"].id if rows else None))
    return {"user": user, "rows": rows, "status": status, "focus": focus_id, "inside_options": True,
            "n_urgent": len(urgent), "urgent": urgent, "watch": watch, "prefs": tp.read(user),
            "checked_on": clock.et_today()}


def _badge_dict(db: Session, user: User) -> dict:
    """Exactly {run_on, finished_at, ok, errors, stale, running, job_missed, ideas_new,
    urgent, watch} - stored rows only, never a quote."""
    latest = job_runs.latest(db, "nightly")
    last_day = clock.last_trading_day(clock.et_date()).isoformat()
    run_on = latest.run_on if latest else None
    finished = latest.finished_at.isoformat() if (latest and latest.finished_at) else None
    seen = (getattr(user, "prefs", None) or {}).get(SEEN_PREF)
    ideas_new = 0
    try:
        q = db.query(OptionIdeaPush).filter(OptionIdeaPush.user_id == user.id)
        if seen:
            seen_dt = _dt.datetime.fromisoformat(str(seen).replace("Z", "+00:00"))
            if seen_dt.tzinfo is not None:
                seen_dt = seen_dt.astimezone(_dt.timezone.utc).replace(tzinfo=None)
            q = q.filter(OptionIdeaPush.sent_at >= seen_dt)
        ideas_new = q.count()
    except Exception as exc:  # noqa: BLE001
        log.warning("badge ideas_new: %s", exc)
    trades = (db.query(OptionTrade).filter(OptionTrade.user_id == user.id, OptionTrade.status == "open").all())
    checks = _latest_checks(db, [t.id for t in trades])
    prefs = option_prefs.read(db, user)
    cards: dict = {}
    urgent = watch = 0
    for t in trades:
        v = _verdict(t, checks.get(t.id), cards, db, user, prefs)
        if v["urgent"] or v["state"] in ("CLOSE", "ROLL", "TAKE"):
            urgent += 1
        elif v["state"] == "WATCH":
            watch += 1
    return {"run_on": run_on, "finished_at": finished,
            "ok": latest.ok if latest else 0, "errors": latest.errors if latest else 0,
            "stale": (run_on or "") < last_day, "running": job_runs.running(db, "nightly"),
            "job_missed": job_runs.missed(db, "nightly"), "ideas_new": ideas_new,
            "urgent": urgent, "watch": watch}


def _status_dict(db: Session, user: User) -> dict:
    """The badge dict + state (ok|warn|bad|none), as_of_oldest, n_basket, n_stale,
    bridge_port, delayed_or_live, paused - what the honesty strip renders."""
    d = _badge_dict(db, user)
    latest = job_runs.latest(db, "nightly")
    any_run = job_runs.latest_any(db, "nightly")
    today = clock.et_date()
    last_day = clock.last_trading_day(today)
    prev_day = clock.prev_trading_day(last_day)
    crashed = False
    if any_run is not None and any_run.finished_at is None and any_run.started_at is not None:
        age_min = job_runs._age_minutes(any_run.started_at)
        crashed = age_min is not None and age_min > 120
    if latest is None:
        state = "none"
    elif crashed or (latest.run_on or "") < prev_day.isoformat():
        state = "bad"
    elif (latest.errors or 0) > 0 or (latest.run_on or "") < last_day.isoformat() or d["job_missed"]:
        state = "warn"
    else:
        state = "ok"
    rows = _basket_rows(db, user)
    prefs = option_prefs.read(db, user)
    sigs = option_store.basket_rows_for(db, user, prefs=prefs) if rows else {}
    ages = [s.get("age_h") for s in sigs.values() if s.get("age_h") is not None]
    n_stale = sum(1 for r in rows if (sigs.get(r.symbol) or {}).get("stale", True))
    stale_syms = [r.symbol for r in rows if (sigs.get(r.symbol) or {}).get("stale", True)]
    oldest = None
    if ages:
        oldest_h = max(ages)
        oldest = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=oldest_h))
    tg = prefs.get("telegram") or {}
    paused = bool(tg.get("paused_until") and str(tg["paused_until"]) >= today.isoformat())
    detail = latest.detail if (latest and isinstance(latest.detail, dict)) else {}
    errs = [f"{k}: {v.get('err')}" for k, v in detail.items()
            if isinstance(v, dict) and v.get("status") in ("error",) and v.get("err")][:3]
    # v4.131: the last IV-history backfill - its own job row (a basket add) or the newest
    # nightly's step-1 summary, whichever is newer - and THIS member's first-time reads in flight
    bf = job_runs.latest(db, "backfill")
    bf_detail = bf.detail if (bf and isinstance(bf.detail, dict)) else {}
    nb = detail.get("_backfill") if isinstance(detail.get("_backfill"), dict) else None
    if nb is not None and nb.get("error"):
        nb = None
    use_nightly = nb is not None and (bf is None or (latest.finished_at and bf.finished_at
                                                      and latest.finished_at > bf.finished_at))
    if use_nightly:
        bf_note, bf_when, bf_rows, bf_short, bf_run_on = (nb.get("note"), latest.finished_at,
                                                          int(nb.get("gained") or 0),
                                                          len(nb.get("short") or []), latest.run_on)
    else:
        bf_note, bf_when, bf_rows, bf_short, bf_run_on = ((bf.note, bf.finished_at, int(bf.rows or 0),
                                                           len(bf_detail.get("_short") or []), bf.run_on)
                                                          if bf else (None, None, 0, 0, None))
    reading = _reading_symbols(user.id)
    d.update({"n_reading": len(reading), "reading_symbols": reading,
              "backfill_note": bf_note, "backfill_when": bf_when, "backfill_rows": bf_rows,
              "backfill_short": bf_short, "backfill_today": bool(bf_run_on and bf_run_on == today.isoformat())})
    d.update({"state": state, "as_of_oldest": _fmt_as_of(oldest) if oldest else "no data yet",
              "n_basket": len(rows), "n_stale": n_stale, "stale_symbols": stale_syms,
              "basket_symbols": [r.symbol for r in rows],
              "bridge_port": BRIDGE_PORT, "delayed_or_live": "delayed", "paused": paused,
              "job_when": latest.finished_at if latest else None,
              "job_symbols": latest.symbols if latest else 0, "job_errors_text": "; ".join(errs),
              "last_run": latest, "crashed": crashed, "bridge_min_version": BRIDGE_MIN_VERSION})
    return d


# ────────────────────────────────── rules drawer ──────────────────────────────────

def _field_words(block: str, key: str, f, value, atr: float | None, sym: str, merged: dict) -> str:
    """The one-line translation beside a field: the SCHEMA's own plain text with
    its placeholders ({lo_usd} / {hi_usd} / {sym} / {v}) filled from the selected
    ticker's ATR. (option_words.rule_words is the picks banner line, not a
    per-field sentence.)"""
    plain = f.plain or ""
    lo_k, hi_k = None, None
    if key.endswith("_lo"):
        lo_k, hi_k = key, key[:-3] + "_hi"
    elif key.endswith("_hi"):
        lo_k, hi_k = key[:-3] + "_lo", key
    lo = merged.get(lo_k, value) if lo_k else value
    hi = merged.get(hi_k, value) if hi_k else value
    sub = {"v": _g(value) if not isinstance(value, bool) else value, "lo": _g(lo), "hi": _g(hi),
           "sym": sym or "the ticker",
           "lo_usd": _g(float(lo) * atr, 0) if (atr and isinstance(lo, (int, float))) else "?",
           "hi_usd": _g(float(hi) * atr, 0) if (atr and isinstance(hi, (int, float))) else "?"}
    try:
        return plain.format(**sub)
    except (KeyError, IndexError, ValueError):
        return plain


def _translation(tab: str, merged: dict, prefs_tp: dict, atr: float | None, sym: str) -> str:
    """The one-line translation at the top of each tab (D3.2)."""
    sh, cv = merged.get("shared") or {}, merged.get("credit_vertical") or {}
    if tab == "credit":
        width = ""
        if atr:
            width = f" (about ${_g(cv['width_atr_lo'] * atr, 0)}-{_g(cv['width_atr_hi'] * atr, 0)} on {sym})"
        return (f"Sell a put or call spread with the short strike at delta {_g(cv['short_delta_lo'])}-{_g(cv['short_delta_hi'])} "
                f"(about a {round((1 - cv['short_delta_hi']) * 100)}-{round((1 - cv['short_delta_lo']) * 100)}% chance it expires worthless), "
                f"{cv['dte_lo']}-{cv['dte_hi']} days out, {_g(cv['width_atr_lo'])}-{_g(cv['width_atr_hi'])} ATR wide{width}, "
                f"for at least {cv['credit_pct_min']}% of what you risk, only when IV rank is at least {cv['iv_gate_min']}"
                f"{', with the short strike outside the level the chart says must hold' if sh.get('chart_constraint') else ''}. "
                f"Take profit at {_g(prefs_tp.get('profit_target_pct'))}% of the credit, stop at {_g(prefs_tp.get('loss_stop_pct'))}% of max loss, "
                f"out by {prefs_tp.get('dte_floor')} days left.")
    if tab == "shared":
        rule = "no earnings inside any trade" if sh.get("earnings_rule") == "none_inside" else "earnings inside only for trades with a fixed worst case"
        return (f"Every leg needs at least {sh.get('min_oi')} contracts open (and {sh.get('oi_per_contract')}x your size), quoted no wider than ${_g(sh.get('max_leg_spread'))}; "
                f"{rule}; a gap through the stop may cost at most {_g(sh.get('gap_mult'))}x your risk budget. "
                f"Account value and risk per trade size every idea; they never change which strikes are picked.")
    if tab == "debit":
        lg, dv, lp = merged.get("long") or {}, merged.get("debit_vertical") or {}, merged.get("leaps") or {}
        return (f"Buy a call or put at delta {_g(lg['delta_lo'])}-{_g(lg['delta_hi'])}, {lg['dte_lo']}-{lg['dte_hi']} days out, decaying at most {_g(lg['theta_pct_max'])}% a day; "
                f"a spread caps the upside at the chart target and must pay at least {_g(dv['reward_cost_min'])}x its cost; "
                f"a long-dated call is {_g(lp['delta_lo'])}-{_g(lp['delta_hi'])} delta, {lp['months_lo']}-{lp['months_hi']} months out, paying at most {lp['extrinsic_pct_max']}% of the share price for time. "
                f"The stop sits 1 ATR under the entry (and under the level that must hold); the target at 2R - the Curated convention, not a setting.")
    if tab == "condor":
        cd = merged.get("condor") or {}
        return (f"Sell both sides of a range at delta {_g(cd['short_delta_lo'])}-{_g(cd['short_delta_hi'])} each, wings {_g(cd['wing_atr_lo'])}-{_g(cd['wing_atr_hi'])} ATR wide, "
                f"{cd['dte_lo']}-{cd['dte_hi']} days out, for at least {cd['credit_pct_min']}% of what you risk; act when either short delta reaches {_g(cd['roll_delta'])}, "
                f"close when the loss equals {cd['loss_stop_pct_credit']}% of the credit.")
    tm = merged.get("time") or {}
    return (f"Calendar: sell the {tm['cal_front_lo']}-{tm['cal_front_hi']} day option and own the {tm['cal_back_lo']}-{tm['cal_back_hi']} day one at the money, take profit at {tm['cal_take_pct']}% of the debit. "
            f"Diagonal: own a {tm['diag_long_dte_lo']}-{tm['diag_long_dte_hi']} day call at delta {_g(tm['diag_long_delta_lo'])}-{_g(tm['diag_long_delta_hi'])} and rent out "
            f"{tm['diag_short_dte_lo']}-{tm['diag_short_dte_hi']} day calls at delta {_g(tm['diag_short_delta_lo'])}-{_g(tm['diag_short_delta_hi'])} under the resistance.")


def _rules_context(db: Session, user: User, tab: str, *, err: list | None = None, msg: str = "",
                   sym: str = "", atr: float | None = None, hash_before: str | None = None) -> dict:
    tab = tab if tab in option_prefs.TABS else "shared"
    prefs = option_prefs.read(db, user)
    over = prefs.get("_overridden") or set()
    prefs_tp = tp.read(user)
    tabs = []
    for key in option_prefs.TABS:
        n = sum(1 for d in over if d.split(".")[0] in option_prefs.TAB_BLOCKS[key])
        tabs.append((key, option_prefs.TAB_LABELS[key], n))
    sections = []
    for block in option_prefs.TAB_BLOCKS[tab]:
        merged = prefs.get(block) or {}
        fields = []
        for key, f in option_prefs.SCHEMA[block].items():
            is_safety = block == "shared" and key in ("chart_constraint", "earnings_rule")
            choices = []
            if f.kind == "choice":
                labels = {"none_inside": "not allowed", "defined_risk_only": "defined-risk trades only"}
                choices = [(v, labels.get(v, v)) for v in option_prefs.CHOICES.get(key, ())]
            fields.append({"key": key, "label": f.label, "help": f.help, "kind": f.kind,
                           "value": merged.get(key, f.default), "default": f.default,
                           "lo": f.lo, "hi": f.hi, "step": f.step, "unit": f.unit, "choices": choices,
                           "overridden": f"{block}.{key}" in over, "is_safety": is_safety,
                           "words": _field_words(block, key, f, merged.get(key, f.default), atr, sym, merged)})
        sections.append((block, option_prefs.BLOCK_LABELS.get(block, block), fields))
    n_over = len(over)
    return {"user": user, "tab": tab, "label": option_prefs.TAB_LABELS[tab], "tabs": tabs,
            "sections": sections, "prefs": prefs, "err": err or [], "msg": msg,
            "nlv": prefs["account"].get("nlv"), "risk_pct": prefs["account"].get("risk_pct"),
            "exit_lines": {"profit_target_pct": prefs_tp["profit_target_pct"], "loss_stop_pct": prefs_tp["loss_stop_pct"],
                           "dte_floor": prefs_tp["dte_floor"], "roll_delta": prefs_tp["roll_delta"]},
            "telegram": prefs.get("telegram") or {}, "atr": atr, "sym": sym,
            "translation": _translation(tab, prefs, prefs_tp, atr, sym),
            "n_overridden": n_over,
            "summary": (f"- {n_over} rule{'s' if n_over != 1 else ''} changed" if n_over else "- house defaults"),
            "hash": option_prefs.prefs_hash(prefs), "hash_before": hash_before,
            "house_hash": option_prefs.HOUSE_HASH, "defaults_tp": {
                "profit_target_pct": tp.DEFAULT_PROFIT_TARGET_PCT, "loss_stop_pct": tp.DEFAULT_LOSS_STOP_PCT,
                "dte_floor": tp.DEFAULT_DTE_FLOOR, "roll_delta": tp.DEFAULT_ROLL_DELTA}}


def _trade_prefs_only(form: dict, tab: str) -> bool:
    """True when the posted form carries NO rule field of the tab's blocks - only
    the trade_prefs lines (nlv / risk_pct / the credit exit lines)."""
    names = set()
    for block in option_prefs.TAB_BLOCKS.get(tab, ()):
        for name in option_prefs.SCHEMA[block]:
            names.update({name, f"{block}.{name}", f"{block}__{name}"})
    has_rule = any(k in names for k in form)
    has_tp = any(k in form for k in option_prefs.TRADE_PREFS_FORM_KEYS)
    return has_tp and not has_rule


def _template_exists(name: str) -> bool:
    """A sibling template another agent owns may not have landed on this checkout."""
    try:
        templates.env.get_template(name)
        return True
    except Exception:  # noqa: BLE001 - TemplateNotFound or a syntax error in it
        return False


def _rules_response(request: Request, ctx: dict, *, changed: bool = False, recompute: bool = False) -> Response:
    resp = templates.TemplateResponse(request, "_options_rules.html", ctx)
    if changed:
        _trigger(resp, {"options:rules-changed": {"tab": ctx["tab"], "hash": ctx["hash"], "recompute": recompute}})
    return resp


def _write_telegram(db: Session, user: User, tg: dict) -> None:
    """prefs['telegram'] on the member's UserOptionPrefs row - its own key, never a
    SCHEMA block, never hashed (the hash is left as it is). Portable ORM."""
    row = getattr(user, "option_prefs", None)
    if row is None:
        row = UserOptionPrefs(user_id=user.id, prefs={}, prefs_hash=option_prefs.HOUSE_HASH,
                              schema_version=option_prefs.SCHEMA_VERSION)
        db.add(row)
        try:
            user.option_prefs = row
        except Exception:  # noqa: BLE001
            pass
    prefs = dict(row.prefs or {})
    prefs["telegram"] = {k: v for k, v in tg.items()}
    row.prefs = prefs
    db.commit()


# ────────────────────────────────── routes: shell + basket ──────────────────────────────────

@router.get("", response_class=HTMLResponse)
def options_home(request: Request, symbol: str = "", tab: str = "ideas", focus: int = 0,
                 pause: int = 0, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """The shell; every slow panel is a lazy HTMX fragment. Writes prefs['options_seen_at']
    so the nav badge's ideas_new count clears."""
    try:
        prefs = dict(getattr(user, "prefs", None) or {})
        prefs[SEEN_PREF] = _utcnow().replace(tzinfo=None).isoformat()
        user.prefs = prefs
        db.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("options_seen_at: %s", exc)
        db.rollback()
    n_basket = len(_basket_rows(db, user))
    return templates.TemplateResponse(request, "options.html", {
        "user": user, "n_basket": n_basket, "symbol": _clean_symbol(symbol),
        "tab": "positions" if tab == "positions" else "ideas", "focus": focus,
        "max_basket": MAX_BASKET, "pause_days": pause if pause in (7, 14, 30) else 0,
        "bridge_port": BRIDGE_PORT, "bridge_min_version": BRIDGE_MIN_VERSION,
        "criteria": DEFAULT_CRITERIA})


@router.get("/basket", response_class=HTMLResponse)
def basket(request: Request, sort: str = "idea", selected: str = "", compact: int = 0,
           user: User = Depends(require_user), db: Session = Depends(get_db)):
    ctx = _basket_context(db, user, sort=sort, selected=_clean_symbol(selected), compact=bool(compact))
    return templates.TemplateResponse(request, "_options_basket.html", ctx)


@router.post("/basket/add", response_class=HTMLResponse)
def basket_add(request: Request, symbol: str = Form(""), note: str = Form(""), sort: str = Form("idea"),
               compact: int = Form(0), user: User = Depends(require_user), db: Session = Depends(get_db)):
    """One typed ticker (form-encoded, hx-vals) = import with source='typed'."""
    sym = _clean_symbol(symbol)
    res = _add_symbols(db, user, [sym] if sym else [], "typed", note)
    if res["added"]:
        _first_read_in_background(res["new"], user.id)       # v4.131: its history, today's chain, the engines - now
    msg = (f"{sym} added - reading its chain and IV history now" if res["added"] else
           ("Basket is full (%d tickers)" % MAX_BASKET if res["over_cap"] else
            (f"{sym} is already in your basket" if sym else "That is not a ticker")))
    events = _toast(msg, "ok" if res["added"] else "err")
    if res["added"]:
        events["options:basket-changed"] = {"new": res["new"]}   # the page reloads the card if it shows one of these
    return _basket_response(request, db, user, sort=sort, selected=sym, compact=bool(compact), events=events)


@router.post("/basket/remove", response_class=HTMLResponse)
def basket_remove(request: Request, symbol: str = Form(""), sort: str = Form("idea"), compact: int = Form(0),
                  user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Deletes the member's row only; the shared snapshot / signal rows expire through prune."""
    sym = _clean_symbol(symbol)
    (db.query(OptionBasket).filter(OptionBasket.owner_key == _owner(user), OptionBasket.symbol == sym)
       .delete(synchronize_session=False))
    db.commit()
    return _basket_response(request, db, user, sort=sort, compact=bool(compact),
                            events=_toast(f"{sym} removed from your basket. Its tracked positions are kept.", "ok"))


@router.post("/basket/import")
def basket_import(payload: BasketImport, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Bulk add (a JSON body). Returns {added, skipped, over_cap, total} with the
    HX-Trigger 'options:basket-changed' so the basket fragment re-renders itself."""
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
    if new:
        _first_read_in_background(new, user.id)              # v4.131: the first-time read for the new ones only
    resp = JSONResponse(res)
    return _trigger(resp, {"options:basket-changed": {"n": res["total"], "new": new},
                           **_toast(f"{res['added']} added, {res['skipped']} skipped"
                                    + (f", {res['over_cap']} over the {MAX_BASKET} cap" if res["over_cap"] else "")
                                    + (" - reading them now" if new else ""),
                                    "ok" if res["added"] else "info")})


# ────────────────────────────────── routes: card, picks, chart ──────────────────────────────────

@router.get("/card/{symbol}", response_class=HTMLResponse)
def card(symbol: str, request: Request, strategy: str = "", pick: int = 0,
         user: User = Depends(require_user), db: Session = Depends(get_db)):
    sym = _clean_symbol(symbol)
    resp = templates.TemplateResponse(request, "_options_card.html",
                                      _card_context(db, user, symbol, strategy=strategy, pick=pick))
    if _pop_done(sym, user.id):                   # its first-time read just finished: this member's basket row changes too
        resp = _trigger(resp, {"options:basket-changed": {}})
    return resp


@router.get("/card/{symbol}/reading", response_class=HTMLResponse)
def card_reading(symbol: str, request: Request, strategy: str = "",
                 user: User = Depends(require_user), db: Session = Depends(get_db)):
    """The poll behind "Reading X now" (v4.131): 204 while the first-time read runs -
    nothing swaps, the chart and the member's chip stay put - then, once, the finished
    card retargeted into #optPane (HX-Retarget / HX-Reswap), which also removes the
    polling span. The basket re-renders through the trigger."""
    sym = _clean_symbol(symbol)
    if _reading(sym):
        return Response(status_code=204)
    _pop_done(sym, user.id)
    resp = templates.TemplateResponse(request, "_options_card.html",
                                      _card_context(db, user, sym, strategy=strategy))
    resp.headers["HX-Retarget"] = "#optPane"
    resp.headers["HX-Reswap"] = "innerHTML"
    return _trigger(resp, {"options:basket-changed": {}})


@router.get("/picks/{symbol}", response_class=HTMLResponse)
def picks(symbol: str, request: Request, strategy: str = "", pick: int = 0, units: str = "$",
          user: User = Depends(require_user), db: Session = Depends(get_db)):
    """The strike table + payoff container for ONE strategy, served apart from the
    card so a chip click (or a rule change) never remounts the chart."""
    return templates.TemplateResponse(request, "_options_picks.html",
                                      _picks_context(db, user, symbol, strategy=strategy, pick=pick, units=units))


def _chart_context_for(db: Session, user: User, sym: str, *, strategy: str, pick: int, trade: int) -> dict:
    ctx = _chart_ctx(db, user, sym)
    spread = levels = None
    family = None
    overlays = {"bounce": None, "trendline": None, "range": None}
    if trade:
        t = db.get(OptionTrade, trade)
        if t is not None and t.user_id == user.id:
            family = t.family
            pick_d = {"legs": t.legs or [], "expiry": t.front_expiry, "breakevens": []}
            spread = _chart_spec(pick_d)
            if t.family in DEBIT_FAMILIES and t.chart_stop is not None:
                levels = {"entry": t.net_entry, "stop": t.chart_stop, "target": t.chart_target, "label": ""}
                levels = None if levels["target"] is None else levels
            card = option_store.card_for(db, t.symbol, user)
            overlays = _chart_overlays((card or {}).get("setup") or {}, t.front_expiry)
    else:
        pc = _picks_context(db, user, sym, strategy=strategy, pick=pick)
        family = pc["family"]
        setup = pc["setup"]
        p = pc["picks"][pc["pick_i"]] if pc["picks"] else None
        overlays = _chart_overlays(setup, p.get("expiry") if p else None)
        if p is not None and family in LEVEL_FAMILIES:
            spread = p["chart_spec"]
        plan = setup.get("plan") or {}
        if family in DEBIT_FAMILIES and plan.get("stop") is not None:
            levels = {"entry": plan.get("entry"), "stop": plan.get("stop"), "target": plan.get("target"), "label": ""}
    ctx.update({"bounce": overlays["bounce"], "trendline": overlays["trendline"], "range": overlays["range"],
                "spread": spread, "levels": levels, "family": family, "strategy": strategy, "pick": pick,
                "trade": trade})
    return ctx


@router.get("/chart/{symbol}", response_class=HTMLResponse)
def chart(symbol: str, request: Request, strategy: str = "", pick: int = 0, trade: int = 0,
          user: User = Depends(require_user), db: Session = Depends(get_db)):
    """The one chart: sector._chart_ctx plus the option overlays, all from the STORED
    setup (card_for) - no detector runs on a request."""
    sym = _clean_symbol(symbol)
    return templates.TemplateResponse(request, "_options_chart.html",
                                      _chart_context_for(db, user, sym, strategy=strategy, pick=pick, trade=trade))


# ────────────────────────────────── routes: payoff, chain, ticket ──────────────────────────────────

def _payoff_unavailable(request: Request, text: str) -> HTMLResponse:
    return HTMLResponse(f'<div class="min-h-[300px] flex items-center justify-center text-[11px] '
                        f'text-amber-300 px-4 text-center">{text}</div>')


def _sigma_for_leg(leg: dict, chk_leg: dict | None, chk_spot, today: str, po):
    """The sigma that reprices the leg at the latest check's mid: the check's own stored
    iv when it has one, else payoff.implied_vol when the engine offers it, else entry_iv."""
    if chk_leg and chk_leg.get("iv"):
        return chk_leg["iv"]
    if chk_leg and chk_leg.get("mid") is not None and chk_spot and po is not None and hasattr(po, "implied_vol"):
        try:
            return po.implied_vol(leg, chk_leg["mid"], chk_spot, today)
        except Exception:  # noqa: BLE001
            pass
    return leg.get("entry_iv") or leg.get("iv")


@router.get("/payoff/{symbol}", response_class=HTMLResponse)
def payoff_pane(symbol: str, request: Request, strategy: str = "", pick: int = 0, units: str = "$",
                trade: int = 0, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Thin: load legs -> payoff.build(...) -> _payoff_chart.html. Always per ONE
    contract; units is passed IN and the client never recomputes."""
    units = units if units in ("$", "R") else "$"
    sym = _clean_symbol(symbol)
    po = _svc("payoff")
    if po is None:
        return _payoff_unavailable(request, "The payoff chart is not available on this build yet.")
    prefs = option_prefs.read(db, user)
    prefs_tp = tp.read(user)
    today = clock.et_today()
    try:
        if trade:
            t = db.get(OptionTrade, trade)
            if t is None or t.user_id != user.id:
                return _payoff_unavailable(request, "That trade is not yours to look at.")
            chk = sorted(t.checks, key=lambda c: c.checked_on)[-1] if t.checks else None
            chk_legs = list(chk.legs or []) if chk else []
            legs = []
            for i, l in enumerate(t.legs or []):
                cl = chk_legs[i] if i < len(chk_legs) else None
                legs.append(po.Leg.from_dict(dict(l, iv=_sigma_for_leg(l, cl, chk.spot if chk else None, today, po)),
                                             price=l.get("entry_price", l.get("price"))))
            card = option_store.card_for(db, t.symbol, user, prefs=prefs) or {}
            setup = card.get("setup") or {}
            built = po.build(legs, strategy=t.strategy, spot=(chk.spot if chk and chk.spot else setup.get("close")),
                             atr=setup.get("atr"), as_of=today, chart_stop=t.chart_stop, target=t.chart_target,
                             premium_stop_pct=_premium_stop_pct(t.strategy, prefs, t),
                             loss_fraction=_loss_fraction(prefs_tp, t),
                             pl_now=(chk.pl if chk else None), units=units,
                             levels=_levels_at(setup, t.front_expiry), symbol=t.symbol)
        else:
            ctx = _picks_context(db, user, sym, strategy=strategy, pick=pick)
            if not ctx["picks"]:
                return _payoff_unavailable(request, "No strikes to draw yet - nothing passes your rules today.")
            p, setup, iv = ctx["picks"][ctx["pick_i"]], ctx["setup"], ctx["iv"]
            legs = [po.Leg.from_dict(l) for l in p["legs"]]
            built = po.build(legs, strategy=ctx["strategy"], spot=setup.get("close"), atr=setup.get("atr"), as_of=today,
                             chart_stop=p.get("chart_stop"),
                             target=setup.get("target") if ctx["family"] in DEBIT_FAMILIES else None,
                             premium_stop_pct=_premium_stop_pct(ctx["strategy"], ctx["prefs"]),
                             loss_fraction=float(ctx["trade_prefs"]["loss_stop_pct"]) / 100.0,
                             levels=_levels_at(setup, p.get("expiry")),
                             sigma_fallback=((iv.get("iv30") or 0) / 100.0) or None, units=units, symbol=sym)
    except Exception as exc:  # noqa: BLE001 - never a blank box
        log.warning("payoff %s: %s", sym, exc, exc_info=True)
        return _payoff_unavailable(request, "Could not draw the payoff (no stored quotes for these strikes). Press Refresh.")
    pane_url = str(request.url.remove_query_params("units"))
    if not _template_exists("_payoff_chart.html"):
        # the payoff pane's partial is another part's file; until it lands the numbers
        # still reach the member in words, inside the same pane height
        return _payoff_unavailable(request, "The payoff chart is not drawn on this build yet. "
                                   + (f"Max profit {_money(built.get('max_profit'))} · max loss {_money(built.get('max_loss'))}."
                                      if isinstance(built, dict) and built.get("max_loss") is not None else ""))
    return templates.TemplateResponse(request, "_payoff_chart.html", {"po": built, "pane_url": pane_url, "user": user})


@router.get("/chain/{symbol}", response_class=HTMLResponse)
def chain(symbol: str, request: Request, expiry: str = "", all: int = 0, strategy: str = "", pick: int = 0,
          user: User = Depends(require_user), db: Session = Depends(get_db)):
    """The full stored chain for the expander: the pick's expiry and +/- 12 strikes
    around spot by default; an expiry picker and 'show all strikes'. Gamma only here."""
    sym = _clean_symbol(symbol)
    ch = option_store.latest_chain(db, sym)
    if ch is None:
        return templates.TemplateResponse(request, "_options_chain.html",
                                          {"user": user, "sym": sym, "rows": [], "expiries": [], "expiry": "",
                                           "spot": None, "all": bool(all), "n_total": 0, "highlight": set(),
                                           "as_of": None, "window": CHAIN_STRIKES_EACH_SIDE})
    rows = list(ch.rows)
    expiries = sorted({r.expiry for r in rows})
    dte_of = {}
    for r in rows:
        dte_of.setdefault(r.expiry, r.dte)
    if expiry not in expiries:
        expiry = ""
    highlight: set = set()
    if strategy or not expiry:
        pc = _picks_context(db, user, sym, strategy=strategy, pick=pick)
        if pc["picks"]:
            p = pc["picks"][pc["pick_i"]]
            if not expiry:
                expiry = p.get("expiry") if p.get("expiry") in expiries else ""
            for l in p.get("legs") or []:
                highlight.add((l.get("expiry"), l.get("right"), float(l.get("strike") or 0)))
    if not expiry:
        expiry = expiries[0] if expiries else ""
    sel = [r for r in rows if r.expiry == expiry]
    spot = ch.spot
    strikes = sorted({r.strike for r in sel})
    if not all and spot and len(strikes) > 2 * CHAIN_STRIKES_EACH_SIDE + 1:
        below = [k for k in strikes if k <= spot][-CHAIN_STRIKES_EACH_SIDE:]
        above = [k for k in strikes if k > spot][:CHAIN_STRIKES_EACH_SIDE]
        keep = set(below) | set(above)
        sel = [r for r in sel if r.strike in keep]
        strikes = sorted(keep)
    by_k: dict[float, dict] = {}
    for r in sel:
        slot = by_k.setdefault(r.strike, {"strike": r.strike, "call": None, "put": None})
        slot["call" if r.right == "C" else "put"] = r
    table = [by_k[k] for k in strikes if k in by_k]
    return templates.TemplateResponse(request, "_options_chain.html", {
        "user": user, "sym": sym, "rows": table, "expiries": [(e, dte_of.get(e)) for e in expiries],
        "expiry": expiry, "spot": spot, "all": bool(all), "n_total": len([r for r in rows if r.expiry == expiry]),
        "highlight": highlight, "as_of": _fmt_as_of(ch.as_of), "window": CHAIN_STRIKES_EACH_SIDE,
        "strategy": strategy, "pick": pick})


@router.get("/ticket/{symbol}", response_class=HTMLResponse)
def ticket(symbol: str, request: Request, strategy: str = "", pick: int = 0, contracts: int | None = None,
           dip: int = 0, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Both broker renderings of the order ticket for the chosen pick, server-rendered
    so the numbers are the pick's. A strategy rejected for earnings inside (and not
    admitted by defined_risk_only) gets ONE line and no ticket."""
    sym = _clean_symbol(symbol)
    ctx = _picks_context(db, user, sym, strategy=strategy, pick=pick)
    card, chosen, prefs = ctx["card"], ctx["chosen"], ctx["prefs"]
    base = {"user": user, "sym": sym, "strategy": ctx["strategy"], "label": ctx["label"], "chosen": chosen,
            "dip": bool(dip), "prefs": prefs, "pick": None, "pick_i": ctx["pick_i"], "contracts": None, "tws": None, "moomoo": None,
            "ticket": None, "refresh_first": False, "as_of": _fmt_as_of((card or {}).get("as_of")) if card else None,
            "source": (card or {}).get("source"), "refused": None, "unavailable": None,
            "footer": None, "sizing_line": None, "earnings": ctx["earnings"]}
    if card is None:
        base["unavailable"] = f"No read yet for {sym} - press Refresh first."
        return templates.TemplateResponse(request, "_options_ticket.html", base)
    if ctx["hide_strikes"]:
        date = ctx["earnings"].get("label") or ctx["earnings"].get("date") or "ahead"
        base["refused"] = f"No ticket: earnings {date} fall inside this trade and your rule says no."
        return templates.TemplateResponse(request, "_options_ticket.html", base)
    if ctx["not_available"] or not ctx["picks"]:
        base["unavailable"] = ("That strategy is not in TradeHunter yet." if ctx["not_available"]
                               else "No strike passes your rules today - nothing to order.")
        return templates.TemplateResponse(request, "_options_ticket.html", base)
    p = ctx["picks"][ctx["pick_i"]]
    sizing = dict(p.get("sizing") or {})
    if contracts is not None:
        sizing["contracts"] = max(0, min(int(contracts), 500))
    p["sizing"] = sizing if sizing else p.get("sizing")
    n = sizing.get("contracts") if sizing else None
    base.update({"pick": p, "contracts": n, "sizing_line": _W.sizing_line(p.get("sizing"))})
    as_of_raw = card.get("as_of")
    base["refresh_first"] = bool(clock.older_than_last_close(as_of_raw) and clock._us_session_open())
    ot = _svc("order_ticket")
    if ot is None:
        base["unavailable"] = "The order ticket text is not available on this build yet."
        base["footer"] = _footer_lines(p, ctx)
        return templates.TemplateResponse(request, "_options_ticket.html", base)
    rejection = ctx["rejection"] if ctx["rejected"] else None
    try:
        t = ot.build(p, ctx["setup"], prefs, dip=bool(dip), rejection=rejection, now=None)
        t = dict(t) if isinstance(t, dict) else t
        base.update({"ticket": t, "tws": ot.render(t, "tws"), "moomoo": ot.render(t, "moomoo")})
        if isinstance(t, dict) and t.get("refresh_first") is not None:
            base["refresh_first"] = base["refresh_first"] or bool(t.get("refresh_first"))
    except Exception as exc:  # noqa: BLE001
        refused_cls = getattr(ot, "TicketRefused", ())
        if refused_cls and isinstance(exc, refused_cls):
            base["refused"] = str(exc) or f"No ticket: earnings fall inside this trade and your rule says no."
        else:
            log.warning("order_ticket %s: %s", sym, exc, exc_info=True)
            base["unavailable"] = f"Could not build the ticket: {exc}"
    base["footer"] = _footer_lines(p, ctx)
    return templates.TemplateResponse(request, "_options_ticket.html", base)


def _footer_lines(p: dict, ctx: dict) -> dict:
    """The lines this panel puts under the broker text: collect / risk / breakeven /
    earnings, and the tracking-only sentence."""
    n = (p.get("sizing") or {}).get("contracts") or 1
    liq = p.get("liquidity") if isinstance(p.get("liquidity"), dict) else {}
    net = p.get("net")
    worst = liq.get("worst_fill")
    be = ", ".join(_g(b) for b in (p.get("breakevens") or []))
    e = ctx["earnings"]
    if e.get("date"):
        if e.get("inside"):
            earn = f"earnings {e['label']} is inside this expiry ({'defined-risk trades only' if e.get('allowed') else 'not allowed by your rule'})"
        else:
            earn = f"earnings {e['label']} is after this expiry"
    else:
        earn = "no earnings date on file - check before you trade"
    if p.get("is_credit") and net is not None:
        lo = round(-worst * 100 * n) if worst is not None else None
        hi = round(-net * 100 * n)
        money = (f"You collect {_money(lo)}-{_money(hi)} (worst likely fill to mid)" if lo is not None
                 else f"You collect about {_money(hi)}")
    elif net is not None:
        money = f"You pay about {_money(net * 100 * n)}"
    else:
        money = "You collect / pay: see the strike table"
    risk = _money((p.get("max_loss") or 0) * n)
    return {"line1": f"{money} · you risk {risk}" + (f" · breakeven {be}" if be else "") + f" · {earn}",
            "line2": "Tracking only: TradeHunter never sends an order."}


# ────────────────────────────────── routes: refresh, live ──────────────────────────────────

def _cooldown_hit(uid: int, sym: str) -> bool:
    now = time.monotonic()
    last = _cooldown.get((uid, sym))
    if last is not None and now - last < REFRESH_COOLDOWN_S:
        return True
    _cooldown[(uid, sym)] = now
    return False


@router.post("/refresh/{symbol}", response_class=HTMLResponse)
def refresh(symbol: str, request: Request, strategy: str = "",
            user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Read today's delayed chain now, store it, recompute the signal, re-render the
    card. 60 s cooldown per (member, ticker) - consulted ONLY when the stored as_of is
    from the current session; the ticket's 'Refresh first' press always goes through."""
    sym = _clean_symbol(symbol)
    if _reading(sym):                     # v4.131: never a second concurrent read of the same symbol
        ctx = _card_context(db, user, sym, strategy=strategy,
                            note=f"A first read of {sym} is already in progress - this card updates itself.",
                            note_kind="info")
        return templates.TemplateResponse(request, "_options_card.html", ctx)
    card = option_store.card_for(db, sym, user)
    if card and not clock.older_than_last_close(card.get("as_of")) and _cooldown_hit(user.id, sym):
        return _trigger(Response(status_code=429), _toast("Just refreshed - try again in a minute.", "info"))
    _cooldown[(user.id, sym)] = time.monotonic()
    nightly = _svc("option_nightly")
    od = _svc("option_data")
    kind = "ok"
    if nightly is None:
        note, kind = "Could not refresh: the delayed-chain reader is not available on this build yet. Showing the stored data.", "warn"
    else:
        try:
            nightly.refresh_symbol(db, sym, user)
            note = "Refreshed from Cboe · " + _W.et_clock()
            _clear_read_error(sym)
        except Exception as exc:  # noqa: BLE001 - ChainError or anything else: keep the stored data
            err_cls = getattr(od, "ChainError", ()) if od is not None else ()
            db.rollback()
            when = _fmt_as_of(card.get("as_of")) if card else "earlier"
            note = f"Could not refresh: {exc}. Showing the stored data from {when}."
            kind = "warn"
            if not (err_cls and isinstance(exc, err_cls)):
                log.warning("refresh %s: %s", sym, exc, exc_info=True)
    ctx = _card_context(db, user, sym, strategy=strategy, note=note, note_kind=kind)
    resp = templates.TemplateResponse(request, "_options_card.html", ctx)
    return _trigger(resp, {"options:basket-changed": {}})


def _b(v, lo, hi):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if lo <= f <= hi else None


@router.post("/live/{symbol}", response_class=HTMLResponse)
def live(symbol: str, payload: LiveIn, request: Request, strategy: str = "",
         user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Grade what the member's own TWS said, in this request, and show it - store
    nothing but the IV series (option_store.bootstrap_iv). The NLV sizes this request
    only; it is written by trade_prefs.write ONLY on the explicit [remember this] click."""
    sym = _clean_symbol(symbol)
    chain = payload.chain or {}
    diag = payload.diag or chain.get("diag")
    bridge_text = (f"Could not reach your IBKR bridge on this PC (127.0.0.1:{BRIDGE_PORT}). Start TWS and "
                   f"bridge\\start_ibkr_bridge.bat, then press Live again. The card still shows the delayed data.")
    if not chain.get("ok") or not chain.get("spot"):
        err = chain.get("error") or ""
        note = bridge_text if not err or "bridge" in err.lower() or "reach" in err.lower() else f"{bridge_text} ({err})"
        ctx = _card_context(db, user, sym, strategy=strategy, note=note, note_kind="bridge", diag=diag)
        return templates.TemplateResponse(request, "_options_card.html", ctx)

    live_d: dict = {"picks": None, "expiry": None, "at": clock.et_now().strftime("%H:%M ET"),
                    "nlv": _b(payload.nlv, 1, 1e12), "iv_rank": _b((payload.iv or {}).get("iv_rank"), 0, 100),
                    "iv_pct": _b((payload.iv or {}).get("iv_percentile"), 0, 100),
                    "bridge_old": False, "iv_note": None, "no_greeks": False, "bootstrap": None}
    notes = []
    od = _svc("option_data")
    prefs = option_prefs.read(db, user)
    card = option_store.card_for(db, sym, user, prefs=prefs)
    strat = strategy if strategy in strategy_rules.STRATEGY_KEYS else ((card or {}).get("recommended") or "")
    if od is not None and card is not None and strat:
        try:
            src = od.BridgePayloadSource(chain, diag=diag, symbol=sym)
            lc = src.chain if hasattr(src, "chain") else src.fetch_chain(sym)
            exps = lc.expiries() if hasattr(lc, "expiries") else sorted({r.expiry for r in lc.rows})
            live_d["expiry"] = exps[0] if exps else None
            live_d["no_greeks"] = not any(getattr(r, "delta", None) is not None for r in lc.rows)
            sp, ol = _svc("strike_picker"), _svc("opt_legs")
            if sp is not None and ol is not None and not live_d["no_greeks"]:
                view = ol.chain_view(lc, today=clock.et_today())
                res = sp.pick(strat, view, card.get("setup") or {}, card.get("iv") or {}, prefs, today=clock.et_today())
                res_picks = res.get("picks") if isinstance(res, dict) else getattr(res, "picks", None)
                live_d["picks"] = [dict(p) if isinstance(p, dict) else dict(vars(p)) for p in (res_picks or [])]
                if not live_d["picks"]:
                    deg = res.get("degenerate") if isinstance(res, dict) else getattr(res, "degenerate", None)
                    live_d["picks"] = [{"status": "none", "degenerate": deg, "legs": []}]
            elif live_d["no_greeks"]:
                notes.append("Your TWS sent quotes but no greeks (no option model yet). Delayed data kept for the picks; try Live again in a minute.")
            else:
                notes.append("The live strike picker is not available on this build yet; the delayed picks are shown.")
        except Exception as exc:  # noqa: BLE001
            log.warning("live %s: %s", sym, exc, exc_info=True)
            notes.append(f"Could not grade the live chain: {exc}. The card still shows the delayed data.")
    elif card is None:
        notes.append(f"No stored read for {sym} yet - press Refresh first so the live quotes have a chart read to grade against.")

    series = (payload.iv or {}).get("series")
    if series:
        try:
            live_d["bootstrap"] = option_store.bootstrap_iv(db, sym, series, source="ibkr")
            notes.append("A year of IV history from your TWS was filed (%d new days)." % live_d["bootstrap"]["inserted"])
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            notes.append(f"Could not file the IV history from your TWS: {exc}")
    else:
        live_d["bridge_old"] = True
        live_d["iv_note"] = f"Your bridge is older than {BRIDGE_MIN_VERSION} - restart bridge\\start_ibkr_bridge.bat"
    note = "Live from your TWS " + live_d["at"] + ("" if not notes else " · " + " ".join(notes))
    ctx = _card_context(db, user, sym, strategy=strat, note=note, note_kind="ok", live=live_d, diag=None)
    resp = templates.TemplateResponse(request, "_options_card.html", ctx)
    return _trigger(resp, {"options:basket-changed": {}})


# ────────────────────────────────── routes: rules ──────────────────────────────────

@router.get("/rules", response_class=HTMLResponse)
def rules(request: Request, tab: str = "shared", sym: str = "",
          user: User = Depends(require_user), db: Session = Depends(get_db)):
    sym = _clean_symbol(sym)
    atr = None
    if sym:
        card = option_store.card_for(db, sym, user)
        atr = ((card or {}).get("setup") or {}).get("atr")
    return _rules_response(request, _rules_context(db, user, tab, sym=sym, atr=atr))


@router.post("/rules", response_class=HTMLResponse)
async def rules_save(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Save one tab's fields (option_prefs.write); nlv / risk_pct / the credit exit
    lines ride along to trade_prefs.write. Re-renders the same tab with the override
    dots and sets HX-Trigger options:rules-changed {tab, hash, recompute}."""
    form = dict(await request.form())
    tab = form.get("tab") or "shared"
    tab = tab if tab in option_prefs.TABS else "shared"
    sym = _clean_symbol(form.get("sym") or "")
    before = option_prefs.prefs_hash(option_prefs.read(db, user))
    gate_before = (option_prefs.read(db, user).get("credit_vertical") or {}).get("iv_gate_min")
    if _trade_prefs_only(form, tab):
        # the '[remember this]' click (tab=shared&nlv=) and the Credit tab's exit lines
        # alone: option_prefs.write would read every absent checkbox of the tab as
        # False (chart_constraint off!), so a form with no rule field goes straight to
        # trade_prefs.write and the hash is left as it is
        _, err = tp.write(db, user, **{dst: form[src] for src, dst in option_prefs.TRADE_PREFS_FORM_KEYS.items()
                                       if src in form and str(form[src]).strip() != ""})
        errors = [err] if err else []
    else:
        _, errors = option_prefs.write(db, user, tab, form)
    ctx = _rules_context(db, user, tab, err=errors, msg=("" if errors else f"{option_prefs.TAB_LABELS[tab]} rules saved."),
                         sym=sym, hash_before=before)
    gate_after = (ctx["prefs"].get("credit_vertical") or {}).get("iv_gate_min")
    recompute = tab == "shared" or gate_before != gate_after
    return _rules_response(request, ctx, changed=not errors, recompute=recompute)


@router.post("/rules/reset", response_class=HTMLResponse)
def rules_reset(request: Request, tab: str = Form("all"), sym: str = Form(""),
                user: User = Depends(require_user), db: Session = Depends(get_db)):
    """tab= clears that tab's overrides; tab=all clears every block (telegram untouched).
    The credit tab's exit lines go back to trade_prefs defaults; nlv / risk_pct never."""
    before = option_prefs.prefs_hash(option_prefs.read(db, user))
    option_prefs.reset(db, user, None if tab == "all" else tab)
    if tab in ("credit", "all"):
        tp.write(db, user, roll_delta=tp.DEFAULT_ROLL_DELTA, loss_stop_pct=tp.DEFAULT_LOSS_STOP_PCT,
                 profit_target_pct=tp.DEFAULT_PROFIT_TARGET_PCT, dte_floor=tp.DEFAULT_DTE_FLOOR)
    show = "shared" if tab == "all" else tab
    ctx = _rules_context(db, user, show, msg=("Every rule is back to the house defaults." if tab == "all"
                                              else f"{option_prefs.TAB_LABELS.get(show, show)} rules are back to the house defaults."),
                         sym=_clean_symbol(sym), hash_before=before)
    return _rules_response(request, ctx, changed=True, recompute=(tab in ("shared", "all", "credit")))


# ────────────────────────────────── routes: telegram ──────────────────────────────────

@router.post("/telegram", response_class=HTMLResponse)
async def telegram_action(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Opt-in, the chat-id handshake (/start -> Send code -> Verify), quiet, pause, disable.
    Body {action, chat_id?, code?, pause_days?} as JSON or form-encoded; the reply is the
    re-rendered Shared tab."""
    ctype = request.headers.get("content-type", "")
    if "json" in ctype:
        try:
            body = dict(await request.json())
        except Exception:  # noqa: BLE001
            body = {}
    else:
        body = dict(await request.form())
    action = str(body.get("action") or "").strip()
    prefs = option_prefs.read(db, user)
    tg = dict(prefs.get("telegram") or option_prefs.TELEGRAM_DEFAULT)
    err: list[str] = []
    msg = ""
    tgm = _svc("telegram")
    now = _utcnow()
    if action == "request_code":
        chat_id = str(body.get("chat_id") or "").strip()
        if not chat_id or not chat_id.lstrip("-").isdigit():
            err.append("Enter the chat id the bot sent you after /start (a number).")
        elif tgm is None or not hasattr(tgm, "send_code"):
            err.append("Telegram is not available on this build yet.")
        else:
            try:
                code = str(tgm.send_code(chat_id))
                tg["pending"] = {"chat_id": chat_id, "code": code,
                                 "expires": (now + _dt.timedelta(minutes=CODE_TTL_MIN)).replace(tzinfo=None).isoformat()}
                msg = "A 6-digit code was sent to that chat. Type it below and press Verify."
            except Exception as exc:  # noqa: BLE001
                err.append(f"Could not send the code: {exc}")
    elif action == "verify":
        code = str(body.get("code") or "").strip()
        pending = tg.get("pending") or {}
        expires = pending.get("expires")
        expired = False
        if expires:
            try:
                expired = _dt.datetime.fromisoformat(str(expires)) < now.replace(tzinfo=None)
            except ValueError:
                expired = True
        if not code or not pending or expired or code != str(pending.get("code")):
            err.append("Enter the 6-digit code the bot sent you after /start.")
        else:
            tg.update({"chat_id": pending.get("chat_id"), "verified": True, "enabled": True, "pending": None})
            msg = "Telegram is on: each new idea is sent once, the morning it appears."
    elif action == "quiet":
        q = body.get("quiet")
        tg["quiet"] = (not tg.get("quiet")) if q is None else str(q).lower() in ("1", "true", "on", "yes")
        msg = "Quiet: ideas stay on the page." if tg["quiet"] else "Messages are on again."
    elif action == "pause":
        try:
            days = int(body.get("pause_days") or 7)
        except (TypeError, ValueError):
            days = 7
        days = max(1, min(days, 90))
        tg["paused_until"] = (clock.et_date() + _dt.timedelta(days=days)).isoformat()
        msg = f"Telegram ideas paused for {days} days."
    elif action == "disable":
        tg.update({"enabled": False, "pending": None})
        msg = "Telegram ideas are off."
    else:
        err.append("Unknown Telegram action.")
    if not err:
        _write_telegram(db, user, tg)
    return _rules_response(request, _rules_context(db, user, "shared", err=err, msg=msg))


# ────────────────────────────────── routes: track, positions ──────────────────────────────────

def _entry_meta(strategy: str, family: str, pick: dict, setup: dict) -> dict:
    """What the exit rules need at entry and nothing else has a column for."""
    atr = setup.get("atr") or 0.0
    rng = setup.get("rng") if isinstance(setup.get("rng"), dict) else {}
    if family == "condor" and rng.get("low") is not None and rng.get("high") is not None:
        pad = LEVEL_PAD_ATR * atr
        return {"range_low": rng["low"] - pad, "range_high": rng["high"] + pad}
    if strategy == "calendar":
        return {"entry_breakevens": list(pick.get("breakevens") or [])}
    if strategy == "diagonal_call":
        short = next((l for l in (pick.get("legs") or []) if l.get("side") == "sell"), None)
        return {"short_leg": {"expiry": short.get("expiry"), "strike": short.get("strike"), "delta": short.get("delta")}} if short else {}
    return {}


@router.post("/track-idea", response_class=HTMLResponse)
def track_idea(request: Request, symbol: str = Form(...), strategy: str = Form(...), pick: int = Form(0),
               contracts: int = Form(..., ge=1, le=500), note: str = Form(""),
               user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Record the chosen pick as a tracked trade in option_trades - every strategy,
    generic legs. The legs are re-read from the signal row (never trusted from the
    browser); a stale index is a 409; an earnings-inside rejection under none_inside is
    a 409; any other rejection is allowed with the sentence as the note's first line."""
    sym = _clean_symbol(symbol)
    ctx = _picks_context(db, user, sym, strategy=strategy, pick=pick)
    if ctx["card"] is None or strategy not in strategy_rules.STRATEGY_KEYS or not ctx["picks"] or pick != ctx["pick_i"]:
        return _trigger(Response(status_code=409), _toast("The strikes changed - look at the card again before tracking.", "err"))
    chosen = ctx["chosen"]
    if ctx["hide_strikes"]:
        date = ctx["earnings"].get("label") or ctx["earnings"].get("date") or "ahead"
        return _trigger(Response(status_code=409),
                        _toast(f"Earnings {date} fall inside this trade and your rule says no - nothing to track.", "err"))
    p = ctx["picks"][ctx["pick_i"]]
    family = option_prefs.family_of(strategy)
    legs = []
    for l in p.get("legs") or []:
        if not isinstance(l, dict):
            continue
        d = {k: l.get(k) for k in ("expiry", "right", "strike", "side", "qty", "price", "bid", "ask", "iv", "delta", "oi", "volume")}
        d.update({"entry_price": l.get("price"), "entry_delta": l.get("delta"), "entry_iv": l.get("iv")})
        legs.append(d)
    expiries = sorted({l["expiry"] for l in legs if l.get("expiry")})
    front = expiries[0] if expiries else (p.get("expiry") or "")
    back = expiries[-1] if len(expiries) > 1 else None
    setup = ctx["setup"]
    plan = setup.get("plan") or {}
    prefs = ctx["prefs"]
    note_text = (note or "").strip()
    if ctx["rejected"] and ctx["rejection"]:
        note_text = f"Not recommended: {ctx['rejection']}. " + note_text
    trade = OptionTrade(
        user_id=user.id, symbol=sym, strategy=strategy, family=family, legs=legs,
        front_expiry=front, back_expiry=back, net_entry=p.get("net") or 0.0, contracts=int(contracts),
        max_loss=p.get("max_loss"), chart_stop=p.get("chart_stop", plan.get("stop")),
        chart_target=plan.get("target") if family in DEBIT_FAMILIES else None,
        roll_dte=(prefs.get("leaps") or {}).get("roll_dte") if strategy in ("leaps_call", "diagonal_call") else None,
        paper=False, signal_id=ctx["card"].get("id"),
        earnings_date_at_entry=(ctx["iv"] or {}).get("earnings_date"),
        meta=_entry_meta(strategy, family, p, setup), note=note_text or None, status="open")
    db.add(trade)
    db.flush()
    ex = _svc("option_exits")
    if ex is not None:
        try:
            ch = option_store.latest_chain(db, sym)
            if ch is not None:
                ol = _svc("opt_legs")
                view = ol.chain_view(ch, today=clock.et_today()) if ol is not None else ch
                snap = ex.mark(trade, view, clock.et_today())
                verdict = ex.grade(trade, snap, setup, prefs, earnings=(ctx["iv"] or {}).get("earnings_date"))
                rec = getattr(ex, "record_check", None)
                if rec is not None:
                    rec(db, trade, snap, verdict, on=clock.et_today(), source=ch.source or "cboe")
                else:
                    db.add(OptionTradeCheck(trade_id=trade.id, checked_on=clock.et_today(),
                                            spot=snap.get("spot"), mark=snap.get("mark"), pl=snap.get("pl"),
                                            dte=snap.get("dte"), net_delta=snap.get("net_delta"),
                                            theta=snap.get("theta"), vega=snap.get("vega"), legs=snap.get("legs"),
                                            state=verdict.get("state") or "UNKNOWN", action=verdict.get("action"),
                                            reasons=verdict.get("reasons"), urgent=bool(verdict.get("urgent")),
                                            source=ch.source or "cboe"))
        except Exception as exc:  # noqa: BLE001 - the row is the point; the first check can wait for the sweep
            log.warning("track-idea first check %s: %s", sym, exc, exc_info=True)
    db.commit()
    ctx2 = _positions_context(db, user, status="open", focus=trade.id)
    resp = templates.TemplateResponse(request, "_options_positions_tab.html", ctx2, status_code=201)
    return _trigger(resp, {"options:tracked": {"id": trade.id},
                           **_toast(f"Tracking {sym} {p.get('label') or strategy_rules.LABELS.get(strategy, strategy)} - it is checked every day.", "ok")})


@router.get("/positions", response_class=HTMLResponse)
def positions(request: Request, status: str = "open", focus: int = 0,
              user: User = Depends(require_user), db: Session = Depends(get_db)):
    """The Positions tab over option_trades - every strategy, one template, stored
    checks only (the sweep wrote them; a trade tracked today carries its first check)."""
    return templates.TemplateResponse(request, "_options_positions_tab.html",
                                      _positions_context(db, user, status=status, focus=focus))


@router.post("/positions/{trade_id}/close", response_class=HTMLResponse)
def close_trade(trade_id: int, request: Request, reason: str = Form(""), status: str = Form("open"),
                user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Mark a trade closed here after closing it in the broker. Tracking only."""
    t = (db.query(OptionTrade).filter(OptionTrade.id == trade_id, OptionTrade.user_id == user.id).one_or_none())
    if t is not None and t.status == "open":
        t.status = "closed"
        t.closed_at = _utcnow()
        t.close_reason = (reason or "")[:24] or None
        db.commit()
    return templates.TemplateResponse(request, "_options_positions_tab.html",
                                      _positions_context(db, user, status=status))


# ────────────────────────────────── routes: badge, strip ──────────────────────────────────

@router.get("/badge")
def badge(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Nav badge JSON, exactly {run_on, finished_at, ok, errors, stale, running,
    job_missed, ideas_new, urgent, watch}. Stored checks only, never a quote."""
    return _badge_dict(db, user)


@router.get("/status/strip", response_class=HTMLResponse)
def status_strip(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """The honesty line: data age, delayed/live, job health. Polled every 300 s (every
    10 s while the job runs or a first-time read is in flight)."""
    d = _status_dict(db, user)
    d["is_admin"] = bool(getattr(user, "is_admin", False))
    resp = templates.TemplateResponse(request, "_options_status.html", d)
    if _take_done(user.id):                        # this member's first-time reads finished since the last poll
        resp = _trigger(resp, {"options:basket-changed": {}})
    return resp


def _run_nightly_in_background(symbols=None) -> None:
    """Start the full seven-step nightly (services/option_nightly.run_nightly) in a
    daemon thread with a session of its own - the job writes its own option_jobs row
    (job_runs.start), so the strip's 'running' state and the result come from the
    same place the scheduled task's do. Module-level so a test can stub it."""
    from ..db import SessionLocal
    from ..services import option_nightly

    def _work():
        db = SessionLocal()
        try:
            option_nightly.run_nightly(db, symbols=symbols, push=True)
        except Exception:  # noqa: BLE001 - the job logs its own per-ticker failures; this is the last resort
            logging.getLogger(__name__).exception("options nightly (started from the page) failed")
        finally:
            db.close()

    threading.Thread(target=_work, name="options-nightly-manual", daemon=True).start()


def _first_read_in_background(symbols, user_id: int | None = None) -> None:
    """The first-time read for freshly added tickers (v4.131; user, 2026-10-06: "all the
    data required to compute the result need to be backfilled the first time"): the IV
    history (the screener's readings, then a year from IB Gateway when it answers),
    today's delayed chain, the engines - services/option_backfill.first_read in a daemon
    thread with its own session. The symbols read as 'reading' meanwhile (the card polls
    itself, the basket row pulses, the strip counts them). Module-level so a test can
    stub it."""
    syms = [s for s in _clean_symbols(list(symbols or []), cap=MAX_BASKET) if s and not _reading(s)]
    if not syms:
        return
    started = time.monotonic()
    _mark_reading(syms, user_id, started)

    def _done(sym: str, status: str | None = None, err: str | None = None) -> None:
        _mark_done(sym, user_id, err=None if status == "ok" else (err or status or "the read did not finish"),
                   started=started)

    def _work():
        from ..db import SessionLocal
        from ..services import option_backfill

        db = SessionLocal()
        try:
            u = db.get(User, user_id) if user_id else None
            option_backfill.first_read(db, syms, u, on_done=_done)
        except Exception as exc:  # noqa: BLE001 - per-symbol failures are recorded inside; this is the last resort
            log.exception("options first read (%s) failed", ", ".join(syms))
            for s in syms:
                _done(s, "error", f"{type(exc).__name__}: {str(exc)[:160]}")
        finally:
            with _reg_lock:
                left = [s for s in syms if _first_reads.get(s, (None, None))[0] == started]
            for s in left:
                _done(s, "error", "the read did not finish")
            db.close()

    threading.Thread(target=_work, name="options-first-read", daemon=True).start()


@router.post("/admin/run-nightly", response_class=HTMLResponse)
def admin_run_nightly(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Run the data job NOW from the page (admin only; user, 2026-10-06: "can user run
    it manually through the interface?"). Same seven steps as the scheduled task, in
    the background; the strip re-polls every 10 s while it runs. A second press
    while one is running is refused with a notice, not a second job."""
    if not getattr(user, "is_admin", False):
        return Response(status_code=403)
    d = _status_dict(db, user)
    d["is_admin"] = True
    if job_runs.running(db, "nightly"):
        d["notice"] = "The data job is already running."
    else:
        _run_nightly_in_background()
        d["running"] = True
        d["state"] = d.get("state") if d.get("state") != "none" else "warn"
        d["notice"] = "Data job started - this line updates as it runs."
    return templates.TemplateResponse(request, "_options_status.html", d)


# ────────────────────────────────── member words (fallback) ──────────────────────────────────

class _Words:
    """option_words' functions when that module has landed; the D2.7 text as the
    fallback on a checkout without it. Every method tries the real module first."""

    @staticmethod
    def _real(name: str):
        ow = _svc("option_words")
        return getattr(ow, name, None) if ow is not None else None

    def chip_row(self, strategies: list) -> dict:
        """{"first": rec|None, "also_fits": [...], "greys": [<= 2 shown rejects], "rest": [...],
        "all": [...]} - the decision-9 order. option_words.chip_row writes each chip's
        words ({key, label, text, fit, reason_key, reason, shown, score}); the grouping
        is the page's."""
        rows = [s for s in (strategies or []) if isinstance(s, dict)]
        chips = None
        fn = self._real("chip_row")
        if fn is not None:
            try:
                chips = [c for c in fn(rows) if isinstance(c, dict)]
            except Exception:  # noqa: BLE001
                chips = None
        if chips is None:
            chips = []
            for s in rows:
                rk = s.get("reason_key")
                lab = s.get("label") or strategy_rules.LABELS.get(s.get("key"), str(s.get("key")))
                ct = strategy_rules.CHIP_TEXT.get(rk or "", "") if rk else ""
                if s.get("fit") == "recommended":
                    text = lab
                elif s.get("fit") == "also_fits":
                    text = lab + (" · not available yet" if rk == "not_available_yet" else "")
                else:
                    text = lab + (f" · {ct}" if ct else "")
                chips.append({"key": s.get("key"), "label": lab, "text": text, "fit": s.get("fit"),
                              "reason_key": rk, "reason": (s.get("reasons") or [None])[0],
                              "shown": bool(s.get("shown")), "score": s.get("score")})
        for c in chips:
            c.setdefault("chip_text", strategy_rules.CHIP_TEXT.get(c.get("reason_key") or "", ""))
        first = next((c for c in chips if c.get("fit") == "recommended"), None)
        also = [c for c in chips if c.get("fit") == "also_fits"]
        greys = [c for c in chips if c.get("fit") == "rejected" and c.get("shown")][:2]
        shown_keys = {c.get("key") for c in ([first] if first else []) + also + greys}
        rest = [c for c in chips if c.get("key") not in shown_keys]
        return {"first": first, "also_fits": also, "greys": greys, "rest": rest, "all": chips}

    def rules_line(self, prefs: dict, strategy: str, setup: dict | None) -> str | None:
        """The picks banner line when the picker stored none (option_words.rule_words
        over the member's merged rules and the stored setup)."""
        fn = self._real("rule_words")
        if fn is None or not strategy:
            return None
        try:
            chart = {"atr": (setup or {}).get("atr"), "setup": setup or {}, "tl": (setup or {}).get("tl")}
            return str(fn(prefs, strategy, chart))
        except Exception:  # noqa: BLE001
            return None

    def idea_short(self, key) -> str | None:
        fn = self._real("idea_short")
        if fn is not None:
            try:
                return fn(key)
            except Exception:  # noqa: BLE001
                pass
        if isinstance(key, dict):
            key = key.get("key")
        return IDEA_WORDS.get(key or "")

    def iv_rank_words(self, iv: dict | None) -> str:
        fn = self._real("iv_rank_words")
        if fn is not None:
            try:
                return str(fn(iv))
            except Exception:  # noqa: BLE001
                pass
        iv = iv or {}
        basis, n = iv.get("basis"), int(iv.get("iv_n") or 0)
        r = iv.get("iv_rank")
        if basis == "rank" and r is not None:
            return (f"IV rank {round(r)} over the last year ({n} days) - today's IV sits {round(r)}% of the way from the "
                    f"year's lowest to its highest. Above 50 options are expensive (sellers are paid); below 30 they are "
                    f"cheap by this stock's own standards.")
        if basis == "percentile":
            p = iv.get("iv_pct") if iv.get("iv_pct") is not None else r
            return (f"IV percentile {round(p) if p is not None else '?'} over the last {n} days (not a full year yet) - "
                    f"IV was lower than today on {round(p) if p is not None else '?'}% of those days.")
        if basis == "provisional":
            return f"IV {_g(iv.get('iv30'), 0)}% against {n} days of history - too short to rank; nothing here is firm yet."
        return (f"We cannot yet say whether options are expensive - {n} of 60 days of history. "
                f"If you have TWS on this PC, press Live to load a year.")

    def iv_hv_words(self, iv: dict | None) -> str | None:
        """'IV 46% vs realised 38% - options are priced for 21% more movement ...'."""
        fn = self._real("iv_hv_words")
        if fn is not None:
            try:
                out = fn(iv)
                return str(out) if out else None
            except Exception:  # noqa: BLE001
                pass
        iv = iv or {}
        if iv.get("iv30") is None or iv.get("hv20") is None:
            return None
        return f"IV {_g(iv['iv30'], 0)}% vs realised {_g(iv['hv20'], 0)}%"

    def gauge(self, iv: dict | None) -> str:
        fn = self._real("gauge")
        if fn is not None:
            try:
                g = fn(iv)
                if isinstance(g, dict):
                    return str(g.get("text") or g.get("line") or g.get("words") or g)
                return str(g)
            except Exception:  # noqa: BLE001
                pass
        iv = iv or {}
        basis, n = iv.get("basis"), int(iv.get("iv_n") or 0)
        r = iv.get("iv_rank") if iv.get("iv_rank") is not None else iv.get("iv_pct")
        verdict = iv.get("verdict") or "UNKNOWN"
        if basis in (None, "unknown") or verdict == "UNKNOWN":
            return (f"We cannot yet say whether options are expensive - {n} of 60 days of history. "
                    f"If you have TWS on this PC, press Live to load a year.")
        span = "over the last year" if basis == "rank" else f"against the last {n} days (not a full year yet)"
        word = {"SELL": "expensive", "BUY": "cheap", "NEUTRAL": "fairly priced"}.get(verdict, "fairly priced")
        verb = "are" if basis == "rank" else "look"
        head = f"Options {verb} {word} (IV rank {round(r)} {span})" if (basis == "rank" and r is not None) else f"Options {verb} {word} {span}"
        tail = ""
        if iv.get("iv30") is not None and iv.get("hv20") is not None:
            tail = f" · IV {_g(iv['iv30'], 0)}% vs realised {_g(iv['hv20'], 0)}%"
        return head + tail

    def earnings_words(self, date, inside: bool, expiry: str | None = None) -> str:
        """option_words.earnings_words(date, expiry) decides 'inside' from the expiry;
        when the engine already said inside (reason_key earnings_inside) and no expiry is
        known, the date itself stands in so the sentence still reads INSIDE."""
        fn = self._real("earnings_words")
        if fn is not None:
            try:
                return str(fn(date, expiry if expiry else (date if inside else None)))
            except Exception:  # noqa: BLE001
                pass
        if not date:
            return "No earnings date on file - check before you trade."
        lbl = strategy_rules.expiry_label(date)
        return (f"Earnings {lbl} falls INSIDE this expiry - the one thing a stop cannot protect you from."
                if inside else f"Earnings {lbl} is after this expiry.")

    def pop_words(self, pop, pop_kind: str) -> str:
        fn = self._real("pop_words")
        if fn is not None:
            try:
                return str(fn(pop, pop_kind))
            except Exception:  # noqa: BLE001
                pass
        p = round(float(pop) * 100) if pop is not None else "?"
        if pop_kind == "keep":
            return (f"About a {p}% chance of keeping the credit - an estimate from today's option prices (the short "
                    f"strike's delta), not a promise. Earnings, news and gaps are not in that number.")
        return (f"About a {p}% chance of profit if held to expiry, at today's volatility; this trade is managed by the "
                f"chart stop and target, so the real odds depend on the move, not this number.")

    def sizing_line(self, sizing: dict | None) -> str:
        fn = self._real("sizing_line")
        if fn is not None:
            try:
                return str(fn(sizing))
            except Exception:  # noqa: BLE001
                pass
        if not sizing or sizing.get("nlv") is None and sizing.get("contracts") is None:
            return "sized once you tell us the account value (My rules → Shared)"
        if sizing.get("line"):
            return str(sizing["line"])
        n = sizing.get("contracts")
        if n is None:
            return "sized once you tell us the account value (My rules → Shared)"
        if int(n) == 0:
            return "Not even one contract fits your 1% - lower the risk or choose a narrower spread"
        return (f"{n} contracts: about {_money(sizing.get('capital_at_risk_usd'))} if the stop fires, up to "
                f"{_money(sizing.get('max_loss_total_usd'))} ({_g(sizing.get('max_loss_pct_nlv'), 1)}% of your account) if the stock gaps past it")

    def et_clock(self, as_of=None) -> str:
        """A feed stamp as the member reads it ('02 Oct 16:04 ET'); the ET clock now
        when no stamp is given (the 'Refreshed from Cboe' note)."""
        if as_of is None:
            return clock.et_now().strftime("%H:%M ET")
        fn = self._real("et_clock")
        if fn is not None:
            try:
                return str(fn(as_of))
            except Exception:  # noqa: BLE001
                pass
        return _fmt_as_of(as_of)


_W = _Words()
