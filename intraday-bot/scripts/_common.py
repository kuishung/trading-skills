"""Shared helpers for intraday_bot.

Centralises: config loading, ET clock, alpaca client construction
(self-contained -- credentials resolved via the central VAULT folder
or an in-folder .env, never via a sibling-skill path), state file
paths, Telegram send, and fill-event logging.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = SKILL_DIR / "state"
CONFIG_PATH = SKILL_DIR / "config.json"
CONFIG_EXAMPLE_PATH = SKILL_DIR / "config.example.json"

# --- intraday-bot bootstrap: make sibling layers importable (for the
# lazy `from ibkr_data import ...` inside the data-provider functions
# below, plus any future cross-layer imports). ---
for _p in [str(SKILL_DIR)] + [str(SKILL_DIR / s) for s in
        ("scripts", "resources", "strategy", "execution", "journal", "review", "dashboard")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)
del _p
# ---

PAPER_BASE_URL = "https://paper-api.alpaca.markets"


# ---------- Config ----------

def load_config() -> dict:
    cfg = json.loads(CONFIG_EXAMPLE_PATH.read_text(encoding="utf-8"))
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError as e:
            sys.exit(f"config.json is not valid JSON: {e}")
    return cfg


# ---------- ET clock ----------

def et_now(fake_now: str | None = None) -> datetime:
    """Current time in US/Eastern. If fake_now='HH:MM', anchor today's date in ET
    at that wall-clock for tests / replays."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
    except ImportError:
        import pytz
        tz = pytz.timezone("America/New_York")
    now = datetime.now(timezone.utc).astimezone(tz)
    if fake_now:
        try:
            hh, mm = fake_now.split(":")
            return now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        except ValueError:
            sys.exit(f"--fake-now must be HH:MM (got {fake_now!r})")
    return now


def et_today_iso(fake_now: str | None = None) -> str:
    return et_now(fake_now).date().isoformat()


