"""Study + collaboration surface (members-only, but shared across all
members -- including the review analytics, per DESIGN.md section 5).

Scaffold: list the MATP board + setups. Creating setups, proposing
entry/SL/PT, commenting, and the review/backtest analytics land in
Phases 2-5.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import MATPLevel, Setup, User
from ..security import require_user

router = APIRouter(prefix="/studies", tags=["studies"])
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


@router.get("", response_class=HTMLResponse)
def list_studies(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    levels = db.query(MATPLevel).order_by(MATPLevel.symbol).all()
    setups = db.query(Setup).order_by(Setup.created_at.desc()).all()
    return templates.TemplateResponse(
        "studies.html",
        {"request": request, "user": user, "levels": levels, "setups": setups},
    )


# TODO Phase 2: POST /studies/refresh-matp  (admin-triggered quarterly refresh)
# TODO Phase 3: POST /studies            (create a setup)
#               POST /studies/{id}/propose  (entry/SL/PT)
#               POST /studies/{id}/comment
# TODO Phase 4: GET  /studies/{id}/option-winrate (Black-Scholes)
# TODO Phase 5: GET  /studies/{id}/backtest       (parquet success-rate)
