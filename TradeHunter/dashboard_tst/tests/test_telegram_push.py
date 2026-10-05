"""The Telegram ideas push (``services/telegram_push.py``) and the sender's
handshake pieces (``services/telegram.py``): the guards, the dedupe, the cap, the
text, the dry run, the opt-in switches and the request_code -> verify round trip.

Nothing touches the Bot API: ``telegram.send`` / ``send_code`` / ``_api`` are
replaced per test. Signal rows are written through ``option_store.upsert_signal``
so the push reads exactly what the nightly job would have stored.
"""
from __future__ import annotations

import datetime as _dt

import pytest

from app import models
from app.services import clock, option_prefs, option_store, telegram, telegram_push

RUN_ON = "2026-10-02"              # a Friday: last_trading_day(RUN_ON) == RUN_ON
HOUSE = option_prefs.HOUSE_HASH
HEADLINE = "Uptrend for 34 days. It bounced off support at 340 on high volume."
MUST = "LRCX stays above 330 until Nov 20. You keep the credit if it does nothing."


# ───────────────────────────────────── builders ─────────────────────────────────────

def _member(db, email="m1@local.test", *, chat_id="12345", enabled=True, verified=True,
            quiet=False, paused_until=None, role=models.ROLE_MEMBER):
    u = models.User(email=email, display_name=email.split("@")[0], role=role,
                    status=models.APPROVED)
    db.add(u)
    db.commit()
    telegram_push._write_telegram(db, u, {"enabled": enabled, "chat_id": chat_id, "verified": verified,
                                          "quiet": quiet, "paused_until": paused_until, "pending": None})
    return u


def _basket(db, user, *symbols):
    for i, s in enumerate(symbols):
        db.add(models.OptionBasket(user_id=user.id, owner_key=f"u{user.id}", symbol=s, source="typed",
                                   active=True, added_on=RUN_ON, pos=i))
    db.commit()


def _pick(symbol, short_strike=330.0, *, pop_kind="keep"):
    return {"symbol": symbol, "strategy": "bull_put", "family": "credit_vertical", "status": "ok",
            "legs": [{"expiry": "2026-11-20", "right": "P", "strike": short_strike, "side": "sell", "qty": 1,
                      "price": 5.70, "bid": 5.65, "ask": 5.75, "iv": 0.46, "delta": -0.25, "oi": 2140, "volume": 412},
                     {"expiry": "2026-11-20", "right": "P", "strike": short_strike - 10, "side": "buy", "qty": 1,
                      "price": 3.60, "bid": 3.55, "ask": 3.65, "iv": 0.47, "delta": -0.174, "oi": 1630, "volume": 230}],
            "expiry": "2026-11-20", "dte": 48, "net": -2.10, "width": 10.0, "max_profit": 210.0,
            "max_loss": 790.0, "breakevens": [short_strike - 2.10], "pop": 0.75, "pop_kind": pop_kind,
            "pop_model": 0.73, "chart_stop": 336.2, "chart_stop_pl": -120.7, "rule_stop_pl": -158.0,
            "score": 0.328, "sizing": None}


