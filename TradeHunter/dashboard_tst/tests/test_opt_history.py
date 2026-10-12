"""The contract history (v4.139): ``app/services/opt_history.py`` - Massive's options day files
converted to Parquet (one file per session), the manifest, what is pending, the reads (one
chain, one contract's life) and the Settings tab's summary + view model
(``option_history_view``). Synthetic day files in a temp flat-file store; no network."""
from __future__ import annotations

import datetime as _dt
import gzip
import json
from pathlib import Path

import pytest

from app.services import flat_files, opt_history, option_history_view

pa = pytest.importorskip("pyarrow")

D1, D2, D3 = "2026-10-05", "2026-10-06", "2026-10-07"     # Mon .. Wed
EXP = _dt.date(2026, 11, 20)
HEADER = "ticker,volume,open,close,high,low,window_start,transactions"


def tick(root, exp, right, k):
    return "O:%s%s%s%08d" % (root, exp.strftime("%y%m%d"), right, int(round(k * 1000)))


def day_rows(day: str, bump: float = 0.0) -> list[str]:
    """One day's CSV lines: AAPL calls / puts, an adjusted root, an index root, MSFT, and rows
    that must be dropped (not a ticker, a bad right, a bad date, a zero strike)."""
    base = 1.0 + bump
    lines = []
    for k in (140.0, 150.0, 160.0):
        for rt in "CP":
            c = round(base + k / 100.0 + (0.5 if rt == "C" else 0.0), 2)
            lines.append("%s,%d,%s,%s,%s,%s,1,%d" % (tick("AAPL", EXP, rt, k), 10 + int(k), c, c, c + 0.1, c - 0.1, 3))
    lines.append("%s,5,1.1,1.2,1.3,1.0,1,2" % tick("MSFT", EXP, "C", 400.0))
    lines.append("%s,7,2.0,2.0,2.0,2.0,1,1" % tick("GME1", EXP, "C", 20.0))      # adjusted series
    lines.append("%s,9,30,31,32,29,1,4" % tick("SPXW", EXP, "P", 5000.0))        # index
    near = _dt.date.fromisoformat(D2)                                             # expires on D2
    lines.append("%s,3,0.5,0.5,0.5,0.5,1,1" % tick("AAPL", near, "C", 150.0))
    lines += ["NOT_A_TICKER,1,1,1,1,1,1,1",
              "O:AAPL261120X00150000,1,1,1,1,1,1,1",
              "O:AAPL269920C00150000,1,1,1,1,1,1,1",
              "O:AAPL261120C00000000,1,1,1,1,1,1,1"]
    return lines


def write_gz(path: Path, lines: list[str], header: str = HEADER) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        f.write(header + "\n" + "\n".join(lines) + "\n")
    return path.stat().st_size


def make_flat(root: Path, days: dict, etag: str = "e1") -> Path:
    """A flat-file store under ``root``: ``{day: lines}`` written + a manifest."""
    man = {}
    for day, lines in days.items():
        key = flat_files.key_for(day)
        size = write_gz(root.joinpath(*key.split("/")), lines)
        man[key] = {"day": day, "size": size, "etag": "%s-%s" % (etag, day), "downloaded_at": "2026-10-08T15:20:00Z"}
    (root / "manifest.json").write_text(json.dumps(man), encoding="utf-8")
    return root


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def flat(tmp_path):
    return make_flat(tmp_path / "flat", {D1: day_rows(D1), D2: day_rows(D2, 0.1), D3: day_rows(D3, 0.2)})


@pytest.fixture
def store(tmp_path, flat):
    clk = Clock(_dt.datetime(2026, 10, 8, 16, 0))
    st = opt_history.Store(root=tmp_path / "pq", flat_root=flat, now=clk, sleep=lambda s: None)
    st.clock = clk
    return st


# ───────────────────────────────────────── one day ─────────────────────────────────────────

def test_a_day_file_becomes_a_sorted_table_without_the_bad_rows(flat):
    src = flat.joinpath(*flat_files.key_for(D2).split("/"))
    tbl, dropped = opt_history.to_table(src, D2)
    assert dropped == 4                                    # not a ticker, a bad right, a bad date, strike 0
    assert tbl.schema.names == list(opt_history.COLUMNS)
    rows = tbl.to_pylist()
    assert [r["root"] for r in rows] == sorted(r["root"] for r in rows)
    assert {r["root"] for r in rows} == {"AAPL", "MSFT", "GME1", "SPXW"}
    aapl = [r for r in rows if r["root"] == "AAPL" and r["expiry"] == EXP]
    assert [(r["right"], r["strike"]) for r in aapl] == [("C", 140.0), ("C", 150.0), ("C", 160.0),
                                                          ("P", 140.0), ("P", 150.0), ("P", 160.0)]
    r = aapl[1]
    assert r["on"] == _dt.date(2026, 10, 6) and r["close"] == pytest.approx(3.1) and r["volume"] == 160
    assert isinstance(r["volume"], int) and r["transactions"] == 3
    assert r["high"] == pytest.approx(3.2) and r["low"] == pytest.approx(3.0)
    assert opt_history.occ_ticker("AAPL", EXP, "C", 150) == tick("AAPL", EXP, "C", 150.0)


