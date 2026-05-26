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

# Data root — set 2026-05-24 to support per-PC external data folders
# (e.g., D:\HermesSync\MarketData on laptop, C:\HermesSync\MarketData on Hermes)
# synced peer-to-peer via Resilio Sync rather than via Dropbox. Falls back
# to the in-folder default (SKILL_DIR / "data") for any PC that doesn't
# override via config. See get_data_root() below.
DATA_DIR_DEFAULT = SKILL_DIR / "data"

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
    # Use utf-8-sig (NOT utf-8) so a UTF-8 BOM at the start of either file
    # is silently stripped. Windows PowerShell 5.1's `Set-Content -Encoding
    # UTF8` writes UTF-8 *with* BOM by default, which utf-8 decode would
    # leave as a literal "﻿" character at position 0 -> json.loads
    # then chokes with "Unexpected UTF-8 BOM (decode using utf-8-sig)".
    # Anyone editing config.json from PS 5.1 hits this trap. utf-8-sig
    # decodes both BOM'd and BOM-less UTF-8 correctly, so no other change
    # is required.
    cfg = json.loads(CONFIG_EXAMPLE_PATH.read_text(encoding="utf-8-sig"))
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")))
        except json.JSONDecodeError as e:
            sys.exit(f"config.json is not valid JSON: {e}")
    return cfg


def get_data_root() -> Path:
    """Return the on-disk data root for `data/price_history/`, `data/journal/`,
    `data/review/`, `data/ticker_profile/`, etc.

    Resolution order:
      1. cfg["data_root"] from config.json (if set + non-empty) — supports
         per-PC absolute paths so the heavy regeneratable parquets can live
         outside the bot folder and sync via Resilio P2P rather than Dropbox.
         Examples:
            "D:\\HermesSync\\MarketData"   (laptop)
            "C:\\HermesSync\\MarketData"   (Hermes VM)
      2. DATA_DIR_DEFAULT (= SKILL_DIR / "data") — keeps the bot fully
         self-contained for any PC that hasn't customised.

    Path is created if missing. Returned as pathlib.Path.

    Cost: re-reads config.json on each call. Negligible (small JSON,
    cached by OS). Strategy modules typically capture the result in a
    module-level constant at import time."""
    cfg = load_config()
    p = cfg.get("data_root")
    if p:
        path = Path(p).expanduser()
    else:
        path = DATA_DIR_DEFAULT
    path.mkdir(parents=True, exist_ok=True)
    return path


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
    """Locate the vendor-credentials directory.

    Resolution order:
      1. cfg["vault_dir"] from config.json (per-PC override, mirrors the
         data_root pattern). Supports absolute paths like
            D:\\HermesSync\\Vault   (laptop, Resilio-synced)
            C:\\HermesSync\\Vault   (Hermes, Resilio-synced from laptop)
         Set 2026-05-24 so credential files (alpaca.env, telegram.env,
         intraday-premarket.env, etc.) can sync peer-to-peer via Resilio
         rather than via the laptop's Dropbox VAULT folder.

      2. Auto-discover (back-compat): walk up from SKILL_DIR looking for
         a VAULT/Claude Credential subfolder. Honours the original
         Dropbox-shared layout for any PC that hasn't customised
         vault_dir in config.json.

    Returns the Path (whichever wins), or None if neither is configured /
    discoverable. Caller falls back to in-folder .env or fails loudly per
    the existing _env_lookup behaviour.
    """
    cfg = load_config()
    vd = cfg.get("vault_dir")
    if vd:
        p = Path(str(vd)).expanduser()
        if p.is_dir():
            return p
        # If configured but missing on disk, log + fall through to
        # auto-discovery so we don't silently break credential resolution
        # mid-session (e.g., Resilio temporarily disconnected on a peer).
        sys.stderr.write(
            f"[_common] vault_dir={vd!r} configured but does not exist; "
            f"falling back to auto-discovery.\n"
        )
    # Fallback: auto-discover (preserves original walk-up behaviour)
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
    None so the caller can swap in the Alpaca implementation.

    Catches both Exception (the new behaviour as of 2026-05-26 — ibkr_data
    now raises ConnectionError on connect failure) AND SystemExit (legacy
    paths and any remaining CLI-style sys.exit). Both branches keep the
    process alive and latch the fallback so subsequent calls in the same
    session don't re-pay the timeout."""
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
    if _provider(cfg) == "parquet":
        return _parquet_minute_bars(symbols, cfg, fake_now, pm_only=True)
    if _provider(cfg) == "ibkr":
        from ibkr_data import ibkr_pm_bars
        result = _try_ibkr("get_pm_bars", ibkr_pm_bars, symbols, cfg, fake_now)
        if result is not None:
            return result
    return _alpaca_minute_bars(symbols, cfg, fake_now, pm_only=True)


def get_rth_minute_bars(symbols: list[str], cfg: dict, fake_now: str | None = None) -> dict[str, list[dict]]:
    """Today's full 1-min bar history (PM + RTH up to now). Caller splits."""
    if _provider(cfg) == "parquet":
        return _parquet_minute_bars(symbols, cfg, fake_now, pm_only=False)
    if _provider(cfg) == "ibkr":
        from ibkr_data import ibkr_full_day_minute_bars
        result = _try_ibkr("get_rth_minute_bars", ibkr_full_day_minute_bars,
                           symbols, cfg, fake_now)
        if result is not None:
            return result
    return _alpaca_minute_bars(symbols, cfg, fake_now, pm_only=False)


# ---------- Parquet-backed replay provider ----------
#
# When cfg["data_provider"] == "parquet", bars come from
# data/price_history/1min/<SYM>.parquet (the bars_store layout). The
# replay-date is read from cfg["replay_date"] (set by the orchestrator's
# --replay-date flag). fake_now becomes the cutoff time-of-day.
#
# This lets the bot re-evaluate a past session by re-running its decision
# code against the bars it would have seen then — the substrate for the
# review/ self-improvement loop.

def _parquet_minute_bars(symbols: list[str], cfg: dict,
                          fake_now: str | None, pm_only: bool) -> dict[str, list[dict]]:
    """Read 1-min bars for the replay date from bars_store, returning the
    same shape as the IBKR/Alpaca providers. pm_only=True trims to 04:00 →
    09:30 ET; pm_only=False returns 04:00 → fake_now (default 16:00) ET.
    """
    date_iso = cfg.get("replay_date")
    if not date_iso:
        sys.stderr.write("[parquet] data_provider=parquet but cfg['replay_date'] is missing\n")
        return {s: [] for s in symbols}
    try:
        import bars_store  # type: ignore
    except ImportError as exc:
        sys.stderr.write(f"[parquet] bars_store import failed: {exc}\n")
        return {s: [] for s in symbols}
    from datetime import datetime as _dt
    et = _et_tz()
    yyyy, mm, dd = (int(x) for x in date_iso.split("-"))
    if pm_only:
        cutoff = fake_now or "09:30"
    else:
        cutoff = fake_now or "16:00"
    h, m = (int(x) for x in cutoff.split(":"))
    start_et = _dt(yyyy, mm, dd, 4, 0, tzinfo=et)
    end_et = _dt(yyyy, mm, dd, h, m, tzinfo=et)
    out: dict[str, list[dict]] = {}
    for sym in symbols:
        bars = bars_store.load_bars(
            sym,
            start=start_et.astimezone(timezone.utc).isoformat(),
            end=end_et.astimezone(timezone.utc).isoformat(),
            timeframe="1min",
        )
        # Bars come back with ISO UTC string `t`; strategy code accepts that.
        out[sym] = bars
    return out


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
