"""Bot session orchestrator — the main entrypoint.

Strategy-agnostic framework. Loads enabled strategies from
config.json -> strategies.{name}, schedules each at its entry_et, and
runs a shared management loop (fill detection, breakeven moves, OCO
completion). Per-strategy max_concurrent + a global cap throttle risk.
EOD safety sweep at 15:58 ET guarantees no position rolls overnight.

No strategy logic lives here — every strategy is its own module in
scripts/strategies/. Adding a strategy = drop in <name>.py, register
in KNOWN_STRATEGIES, add a config block.

Strict rules (enforced at startup, refuse to launch if violated):
  - risk_per_trade_pct <= 1% of NLV   (global, never override)
  - max_position_pct = 10% of NLV     (global notional cap)
  - At least one strategy enabled
  - Each enabled strategy declares take_profit_R > 0 and max_concurrent > 0

Paper-only. Uses alpaca-py directly for execution to allow the multi-step
flow (stop-limit entry then OCO attach on fill) that the alpaca-trader-paper
CLI doesn't expose. The paper-only guard is re-checked at construction time.

CLI:
    py scripts/trade_day.py
    py scripts/trade_day.py --dry-run        # log only, no orders
    py scripts/trade_day.py --fake-now 09:35 # advance to a wall-clock for testing
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --- intraday-bot bootstrap: make sibling layers importable ---
_root = Path(__file__).resolve().parent
while _root != _root.parent and not (_root / "SKILL.md").exists():
    _root = _root.parent
# Layer folders on sys.path so `from base import Strategy` works directly.
# Plus the intraday-bot root so `from strategy import KNOWN_STRATEGIES` works
# as a package import (uses strategy/__init__.py).
for _p in [str(_root)] + [str(_root / s) for s in
        ("scripts", "resources", "strategy", "execution", "journal", "review", "dashboard")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)
del _root, _p
# ---

from _common import (  # noqa: E402
    STATE_DIR, append_fill_event, equity_path, et_at, et_now,
    et_today_iso, fallback_state, fmt_price, get_latest_trade,
    load_config, safe_log_stdout,
    send_telegram, sleep_until, trading_client,
)
from signals import position_size  # noqa: E402
from writer import journal, summarize_by_strategy  # noqa: E402  (journal/writer.py)
from _gating import is_armed as _is_strategy_armed  # noqa: E402
from _gating import is_enabled as _is_strategy_enabled  # noqa: E402
from strategy import Strategy, load_known  # noqa: E402  (strategy/__init__.py)


# ---------- Strict risk rules (enforced at startup) ----------

# Hard limits the live bot refuses to violate. To loosen these, edit this
# block deliberately — the friction is the point.
STRICT_MAX_RISK_PCT = 0.01   # global: at most 1% of NLV risked per trade


def _validate_strict_rules(cfg: dict, strategies: list[Strategy]) -> None:
    """Refuse to start if config violates the strict risk rules.

    Strict rules (per the bot's design contract):
      - Global risk_per_trade_pct must be in (0, STRICT_MAX_RISK_PCT]
        — protects NLV: never risk more than 1% per trade.
      - Each wired strategy must declare a positive take_profit_R and
        a positive max_concurrent (strategy-specific R values are allowed
        but must be deliberate, not missing or zero).
      - At least one wired strategy (config block present + module
        importable) must exist; an empty registry is a wiring bug.

    NOTE: this no longer requires any strategy to be ON. ON/OFF is a
    LIVE runtime gate (state/enabled_<name>.flag) -- the bot is allowed
    to start with every strategy OFF; the orchestrator will just journal
    strategy_off_skipped for each scheduled fire.
    """
    rp = cfg.get("risk_per_trade_pct")
    if rp is None or not (0.0 < rp <= STRICT_MAX_RISK_PCT):
        sys.exit(
            f"STRICT RULE VIOLATION: risk_per_trade_pct={rp} must be in "
            f"(0, {STRICT_MAX_RISK_PCT}] (<= 1% of NLV per trade). "
            f"Edit config.json to comply."
        )
    if not strategies:
        sys.exit(
            "STRICT RULE VIOLATION: no strategies wired. "
            "Add a module under scripts/strategies/ and list it in "
            "KNOWN_STRATEGIES (with a config block in cfg.strategies)."
        )
    for s in strategies:
        if not (s.take_profit_R > 0):
            sys.exit(
                f"STRICT RULE VIOLATION: strategy {s.name!r} take_profit_R="
                f"{s.take_profit_R} must be > 0. Edit config.json to comply."
            )
        if s.max_concurrent <= 0:
            sys.exit(
                f"STRICT RULE VIOLATION: strategy {s.name!r} max_concurrent="
                f"{s.max_concurrent} must be > 0."
            )
    on_now = [s.name for s in strategies if _is_strategy_enabled(s.name)]
    armed_now = [s.name for s in strategies if _is_strategy_armed(s.name)]
    safe_log_stdout(
        f"strict rules OK: risk per trade <= {rp:.2%} of NLV, "
        f"{len(strategies)} strateg{'y' if len(strategies)==1 else 'ies'} wired "
        f"({len(on_now)} ON, {len(armed_now)} ARMED): "
        + ", ".join(f"{s.name}(R={s.take_profit_R},cap={s.max_concurrent})"
                    for s in strategies)
    )


# ---------- Trade record ----------

@dataclass
class TradeRecord:
    symbol: str
    strategy_name: str                     # which strategy opened this trade
    plan: dict
    qty: int
    entry_order_id: str | None = None
    exit_order_id: str | None = None       # OCO parent id
    take_profit_leg_id: str | None = None  # remembered for breakeven swap
    stop_leg_id: str | None = None
    filled_qty: int = 0
    avg_fill_price: float | None = None
    breakeven_moved: bool = False
    closed: bool = False
    realized_pnl: float | None = None      # filled exit price * qty - entry cost
    realized_R: float | None = None        # signed R-multiple at exit
    notes: list[str] = field(default_factory=list)


# ---------- Alpaca order helpers ----------

def get_account_equity(cfg) -> float:
    tc = trading_client(cfg)
    acct = tc.get_account()
    return float(acct.equity)


def submit_setup_entry(cfg, plan_row: dict, qty: int, dry_run: bool) -> str | None:
    """Submit a stop-limit entry. Direction comes from plan_row['side']
    ('long' = BUY stop-limit, 'short' = SELL stop-limit). Returns Alpaca
    order id, or None for dry-run."""
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import StopLimitOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce

    sym = plan_row["symbol"]
    stop = plan_row["entry_stop_trigger"]
    limit = plan_row["entry_limit"]
    side_str = plan_row.get("side", "long")
    side = OrderSide.BUY if side_str == "long" else OrderSide.SELL
    side_label = "buy-stop-limit" if side_str == "long" else "sell-stop-limit (SHORT)"

    if dry_run:
        safe_log_stdout(f"[DRY] would submit {side_label} {sym} qty={qty} "
                        f"stop={stop} limit={limit}")
        return None
    tc = trading_client(cfg)
    req = StopLimitOrderRequest(
        symbol=sym, qty=qty,
        side=side, time_in_force=TimeInForce.DAY,
        stop_price=stop, limit_price=limit,
    )
    o = tc.submit_order(req)
    return str(o.id)


def submit_oco_exit(cfg, sym: str, qty: int, take_profit: float,
                    stop_loss: float, dry_run: bool,
                    position_side: str = "long") -> tuple[str | None, str | None, str | None]:
    """Submit OCO bracket to close the position. For a LONG position the
    bracket is SELL (TP > entry, SL < entry); for a SHORT position the
    bracket is BUY (TP < entry, SL > entry).

    Returns (parent_id, tp_leg_id, sl_leg_id). For dry-run all None.
    """
    from alpaca.trading.requests import LimitOrderRequest, TakeProfitRequest, StopLossRequest
    from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce

    if dry_run:
        safe_log_stdout(f"[DRY] would submit OCO close-{position_side} {sym} qty={qty} "
                        f"tp={take_profit} sl={stop_loss}")
        return None, None, None

    tc = trading_client(cfg)
    close_side = OrderSide.SELL if position_side == "long" else OrderSide.BUY
    req = LimitOrderRequest(
        symbol=sym, qty=qty,
        side=close_side, time_in_force=TimeInForce.DAY,
        limit_price=take_profit,
        order_class=OrderClass.OCO,
        take_profit=TakeProfitRequest(limit_price=take_profit),
        stop_loss=StopLossRequest(stop_price=stop_loss),
    )
    o = tc.submit_order(req)
    tp_id = sl_id = None
    for leg in (getattr(o, "legs", None) or []):
        if str(leg.order_type).lower().endswith("limit"):
            tp_id = str(leg.id)
        elif str(leg.order_type).lower().endswith("stop"):
            sl_id = str(leg.id)
    return str(o.id), tp_id, sl_id


def cancel_order(cfg, order_id: str, dry_run: bool) -> None:
    if dry_run or not order_id:
        return
    tc = trading_client(cfg)
    try:
        tc.cancel_order_by_id(order_id)
    except Exception as exc:
        safe_log_stdout(f"  cancel({order_id}) failed: {exc}")


def replace_stop_leg(cfg, tr: TradeRecord, new_stop: float, dry_run: bool) -> None:
    """Cancel the current OCO and re-submit a new OCO at the new stop level
    (keeping the original take-profit). alpaca-py doesn't expose a clean
    'replace stop leg only' endpoint for OCO children, so we do cancel-and-resubmit
    for the OCO parent; if a partial fill happened we re-size."""
    if dry_run:
        safe_log_stdout(f"[DRY] would move {tr.symbol} stop to BE {new_stop:.2f}")
        tr.breakeven_moved = True
        return
    tc = trading_client(cfg)

    if tr.exit_order_id:
        try:
            tc.cancel_order_by_id(tr.exit_order_id)
        except Exception:
            pass
        # Give Alpaca a beat to release the qty before resubmitting.
        time.sleep(1.0)

    # Determine remaining qty (open position size).
    try:
        pos = tc.get_open_position(tr.symbol)
        remaining = int(float(pos.qty))
    except Exception:
        remaining = max(tr.filled_qty, 0)
    if remaining <= 0:
        tr.notes.append("no_position_to_protect_at_breakeven")
        return

    parent_id, tp_id, sl_id = submit_oco_exit(
        cfg, tr.symbol, remaining,
        tr.plan["take_profit"], new_stop, dry_run=False,
        position_side=tr.plan.get("side", "long"),
    )
    tr.exit_order_id = parent_id
    tr.take_profit_leg_id = tp_id
    tr.stop_leg_id = sl_id
    tr.breakeven_moved = True


def force_close_position(cfg, symbol: str, dry_run: bool) -> None:
    if dry_run:
        safe_log_stdout(f"[DRY] would force-close {symbol}")
        return
    tc = trading_client(cfg)
    try:
        tc.close_position(symbol)
    except Exception as exc:
        safe_log_stdout(f"  close({symbol}) failed: {exc}")


# ---------- Fill monitor ----------

def poll_entry_fills(cfg, trades: dict[str, TradeRecord],
                     date_iso: str, dry_run: bool) -> None:
    """Check every open entry order; if filled (or partially), attach OCO."""
    if dry_run:
        return
    if not any(tr.entry_order_id and not tr.closed and tr.exit_order_id is None
               for tr in trades.values()):
        return
    tc = trading_client(cfg)
    for sym, tr in trades.items():
        if not tr.entry_order_id or tr.closed or tr.exit_order_id:
            continue
        try:
            o = tc.get_order_by_id(tr.entry_order_id)
        except Exception as exc:
            safe_log_stdout(f"  fetch order {tr.entry_order_id} failed: {exc}")
            continue
        status = str(o.status).lower()
        filled_qty = int(float(o.filled_qty or 0))
        if filled_qty > 0:
            tr.filled_qty = filled_qty
            tr.avg_fill_price = float(o.filled_avg_price) if o.filled_avg_price else None
            journal(tr.strategy_name, "entry_filled", symbol=sym,
                    order_id=tr.entry_order_id, filled_qty=filled_qty,
                    avg_price=tr.avg_fill_price,
                    side=tr.plan.get("side", "long"))
            append_fill_event(date_iso, {
                "event": "entry_filled", "symbol": sym,
                "strategy": tr.strategy_name,
                "order_id": tr.entry_order_id,
                "filled_qty": filled_qty, "avg_price": tr.avg_fill_price,
                "side": tr.plan.get("side", "long"),
                "status": status,
            })
            # Attach OCO. Sized at filled_qty (handles partial fills).
            parent_id, tp_id, sl_id = submit_oco_exit(
                cfg, sym, filled_qty,
                tr.plan["take_profit"], tr.plan["stop_loss"], dry_run=False,
                position_side=tr.plan.get("side", "long"),
            )
            tr.exit_order_id = parent_id
            tr.take_profit_leg_id = tp_id
            tr.stop_leg_id = sl_id
            journal(tr.strategy_name, "oco_attached", symbol=sym,
                    parent_id=parent_id, tp=tr.plan["take_profit"],
                    sl=tr.plan["stop_loss"], qty=filled_qty)
            append_fill_event(date_iso, {
                "event": "oco_attached", "symbol": sym,
                "strategy": tr.strategy_name,
                "parent_id": parent_id, "tp": tr.plan["take_profit"],
                "sl": tr.plan["stop_loss"], "qty": filled_qty,
            })
            safe_log_stdout(
                f"  FILLED {sym} ({tr.strategy_name}) qty={filled_qty} "
                f"@ {fmt_price(tr.avg_fill_price)} -> OCO "
                f"TP {fmt_price(tr.plan['take_profit'])} / "
                f"SL {fmt_price(tr.plan['stop_loss'])}"
            )


def poll_breakeven_moves(cfg, trades: dict[str, TradeRecord],
                         date_iso: str, dry_run: bool) -> None:
    """For each filled trade, if last price has reached entry + 1R, move SL to breakeven."""
    candidates = [tr for tr in trades.values()
                  if tr.filled_qty > 0 and not tr.breakeven_moved
                  and not tr.closed and tr.avg_fill_price is not None]
    if not candidates:
        return
    syms = [tr.symbol for tr in candidates]
    trades_resp = get_latest_trade(syms, cfg)
    if not trades_resp:
        return
    for tr in candidates:
        t = trades_resp.get(tr.symbol)
        if t is None:
            continue
        last = float(t["price"])
        r = tr.plan["risk_per_share"]
        side = tr.plan.get("side", "long")
        # 1R-favourable move target depends on direction.
        if side == "long":
            target_be = tr.avg_fill_price + r
            triggered = last >= target_be
        else:
            target_be = tr.avg_fill_price - r
            triggered = last <= target_be
        if triggered:
            safe_log_stdout(
                f"  1R reached on {tr.symbol} ({tr.strategy_name}/{side}, "
                f"last {fmt_price(last)} vs BE+1R {fmt_price(target_be)}) "
                f"-> moving stop to entry {fmt_price(tr.avg_fill_price)}"
            )
            replace_stop_leg(cfg, tr, tr.avg_fill_price, dry_run=False)
            journal(tr.strategy_name, "breakeven_moved", symbol=tr.symbol,
                    side=side, new_stop=tr.avg_fill_price, last_price=last)
            append_fill_event(date_iso, {
                "event": "stop_to_breakeven", "symbol": tr.symbol,
                "strategy": tr.strategy_name, "side": side,
                "new_stop": tr.avg_fill_price, "last_price": last,
            })


def poll_exit_completion(cfg, trades: dict[str, TradeRecord],
                         date_iso: str, dry_run: bool) -> None:
    if dry_run:
        return
    tc = trading_client(cfg)
    for tr in trades.values():
        if tr.closed or not tr.exit_order_id:
            continue
        try:
            o = tc.get_order_by_id(tr.exit_order_id)
        except Exception:
            continue
        status = str(o.status).lower()
        if status in ("filled", "canceled", "expired", "rejected", "replaced"):
            # Look at legs to know which side actually hit (TP vs SL).
            legs = getattr(o, "legs", None) or []
            for leg in legs:
                leg_status = str(leg.status).lower()
                if leg_status == "filled":
                    leg_kind = "tp" if str(leg.order_type).lower().endswith("limit") else "sl"
                    fill_px = float(leg.filled_avg_price) if leg.filled_avg_price else None
                    journal(tr.strategy_name, "exit_filled", symbol=tr.symbol,
                            leg=leg_kind, filled_avg_price=fill_px,
                            side=tr.plan.get("side", "long"))
                    append_fill_event(date_iso, {
                        "event": f"exit_{leg_kind}_filled",
                        "symbol": tr.symbol,
                        "strategy": tr.strategy_name,
                        "filled_avg_price": fill_px,
                    })
                    # Realised P&L + R-multiple (best-effort)
                    if fill_px is not None and tr.avg_fill_price is not None and tr.filled_qty > 0:
                        side = tr.plan.get("side", "long")
                        if side == "long":
                            tr.realized_pnl = (fill_px - tr.avg_fill_price) * tr.filled_qty
                        else:
                            tr.realized_pnl = (tr.avg_fill_price - fill_px) * tr.filled_qty
                        r = tr.plan.get("risk_per_share") or 0
                        if r > 0:
                            tr.realized_R = round((tr.realized_pnl / tr.filled_qty) / r, 2)
            tr.closed = True


# ---------- Strategy orchestrator ----------

def _fire_strategy_entries(cfg, strat: Strategy,
                           trades: dict[str, TradeRecord],
                           opening_equity: float, date_iso: str,
                           dry_run: bool) -> int:
    """Run one strategy's entry phase: pick universe -> fetch bars ->
    evaluate -> submit. Returns count of submissions.

    Universe and concurrency caps are checked per-call. A symbol already
    in `trades` (any strategy) is skipped to keep one-trade-per-symbol-per-day.

    ON/OFF gate: read state/enabled_<name>.flag LIVE. If the strategy is
    OFF at the moment of firing, emit a single strategy_off_skipped
    journal event and return -- no scanner, no resource calls, no
    analysis, no journal noise. Toggling ON in the dashboard takes
    effect at the NEXT scheduled fire (today's fire is already done).
    """
    if not _is_strategy_enabled(strat.name):
        safe_log_stdout(
            f"=== {strat.entry_et} ET -- {strat.name} SKIPPED (strategy is OFF) ==="
        )
        journal(strat.name, "strategy_off_skipped",
                entry_et=strat.entry_et,
                note="state/enabled_<name>.flag absent at scheduled fire; "
                     "no scanner / resources / analysis ran")
        return 0

    safe_log_stdout(f"=== {strat.entry_et} ET — {strat.name} entry phase ===")
    journal(strat.name, "strategy_started",
            entry_et=strat.entry_et, cap=strat.max_concurrent,
            armed=_is_strategy_armed(strat.name))

    symbols = strat.pick_universe(date_iso, cfg)
    journal(strat.name, "universe_picked", n=len(symbols), symbols=symbols)
    safe_log_stdout(f"  {strat.name}: {len(symbols)} symbols in universe")
    if not symbols:
        journal(strat.name, "strategy_finished", n_submitted=0,
                reason="empty universe")
        return 0

    bars_by_sym = strat.fetch_bars(symbols, cfg, strat)
    risk_pct = cfg["risk_per_trade_pct"]
    max_pos_pct = cfg.get("max_position_pct", 0.0)
    global_cap = cfg["max_open_concurrent_positions"]

    submitted = 0
    disarmed_skips = 0
    for sym in symbols:
        # Global cap
        n_open = sum(1 for tr in trades.values() if not tr.closed)
        if n_open >= global_cap:
            safe_log_stdout(f"  {strat.name}: stop — global cap ({global_cap})")
            journal(strat.name, "rejected", symbol=sym,
                    reason=f"global_cap_{global_cap}_reached")
            break
        # Per-strategy cap (open + just-submitted-this-cycle)
        n_strat_open = sum(1 for tr in trades.values()
                           if tr.strategy_name == strat.name and not tr.closed)
        if n_strat_open >= strat.max_concurrent:
            safe_log_stdout(f"  {strat.name}: stop — per-strategy cap "
                            f"({strat.max_concurrent})")
            journal(strat.name, "rejected", symbol=sym,
                    reason=f"strategy_cap_{strat.max_concurrent}_reached")
            break
        # Symbol already traded today by any strategy
        if sym in trades:
            journal(strat.name, "rejected", symbol=sym,
                    reason="symbol already traded today",
                    by_strategy=trades[sym].strategy_name)
            continue

        bars = bars_by_sym.get(sym) or []
        plan = strat.evaluate(sym, bars, strat)
        if plan is None:
            continue  # strat.evaluate() already journaled the reason

        qty = position_size(
            opening_equity, risk_pct, plan["risk_per_share"],
            max_position_pct=max_pos_pct,
            entry_price=plan["entry_limit"] if plan["side"] == "long"
                        else plan["entry_stop_trigger"],
        )
        if qty <= 0:
            journal(strat.name, "rejected", symbol=sym,
                    reason="position_size yielded 0 shares",
                    risk_per_share=plan["risk_per_share"],
                    entry_limit=plan["entry_limit"])
            continue

        # Per-strategy arming gate (skipped when --dry-run is in force --
        # that flag already short-circuits all submits to log-only mode).
        # is_armed() reads state/armed_<strategy>.flag LIVE so toggling
        # from the dashboard mid-session takes effect on the next entry.
        if not dry_run and not _is_strategy_armed(strat.name):
            disarmed_skips += 1
            journal(strat.name, "entry_disarmed", symbol=sym,
                    qty=qty, side=plan["side"],
                    entry_stop=plan["entry_stop_trigger"],
                    entry_limit=plan["entry_limit"],
                    stop_loss=plan["stop_loss"],
                    take_profit=plan["take_profit"],
                    risk_per_share=plan["risk_per_share"],
                    note="strategy disarmed in dashboard; submit skipped, "
                         "no TradeRecord created")
            arrow = "↑" if plan["side"] == "long" else "↓"
            safe_log_stdout(
                f"  [DISARMED] {strat.name}/{plan['side']} {arrow} {sym} "
                f"qty={qty} stop={plan['entry_stop_trigger']} "
                f"limit={plan['entry_limit']} SL={plan['stop_loss']} "
                f"TP={plan['take_profit']}  (would submit if armed)"
            )
            continue

        oid = submit_setup_entry(cfg, plan, qty, dry_run)
        tr = TradeRecord(symbol=sym, strategy_name=strat.name, plan=plan,
                         qty=qty, entry_order_id=oid)
        trades[sym] = tr
        submitted += 1
        journal(strat.name, "entry_submitted", symbol=sym,
                order_id=oid, qty=qty, side=plan["side"],
                entry_stop=plan["entry_stop_trigger"],
                entry_limit=plan["entry_limit"],
                stop_loss=plan["stop_loss"],
                take_profit=plan["take_profit"],
                take_profit_R=plan.get("take_profit_R"),
                strategy_version=plan.get("strategy_version"))
        append_fill_event(date_iso, {
            "event": "entry_submitted", "symbol": sym, "strategy": strat.name,
            "side": plan["side"], "qty": qty,
            "entry_stop": plan["entry_stop_trigger"],
            "entry_limit": plan["entry_limit"],
            "stop_loss": plan["stop_loss"],
            "take_profit": plan["take_profit"],
            "order_id": oid,
        })
        arrow = "↑" if plan["side"] == "long" else "↓"
        safe_log_stdout(
            f"  QUEUED {strat.name}/{plan['side']} {arrow} {sym} qty={qty} "
            f"stop={plan['entry_stop_trigger']} limit={plan['entry_limit']} "
            f"SL={plan['stop_loss']} TP={plan['take_profit']}"
        )

    journal(strat.name, "strategy_finished", n_submitted=submitted,
            n_disarmed_skips=disarmed_skips)
    if submitted == 0:
        if disarmed_skips:
            safe_log_stdout(
                f"  {strat.name}: 0 submitted ({disarmed_skips} would-submit "
                f"skipped because strategy is disarmed)"
            )
        else:
            safe_log_stdout(f"  {strat.name}: no entries submitted")
    return submitted


def _cancel_unfilled_for_strategy(cfg, trades: dict[str, TradeRecord],
                                  strategy_name: str, date_iso: str,
                                  dry_run: bool) -> int:
    """Called when a strategy's entry_cutoff_et passes. Cancels every
    unfilled entry order tagged to this strategy."""
    n_cancelled = 0
    for tr in list(trades.values()):
        if tr.strategy_name != strategy_name:
            continue
        if not tr.entry_order_id or tr.filled_qty > 0 or tr.closed:
            continue
        cancel_order(cfg, tr.entry_order_id, dry_run)
        journal(strategy_name, "entry_cancelled", symbol=tr.symbol,
                order_id=tr.entry_order_id, reason="entry_cutoff_et")
        append_fill_event(date_iso, {
            "event": "entry_canceled_time_cutoff",
            "symbol": tr.symbol, "strategy": strategy_name,
            "order_id": tr.entry_order_id,
        })
        safe_log_stdout(f"  {strategy_name}: canceled unfilled {tr.symbol}")
        n_cancelled += 1
    return n_cancelled


def phase_run_strategies(cfg, strategies: list[Strategy],
                         trades: dict[str, TradeRecord],
                         opening_equity: float, date_iso: str,
                         dry_run: bool, fake_now: str | None) -> None:
    """Fire each enabled strategy at its entry_et; run continuous management
    in between and through to ~eod. Handles per-strategy entry-cutoff
    cancellation. Stops 60 seconds before phase_eod_close_all (15:58).

    fake_now branch: fire every strategy once, run management once, return.
    """
    eod_str = cfg.get("eod_close_all_et", "15:58")
    eod_dt = et_at(date_iso, eod_str)

    if fake_now:
        # Replay/test mode: fire shortlist phase (if any) then entry phase.
        # In TRUE replay (--replay-date), skip the shortlist phase entirely —
        # do_shortlist() pulls from live PM-mover scrapes / GUNS scanner which
        # are TODAY's state, not the replay date's. The entry phase reads the
        # shortlist artifact left over from the historical day's session.
        if not cfg.get("replay_mode"):
            for strat in strategies:
                _fire_strategy_shortlist(cfg, strat, date_iso)
        for strat in strategies:
            _fire_strategy_entries(cfg, strat, trades, opening_equity,
                                   date_iso, dry_run)
        poll_entry_fills(cfg, trades, date_iso, dry_run)
        poll_breakeven_moves(cfg, trades, date_iso, dry_run)
        poll_exit_completion(cfg, trades, date_iso, dry_run)
        return

    fired: set[str] = set()
    shortlisted: set[str] = set()
    cancelled_cutoff: set[str] = set()

    while et_now() < eod_dt - timedelta(seconds=60):
        try:
            now = et_now()

            # Fire shortlist phases whose shortlist_et has been reached.
            # One-shot per strategy per day. Gated by is_enabled() inside
            # _fire_strategy_shortlist so OFF strategies skip cheaply.
            for strat in strategies:
                if strat.name in shortlisted:
                    continue
                sl_et = strat.shortlist_et
                if sl_et and now >= et_at(date_iso, sl_et):
                    _fire_strategy_shortlist(cfg, strat, date_iso)
                    shortlisted.add(strat.name)

            # Fire entry phases whose entry_et has been reached.
            for strat in strategies:
                if strat.name in fired:
                    continue
                if now >= et_at(date_iso, strat.entry_et):
                    _fire_strategy_entries(cfg, strat, trades, opening_equity,
                                           date_iso, dry_run)
                    fired.add(strat.name)

            # Management loop — fills, breakeven, exit completion (shared).
            poll_entry_fills(cfg, trades, date_iso, dry_run)
            poll_breakeven_moves(cfg, trades, date_iso, dry_run)
            poll_exit_completion(cfg, trades, date_iso, dry_run)

            # Per-strategy entry cutoff: cancel unfilled (one-shot per strategy).
            for strat in strategies:
                if strat.name in cancelled_cutoff:
                    continue
                if now >= et_at(date_iso, strat.entry_cutoff_et):
                    _cancel_unfilled_for_strategy(cfg, trades, strat.name,
                                                  date_iso, dry_run)
                    cancelled_cutoff.add(strat.name)
        except Exception as exc:
            safe_log_stdout(f"  strategy-loop exception: {exc}")
            traceback.print_exc()
        time.sleep(5)


def _fire_strategy_shortlist(cfg, strat: Strategy, date_iso: str) -> None:
    """Run one strategy's shortlist phase (optional). Gated by ON/OFF;
    OFF strategies skip silently. Calls strat.shortlist(date_iso, cfg, strat)
    if it's defined. The shortlist callable is expected to write its
    own state artifact (e.g. state/shortlist_<name>_<date>.json) and
    emit its own journal events."""
    if strat.shortlist is None:
        return
    if not _is_strategy_enabled(strat.name):
        # No noisy log here -- the entry-phase fire will emit
        # strategy_off_skipped, which is the per-day OFF marker.
        return
    safe_log_stdout(
        f"=== {strat.shortlist_et} ET -- {strat.name} shortlist phase ==="
    )
    try:
        strat.shortlist(date_iso, cfg, strat)
    except Exception as exc:
        safe_log_stdout(f"  {strat.name} shortlist raised: {exc}")
        traceback.print_exc()
        journal(strat.name, "shortlist_failed", error=f"{type(exc).__name__}: {exc}")


# ---------- EOD history ingest ----------

def _run_eod_history_ingest(cfg) -> None:
    """Post-EOD: incremental Parquet update of today's universe.

    Soft-fail by design -- the bot's success does NOT depend on this.
    If IBKR is down, pacing fails, or pyarrow is missing, we log and
    move on. The user can always rerun manually:
        py resources/ibkr_history.py update --universe
    """
    timeframes = cfg.get("eod_history_timeframes", ["1min", "daily"])
    journal_days = int(cfg.get("eod_history_journal_days", 30))
    seed_days = int(cfg.get("eod_history_seed_days", 60))
    try:
        from ibkr_history import build_universe, bulk_update
    except Exception as exc:
        safe_log_stdout(f"[eod ingest] skipped (import failed): {exc}")
        return
    try:
        symbols = build_universe(cfg, journal_days=journal_days)
    except Exception as exc:
        safe_log_stdout(f"[eod ingest] skipped (universe build failed): {exc}")
        return
    if not symbols:
        safe_log_stdout("[eod ingest] universe is empty -- skipped.")
        return
    safe_log_stdout(f"=== EOD history ingest -- {len(symbols)} symbols x "
                    f"{len(timeframes)} timeframes ===")
    try:
        bulk_update(symbols, timeframes, cfg, lookback_days_for_new=seed_days)
        safe_log_stdout("[eod ingest] complete.")
    except Exception as exc:
        safe_log_stdout(f"[eod ingest] FAILED: {type(exc).__name__}: {exc}")
        try:
            journal("orchestrator", "eod_history_ingest_failed",
                    error=f"{type(exc).__name__}: {exc}")
        except Exception:
            pass


# ---------- EOD safety sweep ----------

def phase_eod_close_all(cfg, trades: dict[str, TradeRecord],
                        date_iso: str, dry_run: bool,
                        fake_now: str | None) -> None:
    """Final EOD safety sweep — cancels ALL open orders and closes ALL open
    positions before the 16:00 ET bell.

    Independent backstop from each setup's own force-close. Runs at
    cfg.eod_close_all_et (default 15:58 ET). Catches:
      - Setup 1/5 positions that somehow survived the 11:00 close
      - AT positions that didn't get closed at 15:55
      - Any manually-opened paper position
      - Stragglers from misbehaving OCO orders

    Uses Alpaca's tc.close_all_positions(cancel_orders=True) endpoint which
    is one call that liquidates everything at market.
    """
    eod_at = et_at(date_iso, cfg.get("eod_close_all_et", "15:58"))
    if not fake_now:
        sleep_until(eod_at, fake_now)

    safe_log_stdout("=== EOD sweep — cancel all orders + close all positions ===")

    if dry_run:
        open_trs = [tr for tr in trades.values() if not tr.closed]
        if not open_trs:
            safe_log_stdout("[DRY] no open trades tracked — nothing to sweep")
        for tr in open_trs:
            safe_log_stdout(f"[DRY] would close {tr.symbol} "
                            f"(strategy={tr.strategy_name}, qty={tr.qty})")
        return

    tc = trading_client(cfg)
    try:
        # cancel_orders=True cancels every open order first, then liquidates
        # every open position. One call, atomic from our perspective.
        responses = tc.close_all_positions(cancel_orders=True)
    except Exception as exc:
        safe_log_stdout(f"  close_all_positions FAILED: {exc}")
        traceback.print_exc()
        append_fill_event(date_iso, {
            "event": "eod_close_all_failed",
            "error": str(exc),
        })
        return

    n = len(responses or [])
    safe_log_stdout(f"  close_all_positions submitted: {n} symbol(s)")
    for resp in responses or []:
        sym = getattr(resp, "symbol", None) or "?"
        status = getattr(resp, "status", None)
        body = getattr(resp, "body", None)
        ok = status in (200, 201, 207, None) or (
            isinstance(status, int) and 200 <= status < 300
        )
        safe_log_stdout(f"  -> {sym}: status={status} ok={ok}")
        append_fill_event(date_iso, {
            "event": "eod_close_all", "symbol": sym,
            "status": str(status),
        })
        tr = trades.get(sym)
        if tr and not tr.closed:
            tr.closed = True
            tr.notes.append("force-closed at EOD sweep")
            journal(tr.strategy_name, "force_closed", symbol=sym,
                    status=str(status))


# ---------- Reporting ----------

def render_report(date_iso: str, trades: dict[str, TradeRecord],
                  opening_equity: float,
                  closing_equity: float | None) -> str:
    """End-of-day report grouped by strategy with per-strategy P&L,
    journal-derived decision counters, and per-trade detail."""
    lines = [f"<b>Bot daily report — {date_iso}</b>"]
    pnl_dollars = None
    if closing_equity is not None:
        pnl_dollars = closing_equity - opening_equity
        pct = (pnl_dollars / opening_equity * 100) if opening_equity else 0.0
        sign = "+" if pnl_dollars >= 0 else ""
        lines.append(
            f"<i>Equity {fmt_price(opening_equity)} -> {fmt_price(closing_equity)} "
            f"({sign}${pnl_dollars:,.2f} / {sign}{pct:.2f}%)</i>"
        )
    lines.append(f"<i>total trades taken: {len(trades)}</i>")
    fb_active, fb_reason = fallback_state()
    if fb_active:
        lines.append(f"<i>⚠️ IBKR data unavailable — fell back to Alpaca IEX. "
                     f"Reason: {fb_reason}</i>")
    lines.append("")

    # ---- Per-strategy rollup ----
    by_strategy: dict[str, list[TradeRecord]] = {}
    for tr in trades.values():
        by_strategy.setdefault(tr.strategy_name, []).append(tr)

    journal_summary = summarize_by_strategy(date_iso)

    if not trades and not journal_summary:
        lines.append("<i>No trades or decisions today.</i>")
        return "\n".join(lines)

    for strat_name in sorted(set(list(by_strategy.keys()) + list(journal_summary.keys()))):
        strat_trades = by_strategy.get(strat_name, [])
        summary = journal_summary.get(strat_name, {})
        # P&L rollup
        realized = sum(tr.realized_pnl or 0 for tr in strat_trades)
        n_wins = sum(1 for tr in strat_trades
                     if (tr.realized_pnl or 0) > 0)
        n_losses = sum(1 for tr in strat_trades
                       if (tr.realized_pnl or 0) < 0)
        n_filled = sum(1 for tr in strat_trades if tr.filled_qty > 0)
        avg_R = (sum(tr.realized_R for tr in strat_trades if tr.realized_R is not None)
                 / max(1, sum(1 for tr in strat_trades if tr.realized_R is not None)))

        lines.append(f"<b>== {strat_name} ==</b>")
        lines.append(
            f"<i>decisions: shortlisted {summary.get('n_shortlisted',0)} · "
            f"rejected {summary.get('n_rejected',0)} · "
            f"planned {summary.get('n_planned',0)} · "
            f"submitted {summary.get('n_submitted',0)} · "
            f"filled {summary.get('n_filled',0)} · "
            f"BE {summary.get('n_breakeven',0)} · "
            f"TP {summary.get('n_exit_tp',0)} · "
            f"SL {summary.get('n_exit_sl',0)} · "
            f"cancel {summary.get('n_cancelled',0)} · "
            f"EOD-closed {summary.get('n_force_closed',0)}</i>"
        )
        sign = "+" if realized >= 0 else ""
        lines.append(
            f"<i>P&amp;L: {sign}${realized:,.2f} · filled {n_filled} · "
            f"wins {n_wins} / losses {n_losses} · avg-R {avg_R:.2f}</i>"
        )
        for tr in strat_trades:
            side = tr.plan.get("side", "long").upper() if isinstance(tr.plan, dict) else "?"
            head = f"  <b>{tr.symbol}</b> {side}"
            if tr.filled_qty == 0:
                lines.append(f"{head} — unfilled (qty {tr.qty})")
            else:
                pieces = [head,
                          f"qty {tr.filled_qty} @ {fmt_price(tr.avg_fill_price)}",
                          f"TP {fmt_price(tr.plan['take_profit'])}",
                          f"SL {fmt_price(tr.plan['stop_loss'])}"]
                if tr.breakeven_moved:
                    pieces.append("BE-moved")
                if tr.closed:
                    if tr.realized_R is not None:
                        pieces.append(f"CLOSED ({tr.realized_R:+.2f}R)")
                    else:
                        pieces.append("CLOSED")
                lines.append("  · ".join(pieces))
            if tr.notes:
                lines.append("    notes: " + "; ".join(tr.notes))
        lines.append("")
    return "\n".join(lines)


# ---------- Main ----------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true",
                   help="Log only; do not submit any Alpaca orders.")
    p.add_argument("--fake-now", default=None,
                   help="ET wall-clock to anchor for testing, HH:MM. "
                        "Disables sleep_until and runs phase_orb in single-shot mode.")
    p.add_argument("--replay-date", default=None,
                   help="YYYY-MM-DD: re-run the orchestrator against bars stored "
                        "in data/price_history/ for this past date. Implies "
                        "--dry-run and data_provider=parquet. Journal is routed "
                        "to data/replay/journal_<date>_<run_id>.jsonl so the "
                        "live data/journal/ stays clean. Pair with --fake-now to "
                        "set the wall-clock cutoff (default 16:00 ET = full day).")
    return p.parse_args()


# ---------- Startup IBKR resolver ----------

def _spawn_ibc_launcher(launcher_bat: str) -> bool:
    """Launch IBC (TWS auto-login) as a DETACHED process so it survives
    after the bot exits — lets the user keep TWS running for manual
    trading. Returns True if Popen succeeded, False otherwise.
    """
    import os
    import subprocess
    if not Path(launcher_bat).exists():
        safe_log_stdout(f"  IBC launcher not found at {launcher_bat}")
        return False
    creation_flags = 0
    if os.name == "nt":
        # DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP fully detaches.
        # Without these, TWS would die when this orchestrator exits.
        creation_flags = (
            subprocess.DETACHED_PROCESS                    # type: ignore[attr-defined]
            | subprocess.CREATE_NEW_PROCESS_GROUP          # type: ignore[attr-defined]
        )
    try:
        subprocess.Popen(
            [launcher_bat],
            shell=False,
            cwd=str(Path(launcher_bat).parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
            close_fds=True,
        )
        return True
    except Exception as exc:
        safe_log_stdout(f"  IBC Popen failed: {exc}")
        return False


def _resolve_ibkr_or_fallback(cfg: dict) -> None:
    """Run the startup data-provider sequence in place on `cfg`.

    Mutates cfg["data_provider"] to "alpaca" if IBKR can't be brought up
    within the configured timeout. Emits journal events at each decision
    point so the review/stats layer can attribute failures.
    """
    try:
        from ibkr_data import probe_ibkr_reachable
    except Exception as exc:
        safe_log_stdout(f"⚠️  probe import failed ({exc}); using Alpaca.")
        cfg["data_provider"] = "alpaca"
        journal("orchestrator", "data_provider_fallback",
                requested="ibkr", actual="alpaca",
                reason=f"probe import failed: {exc}")
        return

    # --- Step 1: initial probe ---
    ok, reason = probe_ibkr_reachable(cfg, timeout=5.0)
    if ok:
        safe_log_stdout("Data provider: IBKR (probe ok)")
        journal("orchestrator", "data_provider_selected",
                provider="ibkr", probe="ok", attempt="initial")
        return

    safe_log_stdout(f"  IBKR initial probe: not reachable ({reason})")

    # --- Step 2: maybe auto-launch IBC ---
    if not bool(cfg.get("ibkr_autolaunch_enabled", True)):
        safe_log_stdout(
            "⚠️  ibkr_autolaunch_enabled=false; falling back to Alpaca."
        )
        cfg["data_provider"] = "alpaca"
        journal("orchestrator", "data_provider_fallback",
                requested="ibkr", actual="alpaca",
                reason=f"initial probe failed; auto-launch disabled. "
                       f"probe_reason={reason}")
        return

    launcher = cfg.get("ibkr_launcher_bat")
    if not launcher:
        safe_log_stdout(
            "⚠️  No ibkr_launcher_bat configured; falling back to Alpaca."
        )
        cfg["data_provider"] = "alpaca"
        journal("orchestrator", "data_provider_fallback",
                requested="ibkr", actual="alpaca",
                reason=f"initial probe failed; no launcher configured. "
                       f"probe_reason={reason}")
        return

    safe_log_stdout(f"  Auto-launching IBC: {launcher}")
    journal("orchestrator", "ibc_autolaunch_started",
            launcher=launcher, probe_reason=reason)
    spawned = _spawn_ibc_launcher(launcher)
    if not spawned:
        safe_log_stdout("⚠️  IBC launcher could not start; using Alpaca.")
        cfg["data_provider"] = "alpaca"
        journal("orchestrator", "data_provider_fallback",
                requested="ibkr", actual="alpaca",
                reason="IBC launcher Popen failed")
        return

    # --- Step 3: retry probe with backoff ---
    deadline_s = float(cfg.get("ibkr_autolaunch_timeout_s", 90))
    waits = [10, 10, 10, 15, 15, 20, 20]   # ~100s total budget
    spent = 0.0
    for sleep_s in waits:
        if spent >= deadline_s:
            break
        time.sleep(sleep_s)
        spent += sleep_s
        ok, reason = probe_ibkr_reachable(cfg, timeout=5.0)
        if ok:
            safe_log_stdout(
                f"Data provider: IBKR (autolaunch succeeded after {spent:.0f}s)"
            )
            journal("orchestrator", "data_provider_selected",
                    provider="ibkr", probe="ok",
                    attempt="after_autolaunch",
                    autolaunch_wait_s=spent)
            return
        safe_log_stdout(
            f"  IBKR still not ready after {spent:.0f}s -- {reason}"
        )

    # --- Step 4: give up, fall back to Alpaca ---
    safe_log_stdout(
        f"⚠️  IBKR did not come up within {spent:.0f}s of auto-launch; "
        f"falling back to Alpaca IEX. The launched TWS process keeps "
        f"running -- IBKR data will be available next session if you "
        f"complete login manually."
    )
    cfg["data_provider"] = "alpaca"
    journal("orchestrator", "data_provider_fallback",
            requested="ibkr", actual="alpaca",
            reason=f"autolaunch timed out after {spent:.0f}s; "
                   f"last_probe_reason={reason}",
            autolaunch_wait_s=spent)


def main() -> int:
    args = parse_args()
    cfg = load_config()
    strategies = load_known(cfg)
    _validate_strict_rules(cfg, strategies)  # refuses to start on bad risk config
    STATE_DIR.mkdir(exist_ok=True)
    date_iso = et_today_iso(args.fake_now)

    # ---- Replay-mode setup ----
    # --replay-date overrides several knobs at once:
    #   - data_provider becomes "parquet" (bars from data/price_history/)
    #   - dry_run is forced True (no Alpaca submissions in replay)
    #   - date_iso comes from --replay-date instead of "today"
    #   - journal events are routed to data/replay/ via writer.set_replay_target()
    # The strategy code is unchanged — it just receives different bars.
    if args.replay_date:
        cfg["data_provider"] = "parquet"
        cfg["replay_date"] = args.replay_date
        cfg["replay_mode"] = True
        args.dry_run = True
        date_iso = args.replay_date
        try:
            from writer import set_replay_target  # journal/writer.py
            from datetime import datetime as _dt
            run_id = _dt.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            _skill_dir = Path(__file__).resolve().parent.parent
            replay_path = _skill_dir / "data" / "replay" / f"journal_{args.replay_date}_{run_id}.jsonl"
            replay_path.parent.mkdir(parents=True, exist_ok=True)
            set_replay_target(replay_path)
            safe_log_stdout(f"[REPLAY] date={args.replay_date} cutoff={args.fake_now or '16:00'} "
                            f"→ journal: {replay_path.relative_to(_skill_dir)}")
        except Exception as exc:
            safe_log_stdout(f"[REPLAY] failed to set journal target: {exc}")

    # Startup data-provider sequence (cfg.data_provider == "ibkr"):
    #   1. Probe IBKR (5s). If reachable -> use IBKR.
    #   2. Else, if ibkr_autolaunch_enabled is true, spawn the IBC
    #      launcher .bat as a detached process (TWS keeps running after
    #      the bot exits, so the user can keep trading manually). Then
    #      retry the probe with backoff up to ibkr_autolaunch_timeout_s
    #      (default 90s). TWS typically takes 30-60s to fully log in.
    #   3. If still not reachable, fall back to Alpaca IEX for the
    #      session. Order routing is unaffected (Alpaca paper either way);
    #      only the bar/quote feed changes.
    if (cfg.get("data_provider") or "").lower() == "ibkr" and not args.dry_run:
        _resolve_ibkr_or_fallback(cfg)

    # 1. Opening equity snapshot
    if args.dry_run:
        opening_equity = 100_000.0
        safe_log_stdout(f"[DRY] using fake equity ${opening_equity:,.2f}")
    else:
        opening_equity = get_account_equity(cfg)
        safe_log_stdout(f"Opening equity: {fmt_price(opening_equity)}")
    equity_path(date_iso).write_text(
        json.dumps({"opening": opening_equity}, indent=2), encoding="utf-8"
    )

    # 2. Strategy orchestrator — fires each enabled strategy at its entry_et,
    #    runs shared management loop, cancels per-strategy unfilled at cutoff.
    trades: dict[str, TradeRecord] = {}
    phase_run_strategies(cfg, strategies, trades, opening_equity,
                         date_iso, args.dry_run, args.fake_now)

    # 3. EOD sweep at 15:58 ET — final close-all guarantee.
    phase_eod_close_all(cfg, trades, date_iso, args.dry_run, args.fake_now)

    # 3b. EOD history ingest — incremental Parquet update of today's universe
    #     so tomorrow's review/backtest has fresh bars without manual action.
    #     Soft-fail: bot success doesn't depend on IBKR being up.
    if cfg.get("eod_history_ingest", True) and not args.dry_run:
        _run_eod_history_ingest(cfg)

    # 4. Report
    closing_equity = None
    if not args.dry_run:
        try:
            closing_equity = get_account_equity(cfg)
        except Exception as exc:
            safe_log_stdout(f"closing equity fetch failed: {exc}")
    equity_state = json.loads(equity_path(date_iso).read_text(encoding="utf-8"))
    equity_state["closing"] = closing_equity
    equity_path(date_iso).write_text(json.dumps(equity_state, indent=2), encoding="utf-8")

    report = render_report(date_iso, trades, opening_equity, closing_equity)
    safe_log_stdout("")
    # Strip HTML for stdout.
    import html as _html
    import re
    safe_log_stdout(_html.unescape(re.sub(r"<[^>]+>", "", report)))

    if not args.dry_run:
        if not send_telegram(cfg, report):
            safe_log_stdout("(Telegram not configured — report printed above only.)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
