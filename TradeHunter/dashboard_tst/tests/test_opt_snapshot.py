"""v4.225: the closing snapshot (``opt_snapshot``) - Massive's own quote-based IV of every contract at
each session's close, kept as Parquet, and the past-day reads that prefer it (the Volatility surface,
the contract history, the Option History tab)."""
from __future__ import annotations

import datetime as _dt
import gzip
import json

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

pytest.importorskip("pyarrow")

from app.screener_models import ScrBase, ScrContract, ScrUnderlying, ScrUnderlyingDaily  # noqa: E402
from app.services import contract_view, flat_files, opt_history, opt_snapshot, option_history_view, vol_surface  # noqa: E402

DAY = "2026-10-07"
ON = _dt.date(2026, 10, 7)
NOV, DEC = _dt.date(2026, 11, 20), _dt.date(2026, 12, 18)
EOD_START = _dt.datetime(2026, 10, 7, 20, 20)        # 16:20 ET
ASOF = _dt.datetime(2026, 10, 7, 20, 25)


@pytest.fixture
def sdb(tmp_path):
    eng = sa.create_engine("sqlite:///" + (tmp_path / "screener.db").as_posix(), future=True)
    ScrBase.metadata.create_all(eng)
    s = sessionmaker(bind=eng, future=True)()
    s.add_all([ScrUnderlying(symbol="AAPL", name="Apple Inc.", sec_type="stock", spot=101.2, spot_src="parity",
                             history_done=True, history_tries=0),
               ScrUnderlying(symbol="SPX", name="S&P 500", sec_type="index", spot=5000.0, spot_src="massive",
                             history_done=False, history_tries=0)])
    for exp in (NOV, DEC):
        for k in (90.0, 100.0, 110.0):
            for rt in "CP":
                s.add(ScrContract(symbol="AAPL", expiry=exp.isoformat(), right=rt, strike=k, weekly=False,
                                  iv=0.25 + (0.05 if rt == "P" else 0.0), session=DAY, as_of=ASOF,
                                  delta=(0.4 if rt == "C" else -0.6), gamma=0.02, theta=-0.04, vega=0.11,
                                  last=k / 50.0, price=k / 50.0 + 0.05, chg_pct=-2.0,
                                  oi=1200, volume=(0 if k == 110.0 else 34),            # the 110s did not trade
                                  last_trade=_dt.datetime(2026, 10, 7, 15, 0),
                                  bar_open=(None if k == 110.0 else k / 50.0 - 0.1),      # v4.225: the day bar
                                  bar_high=(None if k == 110.0 else k / 50.0 + 0.2),
                                  bar_low=(None if k == 110.0 else k / 50.0 - 0.3),
                                  vwap=(None if k == 110.0 else k / 50.0 + 0.01)))
    s.add(ScrContract(symbol="SPX", expiry=NOV.isoformat(), right="P", strike=4000.0, weekly=False, iv=0.3,
                      session=DAY, as_of=ASOF, oi=5, volume=1))
    # a cycle-pass row read before the EOD pass (a symbol the EOD pass failed): not the close
    s.add(ScrContract(symbol="MSFT", expiry=NOV.isoformat(), right="C", strike=400.0, weekly=False, iv=0.2,
                      session=DAY, as_of=_dt.datetime(2026, 10, 7, 19, 30), oi=5, volume=1))
    s.add(ScrUnderlyingDaily(symbol="AAPL", on=DAY, close=101.0, close_raw=101.0))
    s.commit()
    yield s
    s.close()
    eng.dispose()


