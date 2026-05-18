#!/usr/bin/env python
"""Intraday realtime monitor — the dispatcher.

Three modes, ONE brain. All modes load the watchlist + per-ticker
profiles + state, then call the strategy's evaluate_setup() function
on every 1-min bar. The mode only changes (a) where bars come from
and (b) what happens on a YES decision.

  --mode replay --date YYYY-MM-DD
        Pull historical 1m bars from Alpaca REST for the given date.
        Walk bars in chronological order (across all watchlist tickers,
        time-sorted). On each bar, call brain. On YES, simulate a bracket
        order outcome by walking subsequent bars for that symbol until
        stop or target is hit, or end-of-day exit. No orders submitted.

  --mode dry-run
        Subscribe to live Alpaca IEX WebSocket bars for the watchlist.
        Call brain on each bar. On YES, log the decision but DO NOT submit
        any orders. Useful for watching the brain in real conditions
        without paper-account fills.

  --mode live
        Same as dry-run but on YES, submit a bracket order via
        alpaca-trader-paper/scripts/orders.py subprocess. The broker
        enforces TP/SL after entry — so even if this script crashes
        after a fill, exits are still honored. Paper-only (the sibling
        skill refuses live endpoint by design).

All three modes write decisions to runs/<date>_<mode>.jsonl. The replay
output additionally surfaces per-ticker stats (trades / win% / avg R)
so the user can identify which names the strategy has edge on.

Usage:
    py scripts/monitor.py --mode replay --date 2026-05-15
    py scripts/monitor.py --mode dry-run
    py scripts/monitor.py --mode live
    py scripts/monitor.py --mode replay --date 2026-05-15 --tickers NVDA,AMD
    py scripts/monitor.py --mode replay --since 2026-04-18  # replay a range
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
PROFILES_DIR = SKILL_DIR / "profiles"
RUNS_DIR = SKILL_DIR / "runs"
WORKTREE_ROOT = SKILL_DIR.parent
BRIEF_SNAPSHOTS = WORKTREE_ROOT / "intraday-premarket-brief" / "snapshots"
ALPACA_SCRIPTS = WORKTREE_ROOT / "alpaca-trader-paper" / "scripts"
ORDERS_SCRIPT = ALPACA_SCRIPTS / "orders.py"

sys.path.insert(0, str(SCRIPT_DIR))
from _envpath import env_path

ET = ZoneInfo("America/New_York")

RTH_START = time(9, 30)
RTH_END = time(16, 0)
PREMKT_START = time(4, 0)

# Strategy registry — extend when adding new strategies.
def _load_strategy(name: str):
    from strategies import gap_and_go
    return {"gap_and_go": gap_and_go}[name]


# ============================================================
#  State — the dispatcher hands this to the brain on every bar
# ============================================================

class State:
    """Mutable run state. Shared by all modes; brain calls accessors."""

    def __init__(self, watchlist, equity, config):
        self.watchlist = watchlist
        self.equity = float(equity)

        # Config (all overridable via .env or CLI)
        self.max_trades_per_day = config["max_trades_per_day"]
        self.max_concurrent_positions = config["max_concurrent_positions"]
        self.daily_loss_pct_limit = config["daily_loss_pct_limit"]
        self.risk_pct_per_trade = config["risk_pct_per_trade"]

        # Mutable per-day
        self._premkt_high: dict[str, float] = {}
        self._premkt_low: dict[str, float] = {}
        self._open_positions: set[str] = set()
        self._rth_bars_seen: dict[str, int] = defaultdict(int)
        self.day_pnl_pct = 0.0
        self.trade_count_today = 0
        self.minutes_to_close = 9999  # updated each RTH bar by dispatcher

    # ---- Accessors (called by brain) ----
    def premkt_high(self, sym: str):
        return self._premkt_high.get(sym)

    def premkt_low(self, sym: str):
        return self._premkt_low.get(sym)

    def in_position(self, sym: str) -> bool:
        return sym in self._open_positions

    def open_position_count(self) -> int:
        return len(self._open_positions)

    def bars_in_session_so_far(self, sym: str) -> int:
        return self._rth_bars_seen[sym]

    # ---- Mutators (called by dispatcher) ----
    def observe_premkt_bar(self, sym: str, high: float, low: float):
        prev_h = self._premkt_high.get(sym)
        prev_l = self._premkt_low.get(sym)
        self._premkt_high[sym] = max(prev_h, high) if prev_h is not None else high
        self._premkt_low[sym] = min(prev_l, low) if prev_l is not None else low

    def observe_rth_bar(self, sym: str, ts_et: datetime):
        self._rth_bars_seen[sym] += 1
        end = datetime.combine(ts_et.date(), RTH_END, tzinfo=ET)
        delta_minutes = (end - ts_et).total_seconds() / 60
        self.minutes_to_close = max(0, int(delta_minutes))

    def open_position(self, sym: str):
        self._open_positions.add(sym)
        self.trade_count_today += 1

    def close_position(self, sym: str, r_realized: float):
        self._open_positions.discard(sym)
        # Crude: assume each trade risks `risk_pct_per_trade`% of equity.
        # P&L = R-multiple × risk%, so day_pnl_pct moves by that amount.
        self.day_pnl_pct += r_realized * self.risk_pct_per_trade


# ============================================================
#  Profile + watchlist loading
# ============================================================

def load_profiles(tickers: list[str]) -> dict[str, dict]:
    profiles: dict[str, dict] = {}
    missing: list[str] = []
    for t in tickers:
        path = PROFILES_DIR / f"{t}.json"
        if not path.exists():
            missing.append(t)
            continue
        try:
            profiles[t] = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            missing.append(t)
    if missing:
        sys.stderr.write(
            f"WARN: missing/invalid profiles for {len(missing)} tickers: "
            f"{', '.join(missing)}\n"
            f"  Run: py scripts/profile_builder.py --tickers {','.join(missing)}\n"
        )
    return profiles


def load_watchlist(args) -> list[str]:
    if args.tickers:
        return [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    # From snapshot
    if not BRIEF_SNAPSHOTS.exists():
        sys.exit(
            f"ERROR: brief snapshots not found at {BRIEF_SNAPSHOTS}. "
            "Pass --tickers or run intraday-premarket-brief first."
        )
    # Prefer today's T-30; fall back to today's T-60; fall back to most recent.
    today = date.today().isoformat()
    candidates = [
        BRIEF_SNAPSHOTS / f"{today}_t30.json",
        BRIEF_SNAPSHOTS / f"{today}_t60.json",
    ]
    candidates.extend(sorted(BRIEF_SNAPSHOTS.glob("*.json"), reverse=True)[:3])
    for path in candidates:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        sections = data.get("sections", {})
        early = [r["ticker"] for r in sections.get("early_gappers", [])]
        faders = [r["ticker"] for r in sections.get("faders", [])]
        wl = list(dict.fromkeys(early + faders))
        if wl:
            sys.stderr.write(f"Watchlist from {path.name}: {len(wl)} tickers\n")
            return wl
    sys.exit("ERROR: no usable brief snapshot found.")


# ============================================================
#  Decision log
# ============================================================

class DecisionLog:
    def __init__(self, mode: str, run_date: date):
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        self.path = RUNS_DIR / f"{run_date.isoformat()}_{mode}.jsonl"
        self.fh = self.path.open("a", encoding="utf-8")

    def write(self, event: dict):
        event.setdefault("ts_utc", datetime.now(timezone.utc).isoformat())
        self.fh.write(json.dumps(event, default=str) + "\n")
        self.fh.flush()

    def close(self):
        self.fh.close()


# ============================================================
#  Order submission (LIVE mode)
# ============================================================

def submit_bracket_order(decision: dict, dry: bool = False) -> dict:
    """Subproc to alpaca-trader-paper/scripts/orders.py bracket."""
    cmd = [
        sys.executable, str(ORDERS_SCRIPT), "bracket",
        decision["symbol"],
        "--qty", str(decision["qty"]),
        "--side", decision["side"],
        "--take-profit", str(decision["take_profit"]),
        "--stop-loss", str(decision["stop_loss"]),
    ]
    if dry:
        cmd.append("--dry-run")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return {"ok": False, "error": f"subprocess: {exc}"}
    return {
        "ok": result.returncode == 0,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "returncode": result.returncode,
    }


# ============================================================
#  Telegram (optional)
# ============================================================

TG_MAX = 4000


def load_env_dict() -> dict[str, str]:
    p = env_path(SKILL_DIR, "intraday-realtime")
    if not p.exists():
        return {}
    out: dict[str, str] = {}
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def telegram_send(token: str, chat_id: str, text: str):
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text[:TG_MAX],
        "parse_mode": "HTML", "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            json.loads(resp.read())
    except Exception as exc:
        sys.stderr.write(f"telegram send failed: {exc}\n")


# ============================================================
#  Replay mode
# ============================================================

def fetch_replay_bars(tickers, start_dt, end_dt, alpaca_client):
    """1-min bars for a date range from Alpaca paper IEX."""
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    req = StockBarsRequest(
        symbol_or_symbols=tickers,
        timeframe=TimeFrame(1, TimeFrameUnit.Minute),
        start=start_dt,
        end=end_dt,
        feed="iex",
    )
    resp = alpaca_client.get_stock_bars(req)
    return resp.data  # dict[symbol] -> list[Bar]


def simulate_bracket(bars_after_signal, entry_price, stop, target):
    """Walk forward from the signal bar's NEXT bar; return (exit_price,
    outcome, r_multiple, bars_held, exit_ts)."""
    risk = entry_price - stop  # long-only for v0.1
    for b in bars_after_signal:
        # Stop hit first if both hit in same bar (conservative)
        if b.low <= stop:
            return float(stop), "stop", -1.0, _bars_count(b, bars_after_signal), b.timestamp
        if b.high >= target:
            r = (target - entry_price) / risk
            return float(target), "target", r, _bars_count(b, bars_after_signal), b.timestamp
    # Neither hit — exit at last bar's close (EOD)
    if not bars_after_signal:
        return float(entry_price), "eod_no_bars", 0.0, 0, None
    last = bars_after_signal[-1]
    exit_p = float(last.close)
    r = (exit_p - entry_price) / risk
    return exit_p, "eod", r, len(bars_after_signal), last.timestamp


def _bars_count(target_bar, bars_list):
    for i, b in enumerate(bars_list):
        if b.timestamp == target_bar.timestamp:
            return i + 1
    return len(bars_list)


def run_replay(args, watchlist, profiles, state, decision_log):
    print(f"Replay mode for {len(watchlist)} tickers on {args.date}", file=sys.stderr)

    # Set up Alpaca client (reuses alpaca-trader-paper credentials)
    sys.path.insert(0, str(ALPACA_SCRIPTS))
    try:
        from _client import market_data_client
    except ImportError as e:
        sys.exit(f"Cannot import alpaca-trader-paper client: {e}")
    client = market_data_client()

    replay_dt = datetime.fromisoformat(args.date)
    # Pull bars from 04:00 ET that day to 16:30 ET (covers premkt + RTH + a buffer)
    start_dt = datetime.combine(replay_dt.date(), time(3, 30), tzinfo=ET).astimezone(timezone.utc)
    end_dt = datetime.combine(replay_dt.date(), time(20, 0), tzinfo=ET).astimezone(timezone.utc)

    bars_by_sym = fetch_replay_bars(watchlist, start_dt, end_dt, client)

    # Flatten + sort all bars chronologically. Track each symbol's bar list
    # separately too, for fill simulation walking.
    sym_bars: dict[str, list] = {}
    flat: list[tuple] = []
    for sym in watchlist:
        bars = bars_by_sym.get(sym, [])
        sym_bars[sym] = bars
        for b in bars:
            flat.append((b.timestamp, sym, b))
    flat.sort(key=lambda x: x[0])

    if not flat:
        sys.exit("ERROR: no bars returned. Was this a trading day?")

    print(f"Loaded {len(flat)} total bars across {len(watchlist)} tickers", file=sys.stderr)

    # Walk bars; track open positions and their simulated brackets.
    pending: dict[str, dict] = {}  # symbol -> bracket info
    trades: list[dict] = []

    for ts, sym, bar in flat:
        ts_et = ts.astimezone(ET)
        t = ts_et.time()

        # Premkt: update state, don't trade
        if t < RTH_START:
            state.observe_premkt_bar(sym, float(bar.high), float(bar.low))
            continue
        if t >= RTH_END:
            continue

        state.observe_rth_bar(sym, ts_et)

        # If we have an open simulated bracket for this symbol, check exit
        # using the high/low range of THIS bar.
        if sym in pending:
            br = pending[sym]
            if float(bar.low) <= br["stop"]:
                trade = br["trade"]
                trade["exit_price"] = br["stop"]
                trade["exit_ts"] = ts.isoformat()
                trade["outcome"] = "stop"
                trade["r_multiple"] = -1.0
                state.close_position(sym, -1.0)
                decision_log.write({"event": "exit", **trade})
                del pending[sym]
            elif float(bar.high) >= br["target"]:
                trade = br["trade"]
                risk = trade["entry_price"] - trade["stop"]
                r = (br["target"] - trade["entry_price"]) / risk if risk else 0
                trade["exit_price"] = br["target"]
                trade["exit_ts"] = ts.isoformat()
                trade["outcome"] = "target"
                trade["r_multiple"] = round(r, 3)
                state.close_position(sym, r)
                decision_log.write({"event": "exit", **trade})
                del pending[sym]
            continue  # don't evaluate new setups while in position

        profile = profiles.get(sym)
        if not profile:
            continue

        decision = _call_brain(args.strategy, bar, profile, state)
        if not decision:
            continue

        # Simulated fill at signal-bar close
        entry_price = float(bar.close)
        trade = {
            "symbol": sym,
            "strategy": decision["strategy"],
            "entry_ts": ts.isoformat(),
            "entry_price": entry_price,
            "stop": decision["stop_loss"],
            "target": decision["take_profit"],
            "qty": decision["qty"],
            "risk_per_share": decision["risk_per_share"],
            "reason_codes": decision["reason_codes"],
            "exit_price": None,
            "exit_ts": None,
            "outcome": None,
            "r_multiple": None,
        }
        pending[sym] = {"trade": trade, "stop": decision["stop_loss"],
                         "target": decision["take_profit"]}
        trades.append(trade)
        state.open_position(sym)
        decision_log.write({"event": "entry", **trade})

    # EOD: close any still-open positions at last bar's close
    for sym, br in pending.items():
        last = sym_bars[sym][-1] if sym_bars.get(sym) else None
        if not last:
            continue
        trade = br["trade"]
        exit_p = float(last.close)
        risk = trade["entry_price"] - trade["stop"]
        r = (exit_p - trade["entry_price"]) / risk if risk else 0
        trade["exit_price"] = exit_p
        trade["exit_ts"] = last.timestamp.isoformat()
        trade["outcome"] = "eod"
        trade["r_multiple"] = round(r, 3)
        state.close_position(sym, r)
        decision_log.write({"event": "exit_eod", **trade})

    print_per_ticker_stats(trades)
    return trades


def _call_brain(strategy_name: str, bar, profile, state):
    mod = _load_strategy(strategy_name)
    return mod.evaluate_setup(bar, profile, state)


def print_per_ticker_stats(trades: list[dict]):
    by_sym: dict[str, list] = defaultdict(list)
    for t in trades:
        by_sym[t["symbol"]].append(t)

    if not trades:
        print("\nNo trades taken.", file=sys.stderr)
        return

    print("\n" + "=" * 64, file=sys.stderr)
    print(f"{'Ticker':<8} {'Trades':>7} {'Win%':>6} {'Avg R':>7} {'Total R':>9}",
          file=sys.stderr)
    print("-" * 64, file=sys.stderr)

    total_r = 0.0
    total_trades = 0
    wins_total = 0

    for sym, group in sorted(by_sym.items(), key=lambda kv: -sum(t["r_multiple"] or 0 for t in kv[1])):
        completed = [t for t in group if t["r_multiple"] is not None]
        if not completed:
            continue
        wins = sum(1 for t in completed if t["r_multiple"] > 0)
        win_pct = wins / len(completed) * 100
        total_R = sum(t["r_multiple"] for t in completed)
        avg_R = total_R / len(completed)
        total_r += total_R
        total_trades += len(completed)
        wins_total += wins
        print(f"{sym:<8} {len(completed):>7} {win_pct:>5.0f}% {avg_R:>+6.2f}R {total_R:>+8.2f}R",
              file=sys.stderr)

    print("-" * 64, file=sys.stderr)
    overall_win = (wins_total / total_trades * 100) if total_trades else 0
    avg_R_overall = (total_r / total_trades) if total_trades else 0
    print(f"{'ALL':<8} {total_trades:>7} {overall_win:>5.0f}% "
          f"{avg_R_overall:>+6.2f}R {total_r:>+8.2f}R", file=sys.stderr)
    print("=" * 64, file=sys.stderr)


# ============================================================
#  Live + dry-run mode (async stream)
# ============================================================

async def run_live(args, watchlist, profiles, state, decision_log, telegram):
    sys.path.insert(0, str(ALPACA_SCRIPTS))
    try:
        from _client import load_credentials
    except ImportError as e:
        sys.exit(f"Cannot import alpaca-trader-paper client: {e}")
    key, secret = load_credentials()

    from alpaca.data.live import StockDataStream
    stream = StockDataStream(key, secret, feed="iex")

    async def on_bar(bar):
        sym = bar.symbol
        ts_et = bar.timestamp.astimezone(ET)
        t = ts_et.time()

        if t < RTH_START:
            state.observe_premkt_bar(sym, float(bar.high), float(bar.low))
            return
        if t >= RTH_END:
            return

        state.observe_rth_bar(sym, ts_et)
        profile = profiles.get(sym)
        if not profile:
            decision_log.write({"event": "no_profile", "symbol": sym, "ts": ts_et.isoformat()})
            return

        decision = _call_brain(args.strategy, bar, profile, state)
        if not decision:
            return  # silent on non-trade bars to keep the log readable

        decision_log.write({"event": "decision", **decision, "ts": ts_et.isoformat()})

        if args.mode == "live":
            result = submit_bracket_order(decision, dry=False)
            decision_log.write({"event": "order_result", "symbol": sym, **result})
            if result["ok"]:
                state.open_position(sym)
            if telegram:
                telegram_send(*telegram, _fmt_entry_msg(decision, "LIVE"))
        else:  # dry-run
            decision_log.write({"event": "would_order_dry_run", **decision})
            state.open_position(sym)
            if telegram:
                telegram_send(*telegram, _fmt_entry_msg(decision, "DRY-RUN"))

    stream.subscribe_bars(on_bar, *watchlist)

    # Stop signal — Ctrl+C cleanly stops the stream
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    def _stop(*_):
        sys.stderr.write("\nStopping stream...\n")
        stop_event.set()
    if hasattr(signal, "SIGINT"):
        try:
            loop.add_signal_handler(signal.SIGINT, _stop)
        except NotImplementedError:
            pass  # Windows doesn't support add_signal_handler

    # Run stream in background, await stop signal
    stream_task = asyncio.create_task(_run_stream(stream))
    await stop_event.wait()
    stream_task.cancel()
    try:
        await stream_task
    except asyncio.CancelledError:
        pass


async def _run_stream(stream):
    # The Alpaca StockDataStream.run() is sync; we call _run_forever from an
    # executor so it doesn't block the event loop.
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, stream.run)


def _fmt_entry_msg(decision: dict, label: str) -> str:
    return (
        f"<b>[{label}] {decision['symbol']} {decision['side'].upper()} "
        f"{decision['qty']}</b>\n"
        f"Entry: ${decision['entry_ref']:.2f}\n"
        f"Stop:  ${decision['stop_loss']:.2f} "
        f"(risk ${decision['risk_per_share']:.2f}/sh)\n"
        f"Target: ${decision['take_profit']:.2f} "
        f"({decision['r_multiple_target']:.1f}R)\n"
        f"Strategy: {decision['strategy']}\n"
        f"Codes: {', '.join(decision['reason_codes'])}"
    )


# ============================================================
#  Main
# ============================================================

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", required=True, choices=["live", "dry-run", "replay"])
    p.add_argument("--strategy", default="gap_and_go",
                   help="Strategy module name (gap_and_go is the only one in v0.1)")
    p.add_argument("--date", help="Replay date YYYY-MM-DD (required for --mode replay)")
    p.add_argument("--tickers", help="Comma-separated watchlist override")
    p.add_argument("--equity", type=float, default=None,
                   help="Account equity for sizing. Default: fetch from Alpaca (live) "
                        "or use 25000 (replay).")
    p.add_argument("--max-trades-per-day", type=int, default=5)
    p.add_argument("--max-concurrent-positions", type=int, default=3)
    p.add_argument("--daily-loss-pct-limit", type=float, default=-2.0,
                   help="Negative number; stop entering on day-PnL below this.")
    p.add_argument("--risk-pct-per-trade", type=float, default=0.5,
                   help="Risk per trade as percent of equity.")
    p.add_argument("--no-telegram", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    if args.mode == "replay" and not args.date:
        sys.exit("--date is required for --mode replay")

    watchlist = load_watchlist(args)
    profiles = load_profiles(watchlist)
    if not profiles:
        sys.exit("No profiles loaded. Run profile_builder.py first.")

    # Equity
    if args.equity is not None:
        equity = args.equity
    elif args.mode == "replay":
        equity = 25000.0
    else:
        sys.path.insert(0, str(ALPACA_SCRIPTS))
        from _client import trading_client
        try:
            tc = trading_client()
            equity = float(tc.get_account().equity)
        except Exception as exc:
            sys.exit(f"Could not fetch account equity from Alpaca: {exc}")

    config = {
        "max_trades_per_day": args.max_trades_per_day,
        "max_concurrent_positions": args.max_concurrent_positions,
        "daily_loss_pct_limit": args.daily_loss_pct_limit,
        "risk_pct_per_trade": args.risk_pct_per_trade,
    }
    state = State(watchlist, equity, config)

    # Decision log
    run_date = (datetime.fromisoformat(args.date).date()
                if args.mode == "replay" else date.today())
    decision_log = DecisionLog(args.mode, run_date)
    decision_log.write({
        "event": "run_start",
        "mode": args.mode,
        "strategy": args.strategy,
        "watchlist": watchlist,
        "equity": equity,
        "config": config,
        "date": run_date.isoformat(),
    })

    # Telegram (live + dry-run only)
    telegram = None
    if not args.no_telegram and args.mode in ("live", "dry-run"):
        env = load_env_dict()
        tok = env.get("TELEGRAM_BOT_TOKEN")
        chat = env.get("TELEGRAM_CHAT_ID")
        if tok and chat:
            telegram = (tok, chat)

    print(f"=== monitor.py: {args.mode} mode, strategy={args.strategy}, "
          f"equity=${equity:,.0f}, watchlist={watchlist}", file=sys.stderr)

    try:
        if args.mode == "replay":
            run_replay(args, watchlist, profiles, state, decision_log)
        else:
            asyncio.run(run_live(args, watchlist, profiles, state, decision_log, telegram))
    finally:
        decision_log.write({"event": "run_end"})
        decision_log.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
