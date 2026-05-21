"""IBKR data adapter for intraday_bot.

Wraps ib_insync to expose the same shape as _common.py's data abstractions:
    ibkr_pm_bars()                  -> dict[str, list[bar_dict]]
    ibkr_full_day_minute_bars()     -> dict[str, list[bar_dict]]
    ibkr_latest_quote()             -> dict[str, quote_dict]
    ibkr_latest_trade()             -> dict[str, trade_dict]

Connection enforcement:
- Requires Read-Only API mode to be enabled in IB Gateway / TWS. We surface a
  clear error if `clientId` matches a session that has order-placement perms.
  (We can detect this by attempting to call `reqGlobalCancel` in a smoke test;
  it raises if read-only is engaged. But we don't run that automatically —
  this is purely a data adapter, never sends orders.)
- Connects to the PAPER port (7497) by default. The cfg['ibkr_port'] override
  is required for any non-paper port (i.e. 7496 live). We refuse to connect
  to live port without an explicit acknowledgement flag in config because the
  whole point of this skill is paper-only.

Connection lifetime:
- Each public call manages its own connection (connect-fetch-disconnect).
  This is simpler than holding a long-lived connection and safe given the
  bot's polling cadence (every 2s during manage, once per phase otherwise).
- ib_insync uses async under the hood; we wrap each public function so the
  event loop is created/torn-down each call. For higher cadence we'd hold a
  long-lived connection, but the existing 2s cadence on small symbol lists
  is well within IBKR's pacing limits.
"""
from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Lazy import — ib_insync is a heavy dep; only required when data_provider=ibkr.
# Python 3.14 removed the implicit event-loop creation that eventkit (an
# ib_insync transitive dep) relies on. We install a default loop first so the
# eventkit import doesn't crash. The shim is harmless on older Pythons.
try:
    import asyncio as _asyncio
    try:
        _asyncio.get_event_loop()
    except (RuntimeError, DeprecationWarning):
        _loop = _asyncio.new_event_loop()
        _asyncio.set_event_loop(_loop)
    from ib_insync import IB, Stock, util as ib_util
except ImportError:
    IB = None  # type: ignore
except RuntimeError as _ib_exc:
    # eventkit incompat on a Python release. Surface a clear hint rather than
    # leaving an opaque traceback in the user's stack.
    import sys as _sys
    _sys.stderr.write(
        f"\nib_insync import failed: {_ib_exc}\n"
        "This usually means a Python / eventkit compatibility issue. Try:\n"
        "  py -m pip install -U eventkit ib_insync\n"
        "If you're on Python 3.14, downgrading to 3.12 or 3.13 may also help.\n"
    )
    IB = None  # type: ignore

SKILL_DIR = Path(__file__).resolve().parent.parent   # intraday-bot/

# Port mapping:
#   IB Gateway: 4001 (live) / 4002 (paper)  <-- the bot defaults to these
#   TWS:        7496 (live) / 7497 (paper)
# Set cfg["ibkr_port"] explicitly if you're running TWS instead of Gateway.
PAPER_PORT = 4002
LIVE_PORT = 4001
DEFAULT_HOST = "127.0.0.1"
DEFAULT_CLIENT_ID = 71  # arbitrary, just must be unique per concurrent connection


def _et_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except ImportError:
        import pytz
        return pytz.timezone("America/New_York")


def _check_lib() -> None:
    if IB is None:
        sys.exit(
            "ib_insync not installed. Install it:\n"
            "  py -m pip install ib_insync\n"
            "Then ensure IB Gateway (paper) is running on 127.0.0.1:7497."
        )


