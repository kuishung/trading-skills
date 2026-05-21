"""GUNS — float lookup.

GUNS rule (Adam Khoo Lesson 8): only trade LOW-FLOAT names. The PDF
threshold is < 100M shares free float — micro/small-caps run; large
caps don't move enough intraday to fit the 2R-in-minutes profile.

This module is the GUNS scanner's float-filter source. It is NOT a
general-purpose float utility — the threshold default (100M) and the
fallback policy ("unknown -> keep with warning") are tuned for the
GUNS use case.

Source: yfinance Ticker.info.
  - Prefer `floatShares` (true free float).
  - Fall back to `sharesOutstanding` if float is missing. This is
    overcounting (insider/locked shares included) so the filter is
    conservative — a true-100M float might report 130M outstanding
    and get dropped. Acceptable for GUNS: it's a safety net.

Caching:
  state/cache/float_<symbol>.json  with TTL 7 days.
  Float doesn't change day-to-day; a weekly refresh is plenty. Cache
  miss falls through to yfinance; cache hit returns instantly.

Graceful degradation:
  If yfinance is not installed, every lookup returns None with a
  single stderr warning per process. The scanner treats None as
  "unknown float" — kept with a CAUTION comment, not dropped.

Run as CLI for debugging:
    py scripts/guns_float_lookup.py AAPL NVDA HIMS
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPTS_DIR.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from _common import STATE_DIR, safe_log_stdout  # noqa: E402

CACHE_DIR = STATE_DIR / "cache"
CACHE_TTL_S = 7 * 24 * 3600     # 7 days
GUNS_FLOAT_CAP = 100_000_000    # PDF: low float = < 100M


_yf_warned = False


def _load_yf():
    global _yf_warned
    try:
        import yfinance as yf
        return yf
    except ImportError:
        if not _yf_warned:
            sys.stderr.write(
                "guns_float_lookup: yfinance not installed — float filter will be skipped.\n"
                "  Install with: py -m pip install yfinance\n"
            )
            _yf_warned = True
        return None


def _cache_path(symbol: str) -> Path:
    return CACHE_DIR / f"float_{symbol.upper()}.json"


def _read_cache(symbol: str) -> int | None | str:
    """Return cached float (int), None if cache says "unknown", or
    sentinel string "MISS" if no fresh cache entry exists."""
    path = _cache_path(symbol)
    if not path.exists():
        return "MISS"
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return "MISS"
    fetched_at = blob.get("fetched_at", 0)
    if (time.time() - fetched_at) > CACHE_TTL_S:
        return "MISS"
    return blob.get("float_shares")     # int or None


def _write_cache(symbol: str, float_shares: int | None,
                 outstanding: int | None) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    blob = {
        "symbol": symbol.upper(),
        "fetched_at": time.time(),
        "float_shares": float_shares,
        "shares_outstanding": outstanding,
    }
    try:
        _cache_path(symbol).write_text(json.dumps(blob), encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(f"float cache write failed for {symbol}: {exc}\n")


def get_float(symbol: str, use_cache: bool = True) -> int | None:
    """Return free float (or shares outstanding fallback). None on failure.

    A None return means "couldn't determine" — caller should NOT treat
    it as "passes filter" automatically. Scanner default is to keep
    unknowns with a CAUTION comment so the user can decide.
    """
    symbol = symbol.upper()
    if use_cache:
        cached = _read_cache(symbol)
        if cached != "MISS":
            return cached     # may be int or None

    yf = _load_yf()
    if yf is None:
        return None

    try:
        info = yf.Ticker(symbol).info or {}
    except Exception as exc:
        sys.stderr.write(f"yfinance lookup failed for {symbol}: {exc}\n")
        _write_cache(symbol, None, None)
        return None

    float_shares = info.get("floatShares")
    outstanding = info.get("sharesOutstanding")
    chosen: int | None = None
    if isinstance(float_shares, (int, float)) and float_shares > 0:
        chosen = int(float_shares)
    elif isinstance(outstanding, (int, float)) and outstanding > 0:
        chosen = int(outstanding)

    _write_cache(symbol, chosen, int(outstanding) if outstanding else None)
    return chosen


def bulk_get_floats(symbols: list[str], use_cache: bool = True) -> dict[str, int | None]:
    """Look up floats for many symbols. Returns {SYM: int|None}.

    No parallelism — yfinance is rate-limited and the GUNS watchlist is
    small (typically <20 symbols), so serial is fine and predictable.
    """
    out: dict[str, int | None] = {}
    for s in symbols:
        out[s.upper()] = get_float(s, use_cache=use_cache)
    return out


def passes_float_filter(float_shares: int | None,
                        cap: int = GUNS_FLOAT_CAP) -> tuple[bool, str]:
    """Decide whether `float_shares` passes the GUNS low-float filter.

    Returns (passes, reason).
      - None float -> (True, "unknown") so caller can apply CAUTION flag
        rather than silently dropping. Use --strict-float to override.
      - 0 / negative -> (False, "invalid") — bad data.
      - > cap -> (False, "high_float")
      - else -> (True, "low_float")
    """
    if float_shares is None:
        return True, "unknown"
    if float_shares <= 0:
        return False, "invalid"
    if float_shares > cap:
        return False, "high_float"
    return True, "low_float"


def _fmt_shares(n: int | None) -> str:
    if n is None:
        return "n/a"
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{n:,}"


def _cli(argv: list[str]) -> int:
    if not argv:
        sys.stdout.write(__doc__)
        return 0
    use_cache = "--no-cache" not in argv
    symbols = [a for a in argv if not a.startswith("--")]
    for sym in symbols:
        n = get_float(sym, use_cache=use_cache)
        passes, reason = passes_float_filter(n)
        verdict = "PASS" if passes else "DROP"
        if reason == "unknown":
            verdict = "WARN"
        safe_log_stdout(f"{sym.upper():<8} {_fmt_shares(n):>8}  [{verdict}] {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
