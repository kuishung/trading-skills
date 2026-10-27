"""The closing snapshot - Massive's own IV of every contract at each session's close, kept as
Parquet (v4.225).

Why
---
The contract history (``opt_history``, Massive's options DAY FILES) carries only trades: open /
high / low / close / volume. A past day's IV is therefore solved by TradeHunter from each
contract's LAST TRADE, which may be hours before the 4 pm close, and a contract that did not
trade has no row at all. The user (2026-10-27): *our definition of close is the quoted price at
the end of the day when market closes*. Massive's chain snapshot gives exactly that: its IV per
contract is worked out from the contract's QUOTE (the bid/ask midpoint), and the screener
collector reads the whole market once more after the close (the end-of-day pass, from 16:20
ET). Until now each pass replaced the one before (``scr_contract`` keeps only the latest), so
those closing IVs were lost by the next morning. This module keeps them: from the first EOD
pass after this release, every session's closing read is written to one Parquet file. Days
before that stay trade-based - Massive sells no quote history on Options Starter.

Layout
------
``<option history root>/snapshot/YYYY/YYYY-MM-DD.parquet`` (``opt_history.history_root()``:
on Hermes ``C:\\HermesSync\\MarketData\\option_history\\snapshot`` - Resilio carries it to the
laptop), one file per session, columns ``COLUMNS``: ``on``, ``symbol`` (our spelling,
``BRK-B``), ``expiry``, ``right``, ``strike``, ``weekly``, the contract's DAY BAR from the same
read - ``open`` / ``high`` / ``low`` / ``close`` / ``volume`` / ``vwap`` (the user: *"we want the time
consistency of the data rather than rely on separate data source"* - the prices and the IV of one
moment; None / 0 when the contract did not trade that session), ``iv`` (FRACTION, Massive's),
``delta`` / ``gamma`` / ``theta`` / ``vega``, ``price`` (Black-Scholes from that IV), ``chg_pct``,
``oi``, ``last_trade``, ``as_of`` (the read time minus the 15-min delay), ``spot`` / ``spot_src``
(the stock price the pass saw). Only the contracts the screener keeps (open interest or volume) -
the EOD pass's rows, which include contracts that did not trade that day. Sorted by symbol, expiry, right, strike; row groups of
``ROW_GROUP`` rows with statistics; zstd. A file is written once (``write_session``) and never
changed.

Who writes, who reads
---------------------
Written ONLY by the screener collector after an EOD pass on the full list
(``scr_collector._snap_tick``). Read by the Volatility Surface for a past day
(``vol_surface.history_rows`` prefers it over the trade-solved IV), the contract history
(``contract_view``) and the Option History tab (``summary``). Parquet is the project's format
for stored history (CLAUDE.md); the live pages never read it.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
from pathlib import Path

from . import opt_history

SUBDIR = "snapshot"
ROW_GROUP = 16384               # rows per row group (one underlying's rows sit in one or a few)
CHUNK = 50000                   # rows fetched from the DB and written at a time
COMPRESSION = "zstd"
SINCE_MARGIN_MIN = 20           # a row counts as the EOD pass's when read at most this long before it began
_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.parquet$")

COLUMNS = ("on", "symbol", "expiry", "right", "strike", "weekly", "open", "high", "low", "close", "volume", "vwap",
           "iv", "delta", "gamma", "theta", "vega", "price", "chg_pct", "oi", "last_trade", "as_of", "spot", "spot_src")


def snapshot_root(root=None) -> Path:
    """``<option history root>/snapshot`` (``root`` = the option history root, for tests)."""
    return (Path(root) if root is not None else opt_history.history_root()) / SUBDIR


def day_path(day, root=None) -> Path:
    """``<snapshot root>/YYYY/YYYY-MM-DD.parquet`` (whether or not it is there)."""
    d = opt_history._need_date(day, "opt_snapshot.day_path")  # noqa: SLF001 - the module's own date check
    return snapshot_root(root) / ("%04d" % d.year) / (d.isoformat() + ".parquet")


def has_day(day, root=None) -> bool:
    try:
        return day_path(day, root).is_file()
    except ValueError:
        return False


def _schema(pa):
    return pa.schema([
        ("on", pa.date32()), ("symbol", pa.string()), ("expiry", pa.date32()), ("right", pa.string()),
        ("strike", pa.float64()), ("weekly", pa.bool_()), ("open", pa.float64()), ("high", pa.float64()),
        ("low", pa.float64()), ("close", pa.float64()), ("volume", pa.int64()), ("vwap", pa.float64()),
        ("iv", pa.float64()), ("delta", pa.float64()), ("gamma", pa.float64()), ("theta", pa.float64()),
        ("vega", pa.float64()), ("price", pa.float64()), ("chg_pct", pa.float64()), ("oi", pa.int64()),
        ("last_trade", pa.timestamp("s")), ("as_of", pa.timestamp("s")), ("spot", pa.float64()),
        ("spot_src", pa.string()),
    ])


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _intv(v):
    f = _num(v)
    return int(round(f)) if f is not None else None


def _ts(t):
    if not isinstance(t, _dt.datetime):
        return None
    if t.tzinfo is not None:
        t = t.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return t.replace(microsecond=0)


def write_session(sdb, session, *, since=None, root=None, force: bool = False) -> dict:
    """Write the screener's stored contracts of ``session`` (the EOD pass's rows: ``session`` =
    that day and, with ``since``, read no earlier than it) to ``day_path(session)``. Returns
    ``{written, why, rows, symbols, bytes, path}``; ``why`` when nothing was written: ``exists``
    (the file is there - a snapshot is written once), ``superseded`` (a newer session's pass has
    begun replacing the rows, so they are no longer that close as a whole), ``empty`` (no row).
    Raises ``opt_history.HistoryError`` (``config``: pyarrow missing / the folder cannot be
    written; ``error``: anything else)."""
    pa, _pc, _pcsv, _pds, pq = opt_history._pa()  # noqa: SLF001 - the shared lazy import
    from ..screener_models import ScrContract as C, ScrUnderlying as U  # noqa: PLC0415,N817

    d = opt_history._need_date(session, "write_session")  # noqa: SLF001
    ses = d.isoformat()
    path = day_path(d, root)
    out = {"written": False, "why": None, "rows": 0, "symbols": 0, "bytes": 0, "path": str(path), "session": ses}
    if path.is_file() and not force:
        out["why"] = "exists"
        return out
    if sdb.query(C.id).filter(C.session > ses).first() is not None:
        out["why"] = "superseded"
        return out
    spots = {s: (_num(p), src) for s, p, src in sdb.query(U.symbol, U.spot, U.spot_src).all()}
    q = sdb.query(C.symbol, C.expiry, C.right, C.strike, C.weekly, C.iv, C.delta, C.gamma, C.theta, C.vega,
                  C.price, C.last, C.chg_pct, C.volume, C.oi, C.last_trade, C.as_of,
                  C.bar_open, C.bar_high, C.bar_low, C.vwap).filter(C.session == ses)
    if since is not None:
        q = q.filter(C.as_of >= _ts(since))
    q = q.order_by(C.symbol, C.expiry, C.right, C.strike)
    schema = _schema(pa)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise opt_history.HistoryError("config", "the snapshot folder %s cannot be made (%s)" % (path.parent, exc)) from None
    tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
    cols = {c: [] for c in COLUMNS}
    syms: set[str] = set()
    n = 0
    writer = None

    def flush():
        nonlocal writer, cols
        if not cols["symbol"]:
            return
        tbl = pa.Table.from_pydict(cols, schema=schema)
        if writer is None:
            writer = pq.ParquetWriter(str(tmp), schema, compression=COMPRESSION)
        writer.write_table(tbl, row_group_size=ROW_GROUP)
        cols = {c: [] for c in COLUMNS}

    try:
        for (sym, e, right, k, wk, iv, de, ga, th, ve, pr, la, ch, vo, oi, lt, ao, bo, bh, bl, vw) in q.yield_per(CHUNK):
            ed = opt_history._to_date(e)  # noqa: SLF001
            kk = _num(k)
            if ed is None or kk is None or right not in ("C", "P"):
                continue
            sp, src = spots.get(sym, (None, None))
            vol = _intv(vo) or 0
            # the day bar is this session's only when the contract traded in it (the pass sets volume 0 and
            # leaves the bar's open / high / low out otherwise; its close is then an older day's - dropped)
            for c, v in (("on", d), ("symbol", sym), ("expiry", ed), ("right", right), ("strike", kk),
                         ("weekly", bool(wk)), ("open", _num(bo)), ("high", _num(bh)), ("low", _num(bl)),
                         ("close", _num(la) if vol > 0 else None), ("volume", vol), ("vwap", _num(vw)),
                         ("iv", _num(iv)), ("delta", _num(de)), ("gamma", _num(ga)),
                         ("theta", _num(th)), ("vega", _num(ve)), ("price", _num(pr)),
                         ("chg_pct", _num(ch)), ("oi", _intv(oi)), ("last_trade", _ts(lt)),
                         ("as_of", _ts(ao)), ("spot", sp), ("spot_src", src)):
                cols[c].append(v)
            syms.add(sym)
            n += 1
            if len(cols["symbol"]) >= CHUNK:
                flush()
        flush()
        if writer is not None:
            writer.close()
            writer = None
        if not n:
            opt_history._remove(tmp)  # noqa: SLF001
            out["why"] = "empty"
            return out
        opt_history._replace(tmp, path)  # noqa: SLF001 - retried on a Windows sharing violation
    except opt_history.HistoryError:
        raise
    except Exception as exc:  # noqa: BLE001
        if writer is not None:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
        opt_history._remove(tmp)  # noqa: SLF001
        kind = "config" if isinstance(exc, OSError) else "error"
        raise opt_history.HistoryError(kind, "the closing snapshot of %s could not be written (%s: %s)"
                                       % (ses, type(exc).__name__, str(exc)[:200])) from None
    out.update(written=True, rows=n, symbols=len(syms), bytes=path.stat().st_size)
    return out


def chain(symbol, day, *, root=None, columns=None):
    """One underlying's closing snapshot on ``day`` as a pyarrow table (``COLUMNS`` or
    ``columns``), or None when that day has no snapshot."""
    _pa_, _pc, _pcsv, _pds, pq = opt_history._pa()  # noqa: SLF001
    try:
        p = day_path(day, root)
    except ValueError:
        return None
    if not p.is_file():
        return None
    cols = list(columns) if columns else list(COLUMNS)
    try:
        return pq.read_table(str(p), columns=cols, filters=[("symbol", "=", str(symbol).strip().upper())])
    except Exception as exc:  # noqa: BLE001
        raise opt_history.HistoryError("source", "the closing snapshot %s cannot be read (%s: %s)"
                                       % (p.name, type(exc).__name__, str(exc)[:200])) from None


def contract_rows(symbol, expiry, right: str, strike: float, *, root=None, end=None) -> dict[str, dict]:
    """One contract's closing-snapshot rows across every snapshot day up to ``end`` (and never
    after its expiry): ``{YYYY-MM-DD: {open, high, low, close, volume, vwap, iv, oi, spot, spot_src}}``."""
    pa, pc, _pcsv, pds, _pq = opt_history._pa()  # noqa: SLF001
    e = opt_history._need_date(expiry, "contract_rows")  # noqa: SLF001
    t = min(opt_history._to_date(end) or e, e)  # noqa: SLF001
    want = [d for d in days(root) if d <= t.isoformat()]
    if not want:
        return {}
    try:
        ds = pds.dataset([str(day_path(d, root)) for d in want], format="parquet")
        flt = ((pc.field("symbol") == str(symbol).strip().upper()) & (pc.field("expiry") == pa.scalar(e, pa.date32()))
               & (pc.field("right") == str(right).upper()[:1]) & (pc.field("strike") == float(strike)))
        tbl = ds.to_table(filter=flt, columns=["on", "open", "high", "low", "close", "volume", "vwap", "iv", "oi",
                                              "spot", "spot_src"])
    except Exception as exc:  # noqa: BLE001
        raise opt_history.HistoryError("source", "the closing snapshots could not be read (%s: %s)"
                                       % (type(exc).__name__, str(exc)[:200])) from None
    return {r["on"].isoformat(): r for r in tbl.to_pylist()}


def days(root=None) -> list[str]:
    """The sessions with a snapshot file, oldest first."""
    base = snapshot_root(root)
    out = []
    try:
        for y in base.iterdir():
            if y.is_dir():
                out.extend(m.group(1) for m in (_FILE_RE.match(f.name) for f in y.iterdir()) if m)
    except OSError:
        return []
    return sorted(out)


def day_info(day, root=None) -> dict | None:
    """One session's snapshot file: ``{rows, bytes, saved_at}`` (``saved_at`` = the file's time, naive
    UTC; ``rows`` None when pyarrow cannot read the footer), or None when there is none."""
    try:
        p = day_path(day, root)
        st = p.stat()
    except (OSError, ValueError):
        return None
    rows = None
    if not opt_history.available():
        try:
            import pyarrow.parquet as pq  # noqa: PLC0415

            rows = pq.read_metadata(str(p)).num_rows
        except Exception:  # noqa: BLE001 - a count only
            rows = None
    saved = _dt.datetime.fromtimestamp(st.st_mtime, _dt.timezone.utc).replace(tzinfo=None, microsecond=0)
    return {"rows": rows, "bytes": st.st_size, "saved_at": saved}


def summary(root=None) -> dict:
    """``{days, oldest, newest, bytes, newest_rows, root}`` for the Option History tab."""
    ds = days(root)
    size = 0
    for d in ds:
        try:
            size += day_path(d, root).stat().st_size
        except OSError:
            pass
    rows = None
    if ds and not opt_history.available():
        try:
            import pyarrow.parquet as pq  # noqa: PLC0415

            rows = pq.read_metadata(str(day_path(ds[-1], root))).num_rows
        except Exception:  # noqa: BLE001 - a count only
            rows = None
    return {"days": len(ds), "oldest": ds[0] if ds else None, "newest": ds[-1] if ds else None,
            "bytes": size, "newest_rows": rows, "root": str(snapshot_root(root))}


__all__ = ["SUBDIR", "COLUMNS", "snapshot_root", "day_path", "has_day", "write_session", "chain", "contract_rows",
           "days", "day_info", "summary"]
