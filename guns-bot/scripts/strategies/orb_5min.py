"""5-Minute Opening Range Breakout on "Stocks in Play".

Reference: Zarattini, Barbon & Aziz (2024) — A Profitable Day Trading
Strategy For The U.S. Equity Market (SSRN 4729284). Python implementation
follows Concretum Group's published backtest.

Rules:
  1. Universe = top N "Stocks in Play" from IBKR scanners (TOP_PERC_GAIN
     ∪ TOP_VOLUME_RATE ∪ HOT_BY_VOLUME, minus HALTED).
  2. At 09:35 ET, look at the first 5-min bar (09:30:00 - 09:34:59).
  3. Direction = sign of (close - open). Bullish -> LONG, bearish -> SHORT,
     neutral -> skip.
  4. Entry: stop-limit at OR high (long) or OR low (short), 1¢ slip and
     3¢ limit chase.
  5. Stop loss: opposite end of the 5-min OR.
  6. Take profit: take_profit_R * risk_per_share (default 10R per paper).
  7. Time stop: cancel unfilled at entry_cutoff_et; EOD sweep at 15:58.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Make sibling modules importable.
SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _common import get_rth_minute_bars, STATE_DIR  # noqa: E402
from _journal import journal  # noqa: E402
from signals import split_pm_rth  # noqa: E402

from .base import Strategy


# Scanners that constitute "Stocks in Play" for this strategy.
PRIMARY_SCANNERS = ("TOP_PERC_GAIN", "TOP_VOLUME_RATE", "HOT_BY_VOLUME")
HALTED_SCANNER   = "HALTED"


# ---------- pick_universe ----------

def pick_universe(date_iso: str, cfg: dict, strategy: Strategy = None) -> list[str]:
    """Read scanner.snapshot events from today's log; return up to
    strategy.params.max_symbols 'Stocks in Play'.

    Rank priority: TOP_PERC_GAIN first, then TOP_VOLUME_RATE, then
    HOT_BY_VOLUME. Dedupe across scanners. Subtract HALTED.
    """
    max_symbols = (strategy.params if strategy else {}).get("max_symbols", 20)
    log = STATE_DIR / f"events_{date_iso}.jsonl"
    if not log.exists():
        journal("orb_5min", "universe_no_log", reason=str(log))
        return []
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        journal("orb_5min", "universe_read_failed", error=str(exc))
        return []

    latest: dict[str, list[str]] = {}
    seen_codes: set[str] = set()
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") != "scanner.snapshot":
            continue
        payload = ev.get("payload") or {}
        code = payload.get("scan_code")
        if not code or code in seen_codes:
            continue
        seen_codes.add(code)
        rows = payload.get("rows") or []
        latest[code] = [r["symbol"] for r in rows if r.get("symbol")]

    halted = set(latest.get(HALTED_SCANNER, []))
    out: list[str] = []
    seen_syms: set[str] = set()
    for code in PRIMARY_SCANNERS:
        for sym in latest.get(code, []):
            if not sym or " " in sym:  # skip preferred-share / non-stock notations
                continue
            if sym in halted:
                journal("orb_5min", "rejected", symbol=sym,
                        reason="halted", scan_code=code)
                continue
            if sym in seen_syms:
                continue
            out.append(sym)
            seen_syms.add(sym)
            journal("orb_5min", "shortlisted", symbol=sym,
                    scan_code=code, rank_in_strategy=len(out))
            if len(out) >= max_symbols:
                return out
    return out


# ---------- fetch_bars ----------

def fetch_bars(symbols: list[str], cfg: dict,
               strategy: Strategy) -> dict[str, list[dict]]:
    """ORB only needs today's 1-min bars (PM + RTH up to now).
    Caller splits PM/RTH downstream."""
    return get_rth_minute_bars(symbols, cfg, None)


# ---------- evaluate ----------

def evaluate(symbol: str, bars: list[dict],
             strategy: Strategy) -> dict | None:
    """Build a long or short ORB plan from the first N 1-min RTH bars.

    Returns plan dict (shape per base.Strategy docstring) or None if:
      - fewer than orb_minutes RTH bars exist
      - the OR bar is neutral (close == open)
      - resulting risk_per_share is non-positive
    Journals every decision (shortlisted, rejected, entry_planned).
    """
    params = strategy.params
    orb_minutes      = params.get("orb_minutes", 5)
    slip_cents       = params.get("slip_cents", 1.0)
    limit_slip_cents = params.get("limit_slip_cents", 3.0)
    take_profit_R    = strategy.take_profit_R

    _, rth = split_pm_rth(bars)
    if len(rth) < orb_minutes:
        journal("orb_5min", "rejected", symbol=symbol,
                reason=f"only {len(rth)} RTH bars (need {orb_minutes})")
        return None

    or_bars = rth[:orb_minutes]
    or_open  = or_bars[0]["o"]
    or_close = or_bars[-1]["c"]
    or_high  = max(b["h"] for b in or_bars)
    or_low   = min(b["l"] for b in or_bars)

    if or_close > or_open:
        side = "long"
    elif or_close < or_open:
        side = "short"
    else:
        journal("orb_5min", "rejected", symbol=symbol,
                reason="neutral OR bar (close == open)",
                or_open=or_open, or_close=or_close)
        return None

    slip       = slip_cents / 100.0
    limit_slip = limit_slip_cents / 100.0

    if side == "long":
        entry_stop  = round(or_high + slip, 2)
        entry_limit = round(entry_stop + limit_slip, 2)
        stop_loss   = round(or_low - slip, 2)
        risk_per_share = round(entry_limit - stop_loss, 4)
        if risk_per_share <= 0:
            journal("orb_5min", "rejected", symbol=symbol,
                    reason=f"non-positive risk_per_share ({risk_per_share})")
            return None
        take_profit = round(entry_limit + take_profit_R * risk_per_share, 2)
    else:  # short
        entry_stop  = round(or_low - slip, 2)
        entry_limit = round(entry_stop - limit_slip, 2)
        stop_loss   = round(or_high + slip, 2)
        risk_per_share = round(stop_loss - entry_limit, 4)
        if risk_per_share <= 0:
            journal("orb_5min", "rejected", symbol=symbol,
                    reason=f"non-positive risk_per_share ({risk_per_share})")
            return None
        take_profit = round(entry_limit - take_profit_R * risk_per_share, 2)

    plan = {
        "strategy":            "orb_5min",
        "symbol":              symbol,
        "side":                side,
        "or_high":             round(or_high, 4),
        "or_low":              round(or_low, 4),
        "or_open":             round(or_open, 4),
        "or_close":            round(or_close, 4),
        "entry_stop_trigger":  entry_stop,
        "entry_limit":         entry_limit,
        "stop_loss":           stop_loss,
        "take_profit":         take_profit,
        "risk_per_share":      risk_per_share,
        "take_profit_R":       take_profit_R,
    }
    journal("orb_5min", "entry_planned", symbol=symbol, plan=plan)
    return plan


# ---------- factory ----------

def build(cfg: dict) -> Strategy:
    """Construct the ORB strategy from cfg.strategies.orb_5min.{}.
    Defaults match the paper's recommended values."""
    s_cfg = (cfg.get("strategies") or {}).get("orb_5min") or {}
    return Strategy(
        name="orb_5min",
        enabled=bool(s_cfg.get("enabled", False)),
        entry_et=s_cfg.get("entry_et", "09:35"),
        entry_cutoff_et=s_cfg.get("entry_cutoff_et", "15:00"),
        take_profit_R=float(s_cfg.get("take_profit_R", 10.0)),
        max_concurrent=int(s_cfg.get("max_concurrent", 3)),
        params={
            "orb_minutes":      int(s_cfg.get("orb_minutes", 5)),
            "max_symbols":      int(s_cfg.get("max_symbols", 20)),
            "slip_cents":       float(s_cfg.get("slip_cents", 1.0)),
            "limit_slip_cents": float(s_cfg.get("limit_slip_cents", 3.0)),
        },
        pick_universe=pick_universe,
        fetch_bars=fetch_bars,
        evaluate=evaluate,
    )
