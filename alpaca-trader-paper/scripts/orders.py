"""Order placement and management CLI.

Subcommands:
  market       — place a market order
  limit        — place a limit order
  stop         — place a stop order
  stop-limit   — place a stop-limit order
  bracket      — place a bracket order (entry + take-profit + stop-loss)
  list         — list orders (default: open)
  cancel       — cancel a single order by id
  cancel-all   — cancel all open orders

All placement commands accept --dry-run to print the request without sending.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _client import trading_client
from risk import RiskCheckError, run_pre_trade_checks
from trade_log import append_event


def _side_enum(side):
    from alpaca.trading.enums import OrderSide
    return OrderSide.BUY if side == "buy" else OrderSide.SELL


def _tif_enum(tif):
    from alpaca.trading.enums import TimeInForce
    return getattr(TimeInForce, tif.upper())


def _serialize_order(o):
    """Convert an alpaca-py Order object to a JSON-friendly dict."""
    return {
        "id": str(o.id),
        "client_order_id": o.client_order_id,
        "symbol": o.symbol,
        "qty": float(o.qty) if o.qty is not None else None,
        "filled_qty": float(o.filled_qty) if o.filled_qty is not None else 0.0,
        "side": str(o.side),
        "type": str(o.order_type),
        "time_in_force": str(o.time_in_force),
        "status": str(o.status),
        "limit_price": float(o.limit_price) if o.limit_price is not None else None,
        "stop_price": float(o.stop_price) if o.stop_price is not None else None,
        "filled_avg_price": float(o.filled_avg_price) if o.filled_avg_price else None,
        "submitted_at": o.submitted_at.isoformat() if o.submitted_at else None,
        "filled_at": o.filled_at.isoformat() if o.filled_at else None,
        "extended_hours": bool(o.extended_hours),
    }


def _print_dry_run(kind, **fields):
    print(f"[DRY RUN] would submit {kind} order:")
    for k, v in fields.items():
        print(f"    {k}: {v}")


def _submit(req, kind, dry_run, dry_run_fields):
    if dry_run:
        _print_dry_run(kind, **dry_run_fields)
        return
    client = trading_client()
    order = client.submit_order(req)
    out = _serialize_order(order)
    append_event("order_submitted", out)
    print(json.dumps(out, indent=2, default=str))


def cmd_market(args):
    from alpaca.trading.requests import MarketOrderRequest

    symbol = args.symbol.upper()
    if not args.dry_run:
        try:
            run_pre_trade_checks(
                symbol=symbol,
                qty=args.qty,
                side=args.side,
                extended_hours=args.extended_hours,
                max_position_pct=args.max_position_pct,
                max_open_positions=args.max_open_positions,
            )
        except RiskCheckError as e:
            sys.exit(f"Risk check failed:\n{e}")

    req = MarketOrderRequest(
        symbol=symbol,
        qty=args.qty,
        side=_side_enum(args.side),
        time_in_force=_tif_enum(args.time_in_force),
        extended_hours=args.extended_hours,
    )
    _submit(req, "market", args.dry_run, {
        "symbol": symbol, "qty": args.qty, "side": args.side,
        "time_in_force": args.time_in_force, "extended_hours": args.extended_hours,
    })


def cmd_limit(args):
    from alpaca.trading.requests import LimitOrderRequest

    symbol = args.symbol.upper()
    if not args.dry_run:
        try:
            run_pre_trade_checks(
                symbol=symbol,
                qty=args.qty,
                side=args.side,
                limit_price=args.limit_price,
                extended_hours=args.extended_hours,
                max_position_pct=args.max_position_pct,
                max_open_positions=args.max_open_positions,
            )
        except RiskCheckError as e:
            sys.exit(f"Risk check failed:\n{e}")

    req = LimitOrderRequest(
        symbol=symbol,
        qty=args.qty,
        side=_side_enum(args.side),
        time_in_force=_tif_enum(args.time_in_force),
        limit_price=args.limit_price,
        extended_hours=args.extended_hours,
    )
    _submit(req, "limit", args.dry_run, {
        "symbol": symbol, "qty": args.qty, "side": args.side,
        "limit_price": args.limit_price, "time_in_force": args.time_in_force,
        "extended_hours": args.extended_hours,
    })


def cmd_stop(args):
    from alpaca.trading.requests import StopOrderRequest

    symbol = args.symbol.upper()
    if not args.dry_run:
        try:
            run_pre_trade_checks(
                symbol=symbol,
                qty=args.qty,
                side=args.side,
                extended_hours=args.extended_hours,
                max_position_pct=args.max_position_pct,
                max_open_positions=args.max_open_positions,
            )
        except RiskCheckError as e:
            sys.exit(f"Risk check failed:\n{e}")

    req = StopOrderRequest(
        symbol=symbol,
        qty=args.qty,
        side=_side_enum(args.side),
        time_in_force=_tif_enum(args.time_in_force),
        stop_price=args.stop_price,
    )
    _submit(req, "stop", args.dry_run, {
        "symbol": symbol, "qty": args.qty, "side": args.side,
        "stop_price": args.stop_price, "time_in_force": args.time_in_force,
    })


def cmd_stop_limit(args):
    from alpaca.trading.requests import StopLimitOrderRequest

    symbol = args.symbol.upper()
    if not args.dry_run:
        try:
            run_pre_trade_checks(
                symbol=symbol,
                qty=args.qty,
                side=args.side,
                limit_price=args.limit_price,
                extended_hours=args.extended_hours,
                max_position_pct=args.max_position_pct,
                max_open_positions=args.max_open_positions,
            )
        except RiskCheckError as e:
            sys.exit(f"Risk check failed:\n{e}")

    req = StopLimitOrderRequest(
        symbol=symbol,
        qty=args.qty,
        side=_side_enum(args.side),
        time_in_force=_tif_enum(args.time_in_force),
        stop_price=args.stop_price,
        limit_price=args.limit_price,
    )
    _submit(req, "stop-limit", args.dry_run, {
        "symbol": symbol, "qty": args.qty, "side": args.side,
        "stop_price": args.stop_price, "limit_price": args.limit_price,
        "time_in_force": args.time_in_force,
    })


def cmd_bracket(args):
    from alpaca.trading.requests import (
        MarketOrderRequest, LimitOrderRequest, TakeProfitRequest, StopLossRequest,
    )
    from alpaca.trading.enums import OrderClass

    symbol = args.symbol.upper()
    if not args.dry_run:
        try:
            run_pre_trade_checks(
                symbol=symbol,
                qty=args.qty,
                side=args.side,
                limit_price=args.limit_price,
                extended_hours=False,
                max_position_pct=args.max_position_pct,
                max_open_positions=args.max_open_positions,
            )
        except RiskCheckError as e:
            sys.exit(f"Risk check failed:\n{e}")

    take_profit = TakeProfitRequest(limit_price=args.take_profit)
    stop_loss = StopLossRequest(stop_price=args.stop_loss)

    common = dict(
        symbol=symbol,
        qty=args.qty,
        side=_side_enum(args.side),
        time_in_force=_tif_enum(args.time_in_force),
        order_class=OrderClass.BRACKET,
        take_profit=take_profit,
        stop_loss=stop_loss,
    )
    if args.limit_price is not None:
        req = LimitOrderRequest(limit_price=args.limit_price, **common)
        kind = "bracket-limit"
    else:
        req = MarketOrderRequest(**common)
        kind = "bracket-market"

    _submit(req, kind, args.dry_run, {
        "symbol": symbol, "qty": args.qty, "side": args.side,
        "entry": ("limit " + str(args.limit_price)) if args.limit_price else "market",
        "take_profit": args.take_profit, "stop_loss": args.stop_loss,
        "time_in_force": args.time_in_force,
    })


def cmd_list(args):
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus

    status_map = {
        "open": QueryOrderStatus.OPEN,
        "closed": QueryOrderStatus.CLOSED,
        "all": QueryOrderStatus.ALL,
    }
    client = trading_client()
    req = GetOrdersRequest(status=status_map[args.status], limit=args.limit)
    orders = client.get_orders(filter=req)
    out = [_serialize_order(o) for o in orders]
    if args.json:
        print(json.dumps(out, indent=2, default=str))
        return
    if not out:
        print(f"(no {args.status} orders)")
        return
    print(f"{'submitted_at':<27} {'id':<38} {'symbol':<7} {'side':<4} "
          f"{'type':<11} {'qty':>8} {'status':<12}")
    for o in out:
        print(f"{str(o['submitted_at']):<27} {o['id']:<38} {o['symbol']:<7} "
              f"{o['side'][-4:]:<4} {o['type'][-11:]:<11} {o['qty']:>8} {o['status'][-12:]:<12}")


def cmd_cancel(args):
    client = trading_client()
    client.cancel_order_by_id(args.order_id)
    append_event("order_canceled", {"order_id": args.order_id})
    print(f"Canceled order {args.order_id}")


def cmd_cancel_all(args):
    client = trading_client()
    responses = client.cancel_orders()
    append_event("orders_cancel_all", {"count": len(responses)})
    print(f"Canceled {len(responses)} order(s)")
    if args.json:
        print(json.dumps(
            [{"id": str(r.id), "status": r.status} for r in responses],
            indent=2, default=str,
        ))


def build_parser():
    p = argparse.ArgumentParser(description="Place and manage Alpaca paper orders")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common_placement(sp, needs_side=True):
        sp.add_argument("symbol")
        sp.add_argument("--qty", type=float, required=True)
        if needs_side:
            sp.add_argument("--side", choices=["buy", "sell"], required=True)
        sp.add_argument("--time-in-force", default="day",
                        choices=["day", "gtc", "ioc", "fok", "opg", "cls"])
        sp.add_argument("--extended-hours", action="store_true")
        sp.add_argument("--max-position-pct", type=float, default=None)
        sp.add_argument("--max-open-positions", type=int, default=None)
        sp.add_argument("--dry-run", action="store_true")

    p_market = sub.add_parser("market")
    add_common_placement(p_market)
    p_market.set_defaults(func=cmd_market)

    p_limit = sub.add_parser("limit")
    add_common_placement(p_limit)
    p_limit.add_argument("--limit-price", type=float, required=True)
    p_limit.set_defaults(func=cmd_limit)

    p_stop = sub.add_parser("stop")
    add_common_placement(p_stop)
    p_stop.add_argument("--stop-price", type=float, required=True)
    p_stop.set_defaults(func=cmd_stop)

    p_sl = sub.add_parser("stop-limit")
    add_common_placement(p_sl)
    p_sl.add_argument("--stop-price", type=float, required=True)
    p_sl.add_argument("--limit-price", type=float, required=True)
    p_sl.set_defaults(func=cmd_stop_limit)

    p_br = sub.add_parser("bracket")
    add_common_placement(p_br)
    p_br.add_argument("--take-profit", type=float, required=True,
                      help="Take-profit limit price")
    p_br.add_argument("--stop-loss", type=float, required=True,
                      help="Stop-loss stop price")
    p_br.add_argument("--limit-price", type=float, default=None,
                      help="If set, entry is a limit order at this price; "
                           "otherwise entry is a market order")
    p_br.set_defaults(func=cmd_bracket)

    p_list = sub.add_parser("list")
    p_list.add_argument("--status", choices=["open", "closed", "all"], default="open")
    p_list.add_argument("--limit", type=int, default=50)
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_cancel = sub.add_parser("cancel")
    p_cancel.add_argument("order_id")
    p_cancel.set_defaults(func=cmd_cancel)

    p_cancel_all = sub.add_parser("cancel-all")
    p_cancel_all.add_argument("--json", action="store_true")
    p_cancel_all.set_defaults(func=cmd_cancel_all)

    return p


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
