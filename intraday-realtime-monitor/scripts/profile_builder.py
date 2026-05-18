#!/usr/bin/env python
"""Build/refresh per-ticker behavioral profiles.

Each profile captures the baseline metrics the strategy brain uses to
normalize its thresholds (ATR-multiples instead of dollar amounts, vol
z-scores instead of absolute share counts, etc.). One profile JSON per
ticker, written to profiles/<TICKER>.json.

Fields:
  ticker, as_of,
  atr_14d              dollar ATR (14 daily bars)
  atr_pct              ATR as percent of prev close
  avg_minute_vol_rth   mean 1-min volume during regular hours (20-day)
  minute_vol_stddev    stddev for z-score normalization
  premkt_range_avg     mean premkt high-low (20-day; uses 1m bars)
  prev_close           yesterday's RTH close
  daily_trend          Uptrend / Sideways / Downtrend / Unknown

Data sources:
  - Daily bars (1y) from yfinance — used for ATR, prev_close, trend
  - 1-min bars (30d) from Alpaca paper IEX — used for vol stats + premkt range

Usage:
    py scripts/profile_builder.py --tickers NVDA,AMD,PLTR
    py scripts/profile_builder.py --from-snapshot       # uses today's T-30 watchlist
    py scripts/profile_builder.py --from-snapshot --max-age-hours 4   # only refresh if stale
    py scripts/profile_builder.py --tickers NVDA --force # force-refresh even if fresh
"""
from __future__ import annotations

import argparse
import json
import sys
import time as _time
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
PROFILES_DIR = SKILL_DIR / "profiles"
WORKTREE_ROOT = SKILL_DIR.parent
BRIEF_SNAPSHOTS = WORKTREE_ROOT / "intraday-premarket-brief" / "snapshots"

ET = ZoneInfo("America/New_York")

# Trend classification (same algo as MATP/classify_trend.py — duplicated
# inline to keep skill self-contained).
MIN_BARS_FOR_EMA200 = 210
SLOPE_WINDOW = 20

# ATR window in trading days.
ATR_PERIOD = 14

# Lookback windows for volume + premkt range stats.
MINUTE_LOOKBACK_DAYS = 30
RTH_START = time(9, 30)
RTH_END = time(15, 59, 59)
PREMKT_START = time(4, 0)
PREMKT_END = time(9, 29, 59)


# ---------- Math helpers ----------

def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1 - alpha) * out[-1])
    return out


def classify_trend(closes: list[float]) -> str:
    if len(closes) < MIN_BARS_FOR_EMA200:
        return "Unknown"
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    e200 = ema(closes, 200)
    c = closes[-1]
    a20, a50, a200 = e20[-1], e50[-1], e200[-1]
    slope_idx = -1 - SLOPE_WINDOW
    if abs(slope_idx) > len(e50):
        return "Unknown"
    slope_up = e50[-1] > e50[slope_idx]
    slope_dn = e50[-1] < e50[slope_idx]
    if c > a20 > a50 > a200 and slope_up:
        return "Uptrend"
    if c < a20 < a50 < a200 and slope_dn:
        return "Downtrend"
    return "Sideways"


def compute_atr(highs: list[float], lows: list[float], closes: list[float],
                period: int = ATR_PERIOD) -> float:
    """14-day ATR using simple average of true ranges."""
    n = len(closes)
    if n < period + 1:
        return 0.0
    trs: list[float] = []
    for i in range(1, n):
        h, l, prev_c = highs[i], lows[i], closes[i - 1]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    # Use last `period` true ranges.
    recent = trs[-period:]
    return sum(recent) / len(recent)


# ---------- Watchlist loading ----------

def load_watchlist_from_snapshot() -> list[str]:
    """Use the union of Early Gappers + Faders from the most recent T-30
    snapshot. Falls back to today's T-60 if no T-30 yet."""
    if not BRIEF_SNAPSHOTS.exists():
        sys.exit(
            f"ERROR: brief snapshots directory not found at {BRIEF_SNAPSHOTS}. "
            "Run intraday-premarket-brief first, or pass --tickers explicitly."
        )
    today = date.today().isoformat()
    candidates = [
        BRIEF_SNAPSHOTS / f"{today}_t30.json",
        BRIEF_SNAPSHOTS / f"{today}_t60.json",
    ]
    # Also try the most recent any-mode snapshot if today's isn't there.
    all_snaps = sorted(BRIEF_SNAPSHOTS.glob("*.json"), reverse=True)
    candidates.extend(all_snaps[:2])

    for path in candidates:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        sections = data.get("sections", {})
        early = [r["ticker"] for r in sections.get("early_gappers", [])]
        faders = [r["ticker"] for r in sections.get("faders", [])]
        tickers = list(dict.fromkeys(early + faders))  # de-dupe preserving order
        if tickers:
            print(f"Loaded {len(tickers)} tickers from {path.name}", file=sys.stderr)
            return tickers
    sys.exit("ERROR: no usable brief snapshot found. Pass --tickers explicitly.")


