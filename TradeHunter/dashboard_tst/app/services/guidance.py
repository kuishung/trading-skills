"""Forward GUIDANCE from SEC 8-K earnings releases.

Why a separate source from the corpus
-------------------------------------
The local corpus (``edgar_reports.py``) holds 10-Q / 10-K filings, and those
contain **no forward guidance** — verified against NVDA's 2026-Q1 10-Q, which has
zero guidance sentences in 135 KB. Companies publish guidance in the **8-K tagged
item 2.02** ("Results of Operations and Financial Condition"), in the press-release
exhibit (usually EX-99.1). So guidance needs its own fetch; it cannot be mined out
of what we already store.

Everything here is free and official: the SEC submissions + Archives endpoints
need no API key, only a declared ``User-Agent`` (``settings.sec_user_agent``) and
respect for the ~10 req/s fair-use limit.

Numbers, not prose
------------------
Guidance sentences are highly patterned ("Revenue is expected to be $108.0 billion,
plus or minus 2%"), so the figures are extracted **deterministically** — no LLM.
That keeps them exact, auditable and free, and matches the split in
COMPANY_INTELLIGENCE_DESIGN.md: quantitative -> structured data, qualitative -> LLM.
An LLM narrative can be layered on later; it must never be what produces a number.

Coverage is honestly partial by construction: many issuers give guidance only on
the earnings CALL, which is not an SEC filing. When nothing is found we record the
filing with ``items: []`` rather than inventing a figure, so "no guidance issued"
is distinguishable from "not yet fetched".
"""
from __future__ import annotations

import datetime as _dt
import re
import time

import httpx

from ..config import settings

SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{accn}"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

_MIN_INTERVAL = 0.15          # ~7 req/s, inside SEC's ~10 req/s fair-use limit
_last_call = 0.0


def _headers() -> dict:
    return {"User-Agent": settings.sec_user_agent, "Accept-Encoding": "gzip, deflate"}


def _get(url: str, *, timeout: float = 30.0) -> httpx.Response:
    """Rate-limited GET. SEC blocks aggressive clients, so pace every call."""
    global _last_call
    wait = _MIN_INTERVAL - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()
    return httpx.get(url, headers=_headers(), timeout=timeout, follow_redirects=True)


# ---------------------------------------------------------------- ticker -> CIK
_cik_cache: dict[str, int] = {}


def cik_for(symbol: str) -> int | None:
    """CIK for a ticker, from SEC's free company_tickers.json (cached in-process)."""
    global _cik_cache
    sym = (symbol or "").strip().upper()
    if not sym:
        return None
    if not _cik_cache:
        try:
            data = _get(TICKERS_URL).json()
        except Exception:  # noqa: BLE001
            return None
        for row in data.values():
            _cik_cache[str(row["ticker"]).upper()] = int(row["cik_str"])
    return _cik_cache.get(sym)


# ---------------------------------------------------------------- filings
def earnings_8ks(symbol: str, *, limit: int = 40) -> list[dict]:
    """Newest-first earnings 8-Ks: [{accession, filed, cik, items}].

    Item **2.02** is the marker for "Results of Operations" — that is what makes an
    8-K an earnings release rather than a director change or a debt issuance.
    """
    cik = cik_for(symbol)
    if not cik:
        return []
    try:
        recent = _get(SUBMISSIONS.format(cik=cik)).json()["filings"]["recent"]
    except Exception:  # noqa: BLE001
        return []

    out = []
    for form, filed, accn, items in zip(recent.get("form", []),
                                        recent.get("filingDate", []),
                                        recent.get("accessionNumber", []),
                                        recent.get("items", [])):
        if form != "8-K" or "2.02" not in (items or ""):
            continue
        out.append({"accession": accn, "filed": filed, "cik": cik, "items": items})
        if len(out) >= limit:
            break
    return out


