"""Contract history - Massive's options day files as Parquet (v4.139,
OPTIONS_SCREENER_DESIGN.md §14).

Why
---
The screener collector keeps one Massive options day file per trading day on Hermes
(``flat_files``: ``dashboard_tst/state/flatfiles``, ~2 years, gzip CSV, one row per option
contract that traded that day: open / high / low / close / volume / trades). v4.138 read
them only for each stock's IV30. Every contract's own daily bar stayed inside ~500 gzip
files nothing could query. This module turns each day file into a Parquet file pyarrow
reads in milliseconds - a whole chain on one day, one contract's whole life - for the
Options page (Contract history, the Volatility surface of a past day) and for backtests.

Layout
------
``<root>/day/YYYY/YYYY-MM-DD.parquet`` - one file per session, columns ``SCHEMA``:
``on`` (the session), ``root`` (the OCC option root as Massive spells it: ``AAPL``,
``BRKB``, ``SPXW``; an adjusted series keeps its digits, ``GME1``), ``expiry``, ``right``
(C / P), ``strike``, ``open`` / ``high`` / ``low`` / ``close``, ``volume``,
``transactions``. Sorted by root, expiry, right, strike; row groups of ``ROW_GROUP`` rows
with statistics, so a read of one underlying touches only its row groups; zstd. The
ticker text is not kept (``occ_ticker`` rebuilds it). A file is immutable once written:
a day is written again only when Massive re-published its file (the source's size / etag
in the flat-file manifest changed) or ``FORMAT`` goes up. ``<root>/manifest.json`` =
``{day: {rows, dropped, bytes, src_size, src_etag, converted_at, fmt, ms}}``, written
through a temp file + rename after every day (a crash leaves the old one whole).

Where
-----
``TST_OPTION_HISTORY_DIR``; else ``option_history`` beside ``TST_PRICE_HISTORY_DIR`` (on
Hermes ``C:\\HermesSync\\MarketData\\option_history`` - Resilio carries it to the laptop
for backtests); else ``dashboard_tst/state/option_history``. Parquet is the project's
format for stored history (CLAUDE.md "Bars storage = Parquet"). The screener's live
figures never come from here: this is history (the CLAUDE.md carve-out for analysis views).

Who writes, who reads
---------------------
Written ONLY by the screener collector's "parquet" lane (``scr_collector``; ``Store.run``,
newest day first). Read by the Options page (``chain`` / ``contract_history``) and the
Settings -> Data Ingest -> Option History tab (``Store.summary``). pyarrow is imported
lazily - ``available()`` names what is missing - so the web app boots without it.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

from . import flat_files

try:   # app/.env into the environment (no-op when absent) - the settings module does it
    from .. import config as _config  # noqa: F401
except Exception:  # noqa: BLE001 - the module also works outside the app package
    _config = None

log = logging.getLogger(__name__)

FORMAT = 1                      # the Parquet layout: a bump converts every day again
ENV_DIR = "TST_OPTION_HISTORY_DIR"
ENV_PRICE_DIR = "TST_PRICE_HISTORY_DIR"
DASH_ROOT = Path(__file__).resolve().parents[2]          # dashboard_tst/
DEFAULT_ROOT = DASH_ROOT / "state" / "option_history"
MANIFEST_NAME = "manifest.json"
ROW_GROUP = 16384               # rows per row group (one underlying's rows sit in one or a few)
COMPRESSION = "zstd"
FAIL_RETRY_S = 3600.0           # a day file that could not be read: tried again in 1 h (or when it changes)
BUSY_RETRY_S = 600.0            # a Parquet file that was in use / another write error: again in 10 min
REPLACE_TRIES = 5               # os.replace retried on a Windows sharing violation ...
REPLACE_WAIT_S = 0.5            # ... every half second
MAX_HISTORY_DAYS = 1500         # a contract-history read looks at most this many sessions
CACHE_N = 64                    # contract-history reads kept (until the next conversion changes the manifest)

COLUMNS = ("on", "root", "expiry", "right", "strike", "open", "high", "low", "close",
           "volume", "transactions")
PRICE_COLUMNS = ("open", "high", "low", "close")
# O:<root><YYMMDD><C|P><strike x 1000, 8 digits> - the root is letters, then the digits of an
# ADJUSTED series (scr_flativ.parse_ticker's rule)
TICKER_RE = r"^O:(?P<root>[A-Z]+[0-9]*)(?P<ymd>[0-9]{6})(?P<right>[CP])(?P<k>[0-9]{8})$"
_DAY_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


class HistoryError(RuntimeError):
    """A conversion or a read that failed. ``kind``: ``config`` (pyarrow missing, the
    folder cannot be written - every day would fail the same way), ``source`` (one day
    file cannot be read: damaged, not an options day file), ``error`` (anything else)."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind if kind in ("config", "source", "error") else "error"