def _signal(db, symbol, *, snap_on=RUN_ON, phash=HOUSE, status="ok", basis="rank", provisional=False,
            earnings="2026-10-22", step=1, score=90.1, short_strike=330.0, atr=11.54, recommended=True,
            picks_ok=True, headline=HEADLINE, as_of=None):
    strategies = [{"key": "bull_put", "label": "Bull put spread", "fit": "recommended" if recommended else "also_fits",
                   "score": score, "step": step, "why": "...", "must_happen": MUST,
                   "reasons": [], "reason_key": None, "shown": True},
                  {"key": "buy_call", "label": "Buy call", "fit": "rejected", "score": None, "step": 2,
                   "why": None, "must_happen": None, "reasons": ["options too expensive to buy (IV rank 62)"],
                   "reason_key": "expensive", "shown": True}]
    picks = {"bull_put": [_pick(symbol, short_strike)] if picks_ok
             else [{"status": "nearest", "degenerate": {"reason_key": "no_band"}}]}
    sig = {"status": status, "headline": headline,
           "setup": {"kind": "support_bounce", "direction": "long", "atr": atr, "level": 340.9,
                     "plan": {"entry": 349.2, "stop": 336.2, "target": 375.2, "r": 13.0},
                     "stop": 336.2, "target": 375.2, "rng": {"sideways": False}},
           "iv": {"iv30": 46.0, "hv20": 38.0, "iv_rank": 62.0, "iv_pct": 71.0, "iv_n": 252,
                  "basis": basis, "provisional": provisional, "state": "ok",
                  "earnings_date": earnings, "earnings_days": 20, "verdict": "SELL"},
           "strategies": strategies, "picks": picks, "computed_ms": 120,
           "engine_version": option_store._engine_version() or "test"}
    row = option_store.upsert_signal(db, sig, prefs_hash=phash, symbol=symbol, snap_on=snap_on, kind="eod",
                                     as_of=as_of or _dt.datetime(2026, 10, 2, 20, 0, 0))
    db.commit()
    return row


def _header(db, symbol, *, snap_on=RUN_ON, partial=False):
    option_store.upsert_iv_daily(db, {"symbol": symbol, "snap_on": snap_on, "kind": "eod", "source": "cboe",
                                      "as_of": _dt.datetime(2026, 10, 2, 20, 0, 0), "spot": 349.2,
                                      "iv30": 46.0, "partial": partial, "rows": []})
    db.commit()


def _idea(db, user, symbol, **kw):
    _basket(db, user, symbol)
    _header(db, symbol, partial=kw.pop("partial", False))
    return _signal(db, symbol, **kw)


@pytest.fixture
def sender(monkeypatch):
    """A configured bot whose sends are recorded instead of posted."""
    sent = []
    monkeypatch.setattr(telegram, "configured", lambda: True)
    monkeypatch.setattr(telegram, "send", lambda html, *, chat_id=None: (sent.append((chat_id, html)) or (True, "")))
    monkeypatch.setattr(telegram, "answer_starts", lambda db: 0)
    monkeypatch.setattr(telegram_push, "_warned_unconfigured", False)
    return sent


def _rows(db, user):
    return db.query(models.OptionIdeaPush).filter(models.OptionIdeaPush.user_id == user.id).all()


def _logs_on():
    """Alembic's ``fileConfig`` (run by the migration in the ``db`` fixture) disables
    every logger that already existed - the deploy scripts re-enable theirs the
    same way - so caplog sees the module's lines again."""
    telegram_push.log.disabled = False
    telegram.log.disabled = False


# ─────────────────────────────────────── the text ───────────────────────────────────────

def test_first_line_text_and_never_a_contract_count(db, sender):
    u = _member(db)
    _idea(db, u, "LRCX")
    res = telegram_push.run(db, as_of=RUN_ON)
    assert res == {"members": 1, "ideas": 1, "sent": 1, "failed": 0, "skipped": {}}
    assert len(sender) == 1
    chat_id, html = sender[0]
    assert chat_id == "12345"
    first = html.split("\n", 1)[0]
    assert first == "<b>Ideas for tonight's US session (opens 21:30 Malaysia). Prices are last night's close.</b>"
    assert "Options · 1 new idea · Oct 2" in html
    assert "<b>LRCX</b> ↗ uptrend · IV rank 62 over the last year → <b>sell put</b>" in html
    assert HEADLINE in html
    assert "Nov 20 330/320 put · collect ≈ $210 · risk $790 · about 75% chance of keeping it (estimate)" in html
    assert f"What has to happen: {MUST}" in html
    assert "Stop: LRCX under 336.2 (about −$121 today) · earnings Oct 22 inside this trade" in html
    assert '<a href="' in html and "/options?symbol=LRCX" in html and "open the card" in html
    assert "/options?pause=7" in html and "pause for 7 days" in html
    assert "contract" not in html.lower()
    assert "step" not in html.lower() and "delta" not in html.lower()
    assert "and " not in html.split("\n")[-1] or "more on the page" not in html      # no "more" line for one idea
    rows = _rows(db, u)
    assert len(rows) == 1 and rows[0].idea_key == "LRCX|bull_put|2026-11-20"
    assert rows[0].ok is True and rows[0].error is None and rows[0].short_strike == 330.0 and rows[0].atr == 11.54


