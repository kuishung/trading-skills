"""Per-user menu access — the single source of truth for the dashboard menus and
the access helpers shared by the nav (base.html), the route guards (main.py
include_router dependencies) and the admin UI.

Model: admins + moderators always see everything. A member sees the leaf items
granted in ``User.menu_access`` (a JSON list of keys). ``None`` = all (back-compat,
so existing users aren't locked out on first deploy); the admin then restricts.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException

from .models import User
from .security import require_user

# (key, label, group, href). group None = a top-level single item. List = nav order.
MENUS = [
    ("macro",     "Macro",        "Investing",     "/research?kind=macro"),
    ("company",   "Company",      "Investing",     "/research?kind=company"),
    ("matp",      "MATP",         "Swing & Trend", "/matp"),
    ("studies",   "Studies",      "Swing & Trend", "/studies"),
    ("screener",  "Screener",     "Intraday",      "/finviz"),
    ("strategy",  "Strategy",     "Intraday",      "/strategy"),
    ("portfolio", "My Portfolio", None,            "/portfolio"),
]
ALL_KEYS = [m[0] for m in MENUS]
LABELS = {m[0]: m[1] for m in MENUS}


def allowed_keys(user: User) -> set:
    """The menu keys this user may access. Admin/moderator -> all; a member with no
    explicit grant (None) -> all (back-compat); otherwise the stored list."""
    if user is None:
        return set()
    if getattr(user, "can_moderate", False):
        return set(ALL_KEYS)
    acc = getattr(user, "menu_access", None)
    if acc is None:
        return set(ALL_KEYS)
    return set(acc) & set(ALL_KEYS)


def user_can(user: User, *keys: str) -> bool:
    return bool(set(keys) & allowed_keys(user))


def nav_for(user: User):
    """Grouped nav filtered to the user's allowed menus, for base.html. Returns
    (groups, singles): groups = [(group_label, [(href,label),...]),...] in registry
    order; singles = [(href,label),...]."""
    allow = allowed_keys(user)
    singles, order, bucket = [], [], {}
    for key, label, group, href in MENUS:
        if key not in allow:
            continue
        if group is None:
            singles.append((href, label))
        else:
            if group not in bucket:
                bucket[group] = []
                order.append(group)
            bucket[group].append((href, label))
    groups = [(g, bucket[g]) for g in order]
    return groups, singles


def require_menu(*keys: str):
    """Route-guard dependency: if the user lacks access to ANY of these menu keys,
    303-redirect to their first allowed page (or '/'). Applied centrally via
    include_router(dependencies=[...]) in main.py, so direct URLs are blocked too."""
    def dep(user: User = Depends(require_user)):
        if not user_can(user, *keys):
            allow = allowed_keys(user)
            dest = next((href for k, label, group, href in MENUS if k in allow), "/")
            raise HTTPException(status_code=303, detail="No menu access",
                                headers={"Location": dest})
        return True
    return dep
