"""Per-ticker behavior profile -- Layer-1 resource.

Each ticker accumulates a behavioral baseline:

  {
    "ticker": "POET",
    "as_of": "2026-05-21",
    "atr_14d": 0.42,            # 14-day ATR in dollars
    "atr_pct": 2.85,            # ATR as % of close
    "avg_minute_vol_rth": 35000,
    "minute_vol_stddev": 18000,
    "premkt_range_avg": 0.55,   # 20-day avg PM high-low spread $
    "prev_close": 14.71,
    "daily_trend": "Uptrend",
    "float_shares": 6_200_000,
    "data_source": "tradingview" | "ibkr" | "yfinance" | "manual",
    "last_refreshed_utc": "2026-05-21T07:00:00+00:00",
  }

These profiles are what makes the user's "normalized strategy parameters"
rule possible (see CLAUDE.md): every strategy threshold expressed as
ATR multiples / volume z-scores / R-multiples, sourced from the ticker's
own behavioral baseline rather than absolute dollar/share/percent values.

Storage:
  strategy/<FAMILY>/profiles/<TICKER>.json
  (per-family because behavioral baselines can differ in tuning;
  GUNS cares about pre-market vol while ORB might focus on opening range)

Refresh policy:
  Profiles refresh daily pre-market via `refresh_profile(ticker, family)`.
  TTL = 24 hours from `last_refreshed_utc`. If the cache is stale, the
  refresher fetches new data; if fresh, it just returns the cached blob.

Data sources (priority order, configurable):
  1. **TradingView MCP** -- when the MCP is connected and the user has
     navigated TV to the ticker, read ATR / ADR / volume averages from
     the chart's indicator panel. Highest fidelity since it's the same
     view the user looks at.
  2. **IBKR historical bars** -- `resources/ibkr_data.ibkr_history_bars`
     gives multi-day 1-min RTH data; compute ATR + vol stats from it.
  3. **yfinance** -- daily bars for the past 30 days; cheaper but less
     granular than IBKR.
  4. **Manual** -- user can edit the JSON directly for a specific
     ticker if they want to override.

This module's compute functions accept bars from ANY source so the
adapter layer is decoupled from the math.

Current implementation:
  - Read / write / staleness check are done.
  - `compute_profile_from_bars()` computes ATR + vol stats from a
    list of daily bars (yfinance-style).
  - `refresh_profile()` is a SKELETON -- it picks a data source based
    on availability. Today it has the yfinance fallback wired (lowest
    setup friction). TV MCP + IBKR wiring are TODO until the next
    session refreshes my tool inventory.

Caller (e.g. Setup 1 shortlist phase):
    from ticker_profile import get_profile
    profile = get_profile("POET", family="GUNS")
    atr = profile["atr_14d"] if profile else None

Run as CLI for debugging:
    py resources/ticker_profile.py POET                # show / refresh
    py resources/ticker_profile.py --family GUNS NVDA
    py resources/ticker_profile.py --force-refresh POET
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# --- intraday-bot bootstrap ---
_root = Path(__file__).resolve().parent
while _root != _root.parent and not (_root / "SKILL.md").exists():
    _root = _root.parent
SKILL_DIR = _root
for _p in [str(_root)] + [str(_root / s) for s in
        ("scripts", "resources", "strategy", "execution",
         "journal", "review", "dashboard")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)
del _root, _p
# ---

from _common import safe_log_stdout  # noqa: E402


PROFILE_TTL_HOURS = 24
DEFAULT_FAMILY = "GUNS"


# ---------- Path helpers ----------

def profile_path(ticker: str, family: str = DEFAULT_FAMILY) -> Path:
    """strategy/<FAMILY>/profiles/<TICKER>.json"""
    return SKILL_DIR / "strategy" / family / "profiles" / f"{ticker.upper()}.json"


def _is_fresh(profile: dict, ttl_hours: int = PROFILE_TTL_HOURS) -> bool:
    """True if `last_refreshed_utc` is within TTL of now."""
    ts = profile.get("last_refreshed_utc")
    if not ts:
        return False
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    age = datetime.now(timezone.utc) - dt
    return age < timedelta(hours=ttl_hours)


# ---------- Read / write ----------

def get_profile(ticker: str, family: str = DEFAULT_FAMILY) -> dict | None:
    """Return the cached profile if present, else None.

    Does NOT refresh -- caller decides whether to call `refresh_profile`
    based on staleness. Separating read from refresh keeps the
    shortlist-phase path fast (most days profiles are already fresh).
    """
    p = profile_path(ticker, family)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        safe_log_stdout(f"ticker_profile: failed to read {p}: {exc}")
        return None


def save_profile(profile: dict, family: str = DEFAULT_FAMILY) -> None:
    """Write the profile JSON. Creates the profiles/ folder if needed."""
    ticker = profile["ticker"].upper()
    p = profile_path(ticker, family)
    p.parent.mkdir(parents=True, exist_ok=True)
    profile["last_refreshed_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    p.write_text(json.dumps(profile, indent=2, default=str), encoding="utf-8")


def is_stale(ticker: str, family: str = DEFAULT_FAMILY,
             ttl_hours: int = PROFILE_TTL_HOURS) -> bool:
    """True if no profile OR profile's last_refreshed_utc is older than TTL."""
    profile = get_profile(ticker, family)
    if not profile:
        return True
    return not _is_fresh(profile, ttl_hours)


