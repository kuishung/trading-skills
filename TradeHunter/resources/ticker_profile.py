"""Per-ticker behavior profile -- Layer-1 resource (UNIVERSAL).

The profile is a per-ticker JSON file holding behavioral baselines that
ANY strategy can consume. The numbers describe the TICKER, not the
strategy — NVDA's 3m-bar ATR is the same whether GUNS, DITP, or any
future strategy is asking. Different strategies read different fields;
nothing computes the same baseline twice.

These profiles are what makes the user's "normalized strategy parameters"
rule possible (see CLAUDE.md): every strategy threshold expressed as
ATR multiples / volume z-scores / R-multiples / percentile ranks,
sourced from the ticker's own behavioral baseline rather than absolute
dollar/share/percent values.

Storage:
  data/ticker_profile/<TICKER>.json
  Universal (NOT per-family). Sits alongside the parquet bars under
  data/ — gitignored, Dropbox-synced, same lifecycle as price_history.
  Regenerable in one pass over the local parquets, so we don't commit
  the daily churn; the path travels via Dropbox so a fresh PC has them
  the moment it syncs.

Profile shape (one file per ticker, sections per timeframe):

  {
    "ticker": "NVDA",
    "as_of": "2026-05-23",
    "last_refreshed_utc": "2026-05-23T07:00:00+00:00",
    "data_sources": {"daily": "yfinance", "1m": "ibkr", "3m": "derived_from_1m"},

    "stats_daily": {
      "atr_14d":       4.21,    # 14-day ATR in dollars
      "atr_pct":       2.85,    # ATR as % of close
      "prev_close":    147.71,
      "daily_trend":   "Uptrend",
    },
    "stats_1m_rth": {
      "avg_vol":           350000,
      "vol_stddev":        180000,
      "premkt_range_avg":  1.42,
    },
    "stats_3m_rth": {
      "atr":                 0.62,
      "range_p10":           0.18,
      "range_p50":           0.45,
      "range_p90":           1.08,
      "body_ratio_mean":     0.55,
      "body_ratio_stddev":   0.18,
      "upper_tail_p90":      0.32,
      "lower_tail_p90":      0.30,
      "outside_bar_freq":    0.08,
      "n_bars_used":         1330,
      "n_sessions_used":     10,
    },
    # stats_5m_rth / stats_15m_rth are added when a strategy needs them.
  }

Top-level legacy fields (`atr_14d`, `prev_close`, ...) are kept ALONGSIDE
the section-namespaced versions for the duration of the migration. New
code should read from sections; old GUNS reads still work.

Refresh policy:
  TTL = 24 hours from `last_refreshed_utc`. The orchestrator's pre-market
  hook (T-60 before market open) calls `refresh_profile(ticker)` for
  every symbol on tomorrow's watchlist. Within the day, callers use
  `get_profile()` (cache-only, fast) and check `is_stale()` if they
  want a freshness gate.

Data sources (priority order):
  1. **Local parquets** (`data/price_history/`) — the primary source
     for stats_3m_rth / stats_5m_rth / stats_15m_rth. Read via
     `bars_store.load_bars(sym, timeframe='1min')` then aggregate with
     `patterns.aggregate_to_n_min`. No network needed.
  2. **yfinance** — daily ATR + trend (free, no API key).
  3. **TradingView MCP** — chart-derived ATR / ADR (TODO).
  4. **IBKR historical bars** — minute-bar enrichment (TODO).
  5. **Manual** — edit the JSON directly to override any field.

Caller examples:
    from ticker_profile import get_profile
    p = get_profile("NVDA")
    daily_atr = p["stats_daily"]["atr_14d"]                  # any strategy
    p90 = p["stats_3m_rth"]["upper_tail_p90"]                # DITP intraday

Run as CLI:
    py resources/ticker_profile.py POET                # show / refresh
    py resources/ticker_profile.py --force-refresh NVDA
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# --- TradeHunter bootstrap ---
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


# ---------- Path helpers ----------

def profile_path(ticker: str) -> Path:
    """{data_root}/ticker_profile/<TICKER>.json.
    data_root resolves via scripts._common.get_data_root() — honours
    cfg["data_root"] for per-PC external paths."""
    from scripts._common import get_data_root
    return get_data_root() / "ticker_profile" / f"{ticker.upper()}.json"


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

def get_profile(ticker: str) -> dict | None:
    """Return the cached profile if present, else None.

    Does NOT refresh -- caller decides whether to call `refresh_profile`
    based on staleness. Separating read from refresh keeps the
    shortlist-phase path fast (most days profiles are already fresh).
    """
    p = profile_path(ticker)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        safe_log_stdout(f"ticker_profile: failed to read {p}: {exc}")
        return None


def save_profile(profile: dict) -> None:
    """Write the profile JSON. Creates data/ticker_profile/ if needed."""
    ticker = profile["ticker"].upper()
    p = profile_path(ticker)
    p.parent.mkdir(parents=True, exist_ok=True)
    profile["last_refreshed_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    p.write_text(json.dumps(profile, indent=2, default=str), encoding="utf-8")


def is_stale(ticker: str,
             ttl_hours: int = PROFILE_TTL_HOURS) -> bool:
    """True if no profile OR profile's last_refreshed_utc is older than TTL."""
    profile = get_profile(ticker)
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

