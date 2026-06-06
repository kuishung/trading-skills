"""Swing/Trend profile reader for the /profile page.

dashboard_tst is the trend & swing platform, so it surfaces the SWING profile
(`<data_root>/swing_profile/<T>.json`) -- the intraday profile lives in
dashboard_intraday. File-read only (the swing profile JSON is just data the
swing_profile.py regen wrote); no resources import, no parquet. data_root is the
parent of TST_PRICE_HISTORY_DIR, same as pipeline_runs.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..config import settings


def _swing_dir() -> Path | None:
    ph = settings.price_history_dir
    if not ph:
        return None
    return Path(ph).parent / "swing_profile"


def configured() -> bool:
    return bool(settings.price_history_dir)


def swing_profile(ticker: str) -> dict | None:
    """Read one ticker's swing profile, or None if absent / not configured."""
    d = _swing_dir()
    if d is None or not ticker:
        return None
    p = d / f"{ticker.strip().upper()}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def available(limit: int = 2000) -> list[str]:
    """Tickers that have a swing profile (for an autocomplete / count)."""
    d = _swing_dir()
    if d is None or not d.exists():
        return []
    return sorted(p.stem for p in list(d.glob("*.json"))[:limit])


def _f(v, suffix=""):
    return "-" if v is None else f"{v}{suffix}"


def display_rows(p: dict) -> list[dict]:
    """Flatten a swing profile into [{label, value, sub}] cells for the template,
    so the Jinja side stays a simple loop (no fragile inline expressions)."""
    ma = p.get("ma") or {}
    def yn(v):
        return "-" if v is None else ("Y" if v else "N")
    stack = ("stacked bull" if ma.get("stacked_bull")
             else ("stacked bear" if ma.get("stacked_bear") else "mixed"))
    slope = "50 slope up" if ma.get("ema50_slope_up") else "50 slope down"
    pos = p.get("pos_52w")
    return [
        {"label": "EMA 20 / 50 / 200",
         "value": f"{_f(ma.get('ema20'))} / {_f(ma.get('ema50'))} / {_f(ma.get('ema200'))}",
         "sub": f"{stack}, {slope}"},
        {"label": "Above 20 / 50 / 200",
         "value": f"{yn(ma.get('above_20'))} / {yn(ma.get('above_50'))} / {yn(ma.get('above_200'))}", "sub": ""},
        {"label": "52-week position",
         "value": (f"{round(pos*100)}%" if pos is not None else "-"),
         "sub": f"lo {_f(p.get('low_52w'))} / hi {_f(p.get('high_52w'))}"},
        {"label": "Dist 52w hi / lo",
         "value": f"{_f(p.get('dist_from_52w_high_pct'),'%')} / {_f(p.get('dist_from_52w_low_pct'),'%')}", "sub": ""},
        {"label": "Daily ATR",
         "value": _f(p.get("atr_daily")), "sub": _f(p.get("atr_pct"), "% of price")},
        {"label": "Base (vol contraction)",
         "value": _f(p.get("vol_contraction")), "sub": "<1 = tightening"},
        {"label": "Accum / Distrib",
         "value": _f(p.get("accum_dist")), "sub": ">1 = accumulation"},
        {"label": "Pullback EMA20 / 50",
         "value": f"{_f(p.get('pullback_to_ema20_pct'),'%')} / {_f(p.get('pullback_to_ema50_pct'),'%')}", "sub": ""},
        {"label": "Momentum 1m / 3m / 6m",
         "value": f"{_f(p.get('ret_1m'),'%')} / {_f(p.get('ret_3m'),'%')} / {_f(p.get('ret_6m'),'%')}", "sub": ""},
        {"label": "Relative strength",
         "value": (f"{p.get('rs_percentile')} %ile" if p.get("rs_percentile") is not None else "-"),
         "sub": "vs universe (3m)"},
        {"label": "Analyst target / MBP",
         "value": _f(p.get("analyst_target")), "sub": "from MATP"},
        {"label": "Next earnings", "value": _f(p.get("next_earnings")), "sub": ""},
    ]
