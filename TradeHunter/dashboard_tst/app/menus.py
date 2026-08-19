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
# Revamped 2026-07-18 to a top-down investing funnel (user): Macro -> Sector &
# Industry -> Company -> Watchlist -> Portfolio. All 5 are flat top-level items.
MENUS = [
    # /macro = the fixed six-topic board (2026-08-16). Free-form macro research
    # still lives at /research?kind=macro, linked from the board.
    ("macro",            "Macro",             None, "/macro"),
    # Calendar — ONE page: the combined month grid (releases + earnings on the
    # same wall calendar, with a day-detail panel). The standalone Economic and
    # Earnings pages were removed 2026-08-20 (user) — the month view already
    # carries both feeds. The key stays "calendar_month" so existing per-user
    # menu grants keep working.
    ("calendar_month",    "Calendar",          None, "/calendar/month"),
    ("sector",           "Sector & Industry", None, "/sector"),
    ("company_analysis", "Company",           None, "/company-analysis"),
    ("matp",             "Watchlist",         None, "/matp"),
    ("portfolio",        "Portfolio",         None, "/portfolio"),
]
# Routes that stay ACCESSIBLE (granted + reachable by URL) but are no longer shown
# in the top nav after the revamp. Kept in ALL_KEYS so their require_menu() guards
# still pass; simply not rendered by nav_for. "today" is the post-login landing
# (always granted); "company" is the research chat (kind=company); studies/strategy/
# patterns are de-emphasized. Re-add to MENUS to resurface any of them.
HIDDEN_KEYS = ["today", "company", "studies", "strategy", "patterns"]
# NB: /finviz ("Data Ingest") is admin-only (base.html Settings dropdown), not here.
ALL_KEYS = [m[0] for m in MENUS] + HIDDEN_KEYS
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
    # "today" is the landing page — always accessible, never revocable.
    return (set(acc) & set(ALL_KEYS)) | {"today"}


def user_can(user: User, *keys: str) -> bool:
    return bool(set(keys) & allowed_keys(user))


def nav_for(user: User):
    """The nav bar for base.html: the user's allowed menus IN REGISTRY ORDER, with
    grouped entries collapsed into a dropdown at the position of their first item.

    Returns a flat list of dicts, either
      ``{"kind": "item",  "href": ..., "label": ...}`` or
      ``{"kind": "group", "label": ..., "items": [(href, label), ...]}``.

    Order matters here: MENUS is the top-down investing funnel, so a dropdown has
    to sit where its entries sit in that funnel. (The previous shape returned
    groups and singles separately, which forced base.html to render every
    dropdown before every single item regardless of MENUS order.)
    """
    allow = allowed_keys(user)
    out: list[dict] = []
    at: dict[str, dict] = {}
    for key, label, group, href in MENUS:
        if key not in allow:
            continue
        if group is None:
            out.append({"kind": "item", "href": href, "label": label})
        elif group in at:
            at[group]["items"].append((href, label))
        else:
            entry = {"kind": "group", "label": group, "items": [(href, label)]}
            at[group] = entry
            out.append(entry)
    return out


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