# ---------- Profile freshness check ----------

def is_fresh(profile_path: Path, max_age_hours: float) -> bool:
    if not profile_path.exists():
        return False
    try:
        data = json.loads(profile_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    as_of = data.get("as_of")
    if not as_of:
        return False
    try:
        # Use the file's mtime as the timestamp (as_of is just a date)
        mtime = datetime.fromtimestamp(profile_path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return False
    age = datetime.now(timezone.utc) - mtime
    return age.total_seconds() < max_age_hours * 3600


# ---------- Profile build ----------

def fetch_daily_bars(tickers: list[str], yf):
    """One batched yfinance call for 1y of daily bars."""
    return yf.download(
        tickers=tickers,
        period="1y",
        interval="1d",
        group_by="ticker",
        auto_adjust=True,
        progress=False,
        threads=True,
    )


def fetch_minute_bars(tickers: list[str], alpaca_client):
    """30 days of 1-min bars from Alpaca paper IEX. Returns dict[ticker] -> list[Bar]."""
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=MINUTE_LOOKBACK_DAYS + 7)  # buffer for weekends
    req = StockBarsRequest(
        symbol_or_symbols=tickers,
        timeframe=TimeFrame(1, TimeFrameUnit.Minute),
        start=start,
        end=end,
        feed="iex",
    )
    resp = alpaca_client.get_stock_bars(req)
    # resp.data is a dict[symbol] -> list[Bar]
    return resp.data


def compute_minute_stats(bars) -> dict:
    """Given a list of Alpaca Bar objects (1m), compute:
      - avg_minute_vol_rth + minute_vol_stddev (RTH bars only)
      - premkt_range_avg (max-min per day, premkt window only)
    """
    rth_volumes: list[float] = []
    premkt_per_day: dict[date, dict] = {}  # date -> {high, low}

    for b in bars:
        ts_et = b.timestamp.astimezone(ET)
        t = ts_et.time()
        d = ts_et.date()

        if RTH_START <= t <= RTH_END:
            if b.volume:
                rth_volumes.append(float(b.volume))
        elif PREMKT_START <= t <= PREMKT_END:
            entry = premkt_per_day.setdefault(d, {"high": float(b.high),
                                                  "low": float(b.low)})
            entry["high"] = max(entry["high"], float(b.high))
            entry["low"] = min(entry["low"], float(b.low))

    # Volume stats (population stddev)
    if rth_volumes:
        n = len(rth_volumes)
        mean = sum(rth_volumes) / n
        variance = sum((v - mean) ** 2 for v in rth_volumes) / n
        stddev = variance ** 0.5
    else:
        mean, stddev = 0.0, 0.0

    # Premkt range avg
    pm_ranges = [d["high"] - d["low"] for d in premkt_per_day.values()]
    pm_range_avg = sum(pm_ranges) / len(pm_ranges) if pm_ranges else 0.0

    return {
        "avg_minute_vol_rth": mean,
        "minute_vol_stddev": stddev,
        "premkt_range_avg": pm_range_avg,
        "_data_quality": {
            "rth_minutes_observed": len(rth_volumes),
            "premkt_days_observed": len(premkt_per_day),
        },
    }


def build_profile(ticker: str, daily_sub, minute_bars) -> dict:
    """Build one ticker's profile from daily + 1m bars."""
    # --- From daily bars ---
    try:
        df = daily_sub[["Open", "High", "Low", "Close"]].dropna()
    except (KeyError, TypeError):
        return {"ticker": ticker, "error": "no daily data"}
    if len(df) == 0:
        return {"ticker": ticker, "error": "empty daily data"}

    highs = df["High"].tolist()
    lows = df["Low"].tolist()
    closes = df["Close"].tolist()
    prev_close = float(closes[-1])
    atr = compute_atr(highs, lows, closes, period=ATR_PERIOD)
    atr_pct = (atr / prev_close * 100) if prev_close else 0.0
    trend = classify_trend(closes)

    # --- From minute bars ---
    minute_stats = compute_minute_stats(minute_bars or [])

    profile = {
        "ticker": ticker,
        "as_of": date.today().isoformat(),
        "atr_14d": round(atr, 4),
        "atr_pct": round(atr_pct, 4),
        "avg_minute_vol_rth": round(minute_stats["avg_minute_vol_rth"], 2),
        "minute_vol_stddev": round(minute_stats["minute_vol_stddev"], 2),
        "premkt_range_avg": round(minute_stats["premkt_range_avg"], 4),
        "prev_close": round(prev_close, 4),
        "daily_trend": trend,
        "data_quality": {
            "daily_bars_count": len(df),
            **minute_stats["_data_quality"],
        },
    }
    return profile


# ---------- Persistence ----------

def write_profile(profile: dict) -> Path:
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    path = PROFILES_DIR / f"{profile['ticker']}.json"
    path.write_text(json.dumps(profile, indent=2), encoding="utf-8")
    return path


# ---------- Main ----------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--tickers", help="Comma-separated ticker list")
    g.add_argument("--from-snapshot", action="store_true",
                   help="Use today's T-30 (or T-60) brief consensus list")
    p.add_argument("--max-age-hours", type=float, default=0.0,
                   help="Skip refreshing profiles younger than this. Default: always refresh.")
    p.add_argument("--force", action="store_true",
                   help="Refresh even if profile is fresh.")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    # Resolve tickers
    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        tickers = load_watchlist_from_snapshot()

    if not tickers:
        sys.exit("ERROR: empty ticker list")

    # Filter to stale profiles unless --force
    if args.max_age_hours > 0 and not args.force:
        before = len(tickers)
        tickers = [t for t in tickers
                   if not is_fresh(PROFILES_DIR / f"{t}.json", args.max_age_hours)]
        skipped = before - len(tickers)
        if skipped:
            print(f"Skipped {skipped} fresh profiles (use --force to refresh)", file=sys.stderr)
    if not tickers:
        print("All profiles already fresh. Nothing to do.", file=sys.stderr)
        return 0

    print(f"Building profiles for {len(tickers)} tickers: {', '.join(tickers)}", file=sys.stderr)

    # --- Lazy imports so the script can show --help without deps ---
    try:
        import yfinance as yf
    except ImportError:
        sys.exit("yfinance not installed. Run: py -m pip install -r requirements.txt")

    # --- Daily bars (yfinance) ---
    print("Fetching daily bars (yfinance)...", file=sys.stderr)
    daily_data = fetch_daily_bars(tickers, yf)

    # --- Minute bars (Alpaca paper IEX) ---
    print(f"Fetching {MINUTE_LOOKBACK_DAYS}d of 1m bars (Alpaca IEX)...", file=sys.stderr)
    # Delegate to alpaca-trader-paper's _client.py for credential loading.
    alpaca_scripts = SKILL_DIR.parent / "alpaca-trader-paper" / "scripts"
    sys.path.insert(0, str(alpaca_scripts))
    try:
        from _client import market_data_client
    except ImportError as e:
        sys.exit(
            f"Could not import alpaca-trader-paper client: {e}\n"
            f"Expected at: {alpaca_scripts / '_client.py'}"
        )
    alpaca_client = market_data_client()
    minute_data = fetch_minute_bars(tickers, alpaca_client)

    # --- Build + write profiles ---
    built = 0
    failed: list[str] = []
    for t in tickers:
        try:
            daily_sub = daily_data if len(tickers) == 1 else daily_data[t]
            minute_bars = minute_data.get(t, [])
            profile = build_profile(t, daily_sub, minute_bars)
            if "error" in profile:
                failed.append(f"{t}: {profile['error']}")
                continue
            path = write_profile(profile)
            print(
                f"  {t:6s}  ATR ${profile['atr_14d']:.2f} ({profile['atr_pct']:.1f}%)  "
                f"avg_min_vol {profile['avg_minute_vol_rth']:,.0f} ± "
                f"{profile['minute_vol_stddev']:,.0f}  "
                f"premkt_range ${profile['premkt_range_avg']:.2f}  "
                f"trend {profile['daily_trend']}",
                file=sys.stderr,
            )
            built += 1
        except (KeyError, ValueError, TypeError) as e:
            failed.append(f"{t}: {type(e).__name__}: {e}")

    print(f"\nWrote {built}/{len(tickers)} profiles to {PROFILES_DIR}", file=sys.stderr)
    if failed:
        print(f"Failed: {failed}", file=sys.stderr)
    return 0 if built > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
