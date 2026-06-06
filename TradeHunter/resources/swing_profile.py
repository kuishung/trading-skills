"""swing_profile.py — per-ticker SWING/TREND behavioral profile (Layer 1 product).

The trend/swing analog of `ticker_profile.py` (which is INTRADAY-focused). Two
separate profile products by deliberate design (set 2026-06-06):

  - `ticker_profile.py`  -> INTRADAY  (3/5min + daily, recency window, NIGHTLY
                            regen) -> data/ticker_profile/<T>.json -> GUNS/DITP
  - `swing_profile.py`   -> SWING     (daily 2yr + weekly resample, long lookback,
                            WEEKLY regen + earnings-driven) -> data/swing_profile/
                            <T>.json -> MATP / swing setups / dashboard_tst

Why separate: different timeframe, lookback, cadence, and consumer. A swing
profile cares about trend direction/quality/structure over weeks-months, not
about RVOL / gaps / time-of-day.

Computed from `bars_store` daily bars (no network). Fields are all parquet-
derivable EXCEPT analyst-target/MBP and earnings-date, which come from the MATP
pipeline / yfinance and are merged in separately when available (left null here).

Trend state reuses the MATP `classify_trend` rule (Uptrend/Downtrend/Sideways/
Unknown) so the swing system speaks the same trend language as MATP.

CLI:
    py -3.12 resources/swing_profile.py NVDA              # one ticker
    py -3.12 resources/swing_profile.py --all             # full daily universe
    py -3.12 resources/swing_profile.py --all --benchmark SPY   # if SPY seeded
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
for _p in [str(_root), str(_root / "scripts"), str(_root / "resources")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import bars_store                       # noqa: E402
from _common import get_data_root       # noqa: E402

PROFILE_DIRNAME = "swing_profile"
MIN_BARS_FOR_EMA200 = 210
TRADING_DAYS_YEAR = 252


# ---------- storage ----------

def profile_dir() -> Path:
    d = get_data_root() / PROFILE_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d

def profile_path(ticker: str) -> Path:
    return profile_dir() / f"{ticker.upper()}.json"

def get_swing_profile(ticker: str) -> dict | None:
    p = profile_path(ticker)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def save_swing_profile(profile: dict) -> None:
    p = profile_path(profile["ticker"])
    p.write_text(json.dumps(profile, indent=2), encoding="utf-8")


# ---------- math helpers (self-contained; trend rule matches MATP) ----------

def _ema(values: list[float], period: int) -> list[float]:
    """EMA seeded with values[0] — identical to MATP classify_trend.ema so the
    trend states agree across the two systems."""
    if not values:
        return []
    a = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(a * v + (1 - a) * out[-1])
    return out

def _classify(closes: list[float], periods=(20, 50, 200),
              min_bars: int = MIN_BARS_FOR_EMA200, slope_window: int = 20) -> str:
    """Trend rule: Uptrend / Downtrend / Sideways / Unknown.

    Daily uses (20,50,200)/210 bars — identical to MATP `classify_trend` so the
    states agree. Weekly uses a lighter (10,20,40)/~50 set because 2yr of daily
    is only ~104 weeks (not enough for a weekly EMA200, which needs ~4 years)."""
    pf, pm, ps = periods
    if len(closes) < min_bars:
        return "Unknown"
    ef, em, es = _ema(closes, pf), _ema(closes, pm), _ema(closes, ps)
    c = closes[-1]
    af, am, asl = ef[-1], em[-1], es[-1]
    si = -1 - slope_window
    if abs(si) > len(em):
        return "Unknown"
    slope_up = em[-1] > em[si]
    slope_dn = em[-1] < em[si]
    if c > af > am > asl and slope_up:
        return "Uptrend"
    if c < af < am < asl and slope_dn:
        return "Downtrend"
    return "Sideways"

def _atr(bars: list[dict], period: int = 14) -> float | None:
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    recent = trs[-period:]
    return sum(recent) / len(recent)

def _weekly(bars: list[dict]) -> list[dict]:
    """Resample daily bars -> weekly (ISO year-week)."""
    out: dict[tuple, dict] = {}
    for b in bars:
        d = datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
        key = d.isocalendar()[:2]   # (iso_year, iso_week)
        w = out.get(key)
        if w is None:
            out[key] = {"t": b["t"], "o": b["o"], "h": b["h"], "l": b["l"],
                        "c": b["c"], "v": b["v"]}
        else:
            w["h"] = max(w["h"], b["h"]); w["l"] = min(w["l"], b["l"])
            w["c"] = b["c"]; w["v"] += b["v"]
    return [out[k] for k in sorted(out)]

def _pct(a: float, b: float) -> float | None:
    return None if not b else round((a - b) / b * 100, 2)


# ---------- the profile ----------

def compute_swing_profile(ticker: str, daily: list[dict]) -> dict | None:
    """Build the swing/trend profile from daily bars. Returns None if too few."""
    if not daily or len(daily) < 60:
        return None
    closes = [b["c"] for b in daily]
    vols   = [b["v"] for b in daily]
    price  = closes[-1]
    e20, e50, e200 = _ema(closes, 20), _ema(closes, 50), _ema(closes, 200)
    a20, a50 = e20[-1], e50[-1]
    a200 = e200[-1] if len(closes) >= 200 else None

    # 52-week range position
    yr = daily[-TRADING_DAYS_YEAR:] if len(daily) >= TRADING_DAYS_YEAR else daily
    hi52 = max(b["h"] for b in yr); lo52 = min(b["l"] for b in yr)
    pos_52w = round((price - lo52) / (hi52 - lo52), 3) if hi52 > lo52 else None

    # volatility contraction (base quality): recent 20d ATR vs 60d ATR (<1 = tightening)
    atr14 = _atr(daily, 14)
    atr20 = _atr(daily[-21:], 20) if len(daily) >= 21 else None
    atr60 = _atr(daily[-61:], 60) if len(daily) >= 61 else None
    vol_contraction = round(atr20 / atr60, 2) if (atr20 and atr60) else None

    # accumulation/distribution: up-day vol vs down-day vol, last 50d
    win = daily[-50:] if len(daily) >= 50 else daily
    up_v = sum(b["v"] for i, b in enumerate(win) if i and b["c"] >= win[i - 1]["c"])
    dn_v = sum(b["v"] for i, b in enumerate(win) if i and b["c"] < win[i - 1]["c"])
    accum_dist = round(up_v / dn_v, 2) if dn_v else None

    def ret(n):
        return _pct(price, closes[-1 - n]) if len(closes) > n else None

    return {
        "ticker": ticker.upper(),
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "n_daily_bars": len(daily),
        "price": round(price, 4),
        # --- trend ---
        "trend_state": _classify(closes),   # daily (20,50,200)/210
        "weekly_trend_state": _classify(
            [w["c"] for w in _weekly(daily)],
            periods=(10, 20, 40), min_bars=50, slope_window=8),
        # --- moving-average structure ---
        "ma": {
            "ema20": round(a20, 4), "ema50": round(a50, 4),
            "ema200": round(a200, 4) if a200 else None,
            "above_20": price > a20, "above_50": price > a50,
            "above_200": (price > a200) if a200 else None,
            "stacked_bull": (a200 is not None and a20 > a50 > a200),
            "stacked_bear": (a200 is not None and a20 < a50 < a200),
            "ema50_slope_up": (len(e50) > 21 and e50[-1] > e50[-21]),
        },
        # --- range / position ---
        "high_52w": round(hi52, 4), "low_52w": round(lo52, 4),
        "pos_52w": pos_52w,
        "dist_from_52w_high_pct": _pct(price, hi52),
        "dist_from_52w_low_pct": _pct(price, lo52),
        # --- volatility / stops (swing scale) ---
        "atr_daily": round(atr14, 4) if atr14 else None,
        "atr_pct": _pct(price + (atr14 or 0), price) if atr14 else None,
        "vol_contraction": vol_contraction,   # <1 => tightening base
        # --- supply/demand + entry ---
        "accum_dist": accum_dist,             # >1 => accumulation
        "pullback_to_ema20_pct": _pct(price, a20),   # negative => below ema20
        "pullback_to_ema50_pct": _pct(price, a50),
        # --- momentum ---
        "ret_1m": ret(21), "ret_3m": ret(63), "ret_6m": ret(126),
        # --- merged externally (MATP / yfinance) ---
        "analyst_target": None, "mbp": None, "next_earnings": None,
        # --- cross-sectional (filled in --all runs) ---
        "rs_percentile": None,
    }


def refresh_swing_profile(ticker: str) -> dict | None:
    daily = bars_store.load_bars(ticker.upper(), timeframe="daily")
    prof = compute_swing_profile(ticker, daily or [])
    if prof:
        save_swing_profile(prof)
    return prof


def refresh_all_swing(symbols: list[str], log=print) -> dict:
    """Refresh every symbol, then a cross-sectional pass to add rs_percentile
    (rank of 3-month return across the universe — relative strength without an
    index benchmark)."""
    done, skipped = [], []
    rets: dict[str, float] = {}
    for i, t in enumerate(symbols, 1):
        try:
            p = refresh_swing_profile(t)
            if p:
                done.append(t)
                if p.get("ret_3m") is not None:
                    rets[t] = p["ret_3m"]
            else:
                skipped.append(t)
        except Exception as exc:
            skipped.append(t)
            log(f"  {t}: {type(exc).__name__}: {exc}")
        if i % 200 == 0:
            log(f"  swing profiles {i}/{len(symbols)} ...")
    # cross-sectional relative strength (percentile of 3m return)
    if rets:
        order = sorted(rets, key=lambda k: rets[k])
        n = len(order)
        for rank, t in enumerate(order):
            pct = round(100 * rank / max(1, n - 1), 1)
            prof = get_swing_profile(t)
            if prof:
                prof["rs_percentile"] = pct
                save_swing_profile(prof)
    log(f"swing profiles DONE: {len(done)} written, {len(skipped)} skipped")
    return {"written": len(done), "skipped": len(skipped)}


# ---------- CLI ----------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tickers", nargs="*", help="symbols to profile (omit with --all)")
    ap.add_argument("--all", action="store_true", help="profile the full daily universe")
    args = ap.parse_args()

    if args.all:
        syms = bars_store.list_symbols("daily")
        print(f"# swing-profiling {len(syms)} symbols (daily universe)")
        refresh_all_swing(syms)
        return 0
    if not args.tickers:
        ap.error("give one or more tickers, or --all")
    for t in args.tickers:
        p = refresh_swing_profile(t)
        if p:
            print(json.dumps(p, indent=2))
        else:
            print(f"{t}: insufficient daily bars")
    return 0


if __name__ == "__main__":
    sys.exit(main())
