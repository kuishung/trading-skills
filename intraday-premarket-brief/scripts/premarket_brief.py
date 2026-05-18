#!/usr/bin/env python
"""Intraday pre-market brief — ranks a Finviz screener's tickers into
Early Gappers (continuation candidates) and Faders (extended / fade-prone).

Two modes for the twice-daily ritual:

  --mode t60   First study, 60 min before US open (08:30 ET / 20:30 MYT-EDT).
               Emits two sections, top 10 each, plus M&A exclusion list.
               Persists a JSON snapshot to snapshots/<date>_t60.json.

  --mode t30   Confirmation study, 30 min before US open. Loads the same-day
               t60 snapshot and surfaces:
                  Consensus  - tickers in BOTH t60 and t30 top 10 (per section)
                  Faded      - in t60 only
                  Emerged    - in t30 only
               Then prints the t30 top-10 sections in full. Persists
               snapshots/<date>_t30.json. If no t60 snapshot exists, T-30
               still produces a fresh ranking but skips the consensus block.

Pipeline per run:
  1. Scrape Finviz URL (auto-paginated). --tickers <list> overrides scraping.
  2. yfinance: batched 1y daily bars -> EMA20/50/200 trend + prior-day H/L.
  3. yfinance: batched 1d 1m bars (prepost=True) -> premkt gap / vwap / hi / lo.
  4. yfinance: per-ticker news (parallel) -> classify catalyst, drop M&A names.
  5. Score, split into sections (Early Gappers / Faders), take top 10 each.
  6. Render markdown to stdout. Send to Telegram if --send-telegram.

Usage:
    py scripts/premarket_brief.py --mode t60
    py scripts/premarket_brief.py --mode t30
    py scripts/premarket_brief.py --mode t60 --send-telegram
    py scripts/premarket_brief.py --mode t60 --tickers NVDA,AMD,TSLA
    py scripts/premarket_brief.py --mode t60 --url 'https://finviz.com/...'
    py scripts/premarket_brief.py --mode t60 --json
    py scripts/premarket_brief.py --mode t60 --dry-run   # alias for --no-telegram

Run setup.py first to configure the Finviz URL (and optional Telegram).
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))
from _envpath import env_path
ENV_PATH = env_path(SKILL_DIR, "intraday-premarket")
SNAPSHOTS_DIR = SKILL_DIR / "snapshots"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# --- Trend (same algo as MATP/classify_trend.py) ---
MIN_BARS_FOR_EMA200 = 210
SLOPE_WINDOW = 20

# --- Section split thresholds ---
GAPPER_MIN_GAP = 0.5      # %
GAPPER_MAX_GAP = 4.0      # %
GAPPER_MIN_POS = 0.5      # position-in-range
FADER_MIN_GAP = 4.0       # gap above this = fader regardless of structure
FADER_MAX_POS = 0.4       # position below this = fader

# --- Scoring weights ---
W_CATALYST = 0.40
W_GAPVOL = 0.30
W_TREND = 0.20
W_LEVEL = 0.10

# --- Per-section list width ---
TOP_N = 10

# --- Telegram ---
TG_MAX = 4000  # chars per message; split if longer

# --- News lookback window ---
NEWS_LOOKBACK_HOURS = 36  # extra slack for AMC news from previous session


# ============================================================
#  Env / Snapshot persistence
# ============================================================

def load_env() -> dict[str, str]:
    if not ENV_PATH.exists():
        return {}
    out: dict[str, str] = {}
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def snapshot_path(d: date, mode: str) -> Path:
    return SNAPSHOTS_DIR / f"{d.isoformat()}_{mode}.json"


def save_snapshot(d: date, mode: str, payload: dict) -> Path:
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    p = snapshot_path(d, mode)
    p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return p


def load_snapshot(d: date, mode: str) -> dict | None:
    p = snapshot_path(d, mode)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


# ============================================================
#  Finviz scrape
# ============================================================

def strip_pagination(url: str) -> str:
    """Remove the &r=<N> offset so we can append our own."""
    parts = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    qs = [(k, v) for k, v in qs if k != "r"]
    new_query = urllib.parse.urlencode(qs)
    return urllib.parse.urlunparse(parts._replace(query=new_query))


def fetch_finviz_page(url: str) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


def extract_tickers_from_page(html: str) -> list[str]:
    """Pull ticker symbols out of a Finviz screener results page.

    Finviz renders the ticker as <a href="quote?t=NVDA&...">NVDA</a> inside
    the screener table. (Note: the old `quote.ashx?t=...` path was deprecated
    around 2024-2025; current Finviz uses `quote?t=...`.) The regex matches
    both forms in case Finviz reverts. We extract from the href because the
    anchor text is sometimes wrapped in a styling span.
    """
    pattern = re.compile(r'href="(?:quote\.ashx|quote)\?t=([A-Z][A-Z0-9.\-]*)', re.IGNORECASE)
    matches = pattern.findall(html)
    # Deduplicate while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for m in matches:
        t = m.upper()
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def scrape_finviz(base_url: str, max_pages: int = 20) -> list[str]:
    base = strip_pagination(base_url)
    sep = "&" if "?" in base else "?"
    all_tickers: list[str] = []
    seen: set[str] = set()
    for page in range(max_pages):
        offset = 1 + 20 * page
        page_url = base if page == 0 else f"{base}{sep}r={offset}"
        try:
            html = fetch_finviz_page(page_url)
        except Exception as exc:
            sys.stderr.write(f"Finviz fetch failed (page offset r={offset}): {exc}\n")
            break
        tickers = extract_tickers_from_page(html)
        new_tickers = [t for t in tickers if t not in seen]
        if not new_tickers:
            break
        all_tickers.extend(new_tickers)
        seen.update(new_tickers)
        if len(tickers) < 20:
            # Last page (Finviz pages are 20 results).
            break
        # Be polite — small throttle between pages.
        time.sleep(0.4)
    return all_tickers


# ============================================================
#  Catalyst classification
# ============================================================

# Order matters: M&A is checked first because some M&A headlines also
# mention "buy" / "rating" terms that would otherwise classify as analyst.
#
# Patterns are matched against the news TITLE only (not summary), because
# summaries frequently contain tangential keywords that produce false
# positives. We also require phrasings that imply the TICKER is the
# SUBJECT/TARGET of the catalyst, not just mentioned in passing. E.g.
# "Rocket Lab Buys Motiv" should NOT flag RKLB as M&A — RKLB is the buyer,
# its price isn't anchored to a deal. We aim to under-classify rather than
# over-classify; missing a real catalyst is recoverable, but dropping a
# tradeable name from the brief on a false M&A flag is not.
CATALYST_PATTERNS = [
    ("MA", re.compile(
        r"\b(to\s+be\s+acquired|"
        r"agree(s|d)?\s+to\s+(be\s+)?(acquire|acquisition|merger|sale)|"
        r"tender\s+offer|"
        r"go(es|ing)?\s+private|taken\s+private|take(s)?\s+private|"
        r"buyout\s+(offer|deal|bid)|"
        r"all-?cash\s+(offer|bid|takeover|deal)|"
        r"merger\s+agreement|completes?\s+merger|"
        r"definitive\s+(merger|acquisition)\s+agreement|"
        r"goes\s+private)\b",
        re.IGNORECASE)),
    ("Earnings", re.compile(
        r"\b(earnings\s+(report|call|beat|miss|preview|results?|ahead|tomorrow|tonight|today)|"
        r"Q[1-4]\s+(earnings|results?|report|20\d{2}|EPS)|"
        r"EPS\s+(of|beat|miss|miss(es|ed))|"
        r"reports?\s+(Q[1-4]|earnings|quarterly|loss|profit)|"
        r"beats?\s+(earnings|estimates|consensus|expectations|EPS)|"
        r"misses?\s+(earnings|estimates|consensus|expectations|EPS)|"
        r"raises?\s+(full-year\s+)?(guidance|outlook|forecast)|"
        r"cuts?\s+(full-year\s+)?(guidance|outlook|forecast)|"
        r"lowers?\s+(guidance|outlook|forecast)|"
        r"quarterly\s+(report|results?|earnings)|"
        r"posts?\s+(quarterly\s+)?(loss|profit|earnings)|"
        r"announces?\s+(Q[1-4]\s+)?(results?|earnings))\b",
        re.IGNORECASE)),
    ("Regulatory", re.compile(
        r"\b(FDA\s+(approval|approves|rejects?|grants?)|PDUFA|"
        r"phase\s*[123]\s+(trial|results?|data)|"
        r"clinical\s+(trial|data|results?)|"
        r"trial\s+(results?|data|readout)|"
        r"approval\s+(granted|received)|"
        r"breakthrough\s+(therapy|designation)|"
        r"SEC\s+(charges|settles|files|investigat))\b",
        re.IGNORECASE)),
    ("Analyst", re.compile(
        r"\b(upgrade[ds]?\s+(to|from|stock|rating)|"
        r"downgrade[ds]?\s+(to|from|stock|rating)|"
        r"initiate[ds]?\s+(coverage|at)|"
        r"reiterate[ds]?\s+(buy|sell|hold|outperform|underperform|neutral|overweight|underweight)|"
        r"maintains?\s+(buy|sell|hold|outperform|underperform|overweight|underweight)\s+(rating|on)|"
        r"price\s+target\s+(raised|cut|lowered|increased|reduced)|"
        r"(raises?|cuts?|lowers?|boosts?|reduces?|hikes?)\s+(price\s+)?(target|PT)\s+(to|on)|"
        r"named\s+top\s+pick|top\s+pick\s+at|"
        r"new\s+(buy|sell|hold)\s+rating)\b",
        re.IGNORECASE)),
]

CATALYST_WEIGHT = {
    "Earnings": 1.0,
    "Regulatory": 0.9,
    "Analyst": 0.7,
    "MA": 0.0,    # excluded entirely; weight irrelevant
    None: 0.0,
}


def classify_headline(title: str) -> str | None:
    if not title:
        return None
    for tag, pattern in CATALYST_PATTERNS:
        if pattern.search(title):
            return tag
    return None


def _normalize_news_item(item: dict) -> tuple[str, float]:
    """Extract (title, unix_ts) from a yfinance news item.

    yfinance changed its news schema circa 2025: items are now wrapped as
    {id, content: {title, summary, pubDate (ISO), ...}} instead of the old
    flat {title, providerPublishTime, ...}. We support both for forward/
    backward compatibility.

    Returns the TITLE only (not summary) because summaries leak tangential
    keywords that fire false-positive catalyst classifications.
    """
    content = item.get("content") or item  # new schema vs old
    title = content.get("title") or ""

    # Timestamp: new schema uses ISO string at pubDate or displayTime; old
    # used unix int at providerPublishTime. Try in that order.
    ts = 0.0
    pub = content.get("pubDate") or content.get("displayTime")
    if pub:
        try:
            # ISO format from yfinance is like "2026-05-17T10:43:18Z".
            dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            ts = dt.timestamp()
        except (ValueError, AttributeError):
            pass
    if ts == 0:
        legacy = item.get("providerPublishTime")
        if legacy:
            try:
                ts = float(legacy)
            except (ValueError, TypeError):
                pass

    return title, ts


def classify_news(news_items: list[dict], lookback_hours: float = NEWS_LOOKBACK_HOURS) -> dict:
    """Aggregate a ticker's news into a catalyst summary.

    Returns:
      {
        "is_ma": bool,                 # True if any M&A headline in window
        "ma_headline": str | None,
        "top_tag": str | None,         # strongest non-MA catalyst, or None
        "top_headline": str | None,
        "all_tags": list[str],         # distinct tags seen
      }
    """
    cutoff = datetime.now(timezone.utc).timestamp() - lookback_hours * 3600
    is_ma = False
    ma_headline: str | None = None
    seen_tags: dict[str, str] = {}  # tag -> headline
    for item in news_items or []:
        title, ts = _normalize_news_item(item)
        if ts and ts < cutoff:
            continue
        if not title:
            continue
        tag = classify_headline(title)
        if tag == "MA":
            is_ma = True
            if ma_headline is None:
                ma_headline = title
        elif tag:
            if tag not in seen_tags:
                seen_tags[tag] = title
    # Priority for "top tag": Earnings > Regulatory > Analyst
    top_tag: str | None = None
    for candidate in ("Earnings", "Regulatory", "Analyst"):
        if candidate in seen_tags:
            top_tag = candidate
            break
    top_headline = seen_tags.get(top_tag) if top_tag else None
    return {
        "is_ma": is_ma,
        "ma_headline": ma_headline,
        "top_tag": top_tag,
        "top_headline": top_headline,
        "all_tags": list(seen_tags.keys()),
    }


# ============================================================
#  Trend (EMA20/50/200 + slope)
# ============================================================

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


# ============================================================
#  Enrichment via yfinance
# ============================================================

def fetch_daily_bars(tickers: list[str], yf):
    """Returns the yfinance multi-ticker DataFrame for 1y daily bars."""
    return yf.download(
        tickers=tickers,
        period="1y",
        interval="1d",
        group_by="ticker",
        auto_adjust=True,
        progress=False,
        threads=True,
    )


def fetch_minute_bars(tickers: list[str], yf):
    """1d / 1m bars with pre/post-market included. ET-aware index."""
    # 2d to ensure both yesterday's close and today's premkt are present.
    return yf.download(
        tickers=tickers,
        period="2d",
        interval="1m",
        group_by="ticker",
        prepost=True,
        auto_adjust=False,
        progress=False,
        threads=True,
    )


def extract_daily(sub) -> tuple[list[float], list[float], list[float], list[float]] | None:
    try:
        sub = sub[["Open", "High", "Low", "Close"]].dropna()
    except (KeyError, TypeError):
        return None
    if len(sub) == 0:
        return None
    return (
        sub["Open"].tolist(),
        sub["High"].tolist(),
        sub["Low"].tolist(),
        sub["Close"].tolist(),
    )


def extract_premkt(sub):
    """From a 1d/1m yfinance frame for a single ticker, find the most recent
    session with pre-market bars (04:00-09:29 ET) and compute the premkt
    metrics. Returns a dict or None.
    """
    try:
        df = sub[["Open", "High", "Low", "Close", "Volume"]].dropna(how="all")
    except (KeyError, TypeError):
        return None
    if df.empty:
        return None

    idx = df.index
    if idx.tz is None:
        df = df.tz_localize("UTC").tz_convert("US/Eastern")
    else:
        df = df.tz_convert("US/Eastern")

    # All bars in pre-market window (any date).
    premkt_all = df.between_time("04:00", "09:29")
    if premkt_all.empty:
        return None

    # Most recent date with premkt bars.
    target_date = premkt_all.index.date.max()
    today_premkt = premkt_all[premkt_all.index.date == target_date]
    if today_premkt.empty:
        return None

    # Prior session's close: last bar strictly before target_date.
    prior = df[df.index.date < target_date]
    # Regular-hours bars only for "prior close" (16:00 ET).
    prior_rh = prior.between_time("09:30", "15:59")
    prior_close = float(prior_rh["Close"].iloc[-1]) if not prior_rh.empty else (
        float(prior["Close"].iloc[-1]) if not prior.empty else None
    )

    premkt_open = float(today_premkt["Open"].iloc[0])
    premkt_high = float(today_premkt["High"].max())
    premkt_low = float(today_premkt["Low"].min())
    current = float(today_premkt["Close"].iloc[-1])
    volumes = today_premkt["Volume"].fillna(0)
    premkt_volume = float(volumes.sum())
    # VWAP using each bar's typical price.
    typical = (today_premkt["High"] + today_premkt["Low"] + today_premkt["Close"]) / 3
    vwap_num = (typical * volumes).sum()
    vwap_den = volumes.sum()
    premkt_vwap = float(vwap_num / vwap_den) if vwap_den > 0 else current

    gap_pct = ((premkt_open - prior_close) / prior_close * 100) if prior_close else 0.0

    rng = premkt_high - premkt_low
    pos_in_range = ((current - premkt_low) / rng) if rng > 0 else 0.5

    return {
        "premkt_date": target_date.isoformat(),
        "prior_close": prior_close,
        "premkt_open": premkt_open,
        "premkt_high": premkt_high,
        "premkt_low": premkt_low,
        "current": current,
        "premkt_vwap": premkt_vwap,
        "premkt_volume": premkt_volume,
        "gap_pct": gap_pct,
        "pos_in_range": pos_in_range,
        "above_vwap": current >= premkt_vwap,
    }


def fetch_news_parallel(tickers: list[str], yf, max_workers: int = 10) -> dict[str, list[dict]]:
    """yfinance.Ticker.news per ticker, in a thread pool."""
    out: dict[str, list[dict]] = {}

    def _one(t: str) -> tuple[str, list[dict]]:
        try:
            return t, (yf.Ticker(t).news or [])
        except Exception:
            return t, []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for fut in as_completed([pool.submit(_one, t) for t in tickers]):
            t, news = fut.result()
            out[t] = news
    return out


# ============================================================
#  Scoring + section split
# ============================================================

def gap_quality(gap_pct: float) -> float:
    """Bell curve over |gap%|. Penalty for extended gaps (late-to-the-party)."""
    g = abs(gap_pct)
    if g < 0.5:
        return 0.2
    if g < 1.0:
        return 0.5
    if g <= 3.0:
        return 1.0
    if g <= 4.0:
        return 0.7
    if g <= 6.0:
        return 0.4
    if g <= 10.0:
        return 0.2
    return 0.1


def trend_alignment(trend: str, gap_pct: float) -> float:
    if trend == "Uptrend" and gap_pct > 0:
        return 1.0
    if trend == "Downtrend" and gap_pct < 0:
        return 1.0
    return 0.0


def level_proximity(current: float, prior_day_high: float, prior_day_low: float) -> float:
    if current > prior_day_high * 1.001:
        return 1.0
    if current < prior_day_low * 0.999:
        return 1.0
    return 0.0


def compute_score(t_data: dict) -> float:
    cat_weight = CATALYST_WEIGHT.get(t_data.get("top_tag"), 0.0)
    # gap_vol component is currently gap_quality alone — yfinance returns
    # 0 volume for all pre-market bars (Yahoo limitation), so we can't
    # mix in a premkt_volume_signal. Plumbing Alpaca's IEX feed for real
    # premkt volume is a v1.1 follow-up.
    gap_vol = gap_quality(t_data["gap_pct"])
    trend = trend_alignment(t_data["trend"], t_data["gap_pct"])
    level = level_proximity(t_data["current"], t_data["prior_day_high"], t_data["prior_day_low"])
    score = 100.0 * (
        W_CATALYST * cat_weight
        + W_GAPVOL * gap_vol
        + W_TREND * trend
        + W_LEVEL * level
    )
    return round(score, 2)


def assign_section(t_data: dict) -> str | None:
    gap = abs(t_data["gap_pct"])
    pos = t_data["pos_in_range"]
    above_vwap = t_data["above_vwap"]
    # Faders dominate: any single fader trigger wins, regardless of gap size.
    if gap > FADER_MIN_GAP:
        return "faders"
    if pos < FADER_MAX_POS:
        return "faders"
    if not above_vwap:
        return "faders"
    # Otherwise — eligible for Early Gappers if gap is in the sweet spot
    # AND structure is bullish (pos >= 0.5, already above VWAP).
    if GAPPER_MIN_GAP <= gap <= GAPPER_MAX_GAP and pos >= GAPPER_MIN_POS:
        return "early_gappers"
    return None  # neutral — dropped


# ============================================================
#  Render
# ============================================================

def fmt_pct(v: float, sign: bool = True) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    if sign:
        return f"{v:+.2f}%"
    return f"{v:.2f}%"


def fmt_vol(v: float) -> str:
    if v is None or v <= 0:
        return "—"
    if v >= 1e9:
        return f"{v/1e9:.2f}B"
    if v >= 1e6:
        return f"{v/1e6:.2f}M"
    if v >= 1e3:
        return f"{v/1e3:.1f}K"
    return f"{v:.0f}"


def fmt_price(v: float) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return f"${v:.2f}"


def render_section_table_md(rows: list[dict]) -> str:
    if not rows:
        return "_(none)_\n"
    lines = [
        "| # | Ticker | Gap% | AvgVol | VWAP | Pos | Catalyst | Trend | Score |",
        "|---|---|---:|---:|:---:|---:|---|---|---:|",
    ]
    for i, r in enumerate(rows, 1):
        cat = r.get("top_tag") or "—"
        trend_label = r.get("trend") or "Unknown"
        trend_map = {"Uptrend": "↑", "Sideways": "→", "Downtrend": "↓"}
        trend_str = trend_map.get(trend_label, "?")
        pos = r.get("pos_in_range", 0.5)
        vwap_str = "↑" if r.get("above_vwap") else "↓"
        lines.append(
            f"| {i} | **{r['ticker']}** | {fmt_pct(r['gap_pct'])} | "
            f"{fmt_vol(r.get('avg_daily_vol', 0))} | {vwap_str} | "
            f"{pos:.0%} | {cat} | {trend_str} | {r['score']:.1f} |"
        )
    return "\n".join(lines) + "\n"


def render_brief_md(payload: dict, diff: dict | None = None) -> str:
    mode = payload["mode"].upper()
    d = payload["date"]
    premkt_dates = sorted(set(
        r["premkt_date"]
        for s in payload["sections"].values()
        for r in s if r.get("premkt_date")
    ))
    premkt_note = ""
    if premkt_dates and (len(premkt_dates) > 1 or premkt_dates[0] != d):
        premkt_note = f"  _(pre-market data sourced from {', '.join(premkt_dates)})_"

    title_emoji = "🌅" if mode == "T60" else "🎯"
    lines = [f"# {title_emoji} Pre-Market Brief — {mode} — {d}{premkt_note}\n"]
    n_early = len(payload["sections"]["early_gappers"])
    n_faders = len(payload["sections"]["faders"])
    n_neutral = len(payload.get("neutral_dropped", []))
    n_early_trunc = len(payload.get("early_truncated", []))
    n_faders_trunc = len(payload.get("faders_truncated", []))
    early_label = f"🟢 {n_early}" + (f"+{n_early_trunc}" if n_early_trunc else "") + " Early Gappers"
    faders_label = f"🔴 {n_faders}" + (f"+{n_faders_trunc}" if n_faders_trunc else "") + " Faders"
    lines.append(
        f"Universe **{payload['universe_count']}** = "
        f"{early_label} · {faders_label} · "
        f"⊘ {n_neutral} neutral · "
        f"⛔ {len(payload['ma_excluded'])} M&A · "
        f"⚠️ {len(payload['data_failures'])} failed"
        + (
            f"  _(showing top {TOP_N} per section; '+N' = truncated)_"
            if (n_early_trunc or n_faders_trunc) else ""
        )
        + "\n"
    )

    if diff and diff.get("consensus_early") is not None:
        lines.append("## ⭐ Consensus (in BOTH T-60 and T-30)\n")
        ce = diff["consensus_early"]
        cf = diff["consensus_faders"]
        if ce:
            lines.append("### 🟢 Early Gappers — Consensus\n")
            lines.append(render_section_table_md(ce))
        if cf:
            lines.append("### 🔴 Faders — Consensus\n")
            lines.append(render_section_table_md(cf))
        if not ce and not cf:
            lines.append("_No ticker appeared in both T-60 and T-30 top 10 of any section._\n")
        if diff.get("faded_early") or diff.get("faded_faders"):
            lines.append("\n## 📉 Faded (was in T-60, dropped out of T-30)\n")
            faded = (diff.get("faded_early") or []) + (diff.get("faded_faders") or [])
            lines.append(", ".join(f"`{t}`" for t in faded) + "\n")
        if diff.get("emerged_early") or diff.get("emerged_faders"):
            lines.append("\n## 📈 Emerged (new in T-30)\n")
            emerged = (diff.get("emerged_early") or []) + (diff.get("emerged_faders") or [])
            lines.append(", ".join(f"`{t}`" for t in emerged) + "\n")
        if diff.get("note"):
            lines.append(f"\n_{diff['note']}_\n")

    lines.append("\n## 🟢 Early Gappers — Continuation candidates\n")
    lines.append(render_section_table_md(payload["sections"]["early_gappers"]))
    headlines_e = [(r["ticker"], r["top_headline"]) for r in payload["sections"]["early_gappers"] if r.get("top_headline")]
    if headlines_e:
        lines.append("\n_Catalysts:_")
        for tkr, head in headlines_e:
            lines.append(f"- **{tkr}**: {head}")
        lines.append("")

    lines.append("\n## 🔴 Faders — Extended / fade-prone\n")
    lines.append(render_section_table_md(payload["sections"]["faders"]))
    headlines_f = [(r["ticker"], r["top_headline"]) for r in payload["sections"]["faders"] if r.get("top_headline")]
    if headlines_f:
        lines.append("\n_Catalysts:_")
        for tkr, head in headlines_f:
            lines.append(f"- **{tkr}**: {head}")
        lines.append("")

    if payload["ma_excluded"]:
        lines.append("\n## ⛔ Excluded due to M&A (no intraday R:R)\n")
        for entry in payload["ma_excluded_details"]:
            head = entry.get("headline") or "(no headline captured)"
            lines.append(f"- **{entry['ticker']}**: {head}")
        lines.append("")

    if payload["data_failures"]:
        lines.append("\n## ⚠️ Data failures (silently skipped)\n")
        lines.append(", ".join(f"`{t}`" for t in payload["data_failures"]) + "\n")

    return "\n".join(lines)


def render_brief_html(payload: dict, diff: dict | None = None) -> str:
    """Telegram HTML version. Pared down: no markdown tables (Telegram doesn't
    render them well in HTML). Per-ticker blocks instead.
    """
    mode = payload["mode"].upper()
    d = payload["date"]
    title_emoji = "🌅" if mode == "T60" else "🎯"
    lines = [f"<b>{title_emoji} Pre-Market Brief — {mode} — {d}</b>"]
    lines.append(
        f"<i>Universe {payload['universe_count']} · "
        f"M&amp;A excluded {len(payload['ma_excluded'])} · "
        f"Failures {len(payload['data_failures'])}</i>"
    )
    lines.append("")

    def _ticker_line(r: dict) -> str:
        cat = r.get("top_tag") or "—"
        vwap_arrow = "↑VWAP" if r.get("above_vwap") else "↓VWAP"
        return (
            f"<b>{r['ticker']}</b> · Gap {fmt_pct(r['gap_pct'])} · "
            f"{vwap_arrow} · pos {r.get('pos_in_range', 0.5):.0%} · {cat} · "
            f"score <b>{r['score']:.1f}</b>"
        )

    if diff and diff.get("consensus_early") is not None:
        lines.append("⭐ <b>Consensus (T-60 ∩ T-30)</b>")
        if diff["consensus_early"]:
            lines.append("🟢 Early Gappers:")
            for r in diff["consensus_early"]:
                lines.append("  " + _ticker_line(r))
        if diff["consensus_faders"]:
            lines.append("🔴 Faders:")
            for r in diff["consensus_faders"]:
                lines.append("  " + _ticker_line(r))
        if not diff["consensus_early"] and not diff["consensus_faders"]:
            lines.append("<i>(none — no overlap between T-60 and T-30)</i>")
        lines.append("")
        if diff.get("faded_early") or diff.get("faded_faders"):
            faded = (diff.get("faded_early") or []) + (diff.get("faded_faders") or [])
            lines.append(f"📉 <b>Faded:</b> {', '.join(faded)}")
        if diff.get("emerged_early") or diff.get("emerged_faders"):
            emerged = (diff.get("emerged_early") or []) + (diff.get("emerged_faders") or [])
            lines.append(f"📈 <b>Emerged:</b> {', '.join(emerged)}")
        lines.append("")

    lines.append("🟢 <b>Early Gappers</b>")
    for r in payload["sections"]["early_gappers"]:
        lines.append(_ticker_line(r))
        if r.get("top_headline"):
            lines.append(f"  <i>{r['top_headline'][:120]}</i>")
    if not payload["sections"]["early_gappers"]:
        lines.append("<i>(none)</i>")
    lines.append("")

    lines.append("🔴 <b>Faders</b>")
    for r in payload["sections"]["faders"]:
        lines.append(_ticker_line(r))
        if r.get("top_headline"):
            lines.append(f"  <i>{r['top_headline'][:120]}</i>")
    if not payload["sections"]["faders"]:
        lines.append("<i>(none)</i>")
    lines.append("")

    if payload["ma_excluded"]:
        lines.append(f"⛔ <b>M&amp;A excluded:</b> {', '.join(payload['ma_excluded'])}")
    if payload["data_failures"]:
        lines.append(
            f"⚠️ <b>Data failures:</b> {', '.join(payload['data_failures'][:20])}"
        )

    return "\n".join(lines)


# ============================================================
#  Telegram
# ============================================================

def send_telegram(token: str, chat_id: str, html: str) -> None:
    chunks: list[str] = []
    if len(html) <= TG_MAX:
        chunks = [html]
    else:
        buf: list[str] = []
        size = 0
        for line in html.split("\n"):
            if size + len(line) + 1 > TG_MAX and buf:
                chunks.append("\n".join(buf))
                buf, size = [], 0
            buf.append(line)
            size += len(line) + 1
        if buf:
            chunks.append("\n".join(buf))

    for chunk in chunks:
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "intraday-brief/1",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            if not body.get("ok"):
                sys.stderr.write(f"Telegram error: {body}\n")
        except Exception as exc:
            sys.stderr.write(f"Telegram send failed: {exc}\n")


# ============================================================
#  Pipeline
# ============================================================

def run_pipeline(tickers: list[str], mode: str, finviz_url: str | None,
                 news_hours: float = NEWS_LOOKBACK_HOURS) -> dict:
    """Returns the snapshot payload for the given ticker list and mode."""
    try:
        import yfinance as yf
    except ImportError:
        sys.exit("yfinance not installed. Run: py -m pip install -r requirements.txt")

    universe_count = len(tickers)
    print(f"Universe: {universe_count} tickers", file=sys.stderr)

    # --- Daily bars ---
    print(f"Fetching daily bars for {len(tickers)} tickers...", file=sys.stderr)
    daily = fetch_daily_bars(tickers, yf)

    daily_metrics: dict[str, dict] = {}
    failed_daily: list[str] = []
    for t in tickers:
        try:
            sub = daily if len(tickers) == 1 else daily[t]
            d = extract_daily(sub)
            if d is None:
                failed_daily.append(t)
                continue
            opens, highs, lows, closes = d
            avg_vol = (
                sum(_safe_vol(sub, i) for i in range(max(0, len(closes) - 20), len(closes)))
                / max(1, min(20, len(closes)))
            )
            daily_metrics[t] = {
                "trend": classify_trend(closes),
                "prior_day_high": highs[-1] if highs else 0.0,
                "prior_day_low": lows[-1] if lows else 0.0,
                "avg_daily_vol": avg_vol,
            }
        except (KeyError, ValueError, TypeError):
            failed_daily.append(t)

    # Retry failed daily sequentially.
    for t in failed_daily[:]:
        try:
            hist = yf.Ticker(t).history(period="1y", interval="1d", auto_adjust=True)
            d = extract_daily(hist)
            if d is None:
                continue
            opens, highs, lows, closes = d
            volumes = hist["Volume"].dropna().tolist()
            avg_vol = (sum(volumes[-20:]) / max(1, len(volumes[-20:]))) if volumes else 0.0
            daily_metrics[t] = {
                "trend": classify_trend(closes),
                "prior_day_high": highs[-1] if highs else 0.0,
                "prior_day_low": lows[-1] if lows else 0.0,
                "avg_daily_vol": avg_vol,
            }
            failed_daily.remove(t)
        except Exception:
            pass

    # --- Minute bars ---
    print(f"Fetching 1m premkt bars for {len(tickers)} tickers...", file=sys.stderr)
    minute = fetch_minute_bars(tickers, yf)

    premkt_metrics: dict[str, dict] = {}
    failed_premkt: list[str] = []
    for t in tickers:
        try:
            sub = minute if len(tickers) == 1 else minute[t]
            pm = extract_premkt(sub)
            if pm is None:
                failed_premkt.append(t)
                continue
            premkt_metrics[t] = pm
        except (KeyError, ValueError, TypeError):
            failed_premkt.append(t)

    # --- News ---
    print(f"Fetching news for {len(tickers)} tickers (parallel)...", file=sys.stderr)
    news_raw = fetch_news_parallel(tickers, yf, max_workers=10)
    news_metrics: dict[str, dict] = {
        t: classify_news(items, lookback_hours=news_hours)
        for t, items in news_raw.items()
    }

    # --- Merge + score ---
    ma_excluded: list[str] = []
    ma_excluded_details: list[dict] = []
    data_failures: list[str] = []
    neutral_dropped: list[str] = []
    scored: list[dict] = []

    for t in tickers:
        nm = news_metrics.get(t, {})
        if nm.get("is_ma"):
            ma_excluded.append(t)
            ma_excluded_details.append({
                "ticker": t,
                "headline": nm.get("ma_headline"),
            })
            continue

        dm = daily_metrics.get(t)
        pm = premkt_metrics.get(t)
        if not dm or not pm:
            data_failures.append(t)
            continue

        merged = {
            "ticker": t,
            **dm,
            **pm,
            **nm,
        }
        merged["score"] = compute_score(merged)
        merged["section"] = assign_section(merged)
        if merged["section"] is None:
            neutral_dropped.append(t)
            continue
        scored.append(merged)

    # --- Section split + top N ---
    early_all = sorted(
        [r for r in scored if r["section"] == "early_gappers"],
        key=lambda r: -r["score"],
    )
    faders_all = sorted(
        [r for r in scored if r["section"] == "faders"],
        key=lambda r: -r["score"],
    )
    early = early_all[:TOP_N]
    faders = faders_all[:TOP_N]
    early_truncated = [r["ticker"] for r in early_all[TOP_N:]]
    faders_truncated = [r["ticker"] for r in faders_all[TOP_N:]]

    today = date.today()
    payload = {
        "mode": mode,
        "date": today.isoformat(),
        "run_time_utc": datetime.now(timezone.utc).isoformat(),
        "finviz_url": finviz_url,
        "universe_count": universe_count,
        "ma_excluded": ma_excluded,
        "ma_excluded_details": ma_excluded_details,
        "data_failures": data_failures,
        "neutral_dropped": neutral_dropped,
        "early_truncated": early_truncated,
        "faders_truncated": faders_truncated,
        "sections": {
            "early_gappers": early,
            "faders": faders,
        },
    }
    return payload


def _safe_vol(sub, i: int) -> float:
    try:
        v = sub["Volume"].iloc[i]
        return float(v) if v == v else 0.0  # NaN check
    except (KeyError, IndexError, ValueError, TypeError):
        return 0.0


def compute_diff(t60: dict, t30: dict) -> dict:
    """For T-30: who's in both, who faded, who emerged. Per section."""

    def _by_ticker(rows: list[dict]) -> dict[str, dict]:
        return {r["ticker"]: r for r in rows}

    def _section_diff(s_t60: list[dict], s_t30: list[dict]):
        a = _by_ticker(s_t60)
        b = _by_ticker(s_t30)
        consensus_tickers = [t for t in (r["ticker"] for r in s_t30) if t in a]
        consensus = [b[t] for t in consensus_tickers]
        faded = [t for t in a.keys() if t not in b]
        emerged = [t for t in b.keys() if t not in a]
        return consensus, faded, emerged

    ce, fe, ee = _section_diff(
        t60["sections"]["early_gappers"],
        t30["sections"]["early_gappers"],
    )
    cf, ff, ef = _section_diff(
        t60["sections"]["faders"],
        t30["sections"]["faders"],
    )
    return {
        "consensus_early": ce,
        "consensus_faders": cf,
        "faded_early": fe,
        "faded_faders": ff,
        "emerged_early": ee,
        "emerged_faders": ef,
    }


