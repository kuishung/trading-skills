"""Macro board — the top of the investing funnel.

Two panes: a fixed left rail of the six canonical macro topics, and the selected
topic's analysis on the right. The taxonomy is deliberately FIXED (see
`models.MACRO_SECTIONS`) rather than user-created — this is a dashboard you read
the same way every morning, not a notebook. Free-form macro research still lives
on /research, which is unchanged.

Each section combines two things:
  - COMPUTED tiles (live, via services/macro.py) where the answer is arithmetic
  - WRITTEN analysis (MacroAnalysis) where it needs judgement — pushed by the
    Nous agent via /api/macro/{section} or typed by a moderator here.
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


def _rail(db: Session, active: str) -> list[dict]:
    """The left rail: every section, whether it has analysis yet, and which is open."""
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


@router.get("", response_class=HTMLResponse)
def macro_home(
    request: Request,
    section: str | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    key = (section or "").strip() if (section or "").strip() in MACRO_SECTIONS else MACRO_SECTIONS[0]
    # First visit populates the indicator definitions. Idempotent, and after the
    # first run it's a single COUNT — cheaper than a startup hook and it can
    # never clobber a user's edits to the set.
    try:
        from ..services.indicators import ensure_seeded

        ensure_seeded(db)
    except Exception:  # noqa: BLE001
        pass
    return templates.TemplateResponse(request, "macro.html", {
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

    Idempotent — existing observations are left alone, so a re-run only fills
    gaps and appends. Returns per-indicator results so a missing FRED key or a
    dead series is visible rather than silently producing an empty chart."""
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
        return RedirectResponse(url="/macro", status_code=303)
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
    return RedirectResponse(url=f"/macro?section={key}", status_code=303)
