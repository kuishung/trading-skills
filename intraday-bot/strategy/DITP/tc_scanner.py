"""DITP TC (Trend Continuation) — EOD Day-0 scanner.

Source: strategies-reference/DITP.md §6 Setup 4 (capture began chat 2026-05-25).

**Status: Phase 1 of the TC pipeline.** The TC framework as taught so far:

    Day 0      a qualifying event happens (P2 breakout day, or — when P1 is
               taught later — a strong P1 rebound)
    Day +1     the bot trades the follow-through that typically prints in
               the first 1-2 daily candles after the qualifying event
    Day +2     same trade window extended one more day

This file is the **Day-0 EOD scanner**: at the end of the trading day, walk
yesterday's P2 watchlist, filter to symbols whose Day-0 candle actually
broke out AND printed bullish, and write tomorrow's TC watchlist. The
premarket-validation scan (Phase 2) and the live entry pipeline (Phase 3)
are not yet built — see strategies-reference/DITP.md §6 Setup 4 "TBD"
sections for the gaps that still need user teaching before they can land.

Day-0 filters (this scanner):

  1. The P2 watchlist for the source date contains the candidate.
  2. Today's daily close is **above** the P2 candidate's `resistance`
     (which is `range_high`, the top of the mountain-consensus zone).
     This is the "breakout actually happened" check.
  3. Today's daily candle is **bullish**: close > open AND close is in
     the upper half of the daily range. Filters out wicky/barely-green
     breakouts that closed weak — those tend to fail follow-through.

Day-0 filters NOT yet applied (deferred):

  - Strong P1 rebound (alternative Day-0 qualifying event) — requires
    Setup 2 (P1) to be taught first; tracked in DITP.md §4 / §6.
  - Day +2 carryover — once a TC candidate fires on Day +1, can it carry
    over to Day +2? Spec says yes ("Day +1 and Day +2" trade window).
    Phase 1 only emits Day +1 candidates; carryover is a TODO.

Output:

    state/watchlist_tc_<target_date>.json    full per-candidate metadata
    state/watchlist_tc_<target_date>.txt     SYM\\ttier\\tresistance\\tCONF{n}
                                             (orchestrator-facing — D-tier
                                              and confluence Tier 0 omitted,
                                              matching P2 convention)

CLI:

    py strategy/DITP/tc_scanner.py
    py strategy/DITP/tc_scanner.py --source-date 2026-05-26
    py strategy/DITP/tc_scanner.py --target-date 2026-05-27
    py strategy/DITP/tc_scanner.py --no-write       # print only

All thresholds remain ticker-relative per CLAUDE.md Option-A (no absolute
dollar / percent / share counts).
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# --- TradeHunter bootstrap ---
_root = Path(__file__).resolve().parent
while _root != _root.parent and not (_root / "SKILL.md").exists():
    _root = _root.parent
SKILL_DIR = _root
for _p in [str(_root)] + [str(_root / s) for s in
        ("scripts", "resources", "strategy", "execution",
         "journal", "review", "dashboard")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)
del _root, _p
# ---

import bars_store  # noqa: E402  (resources/bars_store.py)
from market_calendar import (  # noqa: E402  (resources/market_calendar.py)
    is_trading_day,
    last_trading_day,
    next_trading_day,
)

STATE_DIR = SKILL_DIR / "state"


# ---------- date helpers ----------

def _bar_date(ts) -> date:
    """Normalize a bar's timestamp field to a `date`. Mirrors
    strategy/DITP/scanner.py::_bar_date — bars_store may yield int (unix s),
    datetime, pandas.Timestamp, or ISO string depending on source/path."""
    if isinstance(ts, date) and not isinstance(ts, datetime):
        return ts
    if isinstance(ts, datetime):
        return ts.date()
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).date()
    if hasattr(ts, "date"):
        try:
            return ts.date()
        except Exception:
            pass
    if isinstance(ts, str):
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
    raise TypeError(f"_bar_date: unsupported timestamp type {type(ts)!r}")


# ---------- P2 watchlist loading ----------

def _find_p2_watchlist(source_date_iso: str | None) -> Path | None:
    """Locate the P2 watchlist whose target date matches `source_date_iso`.

    Why "source date" — the P2 scanner runs EOD on Day -1 and writes a
    watchlist file whose name encodes Day 0 (the trading day the orchestrator
    will watch). When TC scanner runs EOD on Day 0, the file we want is the
    one stamped with Day 0 — i.e., today.

    Fallback: if the exact-date file doesn't exist (e.g., scanner skipped
    last night), fall back to the most recent watchlist_ditp_*.json. The
    fallback also covers weekend / holiday runs where today's file genuinely
    doesn't exist but the most recent trading day's does.
    """
    if source_date_iso:
        p = STATE_DIR / f"watchlist_ditp_{source_date_iso}.json"
        if p.exists():
            return p
        # explicit date not found → don't silently fall back; surface it
        return None
    # No explicit source — find the most recent on disk.
    candidates = sorted(
        STATE_DIR.glob("watchlist_ditp_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _load_p2_watchlist(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        sys.stderr.write(f"[tc_scanner] failed to read {path.name}: {exc}\n")
        return None


# ---------- TC candidate ----------

@dataclass
class TCCandidate:
    """One symbol that qualified as a Day-0 TC candidate.

    Carries enough of the originating P2 metadata that downstream consumers
    (impl.evaluate, dashboard, backtest adapter) don't need to re-read the
    P2 watchlist file.
    """
    symbol: str
    source_event: str               # "p2_breakout" today; "p1_rebound" once P1 is taught
    source_date: str                # ISO date of the Day-0 candle that qualified
    target_date: str                # ISO date the watchlist is targeted at (Day +1)

    # Day-0 breakout-day facts (the bullish-close-above-resistance evidence)
    day0_close: float
    day0_open: float
    day0_high: float
    day0_low: float
    day0_close_position: float      # (close - low) / (high - low), 0..1 — upper-half check
    breakout_strength_atr: float    # (close - resistance) / atr14 — how cleanly the close cleared R

    # Inherited from the originating P2 candidate (read-only references)
    p2_variant: str                 # "A" / "B" / "C"
    p2_tier: str                    # "A" / "B" / "C" / "D" (D dropped from .txt)
    p2_score: int
    resistance: float               # = P2.range_high, the level the breakout cleared
    resistance_low: float
    confluence_tier: int            # 0/1/2/3 — Tier 0 dropped from .txt
    confluence_reasons: list[str]
    cautions: list[str]
    atr14: float
    ema20: float
    ema50: float
    ema200: float
    universes: list[str]

    # Prior-day key levels carried forward — for the dashboard chart panel,
    # the polarity-flip targets (E/F) and yesterday's support (D) are still
    # meaningful intraday references on Day +1.
    yesterday_high: float = 0.0
    yesterday_low: float = 0.0
    yesterday_close: float = 0.0


# ---------- Day-0 filters ----------

def _is_bullish_day0(open_: float, high: float, low: float, close: float
                     ) -> tuple[bool, float]:
    """User rule (DITP.md §6 Setup 4, captured 2026-05-25):
    *"Day 0 daily candle must be a bullish formation."*
    Working definition: close > open AND close in upper half of daily range.
    Filters out the "barely green with a long upper tail" candles that
    typically fail follow-through.

    Returns (is_bullish, close_position) where close_position is
    (close - low) / (high - low), normalized 0..1 (1 = closed AT high,
    0 = closed AT low, 0.5 = closed at midpoint).
    """
    rng = high - low
    if rng <= 0:
        # Flat doji bar — neither close > open nor any meaningful range.
        return False, 0.0
    close_pos = (close - low) / rng
    is_bullish = (close > open_) and (close_pos >= 0.5)
    return is_bullish, close_pos


def _qualify_p2_breakout(cand: dict, source_date: date
                         ) -> TCCandidate | None:
    """Apply the Phase-1 Day-0 filters to one P2 candidate. Returns a
    TCCandidate if it qualifies, None otherwise.

    Logic:
      1. Load today's daily bar from bars_store. The bar's date must match
         source_date exactly — if the parquet doesn't have a bar dated
         source_date, we can't evaluate the candidate (data lag or symbol
         delisted; skip).
      2. close > resistance (range_high) — the breakout actually happened.
      3. _is_bullish_day0 — close > open AND close in upper half of range.
    """
    sym = (cand.get("symbol") or "").upper()
    if not sym:
        return None
    try:
        bars = bars_store.load_bars(sym, timeframe="daily")
    except Exception:
        return None
    if not bars:
        return None
    # Find the bar dated source_date. Walk from the end (most recent first).
    bar = None
    for b in reversed(bars):
        try:
            if _bar_date(b["t"]) == source_date:
                bar = b
                break
        except Exception:
            continue
    if bar is None:
        return None
    o, h, l, c = float(bar["o"]), float(bar["h"]), float(bar["l"]), float(bar["c"])
    resistance = float(cand.get("resistance") or 0)
    if resistance <= 0:
        return None
    # Filter 1: close cleared resistance
    if c <= resistance:
        return None
    # Filter 2: bullish formation
    is_bull, close_pos = _is_bullish_day0(o, h, l, c)
    if not is_bull:
        return None
    atr14 = float(cand.get("atr14") or 0)
    breakout_strength_atr = (c - resistance) / atr14 if atr14 > 0 else 0.0

    return TCCandidate(
        symbol=sym,
        source_event="p2_breakout",
        source_date=source_date.isoformat(),
        target_date=next_trading_day(source_date).isoformat(),
        day0_close=round(c, 2),
        day0_open=round(o, 2),
        day0_high=round(h, 2),
        day0_low=round(l, 2),
        day0_close_position=round(close_pos, 3),
        breakout_strength_atr=round(breakout_strength_atr, 3),
        p2_variant=str(cand.get("variant") or "?"),
        p2_tier=str(cand.get("tier") or "?"),
        p2_score=int(cand.get("score") or 0),
        resistance=round(resistance, 2),
        resistance_low=float(cand.get("resistance_low") or resistance),
        confluence_tier=int(cand.get("confluence_tier") or 0),
        confluence_reasons=list(cand.get("confluence_reasons") or []),
        cautions=list(cand.get("cautions") or []),
        atr14=round(atr14, 2),
        ema20=float(cand.get("ema20") or 0),
        ema50=float(cand.get("ema50") or 0),
        ema200=float(cand.get("ema200") or 0),
        universes=list(cand.get("universes") or []),
        yesterday_high=float(cand.get("yesterday_high") or 0),
        yesterday_low=float(cand.get("yesterday_low") or 0),
        yesterday_close=float(cand.get("yesterday_close") or 0),
    )


def scan_p2_watchlist(p2_blob: dict, source_date: date) -> list[TCCandidate]:
    """Walk every candidate in a P2 watchlist JSON, applying the Day-0
    breakout-and-bullish filters. Returns the qualifying TC candidates,
    sorted by breakout_strength_atr (cleanest break first) then p2_tier."""
    out: list[TCCandidate] = []
    for cand in p2_blob.get("candidates", []) or []:
        try:
            tc = _qualify_p2_breakout(cand, source_date)
        except Exception as exc:
            sys.stderr.write(f"[tc_scanner] {cand.get('symbol')!r}: {exc}\n")
            continue
        if tc is not None:
            out.append(tc)
    tier_rank = {"A": 0, "B": 1, "C": 2, "D": 3}
    out.sort(key=lambda c: (-c.breakout_strength_atr,
                            tier_rank.get(c.p2_tier, 9),
                            -c.p2_score))
    return out


# ---------- Output ----------

def write_watchlist(candidates: list[TCCandidate], target_date_iso: str,
                    source_date_iso: str, source_file: str
                    ) -> tuple[Path, Path]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    txt_path = STATE_DIR / f"watchlist_tc_{target_date_iso}.txt"
    json_path = STATE_DIR / f"watchlist_tc_{target_date_iso}.json"

    # .txt: orchestrator-facing line per tradeable candidate. Mirrors
    # the P2 .txt format (D-tier + Tier-0 confluence dropped) so the
    # downstream entry pipeline can apply the same filter uniformly
    # across DITP setups.
    lines = [
        f"{c.symbol}\t{c.p2_tier}\tTC_{c.p2_variant}\t{c.resistance}\tCONF{c.confluence_tier}"
        for c in candidates
        if c.p2_tier != "D" and c.confluence_tier > 0
    ]
    txt_path.write_text("\n".join(lines) + ("\n" if lines else ""),
                        encoding="utf-8")

    payload = {
        "target_date": target_date_iso,
        "source_date": source_date_iso,
        "source_file": source_file,
        "scanner_run_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_candidates": len(candidates),
        "n_tradeable_after_filter": len(lines),
        "candidates": [asdict(c) for c in candidates],
    }
    json_path.write_text(json.dumps(payload, indent=2, default=str),
                         encoding="utf-8")
    return txt_path, json_path


# ---------- CLI ----------

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--source-date",
        help=("YYYY-MM-DD — the trading day whose P2 watchlist to consume. "
              "Default = use the most recent watchlist_ditp_*.json on disk."),
    )
    ap.add_argument(
        "--target-date",
        help=("YYYY-MM-DD — output file date. Default = next business day "
              "after source-date."),
    )
    ap.add_argument(
        "--no-write", action="store_true",
        help="print only; do not write state/watchlist_tc_*.{txt,json}",
    )
    args = ap.parse_args()

    p2_path = _find_p2_watchlist(args.source_date)
    if p2_path is None:
        if args.source_date:
            sys.stderr.write(
                f"[tc_scanner] no state/watchlist_ditp_{args.source_date}.json "
                f"on disk. Did the P2 scanner run for that date?\n"
            )
        else:
            sys.stderr.write(
                "[tc_scanner] no state/watchlist_ditp_*.json found on disk. "
                "Run 'py strategy/DITP/scanner.py' end-of-day to produce a "
                "P2 watchlist first.\n"
            )
        return 1

    p2_blob = _load_p2_watchlist(p2_path)
    if p2_blob is None:
        return 1
    # The P2 watchlist's target_date IS the Day-0 trading date from TC's
    # perspective (the day the orchestrator was watching for breakouts).
    # If that target lands on a holiday (legacy: P2 scanner pre-2026-05-26
    # used a Sat/Sun-only skip and could write a holiday target), walk
    # back to the last actual trading day so we read the right daily bar.
    # User rule 2026-05-26: *"when scanning for the tickers we will be
    # looking at last trading day setup, skip the holiday"*.
    p2_target = p2_blob.get("target_date") or ""
    try:
        if args.source_date:
            source_date = date.fromisoformat(args.source_date)
        else:
            parsed_target = date.fromisoformat(p2_target)
            source_date = last_trading_day(parsed_target)
            if source_date != parsed_target:
                sys.stdout.write(
                    f"# note: P2 watchlist target_date {p2_target} is not a "
                    f"trading day; walked back to last trading day "
                    f"{source_date.isoformat()}\n"
                )
    except ValueError:
        sys.stderr.write(
            f"[tc_scanner] can't parse source date "
            f"(args.source_date={args.source_date!r}, "
            f"watchlist target_date={p2_target!r})\n"
        )
        return 1

    if not is_trading_day(source_date):
        sys.stderr.write(
            f"[tc_scanner] resolved source_date={source_date.isoformat()} "
            f"is not a trading day. No Day-0 bars to evaluate; aborting.\n"
        )
        return 1

    target_date = (
        date.fromisoformat(args.target_date) if args.target_date
        else next_trading_day(source_date)
    )

    sys.stdout.write(
        f"# TC scan: source={p2_path.name} (P2 target_date={p2_target}, "
        f"resolved source_date={source_date}, output target_date={target_date})\n"
    )
    sys.stdout.flush()

    candidates = scan_p2_watchlist(p2_blob, source_date)

    if not candidates:
        sys.stdout.write(
            "# 0 TC candidates — no P2 watchlist symbol both closed > "
            "resistance AND printed a bullish Day-0 candle.\n"
        )
    else:
        n_tradeable = sum(
            1 for c in candidates
            if c.p2_tier != "D" and c.confluence_tier > 0
        )
        sys.stdout.write(
            f"# {len(candidates)} TC candidates ({n_tradeable} tradeable "
            f"after Tier-0 + D-tier filter), sorted by breakout_strength_atr:\n"
        )
        sys.stdout.write(
            f"{'SYM':<6} {'T':<2} {'V':<2} {'cnf':>3} {'p2_sc':>5} "
            f"{'res':>8} {'D0_close':>9} {'brkATR':>7} {'closPos':>7} "
            f"{'event':<12}  reasons\n"
        )
        for c in candidates:
            cnf = (c.confluence_reasons[0] if c.confluence_reasons else "-")
            cau = ",".join(c.cautions) if c.cautions else "-"
            sys.stdout.write(
                f"{c.symbol:<6} {c.p2_tier:<2} {c.p2_variant:<2} "
                f"{c.confluence_tier:>3d} {c.p2_score:>5d} "
                f"{c.resistance:>8.2f} {c.day0_close:>9.2f} "
                f"{c.breakout_strength_atr:>7.3f} {c.day0_close_position:>7.3f} "
                f"{c.source_event:<12}  {cnf} | {cau}\n"
            )

    if not args.no_write:
        txt_path, json_path = write_watchlist(
            candidates,
            target_date.isoformat(),
            source_date.isoformat(),
            p2_path.name,
        )
        rel_txt = txt_path.relative_to(SKILL_DIR)
        rel_json = json_path.relative_to(SKILL_DIR)
        sys.stdout.write(f"\n# wrote {rel_txt}\n# wrote {rel_json}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
