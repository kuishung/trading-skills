"""The screener frame's reload rules (fix plan v4.137 step 7, OPTIONS_SCREENER_DESIGN.md §5).

* an EMPTY cached frame picks up the first rows on the very next request (bounded wait);
* concurrent requests on an empty frame load the DB once;
* ``reloading()`` is True only while a load really runs, never for a check that found nothing;
* while the FIRST pass runs, the frame grows - at most every 2 minutes - and never on a
  later pass (those reload when they finish);
* security types filed after a finished pass trigger a reload;
* a finished pass that stored no contracts says so (not "no data").

Every test runs on its own temp SQLite screener DB brought to head by the real
``alembic_screener`` migrations; nothing calls Massive.
"""
from __future__ import annotations

import datetime as dt
import threading
import time

import pytest

from app.services.screener import engine, frame as frame_mod

from .test_screener_engine import AS_OF, TODAY, UNDERLYINGS, chain, underlying_row


@pytest.fixture
def sdb(tmp_path, monkeypatch):
    """An empty, migrated screener DB; the ET date pinned to TODAY; a fresh frame cache."""
    from app import screener_db

    screener_db.configure("sqlite:///" + (tmp_path / "screener.db").as_posix())
    screener_db.init_screener_db()
    monkeypatch.setattr(frame_mod.clock, "et_date", lambda now=None: TODAY)
    frame_mod.reset()
    yield screener_db
    frame_mod.reset()
    screener_db.configure(None)


@pytest.fixture
def clock_s(monkeypatch):
    """The frame module's monotonic clock, moved by hand."""
    t = [10_000.0]
    monkeypatch.setattr(frame_mod, "_mono", lambda: t[0])
    return t


def _add(sdb, idx=(0,), *, sec_type=True, exp_days=(11, 39)):
    """Underlyings ``UNDERLYINGS[i]`` and their chains."""
    from app.screener_models import ScrContract, ScrUnderlying

    with sdb.session() as s:
        for i in idx:
            u = UNDERLYINGS[i]
            row = underlying_row(u)
            if not sec_type:
                row["sec_type"] = None
            s.add(ScrUnderlying(**row))
            for c in chain(u, exp_days=exp_days):
                s.add(ScrContract(**c))
        s.commit()


def _pass(sdb, pid, *, kind="eod", session="2026-10-09", finished=None, n_symbols=4, n_contracts=None):
    from app.screener_models import ScrPass

    with sdb.session() as s:
        s.add(ScrPass(id=pid, kind=kind, session=session, started=AS_OF, finished=finished,
                      n_symbols=n_symbols, n_contracts=n_contracts))
        s.commit()


def _type_all(sdb, value="stock"):
    from app.screener_models import ScrUnderlying

    with sdb.session() as s:
        for u in s.query(ScrUnderlying).all():
            u.sec_type = value
        s.commit()


def _count_loads(monkeypatch, delay=0.0):
    """Wrap load_from_db: count the calls and note ``reloading()`` while each runs."""
    calls = []
    real = frame_mod.load_from_db

    def wrapped(today=None):
        calls.append(frame_mod.reloading())
        if delay:
            time.sleep(delay)
        return real(today)

    monkeypatch.setattr(frame_mod, "load_from_db", wrapped)
    return calls


# ─────────────────────────────────── the empty-frame fast path ───────────────────────────────────

def test_empty_frame_returns_data_on_next_request_after_rows_land(sdb):
    fr = frame_mod.current()                      # warm() on an empty DB
    assert fr.empty and fr.meta["warnings"] == [frame_mod.NO_DATA]
    assert frame_mod.NO_DATA == "No option data loaded yet."
    _add(sdb, (0,))                               # the first underlying's chain lands
    fr2 = frame_mod.current()                     # the very next request: no 30 s wait
    assert not fr2.empty and fr2.symbols == ("AAA",)
    assert frame_mod.reloading() is False
    # and through the engine: rows, not NO_DATA
    out = engine.run("long-call", {"filters": []})
    assert out["total"] > 0 and frame_mod.NO_DATA not in out["warnings"]
    assert out["data"]["reloading"] is False


def test_empty_frame_check_is_throttled_and_not_repeated_for_unchanged_rows(sdb, monkeypatch, clock_s):
    _add(sdb, (0,), exp_days=(-3,))               # rows the loader drops (already expired)
    fr = frame_mod.current()
    assert fr.empty and fr.meta["max_cid"] is not None
    calls = _count_loads(monkeypatch)
    clock_s[0] += 60
    assert frame_mod.current() is fr              # same max id: nothing new to load
    frame_mod.wait_reload()
    assert calls == []
    _add(sdb, (1,))                               # a real row: loaded on the next due check
    clock_s[0] += 1                               # inside EMPTY_CHECK_S of the last check
    assert frame_mod.current() is fr
    clock_s[0] += frame_mod.EMPTY_CHECK_S
    assert not frame_mod.current().empty
    assert len(calls) == 1


