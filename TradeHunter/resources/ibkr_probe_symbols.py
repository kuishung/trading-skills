"""One-shot diagnostic: why do certain symbols return +0 bars on ingest?

Read-only. clientId 98 (probe id — never collides with 71 bot / 80 observer /
82 GUNS / 83 laptop ingest / 84 Hermes ingest / 99 dashboard). PAPER port.

For each symbol it:
  1. reqContractDetails on the RAW symbol  ("BRK.B", "BK", ...)
  2. reqContractDetails on the dot->space variant ("BRK B")  if it differs
  3. reqMatchingSymbols(symbol) — IBKR's OWN fuzzy symbol search — to show
     what contract(s) actually exist for that ticker (conId / primaryExchange
     / secType), which distinguishes "needs primaryExchange" from "throttled"
     from "genuinely absent".
  4. if a US-stock match is found, retries reqContractDetails WITH the
     match's primaryExchange, then a tiny 5-D daily TRADES pull.

Requests are PACED (default 1.5s) so a consecutive-request throttle on
reqContractDetails (IBKR error 200 returned spuriously under burst) can't be
mistaken for a genuine "no security definition".

Run (Laptop OR Hermes — wherever IB Gateway paper is reachable):
    py -3.12 resources/ibkr_probe_symbols.py
    py -3.12 resources/ibkr_probe_symbols.py BK ASGN BRK.B     # custom list
    py -3.12 resources/ibkr_probe_symbols.py --pace 3.0 BK     # slower pacing
"""
from __future__ import annotations

import sys
import time
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


def _matching(ib, symbol: str):
    """reqMatchingSymbols — IBKR's fuzzy search. Returns list of ContractDescription."""
    try:
        return ib.reqMatchingSymbols(symbol.upper())
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"


def _details_px(ib, symbol: str, primary: str):
    """reqContractDetails with an explicit primaryExchange."""
    from ib_insync import Stock
    try:
        c = Stock(symbol.upper(), "SMART", "USD")
        c.primaryExchange = primary
        return ib.reqContractDetails(c)
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"


def _hist_by_conid(ib, conid: int) -> str:
    """Qualify a Contract by conId (exchange=SMART) and try a 5-D daily pull.
    This routes history via SMART instead of the dataless 'VALUE' placeholder."""
    from ib_insync import Contract
    try:
        c = Contract(conId=conid, exchange="SMART", currency="USD")
        ib.qualifyContracts(c)
        return f"qualified={c.symbol or '?'}/{c.primaryExchange or '?'} | {_hist_count(ib, c)}"
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {str(exc)[:120]}"


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


def probe(symbol: str, ib, pace: float) -> None:
    print(f"\n=== {symbol} ===")
    variants = [symbol]
    if "." in symbol:
        variants.append(symbol.replace(".", " "))   # dot -> space (IBKR class format)

    resolved = False
    for v in variants:
        det = _details(ib, v)
        time.sleep(pace)
        if isinstance(det, str):
            print(f"  cd {v!r:12} -> {det}")
            continue
        print(f"  cd {v!r:12} -> {len(det)} contract(s)")
        for d in det[:6]:
            c = d.contract
            print(f"        conId={c.conId:<10} sym={c.symbol:<6} "
                  f"local={c.localSymbol:<8} primary={c.primaryExchange or '-':<8} "
                  f"exch={c.exchange}")
        if det:
            resolved = True
            print(f"        hist({v!r}): {_hist_count(ib, det[0].contract)}")
            time.sleep(pace)

    # If neither raw nor dot->space resolved, ask IBKR what it actually has.
    if not resolved:
        query = symbol.replace(".", " ")
        m = _matching(ib, query)
        time.sleep(pace)
        if isinstance(m, str):
            print(f"  matchingSymbols -> {m}")
            return
        # EXACT-symbol US stock matches only (avoid fuzzy junk like BK->BKNG).
        exact = [d for d in m
                 if getattr(d.contract, "secType", "") == "STK"
                 and getattr(d.contract, "currency", "") == "USD"
                 and getattr(d.contract, "symbol", "").upper() == query.upper()]
        print(f"  matchingSymbols({query!r}) -> {len(m)} match(es), {len(exact)} EXACT US stock(s)")
        for d in m[:8]:
            c = d.contract
            print(f"        sym={c.symbol:<8} secType={c.secType:<5} "
                  f"primary={c.primaryExchange or '-':<8} cur={c.currency} conId={c.conId}")
        # The fix hypothesis: take the exact match's conId and request history
        # via SMART (NOT the 'VALUE' placeholder exchange).
        if exact:
            conid = exact[0].contract.conId
            print(f"  -> conId={conid} via SMART:")
            print(f"        hist(conId,SMART): {_hist_by_conid(ib, conid)}")
        else:
            print("  (no exact US-stock match — genuinely absent / renamed in IBKR)")


def main() -> int:
    argv = [a for a in sys.argv[1:]]
    pace = 1.5
    if "--pace" in argv:
        i = argv.index("--pace")
        try:
            pace = float(argv[i + 1])
            del argv[i:i + 2]
        except (IndexError, ValueError):
            print("--pace needs a number"); return 2
    syms = [s.upper() for s in argv] or DEFAULT_SYMBOLS
    cfg = dict(load_config() or {})
    cfg["ibkr_client_id"] = PROBE_CLIENT_ID
    print(f"# probing {len(syms)} symbols on "
          f"{cfg.get('ibkr_host','127.0.0.1')}:{cfg.get('ibkr_port',4002)} "
          f"(clientId={PROBE_CLIENT_ID}, read-only, pace={pace}s)")
    try:
        ib = ibkr_data._connect(cfg)
    except Exception as exc:
        print(f"FATAL: {exc}")
        return 2
    try:
        for s in syms:
            probe(s, ib, pace)
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass
    print("\n# done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
