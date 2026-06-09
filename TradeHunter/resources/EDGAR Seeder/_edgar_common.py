"""Shared helpers for the EDGAR Seeder.

A self-contained SEC EDGAR client built on the Python standard library only
(urllib + html.parser) — no `sec-edgar-downloader`, no `requests`. It talks to
SEC's official REST/JSON APIs directly, which gives us:

  - exact `form`, `accessionNumber`, `filingDate`, `reportDate`, `primaryDocument`
    straight from `data.sec.gov/submissions/CIK<n>.json` (no SGML-header
    scraping, so no "every file defaults to Q1" filename bug)
  - true *fiscal* quarter labelling via the company's `fiscalYearEnd`
    (AAPL/NVDA et al. on non-calendar years come out right)
  - real incremental fetching keyed on accession number (a manifest records
    what's been seeded; `update` only pulls genuinely new filings)

SEC fair-access compliance (both enforced here):
  - **Real User-Agent.** SEC requires a routable `company email` identity and
    throttles / 403s fakes. We NEVER ship a default — the contact is resolved
    from --email/--company, EDGAR_EMAIL/EDGAR_COMPANY env, or a config.json
    `edgar` block, and we hard-fail with guidance if none is found or the email
    is non-routable.
  - **<=10 requests/second.** A process-wide minimum interval is enforced
    between every HTTP call, with exponential backoff on 429/5xx.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL_DIR = HERE.parent.parent                 # resources/EDGAR Seeder -> resources -> TradeHunter
for _p in (str(SKILL_DIR / "scripts"),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---- SEC endpoints ----------------------------------------------------------
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SUBMISSIONS_EXTRA_URL = "https://data.sec.gov/submissions/{name}"
ARCHIVE_DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"

MIN_INTERVAL = 0.15          # s between requests (<10/s, with headroom)
DEFAULT_SINCE = "2011-01-01"
DEFAULT_FORMS = ("10-Q", "10-K")
HTTP_TIMEOUT = 45


# ---- contact / User-Agent ---------------------------------------------------
def resolve_contact(email: str | None = None, company: str | None = None) -> tuple[str, str]:
    """Resolve the SEC User-Agent contact (company + routable email).

    Order: explicit args -> EDGAR_EMAIL/EDGAR_COMPANY env -> config.json
    `edgar` block. Hard-fails (SystemExit) with guidance if unresolved or the
    email is obviously non-routable, because SEC blocks fake identities."""
    email = email or os.environ.get("EDGAR_EMAIL")
    company = company or os.environ.get("EDGAR_COMPANY")
    if not email or not company:
        try:
            from _common import load_config            # type: ignore
            ec = (load_config().get("edgar") or {})
            email = email or ec.get("email")
            company = company or ec.get("company")
        except Exception:
            pass
    if not email or not company:
        raise SystemExit(
            "SEC requires a REAL User-Agent contact (company + routable email).\n"
            "Provide it one of these ways:\n"
            '  --email you@domain.com --company "Your Name / Firm"\n'
            "  set EDGAR_EMAIL and EDGAR_COMPANY environment variables\n"
            '  add  "edgar": {"email": "...", "company": "..."}  to config.json\n'
            "A .local / fake address gets throttled or 403'd by EDGAR."
        )
    if "@" not in email or email.rstrip().endswith(".local"):
        raise SystemExit(
            f"EDGAR contact email {email!r} is not routable; SEC will block it. "
            "Use a real address."
        )
    return email.strip(), company.strip()


def user_agent(email: str, company: str) -> str:
    # SEC's recommended format: "Sample Company Name AdminContact@example.com"
    return f"{company} {email}"


# ---- rate-limited HTTP ------------------------------------------------------
class _Rate:
    last = 0.0


def http_get(url: str, ua: str, binary: bool = False, retries: int = 4):
    """GET with a process-wide rate limit + exponential backoff on 429/5xx.
    Returns str (decoded) or bytes (binary=True)."""
    dt = time.monotonic() - _Rate.last
    if dt < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - dt)
    req = urllib.request.Request(url, headers={"User-Agent": ua,
                                               "Accept": "*/*"})
    delay = 1.0
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                data = r.read()
            _Rate.last = time.monotonic()
            return data if binary else data.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last_exc = e
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(delay); delay *= 2; continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_exc = e
            if attempt < retries:
                time.sleep(delay); delay *= 2; continue
            raise
    raise RuntimeError(f"unreachable: {last_exc}")


# ---- ticker -> CIK ----------------------------------------------------------
def load_cik_map(ua: str, cache_dir: Path) -> dict[str, int]:
    """SEC's ticker->CIK table, cached 7 days under cache_dir."""
    cache = cache_dir / "_company_tickers.json"
    fresh = cache.exists() and (time.time() - cache.stat().st_mtime) < 7 * 86400
    if fresh:
        raw = cache.read_text(encoding="utf-8")
    else:
        raw = http_get(TICKERS_URL, ua)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(raw, encoding="utf-8")
    data = json.loads(raw)
    out: dict[str, int] = {}
    for row in data.values():
        try:
            out[str(row["ticker"]).upper()] = int(row["cik_str"])
        except (KeyError, ValueError, TypeError):
            continue
    return out


