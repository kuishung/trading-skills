"""Pure metric computation over a list of trade dicts.

Strategy-agnostic: operates on trades regardless of which strategy
produced them. Per-bucket cuts are driven by `bucket_by` keyword (any
field in trade['candidate_meta'] OR any top-level trade key).

No I/O. The caller writes to disk.

Phase 1 metrics:
  - n_trades, n_winners, n_losers, n_eod, n_no_trigger, n_rejected
  - win_rate (winners / (winners + losers + eod))
  - expectancy_R, total_R
  - profit_factor (sum_wins_R / abs(sum_losses_R))
  - max_drawdown_R (peak-to-trough on cumulative R curve)
  - max_consecutive_losses
  - avg_winner_R, avg_loser_R
  - avg_hold_minutes
  - by_<key> cuts: drop-in same metrics computed per group

Phase 5 additions queued (not in Phase 1):
  - Sharpe ratio (needs return-per-period normalization)
  - Sortino ratio
  - Calmar ratio (annual_return / max_DD)
  - Recovery factor
  - Risk-of-ruin estimate
  - Monte-Carlo confidence intervals (separate module: backtest_mc.py)
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable


# ---- Trade categorization ----

_FINAL_EXIT_REASONS = {"TP", "SL", "SL_AMBIGUOUS", "EOD"}
_NON_TRADE_REASONS = {"no_trigger", "rejected_tradeability", "rejected_bad_R"}


def _is_trade(t: dict) -> bool:
    """A trade dict counts as a TRADE (vs. a filtered/no-trigger record) iff
    it has an exit_reason in the executed-trade set."""
    return t.get("exit_reason") in _FINAL_EXIT_REASONS


def _is_winner(t: dict) -> bool:
    r = t.get("R_multiple")
    return r is not None and r > 0


def _is_loser(t: dict) -> bool:
    r = t.get("R_multiple")
    return r is not None and r < 0


# ---- Core aggregator ----

def _core_metrics(trades: list[dict]) -> dict:
    """Compute the headline + drawdown + streak metrics for a flat trade list."""
    executed = [t for t in trades if _is_trade(t)]
    n_trades = len(executed)
    rs = [float(t["R_multiple"]) for t in executed if t.get("R_multiple") is not None]
    winners = [r for r in rs if r > 0]
    losers = [r for r in rs if r < 0]
    eod_flat = [t for t in executed if t.get("exit_reason") == "EOD"]

    n_no_trigger = sum(1 for t in trades if t.get("exit_reason") == "no_trigger")
    n_rejected   = sum(1 for t in trades if t.get("exit_reason") in
                       ("rejected_tradeability", "rejected_bad_R"))

    # Cumulative R curve + drawdown
    cumR = 0.0
    peak = 0.0
    max_dd = 0.0
    max_consec_loss = 0
    cur_consec_loss = 0
    for r in rs:
        cumR += r
        if cumR > peak:
            peak = cumR
        dd = cumR - peak
        if dd < max_dd:
            max_dd = dd
        if r < 0:
            cur_consec_loss += 1
            if cur_consec_loss > max_consec_loss:
                max_consec_loss = cur_consec_loss
        else:
            cur_consec_loss = 0

    sum_wins = sum(winners)
    sum_loss = sum(losers)  # negative
    profit_factor = (sum_wins / abs(sum_loss)) if sum_loss < 0 else (
        float("inf") if sum_wins > 0 else 0.0
    )

    holds = [t["hold_minutes"] for t in executed if t.get("hold_minutes") is not None]

    return {
        "n_trades":              n_trades,
        "n_winners":             len(winners),
        "n_losers":              len(losers),
        "n_eod_flat":            len(eod_flat),
        "n_no_trigger":          n_no_trigger,
        "n_rejected":            n_rejected,
        "win_rate":              round(len(winners) / n_trades, 4) if n_trades else 0.0,
        "expectancy_R":          round(sum(rs) / n_trades, 4) if n_trades else 0.0,
        "total_R":               round(sum(rs), 4),
        "profit_factor":         (round(profit_factor, 3)
                                  if profit_factor != float("inf") else "inf"),
        "max_drawdown_R":        round(max_dd, 4),
        "max_consecutive_losses": max_consec_loss,
        "avg_winner_R":          round(sum(winners) / len(winners), 4) if winners else 0.0,
        "avg_loser_R":           round(sum(losers)  / len(losers),  4) if losers else 0.0,
        "avg_hold_minutes":      round(sum(holds) / len(holds), 1) if holds else 0.0,
    }


# ---- Per-bucket cuts ----

def _bucket_value(trade: dict, key: str):
    """Look up `key` first in candidate_meta, then at top-level. Returns
    None if not found (caller routes to '<missing>' bucket)."""
    meta = trade.get("candidate_meta") or {}
    if key in meta:
        return meta[key]
    if key in trade:
        return trade[key]
    return None


def _bucket_label(v) -> str:
    if v is None:
        return "<missing>"
    if isinstance(v, list):
        return ",".join(sorted(str(x) for x in v)) if v else "<empty>"
    return str(v)


def _by_field(trades: list[dict], key: str) -> dict[str, dict]:
    """Group trades by trade[key] (or candidate_meta[key]) and compute core
    metrics per group. Lists are joined with commas; missing -> '<missing>'."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        v = _bucket_value(t, key)
        groups[_bucket_label(v)].append(t)
    return {label: _core_metrics(ts) for label, ts in sorted(groups.items())}