def test_debit_chance_word_is_profit(db, sender):
    u = _member(db)
    _basket(db, u, "ISRG")
    _header(db, "ISRG")
    sig = _signal(db, "ISRG")
    picks = {"bull_put": [dict(_pick("ISRG"), net=3.40, pop_kind="profit", pop=0.55)]}
    sig.picks = picks
    db.commit()
    telegram_push.run(db, as_of=RUN_ON)
    html = sender[0][1]
    assert "pay ≈ $340" in html and "about 55% chance of profit (estimate)" in html


# ─────────────────────────────────────── the guards ───────────────────────────────────────

@pytest.mark.parametrize("kw, reason", [
    ({"status": "no_setup"}, "no_setup"),
    ({"partial": True}, "partial chain"),
    ({"basis": "provisional", "provisional": True}, "IV history too short"),
    ({"basis": "unknown", "provisional": True}, "IV history too short"),
    ({"earnings": None}, "earnings date unknown"),
    ({"step": 2}, "not available yet"),
    ({"snap_on": "2026-10-01"}, "stale"),
])
def test_each_guard_skips_and_logs(db, sender, caplog, kw, reason):
    u = _member(db)
    _idea(db, u, "LRCX", **kw)
    _logs_on()
    with caplog.at_level("INFO", logger="app.services.telegram_push"):
        res = telegram_push.run(db, as_of=RUN_ON)
    assert res["sent"] == 0 and res["ideas"] == 0 and sender == []
    assert res["skipped"] == {reason: 1}
    assert any(f"skipped: {reason}" in r.getMessage() for r in caplog.records)
    assert _rows(db, u) == []


def test_no_recommendation_or_no_pick_is_silent(db, sender):
    u = _member(db)
    _idea(db, u, "AAA", recommended=False)
    _idea(db, u, "BBB", picks_ok=False)
    res = telegram_push.run(db, as_of=RUN_ON)
    assert res["sent"] == 0 and res["skipped"] == {} and sender == []


def test_member_hash_row_is_the_one_read(db, sender):
    """A member with saved rules reads THEIR row; the house row alone is 'not checked'."""
    u = _member(db)
    prefs, errors = option_prefs.write(db, u, "credit", {"credit_vertical.short_delta_hi": "0.35"})
    assert errors == [] and option_prefs.read(db, u)["telegram"]["enabled"]   # the write kept telegram
    mine = option_prefs.prefs_hash(prefs)
    _idea(db, u, "LRCX")                                   # house row only
    assert telegram_push.run(db, as_of=RUN_ON)["sent"] == 0
    _signal(db, "LRCX", phash=mine, short_strike=325.0)    # now the member's own row
    assert telegram_push.run(db, as_of=RUN_ON)["sent"] == 1
    assert "325/315" in sender[0][1]


# ─────────────────────────────────────── the dedupe ───────────────────────────────────────