# ────────────────────────────────── settings ──────────────────────────────────

def history_root() -> Path:
    """Where the Parquet files live (see the module docstring)."""
    v = (os.environ.get(ENV_DIR) or "").strip()
    if v:
        return Path(v)
    ph = (os.environ.get(ENV_PRICE_DIR) or "").strip()
    if ph:
        return Path(ph).parent / "option_history"
    return DEFAULT_ROOT


def available() -> str | None:
    """None when pyarrow imports, else the reason in plain words (with the fix)."""
    try:
        import pyarrow  # noqa: F401,PLC0415
        import pyarrow.csv  # noqa: F401,PLC0415
        import pyarrow.dataset  # noqa: F401,PLC0415
        import pyarrow.parquet  # noqa: F401,PLC0415
    except Exception as exc:  # noqa: BLE001 - ImportError, or a broken install
        return ("pyarrow is not installed on this PC (%s) - run: pip install -r app\\requirements.txt"
                % type(exc).__name__)
    return None


def _pa():
    why = available()
    if why:
        raise HistoryError("config", why)
    import pyarrow as pa  # noqa: PLC0415
    import pyarrow.compute as pc  # noqa: PLC0415
    import pyarrow.csv as pcsv  # noqa: PLC0415
    import pyarrow.dataset as pds  # noqa: PLC0415
    import pyarrow.parquet as pq  # noqa: PLC0415
    return pa, pc, pcsv, pds, pq


# ────────────────────────────────── small helpers ──────────────────────────────────

def _to_date(x) -> _dt.date | None:
    if isinstance(x, _dt.datetime):
        return x.date()
    if isinstance(x, _dt.date):
        return x
    s = str(x or "").strip()
    try:
        return _dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


def _need_date(x, what: str) -> _dt.date:
    d = _to_date(x)
    if d is None:
        raise ValueError("%s: a date is needed (YYYY-MM-DD), got %r" % (what, x))
    return d


def _int(v) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _stamp(now: _dt.datetime | None = None) -> str:
    t = now or _dt.datetime.now(_dt.timezone.utc)
    if t.tzinfo is not None:
        t = t.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return t.replace(microsecond=0).isoformat() + "Z"


