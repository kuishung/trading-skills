"""Login / logout, mode-switchable (config.TST_AUTH_MODE).

password (Path B, default):
  GET  /login  -> password form
  POST /login  -> verify email + password, set session

google (Path A):
  GET  /login          -> redirect to Google consent
  GET  /auth/callback  -> exchange code, upsert (pending), set session
  Authlib is imported lazily so the password-mode deploy needs no OAuth deps.

Both modes share the session + the pending/approval model.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import APPROVED, PENDING, User
from ..security import login_user, logout_user, verify_password

router = APIRouter(tags=["auth"])
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


def _login_ctx(error: str | None = None) -> dict:
    return {"error": error, "auth_mode": settings.auth_mode}


# ----------------------------------------------------------------------------
# Google (Path A) — lazy Authlib registration so password mode has no OAuth dep
# ----------------------------------------------------------------------------
_oauth = None


def _google():
    global _oauth
    if _oauth is None:
        from authlib.integrations.starlette_client import OAuth

        _oauth = OAuth()
        _oauth.register(
            name="google",
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )
    return _oauth.google


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------
@router.get("/login")
async def login(request: Request):
    if settings.is_google_auth:
        if not settings.google_configured:
            return templates.TemplateResponse(
                request,
                "login.html",
                _login_ctx("Google sign-in is not configured on this server."),
                status_code=503,
            )
        redirect_uri = settings.oauth_redirect_uri or str(request.url_for("auth_callback"))
        return await _google().authorize_redirect(request, redirect_uri)
    # password mode
    return templates.TemplateResponse(request, "login.html", _login_ctx())


@router.post("/login")
async def login_password(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    """Password-mode form submit. (In google mode the GET handles everything.)"""
    if settings.is_google_auth:
        return RedirectResponse(url="/login", status_code=303)
    user = db.query(User).filter(User.email == email.strip().lower()).first()
    if user is None or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request, "login.html", _login_ctx("Invalid email or password."), status_code=401
        )
    login_user(request, user)
    return RedirectResponse(url="/", status_code=303)


@router.get("/auth/callback", name="auth_callback")
async def auth_callback(request: Request, db: Session = Depends(get_db)):
    if not settings.is_google_auth:
        return RedirectResponse(url="/login", status_code=303)
    try:
        token = await _google().authorize_access_token(request)
    except Exception:
        return RedirectResponse(url="/login", status_code=303)

    info = token.get("userinfo") or {}
    sub = info.get("sub")
    email = (info.get("email") or "").strip().lower()
    if not sub or not email:
        return RedirectResponse(url="/login", status_code=303)

    if not settings.domain_allowed(email):
        return templates.TemplateResponse(
            request, "pending.html", {"rejected": True, "email": email}, status_code=403
        )

    name = info.get("name") or email
    picture = info.get("picture")
    user = (
        db.query(User).filter(User.google_sub == sub).first()
        or db.query(User).filter(User.email == email).first()
    )
    if user is None:
        is_bootstrap_admin = bool(
            settings.admin_email and email == settings.admin_email.strip().lower()
        )
        user = User(
            email=email,
            google_sub=sub,
            display_name=name,
            picture=picture,
            role="admin" if is_bootstrap_admin else "member",
            status=APPROVED if is_bootstrap_admin else PENDING,
            approved_at=_dt.datetime.now(_dt.timezone.utc) if is_bootstrap_admin else None,
        )
        db.add(user)
    else:
        user.google_sub = sub
        user.picture = picture
        if not user.display_name:
            user.display_name = name
    db.commit()
    login_user(request, user)
    return RedirectResponse(url="/", status_code=303)


@router.post("/logout")
async def logout(request: Request):
    logout_user(request)
    return RedirectResponse(url="/login", status_code=303)
