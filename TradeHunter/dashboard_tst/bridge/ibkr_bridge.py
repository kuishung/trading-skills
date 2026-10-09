r"""TradeHunter IBKR connector 2.0 - runs on YOUR PC, talks to YOUR TWS.

What it is for
--------------
TradeHunter's Options page takes every option figure from IBKR and nothing else
(OPTIONS_V2_DESIGN.md). TWS runs on each member's own machine under their own login,
so the server cannot read it; the browser asks this connector on ``127.0.0.1``
instead and relays the result to the server, which stores it in the shared pool
every member reads (each contract stamped with its time, source and market data
type - so one member's live read helps everyone):

    browser (app.tradehunter.net) --fetch--> 127.0.0.1:9224 --ib_insync--> your TWS
                                  --POST---> server: validate + store (shared pool)

Browsers allow an HTTPS page to fetch ``http://127.0.0.1`` (loopback counts as a
trustworthy origin), which is what makes this work without exposing anything. The
fetching itself is ``th_ibkr.py`` (next to this file), the library the Hermes
collector also uses, so a member's chain and Hermes' chain have one shape.

Settings
--------
``%APPDATA%\TradeHunter\connector.json`` (``~/.config/tradehunter/connector.json``
off Windows), edited on the connector's own page, http://127.0.0.1:9224/ - TWS host,
API port (TWS live 7496 / TWS paper 7497 / Gateway live 4001 / Gateway paper 4002),
client ID, market-data lines. Command-line flags override the file for one run.

Security
--------
This process can read your account, so it answers only allow-listed browser
origins: any HTTPS host in tradehunter.net, local development pages on
http://127.0.0.1 / http://localhost ports 8000-8099 (the web app runs on 8000), plus
extras you add on the settings page or with ``--origin``. A browser request from
ANOTHER site that carries no Origin header at all (an ``<img>``, a ``<script src>``,
a no-cors fetch - the browser says ``Sec-Fetch-Site: cross-site``) is refused on
every endpoint except the settings page, so a hostile page cannot make your TWS do
reads it cannot even see. Requests whose Host header is not a loopback name are
refused (a DNS-rebinding page cannot borrow the loopback address). ``POST /settings``
is same-origin only: no CORS header, and a request from any other origin is refused,
so no website can repoint the connector. Strictly read-only - ``readonly=True`` on
connect, no order path anywhere.

Endpoints
---------
    GET  /                 settings page (self-contained, no CDN)
    POST /settings         save connector.json and reconnect (same-origin only)
    GET  /health           cached state, never waits on IBKR
    GET  /chain2           ?symbol=&spec=<json>  th_ibkr.quote over th_ibkr.plan(spec), 20 s cache;
                           a read that runs long returns what it read ("partial": true)
    GET  /underlying       ?symbol=  spot + 1 year of daily bars and IV (history cached per day)
    GET  /account          net liquidation + currency
    GET  /chain /iv /scan  the 1.x endpoints, unchanged, for the legacy hidden pages

Run
---
    py -3.12 ibkr_bridge.py                 # settings from connector.json
    py -3.12 ibkr_bridge.py --port 4002     # this run only: IB Gateway paper

``ib_insync`` needs Python <= 3.13 (eventkit calls asyncio.get_event_loop() at
import, removed in 3.14).
"""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import datetime as _dt
import html
import inspect
import json
import math
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

VERSION = "2.0"
BRIDGE_PORT = 9224          # 9223 is the TradingView bridge
_HERE = Path(__file__).resolve().parent


def _import_th():
    """``th_ibkr`` ships next to this file. Import it as a sibling; when the script
    was started from another directory, put this folder on sys.path and retry. A
    missing or broken file leaves the legacy endpoints working and makes the 2.0
    ones answer with the reason."""
    try:
        import th_ibkr as mod  # noqa: PLC0415
        return mod, None
    except Exception:  # noqa: BLE001
        pass
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))
    try:
        import th_ibkr as mod  # noqa: PLC0415
        return mod, None
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


th_ibkr, _TH_ERROR = _import_th()

MDT_NAMES = {1: "live", 2: "frozen", 3: "delayed", 4: "delayed_frozen"}


def _th():
    if th_ibkr is None:
        raise RuntimeError("th_ibkr.py is missing or broken next to ibkr_bridge.py "
                           f"({_TH_ERROR}). Download the connector again from the Options page.")
    return th_ibkr


# --------------------------------------------------------------- settings file
DEFAULT_CONFIG = {"tws_host": "127.0.0.1", "tws_port": 7496, "client_id": 86,
                  "max_lines": 40, "allowed_origins": []}
PORT_PRESETS = (("TWS live", 7496), ("TWS paper", 7497),
                ("Gateway live", 4001), ("Gateway paper", 4002))
INT_FIELDS = {                       # key: (label, lowest, highest)
    "tws_port": ("TWS port", 1, 65535),
    "client_id": ("Client ID", 0, 999999),
    "max_lines": ("Market-data lines", 5, 200),
}
MAX_EXTRA_ORIGINS = 20
_HOST_RE = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?|[0-9A-Fa-f:]{2,45})$")


def default_config_path(environ=None, platform=None, home=None) -> Path:
    """``%APPDATA%\\TradeHunter\\connector.json`` on Windows, else
    ``~/.config/tradehunter/connector.json``. Arguments exist for tests."""
    env = os.environ if environ is None else environ
    plat = sys.platform if platform is None else platform
    if plat == "win32" and env.get("APPDATA"):
        return Path(env["APPDATA"]) / "TradeHunter" / "connector.json"
    base = Path(home) if home is not None else Path.home()
    return base / ".config" / "tradehunter" / "connector.json"


def _defaults() -> dict:
    return {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULT_CONFIG.items()}