def test_dedupe_half_atr_vs_1_2_atr(db, sender):
    u = _member(db)
    _idea(db, u, "LRCX", short_strike=330.0, atr=10.0)
    assert telegram_push.run(db, as_of=RUN_ON)["sent"] == 1
    assert telegram_push.run(db, as_of=RUN_ON) == {"members": 1, "ideas": 0, "sent": 0, "failed": 0,
                                                   "skipped": {"already sent": 1}}
    _signal(db, "LRCX", short_strike=335.0, atr=10.0)      # 0.5 ATR: the same idea
    res = telegram_push.run(db, as_of=RUN_ON)
    assert res["sent"] == 0 and res["skipped"] == {"already sent": 1} and len(sender) == 1
    _signal(db, "LRCX", short_strike=342.0, atr=10.0)      # 1.2 ATR from the pushed 330: re-push
    res = telegram_push.run(db, as_of=RUN_ON)
    assert res["sent"] == 1 and len(sender) == 2
    rows = _rows(db, u)
    assert len(rows) == 1 and rows[0].short_strike == 342.0        # UPDATED, not a second row
    # a new expiry is a new idea whatever the strike
    sig = _signal(db, "LRCX", short_strike=342.0, atr=10.0)
    for leg in sig.picks["bull_put"][0]["legs"]:
        leg["expiry"] = "2026-12-18"
    sig.picks = {"bull_put": [dict(sig.picks["bull_put"][0], expiry="2026-12-18")]}
    db.commit()
    assert telegram_push.run(db, as_of=RUN_ON)["sent"] == 1
    assert {r.idea_key for r in _rows(db, u)} == {"LRCX|bull_put|2026-11-20", "LRCX|bull_put|2026-12-18"}


def test_idea_key():
    assert telegram_push.idea_key(" lrcx ", "bull_put", "2026-11-20T00:00") == "LRCX|bull_put|2026-11-20"


# ─────────────────────────────────────── the cap ───────────────────────────────────────

def test_seven_ideas_cap_at_five_by_score_plus_and_two_more(db, sender):
    u = _member(db)
    scores = {"AAA": 70.0, "BBB": 95.0, "CCC": 80.0, "DDD": 60.0, "EEE": 90.0, "FFF": 85.0, "GGG": 75.0}
    for sym, sc in scores.items():
        _idea(db, u, sym, score=sc)
    res = telegram_push.run(db, as_of=RUN_ON)
    assert res["ideas"] == 5 and res["sent"] == 5 and len(sender) == 1
    html = sender[0][1]
    assert "Options · 7 new ideas · Oct 2" in html
    assert "and 2 more on the page · " in html.split("\n")[-1]
    order = [s for s in scores if f"<b>{s}</b>" in html]
    shown = sorted(scores, key=lambda s: -scores[s])[:5]
    assert set(order) == set(shown) and html.index("<b>BBB</b>") < html.index("<b>EEE</b>") < html.index("<b>FFF</b>")
    assert "<b>DDD</b>" not in html and "<b>AAA</b>" not in html
    assert {r.symbol for r in _rows(db, u)} == set(shown)          # the two "more" are not recorded


# ─────────────────────────────────────── dry run / off ───────────────────────────────────────

def test_dry_run_writes_rows_and_sends_nothing(db, monkeypatch, caplog):
    monkeypatch.setattr(telegram, "configured", lambda: False)      # not even configured
    monkeypatch.setattr(telegram, "answer_starts", lambda db: 0)
    monkeypatch.setattr(telegram, "send", lambda *a, **k: pytest.fail("send must not be called on a dry run"))
    u = _member(db)
    _idea(db, u, "LRCX")
    _logs_on()
    with caplog.at_level("INFO", logger="app.services.telegram_push"):
        res = telegram_push.run(db, as_of=RUN_ON, dry_run=True)
    assert res == {"members": 1, "ideas": 1, "sent": 0, "failed": 0, "skipped": {}}
    assert any("dry-run" in r.getMessage() and "Ideas for tonight" in r.getMessage() for r in caplog.records)
    rows = _rows(db, u)
    assert len(rows) == 1 and rows[0].ok is False and rows[0].error == "dry-run"
    assert telegram_push.run(db, as_of=RUN_ON, dry_run=True)["skipped"] == {"already sent": 1}