def et_at(date_iso: str, hhmm: str):
    """Construct an ET-aware datetime for date_iso at HH:MM."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
    except ImportError:
        import pytz
        tz = pytz.timezone("America/New_York")
    y, m, d = (int(x) for x in date_iso.split("-"))
    hh, mm = (int(x) for x in hhmm.split(":"))
    naive = datetime(y, m, d, hh, mm)
    try:
        return naive.replace(tzinfo=tz)
    except TypeError:
        return tz.localize(naive)


# ---------- VAULT-first env resolution (self-contained, no sibling deps) ----------

def _read_dotenv(path: Path) -> dict[str, str]:
    """Tiny dotenv reader. Returns {} if the file is missing."""
    if not path or not path.exists():
        return {}
    try:
        from dotenv import dotenv_values
        return dict(dotenv_values(path) or {})
    except ImportError:
        out: dict[str, str] = {}
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
        return out


def _vault_root() -> Path | None:
    """Walk up from intraday-bot/ looking for a VAULT/Claude Credential folder
    (the central credential store -- see reference_central_env_folder memory).
    Returns None if not found."""
    here = SKILL_DIR.resolve()
    for ancestor in [here, *here.parents]:
        for candidate in (
            ancestor / "VAULT" / "Claude Credential",
            ancestor.parent / "VAULT" / "Claude Credential",
        ):
            if candidate.is_dir():
                return candidate
    return None


def _env_lookup(filename: str, in_folder_name: str = ".env") -> dict[str, str]:
    """Resolve a vendor env in priority order:

      1. INTRADAY_ENV_DIR override   (env var)
      2. <intraday-bot>/.env         (final in-folder fallback)
      3. <VAULT>/Claude Credential/<filename>   (central, shared across PCs)

    Empty dict if nothing matches.
    """
    override = os.environ.get("INTRADAY_ENV_DIR")
    if override:
        env = _read_dotenv(Path(override) / filename)
        if env:
            return env
    env = _read_dotenv(SKILL_DIR / in_folder_name)
    if env.get("ALPACA_API_KEY_ID") or env.get("TELEGRAM_BOT_TOKEN"):
        return env
    vault = _vault_root()
    if vault is not None:
        return _read_dotenv(vault / filename)
    return {}


# ---------- Alpaca client ----------

def load_alpaca_env(cfg: dict | None = None) -> tuple[str, str]:
    """Resolve Alpaca creds. Self-contained -- never reads sibling skill
    folders. Lookup order: INTRADAY_ENV_DIR override -> in-folder .env ->
    VAULT/Claude Credential/alpaca-trader-paper.env (or alpaca.env).

    Refuses to return creds for a non-paper base URL.
    """
    env = _env_lookup("alpaca-trader-paper.env")
    if not (env.get("ALPACA_API_KEY_ID") and env.get("ALPACA_API_SECRET_KEY")):
        env = _env_lookup("alpaca.env")
    if not (env.get("ALPACA_API_KEY_ID") and env.get("ALPACA_API_SECRET_KEY")):
        sys.exit(
            "Alpaca creds not found. Put them in one of:\n"
            "  $INTRADAY_ENV_DIR/alpaca-trader-paper.env\n"
            f"  {SKILL_DIR / '.env'}\n"
            "  <Dropbox>/VAULT/Claude Credential/alpaca-trader-paper.env\n"
            "with ALPACA_API_KEY_ID, ALPACA_API_SECRET_KEY, "
            f"ALPACA_BASE_URL={PAPER_BASE_URL}"
        )
    base = env.get("ALPACA_BASE_URL", PAPER_BASE_URL)
    if base != PAPER_BASE_URL:
        sys.exit(
            f"Refusing to run. ALPACA_BASE_URL is {base!r}, "
            f"but this bot is paper-only ({PAPER_BASE_URL})."
        )
    return env["ALPACA_API_KEY_ID"], env["ALPACA_API_SECRET_KEY"]


def trading_client(cfg: dict):
    from alpaca.trading.client import TradingClient
    key, secret = load_alpaca_env(cfg)
    return TradingClient(key, secret, paper=True)


def data_client(cfg: dict):
    """Alpaca data client. Use only for alpaca-routed code paths; most signal
    code should call the get_pm_bars / get_latest_quote / get_latest_trade
    helpers below, which dispatch by cfg['data_provider']."""
    from alpaca.data.historical import StockHistoricalDataClient
    key, secret = load_alpaca_env(cfg)
    return StockHistoricalDataClient(key, secret)


# ---------- Data provider abstraction (alpaca | ibkr) ----------
#
# All bar/quote/trade fetches in the bot route through these functions so we
# can swap the underlying provider via cfg['data_provider'] without touching
# scan/orchestrator code. The return shapes are intentionally simple dicts
# (not vendor SDK objects) so the abstraction stays clean.
#
# Bar shape:
#   {"t": datetime (ET-aware), "o": float, "h": float, "l": float,
#    "c": float, "v": int}
# Quote shape:
#   {"bid": float | None, "ask": float | None,
#    "bid_size": int | None, "ask_size": int | None,
#    "t": datetime (ET-aware) | None}
# Trade shape:
#   {"price": float, "size": int | None, "t": datetime (ET-aware) | None}

def _provider(cfg: dict) -> str:
    return (cfg.get("data_provider") or "alpaca").lower()


# Once a fallback has fired in this process, downgrade silently for subsequent
# calls so we don't spam warnings.
_FALLBACK_LATCHED: bool = False
_FALLBACK_REASON: str | None = None


def fallback_state() -> tuple[bool, str | None]:
    """Caller (trade_day.py) can read this to mention the fallback in the EOD
    report."""
    return _FALLBACK_LATCHED, _FALLBACK_REASON


def _try_ibkr(call_name: str, ibkr_callable, *args, **kwargs):
    """Run an IBKR call; on failure, latch a process-wide fallback and return
    None so the caller can swap in the Alpaca implementation. SystemExit from
    inside the IBKR adapter is caught here (the adapter uses sys.exit on
    connection failure, which is convenient for CLI smoke-tests but too
    aggressive for the live trading loop)."""
    global _FALLBACK_LATCHED, _FALLBACK_REASON
    if _FALLBACK_LATCHED:
        return None
    try:
        return ibkr_callable(*args, **kwargs)
    except SystemExit as exc:
        _FALLBACK_LATCHED = True
        _FALLBACK_REASON = str(exc) or "IBKR connection failed"
        sys.stderr.write(
            f"\n⚠️  IBKR data unavailable on {call_name}: {_FALLBACK_REASON}\n"
            f"   Falling back to Alpaca IEX feed for the remainder of this session.\n\n"
        )
        return None
    except Exception as exc:
        _FALLBACK_LATCHED = True
        _FALLBACK_REASON = f"{type(exc).__name__}: {exc}"
        sys.stderr.write(
            f"\n⚠️  IBKR data unavailable on {call_name}: {_FALLBACK_REASON}\n"
            f"   Falling back to Alpaca IEX feed for the remainder of this session.\n\n"
        )
        return None


def get_pm_bars(symbols: list[str], cfg: dict, fake_now: str | None = None) -> dict[str, list[dict]]:
    """1-minute bars from 04:00 ET through 'now' (or fake_now), per symbol."""
    if _provider(cfg) == "ibkr":
        from ibkr_data import ibkr_pm_bars
        result = _try_ibkr("get_pm_bars", ibkr_pm_bars, symbols, cfg, fake_now)
        if result is not None:
            return result
    return _alpaca_minute_bars(symbols, cfg, fake_now, pm_only=True)


def get_rth_minute_bars(symbols: list[str], cfg: dict, fake_now: str | None = None) -> dict[str, list[dict]]:
    """Today's full 1-min bar history (PM + RTH up to now). Caller splits."""
    if _provider(cfg) == "ibkr":
        from ibkr_data import ibkr_full_day_minute_bars
        result = _try_ibkr("get_rth_minute_bars", ibkr_full_day_minute_bars,
                           symbols, cfg, fake_now)
        if result is not None:
            return result
    return _alpaca_minute_bars(symbols, cfg, fake_now, pm_only=False)


