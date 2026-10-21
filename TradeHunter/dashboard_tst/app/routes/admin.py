"""Admin control plane (admin-only).

Responsibilities:
  1. Authorization. In password mode (Path B) the admin *creates* member
     accounts directly (approved on creation). In google mode (Path A)
     users self-arrive as 'pending' and the admin approves them.
     Either mode: disable / set-role.
  2. The swing-bot CONTROL surface.

CRITICAL (DESIGN.md section 6): control plane only. It never holds broker
credentials and never submits orders. The swing-bot execution plane runs
trusted-side; these endpoints will relay an authenticated signal to it.
Until that relay is built, bot actions are explicit stubs.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..menus import ALL_KEYS, MENUS, allowed_keys
from ..models import APPROVED, DISABLED, PENDING, ROLE_MEMBER, ROLES, User
from ..security import hash_password, require_admin
from ..services import discord

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)


@router.get("", response_class=HTMLResponse)
def admin_home(
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    pending = db.query(User).filter(User.status == PENDING).order_by(User.created_at).all()
    members = (
        db.query(User).filter(User.status != PENDING).order_by(User.status, User.email).all()
    )
    counts = {
        "total": len(pending) + len(members),
        "pending": len(pending),
        "active": sum(1 for u in members if u.status == APPROVED),
        "disabled": sum(1 for u in members if u.status == DISABLED),
    }
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "user": admin,
            "pending": pending,
            "members": members,
            "counts": counts,
            "auth_mode": settings.auth_mode,
            "is_google_auth": settings.is_google_auth,
            "discord_configured": discord.configured(),
            "menus": MENUS,                                  # (key,label,group,href)
            "menu_allowed": {u.id: allowed_keys(u) for u in members},  # per-user granted set
        },
    )


@router.get("/log", response_class=PlainTextResponse)
def app_log(lines: int = 200, admin: User = Depends(require_admin)):
    """The tail of ``dashboard.log`` (v4.63), in the browser.

    The app runs on Hermes under a Scheduled Task with no console; the log file
    is the only record of a request that failed. An admin should be able to read
    it without RDP - the same reason every other runtime state lives on a page
    (dashboard-visibility rule).
    """
    path = Path(__file__).resolve().parent.parent.parent / "dashboard.log"
    if not path.exists():
        return f"{path} does not exist yet (written on first startup of v4.63+)."
    n = max(10, min(int(lines), 5000))
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 400_000))
            text = f.read().decode("utf-8", "replace")
    except OSError as exc:
        return f"cannot read {path}: {exc}"
    return "\n".join(text.splitlines()[-n:])


@router.get("/portfolio-diag", response_class=PlainTextResponse)
def portfolio_diag(admin: User = Depends(require_admin)):
    """Run ``deploy/portfolio_diag.py`` in a subprocess and show its output.

    Same interpreter as the app, same database URL (inherited environment), a
    hard 120 s cap. Read-only - the script SELECTs, renders in memory, and exits.
    """
    import subprocess
    import sys

    dash_root = Path(__file__).resolve().parent.parent.parent
    script = dash_root / "deploy" / "portfolio_diag.py"
    try:
        r = subprocess.run([sys.executable, str(script)], cwd=str(dash_root),
                           capture_output=True, text=True, timeout=120,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as exc:
        return ("TIMED OUT after 120 s - the Cboe fetch or the render is hanging on this box.\n\n"
                + (exc.stdout or "") + "\n" + (exc.stderr or ""))
    return f"exit {r.returncode}\n\n{r.stdout}\n{r.stderr}"


@router.get("/integrations", response_class=HTMLResponse)
def integrations_page(
    request: Request,
    saved: str = "",
    error: str = "",
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """v4.193 (the user: "Integration where discord and telegram will be set here"): the Settings
    gear -> Integration page - the Discord webhook / bot and the Telegram bot details, saved in
    the app (``app_settings``) over the environment, each with a test send."""
    from ..services import app_settings, telegram

    rows = app_settings.describe(db)
    by = {r["key"]: r for r in rows}
    return templates.TemplateResponse(
        request,
        "admin_integrations.html",
        {
            "user": admin,
            "rows": rows,
            "discord": [by[k] for k in ("discord_webhook_url", "discord_bot_token", "discord_channel_id", "discord_guild_id")],
            "telegram": [by[k] for k in ("telegram_bot_token", "telegram_chat_id")],
            "discord_configured": discord.configured(),
            "discord_bot_configured": discord.bot_configured(),
            "telegram_configured": telegram.configured(),
            "saved": saved,
            "error": error,
        },
    )


@router.post("/integrations")
def integrations_save(
    request: Request,
    discord_webhook_url: str = Form(""),
    discord_bot_token: str = Form(""),
    discord_channel_id: str = Form(""),
    discord_guild_id: str = Form(""),
    telegram_bot_token: str = Form(""),
    telegram_chat_id: str = Form(""),
    clear: list[str] = Form([]),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Save the Integration page. A secret left blank keeps what is saved; ticking its
    "clear" box removes it (the environment stands in again); a non-secret field is saved
    as shown (blank clears it)."""
    from ..services import app_settings

    values: dict = {}
    form = {"discord_webhook_url": discord_webhook_url, "discord_bot_token": discord_bot_token,
            "discord_channel_id": discord_channel_id, "discord_guild_id": discord_guild_id,
            "telegram_bot_token": telegram_bot_token, "telegram_chat_id": telegram_chat_id}
    for key, (_label, _env, secret) in app_settings.KEYS.items():
        v = (form.get(key) or "").strip()
        if key in (clear or []):
            values[key] = ""
        elif v or not secret:
            values[key] = v
    changed = app_settings.set_many(db, admin, values)
    return RedirectResponse(url="/admin/integrations?saved=" + str(len(changed)), status_code=303)


