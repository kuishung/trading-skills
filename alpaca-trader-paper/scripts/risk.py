"""Pre-trade risk checks.

These run *before* an order is sent to Alpaca. A failed check raises
RiskCheckError with a human-readable message; orders.py turns this into
an exit-code-1 failure with no API call made.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _client import load_config, trading_client


class RiskCheckError(Exception):
    pass


def _estimate_notional(symbol, qty, side, limit_price=None):
    """Best-effort notional estimate for sizing checks.

    Uses limit_price if provided; otherwise pulls a latest quote and uses the
    ask (for buys) or bid (for sells). If quote lookup fails, returns None
    and the caller treats sizing as unverifiable.
    """
    if limit_price is not None:
        return float(limit_price) * float(qty)
    try:
        from market_data import get_latest_quote
        q = get_latest_quote(symbol)
        ref = q["ask_price"] if side == "buy" else q["bid_price"]
        if ref and ref > 0:
            return ref * float(qty)
    except Exception:
        return None
    return None


def check_market_hours(extended_hours):
    """Reject trades outside RTH unless --extended-hours was passed."""
    if extended_hours:
        return
    client = trading_client()
    if not client.get_clock().is_open:
        raise RiskCheckError(
            "Market is closed. Pass --extended-hours to attempt the order "
            "anyway (note: not all order types support extended hours)."
        )


def check_max_open_positions(max_open=None):
    """Reject if opening another position would exceed the cap."""
    cfg = load_config()
    cap = max_open if max_open is not None else cfg["max_open_positions"]
    client = trading_client()
    open_count = len(client.get_all_positions())
    if open_count >= cap:
        raise RiskCheckError(
            f"Already at max open positions: {open_count} >= {cap}. "
            f"Close something or raise --max-open-positions."
        )


def check_position_size(symbol, qty, side, limit_price=None, max_pct=None):
    """Reject if this order would push exposure in `symbol` above max_pct of equity.

    The check is approximate — it uses current quote * qty as the order's
    notional and compares (existing position notional + order notional) to
    the equity cap. Sells reduce exposure, so we skip the check for sells.
    """
    if side != "buy":
        return  # selling reduces exposure; cap only applies to buys

    cfg = load_config()
    cap_pct = max_pct if max_pct is not None else cfg["max_position_pct"]

    client = trading_client()
    account = client.get_account()
    equity = float(account.equity)
    cap_dollars = equity * cap_pct

    # Existing exposure in this symbol, if any.
    existing_notional = 0.0
    for p in client.get_all_positions():
        if p.symbol == symbol.upper():
            existing_notional = abs(float(p.market_value))
            break

    order_notional = _estimate_notional(symbol, qty, side, limit_price)
    if order_notional is None:
        raise RiskCheckError(
            f"Could not estimate order notional for {symbol} (no recent quote). "
            f"Pass --limit-price to force a deterministic size check, or "
            f"--max-position-pct to override the cap explicitly."
        )

    projected = existing_notional + order_notional
    if projected > cap_dollars:
        raise RiskCheckError(
            f"Order rejected — would exceed max position size.\n"
            f"  symbol            : {symbol}\n"
            f"  existing_notional : ${existing_notional:,.2f}\n"
            f"  order_notional    : ${order_notional:,.2f}\n"
            f"  projected         : ${projected:,.2f}\n"
            f"  cap ({cap_pct:.0%} of equity) : ${cap_dollars:,.2f}\n"
            f"Reduce --qty, raise --max-position-pct, or close the existing position."
        )


def run_pre_trade_checks(
    symbol,
    qty,
    side,
    limit_price=None,
    extended_hours=False,
    max_position_pct=None,
    max_open_positions=None,
    is_new_position=True,
):
    """All-in-one pre-trade gate. Raises RiskCheckError on any failure."""
    check_market_hours(extended_hours)
    if is_new_position and side == "buy":
        check_max_open_positions(max_open_positions)
    check_position_size(symbol, qty, side, limit_price, max_position_pct)