def _replace(src: Path, dst: Path, sleep=time.sleep) -> None:
    """``os.replace`` retried on a Windows sharing violation (a reader holding the old file)."""
    for n in range(REPLACE_TRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if n + 1 >= REPLACE_TRIES:
                raise
            sleep(REPLACE_WAIT_S)


def _remove(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


def day_path(day, root: Path | None = None) -> Path:
    """``<root>/day/YYYY/YYYY-MM-DD.parquet`` (whether or not it is there)."""
    d = _need_date(day, "day_path")
    base = Path(root) if root is not None else history_root()
    return base / "day" / ("%04d" % d.year) / (d.isoformat() + ".parquet")


def occ_ticker(root: str, expiry, right: str, strike: float) -> str:
    """``("AAPL", 2026-10-16, "C", 150.0)`` -> ``O:AAPL261016C00150000`` (Massive's spelling)."""
    e = _need_date(expiry, "occ_ticker")
    return "O:%s%s%s%08d" % (str(root).upper(), e.strftime("%y%m%d"), str(right).upper()[:1],
                             int(round(float(strike) * 1000)))


def flat_manifest(flat_root: Path | None = None) -> dict:
    """The flat-file manifest READ-ONLY (``flat_files``' ``manifest.json``: ``{key: {day,
    size, etag, downloaded_at}}``), {} when it is missing or cannot be read. Never moves or
    rewrites it (the collector's ``FlatFiles`` owns the file)."""
    p = Path(flat_root or flat_files.DEFAULT_ROOT) / flat_files.MANIFEST_NAME
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - absent, or caught mid-write by a non-atomic writer
        return {}
    return data if isinstance(data, dict) else {}


def _source_days(flat_root: Path, flat: dict) -> dict[str, dict]:
    """``{day: {path, size, etag, downloaded_at, present, mtime}}`` for every day file the
    manifest names; ``present`` = the file is on disk with the manifest's size; ``mtime`` its
    modification time (ns) - a copy downloaded again counts as changed even with the same size
    and etag."""
    out: dict[str, dict] = {}
    for key, ent in (flat or {}).items():
        d = flat_files.day_of_key(key)
        if d is None or not isinstance(ent, dict):
            continue
        path = Path(flat_root).joinpath(*str(key).split("/"))
        size = _int(ent.get("size"))
        try:
            st = path.stat()
            present = path.is_file() and (size is None or st.st_size == size)
            mtime = st.st_mtime_ns
        except OSError:
            present, mtime = False, None
        out[d.isoformat()] = {"path": path, "size": size, "etag": ent.get("etag"),
                              "downloaded_at": ent.get("downloaded_at"), "present": present, "mtime": mtime}
    return out


def _same_source(failed: dict, src: dict) -> bool:
    """Is ``src`` the very copy that failed (same size, etag and modification time)?"""
    return (failed.get("src_size") == src.get("size") and failed.get("src_etag") == src.get("etag")
            and failed.get("src_mtime") == src.get("mtime"))


# ────────────────────────────────── one day file -> Parquet ──────────────────────────────────

def _read_source(path: Path):
    """The day file as a pyarrow table (``ticker, volume, open, close, high, low,
    transactions``; columns found by the header's names, a missing optional one comes back
    as nulls)."""
    pa, _pc, pcsv, _pds, _pq = _pa()
    p = Path(path)
    try:
        with open(p, "rb") as fb:
            gz = fb.read(2) == b"\x1f\x8b"
    except OSError as exc:
        raise HistoryError("source", "cannot open the day file %s (%s)" % (p.name, exc)) from None
    types = {"ticker": pa.string(), "volume": pa.float64(), "open": pa.float64(), "close": pa.float64(),
             "high": pa.float64(), "low": pa.float64(), "transactions": pa.float64()}
    try:
        stream = pa.input_stream(str(p), compression="gzip" if gz else None)
        tbl = pcsv.read_csv(stream, read_options=pcsv.ReadOptions(block_size=1 << 24),
                            convert_options=pcsv.ConvertOptions(
                                column_types=types, include_columns=list(types),
                                include_missing_columns=True, strings_can_be_null=False))
    except Exception as exc:  # noqa: BLE001 - damaged gzip, bad CSV, a wrong file
        raise HistoryError("source", "the day file %s cannot be read (%s: %s)"
                           % (p.name, type(exc).__name__, str(exc)[:200])) from None
    if tbl.num_rows and tbl["ticker"].null_count == tbl.num_rows:
        raise HistoryError("source", "the day file %s has no ticker column" % p.name)
    if tbl.num_rows and tbl["close"].null_count == tbl.num_rows:
        raise HistoryError("source", "the day file %s has no close column" % p.name)
    return tbl


def to_table(src, day):
    """One options day file (path) -> ``(table, dropped)``: the ``COLUMNS`` table sorted by
    root, expiry, right, strike, and the rows left out (not an option ticker, no strike)."""
    pa, pc, _pcsv, _pds, _pq = _pa()
    d = _need_date(day, "to_table")
    tbl = _read_source(Path(src))
    n_in = tbl.num_rows
    parts = pc.extract_regex(tbl["ticker"], TICKER_RE)
    ok = pc.is_valid(parts)
    if n_in:
        tbl = tbl.filter(ok)
        parts = parts.filter(ok)
    expiry = pc.strptime(pc.struct_field(parts, "ymd"), format="%y%m%d", unit="s", error_is_null=True)
    expiry = pc.cast(expiry, pa.date32())
    strike = pc.divide(pc.cast(pc.struct_field(parts, "k"), pa.float64()), 1000.0)
    good = pc.and_(pc.is_valid(expiry), pc.greater(strike, 0.0))
    n = tbl.num_rows

    def _int64(col):
        return pc.cast(pc.round(tbl[col]), pa.int64(), safe=False)

    out = pa.table({
        "on": pa.array([d] * n, type=pa.date32()),
        "root": pc.struct_field(parts, "root"),
        "expiry": expiry,
        "right": pc.struct_field(parts, "right"),
        "strike": strike,
        "open": tbl["open"], "high": tbl["high"], "low": tbl["low"], "close": tbl["close"],
        "volume": _int64("volume"), "transactions": _int64("transactions"),
    })
    if n:
        out = out.filter(pc.fill_null(good, False))
    out = out.sort_by([("root", "ascending"), ("expiry", "ascending"), ("right", "ascending"),
                       ("strike", "ascending")])
    return out, n_in - out.num_rows


def convert_day(src, day, dst, *, sleep=time.sleep) -> dict:
    """Write ``day``'s Parquet file at ``dst`` from the day file ``src`` (through a temp
    file beside it, renamed into place once whole). ``{rows, dropped, bytes, ms}``.
    Raises ``HistoryError`` (``source``: the day file; ``config``: pyarrow / the folder;
    ``error``: the old Parquet file was in use and could not be replaced)."""
    t0 = time.monotonic()
    _pa_, _pc, _pcsv, _pds, pq = _pa()
    try:
        table, dropped = to_table(src, day)
    except HistoryError:
        raise
    except Exception as exc:  # noqa: BLE001 - an unexpected pyarrow error: this day only
        raise HistoryError("source", "the day file %s could not be converted (%s: %s)"
                           % (Path(src).name, type(exc).__name__, str(exc)[:200])) from None
    dst = Path(dst)
    tmp = dst.with_name("%s.%d.tmp" % (dst.name, os.getpid()))
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, str(tmp), compression=COMPRESSION, row_group_size=ROW_GROUP,
                       use_dictionary=["root", "right"], write_statistics=True)
    except OSError as exc:                # the disk / the folder: every day would fail the same way
        _remove(tmp)
        raise HistoryError("config", "could not write %s (%s) - check the free disk space and the folder %s"
                           % (dst.name, exc, dst.parent)) from None
    except Exception as exc:  # noqa: BLE001 - a pyarrow write error
        _remove(tmp)
        raise HistoryError("error", "could not write %s (%s: %s)" % (dst.name, type(exc).__name__,
                                                                     str(exc)[:200])) from None
    try:
        _replace(tmp, dst, sleep)
    except OSError as exc:                # a reader held the old file (Windows): only this day, tried again later
        _remove(tmp)
        raise HistoryError("error", "could not replace %s (%s) - it was in use; tried again later"
                           % (dst.name, exc)) from None
    return {"rows": table.num_rows, "dropped": int(dropped), "bytes": dst.stat().st_size,
            "ms": int((time.monotonic() - t0) * 1000)}


# ────────────────────────────────── the store ──────────────────────────────────

class Store:
    """The Parquet day files under ``root`` and their manifest, next to the flat-file
    store under ``flat_root``. Thread-safe (one lock around the manifest). ``now`` - a
    clock returning a datetime (UTC; default the system clock)."""

    def __init__(self, root=None, flat_root=None, *, now=None, sleep=None, log_=None):
        self.root = Path(root) if root is not None else history_root()
        self.flat_root = Path(flat_root) if flat_root is not None else flat_files.DEFAULT_ROOT
        self._now_fn = now
        self._sleep = sleep or time.sleep
        self.log = log_ or log
        self._lock = threading.RLock()
        self._manifest: dict[str, dict] = self._load()
        self._mtime: float | None = self._manifest_mtime()
        self.failed: dict[str, dict] = {}      # day -> {error, kind, at, src_size, src_etag} (this process)

    def __repr__(self) -> str:
        return "<opt_history.Store root=%s flat_root=%s>" % (self.root, self.flat_root)

    def _now(self) -> _dt.datetime:
        t = self._now_fn() if self._now_fn is not None else _dt.datetime.now(_dt.timezone.utc)
        if isinstance(t, _dt.datetime) and t.tzinfo is not None:
            t = t.astimezone(_dt.timezone.utc).replace(tzinfo=None)
        return t

    # ── the manifest ──

    def _manifest_path(self) -> Path:
        return self.root / MANIFEST_NAME

    def _manifest_mtime(self) -> float | None:
        try:
            return self._manifest_path().stat().st_mtime
        except OSError:
            return None

    def _load(self) -> dict:
        p = self._manifest_path()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception as exc:  # noqa: BLE001 - a broken manifest: every day is checked again
            self.log.warning("option history: the manifest %s could not be read (%s) - the days are "
                             "converted again", p, type(exc).__name__)
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if _DAY_RE.match(str(k)) and isinstance(v, dict)}

    def reload(self) -> None:
        """Read the manifest again when another process wrote it (the web app's view)."""
        m = self._manifest_mtime()
        if m != self._mtime:
            with self._lock:
                self._manifest = self._load()
                self._mtime = m

    def manifest(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self._manifest.items()}

    def _save(self) -> None:
        p = self._manifest_path()
        tmp = p.with_name("%s.%d.tmp" % (MANIFEST_NAME, os.getpid()))
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._manifest, fh, indent=1, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            _replace(tmp, p, self._sleep)
            self._mtime = self._manifest_mtime()
        except Exception as exc:  # noqa: BLE001
            _remove(tmp)
            raise HistoryError("config", "could not save the option-history manifest in %s (%s: %s)"
                               % (self.root, type(exc).__name__, exc)) from None

    # ── what is to do ──

    def sources(self) -> dict[str, dict]:
        """The day files on this PC (``_source_days`` of the flat-file manifest)."""
        return _source_days(self.flat_root, flat_manifest(self.flat_root))

    def _current(self, day: str, src: dict, have: dict | None) -> bool:
        if not have or _int(have.get("fmt")) != FORMAT:
            return False
        if _int(have.get("src_size")) != src.get("size"):
            return False
        a, b = have.get("src_etag"), src.get("etag")
        if a and b and str(a).strip('"').lower() != str(b).strip('"').lower():
            return False
        return day_path(day, self.root).is_file()

    def _waiting(self, day: str, s: dict, now: _dt.datetime) -> bool:
        """Did ``day`` fail with this same source a short while ago (``FAIL_RETRY_S`` for a
        file that cannot be read, ``BUSY_RETRY_S`` for anything else)?"""
        f = self.failed.get(day)
        if not f or not _same_source(f, s):
            return False
        at = f.get("at")
        wait = FAIL_RETRY_S if f.get("kind") == "source" else BUSY_RETRY_S
        return isinstance(at, _dt.datetime) and (now - at).total_seconds() < wait

    def pending(self, sources: dict | None = None) -> list[str]:
        """The days whose day file is on this PC and whose Parquet file is missing or out of
        date (re-published source, older ``FORMAT``), NEWEST first. A day that failed is
        left out for a while (``_waiting``) unless its source changed."""
        src = sources if sources is not None else self.sources()
        now = self._now()
        out = []
        with self._lock:
            man = dict(self._manifest)
        for day in sorted(src, reverse=True):
            s = src[day]
            if not s.get("present") or self._current(day, s, man.get(day)):
                continue
            if self._waiting(day, s, now):
                continue
            out.append(day)
        return out

    def failed_days(self, sources: dict | None = None) -> list[tuple[str, dict]]:
        """The days that failed and are still not converted, with their source unchanged since
        (``[(day, {error, kind, at})]``, NEWEST first) - what the status reports as a problem."""
        src = sources if sources is not None else self.sources()
        with self._lock:
            man = dict(self._manifest)
        out = []
        for day, f in sorted(list(self.failed.items()), reverse=True):
            s = src.get(day)
            if not s or not s.get("present") or self._current(day, s, man.get(day)):
                continue
            if not _same_source(f, s):
                continue                               # a new copy: it is pending again, not failed
            out.append((day, dict(f)))
        return out

    def counts(self, sources: dict | None = None) -> tuple[int, int]:
        """``(days converted and current, day files on this PC)``."""
        src = sources if sources is not None else self.sources()
        with self._lock:
            man = dict(self._manifest)
        local = [d for d, s in src.items() if s.get("present")]
        done = sum(1 for d in local if self._current(d, src[d], man.get(d)))
        return done, len(local)

    # ── converting ──

    def convert(self, day, sources: dict | None = None) -> dict:
        """Convert one day (its day file must be on this PC) and record it. Returns the
        manifest entry. A failure of this day (``source`` / ``error``) is remembered in
        ``failed``; a ``config`` one (pyarrow, the disk, the folder) is not - it is no fault of
        the day, and the day is tried again as soon as the lane runs again."""
        d = _need_date(day, "convert").isoformat()
        src = (sources if sources is not None else self.sources()).get(d)
        if not src or not src.get("present"):
            raise HistoryError("source", "the day file for %s is not on this PC" % d)
        try:
            got = convert_day(src["path"], d, day_path(d, self.root), sleep=self._sleep)
        except HistoryError as exc:
            if exc.kind != "config":
                self.failed[d] = {"error": str(exc), "kind": exc.kind, "at": self._now(),
                                  "src_size": src.get("size"), "src_etag": src.get("etag"),
                                  "src_mtime": src.get("mtime")}
            raise
        ent = {"rows": got["rows"], "dropped": got["dropped"], "bytes": got["bytes"], "ms": got["ms"],
               "src_size": src.get("size"), "src_etag": src.get("etag"),
               "converted_at": _stamp(self._now()), "fmt": FORMAT}
        with self._lock:
            self._manifest[d] = ent
            self._save()
        self.failed.pop(d, None)
        return dict(ent)

    def run(self, days=None, *, stop=None, on_day=None) -> dict:
        """Convert ``days`` (default ``pending()``), in the order given. ``stop()`` -> True
        ends early; ``on_day(done, total, day, entry_or_None, error_or_None)`` after each
        (an error in it is ignored). A ``config`` error (pyarrow, the folder) stops the run
        and is raised; a ``source`` / ``error`` failure skips only that day.
        ``{converted, failed: [(day, text)], rows, bytes, stopped, ms}``."""
        t0 = time.monotonic()
        sources = self.sources()
        todo = list(days) if days is not None else self.pending(sources)
        out = {"converted": 0, "failed": [], "rows": 0, "bytes": 0, "stopped": False, "ms": 0}
        total = len(todo)
        for n, day in enumerate(todo, 1):
            if stop is not None and stop():
                out["stopped"] = True
                break
            ent, err = None, None
            try:
                ent = self.convert(day, sources)
                out["converted"] += 1
                out["rows"] += int(ent.get("rows") or 0)
                out["bytes"] += int(ent.get("bytes") or 0)
            except HistoryError as exc:
                if exc.kind == "config":
                    raise
                err = str(exc)
                out["failed"].append((str(day), err))
                self.log.warning("option history: %s not converted - %s", day, err)
            if on_day is not None:
                try:
                    on_day(n, total, str(day), ent, err)
                except Exception:  # noqa: BLE001 - a progress hook never stops the run
                    pass
        out["ms"] = int((time.monotonic() - t0) * 1000)
        return out

    # ── what the Settings tab shows ──

    def days(self) -> list[str]:
        """The converted days (in the manifest, current format, file on disk), oldest first."""
        with self._lock:
            man = dict(self._manifest)
        return sorted(d for d, e in man.items()
                      if _int(e.get("fmt")) == FORMAT and day_path(d, self.root).is_file())

    def summary(self, window=None, *, recent: int = 15) -> dict:
        """Everything the Settings -> Data Ingest -> Option History tab shows. ``window`` =
        the sessions Massive serves (``scr_collector.flat_days``), oldest first, for the
        missing days and the month table (None: the days on file)."""
        self.reload()
        sources = self.sources()
        with self._lock:
            man = {k: dict(v) for k, v in self._manifest.items()}
        local = {d: s for d, s in sources.items() if s.get("present")}
        conv = {d: e for d, e in man.items() if d in local and self._current(d, local[d], e)}
        stale = {d: e for d, e in man.items() if d not in conv and day_path(d, self.root).is_file()}
        win = list(window) if window else sorted(set(local) | set(conv))
        win_set = set(win)
        missing = [d for d in win if d not in local]
        behind = [d for d in sorted(local, reverse=True) if d not in conv]

        def _max(vals):
            vals = [v for v in vals if v]
            return max(vals) if vals else None

        months: dict[str, dict] = {}
        for d in sorted(win_set | set(local) | set(conv)):
            m = months.setdefault(d[:7], {"month": d[:7], "sessions": 0, "downloaded": 0, "converted": 0,
                                          "rows": 0, "gz_bytes": 0, "pq_bytes": 0})
            if d in win_set:
                m["sessions"] += 1
            if d in local:
                m["downloaded"] += 1
                m["gz_bytes"] += int(local[d].get("size") or 0)
            if d in conv:
                m["converted"] += 1
                m["rows"] += int(conv[d].get("rows") or 0)
                m["pq_bytes"] += int(conv[d].get("bytes") or 0)
        rec = []
        for d in sorted(win_set | set(local), reverse=True)[:max(0, int(recent))]:
            s, e = local.get(d), conv.get(d)
            f = self.failed.get(d)
            state = ("converted" if e else "failed" if f else "pending" if s else "missing")
            rec.append({"day": d, "state": state,
                        "gz_bytes": int(s["size"]) if s and s.get("size") is not None else None,
                        "downloaded_at": s.get("downloaded_at") if s else None,
                        "rows": int(e["rows"]) if e and e.get("rows") is not None else None,
                        "pq_bytes": int(e["bytes"]) if e and e.get("bytes") is not None else None,
                        "converted_at": e.get("converted_at") if e else None,
                        "error": f.get("error") if f else None})
        return {
            "root": str(self.root), "flat_root": str(self.flat_root), "available": available(),
            "format": FORMAT,
            "window": {"sessions": len(win), "oldest": win[0] if win else None, "newest": win[-1] if win else None},
            "flat": {"days": len(local), "bytes": sum(int(s.get("size") or 0) for s in local.values()),
                     "oldest": min(local) if local else None, "newest": max(local) if local else None,
                     "last_download": _max(s.get("downloaded_at") for s in local.values())},
            "pq": {"days": len(conv), "rows": sum(int(e.get("rows") or 0) for e in conv.values()),
                   "bytes": sum(int(e.get("bytes") or 0) for e in conv.values()),
                   "oldest": min(conv) if conv else None, "newest": max(conv) if conv else None,
                   "last_converted": _max(e.get("converted_at") for e in conv.values()),
                   "out_of_date": len(stale)},
            "missing": missing, "behind": behind,
            "failed": [{"day": d, "error": f.get("error")} for d, f in sorted(self.failed.items(), reverse=True)],
            "months": sorted(months.values(), key=lambda m: m["month"], reverse=True),
            "recent": rec,
        }