def compute_3m_stats_from_1m_bars(minute_bars: list[dict],
                                  *, atr_period: int = 14
                                  ) -> dict | None:
    """Compute the `stats_3m_rth` section from a list of 1-minute bars.

    Aggregates to 3m via `patterns.aggregate_to_n_min`, drops pre-market
    bars (keeps only RTH 09:30-16:00 ET via UTC offset awareness — we
    use the bar timestamp's hour heuristic since timezone info on parquet
    bars varies by source), then computes:
      - ATR (Wilder, last bar)
      - range percentiles (p10 / p50 / p90)
      - body / range ratio mean + stddev
      - upper / lower tail ratio p90
      - outside-bar frequency (current.h > prev.h AND current.l < prev.l)

    Returns None if too few RTH bars after aggregation.
    """
    if not minute_bars:
        return None
    try:
        import patterns  # type: ignore
    except ImportError:
        safe_log_stdout("ticker_profile: patterns module unavailable; skip 3m stats")
        return None

    # RTH filter — keep bars whose UTC hour falls inside the widest
    # year-round RTH window (13:30-21:00 UTC covers EDT and EST).
    # Bar timestamps may be either datetime objects OR ISO strings
    # (bars_store returns ISO strings; live ingest returns datetimes).
    def _ts_hour_min(t):
        if t is None:
            return None, None
        # datetime path
        hr = getattr(t, "hour", None)
        if hr is not None:
            return hr, getattr(t, "minute", 0)
        # ISO string path: "2026-05-14T08:38:00+00:00"
        if isinstance(t, str):
            try:
                # cheap parse — split on 'T' then ':'
                time_part = t.split("T", 1)[1]
                hh, mm = time_part.split(":", 2)[:2]
                return int(hh), int(mm)
            except (IndexError, ValueError):
                return None, None
        return None, None

    rth = []
    for b in minute_bars:
        hr, mn = _ts_hour_min(b.get("t"))
        if hr is None:
            continue
        if not (13 <= hr <= 21):
            continue
        if hr == 13 and mn < 30:
            continue
        rth.append(b)

    if len(rth) < atr_period * 3:   # need at least atr_period 3m bars
        return None

    # patterns.aggregate_to_n_min accesses t.year / t.hour / etc., so
    # coerce ISO strings → datetime here.
    rth_dt: list[dict] = []
    for b in rth:
        t = b["t"]
        if isinstance(t, str):
            try:
                t = datetime.fromisoformat(t.replace("Z", "+00:00"))
            except ValueError:
                continue
        b2 = dict(b)
        b2["t"] = t
        rth_dt.append(b2)

    bars_3m = patterns.aggregate_to_n_min(rth_dt, n=3)
    if len(bars_3m) < atr_period + 5:
        return None

    import numpy as np
    o = np.array([b["o"] for b in bars_3m], dtype=float)
    h = np.array([b["h"] for b in bars_3m], dtype=float)
    l = np.array([b["l"] for b in bars_3m], dtype=float)
    c = np.array([b["c"] for b in bars_3m], dtype=float)

    rng = h - l
    body = np.abs(c - o)
    upper_tail = h - np.maximum(o, c)
    lower_tail = np.minimum(o, c) - l
    # Avoid divide-by-zero on flat bars
    safe_rng = np.where(rng > 0, rng, np.nan)
    body_ratio = body / safe_rng
    upper_tail_ratio = upper_tail / safe_rng
    lower_tail_ratio = lower_tail / safe_rng

    # Wilder ATR
    atr_3m = patterns.atr_wilder_np(h, l, c, period=atr_period) \
        if hasattr(patterns, "atr_wilder_np") else float(np.nanmean(rng))

    # Outside-bar frequency
    outside = (h[1:] > h[:-1]) & (l[1:] < l[:-1])
    outside_freq = float(outside.sum()) / max(len(outside), 1)

    def _percentile(arr, q):
        arr = arr[~np.isnan(arr)]
        return float(np.percentile(arr, q)) if len(arr) else None

    return {
        "atr":               round(atr_3m, 4),
        "range_p10":         round(_percentile(rng, 10), 4),
        "range_p50":         round(_percentile(rng, 50), 4),
        "range_p90":         round(_percentile(rng, 90), 4),
        "body_ratio_mean":   round(float(np.nanmean(body_ratio)), 4),
        "body_ratio_stddev": round(float(np.nanstd(body_ratio)), 4),
        "upper_tail_p90":    round(_percentile(upper_tail_ratio, 90), 4),
        "lower_tail_p90":    round(_percentile(lower_tail_ratio, 90), 4),
        "outside_bar_freq":  round(outside_freq, 4),
        "n_bars_used":       len(bars_3m),
        "atr_period":        atr_period,
    }


