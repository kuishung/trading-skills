"""The volatility chart's series (v4.132; user, 2026-10-08: *"the volatility i need it
to be represented by chart whether I need to see the HV, IV and IV Rank"*).

Three lines, two sources. This is a LIVE view, so prices are fetched live (the
CLAUDE.md rule); the IV comes from the stored daily readings, which is the only place
it exists:

* **IV30** - every stored daily reading for the symbol (``iv_daily``, percent): the
  server's own nightly reads, the screener copy, the IB Gateway seed, the member's
  TWS bootstrap - all of them, oldest first.
* **IV rank** - recomputed at EVERY point from that IV30 series with the ONE formula
  (``option_metrics.iv_rank_pct``: a trailing 252-reading window including the day,
  None under 60 readings), so the line shows how the rank itself moved - and why it
  is blank until the history is deep enough.
* **HV20 / HV60** - realised volatility from live daily closes
  (``prices.fetch_daily_ohlc``, two years) through ``option_metrics.hv`` on a rolling
  window, the same formula the card's "stock's own movement" uses.

``series(db, symbol)`` returns JSON-ready points (``{"t": "YYYY-MM-DD", "v": float}``)
for the last ``days`` sessions of each line plus ``latest`` (the newest value of
each) and ``n_iv`` (readings on file). Soft-fail: a price fetch that fails leaves
the HV lines empty and says so in ``error``; the IV lines still draw.
"""
from __future__ import annotations

import logging

from ..models import IVDaily
from . import option_metrics as om
from .opt_constants import HV_LONG, HV_SHORT, IV_RANK_MIN_OBS, SELL_DIR_MIN_RANK, SELL_NEUTRAL_MIN_RANK
from .option_store import IV_SERIES_N

log = logging.getLogger(__name__)

DAYS = IV_SERIES_N                 # the chart shows the last year of sessions


def iv_points(db, symbol: str) -> list[tuple[str, float]]:
    """``[(on, iv30)]`` oldest first - every stored reading with a figure."""
    sym = str(symbol or "").strip().upper()
    rows = (db.query(IVDaily.on, IVDaily.iv30)
              .filter(IVDaily.symbol == sym, IVDaily.iv30.isnot(None))
              .order_by(IVDaily.on.asc())
              .all())
    out: list[tuple[str, float]] = []
    seen: set[str] = set()
    for on, v in rows:
        on = str(on)[:10]
        if on in seen or v is None:
            continue
        seen.add(on)
        out.append((on, float(v)))
    return out


def rank_points(iv: list[tuple[str, float]]) -> list[tuple[str, float | None]]:
    """The IV rank at every point: the metrics formula on the trailing window that
    INCLUDES the day (the bridge's min / max convention), None under 60 readings."""
    vals = [v for _, v in iv]
    out: list[tuple[str, float | None]] = []
    for i, (on, v) in enumerate(iv):
        window = vals[max(0, i - IV_SERIES_N + 1):i + 1]
        r = om.iv_rank_pct(window, v)
        out.append((on, r.get("iv_rank") if isinstance(r, dict) else None))
    return out


def hv_points(bars) -> list[tuple[str, float | None, float | None]]:
    """``[(date, hv20, hv60)]`` from completed daily bars (a bar with ``session_frac``
    is still open and is skipped, as the metrics do), oldest first."""
    closes: list[float] = []
    out: list[tuple[str, float | None, float | None]] = []
    for b in bars or ():
        if not isinstance(b, dict) or b.get("session_frac") is not None:
            continue
        c = om._num(b.get("close", b.get("c")))
        d = str(b.get("time") or b.get("date") or b.get("t") or "")[:10]
        if c is None or not d:
            continue
        closes.append(c)
        out.append((d, om.hv(closes, HV_SHORT), om.hv(closes, HV_LONG)))
    return out


def _pts(items, days: int) -> list[dict]:
    return [{"t": t, "v": round(float(v), 2)} for t, v in items if v is not None][-days:]


def series(db, symbol: str, *, bars=None, days: int = DAYS) -> dict:
    """Everything the fragment draws. ``bars`` can be passed (tests); else the daily
    closes are fetched live."""
    sym = str(symbol or "").strip().upper()
    out: dict = {"symbol": sym, "iv": [], "rank": [], "hv20": [], "hv60": [], "latest": {},
                 "n_iv": 0, "min_obs": IV_RANK_MIN_OBS, "window": IV_SERIES_N,
                 "sell_from": SELL_DIR_MIN_RANK, "neutral_from": SELL_NEUTRAL_MIN_RANK, "error": None}
    if not sym:
        return out
    iv = iv_points(db, sym)
    rank = rank_points(iv)
    if bars is None:
        try:
            from . import prices           # noqa: PLC0415 - lazy: the live fetcher pulls httpx
            bars = prices.fetch_daily_ohlc(sym, rng="2y")
        except Exception as exc:  # noqa: BLE001 - the IV lines still draw
            log.warning("option_vol %s: daily closes unavailable: %s", sym, exc)
            out["error"] = f"realised volatility unavailable: {type(exc).__name__}"
            bars = []
    hv = hv_points(bars)
    out["iv"] = _pts(iv, days)
    out["rank"] = _pts(rank, days)
    out["hv20"] = _pts([(d, a) for d, a, _ in hv], days)
    out["hv60"] = _pts([(d, b) for d, _, b in hv], days)
    out["n_iv"] = len(iv)
    last_hv20 = next((a for _, a, _ in reversed(hv) if a is not None), None)
    last_hv60 = next((b for _, _, b in reversed(hv) if b is not None), None)
    out["latest"] = {
        "on": iv[-1][0] if iv else None,
        "iv30": round(iv[-1][1], 1) if iv else None,
        "iv_rank": (round(rank[-1][1], 0) if (rank and rank[-1][1] is not None) else None),
        "hv20": round(last_hv20, 1) if last_hv20 is not None else None,
        "hv60": round(last_hv60, 1) if last_hv60 is not None else None,
        "hv_on": hv[-1][0] if hv else None,
    }
    return out
