"""The Telegram ideas push - the ONE implementation (OPTIONS_MODULE_DESIGN.md
II.2.15; Part D §D4.2-D4.4; Part A §A4.4).

``run(db, as_of=run_on, dry_run=...)`` is step 6 of the nightly job: after the
per-symbol signals (1-3), ``option_exits.sweep`` (4) and ``option_store.prune`` (5),
before ``job_runs.finish`` (7). For every member who opted in (``prefs["telegram"]``:
enabled + verified, not quiet, not paused) it reads the member's OWN signal rows
through ``option_store.basket_rows_for`` (one batched query) and sends ONE message
per member - at most five ideas by score, the rest as "and N more on the page".

Guards (skip + log the reason): ``status != ok``, a partial chain (``iv_daily``),
a provisional / unknown IV basis, no earnings date on file, an unbuilt recommended
rule, a snapshot older than the last session, and - silently - no recommended
strategy or no real pick under the member's hash. Dedupe: one ``option_idea_push``
row per (member, ``symbol|strategy|front_expiry``); the same key is re-pushed only
when the short strike moved by more than one ATR (``setup.atr``), by UPDATING the
row. ``prune`` drops the rows after 45 days so a repeated setup months later is
pushed again.

What the text never does: state a contract count (sizing is a read-time,
per-member figure), name a greek, or use the words "step" / "phase". Every member
sentence is sentence-case plain language; the first line is verbatim on every
message. The handshake helpers (``request_code`` / ``verify`` / ``set_quiet`` /
``pause`` / ``disable``, dispatched by ``apply``) back ``POST /options/telegram``
and write ``prefs["telegram"]`` - its own key, never hashed.
"""
from __future__ import annotations

import copy
import datetime as _dt
import html as _html
import logging
import re

from ..config import settings
from ..models import (APPROVED, ROLE_ADMIN, IVDaily, OptionIdeaPush, User,
                      UserOptionPrefs, _utcnow)
from . import clock, option_prefs, option_store, telegram

log = logging.getLogger(__name__)

MAX_IDEAS = 5                 # per member per message, by score
REPUSH_ATR = 1.0              # the short strike must move more than this many ATRs
CODE_TTL = _dt.timedelta(minutes=telegram.CODE_TTL_MINUTES)
PAUSE_DAYS_DEFAULT = 7
PAUSE_DAYS_MAX = 90
_CHAT_ID = re.compile(r"^-?\d{4,20}$")

FIRST_LINE = ("Ideas for tonight's US session (opens 21:30 Malaysia). "
              "Prices are last night's close.")

# Skip reasons as the job log prints them (A4.3: "pushed 7, skipped 3 (earnings
# date unknown x2, provisional x1)").
SKIP_PARTIAL = "partial chain"
SKIP_PROVISIONAL = "IV history too short"
SKIP_EARNINGS = "earnings date unknown"
SKIP_UNBUILT = "not available yet"
SKIP_STALE = "stale"
SKIP_SENT = "already sent"

# The basket column's idea words (D2.7); option_words.idea_short is preferred
# when that module has landed, this table is the fallback so the push never
# prints a strategy key.
_IDEA_WORDS = {"bull_put": "sell put", "bear_call": "sell call", "buy_call": "buy call",
               "buy_put": "buy put", "bull_call": "call sprd", "bear_put": "put sprd",
               "leaps_call": "LEAPS", "iron_condor": "condor", "calendar": "calendar",
               "diagonal_call": "diagonal"}
_TREND_WORDS = {"up": "↗ uptrend", "down": "↘ downtrend", "sideways": "↔ sideways"}
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

_warned_unconfigured = False


# ────────────────────────────────────── small helpers ──────────────────────────────────────

def idea_key(symbol: str, strategy: str, front_expiry: str) -> str:
    """``'LRCX|bull_put|2026-11-20'`` - (symbol, strategy, front expiry) defines the
    idea. The same thesis with the short strike drifting one listed strike a day is
    NOT a new idea; a new expiry or a new strategy is. ``as_of`` is deliberately
    not in the key."""
    return f"{str(symbol).strip().upper()}|{strategy}|{str(front_expiry)[:10]}"


def _current_step() -> int:
    try:
        from . import strategy_rules  # noqa: PLC0415 - lazy: the rule table may be mid-build
        return int(getattr(strategy_rules, "CURRENT_STEP", 1))
    except Exception:  # noqa: BLE001
        return 1