def _exhibit_urls(cik: int, accession: str) -> list[str]:
    """Candidate press-release documents inside a filing, best-guess first.

    Issuers do NOT name the exhibit predictably — NVDA files ``q2fy27pr.htm``, not
    ``ex99-1.htm`` — so filename matching alone misses them. We take every HTML
    document in the filing except the obvious non-candidates (the XBRL cover page,
    the R*.htm viewer fragments) and let the extractor decide which one actually
    carries guidance.
    """
    accn = accession.replace("-", "")
    base = ARCHIVE.format(cik=cik, accn=accn)
    try:
        items = _get(f"{base}/index.json").json()["directory"]["item"]
    except Exception:  # noqa: BLE001
        return []

    names = [it["name"] for it in items
             if it["name"].lower().endswith((".htm", ".html"))
             and not re.match(r"^R\d+\.htm", it["name"])           # XBRL viewer fragments
             and "index" not in it["name"].lower()]

    def rank(n: str) -> tuple:
        low = n.lower()
        # press-release-ish names first; the XBRL cover doc (ticker-date.htm) last
        pr = any(k in low for k in ("ex99", "ex-99", "pr.htm", "press", "release",
                                    "earnings", "commentary", "results"))
        cover = bool(re.match(r"^[a-z]+-?\d{8}\.htm", low))
        return (0 if pr else 1, 1 if cover else 0, len(n))

    return [f"{base}/{n}" for n in sorted(names, key=rank)]


# ---------------------------------------------------------------- extraction
def html_to_text(html: str) -> str:
    """Flatten filing HTML to searchable text (tables become spaced runs)."""
    txt = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    txt = re.sub(r"(?i)<(br|/tr|/p|/div|/td)[^>]*>", " ", txt)
    txt = re.sub(r"<[^>]+>", " ", txt)
    for ent, ch in (("&nbsp;", " "), ("&#160;", " "), ("&amp;", "&"), ("&#38;", "&"),
                    ("&#8217;", "'"), ("&rsquo;", "'"), ("&#8212;", "-"),
                    ("&mdash;", "-"), ("&#8211;", "-"), ("&ndash;", "-"),
                    ("&#36;", "$"), ("&#8226;", "-"), ("&bull;", "-"),
                    ("&#58;", ":"), ("&quot;", '"'), ("&#39;", "'"), ("&lt;", "<"),
                    ("&gt;", ">"), ("&#8734;", "inf")):
        txt = txt.replace(ent, ch)
    txt = re.sub(r"&#\d+;", " ", txt)
    return re.sub(r"\s+", " ", txt).strip()


# Forward-expectation statements. Anchored on an expectation verb or an explicit
# guidance/outlook phrase so ordinary past-tense results ("revenue WAS $89.0
# billion") never match. Segments end at a period OR a bullet/pipe boundary --
# issuers put guidance in bulleted lists and table rows at least as often as in
# prose, and requiring a full stop silently dropped all of those.
_GUIDE_SENT = re.compile(
    r"(?:[^.|]|\.(?=\d)){0,220}?\b(?:"
    r"(?:we|the\s+company|management|it)\s+(?:currently\s+|now\s+|continues?\s+to\s+)?"
    r"(?:expects?|anticipates?|projects?|forecasts?|is\s+guiding|reaffirms?|raises?|updates?)"
    r"|(?:is|are)\s+expected\s+to\s+be"
    r"|expects?\s+(?:full[\s-]?year|fiscal|second|third|fourth|next)"
    r"|(?:full[\s-]?year|fiscal\s+\d{4}|20\d\d)\s+(?:guidance|outlook)"
    r"|guidance\s+(?:for|of|range)"
    r"|outlook\s+for\s+(?:the\s+)?(?:first|second|third|fourth|full[\s-]?year|fiscal|next)"
    r"|to\s+be\s+in\s+the\s+range\s+of"
    r"|growth\s+of\s+approximately"
    r")(?:[^.|]|\.(?=\d)){0,340}(?:\.(?!\d)|\||$)",
    re.I,
)

# Headers that introduce a guidance BLOCK. Used to prioritise a region, never to
# exclude the rest of the document -- the first "Guidance" hit is usually the
# headline ("Reaffirms Fiscal 2026 Guidance"), while the numbers sit far below it.
_OUTLOOK_HDR = re.compile(
    r"\b(?:business\s+outlook|financial\s+outlook|fiscal\s+\d{4}\s+guidance|"
    r"full[\s-]?year\s+(?:guidance|outlook)|guidance|outlook)\b", re.I)