def get_history_bars(symbols: list[str], cfg: dict, days: int = 5) -> dict[str, list[dict]]:
    """RTH-only 1-min bars over the last `days` trading days. Used by the
    AT scanner's 5d/3m trend filter (needs EMA50 stabilisation)."""
    if _provider(cfg) == "ibkr":
        from ibkr_data import ibkr_history_bars
        result = _try_ibkr("get_history_bars", ibkr_history_bars, symbols, cfg, days)
        if result is not None:
            return result
    # No Alpaca fallback implemented for multi-day intraday history.
    sys.stderr.write("get_history_bars: no non-IBKR provider implementation; returning empty\n")
    return {s: [] for s in symbols}


def get_latest_quote(symbols: list[str], cfg: dict) -> dict[str, dict]:
    if _provider(cfg) == "ibkr":
        from ibkr_data import ibkr_latest_quote
        result = _try_ibkr("get_latest_quote", ibkr_latest_quote, symbols, cfg)
        if result is not None:
            return result
    return _alpaca_latest_quote(symbols, cfg)


def get_latest_trade(symbols: list[str], cfg: dict) -> dict[str, dict]:
    if _provider(cfg) == "ibkr":
        from ibkr_data import ibkr_latest_trade
        result = _try_ibkr("get_latest_trade", ibkr_latest_trade, symbols, cfg)
        if result is not None:
            return result
    return _alpaca_latest_trade(symbols, cfg)


# ---------- Alpaca-flavoured data fetches (internal) ----------

def _et_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except ImportError:
        import pytz
        return pytz.timezone("America/New_York")


def _alpaca_minute_bars(symbols: list[str], cfg: dict, fake_now: str | None,
                        pm_only: bool) -> dict[str, list[dict]]:
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    if not symbols:
        return {sym: [] for sym in symbols}
    dc = data_client(cfg)
    now = et_now(fake_now)
    start = now.replace(hour=4, minute=0, second=0, microsecond=0)
    end = now
    req = StockBarsRequest(
        symbol_or_symbols=symbols, timeframe=TimeFrame.Minute,
        start=start.astimezone(timezone.utc), end=end.astimezone(timezone.utc),
        feed="iex",
    )
    try:
        bars = dc.get_stock_bars(req)
    except Exception as exc:
        sys.stderr.write(f"Alpaca bar fetch failed: {exc}\n")
        return {sym: [] for sym in symbols}
    et = _et_tz()
    out: dict[str, list[dict]] = {sym: [] for sym in symbols}
    bars_dict = getattr(bars, "data", None) or {}
    rth_start = now.replace(hour=9, minute=30, second=0, microsecond=0)
    for sym in symbols:
        for b in bars_dict.get(sym, []):
            ts = b.timestamp.astimezone(et) if hasattr(b.timestamp, "astimezone") else b.timestamp
            if pm_only and ts >= rth_start:
                continue
            out[sym].append({
                "t": ts, "o": float(b.open), "h": float(b.high),
                "l": float(b.low), "c": float(b.close), "v": int(b.volume),
            })
    return out


def _alpaca_latest_quote(symbols: list[str], cfg: dict) -> dict[str, dict]:
    from alpaca.data.requests import StockLatestQuoteRequest
    if not symbols:
        return {}
    dc = data_client(cfg)
    try:
        req = StockLatestQuoteRequest(symbol_or_symbols=symbols, feed="iex")
        resp = dc.get_stock_latest_quote(req)
    except Exception as exc:
        sys.stderr.write(f"Alpaca quote fetch failed: {exc}\n")
        return {}
    out: dict[str, dict] = {}
    et = _et_tz()
    for sym, q in (resp.items() if hasattr(resp, "items") else []):
        ts = q.timestamp.astimezone(et) if hasattr(q.timestamp, "astimezone") else None
        out[sym] = {
            "bid": float(q.bid_price) if q.bid_price else None,
            "ask": float(q.ask_price) if q.ask_price else None,
            "bid_size": int(q.bid_size) if q.bid_size else None,
            "ask_size": int(q.ask_size) if q.ask_size else None,
            "t": ts,
        }
    return out