# ────────────────────────────────── reading it back ──────────────────────────────────

def _root(root) -> Path:
    return Path(root) if root is not None else history_root()


def converted_days(root=None) -> list[str]:
    """The sessions with a Parquet file (manifest, current format, file on disk), oldest first."""
    return Store(root=_root(root), flat_root=flat_files.DEFAULT_ROOT).days()


def chain(opt_root: str, day, *, root=None, columns=None):
    """One underlying's contracts that traded on ``day`` (``opt_root`` = the OCC root,
    ``scr_flativ.option_root``) as a pyarrow table (``COLUMNS`` or ``columns``), sorted by
    expiry, right, strike; None when ``day`` has no Parquet file."""
    _pa_, pc, _pcsv, _pds, pq = _pa()
    p = day_path(day, _root(root))
    if not p.is_file():
        return None
    cols = list(columns) if columns else list(COLUMNS)
    try:
        return pq.read_table(str(p), columns=cols, filters=[("root", "=", str(opt_root).upper())])
    except Exception as exc:  # noqa: BLE001
        raise HistoryError("source", "the Parquet file %s cannot be read (%s: %s)"
                           % (p.name, type(exc).__name__, str(exc)[:200])) from None


_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()


def contract_history(opt_root: str, expiry, right: str, strike: float, *, root=None, start=None,
                     end=None) -> list[dict]:
    """One contract's daily bars across every converted day from ``start`` to ``end`` (and
    never after its expiry), oldest first: ``[{on, open, high, low, close, volume,
    transactions}]`` (``on`` a date). A read opens every day file up to the expiry (~1 s for
    500), so the last ``CACHE_N`` answers are kept until the manifest changes (a new day)."""
    pa, pc, _pcsv, pds, _pq = _pa()
    e = _need_date(expiry, "contract_history")
    s = _to_date(start)
    t = min(_to_date(end) or e, e)
    base = _root(root)
    store = Store(root=base, flat_root=flat_files.DEFAULT_ROOT)
    key = (str(base), str(opt_root).upper(), e, str(right).upper()[:1], float(strike), s, t, store._mtime)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit is not None:
        return [dict(r) for r in hit]
    days = [d for d in store.days() if d <= t.isoformat() and (s is None or d >= s.isoformat())]
    days = days[-MAX_HISTORY_DAYS:]
    if not days:
        return []
    paths = [str(day_path(d, base)) for d in days]
    try:
        ds = pds.dataset(paths, format="parquet")
        flt = ((pc.field("root") == str(opt_root).upper()) & (pc.field("expiry") == pa.scalar(e, pa.date32()))
               & (pc.field("right") == str(right).upper()[:1]) & (pc.field("strike") == float(strike)))
        tbl = ds.to_table(filter=flt, columns=["on", "open", "high", "low", "close", "volume", "transactions"])
    except Exception as exc:  # noqa: BLE001
        raise HistoryError("source", "the contract history could not be read (%s: %s)"
                           % (type(exc).__name__, str(exc)[:200])) from None
    rows = tbl.sort_by([("on", "ascending")]).to_pylist()
    with _CACHE_LOCK:
        if len(_CACHE) >= CACHE_N:
            _CACHE.pop(next(iter(_CACHE)))           # the oldest answer
        _CACHE[key] = rows
    return [dict(r) for r in rows]