def test_columns_are_found_by_name_and_a_wrong_file_is_a_source_error(tmp_path):
    p = tmp_path / "x.csv.gz"
    write_gz(p, ["2,%s,1.5" % tick("AAPL", EXP, "C", 150.0)], header="volume,ticker,close")   # other order, fewer
    tbl, dropped = opt_history.to_table(p, D1)
    row = tbl.to_pylist()[0]
    assert dropped == 0 and row["close"] == 1.5 and row["volume"] == 2 and row["open"] is None
    bad = tmp_path / "bad.csv.gz"
    write_gz(bad, ["a,b"], header="foo,bar")
    with pytest.raises(opt_history.HistoryError) as ei:
        opt_history.to_table(bad, D1)
    assert ei.value.kind == "source"
    junk = tmp_path / "junk.csv.gz"
    junk.write_bytes(b"\x1f\x8bnot really gzip at all")
    with pytest.raises(opt_history.HistoryError) as ei:
        opt_history.to_table(junk, D1)
    assert ei.value.kind == "source"


# ───────────────────────────────────────── the store ─────────────────────────────────────────

def test_the_store_converts_newest_first_and_only_what_changed(store, flat, monkeypatch):
    assert store.pending() == [D3, D2, D1]
    assert store.counts() == (0, 3)
    seen = []
    res = store.run(on_day=lambda n, total, day, ent, err: seen.append((n, total, day, err)))
    assert res["converted"] == 3 and res["failed"] == [] and res["rows"] == 3 * 10
    assert seen == [(1, 3, D3, None), (2, 3, D2, None), (3, 3, D1, None)]
    assert store.pending() == [] and store.counts() == (3, 3)
    assert opt_history.day_path(D2, store.root).is_file()
    ent = json.loads((store.root / "manifest.json").read_text(encoding="utf-8"))[D2]
    assert ent["rows"] == 10 and ent["dropped"] == 4 and ent["fmt"] == opt_history.FORMAT
    assert ent["src_etag"] == "e1-" + D2 and ent["converted_at"].endswith("Z")
    assert store.days() == [D1, D2, D3]
    # Massive re-published D2 (a new etag): only D2 is converted again
    man = json.loads((flat / "manifest.json").read_text(encoding="utf-8"))
    man[flat_files.key_for(D2)]["etag"] = "e2-" + D2
    (flat / "manifest.json").write_text(json.dumps(man), encoding="utf-8")
    assert store.pending() == [D2]
    assert store.run()["converted"] == 1 and store.pending() == []
    # a new Parquet layout converts every day again
    monkeypatch.setattr(opt_history, "FORMAT", opt_history.FORMAT + 1)
    assert store.pending() == [D3, D2, D1] and store.days() == []
    # a Parquet file deleted by hand is written again
    monkeypatch.setattr(opt_history, "FORMAT", opt_history.FORMAT - 1)
    opt_history.day_path(D1, store.root).unlink()
    assert store.pending() == [D1]


def test_a_day_file_not_on_this_pc_is_never_pending(store, flat):
    p = flat.joinpath(*flat_files.key_for(D1).split("/"))
    p.unlink()                                             # in the manifest, gone from disk
    q = flat.joinpath(*flat_files.key_for(D2).split("/"))
    q.write_bytes(q.read_bytes()[:-5])                     # cut short (a download in flight)
    assert store.pending() == [D3] and store.counts() == (0, 1)


def test_a_damaged_day_fails_alone_and_is_tried_again_later(store, flat):
    key = flat_files.key_for(D2)
    p = flat.joinpath(*key.split("/"))
    junk = b"\x1f\x8b" + b"\x00" * (p.stat().st_size - 2)  # same size as the manifest says, not gzip
    p.write_bytes(junk)
    res = store.run()
    assert res["converted"] == 2 and [d for d, _e in res["failed"]] == [D2]
    assert "cannot be read" in res["failed"][0][1]
    assert store.pending() == []                           # not every minute ...
    store.clock.t += _dt.timedelta(seconds=opt_history.FAIL_RETRY_S + 1)
    assert store.pending() == [D2]                         # ... but an hour later
    store.clock.t -= _dt.timedelta(seconds=opt_history.FAIL_RETRY_S + 1)
    size = write_gz(p, day_rows(D2))                       # a fresh download: tried at once
    man = json.loads((flat / "manifest.json").read_text(encoding="utf-8"))
    man[key]["size"] = size
    (flat / "manifest.json").write_text(json.dumps(man), encoding="utf-8")
    assert store.pending() == [D2]
    assert store.run()["converted"] == 1 and store.failed == {}