def _connect(cfg: dict) -> "IB":
    """Open an IBKR connection. Refuses live port unless explicitly allowed."""
    _check_lib()
    host = cfg.get("ibkr_host", DEFAULT_HOST)
    port = int(cfg.get("ibkr_port", PAPER_PORT))
    client_id = int(cfg.get("ibkr_client_id", DEFAULT_CLIENT_ID))
    allow_live = bool(cfg.get("ibkr_allow_live_port", False))
    # Reject the well-known live ports (Gateway 4001, TWS 7496) unless explicit.
    LIVE_PORTS = {4001, 7496}
    if port in LIVE_PORTS and not allow_live:
        sys.exit(
            f"Refusing to connect to IBKR live port {port}. "
            f"This bot is paper-only. Use {PAPER_PORT} (Gateway paper) or 7497 "
            f"(TWS paper) in config.json's ibkr_port, or set "
            "ibkr_allow_live_port=true if you really mean to read live data "
            "(still no orders are sent — execution is Alpaca paper)."
        )
    ib = IB()
    try:
        ib.connect(host, port, clientId=client_id, timeout=8, readonly=True)
    except Exception as exc:
        sys.exit(
            f"Could not connect to IB Gateway at {host}:{port} "
            f"(clientId={client_id}): {exc}\n"
            f"Checks:\n"
            f"  1. IB Gateway is running and logged in (paper account).\n"
            f"  2. Configure -> Settings -> API -> Settings:\n"
            f"       - Read-Only API is ON (required by this skill).\n"
            f"       - Socket port matches the bot's ibkr_port (Gateway paper = 4002).\n"
            f"       - 127.0.0.1 is in Trusted IPs.\n"
            f"  3. clientId is not already in use by another session.\n"
            f"  4. If you're running TWS instead of Gateway, set "
            f"ibkr_port to 7497 in config.json."
        )
    return ib


def probe_ibkr_reachable(cfg: dict, timeout: float = 5.0,
                         probe_client_id: int = 98) -> tuple[bool, str]:
    """One-shot connect-and-disconnect probe. Non-sys.exit.

    Returns (ok, reason). `reason` is empty on success, a short
    diagnostic string on failure. Uses a distinct client_id so it
    can't collide with a live bot session.

    Use this at startup to decide whether to use IBKR or fall back
    to Alpaca; don't use the heavier `_connect()` for the probe
    because that one sys.exits on failure.
    """
    if IB is None:
        return False, "ib_insync not installed"
    host = cfg.get("ibkr_host", DEFAULT_HOST)
    try:
        port = int(cfg.get("ibkr_port", PAPER_PORT))
    except (TypeError, ValueError):
        return False, f"invalid ibkr_port={cfg.get('ibkr_port')!r}"
    ib = IB()
    try:
        ib.connect(host, port, clientId=probe_client_id,
                   timeout=timeout, readonly=True)
        ok = ib.isConnected()
        try:
            ib.disconnect()
        except Exception:
            pass
        if ok:
            return True, ""
        return False, f"connect to {host}:{port} returned but not connected"
    except SystemExit as exc:
        return False, f"{host}:{port} -> {exc}"
    except Exception as exc:
        return False, f"{host}:{port} -> {type(exc).__name__}: {exc}"


def _stock(symbol: str) -> "Stock":
    # SMART routing; primaryExchange left blank — IBKR will resolve.
    # For ADRs / dual-listed names a strategy may need to override
    # primaryExchange via a custom Stock contract.
    return Stock(symbol.upper(), "SMART", "USD")


def _now_et(fake_now: str | None):
    et = _et_tz()
    now = datetime.now(timezone.utc).astimezone(et)
    if fake_now:
        hh, mm = fake_now.split(":")
        return now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    return now


# ---------- Public API ----------

def ibkr_pm_bars(symbols: list[str], cfg: dict,
                 fake_now: str | None) -> dict[str, list[dict]]:
    """1-min bars from 04:00 ET through (now or fake_now), pre-market only."""
    return _fetch_minute_bars(symbols, cfg, fake_now, pm_only=True)


def ibkr_full_day_minute_bars(symbols: list[str], cfg: dict,
                              fake_now: str | None) -> dict[str, list[dict]]:
    """1-min bars from 04:00 ET through (now or fake_now), PM+RTH."""
    return _fetch_minute_bars(symbols, cfg, fake_now, pm_only=False)


