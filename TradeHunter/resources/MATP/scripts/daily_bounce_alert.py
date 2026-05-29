#!/usr/bin/env python
"""Daily bounce-alert report -> Telegram.

For tickers in MATP_table.csv classified as Uptrend, find the freshest
EMA20 / EMA50 bounce setups for "earliest entry" trade ideas. Sorted into
three buckets:

  HOT      - the most recent closed daily bar TOUCHED EMA20 or EMA50
             (low <= EMA * (1 + tol)) and CLOSED above it. Best entry
             is today's market open; stop just under that bar's low.
  WARM     - touched 1-3 bars ago, closes have stayed above the EMA since,
             and current close is still above. Late but viable.
  WATCHING - in Uptrend, current close is within 1.5% above EMA20 or EMA50
             and has not touched in the last 4 bars. Setup may form in the
             next 1-3 sessions; add to your alert list.

The report is sent via Telegram (configure once with setup_telegram.py)
and also printed to stdout. Re-classifies trend on every run so an old
Sideways stock that turned Uptrend today gets picked up.

Usage:
    py scripts/daily_bounce_alert.py
    py scripts/daily_bounce_alert.py --dry-run     # print only, no Telegram
    py scripts/daily_bounce_alert.py --csv path    # alt CSV
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))
from _envpath import env_path
ENV_PATH = env_path(SKILL_DIR, "matp")
DEFAULT_CSV = SKILL_DIR / "MATP_table.csv"

MIN_BARS_FOR_EMA200 = 210
SLOPE_WINDOW = 20         # bars; same as classify_trend.py
TOLERANCE = 0.005         # 0.5% touch buffer
WARM_LOOKBACK = 3         # bars back, exclusive of "today"
WATCH_BAND = 0.015        # 1.5% above EMA = "approaching"
# Close strength = (close - low) / (high - low), expressed as %.
# A HOT bar must close at least this far up from its low to count as a
# real rejection candle. Weak closes (e.g. 24%) mean the bar touched the
# EMA but closed near the low — not a confident bounce, often continues
# down the next day. 50% = closed in the upper half of the bar's range.
MIN_HOT_STRENGTH = 50.0

# Telegram caps a single message at 4096 chars. We send chunks if longer.
TG_MAX = 4000


# ---------- Math helpers ----------

def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1 - alpha) * out[-1])
    return out


def classify_trend(closes: list[float], slope_window: int) -> str:
    """Same rule as classify_trend.py — duplicated to keep this script
    standalone. Strict stacking + EMA50 slope."""
    if len(closes) < MIN_BARS_FOR_EMA200:
        return "Unknown"
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    e200 = ema(closes, 200)
    c = closes[-1]
    a20, a50, a200 = e20[-1], e50[-1], e200[-1]
    slope_idx = -1 - slope_window
    if abs(slope_idx) > len(e50):
        return "Unknown"
    slope_up = e50[-1] > e50[slope_idx]
    slope_dn = e50[-1] < e50[slope_idx]
    if c > a20 > a50 > a200 and slope_up:
        return "Uptrend"
    if c < a20 < a50 < a200 and slope_dn:
        return "Downtrend"
    return "Sideways"


# ---------- Bounce detection ----------

def detect_bounce(opens, highs, lows, closes, ema_period: int) -> dict | None:
    """Return a state dict for this ticker against this EMA period, or None.

    State is one of HOT / WARM / WATCHING. Caller picks the most aggressive
    when multiple EMAs trigger.
    """
    if len(closes) < ema_period + WARM_LOOKBACK + 1:
        return None
    es = ema(closes, ema_period)
    n = len(closes)

    # HOT: last closed bar touched + closed above EMA + close strength
    # (close position within bar's range) above the minimum. A bar that
    # touched the EMA but closed near its low isn't a rejection — it's
    # a fade, and often breaks the EMA the next day.
    last = n - 1
    if lows[last] <= es[last] * (1 + TOLERANCE) and closes[last] > es[last]:
        bar_range = max(highs[last] - lows[last], 1e-9)
        strength = (closes[last] - lows[last]) / bar_range * 100
        if strength >= MIN_HOT_STRENGTH:
            return {
                "state": "HOT",
                "ema_period": ema_period,
                "ema_value": es[last],
                "touch_depth_pct": (es[last] - lows[last]) / es[last] * 100,
                "close_strength_pct": strength,
                "bar_open": opens[last],
                "bar_high": highs[last],
                "bar_low": lows[last],
                "bar_close": closes[last],
            }
        # Weak rejection — let it fall through. If it holds above EMA
        # tomorrow it'll surface as WARM on the next run.

    # WARM: bars [-2, -3, -4] touched + every close since stayed above
    for ago in range(1, WARM_LOOKBACK + 1):
        idx = last - ago
        if idx < 0:
            break
        if lows[idx] <= es[idx] * (1 + TOLERANCE) and closes[idx] > es[idx]:
            # All bars from idx+1 .. last must have close > EMA at that bar
            held = all(closes[i] > es[i] for i in range(idx + 1, last + 1))
            if held and closes[last] > es[last]:
                return {
                    "state": "WARM",
                    "ema_period": ema_period,
                    "ema_value": es[last],
                    "bars_ago": ago,
                    "last_close": closes[last],
                    "gain_since_pct": (closes[last] - es[last]) / es[last] * 100,
                }

    # WATCHING: close within band above current EMA, not yet touched in window
    if 0 < closes[last] - es[last] <= es[last] * WATCH_BAND:
        return {
            "state": "WATCHING",
            "ema_period": ema_period,
            "ema_value": es[last],
            "last_close": closes[last],
            "distance_pct": (closes[last] - es[last]) / es[last] * 100,
        }
    return None


def pick_strongest(b20: dict | None, b50: dict | None) -> dict | None:
    """If both EMAs gave a signal, prefer HOT > WARM > WATCHING. Within the
    same state, prefer EMA20 (shallower pullback, closer to action)."""
    rank = {"HOT": 3, "WARM": 2, "WATCHING": 1}
    candidates = [b for b in (b20, b50) if b is not None]
    if not candidates:
        return None
    return max(candidates, key=lambda b: (rank[b["state"]], -b["ema_period"]))


# ---------- Formatting ----------

def fmt_hot(ticker: str, b: dict, matp: float | None) -> str:
    period = b["ema_period"]
    out = [f"<b>{ticker}</b> · EMA{period} bounce"]
    out.append(
        f"  Last bar: O {b['bar_open']:.2f}  H {b['bar_high']:.2f}  "
        f"L {b['bar_low']:.2f}  C {b['bar_close']:.2f}"
    )
    out.append(
        f"  EMA{period} ${b['ema_value']:.2f}  ·  touch -{b['touch_depth_pct']:.2f}%  "
        f"·  strength {b['close_strength_pct']:.0f}%"
    )
    entry = b["bar_close"]
    stop = b["bar_low"]
    risk = entry - stop
    out.append(f"  Entry ≥ ${entry:.2f}  ·  Stop &lt; ${stop:.2f}")
    if matp and risk > 0:
        reward = matp - entry
        if reward > 0:
            rr = reward / risk
            target_pct = (matp - entry) / entry * 100
            out.append(f"  Target ${matp:.2f} (+{target_pct:.1f}%)  ·  R/R 1:{rr:.1f}")
    return "\n".join(out)


def fmt_warm(ticker: str, b: dict, matp: float | None) -> str:
    period = b["ema_period"]
    parts = [
        f"<b>{ticker}</b> · EMA{period} bounce {b['bars_ago']}d ago",
        f"now +{b['gain_since_pct']:.1f}% above EMA{period}",
    ]
    if matp:
        gain_to_matp = (matp - b["last_close"]) / b["last_close"] * 100
        parts.append(f"MATP +{gain_to_matp:.1f}%")
    return "  " + "  ·  ".join(parts)


def fmt_watching(ticker: str, b: dict) -> str:
    period = b["ema_period"]
    return (
        f"  <b>{ticker}</b> · C ${b['last_close']:.2f}, EMA{period} ${b['ema_value']:.2f} "
        f"(+{b['distance_pct']:.2f}%) pulling in"
    )


def build_report(
    rows: list[dict],
    bounces: dict[str, dict],
    matps: dict[str, float],
    trends: dict[str, str],
) -> str:
    hot = [(t, b) for t, b in bounces.items() if b["state"] == "HOT"]
    warm = [(t, b) for t, b in bounces.items() if b["state"] == "WARM"]
    watching = [(t, b) for t, b in bounces.items() if b["state"] == "WATCHING"]

    hot.sort(key=lambda x: -x[1]["close_strength_pct"])
    warm.sort(key=lambda x: x[1]["bars_ago"])
    watching.sort(key=lambda x: x[1]["distance_pct"])

    n_uptrend = sum(1 for v in trends.values() if v == "Uptrend")
    today = date.today().isoformat()

    lines = [f"<b>MATP daily bounce report — {today}</b>"]
    lines.append(
        f"<i>{n_uptrend} Uptrend / {len(trends)} total · "
        f"HOT {len(hot)} · WARM {len(warm)} · WATCHING {len(watching)}</i>"
    )
    lines.append("")

    if hot:
        lines.append("🔥 <b>HOT</b> — last bar touched, enter at today's open")
        for t, b in hot:
            lines.append(fmt_hot(t, b, matps.get(t)))
            lines.append("")

    if warm:
        lines.append("🟡 <b>WARM</b> — touched 1-3 bars ago, still above EMA")
        for t, b in warm:
            lines.append(fmt_warm(t, b, matps.get(t)))
        lines.append("")

    if watching:
        lines.append("👀 <b>WATCHING</b> — within 1.5% above EMA, setup may form")
        for t, b in watching:
            lines.append(fmt_watching(t, b))
        lines.append("")

    if not (hot or warm or watching):
        lines.append("<i>No bounce setups today.</i>")

    return "\n".join(lines)


# ---------- Telegram ----------

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


def send_telegram(token: str, chat_id: str, html: str) -> None:
    """Send a single HTML message. Splits at chunk boundaries if oversize."""
    chunks: list[str] = []
    if len(html) <= TG_MAX:
        chunks = [html]
    else:
        # Split on blank lines to preserve formatting.
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
                "User-Agent": "matp-bounce/1",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            if not body.get("ok"):
                sys.stderr.write(f"Telegram error: {body}\n")
        except Exception as exc:
            sys.stderr.write(f"Telegram send failed: {exc}\n")


# ---------- Main ----------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--csv", default=str(DEFAULT_CSV),
                   help=f"Path to MATP CSV (default: {DEFAULT_CSV})")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the report only; do not send to Telegram.")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    # Windows default cp1252 can't print emoji. Switch stdout to UTF-8.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    csv_path = Path(args.csv)
    if not csv_path.exists():
        sys.exit(f"ERROR: CSV not found: {csv_path}. Run the MATP pipeline first.")

    try:
        import yfinance as yf
    except ImportError:
        sys.exit("yfinance not installed. Run: py -m pip install -r requirements.txt")

    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    tickers = [r["Ticker"].strip() for r in rows if r["Ticker"].strip()]
    matps: dict[str, float] = {}
    for r in rows:
        try:
            matps[r["Ticker"].strip()] = float(r.get("MATP", "") or "nan")
        except ValueError:
            pass

    print(f"Fetching 1y daily bars for {len(tickers)} tickers...")
    data = yf.download(
        tickers=tickers, period="1y", interval="1d",
        group_by="ticker", auto_adjust=True, progress=False, threads=True,
    )

    def extract_ohlc(sub):
        # Returns (opens, highs, lows, closes) as lists, dropping any
        # row where Close is NaN.
        try:
            sub = sub[["Open", "High", "Low", "Close"]].dropna()
        except KeyError:
            return None
        if len(sub) == 0:
            return None
        return (
            sub["Open"].tolist(),
            sub["High"].tolist(),
            sub["Low"].tolist(),
            sub["Close"].tolist(),
        )

    trends: dict[str, str] = {}
    bounces: dict[str, dict] = {}
    failed: list[str] = []

    for t in tickers:
        try:
            sub = data if len(tickers) == 1 else data[t]
            ohlc = extract_ohlc(sub)
            if ohlc is None:
                failed.append(t)
                continue
            opens, highs, lows, closes = ohlc
            trends[t] = classify_trend(closes, SLOPE_WINDOW)
            if trends[t] != "Uptrend":
                continue
            b20 = detect_bounce(opens, highs, lows, closes, 20)
            b50 = detect_bounce(opens, highs, lows, closes, 50)
            best = pick_strongest(b20, b50)
            if best is not None:
                bounces[t] = best
        except (KeyError, ValueError, TypeError):
            failed.append(t)

    if failed:
        print(f"Retrying {len(failed)} failed tickers sequentially: {', '.join(failed)}")
        for t in failed:
            try:
                hist = yf.Ticker(t).history(period="1y", interval="1d", auto_adjust=True)
                ohlc = extract_ohlc(hist)
                if ohlc is None:
                    trends[t] = "Unknown"
                    continue
                opens, highs, lows, closes = ohlc
                trends[t] = classify_trend(closes, SLOPE_WINDOW)
                if trends[t] != "Uptrend":
                    continue
                b20 = detect_bounce(opens, highs, lows, closes, 20)
                b50 = detect_bounce(opens, highs, lows, closes, 50)
                best = pick_strongest(b20, b50)
                if best is not None:
                    bounces[t] = best
            except Exception:
                trends[t] = "Unknown"

    report_html = build_report(rows, bounces, matps, trends)

    # Plain-text version for stdout (strip HTML tags and unescape entities).
    import html as _html
    import re
    stdout_text = _html.unescape(re.sub(r"<[^>]+>", "", report_html))
    print()
    print(stdout_text)

    if args.dry_run:
        return 0

    env = load_env()
    token = env.get("TELEGRAM_BOT_TOKEN")
    chat_id = env.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        sys.stderr.write(
            "\nTelegram not configured. Run scripts/setup_telegram.py first, "
            "or pass --dry-run to skip sending.\n"
        )
        return 1

    send_telegram(token, chat_id, report_html)
    print("\nSent to Telegram.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