def refresh_profile(
    ticker: str,
    *,
    source: str = "auto",
) -> dict | None:
    """Refresh the profile for `ticker`. Returns the new profile or
    None if no data source could provide data.

    Build order (each section is best-effort; missing sections are
    just omitted from the output):
      1. stats_daily       — from yfinance (or local parquet daily)
      2. stats_1m_rth      — from yfinance 1m minute bars (or local parquet 1m)
      3. stats_3m_rth      — derived from local parquet 1m via aggregation

    `source` options:
      "auto"     -- yfinance for daily + 1m, local parquet for 3m
      "yfinance" -- yfinance only (daily + 1m; no 3m derived stats)
      "local"    -- local parquet only (daily + 1m + 3m derived)

    Stub-todo: TV MCP + IBKR direct sources.
    """
    profile: dict = {
        "ticker": ticker.upper(),
        "as_of": datetime.now(timezone.utc).date().isoformat(),
        "data_sources": {},
    }

    yfinance_minute_bars: list[dict] = []

    # --- stats_daily ---
    if source in ("auto", "yfinance"):
        try:
            daily, minute = _fetch_yfinance(ticker)
            yfinance_minute_bars = minute or []
            if daily:
                daily_section = compute_profile_from_daily_bars(ticker, daily)
                # Strip the legacy top-level keys that compute_profile_from_daily_bars
                # returns and pack them under stats_daily for the new format.
                stats_daily = {
                    "atr_14d":     daily_section.get("atr_14d"),
                    "atr_pct":     daily_section.get("atr_pct"),
                    "prev_close":  daily_section.get("prev_close"),
                    "daily_trend": daily_section.get("daily_trend"),
                    "atr_period":  daily_section.get("atr_period"),
                    "n_daily_bars_used": daily_section.get("n_daily_bars_used"),
                }
                profile["stats_daily"] = stats_daily
                profile["data_sources"]["daily"] = "yfinance"
                # Legacy top-level keys for back-compat with old GUNS reads
                profile.update({
                    "atr_14d": stats_daily["atr_14d"],
                    "atr_pct": stats_daily["atr_pct"],
                    "prev_close": stats_daily["prev_close"],
                    "daily_trend": stats_daily["daily_trend"],
                })
        except Exception as exc:
            safe_log_stdout(f"ticker_profile: yfinance daily failed for {ticker}: {exc}")

    # --- stats_1m_rth ---
    if yfinance_minute_bars:
        try:
            vol_stats = compute_volume_stats_from_minute_bars(yfinance_minute_bars)
            if vol_stats:
                profile["stats_1m_rth"] = {
                    "avg_vol":             vol_stats.get("avg_minute_vol_rth"),
                    "vol_stddev":          vol_stats.get("minute_vol_stddev"),
                    "premkt_range_today":  vol_stats.get("premkt_range_today"),
                    "n_bars_used":         vol_stats.get("n_rth_bars_used"),
                }
                profile["data_sources"]["1m"] = "yfinance"
                # Legacy top-level keys
                profile.update({
                    "avg_minute_vol_rth": vol_stats.get("avg_minute_vol_rth"),
                    "minute_vol_stddev":  vol_stats.get("minute_vol_stddev"),
                    "premkt_range_today": vol_stats.get("premkt_range_today"),
                })
        except Exception as exc:
            safe_log_stdout(f"ticker_profile: 1m vol stats failed for {ticker}: {exc}")

    # --- stats_3m_rth ---  (from LOCAL 1m parquet, not yfinance)
    try:
        import bars_store  # noqa: E402  (lazy — only when refresh runs)
        local_1m_bars = bars_store.load_bars(ticker.upper(), timeframe="1min")
        if local_1m_bars:
            stats_3m = compute_3m_stats_from_1m_bars(local_1m_bars)
            if stats_3m:
                profile["stats_3m_rth"] = stats_3m
                profile["data_sources"]["3m"] = "derived_from_1m_parquet"
    except Exception as exc:
        safe_log_stdout(f"ticker_profile: 3m derive failed for {ticker}: {exc}")

    if not profile.get("stats_daily") and not profile.get("stats_3m_rth"):
        return None  # no data at all

    save_profile(profile)
    return profile