# ---- filings ----------------------------------------------------------------
@dataclass
class Filing:
    accession: str       # dashed, e.g. 0000320193-24-000081
    form: str            # 10-Q / 10-K
    filing_date: str     # YYYY-MM-DD
    report_date: str     # YYYY-MM-DD (period end)
    primary_doc: str     # e.g. aapl-20240629.htm
    cik: int

    @property
    def acc_nodash(self) -> str:
        return self.accession.replace("-", "")


def fetch_filings(cik: int, ua: str, forms: tuple[str, ...],
                  since: str) -> tuple[str, list[Filing]]:
    """Return (fiscalYearEnd 'MMDD', [Filing...]) for `forms` filed on/after
    `since`. Walks the paginated overflow files so history reaches back to
    `since` even for prolific filers."""
    raw = http_get(SUBMISSIONS_URL.format(cik=cik), ua)
    j = json.loads(raw)
    fye = str(j.get("fiscalYearEnd") or "")
    out: list[Filing] = []

    def ingest(cols: dict) -> None:
        n = len(cols.get("accessionNumber", []))
        rep = cols.get("reportDate", [""] * n)
        pdoc = cols.get("primaryDocument", [""] * n)
        for i in range(n):
            form = cols["form"][i]
            if form not in forms:
                continue
            fdate = cols["filingDate"][i]
            if since and fdate < since:
                continue
            out.append(Filing(
                accession=cols["accessionNumber"][i],
                form=form,
                filing_date=fdate,
                report_date=(rep[i] or ""),
                primary_doc=(pdoc[i] or ""),
                cik=cik,
            ))

    ingest(j.get("filings", {}).get("recent", {}))
    for extra in j.get("filings", {}).get("files", []):
        # Skip overflow shards whose entire window predates `since`.
        if since and extra.get("filingTo", "9999-99-99") < since:
            continue
        raw2 = http_get(SUBMISSIONS_EXTRA_URL.format(name=extra["name"]), ua)
        ingest(json.loads(raw2))
    return fye, out


def fiscal_label(form: str, report_date: str, fye_mmdd: str) -> str:
    """Filename-safe fiscal-period label: '2024_Q3' / '2024_FY' / 'unknown'.
    Fiscal quarter is computed relative to the company's fiscal year end, so
    non-calendar filers are labelled correctly."""
    if not report_date:
        return "unknown"
    try:
        d = datetime.strptime(report_date, "%Y-%m-%d")
    except ValueError:
        return "unknown"
    if form == "10-K":
        return f"{d.year}_FY"
    try:
        fye_m = int(fye_mmdd[:2]) if fye_mmdd else 12
        if not 1 <= fye_m <= 12:
            fye_m = 12
    except ValueError:
        fye_m = 12
    start_m = (fye_m % 12) + 1               # month fiscal year begins
    months_in = (d.month - start_m) % 12
    q = months_in // 3 + 1
    return f"{d.year}_Q{q}"