# ---- Public API ----

def compute(trades: list[dict],
            bucket_by: Iterable[str] = ("scanner_tier", "confluence_tier",
                                         "variant", "exit_reason")) -> dict:
    """Compute overall + per-bucket metrics for a trade list.

    `bucket_by` accepts any keys present in trade dicts or their
    `candidate_meta` sub-dict. Default cuts cover the universal +
    DITP-favoured dimensions; harness can extend per-strategy.

    Returns a dict with:
      - 'overall'        : the headline metrics
      - 'by_<key>'       : one entry per bucket_by key, mapping
                           group_label -> per-group metrics
      - 'exit_reason_breakdown' : flat count per exit_reason (always present)
    """
    by: dict[str, dict] = {}
    for key in bucket_by:
        by[f"by_{key}"] = _by_field(trades, key)

    # Always-on exit reason histogram (counts ALL records including filters)
    reason_counts: dict[str, int] = defaultdict(int)
    for t in trades:
        reason_counts[str(t.get("exit_reason") or "<missing>")] += 1

    return {
        "overall": _core_metrics(trades),
        "exit_reason_breakdown": dict(sorted(reason_counts.items())),
        **by,
    }


# ---- Human-readable headline for CLI ----

def headline(summary: dict) -> str:
    """Compact one-block summary suitable for sys.stdout / journal."""
    o = summary["overall"]
    rb = summary.get("exit_reason_breakdown", {})
    lines = [
        f"trades:        {o['n_trades']:>4d}  "
        f"(W {o['n_winners']} / L {o['n_losers']} / EOD {o['n_eod_flat']})",
        f"no_trigger:    {o['n_no_trigger']:>4d}    rejected: {o['n_rejected']}",
        f"win_rate:      {o['win_rate']*100:>5.1f}%",
        f"expectancy:    {o['expectancy_R']:>+6.3f} R / trade",
        f"total R:       {o['total_R']:>+6.2f}",
        f"profit factor: {o['profit_factor']}",
        f"max DD:        {o['max_drawdown_R']:>+6.2f} R    "
        f"max consec losses: {o['max_consecutive_losses']}",
        f"avg winner:    {o['avg_winner_R']:>+6.3f} R    "
        f"avg loser:     {o['avg_loser_R']:>+6.3f} R",
        f"avg hold:      {o['avg_hold_minutes']:>5.1f} min",
        f"exit reasons:  {rb}",
    ]
    return "\n".join(lines)
