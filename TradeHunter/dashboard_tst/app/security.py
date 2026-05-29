"""Auth dependencies.

Authentication is delegated to Google (OAuth/OIDC) -- this module holds no
passwords. It provides the session helpers + the current-user / approved /
admin dependencies that gate access:

  - ``current_user``  : the logged-in row, or None (may be pending).
  - ``require_user``  : logged in AND approved (pending/disabled -> 401/403).
  - ``require_admin`` : approved admin.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from .db import get_db
from .models import DISABLED, User


def login_user(request: Request, user: User) -> None:
    request.session["uid"] = user.id


def logout_user(request: Request) -> None:
    request.session.pop("uid", None)


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
