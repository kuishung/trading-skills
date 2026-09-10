"""Swing-structure monitor — Higher High / Higher Low vs Lower High / Lower Low.

The user's rule, verbatim (2026-09-10):

    Higher High, Higher Low = Bullish Trend
    Lower High or Lower Low = Momentum Decreased

That is a rule about **swing structure**, not about moving averages, and it is
deliberately kept separate from the EMA-stack `trend` the boards already show
(`Uptrend / Sideways / Downtrend`). The two answer different questions and can
disagree honestly: price can sit above a rising EMA20 while printing a lower
high — that disagreement IS the early warning the rule exists to catch.

Reading the rule precisely matters, because the two halves are not symmetric:

  * the bullish case is an AND — it needs a higher high **and** a higher low;
  * the deceleration case is an OR — **either** a lower high **or** a lower low
    is enough.

So a bar sequence that prints a higher high but a lower low is NOT bullish; the
lower low alone decides it. The classifier therefore tests the OR first.

Everything is computed from LIVE daily bars (`services.prices`, Yahoo) — never
parquet, per the CLAUDE.md scope rule: this is an operational "now" view.

Confirmation lag is inherent and is surfaced, not hidden: a swing high is only a
swing high once `right` bars have printed lower highs after it, so the newest
swing is confirmed a few bars late. Anything else would repaint — today's high
would be "a higher high" until tomorrow undoes it.
"""
from __future__ import annotations

import time

from . import resources_bridge  # noqa: F401  (puts TradeHunter/ on sys.path)
from .prices import fetch_daily_ohlc

# Pivot sensitivity for DAILY bars. find_pivots' own docstring calls 3 a
# reasonable default for 1-min bars and 5-10 for daily; 5 keeps a swing to
# roughly a trading week on each side, which is the structure a swing trader
# actually reads, without collapsing every two-day wiggle into a pivot.
LEFT = RIGHT = 5

# Verdict labels — the user's words, used verbatim in the UI so what the screen
# says and what the rule says can never drift apart.
BULLISH = "Bullish Trend"
DECELERATED = "Momentum Decreased"
UNCLEAR = "Unclear"

_TTL = 900.0  # 15 min, matching the ETF/price caches
_cache: dict[str, tuple[float, dict]] = {}


def _to_pattern_bars(bars: list[dict]) -> list[dict]:
    """`/prices` shape -> the {t,o,h,l,c,v} shape resources.patterns consumes."""
    out = []
    for b in bars or []:
        try:
            out.append({
                "t": b["time"], "o": b["open"], "h": b["high"],
                "l": b["low"], "c": b["close"], "v": 0,
            })
        except (KeyError, TypeError):
            continue
    return out


def classify(bars: list[dict]) -> dict:
    """Classify swing structure from `/prices`-shaped bars.

    Returns:
        {
          verdict:  "Bullish Trend" | "Momentum Decreased" | "Unclear",
          state:    "bullish" | "decelerated" | "unclear",   # for CSS
          reason:   short human sentence naming what decided it,
          high:     {"prev": p, "last": p, "higher": bool} | None,
          low:      {"prev": p, "last": p, "higher": bool} | None,
          pivots:   [{time, price, kind}]  # the four points, for chart markers
        }
    """
    from resources.patterns import find_pivots

    pb = _to_pattern_bars(bars)
    blank = {"verdict": UNCLEAR, "state": "unclear",
             "reason": "not enough price history", "high": None, "low": None,
             "pivots": []}
    if len(pb) < (LEFT + RIGHT + 4):
        return blank

    piv = find_pivots(pb, left=LEFT, right=RIGHT)
    highs = [p for p in piv if p["type"] == "high"]
    lows = [p for p in piv if p["type"] == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return {**blank, "reason": "fewer than two confirmed swings on each side"}

    ph, lh = highs[-2], highs[-1]      # prior / latest swing HIGH
    pl, ll = lows[-2], lows[-1]        # prior / latest swing LOW
    higher_high = lh["price"] > ph["price"]
    higher_low = ll["price"] > pl["price"]

    # The OR is tested FIRST: one lower swing decides the verdict even when the
    # other side is still making higher ones (see the module docstring).
    if not higher_high or not higher_low:
        broke = []
        if not higher_high:
            broke.append("lower high")
        if not higher_low:
            broke.append("lower low")
        verdict, state = DECELERATED, "decelerated"
        reason = " and ".join(broke) + " - momentum decreased"
    else:
        verdict, state = BULLISH, "bullish"
        reason = "higher high and higher low - bullish trend"

    def _pt(p, kind):
        return {"time": p.get("t"), "price": round(float(p["price"]), 2), "kind": kind}

    return {
        "verdict": verdict,
        "state": state,
        "reason": reason,
        "high": {"prev": round(ph["price"], 2), "last": round(lh["price"], 2),
                 "higher": higher_high},
        "low": {"prev": round(pl["price"], 2), "last": round(ll["price"], 2),
                "higher": higher_low},
        "pivots": [
            _pt(ph, "prior high"),
            _pt(lh, "HH" if higher_high else "LH"),
            _pt(pl, "prior low"),
            _pt(ll, "HL" if higher_low else "LL"),
        ],
    }


def structure_for(symbol: str) -> dict:
    """Swing structure for one symbol from live daily bars. Cached ~15 min.

    Soft-fails to the Unclear verdict: a monitor that breaks the page it is
    attached to is worse than a monitor that says nothing.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return classify([])
    now = time.time()
    hit = _cache.get(sym)
    if hit and hit[0] > now:
        return hit[1]
    try:
        out = classify(fetch_daily_ohlc(sym))
    except Exception:  # noqa: BLE001
        out = {"verdict": UNCLEAR, "state": "unclear",
               "reason": "structure unavailable", "high": None, "low": None,
               "pivots": []}
    _cache[sym] = (now + _TTL, out)
    return out


def structure_for_many(symbols) -> dict[str, dict]:
    """Structure for a list of symbols, fetched CONCURRENTLY.

    The Sector ETFs tab monitors thirteen symbols at once; serially that is
    thirteen Yahoo round trips one after another. In parallel it costs about one
    — and after the first call every symbol is served from the 15-min cache
    anyway (the same cache `services.prices` keeps for the charts).
    """
    from concurrent.futures import ThreadPoolExecutor

    syms = [s.strip().upper() for s in symbols if s and s.strip()]
    if not syms:
        return {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        return dict(zip(syms, ex.map(structure_for, syms)))