def _cli(argv=None) -> int:
    """``python -m app.services.opt_history status`` - what is on file (read-only)."""
    import argparse  # noqa: PLC0415

    ap = argparse.ArgumentParser(prog="opt_history", description="Contract history (Parquet) - read-only checks")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="days downloaded / converted, rows, sizes")
    c = sub.add_parser("contract", help="one contract's daily bars")
    c.add_argument("root")
    c.add_argument("expiry")
    c.add_argument("right", choices=["C", "P", "c", "p"])
    c.add_argument("strike", type=float)
    a = ap.parse_args(argv)
    if a.cmd == "status":
        s = Store().summary()
        print("Parquet folder : %s" % s["root"])
        print("Day files      : %d on this PC (%s .. %s), %.1f MB" % (
            s["flat"]["days"], s["flat"]["oldest"], s["flat"]["newest"], s["flat"]["bytes"] / 1e6))
        print("Converted      : %d days (%s .. %s), %s rows, %.1f MB" % (
            s["pq"]["days"], s["pq"]["oldest"], s["pq"]["newest"], "{:,}".format(s["pq"]["rows"]),
            s["pq"]["bytes"] / 1e6))
        print("Still to do    : %d days" % len(s["behind"]))
        if s["available"]:
            print("PROBLEM        : %s" % s["available"])
        return 0
    rows = contract_history(a.root, a.expiry, a.right, a.strike)
    print(occ_ticker(a.root, a.expiry, a.right, a.strike), "-", len(rows), "days")
    for r in rows:
        print(r["on"], r["open"], r["high"], r["low"], r["close"], r["volume"], r["transactions"])
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())


__all__ = ["FORMAT", "COLUMNS", "HistoryError", "Store", "history_root", "available", "day_path",
           "occ_ticker", "flat_manifest", "to_table", "convert_day", "converted_days", "chain",
           "contract_history"]
