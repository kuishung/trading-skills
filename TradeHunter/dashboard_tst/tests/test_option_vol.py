"""services/option_vol - the volatility chart's series (v4.132): IV30 from the stored
readings, the IV rank recomputed at every point with the metrics formula, HV20 / HV60
from daily closes. No network: bars are passed in."""
from __future__ import annotations

import datetime as _dt

from app import models
from app.services import option_metrics as om
from app.services import option_vol as ov
from app.services.opt_constants import IV_RANK_MIN_OBS

from .fixtures.options import bars_synth


def _iv(db, sym: str, n: int, start=_dt.date(2025, 10, 1)):
    for i in range(n):
        db.add(models.IVDaily(symbol=sym, on=(start + _dt.timedelta(days=i)).isoformat(), kind="history",
                              source="iv_history", iv30=30.0 + (i * 7) % 23))
    db.commit()


def test_iv_rank_and_hv_series(db):
    _iv(db, "AAA", 300)
    bars = bars_synth("uptrend_bounce", n=400, start=100.0, end="2026-10-03")
    out = ov.series(db, "aaa", bars=bars)
    assert out["symbol"] == "AAA" and out["n_iv"] == 300 and out["error"] is None
    assert len(out["iv"]) == ov.DAYS and out["iv"][0]["t"] < out["iv"][-1]["t"]          # the last year, ascending
    assert all(set(p) == {"t", "v"} for p in out["iv"])
    # the rank: None (left out) under 60 readings, the metrics formula after - checked at one point
    ranks = ov.rank_points(ov.iv_points(db, "AAA"))
    assert all(r is None for _, r in ranks[:IV_RANK_MIN_OBS - 1]) and ranks[IV_RANK_MIN_OBS - 1][1] is not None
    vals = [v for _, v in ov.iv_points(db, "AAA")]
    i = 200
    expect = om.iv_rank_pct(vals[max(0, i - 251):i + 1], vals[i])["iv_rank"]
    assert ranks[i][1] == expect
    assert len(out["rank"]) == min(ov.DAYS, 300 - IV_RANK_MIN_OBS + 1)
    # HV: none for the first 20 completed bars, then the metrics' hv on the rolling closes
    hv = ov.hv_points(bars)
    assert hv[19][1] is None and hv[20][1] is not None and hv[59][2] is None and hv[60][2] is not None
    closes = [b["close"] for b in bars[:61]]
    assert hv[60][1] == om.hv(closes, 20) and hv[60][2] == om.hv(closes, 60)
    assert len(out["hv20"]) == ov.DAYS and len(out["hv60"]) == ov.DAYS
    L = out["latest"]
    assert L["iv30"] == round(vals[-1], 1) and L["on"] == ov.iv_points(db, "AAA")[-1][0]
    assert L["iv_rank"] == round(ranks[-1][1], 0) and L["hv20"] is not None and L["hv60"] is not None
    assert out["sell_from"] == 30 and out["neutral_from"] == 50 and out["min_obs"] == 60


def test_short_history_and_no_bars(db):
    _iv(db, "BBB", 5)
    out = ov.series(db, "BBB", bars=[])
    assert out["n_iv"] == 5 and len(out["iv"]) == 5 and out["rank"] == [] and out["hv20"] == []
    assert out["latest"]["iv_rank"] is None and out["latest"]["hv20"] is None
    assert ov.series(db, "", bars=[])["iv"] == []
    # an open session's bar is skipped, as the metrics skip it
    bars = bars_synth("uptrend_bounce", n=30, start=100.0, end="2026-10-03", open_last=True)
    hv = ov.hv_points(bars)
    assert len(hv) == 29 and hv[-1][0] < "2026-10-03"


def test_chart_rank_equals_the_nightly_and_stored_rank(db):
    """The ONE window: 252 readings including today, whichever caller. With 252 PRIOR
    readings stored and today's not yet written, the nightly's _window used to rank on
    253 - one more than the stored recompute and this chart. All three agree now."""
    from app.services import option_store

    _iv(db, "DDD", 252)                                     # 252 prior readings, the oldest the extreme
    prior = ov.iv_points(db, "DDD")
    today = "2026-10-09"
    iv30 = 41.0
    window = om._window(prior, iv30, today)
    assert len(window) == 252 and window[-1] == iv30 and window[0] == prior[1][1]      # the oldest fell out
    nightly_rank = om.iv_rank_pct(window, iv30)["iv_rank"]
    chart_rank = ov.rank_points(prior + [(today, iv30)])[-1][1]
    assert chart_rank == nightly_rank
    db.add(models.IVDaily(symbol="DDD", on=today, kind="eod", source="cboe", iv30=iv30))
    db.commit()
    stored = option_store._recompute_rank(db, "DDD", today)["iv_rank"]
    assert stored == nightly_rank == chart_rank
    assert om._window([v for _, v in prior], iv30, today) == window                     # the bare-float branch too


def test_live_fetch_failure_is_soft(db, monkeypatch):
    from app.services import prices

    _iv(db, "CCC", 3)
    monkeypatch.setattr(prices, "fetch_daily_ohlc", lambda sym, *, rng="2y": (_ for _ in ()).throw(RuntimeError("Yahoo down")))
    out = ov.series(db, "CCC")
    assert len(out["iv"]) == 3 and out["hv20"] == [] and "unavailable" in out["error"]