def ibkr_history_bars(symbols: list[str], cfg: dict, days: int = 5) -> dict[str, list[dict]]:
    """RTH-only 1-min bars over the last N trading days per symbol.

    Used by the AT scanner's 5-day/3-min trend filter where EMA6/8/50
    need enough history to stabilise.
    """
    if not symbols:
        return {}
    out: dict[str, list[dict]] = {s: [] for s in symbols}
    duration_str = f"{int(days)} D"
    ib = _connect(cfg)
    try:
        for sym in symbols:
            contract = _stock(sym)
            try:
                ib.qualifyContracts(contract)
            except Exception as exc:
                sys.stderr.write(f"IBKR qualify({sym}) failed: {exc}\n")
                continue
            try:
                bars = ib.reqHistoricalData(
                    contract,
                    endDateTime="",
                    durationStr=duration_str,
                    barSizeSetting="1 min",
                    whatToShow="TRADES",
                    useRTH=True,           # RTH only — matches "5d 3m chart"
                    formatDate=2,
                    keepUpToDate=False,
                )
            except Exception as exc:
                sys.stderr.write(f"IBKR history_bars({sym}) failed: {exc}\n")
                continue
            et = _et_tz()
            for b in bars or []:
                t = b.date
                if isinstance(t, (int, float)):
                    t = datetime.fromtimestamp(t, tz=timezone.utc).astimezone(et)
                elif isinstance(t, datetime):
                    if t.tzinfo is None:
                        t = t.replace(tzinfo=et)
                    else:
                        t = t.astimezone(et)
                else:
                    continue
                out[sym].append({
                    "t": t,
                    "o": float(b.open), "h": float(b.high),
                    "l": float(b.low), "c": float(b.close),
                    "v": int(b.volume) if b.volume and b.volume > 0 else 0,
                })
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass
    return out


def _fetch_minute_bars(symbols: list[str], cfg: dict, fake_now: str | None,
                       pm_only: bool) -> dict[str, list[dict]]:
    """Fetch 1-min bars per symbol via reqHistoricalData."""
    if not symbols:
        return {}
    out: dict[str, list[dict]] = {s: [] for s in symbols}
    now = _now_et(fake_now)
    rth_start = now.replace(hour=9, minute=30, second=0, microsecond=0)
    pm_start = now.replace(hour=4, minute=0, second=0, microsecond=0)
    # How far back do we need? From 04:00 to now -> at most 7-8 hours.
    duration_s = max(int((now - pm_start).total_seconds()) + 60, 600)
    duration_str = f"{duration_s} S" if duration_s < 86400 else f"{duration_s // 86400} D"

    ib = _connect(cfg)
    try:
        # ib_insync makes one historical request per symbol — IBKR rate-limits
        # to ~60/10min for historical, so for our 4-6 symbol watchlists this is fine.
        for sym in symbols:
            contract = _stock(sym)
            try:
                ib.qualifyContracts(contract)
            except Exception as exc:
                sys.stderr.write(f"IBKR qualify({sym}) failed: {exc}\n")
                continue
            try:
                bars = ib.reqHistoricalData(
                    contract,
                    endDateTime="",            # = now
                    durationStr=duration_str,
                    barSizeSetting="1 min",
                    whatToShow="TRADES",
                    useRTH=False,              # include extended hours (PM)
                    formatDate=2,              # epoch seconds
                    keepUpToDate=False,
                )
            except Exception as exc:
                sys.stderr.write(f"IBKR bars({sym}) failed: {exc}\n")
                continue
            et = _et_tz()
            for b in bars or []:
                # ib_insync returns BarData with .date (datetime, UTC or local
                # depending on TWS settings) when formatDate=2 it's epoch s
                t = b.date
                if isinstance(t, (int, float)):
                    t = datetime.fromtimestamp(t, tz=timezone.utc).astimezone(et)
                elif isinstance(t, datetime):
                    if t.tzinfo is None:
                        # TWS sometimes returns naive ET. Anchor in ET.
                        t = t.replace(tzinfo=et) if hasattr(t, "replace") else t
                    else:
                        t = t.astimezone(et)
                else:
                    continue
                if pm_only and t >= rth_start:
                    continue
                out[sym].append({
                    "t": t,
                    "o": float(b.open), "h": float(b.high),
                    "l": float(b.low), "c": float(b.close),
                    "v": int(b.volume) if b.volume and b.volume > 0 else 0,
                })
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass
    return out


