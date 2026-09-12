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
    # Curated — each member's own dated calls (entry/stop/target), judged from
    # price history. Replaced the Portfolio placeholder 2026-09-07 (user).
    ("curated",          "Curated",           None, "/curated"),
    # Portfolio — the member's OWN open option spreads, monitored daily against
    # their delta and max-loss exit lines. Added 2026-09-10 (user).
    #
    # The key is "positions", not "portfolio", and that is deliberate: LEGACY_KEYS
    # below still translates a stored "portfolio" grant to "curated", because the
    # OLD Portfolio placeholder is what became Curated on 2026-09-07. Reusing the
    # key here would make one stored string mean two different pages, and the
    # translation would silently hand this page's grants to Curated instead.
    ("positions",        "Portfolio",         None, "/portfolio"),
    # Options — a dropdown group (user, 2026-09-13: "Option sub menu Spread").
    # First entry: the bull put spread screener, Barchart's screen rebuilt on
    # the platform's own Cboe feed. Room for Flow / Chains later.
    ("spreads",          "Spread",            "Options", "/spreads"),
]
# Routes that stay ACCESSIBLE (granted + reachable by URL) but are no longer shown
# in the top nav after the revamp. Kept in ALL_KEYS so their require_menu() guards
# still pass; simply not rendered by nav_for. "company" is the research chat
# (kind=company); studies/strategy/patterns are de-emphasized. Re-add to MENUS to
# resurface any of them. ("today" was the post-login landing until 2026-09-07, when
# the page was removed and the Calendar became the landing -- see LANDING below.)
HIDDEN_KEYS = ["company", "studies", "strategy", "patterns"]
# Where an approved user lands after sign-in (user, 2026-09-07: "on login go to
# calendar by default"). Kept here rather than hard-coded at each redirect site so
# the three of them (index, password login, OAuth callback) cannot drift apart.
LANDING = "calendar_month"
# NB: /finviz ("Data Ingest") is admin-only (base.html Settings dropdown), not here.
ALL_KEYS = [m[0] for m in MENUS] + HIDDEN_KEYS
# Renamed keys, old -> new. A member's menu_access is a stored list of KEYS, so
# renaming one would silently revoke access for everyone who had been granted it
# individually. Translating on read keeps those grants working without a data
# migration, and without pinning the code to a name the page no longer has.
LEGACY_KEYS = {"portfolio": "curated"}
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
    return {LEGACY_KEYS.get(k, k) for k in acc} & set(ALL_KEYS)


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


def landing_for(user: User) -> str:
    """Where to send an approved user after sign-in.

    The Calendar by default; if this member has not been granted it, their first
    allowed menu instead. Resolving it HERE rather than redirecting to a fixed URL
    means a member without calendar access lands on a page they can actually open,
    instead of bouncing off require_menu's guard.
    """
    allow = allowed_keys(user)
    if LANDING in allow:
        return dict((m[0], m[3]) for m in MENUS)[LANDING]
    return next((href for key, label, group, href in MENUS if key in allow), "/")


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