def _idea_word(key: str) -> str:
    try:
        from . import option_words  # noqa: PLC0415 - lazy by design
        w = option_words.idea_short(key)
        if w:
            return str(w)
    except Exception:  # noqa: BLE001 - not landed, or a key it does not know
        pass
    return _IDEA_WORDS.get(key, key.replace("_", " "))


def _date_label(d: str | None) -> str:
    """``'2026-11-20' -> 'Nov 20'``; an unparsable value comes back as given."""
    try:
        dd = _dt.date.fromisoformat(str(d)[:10])
        return f"{_MONTHS[dd.month - 1]} {dd.day}"
    except (TypeError, ValueError):
        return str(d or "")


def _esc(text) -> str:
    """Text for Telegram's HTML parse mode: only ``<``, ``>`` and ``&`` are escaped;
    quotes and apostrophes stay as written (the first line is verbatim)."""
    return _html.escape(str(text), quote=False)


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _money(v: float) -> str:
    return f"${abs(v):,.0f}"


def _recommended_row(strategies) -> dict | None:
    for s in strategies or []:
        if isinstance(s, dict) and s.get("fit") == "recommended":
            return s
    return None


def _first_pick(picks, key: str | None) -> dict | None:
    if not key or not isinstance(picks, dict):
        return None
    for p in picks.get(key) or []:
        if isinstance(p, dict) and p.get("status", "ok") == "ok":
            return p
    return None


def _short_strike(pick: dict) -> float | None:
    """The strike the dedupe watches: the first SOLD leg (credit spreads), else the
    first leg (a plain long option has no short leg)."""
    legs = pick.get("legs") or []
    for leg in legs:
        if isinstance(leg, dict) and leg.get("side") == "sell":
            return _num(leg.get("strike"))
    for leg in legs:
        if isinstance(leg, dict):
            return _num(leg.get("strike"))
    return None


def _front_expiry(pick: dict) -> str | None:
    if pick.get("expiry"):
        return str(pick["expiry"])[:10]
    exps = sorted({str(l.get("expiry"))[:10] for l in (pick.get("legs") or []) if isinstance(l, dict) and l.get("expiry")})
    return exps[0] if exps else None


def _is_paused(tg: dict, today: str) -> bool:
    until = tg.get("paused_until")
    return bool(until) and str(today)[:10] < str(until)[:10]


# ─────────────────────────────────────── the members ───────────────────────────────────────

def _opted_in(db, today: str) -> list[tuple[User, dict, str]]:
    """``(user, prefs, chat_id)`` for every approved member whose Telegram switch is
    on and verified (an administrator with a blank chat id falls back to the
    vault's id), not quiet, not paused."""
    out: list[tuple[User, dict, str]] = []
    rows = (db.query(UserOptionPrefs, User)
              .join(User, User.id == UserOptionPrefs.user_id)
              .filter(User.status == APPROVED)
              .order_by(UserOptionPrefs.user_id)
              .all())
    for _row, user in rows:
        prefs = option_prefs.read(db, user)
        tg = prefs.get("telegram") or {}
        if not tg.get("enabled") or tg.get("quiet") or _is_paused(tg, today):
            continue
        chat_id = str(tg.get("chat_id") or "").strip()
        if chat_id and not tg.get("verified"):
            continue
        if not chat_id:
            if getattr(user, "role", None) != ROLE_ADMIN:
                continue
            chat_id = str(telegram.default_chat_id() or "").strip()
            if not chat_id:
                continue
        out.append((user, prefs, chat_id))
    return out


# ─────────────────────────────────────── the ideas ───────────────────────────────────────

