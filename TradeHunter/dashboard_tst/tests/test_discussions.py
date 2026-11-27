"""v4.275 - Discussions (DISCUSSIONS_DESIGN.md): one thread per subject on Discord and Telegram, the
contributions read back. The structure: open on both channels (soft-fail per channel), post + mirror,
sync Discord, route Telegram updates (topics, groups seen, /start), the page, the Data Ingest card."""
from __future__ import annotations

import datetime as _dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app import models
from app.db import get_db
from app.main import app
from app.security import current_user
from app.services import app_settings, discord, discussion_sync, discussions, telegram
from tests.test_admin_integrations import _own_db  # noqa: F401 - the settings reader on the test DB (autouse)


@pytest.fixture
def mod(db):
    u = models.User(email="mod@local.test", display_name="Mod", role=models.ROLE_MODERATOR, status=models.APPROVED)
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

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[current_user] = lambda s=__import__("fastapi").Depends(get_db): s.get(models.User, uid)
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def fake_chats(db, mod, monkeypatch):
    """Both bots 'configured'; Discord and Telegram answered by fakes that record what was sent."""
    app_settings.set_many(db, mod, {"discord_bot_token": "t", "discord_channel_id": "777", "discord_guild_id": "999",
                                    "telegram_bot_token": "tt", "telegram_group_id": "-1001234567890"})
    sent = {"discord_threads": [], "discord_posts": [], "tg": []}
    monkeypatch.setattr(discord, "create_study_thread", lambda name, embed=None, **kw: (sent["discord_threads"].append((name, embed)) or "th1"))
    monkeypatch.setattr(discord, "post_thread_message", lambda tid, text: (sent["discord_posts"].append((tid, text)) or True))
    monkeypatch.setattr(discord, "fetch_thread_messages", lambda tid, limit=50: [
        {"id": "m1", "author_id": "u1", "author": "Alice", "content": "looks strong", "embed_text": "", "has_attach": False,
         "ts": "2026-01-05 10:00:00", "bot": False, "reply_to": None},
        {"id": "m0", "author_id": "bot", "author": "TradeHunter", "content": "", "embed_text": "opener", "has_attach": False,
         "ts": "2026-01-05 09:59:00", "bot": True, "reply_to": None}])

    def fake_api(method, data, timeout=None):
        sent["tg"].append((method, dict(data)))
        if method == "createForumTopic":
            return {"ok": True, "result": {"message_thread_id": 55, "name": data["name"]}}
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": 101}}
        return {"ok": True, "result": []}
    monkeypatch.setattr(telegram, "_api", fake_api)
    return sent


def test_open_post_sync_and_route(db, mod, fake_chats):
    d = discussions.open_discussion(db, kind="company", ref="nvda", title="NVDA after earnings", body="What now?", user=mod)
    assert d.kind == "company" and d.ref == "nvda" and d.status == "open"
    assert d.discord_thread_id == "th1" and d.discord_channel_id == "777"
    assert d.telegram_chat_id == "-1001234567890" and d.telegram_topic_id == "55" and not d.channel_errors
    assert fake_chats["discord_threads"][0][0] == "NVDA after earnings"
    assert fake_chats["tg"][0][0] == "createForumTopic" and fake_chats["tg"][1][0] == "sendMessage"
    assert fake_chats["tg"][1][1]["message_thread_id"] == "55" and "What now?" in fake_chats["tg"][1][1]["text"]
    assert d.n_contributions == 1 and db.query(models.Contribution).filter_by(discussion_id=d.id, is_opener=True).count() == 1
    assert discussions.links(d) == {"discord": "https://discord.com/channels/999/th1", "telegram": "https://t.me/c/1234567890/55"}
    assert discussions.subject_url("company", "nvda") == "/company-analysis?symbol=NVDA"
    assert discussions.for_subject(db, "company", "nvda").id == d.id

    # an app post is stored and mirrored into both chats as "Name: text"
    c = discussions.post(db, d, "I would wait for the gap to fill", mod)
    assert c.channel == "app" and c.external_id == f"app:{c.id}" and c.mirrored == {"discord": True, "telegram": "101"}
    assert fake_chats["discord_posts"][-1] == ("th1", "Mod: I would wait for the gap to fill")
    assert d.n_contributions == 2

    # the Discord sync stores the member's message, skips the bot's, and is idempotent
    assert discussions.sync_discord(db, d) == 1 and discussions.sync_discord(db, d) == 0
    assert d.n_contributions == 3 and d.last_synced_at is not None

    # a Telegram update in the topic is stored once; the bot's own, a service message and other chats are not
    upd = {"update_id": 1, "message": {"message_id": 7, "date": 1767610800, "message_thread_id": 55,
                                       "chat": {"id": -1001234567890, "type": "supergroup", "title": "TradeHunter", "is_forum": True},
                                       "from": {"id": 42, "first_name": "Bob", "last_name": "K", "is_bot": False},
                                       "text": "agree, wait", "reply_to_message": {"message_id": 55}}}
    r = discussions.route_telegram_update(db, upd)
    assert r["stored"] is True and r["chat"] == {"id": "-1001234567890", "title": "TradeHunter", "is_forum": True}
    assert discussions.route_telegram_update(db, upd)["stored"] is False
    bot_upd = {"update_id": 2, "message": {"message_id": 8, "date": 1795700001, "message_thread_id": 55,
                                           "chat": {"id": -1001234567890, "type": "supergroup"}, "from": {"id": 1, "is_bot": True}, "text": "Mod: x"}}
    assert discussions.route_telegram_update(db, bot_upd)["stored"] is False
    other = {"update_id": 3, "message": {"message_id": 9, "date": 1795700002, "message_thread_id": 99,
                                         "chat": {"id": -1001234567890, "type": "supergroup"}, "from": {"id": 42, "is_bot": False}, "text": "lost"}}
    assert discussions.route_telegram_update(db, other)["stored"] is False
    msgs = discussions.messages(db, d, me=mod)                        # time order: the chat messages are dated earlier
    assert [m["source"] for m in msgs] == ["discord", "telegram", "web", "web"] and msgs[2]["is_opener"]
    assert msgs[1]["author"] == "Bob K" and msgs[1]["text"] == "agree, wait" and msgs[3]["me"] is True
    bob = db.query(models.Contribution).filter_by(channel="telegram").one()
    assert bob.external_id == "7" and bob.author_external_id == "42" and bob.reply_to_external_id is None

    # /start in a private chat is answered with the chat id (the link-up handshake's first half)
    start = {"update_id": 4, "message": {"message_id": 1, "date": 1795700003, "chat": {"id": 5, "type": "private"},
                                         "from": {"id": 5, "first_name": "Kui", "is_bot": False}, "text": "/start"}}
    assert discussions.route_telegram_update(db, start)["start"] is True
    assert fake_chats["tg"][-1][1]["chat_id"] == "5" and "Your chat id is 5" in fake_chats["tg"][-1][1]["text"]

    discussions.close(db, d)
    assert d.status == "closed" and discussions.for_subject(db, "company", "nvda") is None
    assert discussions.for_subject(db, "company", "nvda", open_only=False).id == d.id