# ---------- Compute from bars ----------

def compute_profile_from_daily_bars(
    ticker: str,
    daily_bars: list[dict],
    *,
    atr_period: int = 14,
) -> dict:
    """Compute ATR + trend stats from daily OHLCV bars.

    Bars are dicts {t, o, h, l, c, v}. Returns a partial profile dict
    without volume-stats fields (those need minute-level data).
    Caller merges with intraday-source stats.

    True Range = max(high-low, |high - prev_close|, |low - prev_close|)
    ATR = simple average of TR over the last `atr_period` bars.
    """
    if len(daily_bars) < 2:
        return {
            "ticker": ticker.upper(),
            "atr_14d": None,
            "atr_pct": None,
            "prev_close": daily_bars[-1]["c"] if daily_bars else None,
            "reason": "insufficient daily bars",
        }
    bars = daily_bars[-(atr_period + 1):]   # need atr_period + 1 to get N TRs
    trs: list[float] = []
    for i in range(1, len(bars)):
        h = bars[i]["h"]
        l = bars[i]["l"]
        prev_c = bars[i - 1]["c"]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    atr = sum(trs) / len(trs) if trs else None
    prev_close = bars[-1]["c"]
    atr_pct = (atr / prev_close * 100.0) if (atr and prev_close > 0) else None

    # Crude daily trend: compare close to 20-day SMA
    closes = [b["c"] for b in daily_bars[-20:]]
    sma20 = sum(closes) / len(closes) if closes else None
    if sma20 is None:
        trend = "Unknown"
    elif prev_close > sma20 * 1.02:
        trend = "Uptrend"
    elif prev_close < sma20 * 0.98:
        trend = "Downtrend"
    else:
        trend = "Sideways"

    return {
        "ticker": ticker.upper(),
        "atr_14d": round(atr, 4) if atr is not None else None,
        "atr_pct": round(atr_pct, 3) if atr_pct is not None else None,
        "prev_close": prev_close,
        "daily_trend": trend,
        "atr_period": atr_period,
        "n_daily_bars_used": len(daily_bars),
    }


def compute_volume_stats_from_minute_bars(minute_bars: list[dict]) -> dict:
    """Average + stddev of 1-min RTH volumes. Excludes pre-market by
    only looking at bars where minute >= 09:30 ET. Returns partial
    profile dict with `avg_minute_vol_rth` + `minute_vol_stddev` +
    `premkt_range_avg`.
    """
    if not minute_bars:
        return {}
    rth_vols: list[int] = []
    pm_bars_today: list[dict] = []
    for b in minute_bars:
        t = b.get("t")
        if t is None:
            continue
        if t.hour >= 9 and not (t.hour == 9 and t.minute < 30):
            rth_vols.append(int(b.get("v", 0) or 0))
        else:
            pm_bars_today.append(b)

    avg_vol = sum(rth_vols) / len(rth_vols) if rth_vols else None
    if rth_vols and len(rth_vols) > 1:
        mu = avg_vol
        var = sum((v - mu) ** 2 for v in rth_vols) / (len(rth_vols) - 1)
        std = var ** 0.5
    else:
        std = None

    pm_range = None
    if pm_bars_today:
        pm_high = max(b["h"] for b in pm_bars_today)
        pm_low = min(b["l"] for b in pm_bars_today)
        pm_range = pm_high - pm_low

    return {
        "avg_minute_vol_rth": int(avg_vol) if avg_vol is not None else None,
        "minute_vol_stddev": int(std) if std is not None else None,
        "premkt_range_today": round(pm_range, 4) if pm_range is not None else None,
        "n_rth_bars_used": len(rth_vols),
    }


# ---------- Refresh ----------