def _ideas_for(db, user, prefs: dict, *, as_of: str, skipped: dict[str, int]) -> list[dict]:
    """Every idea this member may be sent tonight, after the guards and the
    dedupe, UNSORTED. Each dict: symbol, key, strategy, row (the signal dict),
    rec (the recommended strategies row), pick, score, short_strike, atr,
    existing (the OptionIdeaPush row to update, or None)."""
    phash = option_prefs.prefs_hash(prefs)
    rows = option_store.basket_rows_for(db, user, prefs=prefs, prefs_hash=phash,
                                        house_hash=option_prefs.HOUSE_HASH)
    last_session = clock.last_trading_day(as_of).isoformat()
    ideas: list[dict] = []
    for sym, r in rows.items():
        status = r.get("status")
        if status and status != "ok":
            skipped[status] = skipped.get(status, 0) + 1
            log.info("telegram %s: skipped: %s", sym, status)
            continue
        if r.get("pick_state") != "has_picks" or not r.get("idea") or not r.get("snap_on"):
            continue                                            # silent: nothing for this hash
        sig = option_store.signal(db, sym, r["snap_on"], phash)
        if sig is None or sig.status != "ok":
            continue
        hdr = (db.query(IVDaily).filter(IVDaily.symbol == sym, IVDaily.on == sig.snap_on)
                 .one_or_none())
        if hdr is not None and hdr.partial:
            skipped[SKIP_PARTIAL] = skipped.get(SKIP_PARTIAL, 0) + 1
            log.info("telegram %s: skipped: %s", sym, SKIP_PARTIAL)
            continue
        iv = sig.iv if isinstance(sig.iv, dict) else {}
        if iv.get("provisional") or iv.get("basis") in ("provisional", "unknown", None):
            skipped[SKIP_PROVISIONAL] = skipped.get(SKIP_PROVISIONAL, 0) + 1
            log.info("telegram %s: skipped: %s", sym, SKIP_PROVISIONAL)
            continue
        if not iv.get("earnings_date"):
            skipped[SKIP_EARNINGS] = skipped.get(SKIP_EARNINGS, 0) + 1
            log.info("telegram %s: skipped: %s", sym, SKIP_EARNINGS)
            continue
        rec = _recommended_row(sig.strategies)
        if rec is None:
            continue
        if int(rec.get("step") or 1) > _current_step():
            skipped[SKIP_UNBUILT] = skipped.get(SKIP_UNBUILT, 0) + 1
            log.info("telegram %s: skipped: %s", sym, SKIP_UNBUILT)
            continue
        if str(sig.snap_on) < last_session:
            skipped[SKIP_STALE] = skipped.get(SKIP_STALE, 0) + 1
            log.info("telegram %s: skipped: %s (snapshot %s, last session %s)",
                     sym, SKIP_STALE, sig.snap_on, last_session)
            continue
        pick = _first_pick(sig.picks, rec.get("key"))
        front = _front_expiry(pick) if pick else None
        if pick is None or not front:
            continue
        setup = sig.setup if isinstance(sig.setup, dict) else {}
        key = idea_key(sym, rec["key"], front)
        strike = _short_strike(pick)
        atr = _num(setup.get("atr"))
        existing = (db.query(OptionIdeaPush)
                      .filter(OptionIdeaPush.user_id == user.id, OptionIdeaPush.idea_key == key)
                      .one_or_none())
        if existing is not None:
            moved = (strike is not None and existing.short_strike is not None and atr
                     and abs(strike - float(existing.short_strike)) > REPUSH_ATR * atr)
            if not moved:
                skipped[SKIP_SENT] = skipped.get(SKIP_SENT, 0) + 1
                continue
        score = _num(rec.get("score"))
        if score is None:
            score = _num(pick.get("score")) or 0.0
        ideas.append({"symbol": sym, "key": key, "strategy": rec["key"], "row": sig, "rec": rec,
                      "pick": pick, "score": score, "short_strike": strike, "atr": atr,
                      "existing": existing, "setup": setup, "iv": iv})
    return ideas


# ─────────────────────────────────────── the text ───────────────────────────────────────

def _iv_words(iv: dict) -> str:
    basis = iv.get("basis")
    n = iv.get("iv_n")
    rank, pct = _num(iv.get("iv_rank")), _num(iv.get("iv_pct"))
    if basis == "rank" and rank is not None:
        return f"IV rank {rank:.0f} over the last year"
    measure = rank if rank is not None else pct
    if measure is None:
        return "IV not ranked"
    span = f" over the last {int(n)} days" if n else ""
    return f"IV percentile {measure:.0f}{span}"


def _pick_label(pick: dict) -> str:
    """``'Nov 20 330/320 put'`` - the first leg's expiry, every strike in leg order,
    the right of the first leg (a condor reads ``'... 300/310/380/390 put/call'``)."""
    legs = [l for l in (pick.get("legs") or []) if isinstance(l, dict)]
    strikes = "/".join(f"{_num(l.get('strike')):g}" for l in legs if _num(l.get("strike")) is not None)
    rights = []
    for l in legs:
        w = "call" if str(l.get("right", "")).upper().startswith("C") else "put"
        if w not in rights:
            rights.append(w)
    return f"{_date_label(_front_expiry(pick))} {strikes} {'/'.join(rights)}".strip()


