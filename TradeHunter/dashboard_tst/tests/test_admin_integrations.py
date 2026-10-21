"""v4.193: the Settings gear (User Management / Integration / Data Ingest) and the Integration page -
Discord and Telegram details saved in the app (`services/app_settings.py`) over the environment."""
from __future__ import annotations

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app import models
from app.db import get_db
from app.main import app
from app.security import current_user
from app.services import app_settings, discord, telegram


@pytest.fixture
def admin(db):
    u = models.User(email="admin@local.test", display_name="Admin", role=models.ROLE_ADMIN, status=models.APPROVED)
    db.add(u)
    db.commit()
    return u


def _client(engine, uid):
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    def _user(s=Depends(get_db)):
        return s.get(models.User, uid)

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[current_user] = _user
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def aclient(engine, admin):
    try:
        yield _client(engine, admin.id)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def mclient(engine, user):
    try:
        yield _client(engine, user.id)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _own_db(engine, monkeypatch):
    """The service reads through the app's SessionLocal; point it at the test engine."""
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr("app.db.SessionLocal", Session)
    app_settings.clear_cache()
    yield
    app_settings.clear_cache()


def test_a_saved_value_wins_over_the_environment_and_a_blank_falls_back(db, admin, monkeypatch):
    monkeypatch.setenv("TST_DISCORD_WEBHOOK_URL", "https://discord.test/env-hook")
    monkeypatch.delenv("TST_DISCORD_CHANNEL_ID", raising=False)
    assert app_settings.get("discord_webhook_url") == "https://discord.test/env-hook"
    assert app_settings.get("discord_channel_id") is None and app_settings.get("discord_channel_id", "x") == "x"
    changed = app_settings.set_many(db, admin, {"discord_webhook_url": " https://discord.test/saved ", "discord_channel_id": "123",
                                                "nonsense": "ignored"})
    assert changed == ["discord_webhook_url", "discord_channel_id"]
    assert app_settings.get("discord_webhook_url") == "https://discord.test/saved"
    assert discord.configured() is True and discord.bot_configured() is False     # the senders read the same store
    rows = {r["key"]: r for r in app_settings.describe(db)}
    assert rows["discord_webhook_url"]["source"] == "saved" and rows["discord_webhook_url"]["shown"] == "••••aved"
    assert rows["discord_channel_id"]["plain"] == "123" and rows["discord_bot_token"]["source"] == "none"
    assert app_settings.set_many(db, admin, {"discord_webhook_url": ""}) == ["discord_webhook_url"]   # cleared -> env again
    assert app_settings.get("discord_webhook_url") == "https://discord.test/env-hook"
    assert app_settings.set_many(db, admin, {"discord_webhook_url": ""}) == []                          # already clear
    assert app_settings.mask("ab") == "••" and app_settings.mask(None) == "-"


def test_telegram_creds_prefer_the_saved_values_half_by_half(db, admin, monkeypatch):
    monkeypatch.setattr(telegram.thc, "telegram_env", lambda cfg=None: ("vault-token", "111"))
    assert telegram.creds() == ("vault-token", "111")
    app_settings.set_many(db, admin, {"telegram_chat_id": "222"})
    assert telegram.creds() == ("vault-token", "222")
    app_settings.set_many(db, admin, {"telegram_bot_token": "app-token"})
    assert telegram.creds() == ("app-token", "222") and telegram.configured() is True


def test_the_integration_page_saves_and_keeps_secrets_left_blank(aclient, db, admin):
    r = aclient.get("/admin/integrations")
    assert r.status_code == 200
    html = r.text
    assert "<h1" in html and ">Integration</h1>" in html.replace('class="text-2xl font-semibold mb-1"', "").replace(" ", "") or "Integration" in html
    for needle in ('name="discord_webhook_url" type="password"', 'name="discord_channel_id" type="text"',
                   'name="telegram_bot_token" type="password"', 'name="telegram_chat_id" type="text"',
                   'name="clear" value="discord_bot_token"', 'hx-post="/admin/discord-test"',
                   'hx-post="/admin/integrations/test/telegram"', "not set anywhere"):
        assert needle in html, needle
    r = aclient.post("/admin/integrations", data={"discord_webhook_url": "https://discord.test/h1", "discord_channel_id": "42",
                                                  "discord_guild_id": "", "telegram_bot_token": "tok-1", "telegram_chat_id": "9"})
    assert r.status_code == 303 and r.headers["location"] == "/admin/integrations?saved=4"
    html = aclient.get("/admin/integrations?saved=4").text
    assert "Saved - 4 settings changed." in html and 'value="42"' in html and "saved in the app" in html
    assert "https://discord.test/h1" not in html and "tok-1" not in html          # secrets never echoed
    assert "••••t/h1 - leave blank to keep" in html
    # a blank secret keeps what is saved; the clear box removes it; a non-secret blank clears
    r = aclient.post("/admin/integrations", data={"discord_webhook_url": "", "discord_channel_id": "", "telegram_bot_token": "",
                                                  "telegram_chat_id": "9", "clear": ["telegram_bot_token"]})
    assert r.status_code == 303 and r.headers["location"] == "/admin/integrations?saved=2"
    app_settings.clear_cache()
    assert app_settings.get("discord_webhook_url") == "https://discord.test/h1"
    assert db.get(models.AppSetting, "discord_channel_id").value is None
    assert db.get(models.AppSetting, "telegram_bot_token").value is None


def test_the_telegram_test_send_reports_in_words(aclient, db, admin, monkeypatch):
    monkeypatch.setattr(telegram.thc, "telegram_env", lambda cfg=None: (None, None))
    assert "No Telegram bot token" in aclient.post("/admin/integrations/test/telegram").text
    app_settings.set_many(db, admin, {"telegram_bot_token": "t"})
    assert "No default chat id" in aclient.post("/admin/integrations/test/telegram").text
    app_settings.set_many(db, admin, {"telegram_chat_id": "5"})
    sent = {}
    monkeypatch.setattr(telegram, "send", lambda html, chat_id=None: sent.update(chat=chat_id, text=html) or (True, ""))
    assert "Sent" in aclient.post("/admin/integrations/test/telegram").text and sent["chat"] == "5" and "test" in sent["text"]


def test_the_settings_gear_replaces_the_settings_menu_and_the_admin_link(aclient):
    html = aclient.get("/admin").text
    assert "User Management" in html and 'hx-post="/admin/discord-test"' not in html      # the card moved
    assert 'id="settingsGear"' in html and 'title="Settings"' in html
    gear = html[html.index('id="settingsGear"'):html.index('id="themeToggle"')]
    assert '<a href="/admin" class="block' in gear and ">User Management</a>" in gear
    assert '<a href="/admin/integrations" class="block' in gear and ">Integration</a>" in gear
    assert '<a href="/finviz" class="block' in gear and ">Data Ingest</a>" in gear
    assert html.index('id="settingsGear"') < html.index('id="themeToggle"')
    nav = html[html.index("<nav"):html.index("</nav>")]
    assert ">Settings<" not in nav and ">Admin</a>" not in nav and ">Data Ingest</a>" not in nav


def test_a_member_sees_no_gear_and_cannot_open_the_integration_page(mclient):
    page = mclient.get("/options").text
    assert 'id="settingsGear"' not in page and "/admin/integrations" not in page
    assert mclient.get("/admin/integrations").status_code in (302, 303, 403)
