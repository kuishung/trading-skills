"""Option setups shared from the Option Finder (v4.279).

The user (2026-11-27): *"in option finder, i want a right click on the tiles or the table item it will give the
moderator and above (moderator and administrator) to be able to send the studies to telegram and discord the
setup. I will give user the link so that when user open the link, it will go to tradehunter to the page. In the
studies, i need also the snapshot of the chart showing its Entry, SL, PT and also the detail of the selected
strike so that user reading the telegram and discord know the setup and can discuss on it."*

One shared setup = an ``OptionStudy`` row (the call, the stock prices it was judged against, the finder's
figures for it, the moderator's note), a PNG snapshot of the finder's chart with the Entry / Target / Worst
lines (``state/snapshots/option_<id>.png``, never in git), and a ``Discussion`` of kind ``option`` opened on
Discord and Telegram with the picture and the details - the members' replies come back through the
discussions layer and are read on the setup's page (``/setups/option/<id>``) and on the Curated page's
Option setups panel.
"""
from __future__ import annotations

import base64
import datetime as _dt
import logging
import re
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import Discussion, OptionStudy, User
from . import discussions

log = logging.getLogger(__name__)

SNAP_DIR = Path(__file__).resolve().parents[2] / "state" / "snapshots"
MAX_IMAGE = 4 * 1024 * 1024          # a chart PNG is ~100-400 KB; anything bigger is not a chart
_DATA_URL = re.compile(r"^data:image/png;base64,(?P<b64>[A-Za-z0-9+/=\s]+)$")

FIGURE_KEYS = ("dte", "iv", "oi", "cost", "at_target", "at_worst", "breakeven", "be_pct", "rr", "roi", "prob", "no_loss")


class StudyError(ValueError):
    """A share request that cannot be saved - the message is for the member."""


def decode_image(data_url: str | None) -> bytes | None:
    """The chart snapshot the page took (``canvas.toDataURL('image/png')``) as PNG bytes; None when absent,
    not a PNG data URL, or bigger than ``MAX_IMAGE``."""
    if not data_url or not isinstance(data_url, str):
        return None
    m = _DATA_URL.match(data_url.strip())
    if not m:
        return None
    try:
        raw = base64.b64decode(m.group("b64"), validate=False)
    except Exception:  # noqa: BLE001
        return None
    if not raw.startswith(b"\x89PNG") or len(raw) > MAX_IMAGE:
        return None
    return raw


def _f(v, *, name: str, required: bool = False) -> float | None:
    if v is None or v == "":
        if required:
            raise StudyError(f"{name} is missing")
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise StudyError(f"{name} is not a number") from None
    if x != x or x in (float("inf"), float("-inf")):
        raise StudyError(f"{name} is not a number")
    return x


def _date(v, *, name: str, required: bool = False) -> str | None:
    s = (str(v) if v is not None else "").strip()
    if not s:
        if required:
            raise StudyError(f"{name} is missing")
        return None
    try:
        return _dt.date.fromisoformat(s[:10]).isoformat()
    except ValueError:
        raise StudyError(f"{name} is not a date") from None


def snapshot_path(s: OptionStudy) -> Path | None:
    if not s.snapshot:
        return None
    p = SNAP_DIR / Path(s.snapshot).name
    return p if p.is_file() else None


def strike_text(k: float | None) -> str:
    if k is None:
        return "-"
    return ("%d" % k) if float(k).is_integer() else ("%.2f" % k).rstrip("0").rstrip(".")


def _expiry_words(exp: str | None) -> str:
    try:
        return _dt.date.fromisoformat(exp).strftime("%b %d, %Y").replace(" 0", " ")
    except (TypeError, ValueError):
        return exp or "-"


def title_of(s: OptionStudy) -> str:
    return f"{s.symbol} {strike_text(s.strike)} {'put' if s.right == 'P' else 'call'} {_expiry_words(s.expiry)}"


def _pct(a: float | None, b: float | None) -> str:
    if a is None or not b:
        return ""
    return " (%+.1f%%)" % ((a - b) / b * 100)


def _money(v) -> str:
    return "-" if v is None else "%.2f" % float(v)


def _pct1(v, dp: int = 0) -> str:
    return "-" if v is None else ("%." + str(dp) + "f%%") % (float(v) * 100)