@router.post("/integrations/test/telegram", response_class=HTMLResponse)
def telegram_test(admin: User = Depends(require_admin)):
    """A test message to the default chat id with the bot in force (HTMX fragment)."""
    from ..services import telegram

    token, chat_id = telegram.creds()
    if not token:
        return HTMLResponse('<span class="text-rose-300">No Telegram bot token - save one above (or put it in the vault\'s telegram.env).</span>')
    if not chat_id:
        return HTMLResponse('<span class="text-rose-300">No default chat id - save one above.</span>')
    try:
        ok, why = telegram.send("TradeHunter test from %s. If you can read this, the Telegram integration works."
                                % (admin.display_name or admin.email), chat_id=chat_id)
    except Exception as exc:  # noqa: BLE001
        ok, why = False, type(exc).__name__
    if ok:
        return HTMLResponse('<span class="text-emerald-300">Sent &#10003; - check the Telegram chat.</span>')
    return HTMLResponse('<span class="text-rose-300">Telegram send failed: ' + str(why or "see the server log") + '</span>')


@router.post("/discord-test", response_class=HTMLResponse)
def discord_test(admin: User = Depends(require_admin)):
    """Fire a test post to the configured Discord webhook (HTMX fragment result).
    Lets an admin confirm the integration without waiting for a real refresh."""
    if not discord.configured():
        return HTMLResponse(
            '<span class="text-rose-300">No webhook configured — save one on this page '
            '(or set <code>TST_DISCORD_WEBHOOK_URL</code> in app/.env and restart).</span>'
        )
    ok = discord.post_embed(
        title="🔔 TradeHunter · test",
        description=f"Test post from {admin.display_name or admin.email}. "
        "If you can read this in your channel, the Discord integration works.",
        url=settings.public_url,
    )
    if ok:
        return HTMLResponse('<span class="text-emerald-300">Sent ✓ — check your Discord channel.</span>')
    return HTMLResponse('<span class="text-rose-300">Webhook POST failed — see the server log.</span>')


def _get(db: Session, uid: int) -> User | None:
    return db.get(User, uid)


@router.post("/users")
def create_user(
    request: Request,
    email: str = Form(...),
    display_name: str = Form(""),
    password: str = Form(...),
    role: str = Form("member"),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Password-mode account creation (approved immediately). No-op in google
    mode, where users self-arrive via OAuth and are approved instead."""
    if settings.is_google_auth:
        return RedirectResponse(url="/admin", status_code=303)
    email = email.strip().lower()
    if db.query(User).filter(User.email == email).first() is None:
        db.add(
            User(
                email=email,
                display_name=display_name or email,
                password_hash=hash_password(password),
                role=role if role in ROLES else ROLE_MEMBER,
                status=APPROVED,
                approved_at=_dt.datetime.now(_dt.timezone.utc),
            )
        )
        db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{uid}/approve")
def approve_user(uid: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    u = _get(db, uid)
    if u and u.status != APPROVED:
        u.status = APPROVED
        u.approved_at = _dt.datetime.now(_dt.timezone.utc)
        db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{uid}/disable")
def disable_user(uid: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    u = _get(db, uid)
    if u and u.id != admin.id:  # don't let an admin lock themselves out
        u.status = DISABLED
        db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{uid}/role")
def set_role(
    uid: int,
    role: str = Form(...),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    u = _get(db, uid)
    # validate role; don't let an admin change their own role (lockout guard)
    if u and u.id != admin.id and role in ROLES:
        u.role = role
        db.commit()
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{uid}/menus")
def set_menus(
    uid: int,
    keys: list[str] = Form(default=[]),    # checked menu keys (unchecked = absent)
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Set a user's per-page menu access (app/menus.py keys). An explicit empty list
    = no access; the granted subset hides everything else + blocks the URLs. Has no
    effect on admins/moderators (they always see all). Members only."""
    u = _get(db, uid)
    if u is not None:
        u.menu_access = [k for k in keys if k in ALL_KEYS]
        db.commit()
    return RedirectResponse(url="/admin", status_code=303)


# ---- swing-bot control plane (admin-only; NO execution in this process) ----

_ALLOWED_BOT_ACTIONS = {"enable", "disable", "arm", "disarm", "status"}


@router.post("/bot/{action}")
def bot_control(action: str, admin: User = Depends(require_admin)):
    if action not in _ALLOWED_BOT_ACTIONS:
        return {"ok": False, "detail": f"unknown action {action!r}"}
    # TODO Phase 6: relay an authenticated signal to the trusted-side swing
    # orchestrator (enable/disable -> state/enabled_<swing>.flag, arm/disarm
    # -> state/armed_<swing>.flag). This process MUST NOT hold broker creds
    # or open an IBKR session.
    return {
        "ok": False,
        "action": action,
        "detail": "control-plane stub -- execution plane is trusted-side and not wired yet",
    }