def test_the_relay_copies_a_reply_into_the_other_chat_when_the_admin_switch_is_on(db, mod, fake_chats):
    """v4.278 (user: "make it an optional setting by admin, if admin turn on the feature, then both discord and
    telegram message will be relayed"): off by default - a Telegram reply stays in Telegram; on - it is posted into
    the Discord thread as "Name (Telegram): text" and a Discord reply into the topic as "Name (Discord): text"."""
    d = discussions.open_discussion(db, kind="topic", ref="", title="Relay test", body=None, user=mod)
    n_posts, n_tg = len(fake_chats["discord_posts"]), len(fake_chats["tg"])
    upd = {"update_id": 20, "message": {"message_id": 70, "date": 1767610800, "message_thread_id": 55,
                                        "chat": {"id": -1001234567890, "type": "supergroup"},
                                        "from": {"id": 42, "first_name": "Bob", "is_bot": False}, "text": "from telegram"}}
    assert discussions.relay_on() is False
    assert discussions.route_telegram_update(db, upd)["stored"] is True
    assert len(fake_chats["discord_posts"]) == n_posts                                   # off: nothing relayed
    app_settings.set_many(db, mod, {"discussions_relay": "1"})
    assert discussions.relay_on() is True
    upd["update_id"], upd["message"]["message_id"], upd["message"]["text"] = 21, 71, "again, relayed"
    assert discussions.route_telegram_update(db, upd)["stored"] is True
    assert fake_chats["discord_posts"][-1] == ("th1", "Bob (Telegram): again, relayed")
    c = db.query(models.Contribution).filter_by(channel="telegram", external_id="71").one()
    assert c.mirrored == {"discord": True}
    assert discussions.sync_discord(db, d) == 1                                            # Alice's Discord reply
    assert fake_chats["tg"][-1][0] == "sendMessage" and fake_chats["tg"][-1][1]["text"] == "Alice (Discord): looks strong"
    assert fake_chats["tg"][-1][1]["message_thread_id"] == "55"
    assert db.query(models.Contribution).filter_by(channel="discord", external_id="m1").one().mirrored == {"telegram": "101"}
    assert discussions.sync_discord(db, d) == 0 and fake_chats["tg"][-1][1]["text"] == "Alice (Discord): looks strong"   # not re-sent


def test_a_channel_that_fails_is_recorded_in_words(db, mod, monkeypatch):
    app_settings.set_many(db, mod, {"telegram_bot_token": "tt", "telegram_group_id": "-100555"})

    def fake_api(method, data, timeout=None):
        raise telegram.TelegramError("HTTP 400 Bad Request: the chat is not a forum")
    monkeypatch.setattr(telegram, "_api", fake_api)
    d = discussions.open_discussion(db, kind="topic", ref="", title="Free topic", body=None, user=mod)
    assert d.telegram_topic_id is None and d.discord_thread_id is None
    assert "switch Topics on" in d.channel_errors["telegram"] and "no Discord bot token" in d.channel_errors["discord"]
    assert d.ref is None and discussions.links(d) == {"discord": None, "telegram": None}
    with pytest.raises(ValueError):
        discussions.open_discussion(db, kind="nope", ref="", title="x", body=None, user=mod)


