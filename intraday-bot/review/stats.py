"""Per-strategy + per-symbol journal statistics — the analyzer.

Reads data/journal/journal_*.jsonl across a date window and computes:
  - Event counts per strategy (started, finished, planned, submitted,
    filled, disarmed, off_skipped, shortlist_built, rejected, etc.)
  - Rejection-reason breakdown per strategy (why did candidates not fire?)
  - Per-symbol metrics (which tickers got attention)
  - For completed entries: R-multiple distribution, win rate, total R,
    BE-move rate, exit reason mix (TP / SL / forced)

This is the substrate of the enrichment program. It's pure stats — no
LLM, no proposals, no parameter changes. `propose.py` consumes its
output to suggest tuning; humans consume its output to understand what
the bot is doing.

Cold-start tolerant: works on whatever's in the journal today, even
if that's just 1 day of evaluation. Per-strategy stats just become
'n=1' rather than the function crashing.

CLI:
    py review/stats.py
    py review/stats.py --window 7d
    py review/stats.py --window 30d --strategy guns_setup1
    py review/stats.py --symbol NVDA
    py review/stats.py --verbose
    py review/stats.py --json     # emit machine-readable stats
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# --- intraday-bot bootstrap ---
_root = Path(__file__).resolve().parent
while _root != _root.parent and not (_root / "SKILL.md").exists():
    _root = _root.parent
SKILL_DIR = _root
STATE_DIR = SKILL_DIR / "state"
for _p in [str(_root)] + [str(_root / s) for s in
        ("scripts", "resources", "strategy", "execution",
         "journal", "review", "dashboard")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)
del _root, _p
# ---

# JOURNAL_DIR + REVIEW_DIR honour cfg["data_root"] via scripts._common
from _common import get_data_root  # noqa: E402
JOURNAL_DIR = get_data_root() / "journal"
REVIEW_DIR = get_data_root() / "review"


# ---------- Journal record container ----------

@dataclass
class TradeOutcome:
    """In-memory join of entry / fill / exit / closure events for one
    symbol's life cycle. Populated by walking the journal in order."""
    strategy: str
    symbol: str
    version: str | None = None
    plan_R: float | None = None        # planned take_profit_R
    entry_submitted_ts: str | None = None
    entry_stop: float | None = None
    entry_limit: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    risk_per_share: float | None = None

    filled: bool = False
    filled_qty: int = 0
    avg_fill_price: float | None = None
    fill_ts: str | None = None

    breakeven_moved: bool = False
    exit_filled: bool = False
    exit_leg: str | None = None        # "TP" | "SL" | "force_close"
    exit_price: float | None = None
    exit_ts: str | None = None
    cancelled: bool = False
    cancel_reason: str | None = None

    @property
    def r_achieved(self) -> float | None:
        """R-multiple achieved if both entry and exit are known.
        Long-only assumption (GUNS today). For short strategies, sign-flip."""
        if (self.avg_fill_price is None or self.exit_price is None
                or self.risk_per_share is None
                or self.risk_per_share <= 0):
            return None
        return (self.exit_price - self.avg_fill_price) / self.risk_per_share

    @property
    def is_winner(self) -> bool | None:
        r = self.r_achieved
        return None if r is None else r > 0


# ---------- Reader ----------

def parse_window(window: str) -> int:
    """`7d` -> 7, `30d` -> 30, `1d` -> 1. Returns -1 for `all`."""
    if window == "all":
        return -1
    if window.endswith("d"):
        try:
            return int(window[:-1])
        except ValueError:
            pass
    raise ValueError(f"--window must be like '7d', '30d', 'all'; got {window!r}")


