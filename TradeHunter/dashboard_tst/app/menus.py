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
# Industry -> Company -> Watchlist -> Portfolio, all flat top-level items; trimmed
# 2026-10-01 to Calendar -> Sector & Industry -> Watchlist -> Curated (see
# OFF_NAV_KEYS below for the rest).
MENUS = [
    # Calendar — ONE page: the combined month grid (releases + earnings on the
    # same wall calendar, with a day-detail panel). The standalone Economic and
    # Earnings pages were removed 2026-08-20 (user) — the month view already
    # carries both feeds. The key stays "calendar_month" so existing per-user
    # menu grants keep working.
    ("calendar_month",    "Calendar",          None, "/calendar/month"),
    ("sector",           "Sector & Industry", None, "/sector"),
    ("matp",             "Watchlist",         None, "/matp"),
    # Curated — each member's own dated calls (entry/stop/target), judged from
    # price history. Replaced the Portfolio placeholder 2026-09-07 (user).
    ("curated",          "Curated",           None, "/curated"),
    # Options — ONE page (basket · ticker card · My rules) that replaces IV Rank,
    # Spread and Positions (2026-10-04); those three stay reachable by URL (see
    # HIDDEN_KEYS) with their guards widened to accept this key. A flat item, no
    # dropdown (the dropdown is what 2026-10-01 removed).
    ("options",          "Options",           None, "/options"),
]
# Off the nav since 2026-10-01 (user: "i want to disable the Company, macro,
# options") - the SAME treatment as the 2026-09-15 removal: the pages stay
# reachable by URL and by in-page links (the chart's "Full company page", the
# Watchlist's Options tab, the calendar's ticker links), they are just not in
# the bar. To bring one back, move its tuple up into MENUS at its funnel
# position. The tuples are kept here verbatim so that is a cut-and-paste:
#   ("macro",            "Macro",             None,      "/macro")        - the fixed six-topic board (2026-08-16); free-form macro research at /research?kind=macro
#   ("company_analysis", "Company",           None,      "/company-analysis")
#   ("ivscan",           "IV Rank",           "Options", "/ivscan")       - the member's TWS High IV Rank scanner as a watchlist
#   ("spreads",          "Spread",            "Options", "/spreads")      - the bull put spread screener
#   ("positions",        "Positions",         "Options", "/portfolio")    - open spreads graded against the management lines; the nav's exit-line badge renders only while this is in MENUS
# (The Options dropdown had been removed 2026-09-15 and put back 2026-09-18 -
# "put back the option menu and then give me the watchlist" - before this.)
OFF_NAV_KEYS = ["macro", "company_analysis", "ivscan", "spreads", "positions"]
# Routes that stay ACCESSIBLE (granted + reachable by URL) but are no longer shown
# in the top nav after the revamp. Kept in ALL_KEYS so their require_menu() guards
# still pass; simply not rendered by nav_for. "company" is the research chat
# (kind=company); studies/strategy/patterns are de-emphasized. Re-add to MENUS to
# resurface any of them. ("today" was the post-login landing until 2026-09-07, when
# the page was removed and the Calendar became the landing -- see LANDING below.)
#
# "positions" (/portfolio, the member's own open option spreads, added 2026-09-10)
# was taken off the nav 2026-09-15 together with the Options dropdown (user:
# "remove the portfolio menu and also the option menu"). Both came back 2026-09-18:
# the Options dropdown now holds IV Rank, Spread and Positions. NB the Portfolio key is "positions",
# not "portfolio": LEGACY_KEYS translates a stored "portfolio" grant to "curated"
# (the OLD Portfolio placeholder became Curated on 2026-09-07), so reusing that key
# would hand this page's grants to Curated. The nav's exit-line badge poll
# (base.html, /portfolio/badge) renders only while this entry is in MENUS.
HIDDEN_KEYS = OFF_NAV_KEYS + ["company", "studies", "strategy", "patterns"]
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
LABELS.update({"macro": "Macro", "company_analysis": "Company", "ivscan": "IV Rank",
               "spreads": "Spread", "positions": "Positions"})


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
