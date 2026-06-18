"""FastAPI app factory for the dashboard_tst collaboration platform.

Run from the dashboard_tst/ directory:

    uvicorn app.main:app --reload

See DESIGN.md. This process is the COLLABORATION + CONTROL plane only --
it never holds broker credentials and never submits orders. The
execution plane lives trusted-side.
"""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

_START_MONOTONIC = time.monotonic()

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import __version__ as APP_VERSION
from . import menus
from ._build import BUILD
from .config import settings
from .db import SessionLocal, init_db
from .models import APPROVED, User
from .routes import admin as admin_routes
from .routes import agent as agent_routes
from .routes import api as api_routes
from .routes import auth as auth_routes
from .routes import feedback as feedback_routes
from .routes import finviz as finviz_routes
from .routes import matp as matp_routes
from .routes import patterns as patterns_routes
from .routes import pipeline as pipeline_routes
from .routes import portfolio as portfolio_routes
from .routes import research as research_routes
from .routes import strategy as strategy_routes
from .routes import studies as studies_routes
from .routes import today as today_routes
from .security import current_user, hash_password, require_admin

log = logging.getLogger("dashboard_tst")

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

# Expose the build version as a Jinja global on every templates env, so the
# sidebar logo (base.html) shows "v<version>" without each route passing it.
for _routes_mod in (
    auth_routes, today_routes, matp_routes, studies_routes,
    finviz_routes, feedback_routes, admin_routes, agent_routes, pipeline_routes,
    research_routes, patterns_routes, strategy_routes, portfolio_routes,
):
    _routes_mod.templates.env.globals["version"] = APP_VERSION
    _routes_mod.templates.env.globals["nav_for"] = menus.nav_for   # access-filtered nav
templates.env.globals["version"] = APP_VERSION
templates.env.globals["nav_for"] = menus.nav_for


def _bootstrap_admin_password() -> None:
    """password mode: seed an approved admin from env on first startup."""
    if not (settings.admin_email and settings.admin_password):
        return
    import datetime as _dt

    email = settings.admin_email.strip().lower()
    db = SessionLocal()
    try:
        if db.query(User).filter(User.email == email).first() is None:
            db.add(
                User(
                    email=email,
                    display_name="Admin",
                    password_hash=hash_password(settings.admin_password),
                    role="admin",
                    status=APPROVED,
                    approved_at=_dt.datetime.now(_dt.timezone.utc),
                )
            )
            db.commit()
            log.info("Seeded admin account %s", email)
    finally:
        db.close()


def _db_ok() -> bool:
    """Trivial connectivity probe for /status."""
    from sqlalchemy import text

    try:
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
            return True
        finally:
            db.close()
    except Exception:
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if settings.is_google_auth:
        # google mode: admin is auto-promoted on first sign-in (routes/auth.py).
        if not settings.google_configured:
            log.warning(
                "auth_mode=google but Google OAuth is not configured "
                "(TST_GOOGLE_CLIENT_ID / TST_GOOGLE_CLIENT_SECRET). Sign-in unavailable."
            )
    else:
        _bootstrap_admin_password()
    if settings.secret_is_default:
        log.warning(
            "TST_SECRET_KEY is the insecure default. Set a real secret before exposing this app."
        )
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="dashboard_tst - trend & swing collaboration",
        debug=settings.debug,
        lifespan=lifespan,
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie=settings.session_cookie,
        https_only=settings.session_https_only,
        same_site="lax",
    )
    app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")

    app.include_router(auth_routes.router)
    # Today Overview — universal post-login landing (no menu gate; require_user only).
    app.include_router(today_routes.router)
    # page routers carry a per-menu access guard (blocks direct URLs; nav hides them too)
    app.include_router(matp_routes.router, dependencies=[Depends(menus.require_menu("matp"))])
    app.include_router(studies_routes.router, dependencies=[Depends(menus.require_menu("studies"))])
    # Data Ingest (Finviz filter manager) is ADMIN-ONLY now — lives under Settings.
    app.include_router(finviz_routes.router, dependencies=[Depends(require_admin)])
    app.include_router(feedback_routes.router)
    app.include_router(admin_routes.router)
    app.include_router(agent_routes.router)
    app.include_router(api_routes.router)
    app.include_router(pipeline_routes.router)
    app.include_router(research_routes.router, dependencies=[Depends(menus.require_menu("macro", "company"))])
    app.include_router(strategy_routes.router, dependencies=[Depends(menus.require_menu("strategy"))])
    app.include_router(portfolio_routes.router, dependencies=[Depends(menus.require_menu("portfolio"))])
    # Pattern Trainer aborted (user, 2026-06-17): removed from the dashboard menu and
    # the routes disabled. Code/templates/models kept intact — re-enable by
    # uncommenting this line (and restoring the ('/patterns','Patterns') nav entry).
    # app.include_router(patterns_routes.router)

    @app.get("/health")
    def health():
        return {"status": "ok", "version": APP_VERSION}

    @app.get("/status")
    def status_endpoint():
        """Lightweight, unauthenticated operational status for monitoring
        (status_check.ps1). Non-sensitive only -- no user data, no counts."""
        return {
            "status": "ok",
            "version": APP_VERSION,
            "build": BUILD,
            "auth_mode": settings.auth_mode,
            "db_ok": _db_ok(),
            "uptime_seconds": round(time.monotonic() - _START_MONOTONIC, 1),
        }

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, user: User | None = Depends(current_user)):
        # Go straight to the login page for visitors (no marketing landing).
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        # Approved members land on the Today Overview; pending/disabled users
        # see the awaiting-approval page.
        if user.is_approved:
            return RedirectResponse(url="/today", status_code=303)
        return templates.TemplateResponse(request, "dashboard.html", {"user": user})

    return app


app = create_app()
