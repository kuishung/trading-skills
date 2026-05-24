"""Proposal generator -- consumes stats.py output, emits structured
suggestions for strategy parameter changes.

Cold-start safe: refuses to propose anything when sample sizes are
below MIN_SAMPLE_SIZE. Better to say "insufficient data" than to
overfit to 3 trades.

This is the threshold-based v1. Pure stats, no LLM. The qualitative
"why is this strategy losing money" narrative comes later via an
optional LLM-mediated reviewer (review/llm_review.py, not built yet).

Heuristics today (each one returns a list of proposals):
  1. Strategy with high rejection rate by `pm_volume_below_min`:
     suggest lowering MIN_PM_VOLUME (if win-rate on filled trades
     is decent) or raising it (if filled trades lose).
  2. Strategy with high `not_consolidating_near_pmh` rejection:
     suggest widening `consol_band_pct`.
  3. Strategy with low win-rate but adequate sample:
     surface for human review (don't auto-propose).
  4. Strategy never fires (always rejected): flag for review.
  5. Specific tickers with negative R: surface to user's
     ticker-whitelist memory.

Output: a list of proposals, each:
  {strategy, change_type, param, current, proposed, rationale,
   evidence_sample_size, confidence}

CLI:
    py review/propose.py
    py review/propose.py --window 30d
    py review/propose.py --min-sample-size 30
    py review/propose.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# --- intraday-bot bootstrap ---
_root = Path(__file__).resolve().parent
while _root != _root.parent and not (_root / "SKILL.md").exists():
    _root = _root.parent
for _p in [str(_root)] + [str(_root / s) for s in
        ("scripts", "resources", "strategy", "execution",
         "journal", "review", "dashboard")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)
del _root, _p
# ---

import stats as stats_mod   # noqa: E402  (review/stats.py)
from _common import get_data_root  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parent.parent
# REVIEW_DIR honours cfg["data_root"] via scripts._common
REVIEW_DIR = get_data_root() / "review"

DEFAULT_MIN_SAMPLE_SIZE = 30  # below this we never propose anything


# ---------- Heuristics ----------

def _confidence(n: int, min_n: int) -> str:
    if n >= min_n * 3:
        return "high"
    if n >= min_n:
        return "medium"
    if n >= min_n // 2:
        return "low"
    return "insufficient"


def propose_from_rejection_distribution(
    stats: dict,
    *,
    min_sample_size: int,
) -> list[dict]:
    """If a single rejection reason dominates a strategy's stats, that's
    a hint that the eligibility filter is too tight (everything fails
    the same check). Surface for human review."""
    out: list[dict] = []
    for strat, reasons in stats.get("by_strategy_rejections", {}).items():
        total = sum(reasons.values())
        if total < min_sample_size:
            out.append({
                "strategy": strat,
                "change_type": "review_request",
                "rationale": (
                    f"Only {total} rejection events in window; insufficient "
                    f"data to propose anything. Need >= {min_sample_size}."),
                "evidence_sample_size": total,
                "confidence": _confidence(total, min_sample_size),
            })
            continue
        # Single dominant reason?
        top_reason, top_n = max(reasons.items(), key=lambda kv: kv[1])
        share = top_n / total
        if share > 0.6:
            out.append({
                "strategy": strat,
                "change_type": "tighten_or_relax_filter",
                "param_hint": top_reason,
                "rationale": (
                    f"{share*100:.0f}% of rejections ({top_n}/{total}) hit "
                    f"`{top_reason}`. If win-rate on filled trades is "
                    f"strong, consider relaxing this filter to admit more "
                    f"candidates. If filled-trade outcomes are poor, this "
                    f"filter is doing nothing -- consider replacing it."),
                "evidence_sample_size": total,
                "confidence": _confidence(total, min_sample_size),
            })
    return out


def propose_from_outcome_skew(
    stats: dict,
    *,
    min_sample_size: int,
) -> list[dict]:
    """If a strategy is consistently losing or never firing, surface."""
    out: list[dict] = []
    for strat, b in stats.get("by_strategy_outcomes", {}).items():
        n_decided = b.get("winners", 0) + b.get("losers", 0)
        if n_decided < min_sample_size:
            # Don't propose anything outcome-based with too few trades.
            # Just note the gap.
            n_sub = b.get("n_submitted", 0)
            if n_sub > 0 and n_decided == 0:
                out.append({
                    "strategy": strat,
                    "change_type": "data_collection_gap",
                    "rationale": (
                        f"{n_sub} entries submitted but no completed-trade "
                        f"R outcomes journaled. Check that "
                        f"exit_filled / force_closed events carry "
                        f"`filled_avg_price` or `exit_price` so R can be "
                        f"computed."),
                    "evidence_sample_size": n_sub,
                    "confidence": "n/a (data shape issue)",
                })
            continue
        wr = b.get("win_rate")
        avg_r = b.get("avg_R")
        total_r = b.get("total_R", 0.0)
        # Bad strategy: low win-rate AND negative total R
        if wr is not None and wr < 0.30 and total_r < 0:
            out.append({
                "strategy": strat,
                "change_type": "strategy_underperforming",
                "rationale": (
                    f"Win-rate {wr*100:.0f}%, avg R={avg_r:+.2f}, "
                    f"total R={total_r:+.2f} over {n_decided} decided "
                    f"trades. Strongly consider widening the eligibility "
                    f"filter (it's letting in bad setups) OR retiring "
                    f"this strategy. Human review required."),
                "evidence_sample_size": n_decided,
                "confidence": _confidence(n_decided, min_sample_size),
            })
        # Good strategy doing well: surface positively
        elif wr is not None and wr > 0.55 and total_r > 2.0:
            out.append({
                "strategy": strat,
                "change_type": "strategy_performing_well",
                "rationale": (
                    f"Win-rate {wr*100:.0f}%, avg R={avg_r:+.2f}, "
                    f"total R={total_r:+.2f} over {n_decided} decided "
                    f"trades. Consider increasing `max_concurrent` if "
                    f"the strategy is hitting its cap often."),
                "evidence_sample_size": n_decided,
                "confidence": _confidence(n_decided, min_sample_size),
            })
    return out


def propose_from_per_ticker(
    stats: dict,
    *,
    min_sample_size: int = 3,   # per-ticker we tolerate smaller samples
) -> list[dict]:
    """Tickers with consistently negative R are candidates for the
    user's no-trade list. Tickers with strong positive R are
    promotion candidates."""
    out: list[dict] = []
    for sym, b in stats.get("by_symbol", {}).items():
        rvs = b.get("r_values", [])
        if len(rvs) < min_sample_size:
            continue
        total_r = b.get("total_R", 0.0)
        avg_r = b.get("avg_R", 0.0)
        if total_r < -1.5 and avg_r < -0.5:
            out.append({
                "ticker": sym,
                "change_type": "ticker_blacklist_candidate",
                "rationale": (
                    f"{sym}: {len(rvs)} trades, avg R={avg_r:+.2f}, "
                    f"total R={total_r:+.2f}. Consider excluding from "
                    f"watchlists or scanner output."),
                "evidence_sample_size": len(rvs),
                "confidence": _confidence(len(rvs), min_sample_size * 5),
            })
        elif total_r > 3.0 and avg_r > 1.0:
            out.append({
                "ticker": sym,
                "change_type": "ticker_whitelist_candidate",
                "rationale": (
                    f"{sym}: {len(rvs)} trades, avg R={avg_r:+.2f}, "
                    f"total R={total_r:+.2f}. Strong edge; consider "
                    f"pinning to a per-ticker whitelist."),
                "evidence_sample_size": len(rvs),
                "confidence": _confidence(len(rvs), min_sample_size * 5),
            })
    return out


# ---------- Driver ----------

def build_proposals(
    *,
    window: str = "7d",
    min_sample_size: int = DEFAULT_MIN_SAMPLE_SIZE,
    strategy: str | None = None,
) -> dict:
    """Top-level call: read journal, aggregate stats, run heuristics."""
    days = stats_mod.parse_window(window)
    files = stats_mod.journal_files_in_window(days)
    events = stats_mod.read_events(files)
    s = stats_mod.aggregate(events, strategy_filter=strategy)

    proposals: list[dict] = []
    proposals += propose_from_rejection_distribution(s, min_sample_size=min_sample_size)
    proposals += propose_from_outcome_skew(s, min_sample_size=min_sample_size)
    proposals += propose_from_per_ticker(s)

    return {
        "window": window,
        "min_sample_size": min_sample_size,
        "n_events": s["n_events"],
        "n_trades_reconstructed": s["n_trades_reconstructed"],
        "n_proposals": len(proposals),
        "proposals": proposals,
    }


def render_text(report: dict) -> str:
    lines: list[str] = []
    lines.append(f"# Proposals -- window={report['window']}  "
                 f"events={report['n_events']}  "
                 f"trades={report['n_trades_reconstructed']}  "
                 f"min_n={report['min_sample_size']}")
    lines.append("")
    if not report["proposals"]:
        lines.append("(no proposals -- insufficient data or strategies are within "
                     "expected operating envelope)")
        return "\n".join(lines)
    for i, p in enumerate(report["proposals"], 1):
        target = p.get("strategy") or p.get("ticker") or "?"
        lines.append(f"## #{i}  {p['change_type']}  ({target})")
        if p.get("param_hint"):
            lines.append(f"  param hint:   {p['param_hint']}")
        if p.get("current") is not None:
            lines.append(f"  current:      {p['current']}")
        if p.get("proposed") is not None:
            lines.append(f"  proposed:     {p['proposed']}")
        lines.append(f"  confidence:   {p['confidence']}")
        lines.append(f"  sample size:  {p['evidence_sample_size']}")
        lines.append(f"  rationale:    {p['rationale']}")
        lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--window", default="7d",
                   help="Lookback window like '7d', '30d', 'all' (default 7d).")
    p.add_argument("--strategy", default=None,
                   help="Filter to one strategy name.")
    p.add_argument("--min-sample-size", type=int, default=DEFAULT_MIN_SAMPLE_SIZE,
                   help=f"Threshold below which we refuse to propose "
                        f"(default {DEFAULT_MIN_SAMPLE_SIZE}).")
    p.add_argument("--json", action="store_true",
                   help="Emit machine-readable JSON instead of text.")
    p.add_argument("--save", action="store_true",
                   help="Write a JSON snapshot to data/review/proposals_<today>.json "
                        "(in addition to text/JSON stdout).")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    report = build_proposals(
        window=args.window,
        min_sample_size=args.min_sample_size,
        strategy=args.strategy,
    )
    if args.save:
        REVIEW_DIR.mkdir(parents=True, exist_ok=True)
        today = datetime.now(timezone.utc).date().isoformat()
        snap = REVIEW_DIR / f"proposals_{today}.json"
        snap.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        sys.stderr.write(f"# snapshot -> {snap}\n")
    if args.json:
        sys.stdout.write(json.dumps(report, indent=2, default=str) + "\n")
    else:
        sys.stdout.write(render_text(report) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
