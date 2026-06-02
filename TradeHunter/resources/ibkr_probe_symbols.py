"""One-shot diagnostic: why do certain symbols return +0 bars on ingest?

Read-only. clientId 98 (probe id — never collides with 71 bot / 80 observer /
82 GUNS / 83 laptop ingest / 84 Hermes ingest / 99 dashboard). PAPER port.

For each symbol it:
  1. reqContractDetails on the RAW symbol  ("BRK.B", "BK", ...)
  2. reqContractDetails on the dot->space variant ("BRK B")  if it differs
  3. reports how many contracts matched (0 = unknown, >1 = AMBIGUOUS) and the
     conId / primaryExchange of each candidate
  4. attempts a tiny reqHistoricalData (5 D daily, TRADES) on the best
     qualified contract and prints bar count or the IBKR error

This isolates the three hypotheses for the stuck-seed set:
  - dotted share classes  -> raw fails, dot->space succeeds      (code bug)
  - ambiguous SMART        -> >1 contract, qualify can't pin one  (needs primaryExchange)
  - genuinely no data      -> 1 contract qualifies but hist is empty / errors

Run (Laptop OR Hermes — wherever IB Gateway paper is reachable):
    py -3.12 resources/ibkr_probe_symbols.py
    py -3.12 resources/ibkr_probe_symbols.py BK ASGN BRK.B     # custom list
"""
from __future__ import annotations

import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
for _p in [str(_root)] + [str(_root / s) for s in ("scripts", "resources")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _common import load_config            # noqa: E402
import ibkr_data                            # noqa: E402

# The stuck set observed in the ingest logs (return +0 every run).
DEFAULT_SYMBOLS = [
    # dotted share classes (hypothesis: dot-vs-space)
    "BF.B", "BRK.B", "CWEN.A", "MOG.A",
    # plain tickers that also return +0 (hypothesis: ambiguous / no data)
    "ASGN", "BK", "CSGS", "EXPI", "MCW", "PSTG", "SNCY",
    # a known-good control that DID seed fine, for comparison
    "TSLA",
]

PROBE_CLIENT_ID = 98


def _details(ib, symbol: str):
    """reqContractDetails for a SMART US stock. Returns list of ContractDetails."""
    from ib_insync import Stock
    try:
        return ib.reqContractDetails(Stock(symbol.upper(), "SMART", "USD"))
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"


def _hist_count(ib, contract) -> str:
    """Tiny 5-day daily TRADES pull on an explicit contract; report outcome."""
    try:
        bars = ib.reqHistoricalData(
            contract, endDateTime="", durationStr="5 D",
            barSizeSetting="1 day", whatToShow="TRADES",
            useRTH=True, formatDate=2, keepUpToDate=False,
        )
        return f"{len(bars or [])} daily bars"
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {str(exc)[:120]}"


def probe(symbol: str, ib) -> None:
    print(f"\n=== {symbol} ===")
    variants = [symbol]
    if "." in symbol:
        variants.append(symbol.replace(".", " "))   # dot -> space (IBKR class format)

    for v in variants:
        det = _details(ib, v)
        if isinstance(det, str):
            print(f"  query {v!r:12} -> {det}")
            continue
        print(f"  query {v!r:12} -> {len(det)} contract(s)")
        for d in det[:6]:
            c = d.contract
            print(f"        conId={c.conId:<10} sym={c.symbol:<6} "
                  f"local={c.localSymbol:<8} primary={c.primaryExchange or '-':<8} "
                  f"exch={c.exchange}")
        # historical probe on the FIRST qualified candidate
        if det:
            print(f"        hist({v!r}): {_hist_count(ib, det[0].contract)}")


def main() -> int:
    syms = [s.upper() for s in sys.argv[1:]] or DEFAULT_SYMBOLS
    cfg = dict(load_config() or {})
    cfg["ibkr_client_id"] = PROBE_CLIENT_ID
    print(f"# probing {len(syms)} symbols on "
          f"{cfg.get('ibkr_host','127.0.0.1')}:{cfg.get('ibkr_port',4002)} "
          f"(clientId={PROBE_CLIENT_ID}, read-only)")
    try:
        ib = ibkr_data._connect(cfg)
    except Exception as exc:
        print(f"FATAL: {exc}")
        return 2
    try:
        for s in syms:
            probe(s, ib)
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass
    print("\n# done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