def test_concurrent_current_loads_once(sdb, monkeypatch):
    assert frame_mod.current().empty
    _add(sdb, (0, 1))
    calls = _count_loads(monkeypatch, delay=0.3)
    got, barrier = [], threading.Barrier(8)

    def req():
        barrier.wait()
        got.append(frame_mod.current())

    ts = [threading.Thread(target=req) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    frame_mod.wait_reload()
    assert len(calls) == 1                        # spec + run + status at once: one load
    assert len(got) == 8 and all(not f.empty for f in got)
    assert len({id(f) for f in got}) == 1


def test_a_long_load_answers_with_the_empty_frame_and_reloading(sdb, monkeypatch):
    assert frame_mod.current().empty
    _add(sdb, (0,))
    monkeypatch.setattr(frame_mod, "EMPTY_WAIT_S", 0.05)
    gate = threading.Event()
    real = frame_mod.load_from_db

    def slow(today=None):
        gate.wait(5)
        return real(today)

    monkeypatch.setattr(frame_mod, "load_from_db", slow)
    fr = frame_mod.current()                      # the wait runs out: the empty frame ...
    assert fr.empty and frame_mod.reloading() is True     # ... and the page says it is loading
    out = engine.run("long-call", {"filters": []})
    assert out["data"]["reloading"] is True and "reloading" not in fr.meta   # a copy, never the shared meta
    gate.set()
    frame_mod.wait_reload()
    assert frame_mod.reloading() is False and not frame_mod.current().empty


# ─────────────────────────────────── reloading() ───────────────────────────────────

def test_reloading_false_after_noop_check(sdb, monkeypatch):
    _add(sdb, (0,))
    _pass(sdb, 1, finished=AS_OF, n_contracts=10)
    fr = frame_mod.current()
    assert not fr.empty and fr.meta["pass_id"] == 1
    seen = []
    real_check = frame_mod._needs_reload

    def check(cur):
        seen.append(frame_mod.reloading())
        return real_check(cur)

    monkeypatch.setattr(frame_mod, "_needs_reload", check)
    calls = _count_loads(monkeypatch)
    monkeypatch.setattr(frame_mod, "RELOAD_CHECK_S", 0.0)
    assert frame_mod.current() is fr
    frame_mod.wait_reload()
    assert seen == [False] and calls == [] and frame_mod.reloading() is False
    # a newer finished pass: the load runs with reloading() True, then it is False again
    _pass(sdb, 2, finished=AS_OF + dt.timedelta(hours=1), n_contracts=10)
    frame_mod.current()
    frame_mod.wait_reload()
    assert calls == [True] and frame_mod.reloading() is False
    assert frame_mod.current().meta["pass_id"] == 2


# ─────────────────────────────────── the first pass grows ───────────────────────────────────

def test_partial_first_pass_frame_grows_after_interval(sdb, monkeypatch, clock_s):
    _pass(sdb, 1, n_symbols=4)                    # the first pass is running (unfinished)
    _add(sdb, (0,))
    fr = frame_mod.current()
    assert fr.symbols == ("AAA",) and fr.meta["pass_id"] is None
    assert fr.meta["run_id"] == 1 and fr.meta["run_total"] == 4
    hm = frame_mod._et_hm(AS_OF)
    assert fr.meta["warnings"] == [f"The first market pass is still running: results cover 1 of 4 underlyings "
                                   f"read by {hm} ET (refreshed every 2 minutes)."]
    _add(sdb, (1,))
    clock_s[0] += 60
    assert frame_mod._needs_reload(fr) is False   # not before 2 minutes
    clock_s[0] += 61
    assert frame_mod._needs_reload(fr) is True
    monkeypatch.setattr(frame_mod, "RELOAD_CHECK_S", 0.0)
    frame_mod.current()
    frame_mod.wait_reload()
    fr2 = frame_mod.current()
    assert fr2.symbols == ("AAA", "BBB") and fr2.meta["pass_id"] is None
    # nothing new since: no reload, however long
    clock_s[0] += 10_000
    assert frame_mod._needs_reload(fr2) is False


def test_a_legacy_zero_contract_pass_does_not_stop_the_first_real_pass_growing(sdb, clock_s):
    fin = dt.datetime(2026, 10, 10, 16, 9)
    _pass(sdb, 1, kind="eod", session="2026-10-09", finished=fin, n_contracts=0)   # v4.136's empty pass
    _pass(sdb, 2, kind="eod", session="2026-10-09", n_symbols=4)                    # the first real pass runs
    _add(sdb, (0,))
    fr = frame_mod.current()
    assert fr.symbols == ("AAA",) and fr.meta["pass_id"] == 1 and fr.meta["real_pass_id"] is None
    hm = frame_mod._et_hm(AS_OF)
    assert fr.meta["warnings"] == [f"The first market pass is still running: results cover 1 of 4 underlyings "
                                   f"read by {hm} ET (refreshed every 2 minutes)."]
    _add(sdb, (1,))
    clock_s[0] += frame_mod.GROW_RELOAD_S + 1
    assert frame_mod._needs_reload(fr) is True              # it grows like any first pass


def test_an_older_real_pass_means_no_growth_and_no_first_pass_note(sdb, clock_s):
    _add(sdb, (0,))
    _pass(sdb, 1, finished=AS_OF, n_contracts=10)                                    # a real pass
    _pass(sdb, 2, finished=AS_OF + dt.timedelta(hours=1), n_contracts=0)             # a newer empty one
    _pass(sdb, 3, kind="cycle", session="2026-10-12")                               # one running
    fr = frame_mod.current()
    assert (fr.meta["pass_id"], fr.meta["real_pass_id"], fr.meta["real_pass_kind"]) == (2, 1, "eod")
    assert fr.meta["real_session"] == "2026-10-09" and fr.meta["real_finished"]
    assert fr.meta["warnings"] == []
    _add(sdb, (1,))
    clock_s[0] += 10_000
    assert frame_mod._needs_reload(fr) is False


def test_a_slow_load_slows_the_growth_reloads(sdb, clock_s):
    _pass(sdb, 1, n_symbols=4)
    _add(sdb, (0,))
    fr = frame_mod.current()
    fr.meta["load_ms"] = 60_000                   # a 60 s load: at most every 4 min
    _add(sdb, (1,))
    clock_s[0] += 200
    assert frame_mod._needs_reload(fr) is False
    clock_s[0] += 41
    assert frame_mod._needs_reload(fr) is True


def test_no_growth_reload_while_a_second_pass_runs(sdb, clock_s):
    _add(sdb, (0,))
    _pass(sdb, 1, finished=AS_OF, n_contracts=10)
    fr = frame_mod.current()
    assert fr.meta["pass_id"] == 1 and fr.meta["warnings"] == []
    _pass(sdb, 2, kind="cycle", session="2026-10-12")
    _add(sdb, (1,))                               # pass 2 files more rows while it runs
    clock_s[0] += 10_000
    assert frame_mod._needs_reload(fr) is False   # it reloads when pass 2 finishes, not before


def test_identity_after_finished_pass_reloads(sdb, monkeypatch, clock_s):
    _add(sdb, (0, 1), sec_type=False)
    _pass(sdb, 1, finished=AS_OF, n_contracts=10)
    fr = frame_mod.current()
    assert fr.meta["n_sec_type_unknown"] == 2 and fr.meta["n_typed"] == 0
    clock_s[0] += 200
    assert frame_mod._needs_reload(fr) is False   # no new types yet
    _type_all(sdb)
    clock_s[0] -= 100                             # 100 s after the load: not yet
    assert frame_mod._needs_reload(fr) is False
    clock_s[0] += 21
    assert frame_mod._needs_reload(fr) is True
    monkeypatch.setattr(frame_mod, "RELOAD_CHECK_S", 0.0)
    frame_mod.current()
    frame_mod.wait_reload()
    fr2 = frame_mod.current()
    assert fr2 is not fr and fr2.meta["n_sec_type_unknown"] == 0 and fr2.meta["n_typed"] == 2


# ─────────────────────────────────── warnings ───────────────────────────────────

def test_zero_contract_finished_pass_warning(sdb):
    fin = dt.datetime(2026, 10, 10, 16, 9)        # Sat 12:09 ET
    _pass(sdb, 3, kind="eod", session="2026-10-09", finished=fin, n_contracts=0)
    fr = frame_mod.current()
    assert fr.empty and fr.meta["pass_contracts"] == 0
    assert fr.meta["warnings"] == ["The last market pass (end-of-day Fri Oct 9, finished 12:09 ET) stored no "
                                   "contracts - see the collector status above."]
    out = engine.run("bull-put-spread", {})
    assert out["total"] == 0 and frame_mod.NO_DATA not in out["warnings"]


def test_load_records_what_the_checks_compare(sdb):
    _add(sdb, (0, 1))
    _pass(sdb, 1, finished=AS_OF, n_contracts=123)
    _pass(sdb, 2, kind="cycle", n_symbols=7)
    fr = frame_mod.current()
    m = fr.meta
    assert m["max_cid"] and m["n_typed"] == 2 and m["pass_contracts"] == 123
    assert m["run_id"] == 2 and m["run_total"] == 7
    assert fr.loaded_mono is not None and frame_mod._loaded_mono == fr.loaded_mono
