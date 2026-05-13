"""Market data CLI: latest quote, recent bars, market clock.

Usage:
  python scripts/market_data.py quote AAPL
  python scripts/market_data.py bars AAPL --timeframe 1Day --limit 30
  python scripts/market_data.py bars AAPL --timeframe 5Min --limit 100 --json
  python scripts/market_data.py clock
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _client import market_data_client, trading_client


TIMEFRAME_MAP = {
    "1Min": ("Minute", 1),
    "5Min": ("Minute", 5),
    "15Min": ("Minute", 15),
    "1Hour": ("Hour", 1),
    "1Day": ("Day", 1),
}


def _build_timeframe(label):
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    if label not in TIMEFRAME_MAP:
        sys.exit(
            f"Unknown timeframe '{label}'. Supported: {', '.join(TIMEFRAME_MAP)}"
        )
    unit_name, amount = TIMEFRAME_MAP[label]
    unit = getattr(TimeFrameUnit, unit_name)
    return TimeFrame(amount, unit)


def get_latest_quote(symbol):
    from alpaca.data.requests import StockLatestQuoteRequest

    client = market_data_client()
    req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
    quotes = client.get_stock_latest_quote(req)
    q = quotes[symbol]
    return {
        "symbol": symbol,
        "bid_price": float(q.bid_price),
        "bid_size": int(q.bid_size),
        "ask_price": float(q.ask_price),
        "ask_size": int(q.ask_size),
        "timestamp": q.timestamp.isoformat() if q.timestamp else None,
    }


def get_bars(symbol, timeframe_label, limit):
    from alpaca.data.requests import StockBarsRequest

    client = market_data_client()
    tf = _build_timeframe(timeframe_label)

    # Bound the lookback so we can request "limit" recent bars regardless of TF.
    now = datetime.now(timezone.utc)
    if timeframe_label == "1Day":
        start = now - timedelta(days=max(limit * 2, 30))
    elif timeframe_label == "1Hour":
        start = now - timedelta(hours=max(limit * 2, 24))
    else:
        # Minute-level: pull a generous window, slice to `limit` at the end.
        start = now - timedelta(days=max((limit // 78) + 5, 5))

    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=tf,
        start=start,
        end=now,
        limit=limit * 4,  # Alpaca may return more than asked; we trim below.
    )
    resp = client.get_stock_bars(req)
    bars = resp.data.get(symbol, [])
    bars = bars[-limit:]
    return [
        {
            "timestamp": b.timestamp.isoformat(),
            "open": float(b.open),
            "high": float(b.high),
            "low": float(b.low),
            "close": float(b.close),
            "volume": int(b.volume),
            "trade_count": int(b.trade_count) if b.trade_count is not None else None,
            "vwap": float(b.vwap) if b.vwap is not None else None,
        }
        for b in bars
    ]


def get_clock():
    client = trading_client()
    c = client.get_clock()
    return {
        "timestamp": c.timestamp.isoformat() if c.timestamp else None,
        "is_open": bool(c.is_open),
        "next_open": c.next_open.isoformat() if c.next_open else None,
        "next_close": c.next_close.isoformat() if c.next_close else None,
    }


def main():
    parser = argparse.ArgumentParser(description="Alpaca market data")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_quote = sub.add_parser("quote", help="Latest quote for a symbol")
    p_quote.add_argument("symbol")
    p_quote.add_argument("--json", action="store_true")

    p_bars = sub.add_parser("bars", help="Recent OHLCV bars")
    p_bars.add_argument("symbol")
    p_bars.add_argument(
        "--timeframe",
        default="1Day",
        choices=list(TIMEFRAME_MAP.keys()),
    )
    p_bars.add_argument("--limit", type=int, default=30)
    p_bars.add_argument("--json", action="store_true")

    p_clock = sub.add_parser("clock", help="Market clock (is_open, next open/close)")
    p_clock.add_argument("--json", action="store_true")

    args = parser.parse_args()

    if args.cmd == "quote":
        out = get_latest_quote(args.symbol.upper())
        if args.json:
            print(json.dumps(out, indent=2))
        else:
            print(f"{out['symbol']}  bid {out['bid_price']} x {out['bid_size']}  "
                  f"ask {out['ask_price']} x {out['ask_size']}  @ {out['timestamp']}")

    elif args.cmd == "bars":
        out = get_bars(args.symbol.upper(), args.timeframe, args.limit)
        if args.json:
            print(json.dumps(out, indent=2))
        else:
            print(f"{args.symbol.upper()} {args.timeframe}  ({len(out)} bars)")
            print(f"{'timestamp':<27} {'open':>9} {'high':>9} {'low':>9} {'close':>9} {'volume':>12}")
            for b in out:
                print(f"{b['timestamp']:<27} {b['open']:>9.2f} {b['high']:>9.2f} "
                      f"{b['low']:>9.2f} {b['close']:>9.2f} {b['volume']:>12,}")

    elif args.cmd == "clock":
        out = get_clock()
        if args.json:
            print(json.dumps(out, indent=2))
        else:
            state = "OPEN" if out["is_open"] else "CLOSED"
            print(f"Market is {state}")
            print(f"  now        : {out['timestamp']}")
            print(f"  next_open  : {out['next_open']}")
            print(f"  next_close : {out['next_close']}")


if __name__ == "__main__":
    main()
