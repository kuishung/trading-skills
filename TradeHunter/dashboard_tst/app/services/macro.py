"""Computed inputs for the Macro board.

Only the parts that are ARITHMETIC live here — quotes and derived levels. The
judgement parts (policy narrative, calendar, geopolitics) are written by the
agent or a moderator into `MacroAnalysis`; nothing in this module tries to
interpret anything.

DATA-SOURCE RULE (CLAUDE.md): the macro board is a LIVE/OPERATIONAL view — it
reflects the market *now* — so every number here is fetched live (Yahoo via
`services.prices`). It must never read the parquet store, which is reserved for
backtesting and offline analysis. That applies to the breadth gauge too: the
tempting "read 1500 daily parquet files" shortcut is exactly the thing the rule
forbids, so breadth uses live daily bars instead (see `resources/yf_daily_bars`,
which exists for precisely this in-memory, no-disk purpose).

Soft-fail throughout: a macro tile that can't fetch renders as "—" rather than
taking the page down.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from .prices import fetch_quote

# (symbol, label, what a RISE in it usually means for equity risk appetite).
# `risk` is "on" when up = risk-on, "off" when up = risk-off, None when it's
# genuinely ambiguous — we label the direction rather than pretend it's a signal.
_CROSS_ASSET = [
    ("^VIX", "VIX", "off"),
    ("DX-Y.NYB", "Dollar (DXY)", "off"),
    ("^TNX", "US 10y yield", None),
    ("TLT", "Long bonds (TLT)", None),
    ("HYG", "High yield (HYG)", "on"),
    ("GLD", "Gold (GLD)", None),
    ("USO", "Oil (USO)", None),
    ("^GSPC", "S&P 500", "on"),
]

_CACHE: dict[str, tuple[float, list]] = {}
_TTL = 120.0


def cross_asset() -> list[dict]:
    """Live cross-asset strip: [{symbol, label, price, change_pct, risk}].

    Concurrent fetch (each quote is an independent HTTP call), 2-minute cache so
    a page refresh doesn't re-hit Yahoo eight times. Any symbol that fails comes
    back with price=None and still renders.
    """
    hit = _CACHE.get("cross_asset")
    if hit and hit[0] > time.time():
        return hit[1]

    def one(spec):
        sym, label, risk = spec
        q = None
        try:
            q = fetch_quote(sym)
        except Exception:  # noqa: BLE001
            q = None
        return {
            "symbol": sym,
            "label": label,
            "risk": risk,
            "price": (q or {}).get("price"),
            "change_pct": (q or {}).get("change_pct"),
        }

    with ThreadPoolExecutor(max_workers=8) as ex:
        rows = list(ex.map(one, _CROSS_ASSET))
    _CACHE["cross_asset"] = (time.time() + _TTL, rows)
    return rows


def risk_tone(rows: list[dict]) -> dict:
    """A crude, HONEST risk read from the strip: count how many risk-directional
    assets are pointing risk-on vs risk-off today.

    Deliberately simple and deliberately labelled as such in the UI. It is a
    tally of same-day moves, not a regime model — presenting it as more would be
    the kind of false precision this project avoids elsewhere. Symbols with
    `risk=None` (yields, gold, oil) are excluded: their direction genuinely
    doesn't map to risk appetite without context.
    """
    on = off = 0
    for r in rows:
        chg, risk = r.get("change_pct"), r.get("risk")
        if chg is None or risk is None:
            continue
        up = chg > 0
        if (risk == "on" and up) or (risk == "off" and not up):
            on += 1
        elif (risk == "on" and not up) or (risk == "off" and up):
            off += 1
    total = on + off
    if not total:
        return {"tone": "unknown", "on": 0, "off": 0}
    if on > off:
        tone = "risk-on"
    elif off > on:
        tone = "risk-off"
    else:
        tone = "mixed"
    return {"tone": tone, "on": on, "off": off}
