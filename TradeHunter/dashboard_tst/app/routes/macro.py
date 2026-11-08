"""Insights -> Macro.

Since v4.243 the Macro page is the SEVEN TOPICS of the user's macro course (Inflation, Fed
Funds Rate, Exchange Rates, Unemployment, Economic Cycles, Economic Growth, US National Debt;
``services/macro_topics.py``): a side rail of the topics, and for the chosen one an
interactive history chart of its stored series with the course's healthy band drawn on it,
a reading written from the data, the next release and the course's "how to read it" text.
The series come from the indicator store (``services/indicators.py``), refreshed daily
inside the app (``services/macro_refresh.py``).

The earlier six-section STUDY BOARD (``/macro/study``, written analysis per section with
computed tiles; MACRO_STUDY_DESIGN.md) was removed in v4.249 at the user's request. Its
table ``macro_analysis`` stays (the data-handling rule: no schema drop), still written by
``POST /api/macro/{section}`` in ``routes/api.py``.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User
from ..security import require_moderator, require_user

router = APIRouter(prefix="/macro", tags=["macro"])
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


@router.get("", response_class=HTMLResponse)
def macro_home(
    request: Request,
    topic: str | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    from ..services import macro_topics
    from ..services.indicators import ensure_seeded

    try:
        ensure_seeded(db)          # idempotent; adds any series a new release introduced
    except Exception:  # noqa: BLE001
        pass
    from ..services import macro_links

    key = topic if topic in macro_topics.TOPIC_KEYS else macro_topics.TOPIC_KEYS[0]
    try:
        rail = macro_topics.rail(db)
    except Exception:  # noqa: BLE001
        rail = [{"key": t["key"], "label": t["label"], "blurb": t["blurb"], "cadence": t["cadence"],
                 "tone": "none", "word": "", "headline": None, "icon": macro_topics.icon(t["key"])}
                for t in macro_topics.TOPICS]
    return templates.TemplateResponse(request, "macro.html", {
        "user": user, "rail": rail, "active": key, "sheet": macro_links.SHEET,
    })


@router.get("/api/topic/{key}")
def macro_topic_api(key: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """One topic's chart payload: the full stored history of its series, the band and
    reference lines, the reading, the next release and the course text."""
    from ..services import macro_topics

    if key not in macro_topics.TOPIC_KEYS:
        return {"ok": False, "error": "unknown topic"}
    return {"ok": True, **macro_topics.payload(db, key)}


@router.get("/api/status")
def macro_status_api(user: User = Depends(require_user)):
    """The daily refresh's state (the page's "data as of" line)."""
    from ..services import macro_refresh

    return {"ok": True, **macro_refresh.status()}


@router.post("/api/refresh")
def macro_refresh_now(mod: User = Depends(require_moderator)):
    """Fetch every series now (moderators). Runs in the request; ~35 small HTTP calls."""
    from ..services import macro_refresh

    return macro_refresh.run_once()
