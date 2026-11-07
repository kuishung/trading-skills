"""Insights -> Macro, the seven topics (v4.243): the topic definitions, the readings against the
course's rules, the per-tab readings, the link sheet, the page and its API, the daily refresh
and the tray line."""
from __future__ import annotations

import datetime as _dt
import json

import pytest

from app import models
from app.main import app
from app.services import indicators, macro_links, macro_refresh, macro_topics
from tests.test_options_page import _client


# ───────────────────────────────────────── helpers ─────────────────────────────────────────

def _monthly(db, key, values, end=_dt.datetime(2026, 10, 1)):
    """Store `values` as monthly readings ending at `end` (oldest first)."""
    rows = []
    y, m = end.year, end.month
    for v in reversed(values):
        rows.append(models.MacroReading(indicator_key=key, as_of=_dt.datetime(y, m, 1), value=v))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    db.add_all(rows)
    db.commit()


def _quarterly(db, key, values, end=_dt.datetime(2026, 7, 1)):
    rows = []
    y, m = end.year, end.month
    for v in reversed(values):
        rows.append(models.MacroReading(indicator_key=key, as_of=_dt.datetime(y, m, 1), value=v))
        m -= 3
        if m <= 0:
            y, m = y - 1, m + 12
    db.add_all(rows)
    db.commit()


def _level_series(key, level_now, n=30):
    """A YoY-transformed indicator stores LEVELS: build n months whose last YoY is `level_now`%."""
    base = 100.0
    out = []
    for i in range(n):
        out.append(base * (1 + level_now / 100.0) ** (i / 12.0))
    return out


@pytest.fixture
def seeded(db):
    indicators.ensure_seeded(db)
    return db


# ───────────────────────────────────── the definitions ─────────────────────────────────────

def test_seven_topics_whose_series_are_all_seeded():
    assert macro_topics.TOPIC_KEYS == ["inflation", "fed_funds", "dollar", "unemployment", "cycle", "growth", "debt"]
    seeded_keys = {row[0] for row in indicators.SEED}
    for t in macro_topics.TOPICS:
        for s in t["series"]:
            assert s["key"] in seeded_keys, (t["key"], s["key"])
        for p in t.get("panels") or []:
            for k in p["series"]:
                assert k in seeded_keys
        assert t["text"]["interpret"] and t["text"]["takeaways"]
        assert t["key"] in macro_topics.GLANCE


def test_every_link_on_the_sheet_sits_on_a_topic_page():
    """User, 2026-11-07: no separate tab - every link goes to the relevant topic page(s)."""
    for label, url, topics in macro_links.all_items():
        assert topics and all(k in macro_topics.TOPIC_KEYS for k in topics), (label, topics)
        assert url.startswith("http"), label
    assert len(macro_links.all_urls()) >= 100
    fed = macro_links.for_topic("fed_funds")
    assert any(g["group"] == "Monetary policy" for g in fed)
    assert any("FedWatch" in it["label"] for g in fed for it in g["items"])
    assert any("Fidelity" in it["label"] for g in macro_links.for_topic("cycle") for it in g["items"])


def test_ensure_seeded_adds_the_new_series_to_a_live_table(db):
    """A DB seeded before v4.243 holds the study board's indicators only; the topics' series are
    added without touching the rows that exist."""
    db.add(models.MacroIndicator(key="cpi", section="growth_inflation", label="MY CPI LABEL", source="fred",
                                 source_ref="CPIAUCSL", transform="yoy", sort=0, active=True))
    db.commit()
    added = indicators.ensure_seeded(db)
    assert added == len(indicators.SEED) - 1
    assert db.query(models.MacroIndicator).filter_by(key="cpi").one().label == "MY CPI LABEL"
    assert db.query(models.MacroIndicator).filter_by(key="gdpnow").one().source_ref == "GDPNOW"
    assert indicators.ensure_seeded(db) == 0


def test_the_public_fred_csv_is_parsed_without_a_key():
    rows = indicators.parse_fred_csv("observation_date,UNRATE\n2026-08-01,4.3\n2026-09-01,.\n2026-10-01,4.1\n")
    assert rows == [(_dt.datetime(2026, 8, 1), 4.3), (_dt.datetime(2026, 10, 1), 4.1)]


def test_a_refresh_updates_a_revised_value(seeded, monkeypatch):
    ind = seeded.query(models.MacroIndicator).filter_by(key="gdp_qoq").one()
    monkeypatch.setattr(indicators, "_fetch_fred",
                        lambda *_a, **_k: [(_dt.datetime(2026, 4, 1), 3.0), (_dt.datetime(2026, 7, 1), 3.4)])
    r1 = indicators.refresh(seeded, ind)
    monkeypatch.setattr(indicators, "_fetch_fred",
                        lambda *_a, **_k: [(_dt.datetime(2026, 4, 1), 3.0), (_dt.datetime(2026, 7, 1), 3.8)])
    r2 = indicators.refresh(seeded, ind)
    assert (r1["added"], r1["revised"]) == (2, 0) and (r2["added"], r2["revised"]) == (0, 1)
    assert indicators.points_for(seeded, ind)[-1][1] == 3.8


