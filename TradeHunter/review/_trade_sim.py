"""Per-candidate trade simulation — bar walk + virtual bracket fill.

Strategy-agnostic: takes an adapter (review._strategy_adapter.BacktestAdapter)
and a list of primary-timeframe bars for ONE (symbol, date). Returns either
a trade dict (entry triggered, exit determined) or None (no entry / skipped).

Phase 1 simulation surface:
  - Entry: first bar where adapter.entry_signal() returns True
  - Tradeability: adapter.tradeability_ok() veto AFTER signal fires
  - Exit (in priority order, checked each bar after entry):
      1. Same-bar SL+TP -> conservative SL_AMBIGUOUS (assume stop fills first)
      2. Stop hit (low <= stop) -> SL
      3. Target hit (high >= target) -> TP
      4. EOD reached (last bar of session) -> EOD

Phase 4+ optional hooks (not in Phase 1):
  - update_trailing_stop: raise the stop after each bar
  - early_exit_check: force market-close exit on warning pattern
  - add_to_winner_check: scale into a second leg

All bar timestamps are assumed UTC. RTH window detection lives in the
caller (backtest.py); _trade_sim only sees the bars it's handed.

Edge cases (documented behaviour):
  - prev_bar is None on the first bar -> adapter must return False (its
    contract); harness moves on to bar 2.
  - Entry bar = the bar where signal fires. Entry fill price = bar's close
    (conservative — assumes we filled at the breakout-confirming print, not
    the bar's open which would be unrealistically optimistic).
  - Tradeability rejection: candidate is logged as 'rejected_tradeability'
    via a synthetic trade dict so the operator can see WHAT got vetoed
    (counts toward filter-effectiveness stats, doesn't count as a trade in
    the P&L aggregate).
  - Same-bar SL+TP: real markets resolve this in tick sequence, which we
    don't have on 3m bars. Conservative choice: assume the LOSER fills
    first. Stamped as SL_AMBIGUOUS so it can be flagged in metrics.
  - Symbol gaps up over R on session open: first bar has prev_bar=None ->
    entry_signal returns False -> NO trade. Correct behaviour — DITP P2
    is a breakout-RETEST setup, not a gap-up chase.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


# ---- Trade dict factory ----

def _new_trade_dict(adapter, candidate: dict, date_iso: str) -> dict:
    """Pre-populate the universal + candidate_meta fields of a trade dict.
    Entry/exit fields are filled in by the bar walk."""
    universal_keys = {"symbol", "atr_used"}
    # Pull symbol + atr to top level; everything else goes into candidate_meta
    meta = {k: v for k, v in candidate.items() if k not in universal_keys}
    return {
        # Universal trade fields
        "strategy":          adapter.name,
        "engine_version":    adapter.engine_version,
        "primary_timeframe": adapter.primary_timeframe,
        "symbol":            candidate["symbol"],
        "date":              date_iso,
        "atr_used":          float(candidate["atr_used"]),
        # Entry — filled by walk
        "entry_time_utc":    None,
        "entry_bar_close_et": None,
        "entry_price":       None,
        "stop_price":        None,
        "target_price":      None,
        "R_dollars":         None,
        # Exit — filled by walk
        "exit_time_utc":     None,
        "exit_price":        None,
        "exit_reason":       None,   # TP | SL | SL_AMBIGUOUS | EOD | rejected_tradeability | no_trigger
        "R_multiple":        None,
        "hold_minutes":      None,
        # Intraday excursion stats (free with the walk)
        "intraday_max_favorable_R":  None,
        "intraday_max_adverse_R":    None,
        # Strategy-specific data preserved verbatim
        "candidate_meta":    meta,
    }


# ---- Helpers ----

def _to_utc(ts: Any) -> datetime:
    """Normalize a bar timestamp to an aware UTC datetime."""
    if isinstance(ts, datetime):
        return ts.astimezone(timezone.utc) if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)
    if isinstance(ts, str):
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    if hasattr(ts, "to_pydatetime"):
        dt = ts.to_pydatetime()
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    raise TypeError(f"_to_utc: unsupported timestamp type {type(ts)!r}")


def _bar_close_et_hhmm(ts_utc: datetime) -> str:
    """Convert UTC bar close to 'HH:MM' in US/Eastern for the trade dict.
    Uses fixed UTC-5/UTC-4 offsets based on month — DITP P2 lives in US
    cash hours so we don't need a full IANA timezone for backtest display.
    For Mar-Nov use EDT (UTC-4); Nov-Mar use EST (UTC-5)."""
    # Approximate: months 3-10 = EDT, months 11-2 = EST. The boundary days
    # don't matter for backtest readability — true DST shift is the second
    # Sunday of March and the first Sunday of November.
    offset_h = -4 if 3 <= ts_utc.month <= 10 else -5
    et_hour = (ts_utc.hour + offset_h) % 24
    return f"{et_hour:02d}:{ts_utc.minute:02d}"


# ---- The simulation ----

def simulate_trade(adapter, candidate: dict, bars: list[dict],
                   date_iso: str) -> dict | None:
    """Simulate one candidate's day. Returns:
      - trade dict with exit_reason set, OR
      - trade dict with exit_reason='no_trigger' if no entry fired, OR
      - trade dict with exit_reason='rejected_tradeability' if signal fired
        but tradeability_ok said no, OR
      - None if bars is empty / insufficient (caller logs n_skipped).

    The trade dict's `exit_reason` field is the source of truth for what
    happened — metrics module routes accordingly.
    """
    if not bars or len(bars) < 2:
        return None

    trade = _new_trade_dict(adapter, candidate, date_iso)

    # ---- Phase 1: find first entry signal ----
    entry_bar_idx: int | None = None
    for i in range(1, len(bars)):
        curr = bars[i]
        prev = bars[i - 1]
        if adapter.entry_signal(candidate, curr, prev, bars[: i + 1]):
            entry_bar_idx = i
            break

    if entry_bar_idx is None:
        trade["exit_reason"] = "no_trigger"
        return trade

    entry_bar = bars[entry_bar_idx]
    entry_ts_utc = _to_utc(entry_bar["t"])
    entry_price = float(entry_bar["c"])
    stop = float(adapter.stop_price(candidate, entry_price))
    target = float(adapter.target_price(candidate, entry_price))

    # ---- Tradeability veto ----
    if not adapter.tradeability_ok(candidate, entry_price, stop, target):
        trade.update({
            "entry_time_utc":     entry_ts_utc.isoformat().replace("+00:00", "Z"),
            "entry_bar_close_et": _bar_close_et_hhmm(entry_ts_utc),
            "entry_price":        round(entry_price, 4),
            "stop_price":         round(stop, 4),
            "target_price":       round(target, 4),
            "R_dollars":          round(entry_price - stop, 4),
            "exit_reason":        "rejected_tradeability",
        })
        return trade

    R_dollars = entry_price - stop
    if R_dollars <= 0:
        # Defensive: a candidate's stop should always sit below entry.
        # If it doesn't (bad ATR / bad price data), skip with a clear marker.
        trade.update({
            "entry_time_utc":     entry_ts_utc.isoformat().replace("+00:00", "Z"),
            "entry_bar_close_et": _bar_close_et_hhmm(entry_ts_utc),
            "entry_price":        round(entry_price, 4),
            "stop_price":         round(stop, 4),
            "target_price":       round(target, 4),
            "R_dollars":          round(R_dollars, 4),
            "exit_reason":        "rejected_bad_R",
        })
        return trade

    # ---- Walk bars AFTER entry for exit ----
    max_fav = 0.0
    max_adv = 0.0
    exit_bar_idx: int | None = None
    exit_price: float = entry_price
    exit_reason: str = "EOD"

    for j in range(entry_bar_idx + 1, len(bars)):
        bar = bars[j]
        b_high = float(bar["h"])
        b_low = float(bar["l"])
        fav_R = (b_high - entry_price) / R_dollars
        adv_R = (b_low - entry_price) / R_dollars
        if fav_R > max_fav:
            max_fav = fav_R
        if adv_R < max_adv:
            max_adv = adv_R
        hit_stop = b_low <= stop
        hit_target = b_high >= target
        if hit_stop and hit_target:
            # Same-bar both-hit — conservative: stop fills first
            exit_bar_idx = j
            exit_price = stop
            exit_reason = "SL_AMBIGUOUS"
            break
        if hit_stop:
            exit_bar_idx = j
            exit_price = stop
            exit_reason = "SL"
            break
        if hit_target:
            exit_bar_idx = j
            exit_price = target
            exit_reason = "TP"
            break

    if exit_bar_idx is None:
        # Walked all remaining bars without hitting either level -> EOD exit
        exit_bar_idx = len(bars) - 1
        exit_price = float(bars[exit_bar_idx]["c"])
        exit_reason = "EOD"

    exit_ts_utc = _to_utc(bars[exit_bar_idx]["t"])
    hold_minutes = max(0, int((exit_ts_utc - entry_ts_utc).total_seconds() // 60))
    r_multiple = (exit_price - entry_price) / R_dollars

    trade.update({
        "entry_time_utc":     entry_ts_utc.isoformat().replace("+00:00", "Z"),
        "entry_bar_close_et": _bar_close_et_hhmm(entry_ts_utc),
        "entry_price":        round(entry_price, 4),
        "stop_price":         round(stop, 4),
        "target_price":       round(target, 4),
        "R_dollars":          round(R_dollars, 4),
        "exit_time_utc":      exit_ts_utc.isoformat().replace("+00:00", "Z"),
        "exit_price":         round(exit_price, 4),
        "exit_reason":        exit_reason,
        "R_multiple":         round(r_multiple, 3),
        "hold_minutes":       hold_minutes,
        "intraday_max_favorable_R": round(max_fav, 3),
        "intraday_max_adverse_R":   round(max_adv, 3),
    })
    return trade