def refresh_profile(
    ticker: str,
    family: str = DEFAULT_FAMILY,
    *,
    source: str = "auto",
) -> dict | None:
    """Refresh the profile for `ticker`. Returns the new profile or
    None if no data source could provide data.

    `source` options:
      "auto"        -- try TV MCP first, then yfinance fallback
      "tv"          -- TradingView MCP only (errors if not available)
      "yfinance"    -- yfinance.Ticker.history daily bars
      "ibkr"        -- ibkr_history_bars (multi-day RTH minute bars)

    Today only the yfinance path is wired -- the TV MCP path will
    activate the next time Claude Code starts a session with the
    tradingview MCP tools available. See CLAUDE.md for the install.
    """
    if source in ("auto", "yfinance"):
        try:
            return _refresh_from_yfinance(ticker, family)
        except Exception as exc:
            safe_log_stdout(
                f"ticker_profile: yfinance refresh failed for {ticker}: {exc}"
            )
            if source == "yfinance":
                return None

    if source == "ibkr":
        safe_log_stdout(
            "ticker_profile: ibkr source not yet wired (TODO: use "
            "ibkr_history_bars + compute on minute bars)."
        )
        return None

    if source == "tv":
        safe_log_stdout(
            "ticker_profile: TV MCP source not yet wired (TODO: read "
            "ATR / ADR / volume averages from the user's TradingView "
            "chart via the tradesdontlie/tradingview-mcp). Available "
            "in the NEXT Claude Code session after MCP install."
        )
        return None

    return None


def _refresh_from_yfinance(ticker: str, family: str) -> dict | None:
    """Free-tier yfinance refresh. Gets 30 days of daily bars + computes
    ATR + trend. Volume stats come from minute bars (if available).
    """
    try:
        import yfinance as yf
    except ImportError:
        safe_log_stdout("ticker_profile: yfinance not installed; skip.")
        return None

    try:
        t = yf.Ticker(ticker)
        df_daily = t.history(period="60d", interval="1d", auto_adjust=False)
    except Exception as exc:
        safe_log_stdout(f"ticker_profile: yfinance.history failed for {ticker}: {exc}")
        return None

    if df_daily is None or df_daily.empty:
        return None

    daily_bars: list[dict] = []
    for ts, row in df_daily.iterrows():
        daily_bars.append({
            "t": ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts,
            "o": float(row["Open"]),
            "h": float(row["High"]),
            "l": float(row["Low"]),
            "c": float(row["Close"]),
            "v": int(row.get("Volume", 0) or 0),
        })

    profile = compute_profile_from_daily_bars(ticker, daily_bars)
    profile["data_source"] = "yfinance"
    profile["as_of"] = datetime.now(timezone.utc).date().isoformat()

    # Best-effort minute-bar enrichment (yfinance gives last 7 days of
    # 1-min bars on free tier; we use yesterday's RTH session).
    try:
        df_min = t.history(period="5d", interval="1m", auto_adjust=False)
        if df_min is not None and not df_min.empty:
            minute_bars = []
            for ts, row in df_min.iterrows():
                minute_bars.append({
                    "t": ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts,
                    "o": float(row["Open"]),
                    "h": float(row["High"]),
                    "l": float(row["Low"]),
                    "c": float(row["Close"]),
                    "v": int(row.get("Volume", 0) or 0),
                })
            vol_stats = compute_volume_stats_from_minute_bars(minute_bars)
            profile.update(vol_stats)
    except Exception:
        pass

    save_profile(profile, family=family)
    return profile


# ---------- Convenience: get_or_refresh ----------

def get_or_refresh(
    ticker: str,
    family: str = DEFAULT_FAMILY,
    *,
    ttl_hours: int = PROFILE_TTL_HOURS,
    source: str = "auto",
) -> dict | None:
    """One-line API for callers: return a fresh profile, refreshing if
    cached one is stale or missing. Returns None if no data source
    could provide one."""
    if is_stale(ticker, family, ttl_hours):
        new = refresh_profile(ticker, family, source=source)
        if new is not None:
            return new
    return get_profile(ticker, family)


# ---------- CLI ----------

def _cli(argv: list[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("tickers", nargs="+", help="One or more ticker symbols.")
    p.add_argument("--family", default=DEFAULT_FAMILY,
                   help="Strategy family folder under strategy/ (default GUNS).")
    p.add_argument("--source", default="auto",
                   choices=["auto", "tv", "yfinance", "ibkr"],
                   help="Data source override.")
    p.add_argument("--force-refresh", action="store_true",
                   help="Refresh even if the cached profile is fresh.")
    p.add_argument("--ttl-hours", type=int, default=PROFILE_TTL_HOURS)
    args = p.parse_args(argv)

    for ticker in args.tickers:
        ticker = ticker.upper()
        if args.force_refresh:
            prof = refresh_profile(ticker, args.family, source=args.source)
        else:
            prof = get_or_refresh(ticker, args.family,
                                  ttl_hours=args.ttl_hours, source=args.source)
        if not prof:
            safe_log_stdout(f"{ticker}: no profile (refresh failed or no data source)")
            continue
        safe_log_stdout(f"\n=== {ticker} ===")
        for k, v in prof.items():
            safe_log_stdout(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