def journal_files_in_window(days: int) -> list[Path]:
    """Find data/journal/journal_<date>.jsonl within the last `days` (UTC).
    Also includes legacy state/journal_*.jsonl files so historical
    runs from before the data/ migration stay readable.
    `days=-1` returns all journal files we have."""
    primary = sorted(JOURNAL_DIR.glob("journal_*.jsonl"))
    legacy = sorted(STATE_DIR.glob("journal_*.jsonl"))
    seen_dates: set[str] = set()
    all_files: list[Path] = []
    for p in primary + legacy:
        stem = p.stem.removeprefix("journal_")
        if stem in seen_dates:
            continue
        seen_dates.add(stem)
        all_files.append(p)
    all_files.sort(key=lambda p: p.stem)
    if days < 0:
        return all_files
    today = datetime.now(timezone.utc).date()
    cutoff = today - timedelta(days=days)
    keep: list[Path] = []
    for p in all_files:
        stem = p.stem.removeprefix("journal_")
        try:
            dt = datetime.strptime(stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if dt >= cutoff:
            keep.append(p)
    return keep


def read_events(files: list[Path]) -> list[dict]:
    """Read all events from the given JSONL files in chronological order."""
    out: list[dict] = []
    for p in files:
        for raw in p.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError:
                sys.stderr.write(f"stats: bad json line in {p.name}\n")
    out.sort(key=lambda e: e.get("ts", ""))
    return out


# ---------- Trade reconstruction ----------

def reconstruct_trades(events: list[dict]) -> list[TradeOutcome]:
    """Walk events in order, joining entry_submitted -> entry_filled ->
    breakeven_moved -> exit_filled / force_closed / entry_cancelled
    into TradeOutcome rows.

    Keyed by (strategy, symbol, entry_submitted_ts). If a strategy
    submits multiple times for the same symbol on different days,
    they get distinct rows."""
    open_trades: dict[tuple[str, str], TradeOutcome] = {}
    completed: list[TradeOutcome] = []

    for ev in events:
        et = ev.get("event")
        strat = ev.get("strategy") or "?"
        sym = ev.get("symbol")
        key = (strat, sym) if sym else None

        if et == "entry_submitted" and key:
            tr = TradeOutcome(
                strategy=strat, symbol=sym,
                version=ev.get("strategy_version") or ev.get("version"),
                entry_submitted_ts=ev.get("ts"),
                entry_stop=ev.get("entry_stop"),
                entry_limit=ev.get("entry_limit"),
                stop_loss=ev.get("stop_loss"),
                take_profit=ev.get("take_profit"),
                risk_per_share=_risk_from_event(ev),
                plan_R=_R_from_event(ev),
            )
            # If a prior trade for same symbol/strategy is still open
            # without completion, close it as orphan-stale.
            if key in open_trades:
                completed.append(open_trades.pop(key))
            open_trades[key] = tr

        elif et == "entry_filled" and key in open_trades:
            tr = open_trades[key]
            tr.filled = True
            tr.fill_ts = ev.get("ts")
            tr.filled_qty = int(ev.get("filled_qty") or ev.get("qty") or 0)
            avg = ev.get("avg_fill_price") or ev.get("avg_price")
            tr.avg_fill_price = float(avg) if avg is not None else None

        elif et == "breakeven_moved" and key in open_trades:
            open_trades[key].breakeven_moved = True

        elif et == "exit_filled" and key in open_trades:
            tr = open_trades[key]
            tr.exit_filled = True
            tr.exit_leg = ev.get("leg") or ev.get("exit_leg")
            price = ev.get("filled_avg_price") or ev.get("exit_price")
            tr.exit_price = float(price) if price is not None else None
            tr.exit_ts = ev.get("ts")
            completed.append(open_trades.pop(key))

        elif et == "force_closed" and key in open_trades:
            tr = open_trades[key]
            tr.exit_filled = True
            tr.exit_leg = "force_close"
            price = ev.get("filled_avg_price") or ev.get("exit_price")
            tr.exit_price = float(price) if price is not None else None
            tr.exit_ts = ev.get("ts")
            completed.append(open_trades.pop(key))

        elif et == "entry_cancelled" and key in open_trades:
            tr = open_trades[key]
            tr.cancelled = True
            tr.cancel_reason = ev.get("reason")
            completed.append(open_trades.pop(key))

    # Any still-open trades = pending or orphaned
    for tr in open_trades.values():
        completed.append(tr)
    return completed


def _risk_from_event(ev: dict) -> float | None:
    """Risk per share = entry_limit - stop_loss (long-only)."""
    el = ev.get("entry_limit")
    sl = ev.get("stop_loss")
    if el is not None and sl is not None:
        try:
            r = float(el) - float(sl)
            return r if r > 0 else None
        except (TypeError, ValueError):
            return None
    return None


def _R_from_event(ev: dict) -> float | None:
    """The plan-declared TP-R (planned R-multiple at order-submit time)."""
    try:
        return float(ev.get("take_profit_R")) if ev.get("take_profit_R") is not None else None
    except (TypeError, ValueError):
        return None


# ---------- Aggregation ----------

def aggregate(
    events: list[dict],
    *,
    strategy_filter: str | None = None,
    symbol_filter: str | None = None,
) -> dict[str, Any]:
    """Compute per-strategy and per-symbol summaries from a filtered
    event stream."""
    if strategy_filter:
        events = [e for e in events if e.get("strategy") == strategy_filter]
    if symbol_filter:
        events = [e for e in events if e.get("symbol") == symbol_filter]

    trades = reconstruct_trades(events)
    if symbol_filter:
        trades = [t for t in trades if t.symbol == symbol_filter]

    # ---- per-strategy event counts ----
    by_strat: dict[str, dict[str, int]] = defaultdict(Counter)
    for ev in events:
        s = ev.get("strategy") or "?"
        by_strat[s][ev.get("event", "?")] += 1

    # ---- rejection reasons per strategy ----
    reject_reasons: dict[str, Counter] = defaultdict(Counter)
    for ev in events:
        if ev.get("event") == "rejected":
            s = ev.get("strategy") or "?"
            reject_reasons[s][ev.get("reason") or "?"] += 1

    # ---- per-strategy trade outcomes ----
    by_strat_outcomes: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "n_submitted": 0, "n_filled": 0, "n_breakeven_moved": 0,
            "n_tp": 0, "n_sl": 0, "n_force_closed": 0, "n_cancelled": 0,
            "n_pending": 0,
            "winners": 0, "losers": 0,
            "r_values": [], "total_R": 0.0,
            "versions": Counter(),
        }
    )
    for tr in trades:
        bucket = by_strat_outcomes[tr.strategy]
        bucket["n_submitted"] += 1
        if tr.version:
            bucket["versions"][tr.version] += 1
        if tr.filled:
            bucket["n_filled"] += 1
        if tr.breakeven_moved:
            bucket["n_breakeven_moved"] += 1
        if tr.cancelled:
            bucket["n_cancelled"] += 1
        if tr.exit_leg == "TP":
            bucket["n_tp"] += 1
        elif tr.exit_leg == "SL":
            bucket["n_sl"] += 1
        elif tr.exit_leg == "force_close":
            bucket["n_force_closed"] += 1
        if tr.filled and not tr.exit_filled and not tr.cancelled:
            bucket["n_pending"] += 1
        r = tr.r_achieved
        if r is not None:
            bucket["r_values"].append(r)
            bucket["total_R"] += r
            if r > 0:
                bucket["winners"] += 1
            elif r < 0:
                bucket["losers"] += 1

    # Compute win_rate + avg_R from accumulated values
    for bucket in by_strat_outcomes.values():
        rvs = bucket["r_values"]
        decided = bucket["winners"] + bucket["losers"]
        bucket["win_rate"] = bucket["winners"] / decided if decided > 0 else None
        bucket["avg_R"] = sum(rvs) / len(rvs) if rvs else None
        bucket["min_R"] = min(rvs) if rvs else None
        bucket["max_R"] = max(rvs) if rvs else None

    # ---- per-symbol outcomes ----
    by_symbol: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"n_submitted": 0, "n_filled": 0, "r_values": [], "total_R": 0.0,
                 "by_strategy": Counter()}
    )
    for tr in trades:
        s = by_symbol[tr.symbol]
        s["n_submitted"] += 1
        s["by_strategy"][tr.strategy] += 1
        if tr.filled:
            s["n_filled"] += 1
        r = tr.r_achieved
        if r is not None:
            s["r_values"].append(r)
            s["total_R"] += r

    for s in by_symbol.values():
        rvs = s["r_values"]
        s["avg_R"] = sum(rvs) / len(rvs) if rvs else None

    return {
        "n_events": len(events),
        "n_trades_reconstructed": len(trades),
        "by_strategy_events": {k: dict(v) for k, v in by_strat.items()},
        "by_strategy_rejections": {k: dict(v) for k, v in reject_reasons.items()},
        "by_strategy_outcomes": {k: _serializable(v) for k, v in by_strat_outcomes.items()},
        "by_symbol": {k: _serializable(v) for k, v in by_symbol.items()},
    }


