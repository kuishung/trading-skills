"""GUNS session orchestrator — the main entrypoint.

Runs from ~9:00 ET through ~11:05 ET. Builds the day's plan (or loads an
existing plan written by scan_premarket.py), submits Setup 1 entries at 9:25,
evaluates Setup 5 at 9:31, watches for fills, attaches OCO exits, moves stops
to breakeven at 1R, force-cancels unfilled entries at 10:30, force-closes
remaining positions at 11:00, and sends a Telegram report.

Paper-only. Uses alpaca-py directly for execution to allow the multi-step
flow (stop-limit entry then OCO attach on fill) that the alpaca-trader-paper
CLI doesn't expose. The paper-only guard is re-checked at construction time.

CLI:
    py scripts/trade_day.py
    py scripts/trade_day.py --dry-run            # plan & log, no orders
    py scripts/trade_day.py --auto-scan          # build watchlist via news
    py scripts/trade_day.py --watchlist AAA,BBB  # inline watchlist
    py scripts/trade_day.py --fake-now 09:25     # advance to a wall-clock for testing
    py scripts/trade_day.py --replan             # rebuild plan even if it exists
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    STATE_DIR, append_fill_event, equity_path, et_at, et_now,
    et_today_iso, fallback_state, fmt_price, get_latest_trade,
    get_rth_minute_bars, load_config, plan_path, safe_log_stdout,
    send_telegram, sleep_until, trading_client, watchlist_path,
)
from scan_premarket import build_plan, read_watchlist, auto_scan_watchlist
from signals import (  # noqa: E402
    evaluate_setup5_first_minute, position_size, split_pm_rth,
)


# ---------- Trade record ----------

@dataclass
class TradeRecord:
    symbol: str
    setup: int
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
    notes: list[str] = field(default_factory=list)


# ---------- Plan I/O ----------

def load_or_build_plan(args, cfg) -> dict:
    date_iso = et_today_iso(args.fake_now)
    pp = plan_path(date_iso)
    if pp.exists() and not args.replan:
        safe_log_stdout(f"Loading existing plan: {pp}")
        return json.loads(pp.read_text(encoding="utf-8"))

    if args.watchlist:
        symbols = [t.strip().upper() for t in args.watchlist.split(",") if t.strip()]
    elif args.auto_scan:
        symbols = auto_scan_watchlist(cfg, date_iso, args.fake_now)
        if symbols:
            STATE_DIR.mkdir(exist_ok=True)
            watchlist_path(date_iso).write_text("\n".join(symbols) + "\n", encoding="utf-8")
    else:
        symbols = read_watchlist(date_iso, None)

    if not symbols:
        sys.exit("Empty watchlist.")
    safe_log_stdout(f"Building plan for {len(symbols)} ticker(s): {', '.join(symbols)}")
    plan = build_plan(symbols, cfg, args.fake_now)
    pp.parent.mkdir(exist_ok=True)
    pp.write_text(json.dumps(plan, indent=2, default=str), encoding="utf-8")
    safe_log_stdout(f"Plan written: {pp}")
    return plan


# ---------- Alpaca order helpers ----------

def get_account_equity(cfg) -> float:
    tc = trading_client(cfg)
    acct = tc.get_account()
    return float(acct.equity)


def submit_setup_entry(cfg, plan_row: dict, qty: int, dry_run: bool) -> str | None:
    """Submit the buy-stop-limit entry. Returns Alpaca order id, or None for dry-run."""
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import StopLimitOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce

    sym = plan_row["symbol"]
    stop = plan_row["entry_stop_trigger"]
    limit = plan_row["entry_limit"]
    if dry_run:
        safe_log_stdout(f"[DRY] would submit buy-stop-limit {sym} qty={qty} "
                        f"stop={stop} limit={limit}")
        return None
    tc = trading_client(cfg)
    req = StopLimitOrderRequest(
        symbol=sym, qty=qty,
        side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
        stop_price=stop, limit_price=limit,
    )
    o = tc.submit_order(req)
    return str(o.id)


def submit_oco_exit(cfg, sym: str, qty: int, take_profit: float,
                    stop_loss: float, dry_run: bool) -> tuple[str | None, str | None, str | None]:
    """Submit OCO sell pair (take-profit limit + stop-loss stop).

    Returns (parent_id, tp_leg_id, sl_leg_id). For dry-run all None.
    """
    from alpaca.trading.requests import LimitOrderRequest, TakeProfitRequest, StopLossRequest
    from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce

    if dry_run:
        safe_log_stdout(f"[DRY] would submit OCO {sym} qty={qty} "
                        f"tp={take_profit} sl={stop_loss}")
        return None, None, None

    tc = trading_client(cfg)
    # alpaca-py exposes OCO via OrderClass.OCO on a LimitOrderRequest with both
    # take_profit and stop_loss attached.
    req = LimitOrderRequest(
        symbol=sym, qty=qty,
        side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
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
            append_fill_event(date_iso, {
                "event": "entry_filled", "symbol": sym,
                "order_id": tr.entry_order_id,
                "filled_qty": filled_qty, "avg_price": tr.avg_fill_price,
                "status": status,
            })
            # Attach OCO. Sized at filled_qty (handles partial fills).
            parent_id, tp_id, sl_id = submit_oco_exit(
                cfg, sym, filled_qty,
                tr.plan["take_profit"], tr.plan["stop_loss"], dry_run=False,
            )
            tr.exit_order_id = parent_id
            tr.take_profit_leg_id = tp_id
            tr.stop_leg_id = sl_id
            append_fill_event(date_iso, {
                "event": "oco_attached", "symbol": sym,
                "parent_id": parent_id, "tp": tr.plan["take_profit"],
                "sl": tr.plan["stop_loss"], "qty": filled_qty,
            })
            safe_log_stdout(
                f"  FILLED {sym} qty={filled_qty} @ {fmt_price(tr.avg_fill_price)} "
                f"-> OCO TP {fmt_price(tr.plan['take_profit'])} / "
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
        target_be = tr.avg_fill_price + r
        if last >= target_be:
            safe_log_stdout(
                f"  1R reached on {tr.symbol} (last {fmt_price(last)} >= "
                f"BE+1R {fmt_price(target_be)}) -> moving stop to entry "
                f"{fmt_price(tr.avg_fill_price)}"
            )
            replace_stop_leg(cfg, tr, tr.avg_fill_price, dry_run=False)
            append_fill_event(date_iso, {
                "event": "stop_to_breakeven", "symbol": tr.symbol,
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
                    append_fill_event(date_iso, {
                        "event": f"exit_{leg_kind}_filled",
                        "symbol": tr.symbol,
                        "filled_avg_price": float(leg.filled_avg_price) if leg.filled_avg_price else None,
                    })
            tr.closed = True


# ---------- Phase handlers ----------

def phase_setup1(cfg, plan, trades: dict[str, TradeRecord],
                 equity: float, date_iso: str, dry_run: bool) -> None:
    safe_log_stdout("=== 09:25 ET — Setup 1 queue ===")
    cap = cfg["max_setup1_candidates"]
    max_pos = cfg["max_open_concurrent_positions"]
    risk_pct = cfg["risk_per_trade_pct"]
    submitted = 0
    for sym, row in plan["tickers"].items():
        if submitted >= cap:
            break
        if not row.get("setup1_eligible") or not row.get("setup1"):
            continue
        if len(trades) >= max_pos:
            safe_log_stdout(f"  skip {sym}: at max_open_concurrent_positions")
            break
        plan_row = row["setup1"]
        qty = position_size(equity, risk_pct, plan_row["risk_per_share"])
        if qty <= 0:
            safe_log_stdout(f"  skip {sym}: position-size math yielded 0 shares")
            continue
        oid = submit_setup_entry(cfg, plan_row, qty, dry_run)
        tr = TradeRecord(symbol=sym, setup=1, plan=plan_row, qty=qty, entry_order_id=oid)
        trades[sym] = tr
        submitted += 1
        append_fill_event(date_iso, {
            "event": "entry_submitted", "symbol": sym, "setup": 1,
            "qty": qty, "entry_stop": plan_row["entry_stop_trigger"],
            "entry_limit": plan_row["entry_limit"], "stop_loss": plan_row["stop_loss"],
            "take_profit": plan_row["take_profit"], "order_id": oid,
        })
        safe_log_stdout(
            f"  QUEUED Setup 1 {sym} qty={qty} stop={plan_row['entry_stop_trigger']} "
            f"limit={plan_row['entry_limit']} SL={plan_row['stop_loss']} "
            f"TP={plan_row['take_profit']}"
        )
    if submitted == 0:
        safe_log_stdout("  no Setup 1 candidates today")


def phase_setup5(cfg, plan, trades: dict[str, TradeRecord],
                 equity: float, date_iso: str, dry_run: bool,
                 fake_now: str | None) -> None:
    safe_log_stdout("=== 09:31 ET — Setup 5 evaluation ===")
    syms_to_eval = [
        sym for sym, row in plan["tickers"].items()
        if sym not in trades  # not already in a Setup 1 entry/position
        and not row.get("rejections")
    ]
    if not syms_to_eval:
        safe_log_stdout("  nothing to evaluate (all watchlist tickers are in Setup 1 or rejected)")
        return

    bars_by_sym = get_rth_minute_bars(syms_to_eval, cfg, fake_now)

    cap = cfg["max_setup5_candidates"]
    max_pos = cfg["max_open_concurrent_positions"]
    risk_pct = cfg["risk_per_trade_pct"]
    submitted = 0

    for sym in syms_to_eval:
        if submitted >= cap or len(trades) >= max_pos:
            break
        all_bars = bars_by_sym.get(sym, [])
        pm_bars, rth_bars = split_pm_rth(all_bars)
        if not rth_bars:
            continue
        first_min = rth_bars[0]
        plan_row = evaluate_setup5_first_minute(
            first_min, pm_bars, cfg["take_profit_R"], sym,
        )
        if plan_row is None:
            continue
        qty = position_size(equity, risk_pct, plan_row["risk_per_share"])
        if qty <= 0:
            continue
        oid = submit_setup_entry(cfg, plan_row, qty, dry_run)
        tr = TradeRecord(symbol=sym, setup=5, plan=plan_row, qty=qty, entry_order_id=oid)
        trades[sym] = tr
        submitted += 1
        append_fill_event(date_iso, {
            "event": "entry_submitted", "symbol": sym, "setup": 5,
            "qty": qty, "entry_stop": plan_row["entry_stop_trigger"],
            "entry_limit": plan_row["entry_limit"], "stop_loss": plan_row["stop_loss"],
            "take_profit": plan_row["take_profit"], "order_id": oid,
        })
        safe_log_stdout(
            f"  QUEUED Setup 5 {sym} qty={qty} stop={plan_row['entry_stop_trigger']} "
            f"limit={plan_row['entry_limit']} SL={plan_row['stop_loss']} "
            f"TP={plan_row['take_profit']}"
        )
    if submitted == 0:
        safe_log_stdout("  no Setup 5 candidates triggered today")


def phase_manage(cfg, trades: dict[str, TradeRecord], date_iso: str,
                 dry_run: bool, fake_now: str | None) -> None:
    """Loop until 10:30 ET — poll fills, attach OCOs, move stops to breakeven."""
    safe_log_stdout("=== 09:31-10:30 ET — manage ===")
    entry_cutoff = et_at(date_iso, cfg["time_cutoff_entry_et"])
    if fake_now:
        # In test mode we run the management loop once.
        poll_entry_fills(cfg, trades, date_iso, dry_run)
        poll_breakeven_moves(cfg, trades, date_iso, dry_run)
        poll_exit_completion(cfg, trades, date_iso, dry_run)
        return
    while et_now() < entry_cutoff:
        try:
            poll_entry_fills(cfg, trades, date_iso, dry_run)
            poll_breakeven_moves(cfg, trades, date_iso, dry_run)
            poll_exit_completion(cfg, trades, date_iso, dry_run)
        except Exception as exc:
            safe_log_stdout(f"  manage-loop exception: {exc}")
            traceback.print_exc()
        time.sleep(2)


def phase_entry_cutoff(cfg, trades: dict[str, TradeRecord],
                       date_iso: str, dry_run: bool) -> None:
    safe_log_stdout("=== 10:30 ET — cancel unfilled entries ===")
    for sym, tr in trades.items():
        if tr.entry_order_id and tr.filled_qty == 0:
            cancel_order(cfg, tr.entry_order_id, dry_run)
            append_fill_event(date_iso, {
                "event": "entry_canceled_time_cutoff",
                "symbol": sym, "order_id": tr.entry_order_id,
            })
            safe_log_stdout(f"  canceled unfilled entry {sym}")


def phase_force_close(cfg, trades: dict[str, TradeRecord],
                      date_iso: str, dry_run: bool, fake_now: str | None) -> None:
    safe_log_stdout("=== 11:00 ET — force close any remaining positions ===")
    force_close_cutoff = et_at(date_iso, cfg["time_cutoff_force_close_et"])
    if not fake_now:
        # Wait until the cutoff to let exits settle naturally.
        sleep_until(force_close_cutoff, fake_now)
    if not dry_run:
        tc = trading_client(cfg)
        try:
            positions = tc.get_all_positions()
        except Exception as exc:
            safe_log_stdout(f"  positions fetch failed: {exc}")
            positions = []
        for p in positions:
            sym = p.symbol
            if sym in trades and not trades[sym].closed:
                force_close_position(cfg, sym, dry_run)
                append_fill_event(date_iso, {
                    "event": "force_close_eod", "symbol": sym,
                    "qty": float(p.qty), "market_value": float(p.market_value),
                })
                safe_log_stdout(f"  force-closed {sym}")


# ---------- Reporting ----------

def render_report(plan, trades: dict[str, TradeRecord],
                  opening_equity: float, closing_equity: float | None) -> str:
    date = plan["date"]
    lines = [f"<b>GUNS daily report — {date}</b>"]
    pnl_dollars = None
    if closing_equity is not None:
        pnl_dollars = closing_equity - opening_equity
        pct = (pnl_dollars / opening_equity * 100) if opening_equity else 0.0
        sign = "+" if pnl_dollars >= 0 else ""
        lines.append(
            f"<i>Equity {fmt_price(opening_equity)} -> {fmt_price(closing_equity)} "
            f"({sign}${pnl_dollars:,.2f} / {sign}{pct:.2f}%)</i>"
        )
    n_eligible = sum(1 for r in plan["tickers"].values() if r.get("setup1_eligible"))
    lines.append(
        f"<i>{len(plan['tickers'])} watchlist · "
        f"Setup 1 eligible {n_eligible} · trades taken {len(trades)}</i>"
    )
    fb_active, fb_reason = fallback_state()
    if fb_active:
        lines.append(f"<i>⚠️ IBKR data unavailable — fell back to Alpaca IEX. Reason: {fb_reason}</i>")
    lines.append("")
    if not trades:
        lines.append("<i>No trades placed today.</i>")
    else:
        for sym, tr in trades.items():
            head = f"<b>{sym}</b> Setup {tr.setup}"
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
                    pieces.append("CLOSED")
                lines.append("  · ".join(pieces))
            if tr.notes:
                lines.append("  notes: " + "; ".join(tr.notes))
        lines.append("")
    return "\n".join(lines)


# ---------- Main ----------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true",
                   help="Plan & log only; do not submit any Alpaca orders.")
    p.add_argument("--auto-scan", action="store_true",
                   help="Build watchlist via Alpaca News API if no file exists.")
    p.add_argument("--watchlist", default=None,
                   help="Inline comma-separated tickers, overrides file.")
    p.add_argument("--replan", action="store_true",
                   help="Rebuild today's plan even if state/plan_<date>.json exists.")
    p.add_argument("--fake-now", default=None,
                   help="ET wall-clock to anchor for testing, HH:MM. "
                        "Disables sleep_until and runs the manage-loop once.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    cfg = load_config()
    STATE_DIR.mkdir(exist_ok=True)
    date_iso = et_today_iso(args.fake_now)

    # 1. Plan
    plan = load_or_build_plan(args, cfg)

    # 2. Opening equity snapshot
    if args.dry_run:
        opening_equity = 100_000.0
        safe_log_stdout(f"[DRY] using fake equity ${opening_equity:,.2f}")
    else:
        opening_equity = get_account_equity(cfg)
        safe_log_stdout(f"Opening equity: {fmt_price(opening_equity)}")
    equity_path(date_iso).write_text(
        json.dumps({"opening": opening_equity}, indent=2), encoding="utf-8"
    )

    # 3. Phase 1 — Setup 1 at 9:25 ET
    setup1_at = et_at(date_iso, "09:25")
    sleep_until(setup1_at, args.fake_now)
    trades: dict[str, TradeRecord] = {}
    phase_setup1(cfg, plan, trades, opening_equity, date_iso, args.dry_run)

    # 4. Phase 2 — Setup 5 at 9:31 ET
    setup5_at = et_at(date_iso, "09:31")
    sleep_until(setup5_at, args.fake_now)
    phase_setup5(cfg, plan, trades, opening_equity, date_iso,
                 args.dry_run, args.fake_now)

    # 5. Phase 3 — manage until 10:30 ET
    phase_manage(cfg, trades, date_iso, args.dry_run, args.fake_now)

    # 6. Phase 4 — entry cutoff at 10:30 ET
    phase_entry_cutoff(cfg, trades, date_iso, args.dry_run)

    # 7. Phase 5 — force close at 11:00 ET
    phase_force_close(cfg, trades, date_iso, args.dry_run, args.fake_now)

    # 8. Report
    closing_equity = None
    if not args.dry_run:
        try:
            closing_equity = get_account_equity(cfg)
        except Exception as exc:
            safe_log_stdout(f"closing equity fetch failed: {exc}")
    equity_state = json.loads(equity_path(date_iso).read_text(encoding="utf-8"))
    equity_state["closing"] = closing_equity
    equity_path(date_iso).write_text(json.dumps(equity_state, indent=2), encoding="utf-8")

    report = render_report(plan, trades, opening_equity, closing_equity)
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