# ───────────────────────────────────────── readings ────────────────────────────────────────

def test_cpi_inside_the_zone_reads_as_goldilocks(seeded):
    _monthly(seeded, "cpi", _level_series("cpi", 3.0))
    r = macro_topics.reading(seeded, "inflation")
    assert r["tone"] == "ok" and "Goldilocks" in r["word"]
    assert r["headline"]["label"] == "CPI (YoY)" and r["headline"]["text"] == "3.0%"
    assert "2-4% target" in r["sentence"]
    assert r["gauge"]["lo"] == 2.0 and r["gauge"]["hi"] == 4.0 and abs(r["gauge"]["value"] - 3.0) < 0.05


def test_cpi_above_four_leans_to_hiking_and_a_rising_streak_is_flagged(seeded):
    levels = _level_series("cpi", 4.0)
    levels[-3:] = [levels[-4] * 1.004, levels[-4] * 1.009, levels[-4] * 1.015]   # three rising months
    _monthly(seeded, "cpi", levels)
    r = macro_topics.reading(seeded, "inflation")
    assert r["word"] == "Above the zone" and "hiking" in r["sentence"]
    assert any("Rising for 3 months" in f["text"] for f in r["flags"])


def test_each_inflation_tab_has_its_own_reading_and_release(seeded):
    """User, 2026-11-07: "when i press CPI, PCE and PPI the top data did not change"."""
    _monthly(seeded, "cpi", _level_series("cpi", 3.7))
    _monthly(seeded, "pce", _level_series("pce", 2.9))
    _monthly(seeded, "ppi", _level_series("ppi", 6.3))
    p = macro_topics.payload(seeded, "inflation")
    assert [x["key"] for x in p["panels"]] == ["cpi", "pce", "ppi"]
    by = {x["key"]: x for x in p["panels"]}
    # v4.244/4.245: the Fed Funds Rate, SPY and QQQ are ticks on every inflation chart, off until ticked
    assert all(x["series"][1:] == ["effr", "spy", "qqq"] for x in p["panels"])
    by_s = {s["key"]: s for s in p["series"]}
    assert by_s["effr"]["toggle"] is True and by_s["effr"]["on"] is False and by_s["effr"]["label"] == "Fed Funds Rate (effective)"
    assert by_s["spy"]["toggle"] and by_s["spy"]["axis"] == "left" and by_s["spy"]["unit"] == "$" and by_s["spy"]["source"] == "Yahoo SPY"
    assert by_s["qqq"]["toggle"] and by_s["qqq"]["axis"] == "left" and by_s["qqq"]["on"] is False
    assert all(s["toggle"] is False for s in p["series"] if s["key"] not in ("effr", "spy", "qqq"))
    assert by["cpi"]["reading"]["headline"]["label"] == "CPI (YoY)"
    assert by["pce"]["reading"]["headline"]["text"] == "2.9%" and by["pce"]["band"] == {"lo": 2.0, "hi": 4.0, "label": "Goldilocks zone 2-4%"}
    ppi = by["ppi"]["reading"]
    assert by["ppi"]["band"] is None and ppi["headline"]["text"] == "6.3%"
    assert ppi["gauge"]["lo"] is None and any(m["label"] == "CPI" for m in ppi["gauge"]["marks"])
    assert any("pipeline" in f["text"] for f in ppi["flags"])
    assert "no target band" in ppi["sentence"]
    assert p["links"] and all(g["items"] for g in p["links"])


def test_unemployment_rising_three_months_and_the_sahm_rule(seeded):
    _monthly(seeded, "unrate", [3.9] * 20 + [4.0, 4.1, 4.3])
    _monthly(seeded, "sahm", [0.1] * 22 + [0.53])
    r = macro_topics.reading(seeded, "unemployment")
    assert r["headline"]["text"] == "4.3%"
    assert any("Rising for 3 months" in f["text"] for f in r["flags"])
    assert any("Sahm" in f["text"] for f in r["flags"])
    assert r["gauge"]["lo"] == 4.0 and r["gauge"]["hi"] == 6.0