def _serializable(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, Counter):
            out[k] = dict(v)
        else:
            out[k] = v
    return out


# ---------- Rendering ----------

def render_text(stats: dict, *, verbose: bool = False) -> str:
    """Pretty-print stats for human consumption."""
    lines: list[str] = []
    lines.append(f"# Journal stats -- {stats['n_events']} events, "
                 f"{stats['n_trades_reconstructed']} trade-life-cycles reconstructed")
    lines.append("")

    # Per-strategy event counts
    lines.append("## Per-strategy event counts")
    for strat, ev_counts in sorted(stats["by_strategy_events"].items()):
        total = sum(ev_counts.values())
        lines.append(f"  {strat}  ({total} events)")
        for ev, n in sorted(ev_counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"    {n:>4}  {ev}")
    lines.append("")

    # Per-strategy outcomes
    lines.append("## Per-strategy outcomes")
    outcomes = stats["by_strategy_outcomes"]
    if not outcomes:
        lines.append("  (no trades reconstructed yet)")
    for strat, b in sorted(outcomes.items()):
        wr = b.get("win_rate")
        wr_s = f"{wr*100:.0f}%" if wr is not None else "n/a"
        avg_r = b.get("avg_R")
        avg_r_s = f"{avg_r:+.2f}" if avg_r is not None else "n/a"
        lines.append(
            f"  {strat}:  "
            f"sub={b['n_submitted']} fill={b['n_filled']} "
            f"TP={b['n_tp']} SL={b['n_sl']} forced={b['n_force_closed']} "
            f"cancel={b['n_cancelled']} pend={b['n_pending']} | "
            f"WR={wr_s} avgR={avg_r_s} totalR={b['total_R']:+.2f}"
        )
        if b["versions"]:
            lines.append(f"    versions:  {dict(b['versions'])}")
    lines.append("")

    # Per-strategy rejection reasons
    lines.append("## Per-strategy rejection reasons (why candidates didn't fire)")
    rej = stats["by_strategy_rejections"]
    if not rej:
        lines.append("  (no rejections journaled)")
    for strat, reasons in sorted(rej.items()):
        lines.append(f"  {strat}")
        for r, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
            lines.append(f"    {n:>4}  {r}")
    lines.append("")

    # Per-symbol (only if interesting)
    if verbose or stats["by_symbol"]:
        lines.append("## Per-symbol")
        if not stats["by_symbol"]:
            lines.append("  (no symbols seen)")
        for sym, b in sorted(stats["by_symbol"].items(),
                             key=lambda kv: -kv[1]["n_submitted"]):
            if not verbose and b["n_submitted"] == 0:
                continue
            avg_r = b.get("avg_R")
            avg_r_s = f"{avg_r:+.2f}" if avg_r is not None else "n/a"
            lines.append(
                f"  {sym:<8}  sub={b['n_submitted']} fill={b['n_filled']}  "
                f"avgR={avg_r_s} totalR={b['total_R']:+.2f}  "
                f"by={dict(b['by_strategy'])}"
            )

    return "\n".join(lines)