def test_a_parquet_file_in_use_fails_only_that_day(store, monkeypatch):
    """Windows: a page reading the old file while Massive's re-published copy is converted."""
    store.run([D1])
    real = opt_history._replace

    def busy(src, dst, sleep=None):
        if Path(dst).name.startswith(D1):
            raise PermissionError(32, "The process cannot access the file because it is being used")
        return real(src, dst, sleep or (lambda s: None))

    monkeypatch.setattr(opt_history, "_replace", busy)
    monkeypatch.setattr(opt_history, "FORMAT", opt_history.FORMAT + 1)       # every day due again
    res = store.run()
    assert res["converted"] == 2 and [d for d, _e in res["failed"]] == [D1]
    assert "in use" in res["failed"][0][1]
    assert not list(opt_history.day_path(D1, store.root).parent.glob("*.tmp"))   # no temp file left


def test_a_disk_problem_is_not_held_against_the_day(store, monkeypatch):
    """A config error (the disk, the folder) stops the run but marks no day as failed: once it
    is fixed, the newest day goes first again - not an hour later."""
    real = opt_history.convert_day

    def full(src, day, dst, **kw):
        raise opt_history.HistoryError("config", "could not write %s.parquet (disk full)" % day)

    monkeypatch.setattr(opt_history, "convert_day", full)
    with pytest.raises(opt_history.HistoryError):
        store.run()
    assert store.failed == {} and store.failed_days() == [] and store.pending()[0] == D3
    monkeypatch.setattr(opt_history, "convert_day", real)
    assert store.run()["converted"] == 3


def test_an_unexpected_reader_error_fails_only_that_day(store, monkeypatch):
    real = opt_history.to_table

    def boom(src, day):
        if str(day) == D2:
            raise ValueError("ArrowInvalid: something odd in the file")
        return real(src, day)

    monkeypatch.setattr(opt_history, "to_table", boom)
    res = store.run()
    assert res["converted"] == 2 and [d for d, _e in res["failed"]] == [D2]
    assert [d for d, _f in store.failed_days()] == [D2] and store.failed[D2]["kind"] == "source"