def test_two_negative_quarters_read_as_a_technical_recession(seeded):
    _quarterly(seeded, "gdp_qoq", [2.0, 1.5, -0.6, -1.1])
    r = macro_topics.reading(seeded, "growth")
    assert r["tone"] == "bad" and r["word"] == "Technical recession"
    assert r["headline"]["text"] == "-1.1%" and r["trend"]["prev"] == "-0.6%"
    cyc = macro_topics.reading(seeded, "cycle")
    assert cyc["stage"] == "Contraction" and cyc["gauge"] == {"kind": "stages", "stages": ["Expansion", "Peak", "Contraction", "Trough"], "active": "Contraction"}
    assert "Utilities" in cyc["glance"]["good"]


def test_cycle_stage_rules():
    q = [(_dt.datetime(2026, 1, 1), 2.0), (_dt.datetime(2026, 4, 1), 2.5), (_dt.datetime(2026, 7, 1), 3.0)]
    flat_u = [(_dt.datetime(2026, m, 1), 4.1) for m in range(1, 11)]
    assert macro_topics.cycle_stage(q, flat_u, [(_dt.datetime(2026, 9, 1), 3.0)], [])["stage"] == "Expansion"
    hot = macro_topics.cycle_stage(q, flat_u, [(_dt.datetime(2026, 9, 1), 5.0)], [(_dt.datetime(2026, 10, 1), -0.3)])
    assert hot["stage"] == "Peak" and any("inverted" in s for s in hot["reasons"])
    up = q[:2] + [(_dt.datetime(2026, 7, 1), -0.4)]
    assert macro_topics.cycle_stage(up, flat_u, [], [])["stage"] == "Peak"
    rec = [(_dt.datetime(2026, 4, 1), -0.4), (_dt.datetime(2026, 7, 1), 1.2)]
    assert macro_topics.cycle_stage(rec, flat_u, [], [])["stage"] == "Early recovery"


def test_the_fed_reading_names_the_range_the_effr_and_the_markets_lean(seeded):
    day = _dt.datetime(2026, 11, 5)
    for key, v in (("fftr_upper", 4.0), ("fftr_lower", 3.75), ("effr", 3.88), ("dgs2", 4.46)):
        seeded.add_all([models.MacroReading(indicator_key=key, as_of=day - _dt.timedelta(days=d), value=v)
                        for d in range(0, 400, 7)])
    seeded.commit()
    r = macro_topics.reading(seeded, "fed_funds")
    assert r["headline"]["text"] == "3.75%-4.00%" and r["word"] == "Held in range"
    assert "leans to HIKES" in r["sentence"]
    assert r["gauge"]["lo"] == 3.75 and r["gauge"]["hi"] == 4.0 and r["gauge"]["marks"][0]["label"] == "2y yield"


def test_the_dollar_reading_marks_the_52_week_range(seeded):
    day = _dt.datetime(2026, 11, 6)
    seeded.add_all([models.MacroReading(indicator_key="dxy", as_of=day - _dt.timedelta(days=d),
                                        value=100.0 + (d % 50) / 10.0) for d in range(0, 400)])
    seeded.commit()
    r = macro_topics.reading(seeded, "dollar")
    labels = {l["label"] for l in r["levels"]}
    assert {"52w high", "52w low", "200-day"} <= labels
    assert r["gauge"]["band_lo"] == 100.0 and r["gauge"]["band_hi"] == 104.9


def test_a_topic_without_data_reads_as_no_data_yet(seeded):
    r = macro_topics.reading(seeded, "debt")
    assert r["tone"] == "none" and r["word"] == "No data yet" and r["gauge"] is None


def test_the_next_release_follows_the_topic_and_the_tab():
    now = _dt.datetime.utcnow()
    events = [
        {"title": "PPI MoM", "when": now + _dt.timedelta(days=2), "period": "Oct. 2026", "forecast": "0.4%", "previous": "1.3%"},
        {"title": "Inflation Rate YoY", "when": now + _dt.timedelta(days=5), "period": "Oct. 2026", "forecast": "3.9%", "previous": "3.7%"},
        {"title": "PCE Price Index YoY", "when": now + _dt.timedelta(days=20), "period": "Oct. 2026", "forecast": None, "previous": "2.9%"},
        {"title": "Fed Interest Rate Decision", "when": now + _dt.timedelta(days=30), "period": None, "forecast": "3.75%", "previous": "4%"},
    ]
    assert macro_topics.next_release("inflation", events=events)["title"] == "PPI MoM"
    assert macro_topics.next_release("inflation", events=events, pattern=macro_topics._PRICE_INDEX["pce"][1])["title"] == "PCE Price Index YoY"
    assert macro_topics.next_release("fed_funds", events=events)["title"] == "Fed Interest Rate Decision"
    assert macro_topics.next_release("dollar", events=events) is None


# ───────────────────────────────────── the page and API ────────────────────────────────────