# ---------- CLI ----------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--window", default="7d",
                   help="Lookback window like '7d', '30d', 'all' (default 7d).")
    p.add_argument("--strategy", default=None,
                   help="Filter to one strategy name.")
    p.add_argument("--symbol", default=None,
                   help="Filter to one ticker.")
    p.add_argument("--verbose", action="store_true",
                   help="Show per-symbol detail even when empty.")
    p.add_argument("--json", action="store_true",
                   help="Emit machine-readable JSON instead of text.")
    p.add_argument("--save", action="store_true",
                   help="Write a JSON snapshot to data/review/stats_<today>.json "
                        "(in addition to text/JSON stdout).")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    days = parse_window(args.window)
    files = journal_files_in_window(days)
    if not files:
        sys.stdout.write(
            f"# No journal files found in window={args.window}.\n"
            f"# Looked in: {STATE_DIR}\n"
        )
        return 0
    events = read_events(files)
    stats = aggregate(events,
                      strategy_filter=args.strategy,
                      symbol_filter=args.symbol)
    if args.save:
        REVIEW_DIR.mkdir(parents=True, exist_ok=True)
        today = datetime.now(timezone.utc).date().isoformat()
        snap = REVIEW_DIR / f"stats_{today}.json"
        snap.write_text(json.dumps({
            "window": args.window,
            "files": [str(p.name) for p in files],
            "strategy_filter": args.strategy,
            "symbol_filter": args.symbol,
            "stats": stats,
        }, indent=2, default=str), encoding="utf-8")
        sys.stderr.write(f"# snapshot -> {snap}\n")
    if args.json:
        sys.stdout.write(json.dumps(stats, indent=2, default=str))
        sys.stdout.write("\n")
    else:
        header = (f"# Window: {args.window}  "
                  f"({len(files)} journal file(s), "
                  f"{files[0].name if files else '-'} -> "
                  f"{files[-1].name if files else '-'})\n")
        if args.strategy:
            header += f"# Filter: strategy={args.strategy}\n"
        if args.symbol:
            header += f"# Filter: symbol={args.symbol}\n"
        sys.stdout.write(header + "\n")
        sys.stdout.write(render_text(stats, verbose=args.verbose))
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