def test_not_configured_is_logged_once_and_skipped(db, monkeypatch, caplog):
    monkeypatch.setattr(telegram, "configured", lambda: False)
    monkeypatch.setattr(telegram_push, "_warned_unconfigured", False)
    u = _member(db)
    _idea(db, u, "LRCX")
    _logs_on()
    with caplog.at_level("WARNING", logger="app.services.telegram_push"):
        a = telegram_push.run(db, as_of=RUN_ON)
        b = telegram_push.run(db, as_of=RUN_ON)
    assert a == b == {"members": 0, "ideas": 0, "sent": 0, "failed": 0, "skipped": {"not configured": 1}}
    assert sum("not configured" in r.getMessage() for r in caplog.records) == 1
    assert _rows(db, u) == []


def test_send_failure_is_recorded_and_the_next_member_still_runs(db, monkeypatch):
    monkeypatch.setattr(telegram, "configured", lambda: True)
    monkeypatch.setattr(telegram, "answer_starts", lambda db: 0)
    monkeypatch.setattr(telegram, "send", lambda html, *, chat_id=None: (chat_id != "111", "" if chat_id != "111" else "HTTP 403 blocked"))
    a = _member(db, "a@local.test", chat_id="111")
    b = _member(db, "b@local.test", chat_id="222")
    _idea(db, a, "LRCX")
    _basket(db, b, "LRCX")
    res = telegram_push.run(db, as_of=RUN_ON)
    assert res["members"] == 2 and res["sent"] == 1 and res["failed"] == 1
    ra, rb = _rows(db, a)[0], _rows(db, b)[0]
    assert ra.ok is False and "403" in ra.error and rb.ok is True


# ─────────────────────────────────────── the switches ───────────────────────────────────────

def test_quiet_paused_unverified_and_disabled_members_are_skipped(db, sender):
    tomorrow = (_dt.date.fromisoformat(RUN_ON) + _dt.timedelta(days=1)).isoformat()
    _member(db, "q@local.test", quiet=True)
    _member(db, "p@local.test", paused_until=tomorrow)
    _member(db, "u@local.test", verified=False)
    _member(db, "d@local.test", enabled=False)
    ok = _member(db, "ok@local.test", paused_until=RUN_ON)     # the pause ended today
    for u in db.query(models.User).all():
        _basket(db, u, "LRCX")
    _header(db, "LRCX")
    _signal(db, "LRCX")
    res = telegram_push.run(db, as_of=RUN_ON)
    assert res["members"] == 1 and res["sent"] == 1 and sender[0][0] == "12345"
    assert [r.user_id for r in db.query(models.OptionIdeaPush).all()] == [ok.id]


def test_admin_blank_chat_id_falls_back_to_the_vault_id(db, sender, monkeypatch):
    monkeypatch.setattr(telegram, "default_chat_id", lambda: "99900")
    _member(db, "admin@local.test", chat_id=None, verified=False, role=models.ROLE_ADMIN)
    _member(db, "m@local.test", chat_id=None, verified=False)     # a member needs a verified id
    for u in db.query(models.User).all():
        _basket(db, u, "LRCX")
    _header(db, "LRCX")
    _signal(db, "LRCX")
    res = telegram_push.run(db, as_of=RUN_ON)
    assert res["members"] == 1 and sender[0][0] == "99900"


def test_ideas_new_counts_rows_since_seen(db, sender):
    u = _member(db)
    _idea(db, u, "LRCX")
    telegram_push.run(db, as_of=RUN_ON)
    row = _rows(db, u)[0]
    assert telegram_push.ideas_new(db, u, None) == 1
    assert telegram_push.ideas_new(db, u, (row.sent_at - _dt.timedelta(minutes=1)).isoformat()) == 1
    assert telegram_push.ideas_new(db, u, (row.sent_at + _dt.timedelta(minutes=1)).isoformat() + "+00:00") == 0
    assert telegram_push.ideas_new(db, None, None) == 0