def details_lines(s: OptionStudy) -> list[tuple[str, str]]:
    """The setup as (label, value) pairs - the page's table and the chat opener share them."""
    f = s.figures or {}
    rows = [
        ("Stock entry", f"{_money(s.entry)}" + (" (last price)" if s.entry_src != "user" else " (chosen)")),
        ("Target", f"{_money(s.target)}{_pct(s.target, s.entry)}"),
        ("Worst case", f"{_money(s.worst)}{_pct(s.worst, s.entry)}"),
        ("Valued on", (s.valued_on or "-") + (" (expiry)" if s.mode == "expiry" else "")),
        ("Contract", f"{strike_text(s.strike)} {'put' if s.right == 'P' else 'call'}, expires {s.expiry}"
                     + (f" ({f.get('dte')} days)" if f.get("dte") is not None else "")),
        ("Implied volatility", _pct1(f.get("iv"), 1)),
        ("Open interest", "-" if f.get("oi") is None else str(int(f["oi"]))),
        ("Cost today", _money(f.get("cost"))),
        ("At target / at worst", f"{_money(f.get('at_target'))} / {_money(f.get('at_worst'))}"),
        ("Breakeven", _money(f.get("breakeven")) + (f" ({float(f['be_pct']) * 100:+.1f}%)" if f.get("be_pct") is not None else "")),
        ("Reward / risk", "no loss" if f.get("no_loss") else ("-" if f.get("rr") is None else "%.2f" % float(f["rr"]))),
        ("ROI at target", _pct1(f.get("roi"))),
        ("Chance above breakeven", _pct1(f.get("prob"))),
    ]
    return rows


def details_text(s: OptionStudy) -> str:
    lines = [f"{k}: {v}" for k, v in details_lines(s)]
    if s.note:
        lines = [s.note.strip(), ""] + lines
    return "\n".join(lines)


def page_url(s: OptionStudy) -> str:
    return f"/setups/option/{s.id}"


def create(db: Session, user: User, payload: dict, *, image: bytes | None, channels=discussions.CHANNELS) -> OptionStudy:
    """Save the setup, its snapshot and open its discussion on the asked-for chats. Raises ``StudyError`` on a
    bad payload; a chat that refuses is recorded in the discussion's ``channel_errors`` (never raised)."""
    sym = str(payload.get("symbol") or "").strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", sym):
        raise StudyError("the ticker is missing")
    right = "P" if str(payload.get("right") or "C").upper().startswith("P") else "C"
    figs_in = payload.get("figures") or {}
    figures = {k: figs_in.get(k) for k in FIGURE_KEYS if k in figs_in}
    s = OptionStudy(
        symbol=sym, expiry=_date(payload.get("expiry"), name="the expiry", required=True), right=right,
        strike=_f(payload.get("strike"), name="the strike", required=True),
        spot=_f(payload.get("spot"), name="the stock price"), entry=_f(payload.get("entry"), name="the entry price"),
        entry_src="user" if str(payload.get("entry_src") or "") == "user" else "last",
        target=_f(payload.get("target"), name="the target"), worst=_f(payload.get("worst"), name="the worst case"),
        mode="date" if str(payload.get("mode") or "") == "date" else "expiry",
        valued_on=_date(payload.get("valued_on"), name="the valuation date"),
        figures=figures, note=(str(payload.get("note") or "").strip()[:2000] or None),
        created_by=getattr(user, "id", None),
    )
    if s.strike <= 0:
        raise StudyError("the strike must be above zero")
    db.add(s)
    db.flush()                                              # the id names the snapshot and the page
    if image:
        try:
            SNAP_DIR.mkdir(parents=True, exist_ok=True)
            name = f"option_{s.id}.png"
            (SNAP_DIR / name).write_bytes(image)
            s.snapshot = name
        except OSError as exc:
            log.warning("option study %s: snapshot not saved: %s", s.id, exc)
    d = discussions.open_discussion(
        db, kind="option", ref=str(s.id), title=title_of(s), body=details_text(s), user=user,
        channels=channels, image=image, page=page_url(s),
    )
    s.discussion_id = d.id
    db.commit()
    db.refresh(s)
    return s


def recent(db: Session, *, limit: int = 60, symbol: str | None = None) -> list[OptionStudy]:
    q = db.query(OptionStudy)
    if symbol:
        q = q.filter(OptionStudy.symbol == symbol.upper())
    return q.order_by(OptionStudy.created_at.desc(), OptionStudy.id.desc()).limit(limit).all()


def discussion_of(db: Session, s: OptionStudy) -> Discussion | None:
    return db.get(Discussion, s.discussion_id) if s.discussion_id else None


def rr_url(s: OptionStudy) -> str:
    """The Reward / risk working window with the setup's own inputs (routes/options_page.py::options_rr)."""
    from urllib.parse import urlencode

    q = {"symbol": s.symbol, "expiry": s.expiry, "strike": strike_text(s.strike), "target": s.target or "",
         "worst": s.worst or "", "mode": s.mode or "expiry"}
    if s.mode == "date" and s.valued_on:
        q["date"] = s.valued_on
    if s.entry_src == "user" and s.entry:
        q["entry"] = s.entry
    return "/options/rr?" + urlencode(q)


def history_url(s: OptionStudy) -> str:
    from urllib.parse import urlencode

    return "/options/history?" + urlencode({"symbol": s.symbol, "expiry": s.expiry, "right": s.right, "strike": strike_text(s.strike)})