def test_a_contract_read_is_kept_until_the_next_day_arrives(store):
    import os

    store.run([D2, D1])
    first = opt_history.contract_history("AAPL", EXP, "C", 150, root=store.root)
    assert [r["on"].isoformat() for r in first] == [D1, D2]
    opt_history.day_path(D1, store.root).unlink()             # the cached answer does not read the files ...
    again = opt_history.contract_history("AAPL", EXP, "C", 150, root=store.root)
    assert [r["on"].isoformat() for r in again] == [D1, D2]
    again[0]["close"] = -1.0                                   # ... and a caller cannot change it
    assert opt_history.contract_history("AAPL", EXP, "C", 150, root=store.root)[0]["close"] == pytest.approx(3.0)
    store.run([D3])                                            # a new day: the manifest changed
    man = store.root / "manifest.json"
    st = man.stat()
    os.utime(man, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    fresh = opt_history.contract_history("AAPL", EXP, "C", 150, root=store.root)
    assert [r["on"].isoformat() for r in fresh] == [D2, D3]   # D1's file is gone, D3 is new


def test_without_pyarrow_the_run_stops_with_a_config_error(store, monkeypatch):
    monkeypatch.setattr(opt_history, "available", lambda: "pyarrow is not installed on this PC (ImportError)")
    with pytest.raises(opt_history.HistoryError) as ei:
        store.run()
    assert ei.value.kind == "config" and "pyarrow" in str(ei.value)


def test_the_flat_manifest_is_only_read(tmp_path):
    root = tmp_path / "flat"
    root.mkdir()
    (root / "manifest.json").write_text("{not json", encoding="utf-8")
    assert opt_history.flat_manifest(root) == {}
    assert (root / "manifest.json").read_text(encoding="utf-8") == "{not json"   # never set aside
    assert opt_history.flat_manifest(tmp_path / "nowhere") == {}


def test_where_the_parquet_files_live(monkeypatch, tmp_path):
    monkeypatch.delenv(opt_history.ENV_DIR, raising=False)
    monkeypatch.delenv(opt_history.ENV_PRICE_DIR, raising=False)
    assert opt_history.history_root() == opt_history.DEFAULT_ROOT
    monkeypatch.setenv(opt_history.ENV_PRICE_DIR, str(tmp_path / "MarketData" / "price_history"))
    assert opt_history.history_root() == tmp_path / "MarketData" / "option_history"
    monkeypatch.setenv(opt_history.ENV_DIR, str(tmp_path / "elsewhere"))
    assert opt_history.history_root() == tmp_path / "elsewhere"
    assert opt_history.day_path("2026-10-07", tmp_path) == tmp_path / "day" / "2026" / "2026-10-07.parquet"


# ───────────────────────────────────────── the reads ─────────────────────────────────────────

def test_one_chain_and_one_contracts_life(store):
    store.run()
    tbl = opt_history.chain("aapl", D2, root=store.root)
    assert tbl.num_rows == 7 and set(tbl.column("root").to_pylist()) == {"AAPL"}
    assert opt_history.chain("AAPL", "2026-10-02", root=store.root) is None       # no file that day
    life = opt_history.contract_history("AAPL", EXP, "c", 150, root=store.root)
    assert [r["on"].isoformat() for r in life] == [D1, D2, D3]
    assert [r["close"] for r in life] == pytest.approx([3.0, 3.1, 3.2])
    assert [r["on"].isoformat() for r in opt_history.contract_history("AAPL", EXP, "C", 150, root=store.root,
                                                                      start=D2)] == [D2, D3]
    # a contract that expired on D2 has no row after it
    near = opt_history.contract_history("AAPL", D2, "C", 150, root=store.root)
    assert [r["on"].isoformat() for r in near] == [D1, D2]
    assert opt_history.contract_history("AAPL", EXP, "C", 155, root=store.root) == []
    assert opt_history.converted_days(store.root) == [D1, D2, D3]


# ───────────────────────────────────────── the Settings tab ─────────────────────────────────────────

def test_the_summary_counts_months_and_recent_days(store):
    store.run([D3, D2])
    window = ["2026-09-30", "2026-10-01", "2026-10-02", D1, D2, D3]
    s = store.summary(window=window, recent=4)
    assert s["window"] == {"sessions": 6, "oldest": "2026-09-30", "newest": D3}
    assert s["flat"]["days"] == 3 and s["flat"]["newest"] == D3 and s["flat"]["last_download"] == "2026-10-08T15:20:00Z"
    assert s["pq"]["days"] == 2 and s["pq"]["rows"] == 20 and s["pq"]["oldest"] == D2
    assert s["missing"] == ["2026-09-30", "2026-10-01", "2026-10-02"] and s["behind"] == [D1]
    assert [(m["month"], m["sessions"], m["downloaded"], m["converted"]) for m in s["months"]] == [
        ("2026-10", 5, 3, 2), ("2026-09", 1, 0, 0)]
    assert [(r["day"], r["state"]) for r in s["recent"]] == [(D3, "converted"), (D2, "converted"),
                                                             (D1, "pending"), ("2026-10-02", "missing")]
    assert s["available"] is None and s["root"] == str(store.root)


def test_the_tab_view_reads_the_collector_progress(store):
    store.run([D3])
    now = _dt.datetime(2026, 10, 8, 16, 0)
    col = {"state": "history", "detail": "Contract history: converting ...", "heartbeat": now - _dt.timedelta(seconds=20),
           "progress": {"flat_state": "ready", "flat_days_done": 3, "flat_days_total": 3, "pq_state": "building",
                        "pq_days_done": 1, "pq_days_total": 3, "pq_eta_s": 120, "pq_failed": [D1]}}
    v = option_history_view.view(now, store=store, collector=col, window=[D1, D2, D3])
    assert v["collector"]["stale"] is False and v["collector"]["age"] == "20 s ago"
    assert (v["download"]["done"], v["download"]["total"], v["download"]["pct"]) == (3, 3, 100)
    assert (v["parquet"]["done"], v["parquet"]["total"], v["parquet"]["behind"]) == (1, 3, 2)
    assert v["parquet"]["state"] == "building" and v["parquet"]["eta"] == "2 min"
    assert v["parquet"]["failed"] == [{"day": D1}]
    assert {r["day"]: r["state"] for r in v["recent"]} == {D3: "converted", D2: "pending", D1: "failed"}
    assert v["iv"]["pct"] == 100
    nxt = v["download"]["next"]                            # D3's file is in: next is Thursday's, due Friday 11:15 ET
    assert nxt["session"] == "2026-10-08" and nxt["due_et"] == "Fri 2026-10-09 11:15 ET" and not nxt["late"]
    assert nxt["due_myt"] == "Fri 23:15 MYT"
    stale = option_history_view.view(now, store=store, collector=dict(col, heartbeat=now - _dt.timedelta(hours=1)),
                                     window=[D1, D2, D3])
    assert stale["collector"]["stale"] is True
    assert option_history_view.size_text(1_850_000_000) == "1.85 GB" and option_history_view.size_text(3_900_000) == "3.9 MB"
