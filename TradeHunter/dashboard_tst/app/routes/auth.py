"""Google OAuth (OIDC) login + logout.

Flow:
  GET  /login          -> redirect to Google's consent screen
  GET  /auth/callback  -> exchange code, upsert the user, set session
  POST /logout         -> clear session

New users are created with ``status='pending'`` and must be approved by an
admin (see routes/admin.py). The single ``TST_ADMIN_EMAIL`` is auto-promoted
to admin + approved on first sign-in. No passwords are ever handled here.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import APPROVED, PENDING, User
from ..security import login_user, logout_user

router = APIRouter(tags=["auth"])
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)

# Register the Google provider via OIDC discovery. Safe to register even if
# creds are absent; the /login route checks `settings.google_configured`.
oauth = OAuth()
oauth.register(
    name="google",
    client_id=settings.google_client_id,
    client_secret=settings.google_client_secret,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


@router.get("/login")
async def login(request: Request):
    if not settings.google_configured:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Google sign-in is not configured on this server."},
            status_code=503,
        )
    redirect_uri = settings.oauth_redirect_uri or str(request.url_for("auth_callback"))
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/auth/callback", name="auth_callback")
async def auth_callback(request: Request, db: Session = Depends(get_db)):
    try:
        token = await oauth.google.authorize_access_token(request)
    except OAuthError:
        return RedirectResponse(url="/login", status_code=303)

    info = token.get("userinfo") or {}
    sub = info.get("sub")
    email = (info.get("email") or "").strip().lower()
    if not sub or not email:
        return RedirectResponse(url="/login", status_code=303)

    # Optional domain gate before we even create a pending row.
    if not settings.domain_allowed(email):
        return templates.TemplateResponse(
            "pending.html",
            {"request": request, "rejected": True, "email": email},
            status_code=403,
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
        # Returning user: keep status/role; refresh OIDC identity + profile.
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
