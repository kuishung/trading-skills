"""v4.279 - option setups shared from the Option Finder (services/option_studies.py, routes/option_studies.py):
a moderator's right-click share saves the call + the chart snapshot, opens a discussion of kind ``option`` on
both chats with the picture, the page a chat link opens shows it all, the Curated page's panel lists them;
members cannot share."""
from __future__ import annotations

import base64

import pytest

from app import models
from app.services import discord, discussions, option_studies, telegram
from tests.test_admin_integrations import _own_db  # noqa: F401 - the settings reader on the test DB (autouse)
from tests.test_discussions import _client, fake_chats, mod  # noqa: F401 - the fixtures

# a 1x1 PNG
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
DATA_URL = "data:image/png;base64," + base64.b64encode(PNG).decode()

PAYLOAD = {
    "symbol": "nvda", "expiry": "2026-12-18", "strike": 200, "right": "C", "spot": 185.5, "entry": 185.5,
    "entry_src": "last", "target": 210.0, "worst": 170.0, "mode": "expiry", "valued_on": "2026-12-18",
    "figures": {"dte": 21, "iv": 0.42, "oi": 1500, "cost": 4.2, "at_target": 10.0, "at_worst": 0.0, "breakeven": 204.2,
                "be_pct": 0.1008, "rr": 1.38, "roi": 1.38, "prob": 0.31, "no_loss": False, "expiry": "2026-12-18"},
    "note": "Momentum call into the December opex",
}


@pytest.fixture
def snap_dir(tmp_path, monkeypatch):
    d = tmp_path / "snapshots"
    monkeypatch.setattr(option_studies, "SNAP_DIR", d)
    return d


@pytest.fixture
def fake_images(monkeypatch, fake_chats):  # noqa: F811
    """The image-capable openers recorded: Discord's create_study_thread gets ``image=``, Telegram's send_photo."""
    monkeypatch.setattr(discord, "create_study_thread",
                        lambda name, embed=None, image=None, image_name="snapshot.png":
                        (fake_chats["discord_threads"].append((name, embed, image)) or "th9"))
    photos = []
    monkeypatch.setattr(telegram, "send_photo",
                        lambda image, *, chat_id, caption_html="", thread_id=None, filename="snapshot.png":
                        (photos.append({"image": image, "chat_id": chat_id, "caption": caption_html, "thread_id": thread_id})
                         or {"message_id": 202}))
    fake_chats["photos"] = photos
    return fake_chats


def test_decode_image():
    assert option_studies.decode_image(DATA_URL) == PNG
    assert option_studies.decode_image("") is None
    assert option_studies.decode_image("data:image/jpeg;base64,AAAA") is None
    assert option_studies.decode_image("data:image/png;base64," + base64.b64encode(b"not a png").decode()) is None


def test_create_saves_snapshot_and_opens_on_both_chats(db, mod, fake_images, snap_dir):  # noqa: F811
    s = option_studies.create(db, mod, PAYLOAD, image=PNG)
    assert s.id and s.symbol == "NVDA" and s.strike == 200 and s.expiry == "2026-12-18" and s.entry_src == "last"
    assert s.figures["rr"] == 1.38 and "expiry" not in s.figures          # only the known figure keys are kept
    assert s.snapshot == f"option_{s.id}.png" and (snap_dir / s.snapshot).read_bytes() == PNG
    assert option_studies.snapshot_path(s) == snap_dir / s.snapshot
    d = option_studies.discussion_of(db, s)
    assert d is not None and d.kind == "option" and d.ref == str(s.id) and d.status == "open" and not d.channel_errors
    assert d.title == "NVDA 200 call Dec 18, 2026" == option_studies.title_of(s)
    assert discussions.subject_url("option", str(s.id)) == f"/setups/option/{s.id}"
    # Discord: the thread opener carried the picture and the setup's page link
    name, embed, image = fake_images["discord_threads"][-1]
    assert name == d.title and image == PNG
    assert "Reward / risk: 1.38" in embed["description"] and "Momentum call" in embed["description"]
    # Telegram: the topic, then the photo with the opener as the caption (not a plain message)
    assert fake_images["tg"][0][0] == "createForumTopic"
    assert [m for m, _ in fake_images["tg"]].count("sendMessage") == 0
    ph = fake_images["photos"][0]
    assert ph["image"] == PNG and ph["thread_id"] == "55" and "Breakeven: 204.20 (+10.1%)" in ph["caption"]
    assert f"/setups/option/{s.id}" in ph["caption"]
    # the figures the members read
    text = option_studies.details_text(s)
    assert "Target: 210.00 (+13.2%)" in text and "Worst case: 170.00 (-8.4%)" in text
    assert "Contract: 200 call, expires 2026-12-18 (21 days)" in text and "Implied volatility: 42.0%" in text
    assert option_studies.rr_url(s).startswith("/options/rr?symbol=NVDA&expiry=2026-12-18&strike=200&target=210.0&worst=170.0&mode=expiry")
    assert option_studies.recent(db)[0].id == s.id