def _money_line(pick: dict) -> str:
    net = _num(pick.get("net"))
    parts = []
    if net is not None:
        per = abs(net) * 100.0
        parts.append(("collect ≈ " if net < 0 else "pay ≈ ") + _money(per))
    ml = _num(pick.get("max_loss"))
    if ml is not None:
        parts.append("risk " + _money(ml))
    pop = _num(pick.get("pop"))
    if pop is not None:
        p = round(pop * 100)
        word = "keeping it" if pick.get("pop_kind", "keep") == "keep" else "profit"
        parts.append(f"about {p}% chance of {word} (estimate)")
    return " · ".join(parts)


def _stop_line(idea: dict) -> str:
    setup, pick, iv = idea["setup"], idea["pick"], idea["iv"]
    sym = idea["symbol"]
    plan = setup.get("plan") if isinstance(setup.get("plan"), dict) else {}
    stop = _num(plan.get("stop"))
    if stop is None:
        stop = _num(setup.get("stop"))
    if stop is None:
        stop = _num(pick.get("chart_stop"))
    bits = []
    if stop is not None:
        side = "over" if setup.get("direction") in ("short", "down") else "under"
        cost = _num(pick.get("chart_stop_pl"))
        cost_s = f" (about −{_money(cost)} today)" if cost is not None and cost < 0 else ""
        bits.append(f"Stop: {sym} {side} {stop:g}{cost_s}")
    e_date = iv.get("earnings_date")
    if e_date:
        front = _front_expiry(pick) or ""
        where = "inside this trade" if str(e_date)[:10] <= front else "after this trade"
        bits.append(f"earnings {_date_label(e_date)} {where}")
    return " · ".join(bits)


def idea_block(idea: dict) -> str:
    """One idea as HTML lines (D4.4): the symbol line, the stored headline, the first
    pick in collect / risk / chance words, the What-has-to-happen line, the stop
    with its T+0 cost and the earnings flag, the deep link. Never a contract count."""
    sig, rec, pick = idea["row"], idea["rec"], idea["pick"]
    sym = idea["symbol"]
    trend = _TREND_WORDS.get(str(sig.trend or ""), "no clear trend")
    url = f"{settings.public_url}/options?symbol={sym}"
    lines = [f"<b>{_esc(sym)}</b> {trend} · {_esc(_iv_words(idea['iv']))} "
             f"→ <b>{_esc(_idea_word(rec['key']))}</b>"]
    if sig.headline:
        lines.append(_esc(str(sig.headline)))
    lines.append(_esc(f"{_pick_label(pick)} · {_money_line(pick)}"))
    if rec.get("must_happen"):
        lines.append(_esc(f"What has to happen: {rec['must_happen']}"))
    stop = _stop_line(idea)
    if stop:
        lines.append(_esc(stop))
    lines.append(f'<a href="{_html.escape(url, quote=True)}">open the card</a>')
    return "\n".join(lines)


def compose(ideas: list[dict], *, as_of: str, more: int = 0) -> str:
    """The whole message for one member: the fixed first line, a count line, the
    idea blocks (already capped and sorted by the caller), then "and N more on the
    page" and the pause link."""
    n = len(ideas) + more
    head = [f"<b>{_esc(FIRST_LINE)}</b>",
            f"Options · {n} new idea{'s' if n != 1 else ''} · {_date_label(as_of)}"]
    blocks = [idea_block(i) for i in ideas]
    pause = f'<a href="{_html.escape(settings.public_url + "/options?pause=7", quote=True)}">pause for 7 days</a>'
    tail = (f"and {more} more on the page · {pause}") if more > 0 else pause
    return "\n\n".join(["\n".join(head)] + blocks + [tail])


# ─────────────────────────────────────── the run ───────────────────────────────────────