# ============================================================
#  Main
# ============================================================

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--mode", choices=["t60", "t30"], required=True,
                   help="Which leg of the twice-daily ritual to run.")
    p.add_argument("--url", default=None,
                   help="Override the Finviz URL from .env for this run only.")
    p.add_argument("--tickers", default=None,
                   help="Comma-separated ticker list. Skips Finviz scrape.")
    p.add_argument("--send-telegram", action="store_true",
                   help="Push brief to Telegram (must have setup.py run).")
    p.add_argument("--no-telegram", action="store_true",
                   help="Force stdout-only, even if Telegram is configured.")
    p.add_argument("--dry-run", action="store_true",
                   help="Alias for --no-telegram.")
    p.add_argument("--json", action="store_true",
                   help="Print the snapshot payload as JSON instead of markdown.")
    p.add_argument("--max-tickers", type=int, default=None,
                   help="Cap universe size for testing.")
    p.add_argument("--news-hours", type=float, default=NEWS_LOOKBACK_HOURS,
                   help=f"News lookback window in hours (default: {NEWS_LOOKBACK_HOURS}). "
                        f"Bump to 72 when testing on weekends to catch Friday catalysts.")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    env = load_env()

    # Resolve ticker universe.
    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        finviz_url = None
    else:
        finviz_url = args.url or env.get("INTRADAY_FINVIZ_URL", "").strip()
        if not finviz_url:
            sys.exit(
                "ERROR: No Finviz URL configured. Either run scripts/setup.py, "
                "pass --url <URL>, or pass --tickers TKR1,TKR2,..."
            )
        print(f"Scraping Finviz: {finviz_url}", file=sys.stderr)
        tickers = scrape_finviz(finviz_url)
        if not tickers:
            sys.exit(
                "ERROR: Finviz scrape returned 0 tickers. Site may be rate-"
                "limiting or HTML structure changed. Try --tickers as fallback."
            )

    if args.max_tickers:
        tickers = tickers[:args.max_tickers]

    payload = run_pipeline(tickers, args.mode, finviz_url, news_hours=args.news_hours)

    # Persist snapshot.
    today = date.today()
    snap_path = save_snapshot(today, args.mode, payload)
    print(f"Snapshot: {snap_path}", file=sys.stderr)

    # Diff vs T-60 if this is T-30.
    diff: dict | None = None
    if args.mode == "t30":
        t60_payload = load_snapshot(today, "t60")
        if t60_payload:
            diff = compute_diff(t60_payload, payload)
        else:
            diff = {"note": "No same-day T-60 snapshot found — Consensus block skipped."}

    # Render.
    if args.json:
        out = {"payload": payload, "diff": diff}
        print(json.dumps(out, indent=2, default=str))
    else:
        print(render_brief_md(payload, diff))

    # Telegram.
    no_tg = args.no_telegram or args.dry_run
    if args.send_telegram and not no_tg:
        token = env.get("TELEGRAM_BOT_TOKEN")
        chat_id = env.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            sys.stderr.write(
                "ERROR: --send-telegram passed but Telegram not configured. "
                "Run scripts/setup.py to add it.\n"
            )
            return 1
        send_telegram(token, chat_id, render_brief_html(payload, diff))
        print("\nSent to Telegram.", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