def _fetch_yfinance(ticker: str) -> tuple[list[dict] | None, list[dict] | None]:
    """Returns (daily_bars, minute_bars). Either can be None on failure."""
    try:
        import yfinance as yf
    except ImportError:
        safe_log_stdout("ticker_profile: yfinance not installed; skip.")
        return None, None
    try:
        t = yf.Ticker(ticker)
    except Exception:
        return None, None

    daily_bars: list[dict] | None = None
    try:
        df = t.history(period="60d", interval="1d", auto_adjust=False)
        if df is not None and not df.empty:
            daily_bars = [{
                "t": ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts,
                "o": float(row["Open"]), "h": float(row["High"]),
                "l": float(row["Low"]), "c": float(row["Close"]),
                "v": int(row.get("Volume", 0) or 0),
            } for ts, row in df.iterrows()]
    except Exception:
        pass

    minute_bars: list[dict] | None = None
    try:
        df = t.history(period="5d", interval="1m", auto_adjust=False)
        if df is not None and not df.empty:
            minute_bars = [{
                "t": ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts,
                "o": float(row["Open"]), "h": float(row["High"]),
                "l": float(row["Low"]), "c": float(row["Close"]),
                "v": int(row.get("Volume", 0) or 0),
            } for ts, row in df.iterrows()]
    except Exception:
        pass

    return daily_bars, minute_bars


# ---------- Convenience: get_or_refresh ----------

def refresh_many(
    tickers: list[str],
    *,
    source: str = "auto",
    pacing_s: float = 0.5,
    on_progress=None,
) -> dict:
    """Refresh a batch of profiles. Returns a summary dict:
        {n_total, n_ok, n_partial, n_failed, failures: [...]}

    `pacing_s` puts a delay between yfinance calls so we don't trip
    free-tier rate limits. 0.5s × 1500 tickers ≈ 12 min, which is
    fine for a one-shot bulk refresh.

    `on_progress(i, ticker, status)` is called after each ticker if
    provided — used by the dashboard endpoint to stream progress.
    """
    import time
    summary = {
        "n_total": len(tickers),
        "n_ok": 0,
        "n_partial": 0,
        "n_failed": 0,
        "failures": [],
    }
    for i, t in enumerate(tickers):
        t = t.upper()
        try:
            p = refresh_profile(t, source=source)
        except Exception as exc:
            p = None
            safe_log_stdout(f"refresh_many: {t} raised {exc}")
        if p is None:
            summary["n_failed"] += 1
            summary["failures"].append(t)
            status = "failed"
        elif "stats_3m_rth" in p and "stats_daily" in p:
            summary["n_ok"] += 1
            status = "ok"
        else:
            summary["n_partial"] += 1
            status = "partial"
        if on_progress:
            try:
                on_progress(i + 1, t, status)
            except Exception:
                pass
        if pacing_s > 0 and i < len(tickers) - 1:
            time.sleep(pacing_s)
    return summary