def _as_int(v):
    """A whole number from JSON or a form field, else None (bools are not numbers)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v) if v.is_integer() else None
    if isinstance(v, str) and re.fullmatch(r"\s*[+-]?\d{1,9}\s*", v):
        return int(v)
    return None


def normalize_origin(o):
    """``scheme://host[:port]`` as a browser sends it in the Origin header (lower-case,
    default port dropped), "" for a blank entry, None when it is not an origin."""
    o = str(o or "").strip().rstrip("/")
    if not o:
        return ""
    try:
        u = urlparse(o)
        port = u.port
    except ValueError:
        return None
    if (u.scheme not in ("http", "https") or not u.hostname or u.path or u.params
            or u.query or u.fragment or u.username or u.password):
        return None
    host = u.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    if port in (None, 80 if u.scheme == "http" else 443):
        return f"{u.scheme}://{host}"
    return f"{u.scheme}://{host}:{port}"


def validate_config(data, base=None) -> tuple[dict, list[str]]:
    """Merge ``data`` over ``base`` (default: the defaults) -> (clean, errors).

    A bad field keeps the base value and adds one plain-words error; unknown keys
    are ignored, so a partial update ({"tws_port": 7497}) is valid."""
    out = _defaults()
    if base:
        for k in out:
            if k in base:
                out[k] = list(base[k]) if isinstance(base[k], list) else base[k]
    if not isinstance(data, dict):
        return out, ["Settings must be a JSON object."]
    errors: list[str] = []
    if "tws_host" in data:
        h = str(data["tws_host"] or "").strip()
        if _HOST_RE.match(h):
            out["tws_host"] = h
        else:
            errors.append("TWS host must be a host name or IP address, e.g. 127.0.0.1.")
    for key, (label, lo, hi) in INT_FIELDS.items():
        if key in data:
            v = _as_int(data[key])
            if v is None or not lo <= v <= hi:
                errors.append(f"{label} must be a whole number from {lo} to {hi}.")
            else:
                out[key] = v
    if "allowed_origins" in data:
        raw = data["allowed_origins"]
        if raw is None:
            raw = []
        if isinstance(raw, str):
            raw = raw.replace(",", "\n").splitlines()
        if not isinstance(raw, (list, tuple)):
            errors.append("Extra allowed origins must be a list.")
        else:
            good, bad = [], []
            for item in raw:
                n = normalize_origin(item)
                if n is None:
                    bad.append(str(item).strip())
                elif n and n not in good:
                    good.append(n)
            if bad:
                errors.append("Not a web origin (scheme://host[:port], no path): "
                              + ", ".join(bad[:5]) + ".")
            elif len(good) > MAX_EXTRA_ORIGINS:
                errors.append(f"At most {MAX_EXTRA_ORIGINS} extra allowed origins.")
            else:
                out["allowed_origins"] = good
    return out, errors


def load_config(path=None) -> tuple[dict, list[str]]:
    """(settings, problems). Never raises: a missing file gives the defaults, an
    unreadable one gives the defaults plus a problem, a bad field its default."""
    p = Path(path) if path else default_config_path()
    if not p.exists():
        return _defaults(), []
    try:
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        return _defaults(), [f"{p} could not be read ({exc}); using the defaults."]
    return validate_config(raw)


def save_config(conf, path=None) -> Path:
    """Validate and write atomically (temp file + replace). ValueError on bad values."""
    clean, errors = validate_config(conf)
    if errors:
        raise ValueError(" ".join(errors))
    p = Path(path) if path else default_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(clean, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return p


# --------------------------------------------------------------- runtime config
# Runtime keys keep their 1.x names because the legacy /chain code reads them.
CFG = {"host": DEFAULT_CONFIG["tws_host"], "port": DEFAULT_CONFIG["tws_port"],
       "client_id": DEFAULT_CONFIG["client_id"], "max_lines": DEFAULT_CONFIG["max_lines"],
       "strike_window": 10, "quote_wait": 8.0, "allowed": []}
RUN = {"config_path": None, "cli_origins": [], "overrides": {}, "load_problems": []}
_OVERRIDE_KEYS = {"host": "tws_host", "port": "tws_port", "client_id": "client_id",
                  "max_lines": "max_lines"}


def config_path() -> Path:
    return Path(RUN["config_path"]) if RUN.get("config_path") else default_config_path()


def apply_config(conf: dict, overrides=None) -> None:
    """Put a validated settings dict (plus CLI overrides) into the running CFG."""
    CFG.update(host=conf["tws_host"], port=int(conf["tws_port"]),
               client_id=int(conf["client_id"]), max_lines=int(conf["max_lines"]))
    CFG["allowed"] = list(conf.get("allowed_origins") or []) + list(RUN.get("cli_origins") or [])
    for k, v in (overrides or {}).items():
        if k in _OVERRIDE_KEYS and v is not None:
            CFG[k] = v


# --------------------------------------------------------------- origin + host rules
ALLOWED_DOMAIN = "tradehunter.net"
_LOOPBACK_NAMES = ("127.0.0.1", "localhost")
DEV_PORTS = (8000, 8099)    # local development pages always allowed (the web app runs on
                            # 8000; 1.x allowed 8000/8010/8011). Any other local page -
                            # another program on this PC - needs an explicit extra origin.


def origin_allowed(origin) -> bool:
    """True for the platform's HTTPS hosts (apex or any subdomain - the site is
    app.tradehunter.net, and an apex-only list refused it with an opaque "Failed to
    fetch"), http://127.0.0.1 / http://localhost on the development ports DEV_PORTS,
    and configured extras."""
    if not origin:
        return False
    if origin in CFG["allowed"]:
        return True
    try:
        u = urlparse(origin)
        host = (u.hostname or "").lower()
        _ = u.port
    except ValueError:
        return False
    if not host:
        return False
    if u.scheme == "https" and (host == ALLOWED_DOMAIN or host.endswith("." + ALLOWED_DOMAIN)):
        return True
    return (u.scheme == "http" and host in _LOOPBACK_NAMES and u.port is not None
            and DEV_PORTS[0] <= u.port <= DEV_PORTS[1])


def cross_site_without_origin(headers) -> bool:
    """A browser request from ANOTHER site that carries no Origin header - an ``<img>``,
    a ``<script src>``, ``fetch(url, {mode: "no-cors"})``. Browsers leave Origin off
    such GETs but always say where they come from in ``Sec-Fetch-Site``. The caller
    cannot read the answer, but the read itself would still run on the member's TWS
    (market-data lines, the historical-request pacing), so these are refused before
    any work. Our own pages fetch with CORS (Origin sent); the settings page's own
    polls are same-origin; curl / a script send neither header."""
    if headers.get("Origin") is not None:
        return False
    site = (headers.get("Sec-Fetch-Site") or "").strip().lower()
    return bool(site) and site not in ("same-origin", "none")


def host_allowed(host_header) -> bool:
    """The Host header must name loopback. A DNS-rebinding page (evil.example
    re-pointed at 127.0.0.1) makes same-origin requests that carry no Origin header
    but do carry its own Host - this is what refuses them. No Host = not a browser."""
    if not host_header:
        return True
    try:
        name = urlparse("//" + str(host_header).strip()).hostname
    except ValueError:
        return False
    return name in ("127.0.0.1", "localhost", "::1")


def same_origin_request(headers, own_port: int) -> bool:
    """True only when the request comes from the connector's own page (or from no
    page at all, e.g. curl). Browsers send Origin on every POST; Sec-Fetch-Site,
    when present, must say same-origin; Referer is the fallback for old browsers."""
    own = {f"http://127.0.0.1:{own_port}", f"http://localhost:{own_port}"}
    site = (headers.get("Sec-Fetch-Site") or "").strip().lower()
    if site and site not in ("same-origin", "none"):
        return False
    origin = headers.get("Origin")
    if origin is not None:
        return origin.strip().rstrip("/").lower() in own
    ref = headers.get("Referer")
    if ref:
        try:
            u = urlparse(ref)
        except ValueError:
            return False
        return f"{u.scheme}://{u.netloc}".lower() in own
    return True


# --------------------------------------------------------------- cached state
class _Status:
    """What /health reports. Written by the connection keeper and by every fetch;
    read without touching IBKR, so /health answers in milliseconds."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.tws_connected = False
        self.error = None
        self.mdt = None
        self.account_type = None
        self.since = None

    def set(self, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def snapshot(self) -> dict:
        with self._lock:
            return {"tws_connected": self.tws_connected, "error": self.error,
                    "mdt": self.mdt, "account_type": self.account_type, "since": self.since}


STATE = _Status()


def health_payload() -> dict:
    s = STATE.snapshot()
    err = s["error"]
    if err is None and th_ibkr is None:
        err = f"th_ibkr.py is missing next to the connector ({_TH_ERROR})"
    return {"ok": True, "version": VERSION, "tws_connected": bool(s["tws_connected"]),
            "connected": bool(s["tws_connected"]),          # the 1.x name, read by legacy pages
            "tws": f"{CFG['host']}:{CFG['port']}", "client_id": CFG["client_id"],
            "mdt": s["mdt"], "account_type": s["account_type"], "error": err}


def _mdt_name(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        names = getattr(th_ibkr, "MDT_NAMES", None) or MDT_NAMES
        return names.get(v)
    if isinstance(v, str) and v in MDT_NAMES.values():
        return v
    return None


def _pos(v):
    """A finite positive float, else None."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


# --------------------------------------------------------------- IB worker
KEEP_TICK = 0.5             # how often the keeper looks at the connection
RETRY_MIN, RETRY_MAX = 5.0, 30.0
PROBE_SYMBOL = "SPY"        # one stock quote after connecting tells the data type


class _Worker:
    """Owns the asyncio loop and the single IB client, off the HTTP threads, and
    runs the keeper that connects at start-up and reconnects after a drop."""

    def __init__(self, ib_factory=None, keep=True) -> None:
        self.loop = asyncio.new_event_loop()
        self.ib = None
        self.reconnect = threading.Event()
        self.stopping = threading.Event()
        self._factory = ib_factory
        self._keep = keep
        self._ready = threading.Event()
        threading.Thread(target=self._run, name="ib", daemon=True).start()
        self._ready.wait(timeout=10)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            if self._factory is None:
                from ib_insync import IB  # noqa: PLC0415
                self._factory = IB
            self.ib = self._factory()
        except Exception as exc:  # noqa: BLE001
            STATE.set(error=f"ib_insync could not start: {exc}")
        self._ready.set()
        if self._keep and self.ib is not None:
            self.loop.create_task(_keeper(self))
        self.loop.run_forever()

    def submit(self, coro, timeout=60):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return fut.result(timeout)
        except concurrent.futures.TimeoutError:
            if fut.done():          # the coroutine itself raised a timeout
                raise
            fut.cancel()            # cancels the task, which releases its market data
            # (a chain read that runs long returns what it has before this limit, so
            # reaching it means TWS itself stalled - fewer lines would only be slower)
            raise RuntimeError(f"TWS did not finish within {timeout:.0f} s - check that TWS "
                               "is responding (no dialog waiting in its window), then try "
                               "again.") from None

    def request_reconnect(self) -> None:
        self.reconnect.set()

    def close(self) -> None:
        """Hand the clientId back (or TWS keeps the slot) and stop the loop."""
        self.stopping.set()

        async def _bye():
            if self.ib is not None and _is_connected(self.ib):
                self.ib.disconnect()
            others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for t in others:            # the keeper and any fetch still running
                t.cancel()
            await asyncio.gather(*others, return_exceptions=True)

        try:
            self.submit(_bye(), timeout=3)
        except Exception:  # noqa: BLE001
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)


_worker: _Worker | None = None
_worker_lock = threading.Lock()


def worker() -> _Worker:
    global _worker
    with _worker_lock:
        if _worker is None:
            _worker = _Worker()
        return _worker


def _ib():
    ib = worker().ib
    if ib is None:
        raise RuntimeError("ib_insync is not available. Run install_bridge.ps1, or: "
                           "py -3.12 -m pip install --user ib_insync")
    return ib


def _is_connected(ib) -> bool:
    try:
        return bool(ib.isConnected())
    except Exception:  # noqa: BLE001
        return False


def _account_type(ib):
    """IBKR paper account ids start with D (DU..., DF...); live ones do not."""
    try:
        accts = [str(a) for a in (ib.managedAccounts() or []) if a]
    except Exception:  # noqa: BLE001
        return None
    if not accts:
        return None
    return "paper" if accts[0].upper().startswith("D") else "live"


_locks: dict = {}


def _loop_lock(name: str) -> asyncio.Lock:
    """One asyncio.Lock per name, created on the worker loop the first time."""
    lk = _locks.get(name)
    if lk is None:
        lk = _locks[name] = asyncio.Lock()
    return lk


async def _locked(coro):
    """Run a quoting coroutine alone: the market-data line limit is shared by every
    client of the login, so two chains at once would exceed it."""
    async with _loop_lock("quote"):
        return await coro


CLIENT_ID_TRIES = 6


async def _connect(ib):
    """Connect if not connected (one attempt at a time) and record the outcome."""
    if _is_connected(ib):
        return
    async with _loop_lock("connect"):
        if _is_connected(ib):
            return
        try:
            await _handshake(ib)
        except Exception as exc:  # noqa: BLE001
            STATE.set(tws_connected=False, account_type=None,
                      error=str(exc) or type(exc).__name__)
            raise
        STATE.set(tws_connected=True, error=None, account_type=_account_type(ib),
                  since=time.time())


async def _handshake(ib):
    """Connect, stepping to a free clientId if the configured one is held.

    TWS keeps a client slot registered when a process dies without disconnecting
    — a force-kill, a crash, a closed laptop lid — and the next connection on
    that id then dies in the handshake with an EMPTY error message. Making the
    user restart TWS to clear that is a poor trade, so we simply walk to the next
    id and remember it. A genuinely unreachable TWS fails on the first try with a
    real message (connection refused), so this never masks that case.
    """
    first_detail = None
    base = CFG["client_id"]
    for n in range(CLIENT_ID_TRIES):
        cid = base + n
        try:
            await ib.connectAsync(CFG["host"], CFG["port"], clientId=cid,
                                  timeout=8, readonly=True)
            if n:
                print(f"  clientId {base} was held by another session; using {cid}")
                CFG["client_id"] = cid
            return
        except Exception as exc:  # noqa: BLE001
            detail = str(exc).strip()
            if first_detail is None:
                first_detail = detail
            if detail:
                # A real error (refused, wrong port, TWS down) — no point walking
                # ids, the socket itself is the problem.
                raise RuntimeError(
                    f"Cannot reach TWS on {CFG['host']}:{CFG['port']}. Start TWS, enable "
                    "File > Global Configuration > API > 'Enable ActiveX and Socket "
                    f"Clients', and check the socket port. Details: {detail}"
                ) from exc
            # empty message => handshake stalled; try the next id
            try:
                ib.disconnect()
            except Exception:  # noqa: BLE001
                pass

    raise RuntimeError(
        f"TWS answered on {CFG['host']}:{CFG['port']} but no clientId in "
        f"{base}-{base + CLIENT_ID_TRIES - 1} completed the handshake. Either TWS is "
        "showing an 'Accept incoming connection attempt?' prompt (check the TWS "
        "window), or those ids are all held by dead sessions — restarting TWS "
        "releases them."
    )


async def _probe_mdt(ib) -> None:
    """After a connect: one stock quote, so the status shows live / delayed before
    the first chain. Option chains overwrite it with their own data type."""
    if th_ibkr is None:
        return
    try:
        sp = await asyncio.wait_for(_locked(th_ibkr.spot(ib, PROBE_SYMBOL)), 15)
    except Exception:  # noqa: BLE001
        return
    name = _mdt_name((sp or {}).get("mdt"))
    if name and STATE.snapshot()["mdt"] is None:
        STATE.set(mdt=name)


async def _keeper(w: _Worker) -> None:
    """Connect at start-up, reconnect after a drop (5 s backoff doubling to 30 s),
    and reconnect at once when the settings change."""
    global _mkt_type
    ib = w.ib
    delay, next_try = RETRY_MIN, 0.0
    while not w.stopping.is_set():
        if w.reconnect.is_set():
            w.reconnect.clear()
            if _is_connected(ib):
                try:
                    ib.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            _mkt_type = None            # the 1.x chain's live/delayed verdict is per login
            for name in ("reset_mdt", "clear_cache"):     # th_ibkr's verdicts are too
                fn = getattr(th_ibkr, name, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception:  # noqa: BLE001
                        pass
            STATE.set(tws_connected=False, mdt=None, account_type=None,
                      error=f"Connecting to TWS {CFG['host']}:{CFG['port']}...")
            delay, next_try = RETRY_MIN, 0.0
        if _is_connected(ib):
            if not STATE.snapshot()["tws_connected"]:
                STATE.set(tws_connected=True, error=None, account_type=_account_type(ib))
            delay = RETRY_MIN
        else:
            if STATE.snapshot()["tws_connected"]:
                STATE.set(tws_connected=False, mdt=None,
                          error=f"Lost the connection to TWS {CFG['host']}:{CFG['port']}; reconnecting.")
                next_try = 0.0
            if time.monotonic() >= next_try:
                try:
                    await _connect(ib)
                    delay = RETRY_MIN
                    w.loop.create_task(_probe_mdt(ib))
                except Exception:  # noqa: BLE001  (_connect recorded the reason)
                    next_try = time.monotonic() + delay
                    delay = min(RETRY_MAX, delay * 2)
        await asyncio.sleep(KEEP_TICK)


# =============================================================== 1.x endpoints
# /chain, /iv and /scan below are the 1.6 code, unchanged: the legacy hidden pages
# (IV Rank, Spread, Positions) still call them.

def _fmt(ymd: str) -> str:
    try:
        return _dt.datetime.strptime(ymd, "%Y%m%d").date().isoformat()
    except Exception:  # noqa: BLE001
        return ymd


def _dte(ymd: str):
    try:
        return (_dt.datetime.strptime(ymd, "%Y%m%d").date() - _dt.date.today()).days
    except Exception:  # noqa: BLE001
        return None


_mkt_type: int | None = None
_mkt_type_at = 0.0          # when that verdict was reached (time.monotonic)
MKT_RECHECK = 900.0         # a "delayed" verdict is re-tested after this many seconds


QUOTE_POLL = 0.25       # how often the quote window looks at what has arrived
QUOTE_MIN = 1.5         # never return sooner than this
QUOTE_QUIET = 1.0       # priced + greeks in, and nothing new for this long = done
QUOTE_STALL = 3.0       # nothing new AT ALL for this long = done, complete or not
_NAN = float("nan")


def _filled(t) -> tuple[int, bool, bool]:
    """(populated fields, has a price, has greeks) for one ticker."""
    def ok(v):
        return v is not None and v == v and v > 0
    priced = (ok(t.bid) and ok(t.ask)) or ok(t.last) or ok(t.close)
    greeks = bool(t.modelGreeks and t.modelGreeks.delta is not None)
    n = sum(1 for v in (t.bid, t.ask, t.last, t.close) if ok(v))
    n += 1 if greeks else 0
    n += sum(1 for v in (t.putOpenInterest, t.callOpenInterest, t.volume)
             if v is not None and v == v)
    return n, priced, greeks


def _forget(ib, contract) -> None:
    """Blank what an EARLIER subscription left on this contract's ticker.

    ib_insync keeps one Ticker per contract for the life of the connection, and
    cancelMktData does not clear it. Reading "has the data arrived yet?" off a
    ticker that still holds last time's numbers answers yes before TWS has said
    anything: a second quote window closed at once with the first one's partial
    greeks, and a spot price could be hours old on a bridge left running all day.
    """
    t = ib.ticker(contract)
    if t is None:
        return
    for name in ("bid", "ask", "last", "close", "volume", "putOpenInterest",
                 "callOpenInterest", "bidSize", "askSize", "lastSize"):
        try:
            setattr(t, name, _NAN)
        except Exception:  # noqa: BLE001
            pass
    t.modelGreeks = t.bidGreeks = t.askGreeks = t.lastGreeks = None
    t.time = None


async def _quote(ib, contracts, fresh=True):
    """Subscribe all, wait until the data has ARRIVED, read it, release the lines.

    ``fresh`` blanks whatever an earlier subscription left on these tickers first
    (see ``_forget``). False only for the second half of the entitlement probe.

    The wait used to be a fixed ``quote_wait`` (8 s) sleep. Measured against a live
    TWS (2026-09-19, 22 JPM puts): every field that was ever going to arrive had
    arrived by ~3.6 s (open interest ~1.0 s, greeks ~1.5-2.0 s, prices ~3.1-3.6 s
    out of hours) and then nothing changed, so 4+ s of each chain was spent asleep.
    Now the window closes once the picture is COMPLETE and has stopped changing:
    every contract priced, greeks in on at least half of them (deep OTM strikes
    never get a model, so "all" would never come true), and no new field for
    QUOTE_QUIET. Both halves are required - greeks arrive before prices out of
    hours and after them in hours, and either alone closes the window on a chain
    that cannot be graded. A feed that sends nothing at all (no entitlement for
    this market data type) ends after QUOTE_STALL instead of the full wait. "New"
    means a FIELD becoming populated, not a tick: in market hours bid/ask ticks
    never stop, but the set of filled fields saturates. ``quote_wait`` remains the
    ceiling, so a slow feed still gets its full 8 s.

    reqTickersAsync is not used: it waits for EVERY contract's snapshot to end, so
    on a delayed feed it always burns its full timeout.

    Generic tick 101 = option OPEN INTEREST (1.4). It is not part of the default
    tick set, so without asking for it every leg's OI came back empty and the
    liquidity check could only look at the bid/ask. Streaming request (not a
    snapshot) on purpose: IBKR refuses generic ticks on snapshots. Day VOLUME
    needs nothing extra - it is in the default set.
    """
    for c in contracts:
        try:
            if fresh:
                _forget(ib, c)
            ib.reqMktData(c, "101" if getattr(c, "secType", "") == "OPT" else "", False, False)
        except Exception:  # noqa: BLE001
            pass
    t0 = last_change = time.monotonic()
    seen = 0
    while True:
        await asyncio.sleep(QUOTE_POLL)
        now = time.monotonic()
        total = n_priced = n_greeks = 0
        for c in contracts:
            try:
                n, priced, greeks = _filled(ib.ticker(c))
            except Exception:  # noqa: BLE001
                n, priced, greeks = 0, False, False
            total += n
            n_priced += priced
            n_greeks += greeks
        if total != seen:
            seen, last_change = total, now
        quiet = now - last_change
        if now - t0 >= CFG["quote_wait"]:
            break
        complete = (bool(contracts) and n_priced == len(contracts)
                    and n_greeks * 2 >= len(contracts))
        if now - t0 >= QUOTE_MIN and ((complete and quiet >= QUOTE_QUIET)
                                      or quiet >= QUOTE_STALL):
            break
    out = []
    for c in contracts:
        try:
            out.append(ib.ticker(c))
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                ib.cancelMktData(c)
            except Exception:  # noqa: BLE001
                pass
    return [t for t in out if t is not None]


def _count(v):
    """A tick count (open interest, volume) or None. ib_insync leaves a tick that
    never arrived as NaN; 0 is a real answer ("nobody holds this strike")."""
    try:
        return int(v) if (v is not None and v == v and v >= 0) else None
    except (TypeError, ValueError):
        return None


def _open_interest(t, right):
    """IBKR reports a contract's OI on the tick for ITS side: 27 (call) / 28 (put)."""
    first, second = ((t.putOpenInterest, t.callOpenInterest) if right.startswith("P")
                     else (t.callOpenInterest, t.putOpenInterest))
    oi = _count(first)
    return oi if oi is not None else _count(second)


def _row(t, right):
    c, g = t.contract, t.modelGreeks
    bid = t.bid if t.bid and t.bid > 0 else None
    ask = t.ask if t.ask and t.ask > 0 else None
    mid = round((bid + ask) / 2, 4) if (bid is not None and ask is not None) else None
    last = t.last if t.last and t.last > 0 else (t.close if t.close and t.close > 0 else None)
    return {
        "right": right, "strike": float(c.strike), "bid": bid, "ask": ask,
        "mid": mid, "last": last,
        "spread_pct": (round((ask - bid) / mid * 100, 1)
                       if (bid is not None and ask is not None and mid) else None),
        "iv": round(g.impliedVol * 100, 1) if (g and g.impliedVol) else None,
        "delta": round(g.delta, 3) if (g and g.delta is not None) else None,
        "gamma": round(g.gamma, 4) if (g and g.gamma is not None) else None,
        "theta": round(g.theta, 3) if (g and g.theta is not None) else None,
        "vega": round(g.vega, 3) if (g and g.vega is not None) else None,
        "oi": _open_interest(t, right),
        "volume": _count(t.volume),
    }


SPOT_WAIT = 6.0         # longest wait for any price. Out of hours the close lands at
                        # 3.1-3.6 s (measured); 3.0 was tried first and failed HD / XOM
SPOT_SETTLE = 0.8       # after this, yesterday's close is good enough to pick strikes


async def _spot(ib, stock):
    """The underlying's price, as fast as TWS can give it.

    This was ``reqTickersAsync`` - a SNAPSHOT, which IBKR holds open until the
    snapshot "ends": up to 11 seconds whenever no fresh trade arrives (every
    weekend, every evening, and on thin names in hours). Measured 11.1 s on JPM
    with the close sitting in the ticker after 0.3 s. The spot only anchors which
    strikes to quote, so a streaming subscription read as soon as it has a number
    is enough: a live price the moment one exists, otherwise the close once the
    feed has had SPOT_SETTLE to show it has nothing better. If the current market
    data type yields nothing at all (no live subscription for this stock), the free
    delayed-frozen type is tried once before giving up.
    """
    def ok(v):
        return v is not None and v == v and v > 0

    async def attempt():
        _forget(ib, stock)
        ib.reqMktData(stock, "", False, False)
        t0 = time.monotonic()
        try:
            while True:
                await asyncio.sleep(0.15)
                el = time.monotonic() - t0
                t = ib.ticker(stock)
                if t is not None:
                    mp = t.marketPrice()
                    if ok(mp):
                        return float(mp)
                    if el >= SPOT_SETTLE:
                        for v in (t.last, t.close):
                            if ok(v):
                                return float(v)
                if el >= SPOT_WAIT:
                    return None
        finally:
            try:
                ib.cancelMktData(stock)
            except Exception:  # noqa: BLE001
                pass

    price = await attempt()
    if price is None and _mkt_type != 4:
        ib.reqMarketDataType(4)         # _chain sets the type it wants again before quoting
        price = await attempt()
    return price


async def _chain_def(ib, symbol):
    from ib_insync import Stock

    q = await ib.qualifyContractsAsync(Stock(symbol, "SMART", "USD"))
    if not q:
        raise RuntimeError(f"IBKR does not recognise the symbol {symbol}.")
    stock = q[0]
    # The price and the chain definition need nothing from each other - ask together.
    spot, params = await asyncio.gather(
        _spot(ib, stock),
        ib.reqSecDefOptParamsAsync(stock.symbol, "", "STK", stock.conId))
    if not spot:
        raise RuntimeError(f"No price for {symbol} — market closed with no frozen data.")
    chains = [p for p in params if p.exchange == "SMART"] or list(params)
    if not chains:
        raise RuntimeError(f"IBKR returned no option chain for {symbol}.")
    return stock, float(spot), chains[0]


PUT_SIDE_BELOW = 26     # puts-only mode: strikes quoted below spot ...
PUT_SIDE_ABOVE = 2      # ... and above it
PUT_SIDE_FLOOR = 0.72   # never probe further than 28% under the price


async def _chain(symbol, expiry=None, dte_min=None, dte_max=None, put_side=False):
    """One expiry of the chain.

    ``put_side`` is the bull put spread view (1.3): PUTS only, from just above
    the price down. The symmetric window (10 strikes each way) is right for
    reading a chain but wrong for finding a short put at delta 0.20-0.25 - on a
    $700 stock with $5 strikes it stops 7% under the price, well short of where
    that delta sits at 45-60 days. Dropping the calls pays for the longer reach:
    28 puts cost fewer market-data lines than 21 calls + 21 puts.
    """
    global _mkt_type
    from ib_insync import Option

    w = worker()
    await _connect(w.ib)
    ib = w.ib
    _, spot, chain = await _chain_def(ib, symbol)

    exps = sorted(chain.expirations)
    if not exps:
        raise RuntimeError(f"No listed expirations for {symbol}.")
    if expiry and expiry in exps:
        chosen = expiry
    elif dte_min is not None:
        dated = [(e, _dte(e)) for e in exps if _dte(e) is not None]
        inside = [e for e in dated if dte_min <= e[1] <= dte_max]
        pool = inside or dated
        chosen = min(pool, key=lambda e: abs(e[1] - (dte_min + dte_max) / 2))[0] if pool else exps[0]
    else:
        chosen = exps[0]

    # reqSecDefOptParams returns the UNION of strikes across all expirations, so
    # most do not exist on a given monthly. Qualify a generous slice first (free,
    # no market data), then quote only strikes that really exist — otherwise the
    # chain comes back starved and the closest-to-target-delta strike changes.
    strikes = sorted(chain.strikes)
    near = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
    win = CFG["strike_window"]
    if put_side:
        lo = spot * PUT_SIDE_FLOOR
        probe = [k for k in strikes[:near + PUT_SIDE_ABOVE * 3 + 1] if k >= lo][-90:]
        rights = ("P",)
    else:
        probe = strikes[max(0, near - win * 3): near + win * 3 + 1]
        rights = ("C", "P")
    cand = [Option(symbol, chosen, k, r, "SMART", tradingClass=chain.tradingClass)
            for k in probe for r in rights]
    qualified = await ib.qualifyContractsAsync(*cand)
    real = sorted({c.strike for c in qualified if getattr(c, "conId", None)})
    if not real:
        raise RuntimeError(f"No {symbol} {_fmt(chosen)} contracts qualified near spot.")
    j = min(range(len(real)), key=lambda i: abs(real[i] - spot))
    if put_side:
        keep = set(real[max(0, j - PUT_SIDE_BELOW): j + PUT_SIDE_ABOVE + 1])
    else:
        keep = set(real[max(0, j - win): j + win + 1])
    contracts = [c for c in qualified if getattr(c, "conId", None) and c.strike in keep]

    # Sticky entitlement probe: one modelGreeks in forty is noise, not a live feed.
    # A "delayed" verdict EXPIRES (1.5): it used to last for the life of the process,
    # so a bridge started on a Saturday - when even a fully entitled account gets few
    # model greeks - kept serving 15-minute-old quotes through Monday's session.
    global _mkt_type_at
    greeks_from_delayed = False
    if _mkt_type == 4 and time.monotonic() - _mkt_type_at > MKT_RECHECK:
        _mkt_type = None
    if _mkt_type is None:
        ib.reqMarketDataType(1)
        tickers = await _quote(ib, contracts)
        got = sum(1 for x in tickers if x.modelGreeks)
        if tickers and got >= max(2, len(tickers) // 2):
            _mkt_type = 1
        else:
            _mkt_type = 4       # delayed-frozen; IBKR provides it free
            ib.reqMarketDataType(_mkt_type)
            # fresh=False: keep what the live attempt did deliver. TWS does not
            # resend model greeks to a re-subscription seconds after the first, so
            # wiping them here returned a chain with prices and NO deltas.
            tickers = await _quote(ib, contracts, fresh=False)
        _mkt_type_at = time.monotonic()
    else:
        ib.reqMarketDataType(_mkt_type)
        tickers = await _quote(ib, contracts)
        # A chain with NO deltas cannot be graded at all (the short leg is chosen by
        # delta). Out of hours TWS sometimes sends no model greeks for a large chain
        # under one market data type and does under the other (COST, 29 puts,
        # 2026-09-19: 0 under one, 11 under the other, and not always the same one).
        # One extra window with the OTHER type, only in that case; prices already
        # read are kept and the sticky verdict does not change.
        if tickers and not any(x.modelGreeks for x in tickers):
            ib.reqMarketDataType(4 if _mkt_type == 1 else 1)
            tickers = await _quote(ib, contracts, fresh=False)
            greeks_from_delayed = _mkt_type == 1 and any(x.modelGreeks for x in tickers)
            ib.reqMarketDataType(_mkt_type)   # the next chain's spot reads the right feed

    calls, puts = [], []
    for t in tickers:
        r = getattr(t.contract, "right", "")
        (calls if r.startswith("C") else puts).append(_row(t, r))
    calls.sort(key=lambda r: r["strike"])
    puts.sort(key=lambda r: r["strike"])
    atm_src = calls or puts
    atm_iv = min(atm_src, key=lambda r: abs(r["strike"] - spot)).get("iv") if atm_src else None

    return {
        "ok": True, "symbol": symbol, "spot": round(spot, 2),
        "expiry": chosen, "expiry_label": _fmt(chosen), "dte": _dte(chosen),
        "expirations": [{"value": e, "label": _fmt(e), "dte": _dte(e)} for e in exps[:24]],
        "calls": calls, "puts": puts, "atm_iv": atm_iv,
        "data_mode": "live" if _mkt_type == 1 else "delayed",
        # true = the live feed sent no model greeks, so deltas came from the delayed one
        "greeks_from_delayed": greeks_from_delayed,
        "greeks_ok": any(c.get("delta") is not None for c in calls + puts),
        # did TWS send open interest at all? False = a feed that carries no OI, which
        # the server reports as "unknown" rather than grading every leg as empty
        "oi_ok": any(c.get("oi") is not None for c in calls + puts),
        "bridge": Handler.server_version.split("/")[-1],
        "strike_window": win, "put_side": bool(put_side),
        "source": f"TWS {CFG['host']}:{CFG['port']}",
    }


IV_SERIES_MAX = 400     # 1.6: /iv?series=1 returns at most this many daily points


def _bar_day(b):
    """``YYYY-MM-DD`` of a daily bar. With ``formatDate=1`` ib_insync hands a
    ``datetime.date`` for day bars, but a raw ``YYYYMMDD`` string has been seen on
    older builds - accept both."""
    d = getattr(b, "date", None)
    if hasattr(d, "isoformat"):
        return d.isoformat()[:10]
    s = str(d or "").strip()
    if len(s) >= 8 and s[:8].isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s[:10]


async def _iv(symbol, series=False):
    from ib_insync import Stock

    w = worker()
    await _connect(w.ib)
    ib = w.ib
    q = await ib.qualifyContractsAsync(Stock(symbol, "SMART", "USD"))
    if not q:
        raise RuntimeError(f"IBKR does not recognise {symbol}.")
    bars = await ib.reqHistoricalDataAsync(
        q[0], endDateTime="", durationStr="1 Y", barSizeSetting="1 day",
        whatToShow="OPTION_IMPLIED_VOLATILITY", useRTH=True, formatDate=1)
    good = [b for b in (bars or []) if b.close and b.close > 0]
    vals = [b.close for b in good]
    # 1.6: the dated daily series in PERCENT - round(close * 100, 1), the unit
    # iv_current / iv_low / iv_high below already use - oldest first, at most
    # IV_SERIES_MAX points. The server stores it AS-IS (option_store.bootstrap_iv)
    # and never lets it overwrite a day it read itself. Only when asked for, so
    # the IV Rank page and the Watchlist tab see an unchanged reply.
    extra = {}
    if series:
        pts = [{"on": _bar_day(b), "iv": round(b.close * 100, 1)} for b in good]
        pts = [p for p in pts if p["on"]]
        extra["series"] = pts[-IV_SERIES_MAX:]
    if len(vals) < 30:
        return {"ok": True, "iv_percentile": None, "iv_rank": None,
                "iv_current": None, "n": len(vals),
                "note": "Not enough IV history from IBKR to compute a percentile.", **extra}
    cur, lo, hi = vals[-1], min(vals), max(vals)
    return {"ok": True, "iv_current": round(cur * 100, 1),
            "iv_percentile": round(sum(1 for v in vals if v < cur) / len(vals) * 100, 1),
            "iv_rank": round((cur - lo) / (hi - lo) * 100, 1) if hi > lo else None,
            "iv_low": round(lo * 100, 1), "iv_high": round(hi * 100, 1),
            "n": len(vals), "note": "", **extra}


async def _scan(iv_rank: float, price: float, volume: float, rows: int = 50):
    """The member's TWS "High IV Rank" scanner through the API.

    ``SCAN_ivRank52w_DESC`` is the API name of TWS's "52 Week IV Rank" sort, and
    ``ivRank52wAbove`` / ``priceAbove`` / ``volumeAbove`` are the scanner's own
    filter codes (read from reqScannerParameters, 2026-09-18). The rank filter is
    in PERCENT (30 = "greater than 30"), like the TWS field. The scanner returns
    contracts in rank order but not the rank figure itself - the page reads that
    per ticker from /iv.
    """
    from ib_insync import ScannerSubscription, TagValue

    w = worker()
    await _connect(w.ib)
    ib = w.ib
    sub = ScannerSubscription(instrument="STK", locationCode="STK.US.MAJOR",
                              scanCode="SCAN_ivRank52w_DESC",
                              numberOfRows=max(1, min(int(rows), 50)))
    tags = [TagValue("ivRank52wAbove", "%g" % iv_rank)]
    if price > 0:
        tags.append(TagValue("priceAbove", "%g" % price))
    if volume > 0:
        tags.append(TagValue("volumeAbove", "%d" % int(volume)))
    data = await ib.reqScannerDataAsync(sub, [], tags)
    seen, symbols = set(), []
    for d in data or []:
        try:
            s = (d.contractDetails.contract.symbol or "").strip().upper().replace(" ", ".")
        except Exception:  # noqa: BLE001
            continue
        if s and s not in seen:
            seen.add(s)
            symbols.append(s)
    return {"ok": True, "symbols": symbols, "n": len(symbols),
            "scan_code": "SCAN_ivRank52w_DESC",
            "criteria": {"iv_rank": iv_rank, "price": price, "volume": volume}}


# =============================================================== 2.0 endpoints
class BadRequest(ValueError):
    """Input the caller got wrong (answered 400), as opposed to an IBKR failure."""


class ConnectorBusy(RuntimeError):
    """Another read held the connector too long to start this one: answered
    ``{"ok": false, "busy": true, "error"}`` so the page can tell it from a TWS failure."""


CHAIN2_TTL = 20.0           # same (symbol, spec) inside this many seconds = one TWS read
CHAIN2_TIMEOUT = 150.0      # under the page's 160 s fetch limit, so it gets a JSON answer
CHAIN2_MARGIN = 15.0        # the read's deadline is this long before CHAIN2_TIMEOUT: by
                            # then th_ibkr stops starting waves and returns what it read
                            # ("partial"), instead of being cancelled with nothing
CHAIN2_MIN_LEFT = 20.0      # after waiting this long for another read, too little time is
                            # left for a useful one: answer "busy" rather than an empty read
SPOT_TIMEOUT = 20.0
UND_TIMEOUT = 100.0
HIST_TTL = 24 * 3600.0      # the history key also carries the date: one pull per symbol per day
HIST_BARS = 260             # about one year of sessions (th_ibkr paces the requests)
PLAN_KEYS = ("iv_hint", "expiries", "max_weekly_dte", "max_dte", "sigma_k",
             "min_side", "max_side", "max_expiries")
# iv_hint is a FRACTION; th_ibkr.plan reads a value above 5 as a percent, so allow it.
_SPEC_NUM = {"spot": (0.0, 1e7), "iv_hint": (0.0, 500.0), "sigma_k": (0.0, 10.0)}
MAX_SPEC_EXPIRIES = 80
_SPEC_INT = {"max_weekly_dte": (0, 4000), "max_dte": (0, 4000),
             "min_side": (0, 400), "max_side": (0, 400),
             "max_expiries": (0, MAX_SPEC_EXPIRIES)}     # 0 = no cap
_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9 .\-]{0,11}$")


def clean_symbol(raw) -> str:
    s = str(raw or "").strip().upper()
    if not _SYMBOL_RE.match(s):
        raise BadRequest("symbol is required (letters, digits, '.', '-', up to 12)")
    return s


def parse_spec(raw) -> dict:
    """The fetch-window spec (design §3.2) from its JSON text -> the keys plan() takes
    plus ``spot``. Unknown keys (``symbol``) are ignored; None means "plan's default"."""
    if raw is None or str(raw).strip() == "":
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise BadRequest(f"spec is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise BadRequest("spec must be a JSON object")
    out: dict = {}
    for k, (lo, hi) in _SPEC_NUM.items():
        v = data.get(k)
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) \
                or not lo < v <= hi:
            raise BadRequest(f"spec.{k} must be a number above {lo:g} and at most {hi:g}")
        out[k] = float(v)
    for k, (lo, hi) in _SPEC_INT.items():
        v = data.get(k)
        if v is None:
            continue
        iv = _as_int(v) if not isinstance(v, str) else None
        if iv is None or not lo <= iv <= hi:
            raise BadRequest(f"spec.{k} must be a whole number from {lo} to {hi}")
        out[k] = iv
    exps = data.get("expiries")
    if exps is not None:
        if not isinstance(exps, list) or len(exps) > MAX_SPEC_EXPIRIES:
            raise BadRequest(f"spec.expiries must be a list of at most {MAX_SPEC_EXPIRIES} dates")
        good = []
        for e in exps:
            try:
                good.append(_dt.date.fromisoformat(str(e)[:10]).isoformat())
            except ValueError:
                raise BadRequest(f"spec.expiries: {e!r} is not a YYYY-MM-DD date") from None
        out["expiries"] = sorted(set(good))
    return out


async def _try_spot(th, ib, symbol):
    try:
        return await th.spot(ib, symbol)
    except Exception:  # noqa: BLE001  (the spec's spot is the fallback)
        return None


def _takes(fn, name: str) -> bool:
    """Does ``fn`` accept the keyword ``name`` (by name or through ``**kwargs``)? A
    th_ibkr.py older than the connector may not know ``deadline`` yet."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is p.VAR_KEYWORD for p in params.values())


async def _chain2(symbol: str, spec: dict) -> dict:
    """One chain read for the window ``spec`` describes, one chain at a time.

    ``th_ibkr.fetch`` is that read (chain_defs + a fresh spot -> plan -> quote), the
    same one the Hermes collector does; ``_chain2_steps`` spells it out for a
    th_ibkr that only has the parts. The window is planned on the price TWS gives
    now; the spec's stored spot is the fallback (it can be days old). The spec's
    ``max_expiries`` / ``max_side`` (a member's chunk) reach ``plan``.

    The read has a deadline CHAIN2_MARGIN before CHAIN2_TIMEOUT, counted from the
    request: once it passes th_ibkr starts no new wave and returns the rows read so
    far with ``partial`` True - a big chain is never cancelled with nothing."""
    t0 = time.monotonic()
    th = _th()
    ib = _ib()
    await _connect(ib)
    lines = int(CFG["max_lines"])
    async with _loop_lock("quote"):
        waited = time.monotonic() - t0
        left = max(0.0, CHAIN2_TIMEOUT - CHAIN2_MARGIN - waited)
        if waited > 1.0 and left < CHAIN2_MIN_LEFT:
            raise ConnectorBusy(f"The connector was busy with another read for {waited:.0f} s - "
                                "try again.")
        if callable(getattr(th, "fetch", None)):
            kw = {"deadline": left} if _takes(th.fetch, "deadline") else {}
            out = dict(await th.fetch(ib, symbol, spec, max_lines=lines, **kw) or {})
        else:
            out = await _chain2_steps(th, ib, symbol, spec, lines, deadline=left)
    out["symbol"] = out.get("symbol") or symbol
    name = _mdt_name(out.get("mdt")) or _mdt_name(out.get("spot_mdt"))
    out["mdt"] = name
    if name:
        STATE.set(mdt=name)
    out["partial"] = bool(out.get("partial"))
    out.update(ok=True, connector_version=VERSION)
    return out


async def _chain2_steps(th, ib, symbol: str, spec: dict, lines: int, *, deadline=None) -> dict:
    t0 = time.monotonic()
    defs, sp = await asyncio.gather(th.chain_defs(ib, symbol), _try_spot(th, ib, symbol))
    spot = _pos((sp or {}).get("spot")) or _pos(spec.get("spot"))
    if not spot:
        raise RuntimeError(f"No price for {symbol} from TWS, and none in the request.")
    kw = {k: spec[k] for k in PLAN_KEYS if spec.get(k) is not None}
    window = th.plan(defs, spot=spot, **kw)
    qkw = {}
    if deadline is not None and _takes(th.quote, "deadline"):
        qkw["deadline"] = max(0.0, deadline - (time.monotonic() - t0))
    out = dict(await th.quote(ib, symbol, window, max_lines=lines, **qkw) or {})
    if not _pos(out.get("spot")):
        out["spot"] = spot
    out.setdefault("spot_mdt", (sp or {}).get("mdt"))
    return out


async def _und_history(symbol: str) -> dict:
    """One year of daily bars + IBKR's daily 30-day IV. th_ibkr spaces historical
    requests (60 per 10 minutes per login), so this takes 11 s or more."""
    th = _th()
    ib = _ib()
    await _connect(ib)
    bars = list(await th.daily_bars(ib, symbol, "1 Y") or [])
    if not bars:
        # ib_insync answers a failed historical request (pacing, HMDS busy or down, its
        # own timeout) with an EMPTY list, not an error. Raising keeps that out of the
        # one-pull-per-day cache, so the next ask tries IBKR again.
        raise RuntimeError(f"IBKR returned no daily bars for {symbol} (its history service "
                           "is busy or has no data) - try again later.")
    ivs = await th.iv_history(ib, symbol, "1 Y")
    return {"bars": bars[-HIST_BARS:], "iv_series": list(ivs or [])[-HIST_BARS:]}


async def _und_spot(symbol: str) -> dict:
    th = _th()
    ib = _ib()
    await _connect(ib)
    return dict(await th.spot(ib, symbol) or {})


async def _account():
    """Net liquidation + currency (th_ibkr.account; the 1.x summary read if absent)."""
    ib = _ib()
    await _connect(ib)
    if th_ibkr is not None and hasattr(th_ibkr, "account"):
        a = dict(await th_ibkr.account(ib) or {})
        return {"ok": True, "net_liquidation": a.get("net_liquidation"),
                "currency": a.get("currency")}
    for r in await ib.accountSummaryAsync():
        if r.tag == "NetLiquidation":
            try:
                return {"ok": True, "net_liquidation": float(r.value),
                        "currency": getattr(r, "currency", None) or None}
            except (TypeError, ValueError):
                break
    return {"ok": True, "net_liquidation": None, "currency": None}


# --------------------------------------------------------------- response cache
_CACHE_TTL = 45.0
_CACHE_MAX = 400
_cache: dict = {}
_cache_lock = threading.Lock()
_inflight: dict = {}


def cached(key, fn, ttl=_CACHE_TTL):
    """Serve ``key`` from memory while fresh; otherwise run ``fn`` once even when
    several requests ask at the same moment (they wait for the first). Errors are
    not cached."""
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
        klock = _inflight.setdefault(key, threading.Lock())
    with klock:
        with _cache_lock:
            hit = _cache.get(key)
            if hit and hit[0] > time.time():
                return hit[1]
        val = fn()
        with _cache_lock:
            _cache[key] = (time.time() + ttl, val)   # stamp at STORE time, not entry
            if len(_cache) > _CACHE_MAX:
                now = time.time()
                for k in [k for k, v in _cache.items() if v[0] <= now]:
                    _cache.pop(k, None)
                    lk = _inflight.get(k)
                    if lk is not None and not lk.locked():
                        _inflight.pop(k, None)
        return val


def clear_caches() -> None:
    with _cache_lock:
        _cache.clear()


def _json_safe(obj):
    """NaN / inf -> None (JSON.parse rejects them); dates -> ISO text."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, (_dt.date, _dt.datetime)):
        return obj.isoformat()
    return obj


# --------------------------------------------------------------- settings
def form_values() -> dict:
    """What the settings form shows: the file, plus this run's CLI overrides."""
    conf, _ = load_config(config_path())
    for k, v in (RUN.get("overrides") or {}).items():
        if k in _OVERRIDE_KEYS and v is not None:
            conf[_OVERRIDE_KEYS[k]] = v
    return conf


def update_settings(data) -> tuple[dict, list[str]]:
    """Validate ``data`` over the saved file, write it, apply it, reconnect.
    A Save replaces this run's command-line overrides (the member's latest choice)."""
    path = config_path()
    base, _ = load_config(path)
    conf, errors = validate_config(data, base=base)
    if errors:
        return conf, errors
    try:
        save_config(conf, path)
    except (OSError, ValueError) as exc:
        return conf, [f"Could not write {path}: {exc}"]
    RUN["overrides"] = {}
    RUN["load_problems"] = []
    apply_config(conf)
    clear_caches()
    w = _worker
    if w is not None:
        STATE.set(tws_connected=False, mdt=None, account_type=None,
                  error=f"Connecting to TWS {CFG['host']}:{CFG['port']}...")
        w.request_reconnect()
    return conf, []


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TradeHunter IBKR connector</title>
<style>
:root{color-scheme:dark;--bg:#0b1220;--panel:#111a2e;--line:#22304d;--text:#e2e8f0;--muted:#94a3b8;--ok:#10b981;--warn:#f59e0b;--bad:#ef4444}
*{box-sizing:border-box;scrollbar-width:thin;scrollbar-color:transparent transparent}
*:hover{scrollbar-color:rgba(148,163,184,.35) transparent}
*::-webkit-scrollbar{width:8px;height:8px}*::-webkit-scrollbar-thumb{background:transparent;border-radius:4px}
*:hover::-webkit-scrollbar-thumb{background:rgba(148,163,184,.35)}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:640px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:18px;margin:0 0 4px}h1 span{color:var(--muted);font-weight:400;font-size:14px}
.sub{color:var(--muted);margin:0 0 18px}
.status{display:flex;gap:10px;align-items:flex-start;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:var(--panel);margin-bottom:18px}
.dot{width:10px;height:10px;border-radius:50%;background:var(--bad);flex:none;margin-top:5px}
.dot.ok{background:var(--ok)}.dot.warn{background:var(--warn)}
form{border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:16px}
label{display:block;font-weight:600;margin:14px 0 4px}
.first{margin-top:0}
.help{color:var(--muted);font-size:12px;margin-top:4px}
input,textarea{width:100%;background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:8px 10px;font:inherit}
input:focus,textarea:focus,button:focus-visible{outline:2px solid var(--ok);outline-offset:-1px}
.presets{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.presets button{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:999px;padding:4px 10px;font:inherit;font-size:12px;cursor:pointer}
.presets button.on{border-color:var(--ok);color:var(--ok)}
.row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media (max-width:480px){.row{grid-template-columns:1fr}}
.save{margin-top:18px;background:var(--ok);color:#04120c;border:0;border-radius:6px;padding:10px 16px;font:inherit;font-weight:700;cursor:pointer}
.msg{margin-top:12px;min-height:1.5em}.msg.ok{color:var(--ok)}.msg.bad{color:var(--bad)}
.note{margin-top:14px;padding:8px 10px;border:1px solid var(--warn);border-radius:6px;color:var(--warn);font-size:12px}
details{margin-top:14px}summary{cursor:pointer;color:var(--muted)}
.foot{color:var(--muted);font-size:12px;margin-top:18px}
.foot a{color:var(--ok)}
code{background:var(--bg);border:1px solid var(--line);border-radius:4px;padding:0 4px;word-break:break-all}
</style>
</head>
<body>
<main>
<h1>TradeHunter IBKR connector <span>@@VERSION@@</span></h1>
<p class="sub">Runs on this PC and reads option data from your own TWS or IB Gateway, read-only.
The TradeHunter Options page uses it while the page is open.</p>
<div class="status" role="status" aria-live="polite"><span class="dot" id="dot"></span><span id="statusText">Checking the connection...</span></div>
<form id="f" method="post" action="/settings">
<label class="first" for="tws_port">TWS / IB Gateway API port</label>
<input id="tws_port" name="tws_port" type="number" min="1" max="65535" value="@@PORT@@" required>
<div class="presets">@@PRESETS@@</div>
<div class="help">Must match TWS: File &gt; Global Configuration &gt; API &gt; Settings &gt; Socket port
(IB Gateway: Configure &gt; Settings &gt; API &gt; Settings).</div>
<div class="row">
<div><label for="tws_host">TWS host</label>
<input id="tws_host" name="tws_host" value="@@HOST@@" required>
<div class="help">127.0.0.1 when TWS runs on this PC.</div></div>
<div><label for="client_id">Client ID</label>
<input id="client_id" name="client_id" type="number" min="0" max="999999" value="@@CID@@" required>
<div class="help">Any number no other API program uses. If it is held, the next free one is used.</div></div>
</div>
<label for="max_lines">Market-data lines at once</label>
<input id="max_lines" name="max_lines" type="number" min="5" max="200" value="@@LINES@@" required>
<div class="help">How many option quotes are read at the same time. An IBKR login has 100 lines by
default, shared with your TWS windows, so 40 leaves room.</div>
<details@@DETAILS_OPEN@@><summary>Advanced: extra allowed web pages</summary>
<label for="allowed_origins">Extra allowed origins, one per line</label>
<textarea id="allowed_origins" name="allowed_origins" rows="3" spellcheck="false">@@ORIGINS@@</textarea>
<div class="help">TradeHunter (https://app.tradehunter.net) and local development pages on
127.0.0.1 / localhost ports 8000-8099 are always allowed. Add only pages you run yourself,
e.g. https://my-dev-box:8443.</div>
</details>
@@NOTES@@
<button class="save" type="submit">Save &amp; reconnect</button>
<div class="msg@@MSG_CLASS@@" id="msg" role="alert">@@MSG@@</div>
</form>
<p class="foot">Settings file: <code>@@PATH@@</code><br>
In TWS tick <b>Enable ActiveX and Socket Clients</b>. <b>Read-Only API</b> can stay ticked - the connector never places orders.<br>
<a href="https://app.tradehunter.net/options">Back to TradeHunter Options</a></p>
</main>
<script>
(function () {
  var port = document.getElementById('tws_port');
  var presets = document.querySelectorAll('.presets button');
  function mark() {
    for (var i = 0; i < presets.length; i++) {
      presets[i].classList.toggle('on', presets[i].getAttribute('data-port') === String(port.value));
    }
  }
  for (var i = 0; i < presets.length; i++) {
    presets[i].addEventListener('click', function (e) { port.value = e.currentTarget.getAttribute('data-port'); mark(); });
  }
  port.addEventListener('input', mark);
  mark();

  var dot = document.getElementById('dot'), txt = document.getElementById('statusText');
  function show(h) {
    if (!h) { dot.className = 'dot'; txt.textContent = 'The connector is not answering.'; return; }
    if (h.tws_connected) {
      dot.className = 'dot ok';
      txt.textContent = 'Connected to TWS ' + h.tws + ' (client ID ' + h.client_id + ')' +
        (h.account_type ? ' - ' + h.account_type + ' account' : '') +
        (h.mdt ? ' - ' + String(h.mdt).replace('_', ' ') + ' data' : ' - data type shows after the first read');
    } else {
      dot.className = 'dot warn';
      var e = h.error ? String(h.error) : 'Not connected to TWS ' + h.tws + '.';
      if (!/[.!?]$/.test(e)) { e += '.'; }
      txt.textContent = /^Connecting/.test(e) ? e : e + ' Retrying automatically.';
    }
  }
  function poll() {
    fetch('/health', { cache: 'no-store' }).then(function (r) { return r.json(); })
      .then(show).catch(function () { show(null); });
  }
  poll();
  setInterval(poll, 2000);

  var f = document.getElementById('f'), msg = document.getElementById('msg');
  f.addEventListener('submit', function (e) {
    e.preventDefault();
    var body = {};
    ['tws_host', 'tws_port', 'client_id', 'max_lines', 'allowed_origins'].forEach(function (k) {
      body[k] = document.getElementById(k).value;
    });
    msg.className = 'msg'; msg.textContent = 'Saving...';
    fetch('/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (j.ok) { msg.className = 'msg ok'; msg.textContent = 'Saved. Reconnecting to TWS ' + j.config.tws_host + ':' + j.config.tws_port + '...'; poll(); }
        else { msg.className = 'msg bad'; msg.textContent = (j.errors || [j.error]).join(' '); }
      })
      .catch(function () { msg.className = 'msg bad'; msg.textContent = 'The connector did not answer.'; });
  });
})();
</script>
</body>
</html>
"""


def settings_page(message: str = "", errors=None, values=None) -> str:
    """The self-contained settings page (inline CSS + JS, no external requests)."""
    v = values or form_values()
    esc = html.escape
    presets = "".join(f'<button type="button" data-port="{p}">{esc(n)} {p}</button>'
                      for n, p in PORT_PRESETS)
    notes = []
    ov = RUN.get("overrides") or {}
    if ov:
        flags = ", ".join(f"{_OVERRIDE_KEYS[k]}={val}" for k, val in ov.items() if k in _OVERRIDE_KEYS)
        notes.append("This run was started with command-line settings (" + esc(flags) + "), shown "
                     "above. Save writes what is shown to the settings file.")
    for p in RUN.get("load_problems") or []:
        notes.append("Settings file problem: " + esc(p))
    if th_ibkr is None:
        notes.append("th_ibkr.py is missing next to the connector (" + esc(str(_TH_ERROR)) + "). "
                     "Download the connector again from the Options page.")
    msg_class = " bad" if errors else (" ok" if message else "")
    msg = esc(" ".join(errors)) if errors else esc(message)
    page = _PAGE
    for token, val in (
        ("@@VERSION@@", esc(VERSION)),
        ("@@PORT@@", esc(str(v["tws_port"]))),
        ("@@HOST@@", esc(str(v["tws_host"]))),
        ("@@CID@@", esc(str(v["client_id"]))),
        ("@@LINES@@", esc(str(v["max_lines"]))),
        ("@@ORIGINS@@", esc("\n".join(v.get("allowed_origins") or []))),
        ("@@DETAILS_OPEN@@", " open" if v.get("allowed_origins") else ""),
        ("@@PRESETS@@", presets),
        ("@@NOTES@@", "".join(f'<div class="note">{n}</div>' for n in notes)),
        ("@@MSG_CLASS@@", msg_class),
        ("@@MSG@@", msg),
        ("@@PATH@@", esc(str(config_path()))),
    ):
        page = page.replace(token, val)
    return page


# --------------------------------------------------------------- HTTP layer
class Handler(BaseHTTPRequestHandler):
    # 2.0: settings page + connector.json, /chain2, /underlying, cached /health.
    # 1.1 /scan; 1.2 one bridge per port; 1.3 put-side chain; 1.4 open interest per leg;
    # 1.5 no fixed waits (spot + quote window); 1.6 /iv?series=1 (dated daily IV, percent)
    server_version = "TradeHunterIBKRBridge/" + VERSION
    MAX_BODY = 16 * 1024

    def _own_port(self) -> int:
        try:
            return int(self.server.server_address[1])
        except Exception:  # noqa: BLE001
            return BRIDGE_PORT

    def _origin_ok(self):
        o = self.headers.get("Origin")
        # No Origin => a direct visit (curl, address bar), not a cross-site read.
        return (None, True) if o is None else (o, origin_allowed(o))

    def _send(self, code, payload, origin=None):
        body = json.dumps(_json_safe(payload), allow_nan=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, code, page: str):
        body = page.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")      # no clickjacking of Save
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'unsafe-inline'; "
                         "script-src 'unsafe-inline'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _deny(self, code=403, msg="Forbidden"):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()
        if msg:
            sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] refused: {msg}\n")

    def do_OPTIONS(self):  # noqa: N802  CORS preflight
        path = urlparse(self.path).path
        origin, ok = self._origin_ok()
        if not host_allowed(self.headers.get("Host")) or not ok or path in ("/", "/settings"):
            # /settings is same-origin only: a cross-origin preflight never succeeds.
            self._deny(403, None)
            return
        self.send_response(204)
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Vary", "Origin")
            # Private Network Access (Chrome 104+). A page on a PUBLIC origin
            # (https://tradehunter.net) reaching a PRIVATE address (127.0.0.1) is
            # preflighted even for a simple GET, and the browser drops the request
            # unless this header comes back. Without it the tab reports "no bridge"
            # while the bridge is plainly running — and it only shows up from the
            # real site, because localhost -> localhost is private->private and
            # never triggers PNA at all.
            if self.headers.get("Access-Control-Request-Private-Network") == "true":
                self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        if not host_allowed(self.headers.get("Host")):
            self._send(403, {"ok": False, "error": "Host not allowed."})
            return
        origin, ok = self._origin_ok()
        if not ok:
            # An un-allow-listed page must not be able to read the account.
            self._send(403, {"ok": False, "error":
                             f"Origin {origin} is not allowed by this bridge."})
            return

        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}

        if u.path not in ("/", "/settings") and cross_site_without_origin(self.headers):
            # an <img> / no-cors fetch from another site: it could not read the answer,
            # but the TWS read would still run - refused before any work. (The settings
            # page itself stays reachable: the Options page links to it.)
            self._send(403, {"ok": False, "error": "Cross-site request without an Origin "
                             "refused - only allow-listed pages may use this connector."})
            return

        if u.path in ("/", "/settings"):
            # Same-origin page: never a CORS header, whoever asks.
            saved = q.get("saved") == "1"
            self._send_html(200, settings_page("Saved. Reconnecting to TWS..." if saved else ""))
            return
        if u.path == "/health":
            self._send(200, health_payload(), origin)
            return

        sym = (q.get("symbol") or "").strip().upper()
        try:
            if u.path == "/chain2":
                s = clean_symbol(q.get("symbol"))
                spec = parse_spec(q.get("spec"))
                key = ("chain2", s, json.dumps(spec, sort_keys=True))
                self._send(200, cached(key, lambda: worker().submit(
                    _chain2(s, spec), CHAIN2_TIMEOUT), ttl=CHAIN2_TTL), origin)
            elif u.path == "/underlying":
                s = clean_symbol(q.get("symbol"))
                self._send(200, self._underlying(s), origin)
            elif u.path == "/account":
                self._send(200, cached(("acct",), lambda: worker().submit(
                    _account(), 30), ttl=60), origin)
            elif u.path == "/chain":
                if not sym:
                    raise RuntimeError("symbol is required")
                exp = q.get("exp") or None
                dmin = int(q["dte_min"]) if q.get("dte_min") else None
                dmax = int(q["dte_max"]) if q.get("dte_max") else None
                side = q.get("side") == "put"      # 1.3: the bull put spread view
                key = ("chain", sym, exp or "", dmin, dmax, side)
                self._send(200, cached(key, lambda: worker().submit(
                    _locked(_chain(sym, exp, dmin, dmax, put_side=side)), 120)), origin)
            elif u.path == "/iv":
                if not sym:
                    raise RuntimeError("symbol is required")
                want_series = str(q.get("series", "")).strip().lower() in ("1", "true", "yes")   # 1.6
                self._send(200, cached(("iv", sym, want_series), lambda: worker().submit(
                    _iv(sym, series=want_series), 60), ttl=600), origin)
            elif u.path == "/scan":
                def _f(name, dflt):
                    try:
                        return max(0.0, float(q.get(name, dflt)))
                    except (TypeError, ValueError):
                        return float(dflt)
                ivr, px, vol = _f("iv_rank", 30), _f("price", 100), _f("volume", 200000)
                self._send(200, cached(("scan", ivr, px, vol), lambda: worker().submit(
                    _scan(ivr, px, vol), 60), ttl=120), origin)
            else:
                self._send(404, {"ok": False, "error": "unknown endpoint"}, origin)
        except BadRequest as exc:
            self._send(400, {"ok": False, "error": str(exc)}, origin)
        except Exception as exc:  # noqa: BLE001
            body = {"ok": False, "error": str(exc) or type(exc).__name__}
            if isinstance(exc, ConnectorBusy):
                body["busy"] = True
            self._send(200, body, origin)

    @staticmethod
    def _underlying(sym: str) -> dict:
        """Spot (fresh, 20 s cache) + one year of bars and IV (one pull per day)."""
        hist = cached(("und_hist", sym, _dt.date.today().isoformat()),
                      lambda: worker().submit(_und_history(sym), UND_TIMEOUT), ttl=HIST_TTL)
        out = {"ok": True, "symbol": sym, "spot": None, "mdt": None,
               "bars": hist["bars"], "iv_series": hist["iv_series"]}
        try:
            sp = cached(("und_spot", sym), lambda: worker().submit(_und_spot(sym), SPOT_TIMEOUT),
                        ttl=CHAIN2_TTL)
            out["spot"] = _pos(sp.get("spot"))
            out["mdt"] = _mdt_name(sp.get("mdt"))
        except Exception as exc:  # noqa: BLE001  (the history is still worth returning)
            out["spot_error"] = str(exc) or type(exc).__name__
        return out

    def do_POST(self):  # noqa: N802
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > self.MAX_BODY:
            self.close_connection = True
            self._send(413, {"ok": False, "error": "request body too large"})
            return
        # Read the body before ANY reply: closing with unread bytes makes Windows reset
        # the connection, and the caller then sees an abort instead of the refusal.
        raw = self.rfile.read(n) if n else b""
        path = urlparse(self.path).path
        if not host_allowed(self.headers.get("Host")):
            self._send(403, {"ok": False, "error": "Host not allowed."})
            return
        if path != "/settings":
            self._send(404, {"ok": False, "error": "unknown endpoint"})
            return
        port = self._own_port()
        if not same_origin_request(self.headers, port):
            # No CORS header either way: a cross-origin caller cannot even read this.
            self._send(403, {"ok": False, "error": "Settings can only be changed on the "
                             f"connector's own page, http://127.0.0.1:{port}/"})
            return
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        is_form = ctype == "application/x-www-form-urlencoded"
        try:
            text = raw.decode("utf-8")
            if is_form:
                data = {k: v[-1] for k, v in parse_qs(text, keep_blank_values=True).items()}
            else:
                data = json.loads(text or "{}")
        except (UnicodeDecodeError, ValueError):
            self._send(400, {"ok": False, "error": "Send the settings as JSON or form fields."})
            return
        conf, errors = update_settings(data)
        if errors:
            if is_form:
                self._send_html(400, settings_page(errors=errors, values=conf))
            else:
                self._send(400, {"ok": False, "error": " ".join(errors), "errors": errors})
            return
        if is_form:
            self.send_response(303)
            self.send_header("Location", "/?saved=1")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send(200, {"ok": True, "config": conf, "path": str(config_path()),
                         "reconnecting": True})

    def log_request(self, code="-", size="-"):
        if str(code) == "200" and self.path.startswith("/health"):
            return                      # polled every few seconds by every open page
        super().log_request(code, size)

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))


class _ExclusiveServer(ThreadingHTTPServer):
    """One bridge per port, enforced by the socket.

    ``HTTPServer`` sets SO_REUSEADDR, and on WINDOWS that lets a second process bind
    a port that is already being listened on - silently. Both "listen", the OLDEST
    one receives every request, and a freshly started bridge never sees traffic.
    That is how a 1.0 bridge from the morning kept answering after two restarts
    onto 1.1 (2026-09-18). Without the flag the second bind fails loudly instead.
    """
    allow_reuse_address = sys.platform != "win32"
    daemon_threads = True


def make_server(port: int = BRIDGE_PORT, host: str = "127.0.0.1") -> _ExclusiveServer:
    """The HTTP server on loopback (port 0 = any free port, for tests)."""
    return _ExclusiveServer((host, port), Handler)


def _retire_other_copies(port: int) -> None:
    """Starting the bridge means "run THIS bridge": stop any copy already on the port.

    Windows only (the launcher, the Startup shortcut and the web app's Start button
    are all Windows). Only processes that are LISTENING on our port AND whose
    command line names this script are touched; the whole launcher tree
    (cmd.exe -> py.exe -> python.exe) goes, so no stale "press any key" window is
    left behind. Best effort: any failure here falls through to the bind, which
    reports a held port plainly.
    """
    if sys.platform != "win32":
        return
    import subprocess  # noqa: PLC0415

    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             text=True, timeout=15).stdout
        listeners = set()
        for line in out.splitlines():
            f = line.split()
            if len(f) >= 5 and f[0] == "TCP" and f[3] == "LISTENING" \
                    and f[1].endswith(":%d" % port) and f[4].isdigit():
                listeners.add(int(f[4]))
        listeners.discard(os.getpid())
        if not listeners:
            return
        ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "
              "'*ibkr_bridge*' } | ForEach-Object { '{0},{1}' -f $_.ProcessId, $_.ParentProcessId }")
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, text=True, timeout=30).stdout
        parent = {}
        for line in out.splitlines():
            a, _, b = line.strip().partition(",")
            if a.isdigit() and b.isdigit():
                parent[int(a)] = int(b)
        mine = set()                     # this process and its own launcher chain
        p = os.getpid()
        while p in parent and p not in mine:
            mine.add(p)
            p = parent[p]
        mine.add(os.getpid())
        for pid in listeners:
            if pid not in parent:        # something else owns the port - not ours to stop
                continue
            top = pid
            while parent.get(top) in parent and parent[top] not in mine:
                top = parent[top]
            if top in mine:
                continue
            print(f"  an earlier bridge is still on port {port} (pid {pid}) - stopping it")
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(top)],
                           capture_output=True, timeout=15)
    except Exception as exc:  # noqa: BLE001
        print(f"  (could not check for an earlier bridge: {exc})", file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser(description="TradeHunter IBKR connector " + VERSION)
    ap.add_argument("--config", default=None,
                    help="settings file (default: %%APPDATA%%\\TradeHunter\\connector.json)")
    ap.add_argument("--tws-host", default=None, help="this run only: TWS host")
    ap.add_argument("--port", type=int, default=None, help="this run only: TWS socket port")
    ap.add_argument("--client-id", type=int, default=None, help="this run only: API client id")
    ap.add_argument("--max-lines", type=int, default=None,
                    help="this run only: market-data lines per wave")
    ap.add_argument("--bridge-port", type=int, default=BRIDGE_PORT)
    ap.add_argument("--strike-window", type=int, default=CFG["strike_window"],
                    help="legacy /chain: strikes each side of the price")
    ap.add_argument("--origin", action="append", default=[],
                    help="this run only: extra allowed browser origin (repeatable)")
    a = ap.parse_args(argv)

    path = Path(a.config) if a.config else default_config_path()
    conf, problems = load_config(path)
    if not path.exists():
        try:
            save_config(conf, path)      # so the member can find (and edit) the file
        except (OSError, ValueError):
            pass

    cli = {"tws_host": a.tws_host, "tws_port": a.port, "client_id": a.client_id,
           "max_lines": a.max_lines}
    cli = {k: v for k, v in cli.items() if v is not None}
    _, cli_errors = validate_config(cli)
    origins = [normalize_origin(o) for o in a.origin]
    if None in origins:
        cli_errors.append("--origin must be scheme://host[:port]")
    if cli_errors:
        print("Bad command-line value: " + " ".join(cli_errors), file=sys.stderr)
        raise SystemExit(2)
    overrides = {k: cli[v] for k, v in _OVERRIDE_KEYS.items() if v in cli}
    RUN.update(config_path=path, cli_origins=[o for o in origins if o],
               overrides=overrides, load_problems=problems)
    CFG["strike_window"] = a.strike_window
    apply_config(conf, overrides=overrides)
    for p in problems:
        print(f"  settings: {p}", file=sys.stderr)

    try:
        import ib_insync  # noqa: F401, PLC0415
    except Exception as exc:  # noqa: BLE001
        print(f"ib_insync is not importable: {exc}\n"
              "  py -3.12 -m pip install --user ib_insync   (Python 3.12 - 3.14 removed the\n"
              "  event loop API that eventkit needs at import time)", file=sys.stderr)
        raise SystemExit(1)
    if th_ibkr is None:
        print(f"  WARNING: th_ibkr.py did not load ({_TH_ERROR}); /chain2, /underlying "
              "will answer with an error until it is next to ibkr_bridge.py.", file=sys.stderr)

    _retire_other_copies(a.bridge_port)
    srv = None
    for attempt in range(10):           # the retired copy's socket can take a moment to go
        try:
            srv = make_server(a.bridge_port)
            break
        except OSError as exc:
            if attempt == 9:
                print(f"Port {a.bridge_port} is held by another program and could not be "
                      f"freed: {exc}\n  Close the other bridge window (or end its python.exe "
                      "in Task Manager), then start this again.", file=sys.stderr)
                raise SystemExit(1)
            time.sleep(0.5)
    w = worker()                        # connects in the background; /health shows progress
    print(f"TradeHunter IBKR connector {VERSION} on http://127.0.0.1:{a.bridge_port}")
    print(f"  settings page: http://127.0.0.1:{a.bridge_port}/   file: {path}")
    print(f"  -> TWS {CFG['host']}:{CFG['port']} (clientId {CFG['client_id']}, read-only, "
          f"{CFG['max_lines']} lines)")
    extra = ", ".join(CFG["allowed"]) or "none"
    print(f"  allowed: https://*.{ALLOWED_DOMAIN}, http://127.0.0.1 / localhost ports "
          f"{DEV_PORTS[0]}-{DEV_PORTS[1]}; extra: {extra}")
    print("  Close this window or Ctrl-C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        w.close()


if __name__ == "__main__":
    main()
