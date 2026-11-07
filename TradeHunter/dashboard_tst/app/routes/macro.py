"""Insights -> Macro.

Since v4.243 the Macro page is the SEVEN TOPICS of the user's macro course (Inflation, Fed
Funds Rate, Exchange Rates, Unemployment, Economic Cycles, Economic Growth, US National Debt;
``services/macro_topics.py``): a side rail of the topics, and for the chosen one an
interactive history chart of its stored series with the course's healthy band drawn on it,
a reading written from the data, the next release and the course's "how to read it" text.
The series come from the indicator store (``services/indicators.py``), refreshed daily
inside the app (``services/macro_refresh.py``).

The earlier six-section STUDY BOARD (written analysis per section, computed cross-asset
tiles, the per-section indicator sparklines; MACRO_STUDY_DESIGN.md) is kept at
``/macro/study`` with its fragment and edit routes unchanged - the user: "you can keep what
you have already done".
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (MACRO_SECTION_BLURBS, MACRO_SECTION_LABELS, MACRO_SECTIONS,
                      MacroAnalysis, User, _utcnow)
from ..security import require_moderator, require_user

router = APIRouter(prefix="/macro", tags=["macro"])
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


# ---------------------------------------------------------------------------
# The seven topics (v4.243)
# ---------------------------------------------------------------------------
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
                 "tone": "none", "word": "", "headline": None} for t in macro_topics.TOPICS]
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


# ---------------------------------------------------------------------------
# The study board (the pre-v4.243 page), kept at /macro/study
# ---------------------------------------------------------------------------
def _rail(db: Session, active: str) -> list[dict]:
    """The study board's left rail: every section, whether it has analysis yet, and which is open."""
    rows = {r.section: r for r in db.query(MacroAnalysis).all()}
    return [
        {
            "key": k,
            "label": MACRO_SECTION_LABELS[k],
            "blurb": MACRO_SECTION_BLURBS[k],
            "has_analysis": bool(rows.get(k) and (rows[k].body or rows[k].content)),
            "as_of": getattr(rows.get(k), "as_of", None),
            "active": k == active,
        }
        for k in MACRO_SECTIONS
    ]


def _section_ctx(db: Session, key: str) -> dict:
    """Right-pane context for one section: its stored analysis plus whichever
    computed tiles that section owns."""
    row = db.query(MacroAnalysis).filter(MacroAnalysis.section == key).first()
    ctx = {
        "key": key,
        "label": MACRO_SECTION_LABELS[key],
        "blurb": MACRO_SECTION_BLURBS[key],
        "row": row,
        "cross": None,
        "tone": None,
    }
    # Cross-asset is the one section that is fully computable today, so it gets
    # live tiles. The others are written-analysis only until their computed
    # counterparts land (breadth for internals, calendar for growth/inflation).
    if key == "cross_asset":
        try:
            from ..services.macro import cross_asset, risk_tone

            ctx["cross"] = cross_asset()
            ctx["tone"] = risk_tone(ctx["cross"])
        except Exception:  # noqa: BLE001
            ctx["cross"] = None
    # Tracked indicator series — the trend layer (MACRO_STUDY_DESIGN Phase A).
    # Soft-fail: an empty or broken series must not blank the written analysis.
    try:
        from ..services.indicators import section_summaries

        ctx["indicators"] = section_summaries(db, key)
    except Exception:  # noqa: BLE001
        ctx["indicators"] = []
    return ctx


@router.get("/study", response_class=HTMLResponse)
def macro_study(
    request: Request,
    section: str | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    key = (section or "").strip() if (section or "").strip() in MACRO_SECTIONS else MACRO_SECTIONS[0]
    try:
        from ..services.indicators import ensure_seeded

        ensure_seeded(db)
    except Exception:  # noqa: BLE001
        pass
    return templates.TemplateResponse(request, "macro_study.html", {
        "user": user, "rail": _rail(db, key), "sec": _section_ctx(db, key),
    })


@router.get("/section/{key}", response_class=HTMLResponse)
def macro_section(
    request: Request,
    key: str,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Right-pane fragment (HTMX swap), so switching topics never reloads the page
    or re-fetches the other sections' tiles."""
    if key not in MACRO_SECTIONS:
        key = MACRO_SECTIONS[0]
    return templates.TemplateResponse(request, "_macro_section.html", {
        "user": user, "sec": _section_ctx(db, key),
    })


@router.post("/section/{key}/refresh")
def macro_section_refresh(
    key: str,
    mod: User = Depends(require_moderator),
    db: Session = Depends(get_db),
):
    """Backfill/refresh this section's indicator series from source.

    Idempotent — a re-run fills gaps, appends and updates revised values. Returns
    per-indicator results so a dead series is visible rather than silently producing
    an empty chart."""
    if key not in MACRO_SECTIONS:
        return {"ok": False, "error": "unknown section"}
    from ..services.indicators import ensure_seeded, refresh_section

    ensure_seeded(db)
    results = refresh_section(db, key)
    added = sum(r.get("added", 0) for r in results)
    failed = [r for r in results if not r.get("ok")]
    return {
        "ok": True, "section": key, "added": added,
        "indicators": len(results), "failed": failed,
        "note": (f"{added} new observations across {len(results)} indicators"
                 + (f"; {len(failed)} failed" if failed else "")),
    }


@router.post("/section/{key}")
def macro_section_save(
    key: str,
    body: str = Form(""),
    confidence: str = Form(""),
    mod: User = Depends(require_moderator),
    db: Session = Depends(get_db),
):
    """Moderator edit. Portable upsert (query-then-write) per the data-handling rule."""
    if key not in MACRO_SECTIONS:
        return RedirectResponse(url="/macro/study", status_code=303)
    row = db.query(MacroAnalysis).filter(MacroAnalysis.section == key).first()
    if row is None:
        row = MacroAnalysis(section=key)
        db.add(row)
    row.body = (body or "").strip() or None
    row.confidence = (confidence or "").strip() or None
    row.source_kind = "manual"
    row.as_of = _utcnow()
    row.updated_by = mod.email
    db.commit()
    return RedirectResponse(url=f"/macro/study?section={key}", status_code=303)
