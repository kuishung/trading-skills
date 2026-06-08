# EDGAR Seeder

**Role (one line):** fetch SEC earnings filings (10-Q / 10-K) into a versioned,
Obsidian-friendly corpus, with real incremental "fetch only what's new" and a
consistency auditor for what's already been seeded.

This is the in-repo, hardened successor to the original `~/hermes_tools/fetch_edgar.py`
prototype. It is **stdlib-only** (no `sec-edgar-downloader`, no `requests`) and
talks to SEC's official REST/JSON API directly, which fixes the prototype's bugs
(everything-defaults-to-Q1 filenames, fake User-Agent, no incremental, calendar-
only quarters) and removes the pip dependency.

## Contents

| File | What it does |
|---|---|
| `edgar_seeder.py` | CLI: `seed` (bulk historical), `update` (incremental — only new filings), `tickers` (resolve ticker→CIK). |
| `check_edgar_integrity.py` | Read-only consistency audit of the seeded corpus (missing/empty/orphan files, label collisions, **missing 10-Q quarters**, stale tickers). Writes `_edgar_health.json` with `--json`. |
| `_edgar_common.py` | Shared SEC client: rate-limited HTTP (≤10 req/s + backoff), real-User-Agent enforcement, ticker→CIK map (7-day cache), submissions parsing, **fiscal-quarter labelling via `fiscalYearEnd`**, HTML→text, manifest + Markdown writers. |

## How it works

1. **Contact (mandatory).** SEC throttles/403s fake identities, so the seeder
   refuses to run without a real `company + routable email`. Resolution order:
   `--email/--company` → `EDGAR_EMAIL`/`EDGAR_COMPANY` env → `config.json`
   `"edgar": {"email","company"}` block. A `.local` / no-`@` address is rejected.
2. **Discovery.** `ticker → CIK` from `company_tickers.json` (cached), then
   `data.sec.gov/submissions/CIK<n>.json` (+ paginated overflow shards) gives exact
   `form / accessionNumber / filingDate / reportDate / primaryDocument` — no SGML
   scraping.
3. **Fiscal labels.** `fiscalYearEnd` (MMDD) → true fiscal quarter, so non-calendar
   filers (NVDA, AAPL, …) come out right. Label = `<periodEndYear>_Q1..3` for 10-Q,
   `<year>_FY` for 10-K. Restated/duplicate periods get an accession-tail suffix
   instead of silently overwriting.
4. **Output.** Per ticker: `<TICKER>_<label>.html` (raw primary doc) +
   `<TICKER>_<label>.md` (YAML frontmatter + `[[wikilink]]` + extracted full text).
   A `_edgar_manifest.json` at the corpus root records every seeded accession →
   the source of truth for incremental updates and the integrity check.
5. **Incremental.** `update` pulls only filings newer than each ticker's latest
   seeded `filing_date` (and never re-downloads a known accession).

Corpus root defaults to **`<data_root>/edgar/`** (Resilio-synced like other data;
see CLAUDE.md). Override with `--out` (e.g. point it at an Obsidian vault).

## Usage

```bash
# (run on any PC — no IBKR, no py-3.12 requirement; stdlib only)

# sanity-check ticker resolution
py "resources/EDGAR Seeder/edgar_seeder.py" tickers AAPL NVDA BRK.B

# one-time historical seed (default since 2011)
py "resources/EDGAR Seeder/edgar_seeder.py" seed --tickers AAPL MSFT NVDA

# incremental "get new earnings" for a watchlist (only fetches what's new)
py "resources/EDGAR Seeder/edgar_seeder.py" update --tickers-file resources/universe_full.txt

# update everything already in the corpus
py "resources/EDGAR Seeder/edgar_seeder.py" update

# audit the seeded corpus (and emit machine-readable health for the dashboard)
py "resources/EDGAR Seeder/check_edgar_integrity.py" --json
```

Set the contact once in `config.json` (gitignored, per-PC) so you don't pass it
every run:

```jsonc
"edgar": { "email": "you@yourdomain.com", "company": "Your Name / Firm" }
```

## Notes / tradeoffs

- **Markdown carries the full report text** (hundreds of KB each) — by design, for
  Obsidian/agent search. Budget ~several MB per ticker across a full 2011→now seed.
- Quarter labels are anchored on the **period-end calendar year**; the exact
  `period_end` is in each `.md`'s frontmatter, so there's never ambiguity even when
  a fiscal-year label would differ (e.g. NVDA FY25-Q1 ends Apr-2024 → `2024_Q1`).
- The integrity check's "missing quarter" logic expects **Q1–Q3 10-Qs per fiscal
  year** (Q4 is the 10-K, never a 10-Q) and treats the first/last seeded years as
  partial to avoid false positives around IPOs and not-yet-filed quarters.
- Rate limited to <10 req/s with exponential backoff on 429/5xx — safe to run over
  large universes unattended.

## Changelog

### 2026-06-09 — initial in-repo version (v1.0.0)
- Created the folder as the hardened, in-repo successor to the standalone
  `~/hermes_tools/fetch_edgar.py` prototype shared by the user.
- `_edgar_common.py`: stdlib SEC client — real-User-Agent enforcement (rejects
  fake/.local), ≤10 req/s + backoff, `company_tickers.json` CIK map (7-day cache),
  submissions + overflow-shard parsing, fiscal-quarter labelling via `fiscalYearEnd`,
  `<script>/<style>`-aware HTML→text, manifest + Obsidian-Markdown writers, output
  root = `<data_root>/edgar` via `_common.get_data_root()`.
- `edgar_seeder.py`: `seed` / `update` / `tickers` subcommands; per-ticker
  incremental cutoff from the manifest; crash-safe (manifest saved after each
  ticker); restatement-collision suffixing; clear per-filing progress.
- `check_edgar_integrity.py`: corpus audit — missing/empty/orphan files, text-
  extraction failures, missing frontmatter, label collisions, **missing 10-Q
  quarter** detection (partial-year aware), staleness; `--json` writes
  `_edgar_health.json` for a future dashboard pill.
- Wired the SEC contact into `config.example.json` (`edgar` block) so the structure
  travels cross-PC; `config.json` holds the real per-PC values.
- Fixes vs the prototype: no Q1-default filename bug (exact dates from the API),
  no fake `compliance@firm.local` User-Agent, true incremental fetch, correct
  fiscal quarters, and a consistency auditor that the prototype lacked.
- Smoke-tested live against SEC: NVDA seed (10 filings, correct FY/Q labels,
  354 KB+ extracted each), check = CLEAN/no gaps, second `update` fetched 0 new.