def _alpaca_latest_trade(symbols: list[str], cfg: dict) -> dict[str, dict]:
    from alpaca.data.requests import StockLatestTradeRequest
    if not symbols:
        return {}
    dc = data_client(cfg)
    try:
        req = StockLatestTradeRequest(symbol_or_symbols=symbols, feed="iex")
        resp = dc.get_stock_latest_trade(req)
    except Exception as exc:
        sys.stderr.write(f"Alpaca trade fetch failed: {exc}\n")
        return {}
    out: dict[str, dict] = {}
    et = _et_tz()
    for sym, t in (resp.items() if hasattr(resp, "items") else []):
        ts = t.timestamp.astimezone(et) if hasattr(t.timestamp, "astimezone") else None
        out[sym] = {
            "price": float(t.price),
            "size": int(t.size) if t.size else None,
            "t": ts,
        }
    return out


# ---------- State paths ----------

def watchlist_path(date_iso: str) -> Path:
    return STATE_DIR / f"watchlist_{date_iso}.txt"


def plan_path(date_iso: str) -> Path:
    return STATE_DIR / f"plan_{date_iso}.json"


def fills_path(date_iso: str) -> Path:
    return STATE_DIR / f"fills_{date_iso}.jsonl"


def equity_path(date_iso: str) -> Path:
    return STATE_DIR / f"equity_{date_iso}.json"


def append_fill_event(date_iso: str, event: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    event = dict(event)
    event["ts"] = datetime.now(timezone.utc).isoformat()
    with fills_path(date_iso).open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, default=str) + "\n")


# ---------- Telegram ----------

def telegram_env(cfg: dict | None = None) -> tuple[str | None, str | None]:
    """Resolve Telegram creds. Self-contained -- never reads sibling skill
    folders. Lookup: INTRADAY_ENV_DIR override -> in-folder .env ->
    VAULT/Claude Credential/telegram.env (then matp.env as a back-compat
    fallback since the user's MATP-era setup used that filename).

    Returns (token, chat_id) or (None, None) if not configured -- Telegram
    notifications are optional, so this never sys.exits.
    """
    env = _env_lookup("telegram.env")
    if not (env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID")):
        env = _env_lookup("matp.env")  # legacy filename
    return env.get("TELEGRAM_BOT_TOKEN") or None, env.get("TELEGRAM_CHAT_ID") or None


def send_telegram(cfg: dict, html: str) -> bool:
    token, chat_id = telegram_env(cfg)
    if not token or not chat_id:
        return False
    # Telegram caps single message at 4096; chunk on blank lines if needed.
    MAX = 4000
    chunks: list[str] = []
    if len(html) <= MAX:
        chunks = [html]
    else:
        buf: list[str] = []
        size = 0
        for line in html.split("\n"):
            if size + len(line) + 1 > MAX and buf:
                chunks.append("\n".join(buf))
                buf, size = [], 0
            buf.append(line)
            size += len(line) + 1
        if buf:
            chunks.append("\n".join(buf))
    ok = True
    for chunk in chunks:
        data = urllib.parse.urlencode({
            "chat_id": chat_id, "text": chunk,
            "parse_mode": "HTML", "disable_web_page_preview": "true",
        }).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "User-Agent": "intraday_bot/0.7"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            if not body.get("ok"):
                sys.stderr.write(f"Telegram error: {body}\n")
                ok = False
        except Exception as exc:
            sys.stderr.write(f"Telegram send failed: {exc}\n")
            ok = False
    return ok


# ---------- Misc ----------

def fmt_price(p) -> str:
    if p is None:
        return "—"
    return f"${float(p):.2f}"


def safe_log_stdout(msg: str) -> None:
    """Print to stdout, tolerating cp1252 consoles on Windows when emoji slip in."""
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((msg + "\n").encode("utf-8", errors="replace"))
        sys.stdout.flush()


def sleep_until(target_dt, fake_now: str | None) -> None:
    """Sleep until `target_dt` (ET-aware). No-op in fake-now mode."""
    if fake_now:
        return
    while True:
        now = et_now()
        delta = (target_dt - now).total_seconds()
        if delta <= 0:
            return
        time.sleep(min(delta, 30))