# ---- HTML -> text -----------------------------------------------------------
class _TextExtractor(HTMLParser):
    """Strip tags to plain text, dropping <script>/<style>/<head> bodies and the
    inline-XBRL header (`<ix:header>` holds the hidden duplicate facts + context
    /unit definitions — without dropping it the extracted text begins with a wall
    of XBRL gibberish like `...us-gaap:CommonStockMember2025...` before the real
    report). Visible inline facts (`<ix:nonFraction>` etc. in the body) are
    outside the header, so their numbers still render."""
    _DROP = {"script", "style", "head", "ix:header"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._DROP:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in self._DROP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        t = "".join(self.parts)
        t = re.sub(r"[ \t\xa0]+", " ", t)
        t = re.sub(r"\n[ \t]+", "\n", t)
        t = re.sub(r"\n{3,}", "\n\n", t)
        return t.strip()


def html_to_text(html_bytes: bytes) -> str:
    try:
        html = html_bytes.decode("utf-8", "ignore")
        p = _TextExtractor()
        p.feed(html)
        return p.text()
    except Exception:
        return ""


TEXT_FAIL_MARKER = "*(Text extraction failed)*"


# ---- output paths + manifest ------------------------------------------------
def default_out_root() -> Path:
    """Default corpus root: <data_root>/edgar (Resilio-synced like other data),
    falling back to TradeHunter/data/edgar if config can't be read."""
    try:
        from _common import get_data_root          # type: ignore
        return Path(get_data_root()) / "edgar"
    except Exception:
        return SKILL_DIR / "data" / "edgar"


def manifest_path(root: Path) -> Path:
    return root / "_edgar_manifest.json"


def load_manifest(root: Path) -> dict:
    p = manifest_path(root)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_manifest(root: Path, m: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    manifest_path(root).write_text(
        json.dumps(m, indent=2, sort_keys=True), encoding="utf-8")


def write_report(root: Path, ticker: str, filing: Filing, label: str,
                 html_bytes: bytes, text: str) -> tuple[str, str]:
    """Write `<ticker>/<ticker>_<label>.html` + `.md` (Obsidian-friendly
    frontmatter + wikilink + full text). Returns (html_name, md_name)."""
    tdir = root / ticker
    tdir.mkdir(parents=True, exist_ok=True)
    html_name = f"{ticker}_{label}.html"
    md_name = f"{ticker}_{label}.md"
    (tdir / html_name).write_bytes(html_bytes)

    body = text if text else TEXT_FAIL_MARKER
    period = label.replace("_", "-")
    md = (
        "---\n"
        "type: financial-filing\n"
        f"ticker: {ticker}\n"
        f"period: {period}\n"
        f"document: {filing.form}\n"
        f"accession: {filing.accession}\n"
        f"period_end: {filing.report_date}\n"
        f"filed: {filing.filing_date}\n"
        "status: Seeded\n"
        "tags:\n"
        "  - equity-research\n"
        f"  - earnings-{filing.form.lower().replace('-', '')}\n"
        f"  - {ticker.lower()}\n"
        "---\n\n"
        f"# {ticker} - {period} {filing.form}\n"
        f"**Interactive Layout:** [[{html_name}]]  ·  "
        f"filed {filing.filing_date}  ·  period end {filing.report_date}\n\n"
        "## Summary & Key Metrics\n"
        "*(Hermes Agent: insert financial-metrics analysis here)*\n\n"
        "---\n\n"
        "## FULL REPORT CONTENT\n\n"
        f"{body}\n"
    )
    (tdir / md_name).write_text(md, encoding="utf-8")
    return html_name, md_name


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
