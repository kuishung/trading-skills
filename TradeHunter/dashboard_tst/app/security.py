"""Auth: password hashing + session-based current-user dependencies.

Two auth modes (see config.TST_AUTH_MODE):
  - password (Path B): the admin seeds accounts with passwords; access is
    gated by the Hamachi VPN + login. Hashing is stdlib PBKDF2-HMAC-SHA256
    (zero crypto deps). For an internet-facing deploy prefer argon2/bcrypt.
  - google (Path A): authentication is delegated to Google; no passwords.

Either way this module provides the session helpers + the gates:
  - current_user  : the logged-in row, or None (may be pending).
  - require_user  : logged in AND approved (pending/disabled -> 401/403).
  - require_admin : approved admin.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from .db import get_db
from .models import DISABLED, User

_ALGO = "pbkdf2_sha256"
_ITERATIONS = 240_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"{_ALGO}${_ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, iters, b64salt, b64hash = stored.split("$")
        if algo != _ALGO:
            return False
        salt = base64.b64decode(b64salt)
        expected = base64.b64decode(b64hash)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iters))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


# ---- session helpers ----

def login_user(request: Request, user: User) -> None:
    request.session["uid"] = user.id


def logout_user(request: Request) -> None:
    request.session.pop("uid", None)


# ---- FastAPI dependencies ----

def current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    uid = request.session.get("uid")
    if not uid:
        return None
    return db.get(User, uid)


def require_user(user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required")
    if user.status == DISABLED:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account disabled")
    if not user.is_approved:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Awaiting admin approval")
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return user


def require_moderator(user: User = Depends(require_user)) -> User:
    """Moderators and admins (content moderation)."""
    if not user.can_moderate:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Moderator or admin only")
    return user