def run(db, *, as_of: str, dry_run: bool = False) -> dict:
    """Send tonight's ideas to every opted-in member (D4.3). ``as_of`` is the run's
    ET date (``run_on``). Returns ``{members, ideas, sent, failed, skipped}`` -
    ``members`` = opted-in members considered, ``ideas`` = ideas composed,
    ``sent`` = ideas delivered (0 on a dry run), ``failed`` = ideas whose message
    did not go out, ``skipped`` = ``{reason: n}``. Soft-fail per member. A dry run
    composes and logs the message and writes the ``option_idea_push`` rows with
    ``error='dry-run'`` so the next run still dedupes. Not configured (and not a
    dry run) -> logged once, nothing done."""
    global _warned_unconfigured
    out = {"members": 0, "ideas": 0, "sent": 0, "failed": 0, "skipped": {}}
    if not dry_run and not telegram.configured():
        if not _warned_unconfigured:
            log.warning("telegram: not configured (no bot token in the vault) - ideas push skipped")
            _warned_unconfigured = True
        out["skipped"]["not configured"] = 1
        return out
    as_of = str(as_of)[:10]
    try:
        telegram.answer_starts(db)
    except Exception as exc:  # noqa: BLE001 - never blocks the push
        log.warning("telegram /start answerer failed: %s", exc)
    members = _opted_in(db, as_of)
    out["members"] = len(members)
    skipped: dict[str, int] = out["skipped"]
    for user, prefs, chat_id in members:
        try:
            ideas = _ideas_for(db, user, prefs, as_of=as_of, skipped=skipped)
            if not ideas:
                continue
            ideas.sort(key=lambda i: (-(i["score"] or 0.0), i["symbol"]))
            batch, rest = ideas[:MAX_IDEAS], ideas[MAX_IDEAS:]
            html = compose(batch, as_of=as_of, more=len(rest))
            out["ideas"] += len(batch)
            if dry_run:
                ok, err = False, "dry-run"
                log.info("telegram dry-run for user %s (%d idea(s), %d more):\n%s",
                         user.id, len(batch), len(rest), html)
            else:
                ok, err = telegram.send(html, chat_id=chat_id)
                if ok:
                    out["sent"] += len(batch)
                else:
                    out["failed"] += len(batch)
                    log.warning("telegram push to user %s failed: %s", user.id, err)
            now = _utcnow()
            for idea in batch:
                row = idea["existing"]
                if row is None:
                    row = OptionIdeaPush(user_id=user.id, symbol=idea["symbol"], idea_key=idea["key"])
                    db.add(row)
                row.short_strike = idea["short_strike"]
                row.atr = idea["atr"]
                row.score = idea["score"]
                row.sent_at = now
                row.ok = bool(ok)
                row.error = None if ok else (err or None)
            db.commit()
        except Exception:  # noqa: BLE001 - the next member still gets theirs
            log.exception("telegram push for user %s failed", getattr(user, "id", "?"))
            db.rollback()
    log.info("telegram: %d member(s), %d idea(s), sent %d, failed %d, skipped %s",
             out["members"], out["ideas"], out["sent"], out["failed"],
             ", ".join(f"{k} x{v}" for k, v in sorted(skipped.items())) or "none")
    return out


def ideas_new(db, user, seen_at) -> int:
    """The badge's ``ideas_new``: this member's push rows with ``sent_at >=
    seen_at`` (``prefs["options_seen_at"]``, an ISO string or a datetime; None =
    every row)."""
    if user is None:
        return 0
    q = db.query(OptionIdeaPush).filter(OptionIdeaPush.user_id == user.id)
    if seen_at:
        ts = seen_at
        if isinstance(ts, str):
            try:
                ts = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                ts = None
        if isinstance(ts, _dt.datetime):
            if ts.tzinfo is not None:
                ts = ts.astimezone(_dt.timezone.utc).replace(tzinfo=None)
            q = q.filter(OptionIdeaPush.sent_at >= ts)
    return int(q.count())


# ─────────────────────────────────── the handshake (POST) ───────────────────────────────────

def _telegram_prefs(db, user) -> tuple[dict, dict]:
    """``(stored prefs dict, telegram dict)`` - a deep copy of the member's raw row
    JSON (so no caller ever mutates the ORM-loaded value in place, which SQLAlchemy
    would not see) and its ``telegram`` key merged over the default."""
    row = option_prefs._row_of(user)
    stored = copy.deepcopy((getattr(row, "prefs", None) if row is not None else None) or {})
    tg = {**option_prefs.TELEGRAM_DEFAULT, **(stored.get("telegram") if isinstance(stored.get("telegram"), dict) else {})}
    return stored, tg


def _write_telegram(db, user, tg: dict) -> dict:
    """Store ``tg`` under the member's ``prefs["telegram"]`` (through
    ``option_prefs._store`` so the blocks, the hash and the schema version are
    kept as they are; the key is never hashed). Returns the stored dict."""
    stored, _ = _telegram_prefs(db, user)
    stored["telegram"] = {k: tg.get(k) for k in option_prefs.TELEGRAM_DEFAULT}
    option_prefs._store(db, user, option_prefs._row_of(user), stored)
    return dict(stored["telegram"])


