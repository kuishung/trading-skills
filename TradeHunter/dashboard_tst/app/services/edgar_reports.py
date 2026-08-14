"""Read the LOCAL EDGAR quarterly-report corpus for the Company page's Earnings tab.

The corpus (per-ticker folders of 10-Q / 10-K files — a raw ``.html`` and a cleaned
``.md`` per filing) lives at ``settings.edgar_dir`` — on Hermes/AI-Hermes the
Resilio-synced ``C:\\HermesSync\\MarketResearch\\QuarterlyReport``. When that dir
isn't reachable the caller falls back to the pushed ``EdgarIngestHealth`` inventory
(list only, no bodies). We only ever read files that appear in a ticker's own
folder listing — a requested (symbol, period) is resolved to a real filename, so
there's no path traversal.
"""
from __future__ import annotations

import os
import re

from ..config import settings

_PERIOD_RE = re.compile(r"(20\d{2})[\-_](Q[1-4]|FY)", re.IGNORECASE)
_FORM_RE = re.compile(r"10[\-_]?([QK])", re.IGNORECASE)
_DEFAULT_ROOT = r"C:\HermesSync\MarketResearch\QuarterlyReport"


def _root() -> str:
    return (settings.edgar_dir or "").strip() or _DEFAULT_ROOT


def _ticker_dir(symbol: str) -> str:
    # symbol is uppercased + stripped; only [A-Z0-9.-] survive so it can't escape root.
    sym = re.sub(r"[^A-Z0-9.\-]", "", (symbol or "").strip().upper())
    return os.path.join(_root(), sym)


def corpus_available() -> bool:
    return os.path.isdir(_root())


def _scan(symbol: str) -> dict:
    """{(year, period): {year, period, form, md, html, mtime}} for a ticker folder."""
    d = _ticker_dir(symbol)
    out: dict = {}
    if not os.path.isdir(d):
        return out
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for name in names:
        path = os.path.join(d, name)
        if not os.path.isfile(path):
            continue
        pm = _PERIOD_RE.search(name)
        if not pm:
            continue
        yr, per = int(pm.group(1)), pm.group(2).upper()
        fm = _FORM_RE.search(name)
        is_k = per == "FY" or (fm and fm.group(1).upper() == "K")
        low = name.lower()
        f = out.setdefault((yr, per), {"year": yr, "period": per, "form": "10-Q",
                                       "md": None, "html": None, "mtime": 0.0})
        if is_k:
            f["form"] = "10-K"
        try:
            mt = os.stat(path).st_mtime
        except OSError:
            mt = 0.0
        f["mtime"] = max(f["mtime"], mt)
        if low.endswith((".html", ".htm")):
            f["html"] = name
        elif low.endswith(".md"):
            f["md"] = name
    return out


def _period_key(f: dict) -> tuple[int, int]:
    return (f["year"], 4 if f["period"] == "FY" else int(f["period"][1]))


def list_filings(symbol: str) -> list[dict]:
    """Newest-first [{period, form, has_md, has_html}] for one ticker's LOCAL corpus
    folder. Empty when the corpus dir isn't reachable / the ticker isn't present."""
    rows = sorted(_scan(symbol).values(), key=_period_key, reverse=True)
    return [
        {"period": f"{f['year']}-{f['period']}", "form": f["form"],
         "has_md": bool(f["md"]), "has_html": bool(f["html"])}
        for f in rows
    ]


def filing_meta(symbol: str, period: str) -> dict | None:
    """Metadata for ONE period's filing (resolved from the folder listing):
    {period, form, has_html, has_md, html_name, md_name} or None if not present."""
    want = (period or "").strip().upper()
    for f in _scan(symbol).values():
        if f"{f['year']}-{f['period']}" == want:
            return {"period": want, "form": f["form"],
                    "has_html": bool(f["html"]), "has_md": bool(f["md"]),
                    "html_name": f["html"], "md_name": f["md"]}
    return None


def read_file(symbol: str, period: str, kind: str) -> str | None:
    """Raw text of one filing's ``html`` or ``md`` file for a period. The filename is
    resolved from the folder listing (never taken from the request), so only real
    corpus files are read — no path traversal."""
    meta = filing_meta(symbol, period)
    if not meta:
        return None
    name = meta["html_name"] if kind == "html" else meta["md_name"]
    if not name:
        return None
    path = os.path.join(_ticker_dir(symbol), name)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None
