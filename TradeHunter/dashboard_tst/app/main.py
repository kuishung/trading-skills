"""FastAPI app factory for the dashboard_tst collaboration platform.

Run from the dashboard_tst/ directory:

    uvicorn app.main:app --reload

See DESIGN.md. This process is the COLLABORATION + CONTROL plane only --
it never holds broker credentials and never submits orders. The
execution plane lives trusted-side.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import __version__ as APP_VERSION
from .config import settings
from .db import init_db
from .models import User
from .routes import admin as admin_routes
from .routes import auth as auth_routes
from .routes import studies as studies_routes
from .security import current_user

log = logging.getLogger("dashboard_tst")

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # The admin is bootstrapped on first Google sign-in (TST_ADMIN_EMAIL is
    # auto-promoted to admin+approved in routes/auth.py), so there is no
    # password-based seed step.
    if settings.secret_is_default:
        log.warning(
            "TST_SECRET_KEY is the insecure default. Set a real secret before "
            "exposing this app."
        )
    if not settings.google_configured:
        log.warning(
            "Google OAuth not configured (TST_GOOGLE_CLIENT_ID / "
            "TST_GOOGLE_CLIENT_SECRET). Sign-in will be unavailable until set."
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
    app.include_router(studies_routes.router)
    app.include_router(admin_routes.router)

    @app.get("/health")
    def health():
        return {"status": "ok", "version": APP_VERSION}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, user: User | None = Depends(current_user)):
        return templates.TemplateResponse(
            "dashboard.html", {"request": request, "user": user}
        )

    return app


app = create_app()