def test_create_without_image_sends_a_message(db, mod, fake_images):  # noqa: F811
    s = option_studies.create(db, mod, PAYLOAD, image=None, channels=("telegram",))
    assert s.snapshot is None and not fake_images["photos"]
    assert [m for m, _ in fake_images["tg"]] == ["createForumTopic", "sendMessage"]
    d = option_studies.discussion_of(db, s)
    assert d.discord_thread_id is None and d.telegram_topic_id == "55"


def test_bad_payloads(db, mod, fake_images):  # noqa: F811
    with pytest.raises(option_studies.StudyError, match="ticker"):
        option_studies.create(db, mod, dict(PAYLOAD, symbol=""), image=None)
    with pytest.raises(option_studies.StudyError, match="expiry"):
        option_studies.create(db, mod, dict(PAYLOAD, expiry="soon"), image=None)
    with pytest.raises(option_studies.StudyError, match="strike"):
        option_studies.create(db, mod, dict(PAYLOAD, strike="x"), image=None)
    with pytest.raises(option_studies.StudyError, match="target"):
        option_studies.create(db, mod, dict(PAYLOAD, target="nan"), image=None)


def test_share_endpoint_page_and_panel(db, engine, mod, fake_images, snap_dir):  # noqa: F811
    c = _client(engine, mod.id)
    try:
        r = c.post("/setups/option", json=dict(PAYLOAD, image=DATA_URL, channels=["discord", "telegram"]))
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["ok"] and out["snapshot"] and out["url"] == f"/setups/option/{out['id']}"
        assert out["links"] == {"discord": "https://discord.com/channels/999/th9", "telegram": "https://t.me/c/1234567890/55"}
        sid = out["id"]
        # the page a chat link opens
        r = c.get(f"/setups/option/{sid}")
        assert r.status_code == 200
        body = r.text
        assert "NVDA 200 call Dec 18, 2026" in body and f"/setups/option/{sid}/snapshot.png" in body
        assert "Momentum call into the December opex" in body and "Reward / risk working" in body
        assert 'id="dscbox"' in body and f"/discussions/{out['discussion_id']}/post" in body
        assert "https://t.me/c/1234567890/55" in body
        r = c.get(f"/setups/option/{sid}/snapshot.png")
        assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content == PNG
        r = c.get(f"/setups/option/{sid}/chat")
        assert r.status_code == 200 and "opener" in r.text
        # the Curated page's panel: the list and the picked setup's discussion
        r = c.get("/setups/panel")
        assert r.status_code == 200 and 'id="osPanel"' in r.text and "NVDA 200C" in r.text and 'id="dscbox"' in r.text
        r = c.get(f"/setups/panel?sel={sid}")
        assert r.status_code == 200 and f"/setups/option/{sid}" in r.text
        assert c.get("/setups/option/999999").status_code == 404
        assert c.get("/setups/option/999999/snapshot.png").status_code == 404
        # a bad body
        r = c.post("/setups/option", json=dict(PAYLOAD, strike=0))
        assert r.status_code == 400 and "strike" in r.json()["error"]
        r = c.post("/setups/option", content=b"not json", headers={"Content-Type": "application/json"})
        assert r.status_code == 400
        # the curated list loads the panel
        r = c.get("/curated/list")
        assert r.status_code == 200 and 'hx-get="/setups/panel"' in r.text
    finally:
        c.app.dependency_overrides.clear()


def test_member_cannot_share(db, engine, fake_images):  # noqa: F811
    u = models.User(email="m@local.test", display_name="Mem", role=models.ROLE_MEMBER, status=models.APPROVED)
    db.add(u)
    db.commit()
    c = _client(engine, u.id)
    try:
        assert c.post("/setups/option", json=PAYLOAD).status_code == 403
        assert c.get("/setups/panel").status_code == 200           # members read the panel
    finally:
        c.app.dependency_overrides.clear()


def test_finder_boot_tells_the_page_who_may_share(db, engine, mod):  # noqa: F811
    c = _client(engine, mod.id)
    try:
        r = c.get("/options")
        assert r.status_code == 200
        assert '"can_moderate": true' in r.text and 'id="ofRowMenu"' in r.text and "Share this setup to Discord" in r.text
    finally:
        c.app.dependency_overrides.clear()