def ibkr_latest_quote(symbols: list[str], cfg: dict) -> dict[str, dict]:
    if not symbols:
        return {}
    out: dict[str, dict] = {}
    ib = _connect(cfg)
    et = _et_tz()
    try:
        contracts = [_stock(s) for s in symbols]
        try:
            ib.qualifyContracts(*contracts)
        except Exception as exc:
            sys.stderr.write(f"IBKR qualify (batch) failed: {exc}\n")
            return {}
        tickers = ib.reqTickers(*contracts)
        for tk in tickers:
            sym = tk.contract.symbol.upper()
            ts = tk.time.astimezone(et) if tk.time else None
            out[sym] = {
                "bid": float(tk.bid) if tk.bid and tk.bid > 0 else None,
                "ask": float(tk.ask) if tk.ask and tk.ask > 0 else None,
                "bid_size": int(tk.bidSize) if tk.bidSize and tk.bidSize > 0 else None,
                "ask_size": int(tk.askSize) if tk.askSize and tk.askSize > 0 else None,
                "t": ts,
            }
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass
    return out


def ibkr_latest_trade(symbols: list[str], cfg: dict) -> dict[str, dict]:
    """IBKR doesn't have a clean 'latest trade' REST endpoint; we use the
    snapshot ticker which carries `last` and `lastSize`."""
    if not symbols:
        return {}
    out: dict[str, dict] = {}
    ib = _connect(cfg)
    et = _et_tz()
    try:
        contracts = [_stock(s) for s in symbols]
        try:
            ib.qualifyContracts(*contracts)
        except Exception:
            return {}
        tickers = ib.reqTickers(*contracts)
        for tk in tickers:
            sym = tk.contract.symbol.upper()
            ts = tk.time.astimezone(et) if tk.time else None
            price = tk.last if tk.last and tk.last > 0 else (
                tk.close if tk.close and tk.close > 0 else None
            )
            if price is None:
                continue
            out[sym] = {
                "price": float(price),
                "size": int(tk.lastSize) if tk.lastSize and tk.lastSize > 0 else None,
                "t": ts,
            }
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass
    return out


# ---------- Smoke test ----------

def smoke_test(cfg: dict) -> int:
    """Connect, list accounts, disconnect. Then run a second short-lived
    bar fetch via the public helper. Each connection uses the same clientId
    but they are NOT nested — the first one closes before the second opens."""
    host = cfg.get("ibkr_host", DEFAULT_HOST)
    port = cfg.get("ibkr_port", PAPER_PORT)
    print(f"Connecting to IBKR at {host}:{port} ...")

    # Phase 1 — short-lived connect to print account info.
    ib = _connect(cfg)
    try:
        accts = ib.managedAccounts()
        print(f"Connected. Managed accounts: {accts}")
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass

    # Tiny pause so IBKR releases the clientId before we reuse it.
    import time
    time.sleep(0.5)

    # Phase 2 — exercise the public bar-fetch helper end-to-end.
    print("Probing AAPL bars via the public helper ...")
    bars = ibkr_full_day_minute_bars(["AAPL"], cfg, fake_now=None)
    n = len(bars.get("AAPL", []))
    print(f"Probe AAPL: fetched {n} 1-min bars today.")
    if n == 0:
        print("  (0 bars is possible if you're running this outside market hours.)")
    return 0


if __name__ == "__main__":
    # Allow `py scripts/_ibkr_data.py` for a quick connect check.
    sys.path.insert(0, str(SKILL_DIR / "scripts"))
    from _common import load_config
    sys.exit(smoke_test(load_config()))
