# Company Intelligence — Obsidian-backed analysis + per-metric alerts

**Status: DISCUSSION / not started.** No code written yet. This captures the
in-progress design so it can be resumed on any machine (cross-PC via git+Dropbox).
Last updated 2026-08-14.

## The vision (user)

- Link the system to **Obsidian**. Analyse each earnings report of a company and
  save the note + analysis into Obsidian as Markdown, so that **whenever analysis
  is made on a company, that Obsidian note is the canonical reference** (reused,
  not re-derived).
- Track per-quarter financial metrics and **alert when a metric is deteriorating**.
  Driving example: **NVDA free cash flow (FCF) per quarter → alert if FCF is
  depleting.** FCF is just one example metric; the mechanism must generalise.

## Decisions made so far (user, 2026-08-14)

1. **Next step:** keep discussing — design not finalised, don't build yet.
2. **Vault:** a **separate Obsidian vault** (not inside the EDGAR corpus). New
   config e.g. `TST_OBSIDIAN_DIR`.
3. **Numbers source:** source metrics from **free-tier market data** (NOT
   LLM-reading the filings, NOT a paid API).
4. **Alerts:** **in-app only** — a badge/notice on the Company page. No
   Telegram/Discord for now.
5. **Vault SITS ON THE SERVER, shared by ALL users** (user, 2026-08-14): it is NOT
   a personal laptop-Obsidian sync — it's a **shared platform knowledge base** that
   every member reads through the web app. So the vault lives in the server-side
   shared tree (`HermesSync`, reachable by both the Hermes dashboard and the Nous
   agent), and the Company page renders the note to all users. (The MD/Obsidian
   format keeps it portable + editable; someone with access to the folder can still
   open it in Obsidian directly, but the PRIMARY consumer is the web app.)
6. **LLM narrative = YES** (user, 2026-08-14): the per-company note includes an
   LLM-written "what changed this quarter" commentary — so the **Nous agent** (LLM +
   corpus access) is the writer, not the dashboard.

## Refined architecture (given those choices)

Because the numbers come from a free API and alerts are in-app, the metrics loop
can live **entirely in the dashboard** — no Nous-agent round-trip, no outbound
messaging. Much lighter than the first (agent-based) sketch.

### Metrics source — free, no key, no LLM

- **SEC EDGAR `companyfacts` API (recommended primary):**
  `https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json`. Free, **no API
  key**, official. It's the structured (XBRL) form of the same filings already in
  the corpus, so numbers are exact and consistent with the 10-Qs.
  - **FCF = Operating Cash Flow − CapEx**, both standard XBRL concepts
    (`NetCashProvidedByUsedInOperatingActivities` −
    `PaymentsToAcquirePropertyPlantAndEquipment`).
  - Ticker→CIK from the free `https://www.sec.gov/files/company_tickers.json`.
  - Constraints: fair-use rate limit (~10 req/s) + a declared `User-Agent` header.
- **yfinance / Yahoo (easy fallback):** already used in the app for prices;
  `quarterly_cashflow` exposes "Free Cash Flow" directly. Easier, but scraped →
  more fragile / occasionally gappy.
- Net: **no key, no paid tier, no LLM needed for the numbers.**

### Data model (portable ORM — Postgres-upgradeable, per the data-handling rule)

- `company_metrics(symbol, period, metric, value, unit, source, as_of)` — one row
  per (symbol, quarter, metric). Powers sparklines + alert evaluation.
- `metric_alerts(symbol, metric, rule, threshold, enabled)` — per-ticker rules,
  editable in the UI. Rule ideas: *declining N quarters in a row*, *YoY % drop >
  X*, *absolute threshold*, *turned negative*.

### Dashboard surfaces (per the dashboard-visibility rule)

- Company page **Metrics tab**: per-quarter sparkline per metric (FCF first) + an
  **in-app alert badge** when a rule trips. Rules editable inline.
- The metrics fetch is **cached** like prices are today.

### The Obsidian note (the durable artifact)

- One note per company, e.g. `<vault>/Companies/NVDA.md`: YAML frontmatter
  (ticker, sector, updated) + narrative + a **per-quarter metrics table** +
  `[[wikilinks]]` to each raw filing in the corpus.
- The Company page reads/renders this note (same mechanism as the Earnings-tab
  report viewer shipped in v3.72), so it's the canonical reference "reused
  whenever analysis is made."

## Finalised topology (both open questions now RESOLVED)

- **Vault:** a shared, server-side Obsidian vault in the `HermesSync` tree, e.g.
  `HermesSync/Vault/Companies/<TICKER>.md`. Config `TST_OBSIDIAN_DIR` (on Hermes ->
  `C:\HermesSync\Vault`). Reachable by the Nous agent (writes it, via cifs
  `/mnt/hermes_sync`) AND the Hermes dashboard (reads it, serves to all members).
- **Writer = Nous agent.** Per new filing: (1) pull free metrics (SEC companyfacts
  / yfinance), (2) generate the LLM "what changed" narrative, (3) write/update
  `Companies/<TICKER>.md` (frontmatter + narrative + per-quarter metrics table +
  `[[wikilinks]]` to the corpus filings), (4) POST structured metrics to the
  dashboard (`/api/company-metrics`).
- **Reader = the web app, for ALL users.** The Company page renders the note (like
  the v3.72/73 report viewer) + a Metrics tab (FCF sparkline + in-app alert badge).
- **Access model:** view = all members (shared knowledge base); edit = moderators
  (matches the existing company_analysis role model) or agent-authored. NOT
  per-user — one shared note per company.

### Remaining smaller decisions (not blockers)

1. **Exact vault path** under HermesSync (`HermesSync/Vault` vs a subfolder of an
   existing vault) + confirm the Nous agent's cifs mount can write there.
2. **Which LLM** for the narrative (reuse the agent's existing model; DeepSeek is
   already wired for Research). Keep the narrative short + cite the filing.
3. **Metric set v1** (start: revenue, operating cash flow, FCF, net income,
   gross/operating margin, cash, debt, shares out, EPS) + alert rules v1
   (declining-N-quarters, YoY-drop-%, turned-negative).

## Suggested phased rollout (when we proceed)

1. Free-API metrics fetch → `company_metrics` → Company-page **Metrics tab**
   (FCF sparkline) + in-app alert badge. (No Obsidian, no LLM — fastest value.)
2. Write/update the per-company Obsidian note (metrics table + links) to the
   synced vault; dashboard renders it.
3. Optional LLM narrative in the note; more metrics + richer alert rules.

## Reuse / prior art in the repo

- Earnings-tab report reader (v3.72): `services/edgar_reports.py`,
  `_edgar_report.html`, `GET /company-analysis/{symbol}/report`,
  `config.edgar_dir` — the note renderer can mirror this.
- Live price fetch + caching pattern: `services/prices.py` (Yahoo via httpx).
- Pushed-report/inventory pattern: `services/edgar_health.py`,
  `EdgarIngestHealth`, `/api/ingest/edgar`.