def test_the_macro_page_lists_the_seven_topics_and_no_sources_tab(engine, user):
    try:
        c = _client(engine, user.id)
        html = c.get("/macro").text
        api = c.get("/macro/api/topic/growth").json()
        study = c.get("/macro/study")
        bad = c.get("/macro/api/topic/nope").json()
    finally:
        app.dependency_overrides.clear()
    for label in ("Inflation", "Fed Funds Rate", "Exchange Rates", "Unemployment", "Economic Cycles",
                  "Economic Growth", "US National Debt"):
        assert label in html
    assert html.count('class="mc-tab ') == 7 and 'data-topic="sources"' not in html
    assert "Where to look it up" in html and "lightweight-charts" in html
    assert api["ok"] and [s["key"] for s in api["series"]] == ["gdp_qoq", "gdpnow", "gdp_yoy"]
    assert api["series"][0]["style"] == "bars" and api["panels"][0]["series"] == ["gdp_qoq", "gdpnow", "gdp_yoy"]
    assert api["reading"]["word"] == "No data yet" and api["links"]
    assert study.status_code == 200 and "Macro study" in study.text
    assert bad == {"ok": False, "error": "unknown topic"}


def test_the_data_ingest_page_has_the_macro_series_tab(engine, db):
    admin = models.User(email="admin@local.test", display_name="Admin", role=models.ROLE_ADMIN,
                        status=models.APPROVED, created_at=_dt.datetime(2026, 1, 5))
    db.add(admin)
    db.commit()
    try:
        c = _client(engine, admin.id)
        page = c.get("/finviz").text
        frag = c.get("/finviz/macro").text
    finally:
        app.dependency_overrides.clear()
    assert 'data-tab="macro"' in page and ">Macro Series</button>" in page
    assert "Not refreshed yet" in frag and "GDPNow (Atlanta Fed)" in frag and 'id="msRefresh"' in frag


# ───────────────────────────────── the daily refresh and the tray ──────────────────────────

def test_run_once_refreshes_every_series_and_mirrors_the_state(engine, tmp_path, monkeypatch):
    from sqlalchemy.orm import sessionmaker
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    calls = []

    def fake_refresh(db, ind, years=25):
        calls.append(ind.key)
        if ind.key == "dxy":
            return {"key": ind.key, "ok": False, "error": "boom", "added": 0}
        return {"key": ind.key, "ok": True, "added": 3, "revised": 1, "total": 3}

    monkeypatch.setattr(indicators, "refresh", fake_refresh)
    state = tmp_path / "macro_refresh.json"
    out = macro_refresh.run_once(Session, state_path=state)
    assert out["ok"] and out["indicators"] == len(indicators.SEED) and len(calls) == len(indicators.SEED)
    assert out["failed"] == [{"key": "dxy", "label": "Dollar index", "error": "boom"}]
    assert out["added"] == 3 * (len(indicators.SEED) - 1) and out["revised"] == len(indicators.SEED) - 1
    doc = json.loads(state.read_text(encoding="utf-8"))
    assert doc["last_finished"] and doc["failed"][0]["key"] == "dxy" and "results" not in doc


def test_the_tray_line_reads_the_state_file(tmp_path):
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2] / "dashboard_intraday"))
    try:
        import tray_status  # noqa: E402
    except (ImportError, SystemExit):      # the tray needs pystray + Pillow (Hermes); skip elsewhere
        pytest.skip("tray_status needs pystray")

    now = _dt.datetime(2026, 11, 7, 12, 0, tzinfo=_dt.timezone.utc)
    p = tmp_path / "macro_refresh.json"
    assert tray_status.get_macro_series_status(p, now)["state"] == "absent"
    p.write_text(json.dumps({"last_finished": "2026-11-07T11:00:00Z", "indicators": 33, "failed": [], "added": 5}))
    ok = tray_status.get_macro_series_status(p, now)
    assert ok["color"] == "green" and ok["tip"] == "Macro OK" and "33 series" in ok["line"] and "5 new" in ok["line"]
    p.write_text(json.dumps({"last_finished": "2026-11-07T11:00:00Z", "indicators": 33,
                             "failed": [{"key": "dxy", "error": "boom"}]}))
    bad = tray_status.get_macro_series_status(p, now)
    assert bad["color"] == "amber" and bad["tip"] == "Macro 1 fail" and "dxy" in bad["line"]
    p.write_text(json.dumps({"last_finished": "2026-11-05T11:00:00Z", "indicators": 33, "failed": []}))
    assert tray_status.get_macro_series_status(p, now)["state"] == "stale"
    p.write_text(json.dumps({"running": True, "indicators": 33}))
    assert tray_status.get_macro_series_status(p, now)["state"] == "running"