def request_code(db, user, chat_id: str | None) -> tuple[dict, str | None]:
    """Send a 6-digit code to the typed chat id and remember it as ``pending``
    (10 minutes). Returns ``(telegram dict, error)``; the dict is unchanged when
    ``error`` is set."""
    _, tg = _telegram_prefs(db, user)
    cid = str(chat_id or "").strip()
    if not _CHAT_ID.match(cid):
        return tg, "Enter the chat id the bot sent you after /start (digits only)."
    if not telegram.configured():
        return tg, "Telegram is not set up on this server yet."
    try:
        code = telegram.send_code(cid)
    except telegram.TelegramError as exc:
        log.warning("telegram send_code to %s failed: %s", cid, exc)
        return tg, "Could not send a code to that chat id. Send /start to the TradeHunter bot first, then try again."
    expires = (_dt.datetime.now(_dt.timezone.utc) + CODE_TTL).isoformat(timespec="seconds")
    tg["pending"] = {"chat_id": cid, "code": code, "expires": expires}
    return _write_telegram(db, user, tg), None


def verify(db, user, code: str | None) -> tuple[dict, str | None]:
    """Compare the typed code with ``pending``; on a match the pending chat id
    becomes the member's, ``verified`` and ``enabled`` are set together and the
    pending code is cleared."""
    _, tg = _telegram_prefs(db, user)
    pending = tg.get("pending") if isinstance(tg.get("pending"), dict) else None
    if not pending:
        return tg, "Press Send code first, then enter the 6-digit code the bot sent you."
    typed = re.sub(r"\D", "", str(code or ""))
    try:
        exp = _dt.datetime.fromisoformat(str(pending.get("expires")))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=_dt.timezone.utc)
    except (TypeError, ValueError):
        exp = None
    if exp is None or _dt.datetime.now(_dt.timezone.utc) > exp:
        return tg, "That code has expired. Press Send code again."
    if not typed or typed != str(pending.get("code") or ""):
        return tg, "That code does not match. Enter the 6-digit code the bot sent you after /start."
    tg.update({"chat_id": str(pending.get("chat_id")), "verified": True, "enabled": True,
               "pending": None})
    return _write_telegram(db, user, tg), None


def set_quiet(db, user, quiet: bool) -> tuple[dict, str | None]:
    """The quiet switch: the opt-in stays, nothing is sent while it is on."""
    _, tg = _telegram_prefs(db, user)
    tg["quiet"] = bool(quiet)
    return _write_telegram(db, user, tg), None


def pause(db, user, days: int | None) -> tuple[dict, str | None]:
    """Pause the ideas for ``days`` (1..90, default 7): ``paused_until`` = the ET
    date that many days ahead."""
    _, tg = _telegram_prefs(db, user)
    try:
        n = int(days) if days is not None else PAUSE_DAYS_DEFAULT
    except (TypeError, ValueError):
        return tg, "Enter how many days to pause for (1 to 90)."
    if n < 1 or n > PAUSE_DAYS_MAX:
        return tg, "Enter how many days to pause for (1 to 90)."
    until = clock.et_date() + _dt.timedelta(days=n)
    tg["paused_until"] = until.isoformat()
    return _write_telegram(db, user, tg), None


def disable(db, user) -> tuple[dict, str | None]:
    """Unticking the switch: ``enabled`` off and any pending code dropped; the chat
    id and its verification are kept so the member can switch back on by
    verifying again."""
    _, tg = _telegram_prefs(db, user)
    tg.update({"enabled": False, "pending": None})
    return _write_telegram(db, user, tg), None


ACTIONS = ("request_code", "verify", "quiet", "pause", "disable")


def apply(db, user, action: str, *, chat_id: str | None = None, code: str | None = None,
          pause_days=None, quiet=None) -> tuple[dict, str | None]:
    """Dispatch ``POST /options/telegram``'s body ``{action, chat_id?, code?,
    pause_days?}`` (II.2.15). ``quiet`` toggles when not given explicitly."""
    a = str(action or "").strip().lower()
    if a == "request_code":
        return request_code(db, user, chat_id)
    if a == "verify":
        return verify(db, user, code)
    if a == "quiet":
        _, tg = _telegram_prefs(db, user)
        if quiet is None:
            quiet = not tg.get("quiet")
        elif isinstance(quiet, str):
            quiet = quiet.strip().lower() in ("1", "true", "yes", "on", "y", "t")
        return set_quiet(db, user, bool(quiet))
    if a == "pause":
        return pause(db, user, pause_days)
    if a == "disable":
        return disable(db, user)
    _, tg = _telegram_prefs(db, user)
    return tg, "That Telegram action is not one we know."
