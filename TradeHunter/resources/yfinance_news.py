"""GUNS — catalyst classifier.

GUNS rule (Adam Khoo Lesson 8): a gap-up name needs REAL news to run.
Two failure modes the strategy explicitly bans:

  1. M&A target — gap is locked at the buyout price; no extension.
  2. Capital raises (offerings, dilution, S-3, ATM, PIPE) — sellers
     bleed all morning; price drifts to the offering ref price.

This classifier reads a symbol's recent headlines and tags the gap
as `good`, `bad`, or `unknown`. The scanner drops `bad` from the
watchlist and flags `unknown` with a CAUTION comment so the user
can decide.

This is a GUNS-specific module — the bad/good patterns are tuned for
the PDF's recipe, not a general news sentiment tool. The scanner is
its only intended consumer; do not import it from non-GUNS code.

Source: yfinance Ticker.news (free, no API key). Each item:
    item["content"]["title"]
    item["content"]["pubDate"]      ISO 8601
    item["content"]["provider"]["displayName"]
    item["content"]["canonicalUrl"]["url"]   (may be missing)

Freshness:
    Only news from the last `MAX_AGE_HOURS` (default 36) is considered.
    Gap catalysts are same-day or last-night events; older stuff
    doesn't explain a today-only gap.

Priority:
    BAD always wins over GOOD when both appear in the freshness window.
    A stock with both an earnings beat AND an M&A-target headline is
    still M&A-locked.

Caching:
    state/cache/catalyst_<symbol>.json  with TTL 4 hours.
    Short TTL so a 09:00 rescan picks up overnight news a 04:00 cache
    miss might have stored as unknown.

Run as CLI for debugging:
    py scripts/guns_catalyst_classifier.py MLGO HIMS DJT
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# --- TradeHunter bootstrap: make sibling layers importable ---
_root = Path(__file__).resolve().parent
while _root != _root.parent and not (_root / "SKILL.md").exists():
    _root = _root.parent
SKILL_DIR = _root
for _p in [str(_root)] + [str(_root / s) for s in
        ("scripts", "resources", "strategy", "execution", "journal", "review", "dashboard")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)
del _root, _p
# ---

from _common import STATE_DIR, safe_log_stdout  # noqa: E402

CACHE_DIR = STATE_DIR / "cache"
CACHE_TTL_S = 4 * 3600
MAX_AGE_HOURS = 36


# ---------- Pattern tables ----------
#
# Order within a class matters only for the category label that gets
# attached. Any match in BAD_PATTERNS wins over any match in
# GOOD_PATTERNS. Patterns are case-insensitive. Use \b boundaries to
# avoid sub-string traps (e.g. "offer" inside "offering").

BAD_PATTERNS: list[tuple[str, str]] = [
    # M&A target / take-private (GUNS calls these "gap locked")
    ("ma_target",   r"\bto be acquired\b"),
    ("ma_target",   r"\bagrees to be acquired\b"),
    ("ma_target",   r"\bacquired by\b"),
    ("ma_target",   r"\bacquisition by\b"),
    ("ma_target",   r"\bdefinitive merger agreement\b"),
    ("ma_target",   r"\bdefinitive agreement\b"),
    ("ma_target",   r"\bmerger agreement\b"),
    ("ma_target",   r"\bbuyout offer\b"),
    ("ma_target",   r"\btake[- ]private\b"),
    ("ma_target",   r"\bgoing private\b"),
    ("ma_target",   r"\btender offer\b"),
    ("ma_target",   r"\ball[- ](?:stock|cash) (?:deal|transaction|offer)\b"),
    # Generic "to acquire" — when our symbol IS in the headline the gap
    # is either acquirer or target; PDF says drop either way.
    ("ma_generic",  r"\bto acquire\b"),
    ("ma_generic",  r"\bagrees to acquire\b"),
    ("ma_generic",  r"\bin talks to (?:buy|acquire|merge)\b"),

    # Capital raise / dilution
    ("offering",    r"\bpublic offering\b"),
    ("offering",    r"\bunderwritten offering\b"),
    ("offering",    r"\bcommon stock offering\b"),
    ("offering",    r"\bsecondary offering\b"),
    ("offering",    r"\bregistered direct (?:offering)?\b"),
    ("offering",    r"\bat[- ]the[- ]market offering\b"),
    ("offering",    r"\bATM (?:offering|sales)\b"),
    ("offering",    r"\bPIPE (?:financing|investment|deal)\b"),
    ("offering",    r"\bprivate placement\b"),
    ("offering",    r"\bshelf registration\b"),
    ("offering",    r"\bproposed (?:offering|public offering)\b"),
    ("offering",    r"\bprices (?:public )?offering\b"),
    ("offering",    r"\bpricing of (?:public )?offering\b"),
    ("offering",    r"\bconvertible (?:notes? offering|senior notes?)\b"),
    ("offering",    r"\bincreases? authorized shares\b"),
    ("dilution",    r"\bwarrant (?:exercise|inducement)\b"),

    # Structural bad
    ("split",       r"\breverse (?:stock )?split\b"),
    ("split",       r"\b1[- ]for[- ]\d{1,3} (?:reverse )?split\b"),
    ("going_concern", r"\bgoing concern\b"),
    ("going_concern", r"\bsubstantial doubt\b"),
    ("bankruptcy",  r"\bchapter 1[13]\b"),
    ("bankruptcy",  r"\bfiles for bankruptcy\b"),
    ("delisting",   r"\bdelisting\b"),
    ("delisting",   r"\bdelisted\b"),
    ("delisting",   r"\bnotice of non[- ]compliance\b"),

    # Regulatory bad
    ("sec_action",  r"\bSEC (?:investigat|subpoena|charge|sues|action)"),
    ("sec_action",  r"\bDOJ (?:investigat|probe|charge)"),
    ("sec_action",  r"\bFTC (?:investigat|probe|sues)"),
    ("fraud",       r"\bfraud\b"),
    ("fraud",       r"\baccounting (?:fraud|scandal|restatement)\b"),
    ("class_action", r"\bclass action\b"),
    ("class_action", r"\bshareholder lawsuit\b"),
    ("fda_bad",     r"\bFDA reject"),
    ("fda_bad",     r"\bcomplete response letter\b"),
    ("fda_bad",     r"\bCRL\b"),
    ("fda_bad",     r"\bclinical hold\b"),
    ("fda_bad",     r"\btrial halt"),
    ("guidance_cut", r"\bcuts? (?:full[- ]year )?guidance\b"),
    ("guidance_cut", r"\blowers? (?:full[- ]year )?guidance\b"),
]

GOOD_PATTERNS: list[tuple[str, str]] = [
    # Earnings (most common GUNS catalyst)
    ("earnings_beat", r"\bbeats?\b.{0,30}\b(?:estimates?|expectations?|consensus)\b"),
    ("earnings_beat", r"\btops?\b.{0,30}\bQ[1-4]\b"),
    ("earnings_beat", r"\bQ[1-4]\b.{0,30}\bbeat\b"),
    ("earnings_beat", r"\bearnings (?:beat|smash|crush|trounce)"),
    ("earnings_beat", r"\bblowout earnings\b"),
    ("earnings_beat", r"\brecord (?:revenue|sales|profit|earnings)\b"),
    ("guidance_up", r"\braises? (?:full[- ]year )?guidance\b"),
    ("guidance_up", r"\braised (?:full[- ]year )?guidance\b"),
    ("guidance_up", r"\bupbeat outlook\b"),

    # FDA / drug
    ("fda_good",   r"\bFDA approv"),
    ("fda_good",   r"\bFDA clear"),
    ("fda_good",   r"\bFDA grants?\b"),
    ("fda_good",   r"\bbreakthrough (?:designation|therapy)\b"),
    ("fda_good",   r"\borphan drug (?:designation)?\b"),
    ("trial_good", r"\bphase (?:II|III|2|3) (?:data|results|trial)"),
    ("trial_good", r"\bprimary endpoint\b"),
    ("trial_good", r"\bmet (?:its )?(?:primary )?endpoint"),
    ("trial_good", r"\bpositive (?:topline )?(?:data|results)\b"),

    # Contracts / partnerships
    ("contract",   r"\b(?:wins?|awarded|secures?|receives?) (?:a )?(?:contract|order)\b"),
    ("contract",   r"\bdepartment of defense\b"),
    ("contract",   r"\bpentagon (?:awards?|contract)\b"),
    ("contract",   r"\b\$\d+[\.,]?\d* ?(?:million|billion|M|B) contract\b"),
    ("partnership", r"\bstrategic partnership\b"),
    ("partnership", r"\bpartnership with\b"),
    ("partnership", r"\bcollaboration with\b"),
    ("partnership", r"\bteams up with\b"),

    # Analyst action
    ("upgrade",    r"\bupgraded? to (?:buy|outperform|overweight|strong buy)\b"),
    ("upgrade",    r"\bprice target (?:raise|raised|increased|hiked)\b"),
    ("upgrade",    r"\binitiated (?:coverage )?(?:at|with) (?:buy|outperform)\b"),

    # Generic AI sympathy (small-cap gappers ride AI news)
    ("ai_play",    r"\b(?:artificial intelligence|AI[- ](?:tech|chip|stock|partnership))\b"),
    ("ai_play",    r"\b(?:nvidia|openai|anthropic) (?:partnership|deal|contract)\b"),
]

# Pre-compile.
_BAD_RE = [(cat, re.compile(p, re.I)) for cat, p in BAD_PATTERNS]
_GOOD_RE = [(cat, re.compile(p, re.I)) for cat, p in GOOD_PATTERNS]


# ---------- yfinance lazy import ----------

_yf_warned = False


def _load_yf():
    global _yf_warned
    try:
        import yfinance as yf
        return yf
    except ImportError:
        if not _yf_warned:
            sys.stderr.write(
                "guns_catalyst_classifier: yfinance not installed — catalyst filter "
                "will be skipped.\n"
                "  Install with: py -m pip install yfinance\n"
            )
            _yf_warned = True
        return None


# ---------- Cache ----------

def _cache_path(symbol: str) -> Path:
    return CACHE_DIR / f"catalyst_{symbol.upper()}.json"


def _read_cache(symbol: str) -> dict | str:
    path = _cache_path(symbol)
    if not path.exists():
        return "MISS"
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return "MISS"
    if (time.time() - blob.get("fetched_at", 0)) > CACHE_TTL_S:
        return "MISS"
    return blob.get("result", "MISS")


def _write_cache(symbol: str, result: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    blob = {"fetched_at": time.time(), "result": result}
    try:
        _cache_path(symbol).write_text(json.dumps(blob, default=str), encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(f"catalyst cache write failed for {symbol}: {exc}\n")


# ---------- News fetch ----------

def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        # yfinance pubDate is e.g. "2026-05-21T01:00:00Z"
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _fetch_news(symbol: str) -> list[dict]:
    yf = _load_yf()
    if yf is None:
        return []
    try:
        news = yf.Ticker(symbol).news or []
    except Exception as exc:
        sys.stderr.write(f"yfinance news fetch failed for {symbol}: {exc}\n")
        return []
    fresh: list[dict] = []
    cutoff = datetime.now(timezone.utc).timestamp() - MAX_AGE_HOURS * 3600
    for item in news:
        content = item.get("content") or {}
        title = content.get("title") or ""
        pub = _parse_iso(content.get("pubDate") or content.get("displayTime"))
        if not title or pub is None:
            continue
        if pub.timestamp() < cutoff:
            continue
        provider = (content.get("provider") or {}).get("displayName")
        url = ((content.get("canonicalUrl") or {}).get("url")
               or (content.get("clickThroughUrl") or {}).get("url"))
        fresh.append({
            "title": title,
            "pub_date": pub.isoformat(),
            "provider": provider,
            "url": url,
        })
    return fresh


# ---------- Classify ----------

def _scan_patterns(headlines: list[dict], regex_table) -> list[dict]:
    """Return a list of {category, keyword, headline, url, pub_date} for
    every pattern that matched any headline. Empty list if none."""
    hits: list[dict] = []
    for h in headlines:
        title = h["title"]
        for category, rx in regex_table:
            m = rx.search(title)
            if m:
                hits.append({
                    "category": category,
                    "keyword": m.group(0),
                    "headline": title,
                    "url": h.get("url"),
                    "pub_date": h.get("pub_date"),
                })
    return hits


def classify(symbol: str, use_cache: bool = True) -> dict:
    """Return {classification, category, headline, url, pub_date,
    matches[], confidence} for `symbol`.

    Shape:
      classification: "good" | "bad" | "unknown"
      category:       human label of the matched pattern family
                      ("ma_target", "earnings_beat", ...) or None
      headline:       the matched headline (truncated to 140 chars)
      url:            link to story (may be None)
      pub_date:       ISO timestamp of the matched headline
      matches:        list of every BAD or GOOD hit found (debugging)
      confidence:     0.0 to 1.0
                      0.9: multiple matches in same class
                      0.7: single match
                      0.5: only weak match (ma_generic, ai_play)
                      0.0: unknown
    """
    symbol = symbol.upper()
    if use_cache:
        cached = _read_cache(symbol)
        if cached != "MISS":
            return cached

    news = _fetch_news(symbol)
    if not news:
        result = {
            "classification": "unknown", "category": None, "headline": None,
            "url": None, "pub_date": None, "matches": [], "confidence": 0.0,
            "n_headlines_considered": 0,
        }
        _write_cache(symbol, result)
        return result

    bad_hits = _scan_patterns(news, _BAD_RE)
    good_hits = _scan_patterns(news, _GOOD_RE)

    if bad_hits:
        # BAD always wins. Pick the highest-priority match (first in the
        # BAD_PATTERNS list = most specific).
        chosen = bad_hits[0]
        confidence = 0.9 if len(bad_hits) >= 2 else 0.7
        if chosen["category"] in ("ma_generic", "ai_play"):
            confidence = min(confidence, 0.5)
        result = {
            "classification": "bad",
            "category": chosen["category"],
            "headline": chosen["headline"][:140],
            "url": chosen["url"],
            "pub_date": chosen["pub_date"],
            "matches": bad_hits + good_hits,
            "confidence": confidence,
            "n_headlines_considered": len(news),
        }
    elif good_hits:
        chosen = good_hits[0]
        confidence = 0.9 if len(good_hits) >= 2 else 0.7
        if chosen["category"] == "ai_play":
            confidence = 0.5
        result = {
            "classification": "good",
            "category": chosen["category"],
            "headline": chosen["headline"][:140],
            "url": chosen["url"],
            "pub_date": chosen["pub_date"],
            "matches": good_hits,
            "confidence": confidence,
            "n_headlines_considered": len(news),
        }
    else:
        result = {
            "classification": "unknown", "category": None, "headline": None,
            "url": None, "pub_date": None, "matches": [], "confidence": 0.0,
            "n_headlines_considered": len(news),
        }

    _write_cache(symbol, result)
    return result


def bulk_classify(symbols: list[str], use_cache: bool = True) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for s in symbols:
        out[s.upper()] = classify(s, use_cache=use_cache)
    return out


def passes_catalyst_filter(result: dict, strict: bool = False) -> tuple[bool, str]:
    """Decide whether `result` from classify() passes GUNS.

    Default (strict=False):
      good    -> pass
      unknown -> pass with CAUTION
      bad     -> drop

    strict=True:
      good    -> pass
      else    -> drop (no caution-keep)
    """
    cls = result.get("classification")
    if cls == "bad":
        return False, f"bad:{result.get('category')}"
    if cls == "good":
        return True, f"good:{result.get('category')}"
    # unknown
    if strict:
        return False, "no_catalyst"
    return True, "unknown_catalyst"


# ---------- CLI ----------

def _cli(argv: list[str]) -> int:
    if not argv:
        sys.stdout.write(__doc__)
        return 0
    use_cache = "--no-cache" not in argv
    strict = "--strict" in argv
    symbols = [a for a in argv if not a.startswith("--")]
    for sym in symbols:
        r = classify(sym, use_cache=use_cache)
        passes, reason = passes_catalyst_filter(r, strict=strict)
        verdict = "PASS" if passes and r["classification"] == "good" else (
            "WARN" if passes else "DROP")
        head = (r["headline"] or "(no fresh news)")[:80]
        safe_log_stdout(
            f"{sym.upper():<8} [{verdict}] {r['classification']:<8} "
            f"{(r['category'] or '-'):<14} conf={r['confidence']:.1f}  {head}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