# ─────────────────────────────────────── the handshake ───────────────────────────────────────

def test_request_code_then_verify_round_trip(db, user, monkeypatch):
    sent_to = []
    monkeypatch.setattr(telegram, "configured", lambda: True)
    monkeypatch.setattr(telegram, "send_code", lambda chat_id: (sent_to.append(chat_id) or "042517"))
    before = option_prefs.read(db, user)
    assert before["telegram"] == option_prefs.TELEGRAM_DEFAULT

    tg, err = telegram_push.apply(db, user, "request_code", chat_id="abc")
    assert err and "digits" in err and sent_to == []
    tg, err = telegram_push.apply(db, user, "request_code", chat_id=" 9876543 ")
    assert err is None and sent_to == ["9876543"]
    assert tg["pending"]["chat_id"] == "9876543" and tg["pending"]["code"] == "042517"
    assert tg["enabled"] is False and tg["verified"] is False and tg["chat_id"] is None

    tg, err = telegram_push.apply(db, user, "verify", code="000000")
    assert err and "does not match" in err and tg["verified"] is False
    tg, err = telegram_push.apply(db, user, "verify", code="042 517")
    assert err is None
    assert (tg["enabled"], tg["verified"], tg["chat_id"], tg["pending"]) == (True, True, "9876543", None)

    after = option_prefs.read(db, user)
    assert after["telegram"]["chat_id"] == "9876543" and after["telegram"]["verified"]
    assert option_prefs.prefs_hash(after) == option_prefs.prefs_hash(before) == HOUSE      # never hashed
    assert user.option_prefs.prefs_hash == HOUSE

    # the member can now be pushed to
    _basket(db, user, "LRCX")
    _header(db, "LRCX")
    _signal(db, "LRCX")
    sent = []
    monkeypatch.setattr(telegram, "send", lambda html, *, chat_id=None: (sent.append(chat_id) or (True, "")))
    monkeypatch.setattr(telegram, "answer_starts", lambda db: 0)
    assert telegram_push.run(db, as_of=RUN_ON)["sent"] == 1 and sent == ["9876543"]


def test_verify_expired_or_without_a_pending_code(db, user, monkeypatch):
    tg, err = telegram_push.verify(db, user, "123456")
    assert err and "Send code first" in err
    monkeypatch.setattr(telegram, "configured", lambda: True)
    monkeypatch.setattr(telegram, "send_code", lambda chat_id: "123456")
    telegram_push.request_code(db, user, "5551234")
    _, tg = telegram_push._telegram_prefs(db, user)
    tg["pending"]["expires"] = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=1)).isoformat()
    telegram_push._write_telegram(db, user, tg)
    tg, err = telegram_push.verify(db, user, "123456")
    assert err and "expired" in err and tg["verified"] is False


def test_request_code_when_the_bot_cannot_deliver(db, user, monkeypatch):
    monkeypatch.setattr(telegram, "configured", lambda: False)
    tg, err = telegram_push.request_code(db, user, "5551234")
    assert err and "not set up" in err and tg["pending"] is None
    monkeypatch.setattr(telegram, "configured", lambda: True)

    def fail(chat_id):
        raise telegram.TelegramError("HTTP 400 chat not found")

    monkeypatch.setattr(telegram, "send_code", fail)
    tg, err = telegram_push.request_code(db, user, "5551234")
    assert err and "/start" in err and tg["pending"] is None


