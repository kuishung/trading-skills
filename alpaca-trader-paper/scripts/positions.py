"""Position management CLI.

Subcommands:
  list        — list open positions
  close       — close a single position (full, by qty, or by percentage)
  close-all   — close every open position
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _client import trading_client
from trade_log import append_event


def _serialize_position(p):
    return {
        "symbol": p.symbol,
        "qty": float(p.qty),
        "side": str(p.side),
        "avg_entry_price": float(p.avg_entry_price),
        "current_price": float(p.current_price) if p.current_price else None,
        "market_value": float(p.market_value),
        "cost_basis": float(p.cost_basis),
        "unrealized_pl": float(p.unrealized_pl),
        "unrealized_plpc": float(p.unrealized_plpc),
        "change_today": float(p.change_today) if p.change_today else None,
        "asset_class": str(p.asset_class),
    }


def list_positions():
    client = trading_client()
    return [_serialize_position(p) for p in client.get_all_positions()]


def cmd_list(args):
    out = list_positions()
    if args.json:
        print(json.dumps(out, indent=2, default=str))
        return
    if not out:
        print("(no open positions)")
        return
    print(f"{'symbol':<7} {'side':<6} {'qty':>10} {'avg_entry':>10} {'current':>10} "
          f"{'mkt_value':>12} {'unreal_pl':>12} {'pl_%':>8}")
    for p in out:
        print(f"{p['symbol']:<7} {p['side'][-6:]:<6} {p['qty']:>10.2f} "
              f"{p['avg_entry_price']:>10.2f} "
              f"{(p['current_price'] or 0):>10.2f} "
              f"{p['market_value']:>12,.2f} "
              f"{p['unrealized_pl']:>12,.2f} "
              f"{p['unrealized_plpc']*100:>7.2f}%")


def cmd_close(args):
    from alpaca.trading.requests import ClosePositionRequest

    symbol = args.symbol.upper()
    if args.qty is not None and args.pct is not None:
        sys.exit("Pass --qty OR --pct, not both.")

    client = trading_client()
    if args.qty is not None:
        req = ClosePositionRequest(qty=str(args.qty))
    elif args.pct is not None:
        req = ClosePositionRequest(percentage=str(args.pct))
    else:
        req = None  # full close

    if args.dry_run:
        kind = "qty=" + str(args.qty) if args.qty is not None else (
            "pct=" + str(args.pct) if args.pct is not None else "full"
        )
        print(f"[DRY RUN] would close position {symbol} ({kind})")
        return

    order = client.close_position(symbol_or_asset_id=symbol, close_options=req)
    payload = {
        "symbol": symbol,
        "close_qty": args.qty,
        "close_pct": args.pct,
        "resulting_order_id": str(order.id) if order else None,
    }
    append_event("position_close_requested", payload)
    print(json.dumps(payload, indent=2, default=str))


def cmd_close_all(args):
    if args.dry_run:
        positions = list_positions()
        print(f"[DRY RUN] would close {len(positions)} position(s):")
        for p in positions:
            print(f"    {p['symbol']}  qty={p['qty']}  mkt_value=${p['market_value']:,.2f}")
        return

    client = trading_client()
    responses = client.close_all_positions(cancel_orders=args.cancel_orders)
    append_event("positions_close_all", {
        "count": len(responses),
        "cancel_orders": args.cancel_orders,
    })
    print(f"Requested close on {len(responses)} position(s)")
    if args.json:
        print(json.dumps(
            [{"symbol": r.symbol, "status": r.status} for r in responses],
            indent=2, default=str,
        ))


def build_parser():
    p = argparse.ArgumentParser(description="Manage Alpaca paper positions")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_close = sub.add_parser("close")
    p_close.add_argument("symbol")
    p_close.add_argument("--qty", type=float, default=None,
                         help="Close exactly this many shares")
    p_close.add_argument("--pct", type=float, default=None,
                         help="Close this percentage of the position (0-100)")
    p_close.add_argument("--dry-run", action="store_true")
    p_close.set_defaults(func=cmd_close)

    p_all = sub.add_parser("close-all")
    p_all.add_argument("--cancel-orders", action="store_true",
                       help="Also cancel any open orders before closing")
    p_all.add_argument("--dry-run", action="store_true")
    p_all.add_argument("--json", action="store_true")
    p_all.set_defaults(func=cmd_close_all)

    return p


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