def profile_health() -> dict:
    """Summarise the state of every profile under data/ticker_profile/.

    Returns:
      {
        n_total:     number of profile JSONs on disk
        n_fresh:     within the 24h TTL
        n_stale:     older than 24h
        n_full:      has stats_daily AND stats_3m_rth
        n_partial:   has stats_daily, lacks stats_3m_rth
        n_no_daily:  lacks stats_daily (rare — yfinance must have failed)
        oldest_ts:   ISO ts of the oldest profile
        newest_ts:   ISO ts of the newest profile
        symbols_3m:  list of symbols with full 3m profile
      }
    """
    from scripts._common import get_data_root
    root = get_data_root() / "ticker_profile"
    if not root.exists():
        return {
            "n_total": 0, "n_fresh": 0, "n_stale": 0,
            "n_full": 0, "n_partial": 0, "n_no_daily": 0,
            "oldest_ts": None, "newest_ts": None,
            "symbols_3m": [],
        }
    files = sorted(root.glob("*.json"))
    n_fresh = n_stale = n_full = n_partial = n_no_daily = 0
    oldest_ts = None
    newest_ts = None
    symbols_3m: list[str] = []
    for p in files:
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if _is_fresh(obj):
            n_fresh += 1
        else:
            n_stale += 1
        ts = obj.get("last_refreshed_utc")
        if ts:
            if oldest_ts is None or ts < oldest_ts:
                oldest_ts = ts
            if newest_ts is None or ts > newest_ts:
                newest_ts = ts
        has_daily = "stats_daily" in obj
        has_3m = "stats_3m_rth" in obj
        if has_daily and has_3m:
            n_full += 1
            sym = obj.get("ticker") or p.stem
            symbols_3m.append(sym.upper())
        elif has_daily:
            n_partial += 1
        else:
            n_no_daily += 1
    return {
        "n_total":    len(files),
        "n_fresh":    n_fresh,
        "n_stale":    n_stale,
        "n_full":     n_full,
        "n_partial":  n_partial,
        "n_no_daily": n_no_daily,
        "oldest_ts":  oldest_ts,
        "newest_ts":  newest_ts,
        "symbols_3m": sorted(symbols_3m),
    }


def get_or_refresh(
    ticker: str,
    *,
    ttl_hours: int = PROFILE_TTL_HOURS,
    source: str = "auto",
) -> dict | None:
    """One-line API for callers: return a fresh profile, refreshing if
    cached one is stale or missing. Returns None if no data source
    could provide one."""
    if is_stale(ticker, ttl_hours):
        new = refresh_profile(ticker, source=source)
        if new is not None:
            return new
    return get_profile(ticker)


# ---------- CLI ----------

def _cli(argv: list[str]) -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("tickers", nargs="+", help="One or more ticker symbols.")
    p.add_argument("--source", default="auto",
                   choices=["auto", "yfinance", "local"],
                   help="Data source override.")
    p.add_argument("--force-refresh", action="store_true",
                   help="Refresh even if the cached profile is fresh.")
    p.add_argument("--ttl-hours", type=int, default=PROFILE_TTL_HOURS)
    args = p.parse_args(argv)

    for ticker in args.tickers:
        ticker = ticker.upper()
        if args.force_refresh:
            prof = refresh_profile(ticker, source=args.source)
        else:
            prof = get_or_refresh(ticker, ttl_hours=args.ttl_hours, source=args.source)
        if not prof:
            safe_log_stdout(f"{ticker}: no profile (refresh failed or no data source)")
            continue
        safe_log_stdout(f"\n=== {ticker} ===")
        # Print the new section-based view first
        for section in ("stats_daily", "stats_1m_rth", "stats_3m_rth",
                        "stats_5m_rth", "stats_15m_rth"):
            if section in prof:
                safe_log_stdout(f"  [{section}]")
                for k, v in prof[section].items():
                    safe_log_stdout(f"    {k}: {v}")
        if "data_sources" in prof:
            safe_log_stdout(f"  data_sources: {prof['data_sources']}")
        safe_log_stdout(f"  last_refreshed_utc: {prof.get('last_refreshed_utc')}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