def test_write_session_keeps_the_eod_pass_rows_once(sdb, tmp_path):
    root = tmp_path / "pq"
    res = opt_snapshot.write_session(sdb, DAY, since=EOD_START - _dt.timedelta(minutes=20), root=root)
    assert res["written"] is True and res["rows"] == 13 and res["symbols"] == 2       # AAPL 12 + SPX 1, not MSFT
    p = opt_snapshot.day_path(DAY, root)
    assert p == root / "snapshot" / "2026" / "2026-10-07.parquet" and p.is_file()
    assert opt_snapshot.days(root) == [DAY]
    tbl = opt_snapshot.chain("aapl", DAY, root=root)
    rows = tbl.to_pylist()
    assert len(rows) == 12 and list(tbl.column_names) == list(opt_snapshot.COLUMNS)
    r = next(x for x in rows if x["strike"] == 90.0 and x["right"] == "P" and x["expiry"] == NOV)
    assert (r["on"], r["iv"], r["delta"], r["oi"], r["volume"], r["spot"], r["spot_src"]) == (ON, 0.3, -0.6, 1200, 34, 101.2, "parity")
    assert r["as_of"] == ASOF and r["last_trade"] == _dt.datetime(2026, 10, 7, 15, 0)
    # the day bar of the same read beside the IV (one source, one moment)
    assert (r["open"], r["high"], r["low"], r["close"], r["vwap"]) == pytest.approx((1.7, 2.0, 1.5, 1.8, 1.81))
    quiet = next(x for x in rows if x["strike"] == 110.0 and x["right"] == "C" and x["expiry"] == NOV)
    assert (quiet["open"], quiet["close"], quiet["volume"], quiet["iv"]) == (None, None, 0, 0.25)   # no trade, still an IV
    # written once: a second call leaves the file as it is
    again = opt_snapshot.write_session(sdb, DAY, root=root)
    assert again["written"] is False and again["why"] == "exists"
    assert opt_snapshot.chain("ZZZZ", DAY, root=root).num_rows == 0
    assert opt_snapshot.chain("AAPL", "2026-10-06", root=root) is None
    sm = opt_snapshot.summary(root)
    assert (sm["days"], sm["oldest"], sm["newest"], sm["newest_rows"]) == (1, DAY, DAY, 13) and sm["bytes"] > 0


def test_write_session_skips_a_superseded_or_empty_session(sdb, tmp_path):
    root = tmp_path / "pq"
    assert opt_snapshot.write_session(sdb, "2026-10-06", root=root)["why"] == "superseded"   # rows of a later session
    assert opt_snapshot.write_session(sdb, "2026-10-08", root=root)["why"] == "empty"
    assert not opt_snapshot.has_day("2026-10-08", root) and opt_snapshot.days(root) == []