_METRIC_PATTERNS = [
    ("revenue", r"\b(?:total\s+)?(?:revenue|net\s+sales|sales|comparable\s+sales)\b"),
    ("gross_margin", r"\bgross\s+margins?\b"),
    ("operating_expenses", r"\boperating\s+expenses?\b"),
    ("operating_margin", r"\boperating\s+margins?\b"),
    ("eps", r"\b(?:diluted\s+)?(?:earnings\s+per\s+share|EPS)\b"),
    ("tax_rate", r"\btax\s+rates?\b"),
    ("capex", r"\b(?:capital\s+expenditures?|capex)\b"),
    ("free_cash_flow", r"\bfree\s+cash\s+flow\b"),
    ("operating_income", r"\boperating\s+(?:income|earnings|profit)\b"),
]

_MONEY = re.compile(
    r"\$\s?([\d,]+(?:\.\d+)?)\s*(billion|million|bn|mm|m\b|b\b)?", re.I)
_PCT = re.compile(r"([\d.]+)\s?%")
_BOILERPLATE = re.compile(
    r"forward[\s-]looking statements|Private Securities Litigation|"
    r"do not undertake to update|speak only as of|risks?\s+and\s+uncertaint", re.I)


def _classify(sentence: str) -> list[str]:
    return [name for name, pat in _METRIC_PATTERNS if re.search(pat, sentence, re.I)]


def extract_guidance(text: str) -> list[dict]:
    """Guidance statements found in a press release.

    Returns [{metric, sentence, money, percents}]. ``metric`` may be ``"other"``
    when a forward statement doesn't map to a known line item -- kept rather than
    dropped, because the sentence itself is the durable artifact for the knowledge
    base.

    The WHOLE document is scanned. An earlier version only looked 2500 chars past
    each "guidance"/"outlook" header, which matched the press-release HEADLINE
    ("Reaffirms Fiscal 2026 Guidance") and therefore scanned the results paragraphs
    instead of the guidance section further down -- it found nothing for HD, KO or
    CAT, all of which do guide. Precision now comes from the sentence pattern plus
    the boilerplate filter, not from where we look.
    """
    if not text:
        return []

    seen, out = set(), []
    for m in _GUIDE_SENT.finditer(text):
        sent = re.sub(r"\s+", " ", m.group(0)).strip(" -•	|")
        if len(sent) < 25 or _BOILERPLATE.search(sent):
            continue
        key = sent.lower()[:120]
        if key in seen:
            continue
        seen.add(key)

        money = [{"amount": float(a.replace(",", "")), "scale": (s or "").lower() or None}
                 for a, s in _MONEY.findall(sent)]
        pcts = [float(p) for p in _PCT.findall(sent)]
        if not money and not pcts:
            continue                    # a forward sentence with no number is narrative
        for metric in (_classify(sent) or ["other"]):
            out.append({"metric": metric, "sentence": sent,
                        "money": money, "percents": pcts})
    return out


def guidance_for_filing(cik: int, accession: str) -> dict:
    """Pull one earnings 8-K and extract its guidance.

    Returns {accession, source_url, items, chars}. ``items == []`` means the filing
    was read and genuinely contained no quantified guidance — recorded as a fact,
    never guessed at.
    """
    for url in _exhibit_urls(cik, accession)[:6]:
        try:
            html = _get(url).text
        except Exception:  # noqa: BLE001
            continue
        text = html_to_text(html)
        if len(text) < 400:
            continue
        items = extract_guidance(text)
        if items:
            return {"accession": accession, "source_url": url,
                    "items": items, "chars": len(text)}
    return {"accession": accession, "source_url": None, "items": [], "chars": 0}


def period_label(filed: str, fiscal_hint: str | None = None) -> str:
    """Calendar period label for the note filename, e.g. ``2026-Q3``.

    Uses the FILING date's calendar quarter, not the fiscal quarter: fiscal years
    differ per issuer (NVDA's FY2027 Q2 is calendar 2026), and a filing-dated label
    keeps notes sortable and comparable across companies. The fiscal wording from
    the release is preserved inside the note itself.
    """
    try:
        d = _dt.date.fromisoformat(filed)
    except (TypeError, ValueError):
        return "unknown"
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"