def test_the_sync_poll_routes_updates_and_remembers_groups(db, mod, fake_chats, monkeypatch, tmp_path):
    d = discussions.open_discussion(db, kind="topic", ref="", title="Poll test", body=None, user=mod, channels=("telegram",))
    updates = [{"update_id": 10, "message": {"message_id": 70, "date": 1795700000, "message_thread_id": int(d.telegram_topic_id),
                                              "chat": {"id": -1001234567890, "type": "supergroup", "title": "TradeHunter", "is_forum": True},
                                              "from": {"id": 42, "first_name": "Bob", "is_bot": False}, "text": "from the poll"}},
               {"update_id": 11, "my_chat_member": {"chat": {"id": -100777, "type": "group", "title": "Another"}}}]
    monkeypatch.setattr(telegram, "get_updates", lambda offset, timeout=20: updates if not offset else [])
    monkeypatch.setattr(discussion_sync, "STATE_PATH", tmp_path / "s.json")
    monkeypatch.setattr(discussion_sync, "_started", True)          # status() answers from the live state
    Session = sessionmaker(bind=db.get_bind(), autoflush=False, autocommit=False, future=True)
    assert discussion_sync.poll_telegram_once(Session) == 1
    assert discussion_sync.poll_telegram_once(Session) == 0            # the offset moved on
    st = discussion_sync.status()
    tg = st["telegram"]
    assert tg["offset"] == 12 and tg["stored_total"] == 1 and tg["updates_total"] == 2
    assert [g["id"] for g in tg["seen_groups"]] == ["-100777", "-1001234567890"]
    threads, stored = discussion_sync.discord_pass(Session)
    assert threads == 0 and stored == 0                                 # this one has no Discord thread
    assert db.query(models.Contribution).filter_by(channel="telegram").count() == 1


def test_the_page_the_strip_and_the_health_card(engine, db, mod, user, fake_chats):
    try:
        c = _client(engine, mod.id)
        html = c.get("/discussions").text
        assert "Discussions" in html and "New discussion" in html and "No discussions" in html
        assert 'Discord ready' in html and 'Telegram ready' in html
        r = c.post("/discussions/new", data={"kind": "macro", "ref": "inflation", "title": "CPI week", "body": "the print",
                                               "ch_discord": "1", "ch_telegram": "1"})
        assert r.status_code == 303 and r.headers["location"].startswith("/discussions?id=")
        did = int(r.headers["location"].split("id=")[1].split("&")[0])
        html = c.get(f"/discussions?id={did}&status=all").text
        assert "CPI week" in html and 'href="/macro#inflation"' in html and "Open in Discord" in html and "Open in Telegram" in html
        assert "opener" in html and "the print" in html
        frag = c.post(f"/discussions/{did}/post", data={"body": "my view"}, headers={"HX-Request": "true"}).text
        assert "my view" in frag and 'hx-get="/discussions/%d/chat"' % did in frag
        assert "looks strong" in c.get(f"/discussions/{did}/chat").text          # the Discord sync ran on read
        assert c.post(f"/discussions/{did}/close").status_code == 303
        assert "This discussion is closed" in c.get(f"/discussions/{did}/chat").text
        health = c.get("/discussions/health").text
        assert "Discussions" not in health or "sync" in health
        assert "group id -1001234567890" in health
        # a member sees no New discussion form and cannot open one
        app.dependency_overrides.clear()
        m = _client(engine, user.id)
        assert "New discussion" not in m.get("/discussions").text
        assert m.post("/discussions/new", data={"title": "x"}).status_code in (401, 403)
    finally:
        app.dependency_overrides.clear()


def test_the_study_chat_reads_through_the_discussion(engine, db, mod, fake_chats):
    s = models.Setup(symbol="NVDA", title="Breakout", created_by=mod.id)
    db.add(s)
    db.commit()
    try:
        c = _client(engine, mod.id)
        html = c.get(f"/studies/{s.id}/chat").text
        assert "Open the discussion in Discord / Telegram" in html
        assert c.post(f"/studies/{s.id}/discord-thread").status_code == 303
        d = discussions.for_subject(db, "setup", str(s.id))
        assert d is not None and d.discord_thread_id == "th1" and d.telegram_topic_id == "55"
        html = c.get(f"/studies/{s.id}/chat").text
        assert "Reply in Discord" in html and "Reply in Telegram" in html and "looks strong" in html
        frag = c.post(f"/studies/{s.id}/comment", data={"body": "noted"}, headers={"HX-Request": "true"}).text
        assert "noted" in frag and fake_chats["discord_posts"][-1][1] == "Mod: noted"
        assert db.query(models.Comment).count() == 0                            # mirrored, not a legacy comment
    finally:
        app.dependency_overrides.clear()