def _flat_day(tmp_path, day: _dt.date, closes: dict):
    """One contract-history day for AAPL: {(expiry, right, strike): close}."""
    flat = tmp_path / "flat"
    lines = ["ticker,volume,open,close,high,low,window_start,transactions"]
    for (exp, rt, k), c in closes.items():
        lines.append("O:AAPL%s%s%08d,10,%s,%s,%s,%s,1,2" % (exp.strftime("%y%m%d"), rt, int(k * 1000), c, c, c, c))
    key = flat_files.key_for(day)
    path = flat.joinpath(*key.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        f.write("\n".join(lines) + "\n")
    mp = flat / "manifest.json"
    man = json.loads(mp.read_text(encoding="utf-8")) if mp.exists() else {}
    man[key] = {"day": day.isoformat(), "size": path.stat().st_size, "etag": "x", "downloaded_at": None}
    mp.write_text(json.dumps(man), encoding="utf-8")
    opt_history.Store(root=tmp_path / "pq", flat_root=flat).run()
    return tmp_path / "pq"


def test_a_past_day_with_a_snapshot_uses_massives_closing_iv(sdb, tmp_path):
    root = _flat_day(tmp_path, ON, {(NOV, "C", 110.0): 1.0, (NOV, "P", 90.0): 1.5})
    # no snapshot yet: the day is BLANK (the user's choice) - never the IV solved from the last trade
    with pytest.raises(vol_surface.SurfaceError, match="no closing snapshot is saved for 2026-10-07"):
        vol_surface.surface(sdb, "AAPL", DAY, root=root, band=0, common=False)
    rows, meta = vol_surface.trade_rows(sdb, "AAPL", DAY, band=0, max_dte=0, root=root)   # kept for a by-hand look
    assert meta["iv_src"] == "trade" and len(rows) == 2
    opt_snapshot.write_session(sdb, DAY, since=EOD_START, root=root)
    g = vol_surface.surface(sdb, "AAPL", DAY, root=root, band=0, common=False)
    assert (g["source"], g["iv_src"], g["has_greeks"]) == ("history", "quote", True)
    assert g["method"] == vol_surface.SNAPSHOT_METHOD and g["contracts"] == 12 and g["traded"] == 8
    assert g["spot"] == 101.0 and g["spot_src"] == "close"                # the official close, not the pass's 101.2
    by = dict(zip(g["strikes"], g["cells"]))
    assert by[90.0] == [30.0, 30.0] and by[110.0] == [25.0, 25.0]          # every contract, the 110s that did not trade too
    x = dict(zip(g["strikes"], g["extra"]))[90.0][0]
    assert x["d"] == -0.6 and x["oi"] == 1200 and x["last"] == 1.8 and x["rk"] is not None
    # an index: no stock close, the snapshot still draws it at the pass's price
    spx = vol_surface.surface(sdb, "SPX", DAY, root=root, band=0, common=False)
    assert spx["iv_src"] == "quote" and spx["spot"] == 5000.0 and spx["spot_src"] == "massive"
    # a day without a snapshot stays blank, and says from when they are kept
    _flat_day(tmp_path, _dt.date(2026, 10, 6), {(NOV, "C", 110.0): 1.0})
    with pytest.raises(vol_surface.SurfaceError, match="saved from 2026-10-07 on"):
        vol_surface.surface(sdb, "AAPL", "2026-10-06", root=root, band=0, common=False)
    with pytest.raises(vol_surface.SurfaceError, match="ZZZZ is not in the screener"):
        vol_surface.surface(sdb, "ZZZZ", "2026-10-06", root=root)


def test_the_contract_history_takes_the_closing_iv_on_snapshot_days(sdb, tmp_path):
    root = _flat_day(tmp_path, _dt.date(2026, 10, 6), {(NOV, "C", 110.0): 1.0})
    _flat_day(tmp_path, ON, {(NOV, "C", 110.0): 1.1})
    sdb.add(ScrUnderlyingDaily(symbol="AAPL", on="2026-10-06", close=100.0, close_raw=100.0))
    sdb.commit()
    opt_snapshot.write_session(sdb, DAY, root=root)
    j = contract_view.contract_series(sdb, "AAPL", NOV.isoformat(), "C", 110.0, root=root)
    by = {r["on"]: r for r in j["rows"]}
    assert by["2026-10-06"]["iv"] is None and by["2026-10-06"]["iv_src"] is None   # no snapshot: blank
    assert by["2026-10-06"]["close"] == 1.0 and by["2026-10-06"]["src"] == "file"    # the trade file's prices
    # the snapshot day: EVERY figure from the end-of-day read, not the trade file's 1.1 close
    assert by[DAY]["src"] == "snapshot" and by[DAY]["iv_src"] == "quote" and by[DAY]["iv"] == 25.0
    assert by[DAY]["close"] is None and by[DAY]["volume"] == 0                       # it did not trade at the close read
    j3 = contract_view.contract_series(sdb, "AAPL", NOV.isoformat(), "C", 100.0, root=root)
    r3 = {r["on"]: r for r in j3["rows"]}[DAY]
    assert (r3["open"], r3["high"], r3["low"], r3["close"], r3["volume"], r3["vwap"]) == pytest.approx((1.9, 2.2, 1.7, 2.0, 34, 2.01))
    assert j["n_quote"] == 1
    # a contract that did not trade on the snapshot day still gets that day's row
    j2 = contract_view.contract_series(sdb, "AAPL", DEC.isoformat(), "P", 90.0, root=root)
    assert [r["on"] for r in j2["rows"]] == [DAY]
    r = j2["rows"][0]
    assert (r["iv"], r["iv_src"], r["close"], r["spot"], r["src"]) == (30.0, "quote", 1.8, 101.0, "snapshot")


def test_the_option_history_tab_shows_the_snapshot_and_counts_the_window(sdb, tmp_path):
    root = _flat_day(tmp_path, ON, {(NOV, "C", 110.0): 1.0})
    _flat_day(tmp_path, _dt.date(2026, 10, 1), {(NOV, "C", 110.0): 1.0})      # a day before the window
    st = opt_history.Store(root=root, flat_root=tmp_path / "flat")
    now = _dt.datetime(2026, 10, 8, 14, 0)
    v = option_history_view.view(now, store=st, collector={"state": "idle", "heartbeat": now.isoformat() + "Z",
                                                           "progress": {}}, window=[DAY])
    assert (v["download"]["done"], v["download"]["total"], v["download"]["older"]) == (1, 1, 1)
    assert v["snapshot"]["days"] == 0 and v["snapshot"]["state"] == "waiting"
    opt_snapshot.write_session(sdb, DAY, root=root)
    v = option_history_view.view(now, store=st, collector={"state": "idle", "heartbeat": now.isoformat() + "Z",
                                                           "progress": {"snap_state": "saved", "snap_session": DAY,
                                                                        "snap_at": "2026-10-07T21:00:00Z"}},
                                 window=[DAY])
    sn = v["snapshot"]
    assert (sn["days"], sn["newest"], sn["rows"], sn["state"]) == (1, DAY, 14, "saved") and "MYT" in sn["last"]