def test_quiet_pause_and_disable_actions(db, user, monkeypatch):
    monkeypatch.setattr(telegram, "configured", lambda: True)
    monkeypatch.setattr(telegram, "send_code", lambda chat_id: "111111")
    _, err = telegram_push.apply(db, user, "request_code", chat_id="7777777")
    assert err is None
    _, err = telegram_push.apply(db, user, "verify", code="111111")
    assert err is None

    tg, err = telegram_push.apply(db, user, "quiet")
    assert err is None and tg["quiet"] is True
    tg, _ = telegram_push.apply(db, user, "quiet", quiet="0")
    assert tg["quiet"] is False

    tg, err = telegram_push.apply(db, user, "pause", pause_days=7)
    assert err is None and tg["paused_until"] == (clock.et_date() + _dt.timedelta(days=7)).isoformat()
    tg, err = telegram_push.apply(db, user, "pause", pause_days=0)
    assert err and "1 to 90" in err
    tg, err = telegram_push.apply(db, user, "pause", pause_days=None)
    assert err is None and tg["paused_until"] == (clock.et_date() + _dt.timedelta(days=7)).isoformat()

    tg, err = telegram_push.apply(db, user, "disable")
    assert err is None and tg["enabled"] is False and tg["verified"] is True and tg["chat_id"] == "7777777"
    tg, err = telegram_push.apply(db, user, "nope")
    assert err and "not one we know" in err


# ─────────────────────────────────────── the sender ───────────────────────────────────────

def test_chunks_split_on_lines_under_the_limit():
    text = "\n".join("x" * 100 for _ in range(100))         # 10,100 chars
    parts = telegram.chunks(text)
    assert len(parts) == 3 and all(len(p) <= telegram.MAX_CHUNK for p in parts)
    assert "\n".join(parts) == text
    assert telegram.chunks("short") == ["short"]


def test_send_without_a_token_or_chat_id(monkeypatch):
    monkeypatch.setattr(telegram, "creds", lambda: (None, None))
    assert telegram.send("hi", chat_id="1") == (False, "not configured")
    monkeypatch.setattr(telegram, "creds", lambda: ("tok", None))
    assert telegram.send("hi") == (False, "no chat id")


def test_answer_starts_keeps_the_offset_in_one_job_row(db, monkeypatch):
    seen = []
    replies = []
    updates = [{"update_id": 10, "message": {"text": "/start", "chat": {"id": 555}}},
               {"update_id": 11, "message": {"text": "hello", "chat": {"id": 556}}},
               {"update_id": 12, "message": {"text": "/start@TradeHunterBot", "chat": {"id": 557}}}]

    def api(method, data):
        seen.append((method, data.get("offset")))
        off = int(data.get("offset") or 0)
        return {"ok": True, "result": [u for u in updates if u["update_id"] >= off]}

    monkeypatch.setattr(telegram, "creds", lambda: ("tok", "1"))
    monkeypatch.setattr(telegram, "_api", api)
    monkeypatch.setattr(telegram, "send", lambda html, *, chat_id=None: (replies.append((chat_id, html)) or (True, "")))

    assert telegram.answer_starts(db) == 2
    assert [c for c, _ in replies] == ["555", "557"] and "Your chat id is 555." in replies[0][1]
    rows = db.query(models.OptionJob).filter(models.OptionJob.job == "telegram_poll").all()
    assert len(rows) == 1 and rows[0].detail["offset"] == 13 and rows[0].finished_at is not None
    assert telegram.answer_starts(db) == 0                       # nothing new past the offset
    assert seen[-1] == ("getUpdates", 13)
    assert db.query(models.OptionJob).filter(models.OptionJob.job == "telegram_poll").count() == 1   # reused
    assert rows[0].detail["answered_total"] == 2


def test_send_code_raises_when_undelivered(monkeypatch):
    monkeypatch.setattr(telegram, "send", lambda html, *, chat_id=None: (False, "HTTP 400 chat not found"))
    with pytest.raises(telegram.TelegramError):
        telegram.send_code("1")
    monkeypatch.setattr(telegram, "send", lambda html, *, chat_id=None: (True, ""))
    code = telegram.send_code("1")
    assert len(code) == 6 and code.isdigit()
