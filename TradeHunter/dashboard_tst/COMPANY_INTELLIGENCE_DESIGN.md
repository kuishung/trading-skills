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
3. **Metric set + alert rules + LLM checklist** — now PINNED, see
   "Analyst framework" below.

## Analyst framework → metric set, alerts, LLM checklist (pinned 2026-08-15)

What a financial analyst studies in an earnings report, mapped to how the system
handles it. The split is the architecture: **quantitative → structured data +
alerts (no LLM); qualitative → LLM narrative.**

### Data sourcing (structured, free)

- **Primary: SEC EDGAR XBRL `companyfacts` API** — `data.sec.gov/api/xbrl/
  companyfacts/CIK##########.json`. Free, no key. Every XBRL-tagged line item from
  the 10-K/10-Q (same filings as the corpus). Coverage strong since ~2009.
- **Pull RAW line items and compute ratios ourselves** (exact, free, no rate cap,
  formula documented) rather than trusting a provider's pre-computed ratios.
- Fallbacks: **yfinance** (has FCF directly, scraped/fragile), **FMP** free tier
  (pre-computed ratios, ~250 calls/day cap).
- **NOT in XBRL → LLM/press-release:** non-GAAP/"adjusted" figures (in the 8-K
  earnings release, not the 10-Q), guidance, MD&A narrative, risk-factor changes.

### v1 metric set (structured; base line items + derived ratios)

Base (from XBRL): revenue, operating cash flow (OCF), capex, net income, gross
profit, operating income, cash & equivalents, total debt, diluted shares
outstanding, diluted EPS, inventory, receivables, EBITDA, interest expense.

Derived (computed): **FCF = OCF − capex**, FCF margin, **FCF conversion = FCF/NI**,
gross/operating/net margin, revenue growth YoY & QoQ, **OCF-vs-NI gap** (earnings
quality), net debt, **net debt/EBITDA**, **interest coverage**, **DSO** (receivables
days), **DIO** (inventory days), YoY share-count change (dilution).

### v1 alert rules (in-app badge)

- **FCF depleting** — declining N quarters in a row (the driving example), or FCF
  turned negative, or FCF conversion < threshold.
- **Earnings quality** — OCF < net income (cash not backing profit).
- **Margin compression** — gross/operating margin down YoY > X pp.
- **Growth deceleration** — revenue growth YoY falling N quarters.
- **Leverage stress** — net debt/EBITDA rising above threshold, or interest
  coverage falling below.
- **Dilution** — diluted share count up > X% YoY.
- **Working-capital drag** — receivables or inventory growing faster than revenue.
All rules per-ticker, editable in the UI; thresholds have sensible defaults.

### LLM narrative — section checklist (qualitative only)

The agent reads only the parts that carry the "why" (not the whole multi-MB
filing): **MD&A**, the **cash-flow & income statements** (to explain the metric
deltas), **balance-sheet notes**, **segment/geographic notes**, **forward
guidance**, **GAAP↔non-GAAP reconciliation**, and **changed risk factors**. Output:
a short "what changed this quarter and why it matters," explicitly flagging the
qualitative red flags the numbers can't show (e.g. "FCF fell because capex doubled
for new fabs; management guided capex higher again"). Cite the filing/period.

### Red flags the system watches (numeric = alert; qualitative = LLM)

Declining FCF while net income rises · OCF diverging below NI · widening
GAAP↔non-GAAP gap · recurring "one-time" charges · rising share count ·
inventory/receivables outrunning sales · deteriorating interest coverage ·
guidance cuts / kitchen-sink quarters.

## Suggested phased rollout (when we proceed)

1. Free-API metrics fetch → `company_metrics` → Company-page **Metrics tab**
   (FCF sparkline) + in-app alert badge. (No Obsidian, no LLM — fastest value.)
2. Write/update the per-company Obsidian note (metrics table + links) to the
   synced vault; dashboard renders it.
3. Optional LLM narrative in the note; more metrics + richer alert rules.

## SCOPE UPDATE (user, 2026-08-15): build the FULL GuruFocus-style quant suite

The user chose to **replicate the full financial suite in-app** (Financials trend
charts + Profitability / Debt-&-Liquidity / Efficiency / Financial / Price ratio
tables with per-year + Current + 5Y/10Y-Avg columns), on top of (not instead of)
the qualitative earnings/guidance layer. Honest limits flagged + accepted: a few
tier-3 items can't be exactly reproduced from free data — **Forward PE / PEG**
(need analyst estimates), **"PE/PEG without NRI"** (GuruFocus-proprietary NRI
adjustment), a precise **WACC** (assumption-based), **historical share float**
(paid). These are computed with a documented free method where possible, else
marked n/a — never faked.

### Build log

- **Phase A — SEC XBRL data layer: DONE + verified.** `app/config.py::sec_user_agent`
  (SEC requires a UA) + `app/services/sec_xbrl.py`: ticker→CIK, companyfacts fetch
  (12h cache), candidate-tag fallbacks, `annual_financials(symbol)` → per-fiscal-year
  base line items + derived ratios. **Fiscal year keyed by period-END year** (not the
  XBRL `fy` tag, which is offset for Jan-ending years like NVDA). Return/efficiency
  ratios use **average balances** (begin+end)/2. Verified vs GuruFocus on NVDA:
  **margins, ROE, ROA match exactly**; revenue/NI correct; CCC within ~5–8 days
  (day-count convention — minor, to refine).
- **Next — Phase B:** the two Trend-Chart sections (10 metrics + margins + returns;
  Line/Bar · Annual/Quarterly · TTM) as a Financials tab. Then Phase C ratio tables,
  Phase D price/market ratios. Quarterly + TTM (needs Q4 = FY − 9mo derivation) and
  5Y/10Y-avg columns are follow-ups in the data layer.

## GUIDANCE layer -- findings from the 2026-09-05 spike

User asked for TradeHunter to *get guidance*, save it as .md, and build an Obsidian
knowledge base of earnings + guidance per ticker. Universe chosen: **top 50 tickers
per sector** (11 sectors ~= 550 names). Note layout chosen: **company hub note +
per-quarter notes**.

### Established by measurement, not assumption

1. **The corpus contains NO guidance.** The 737-ticker EDGAR corpus is 10-Q/10-K
   only. NVDA 2026-Q1 (135 KB) has zero guidance sentences. Companies never put
   forward guidance in a 10-Q, so guidance cannot be mined from what we store.
2. **Guidance IS free and official** -- SEC **8-K item 2.02**, press-release
   exhibit. Verified live on NVDA: revenue $108.0B +/-2%, gross margin 74.0%
   +/-50bps, opex ~$9.2B GAAP / $9.0B non-GAAP, FY27 tax 16-18%. No API key.
3. **Exhibits are NOT predictably named** -- NVDA files `q2fy27pr.htm`, HD files
   `hd_exhibit991x08022026.htm`. Filename matching misses them; rank every HTML
   document in the filing and let the extractor decide which carries guidance.

### Why deterministic extraction is NOT sufficient (measured, not assumed)

A regex extractor over a 15-ticker cross-sector sample scored **7/15 with poor
precision**: it matched NVDA's operating-expense RESULTS table instead of its
outlook, and read WMT's "Revenue growth of 5.9%" (an actual) as guidance.

The cause is structural, not tuning: **guidance and results sit side by side in the
same document, in the same prose and table forms.** "Revenue grew 5.9%" and "we
expect revenue growth of 5.9%" are lexically near-identical. Separating them needs
semantics.

This does NOT contradict the doc's "quant -> deterministic, qual -> LLM" split.
That split assumed **XBRL-tagged** numbers. Guidance is not XBRL-tagged -- it is
free prose in an exhibit -- so it falls on the qualitative side of the same line.

### Recommended architecture

- **Deterministic** (`services/guidance.py`, built and working): rate-limited SEC
  client, ticker->CIK, item-2.02 discovery, exhibit ranking, HTML->text, and
  locating the guidance region. Free, no key, no LLM.
- **LLM** parses that located region into structured rows: metric, fiscal period,
  low / high / midpoint, GAAP vs non-GAAP basis. Store the structured numbers AND
  the verbatim sentence + filing URL so every figure is auditable to its source.
- The LLM must never produce a number absent from the quoted sentence.

### Open decision before building further

Which LLM and where it runs: the **Nous agent** (per this doc's writer=agent
decision; DeepSeek already wired for Research) versus an LLM call from the Hermes
dashboard. This gates the backfill for ~550 tickers x ~40 quarters.

## Reuse / prior art in the repo

- Earnings-tab report reader (v3.72): `services/edgar_reports.py`,
  `_edgar_report.html`, `GET /company-analysis/{symbol}/report`,
  `config.edgar_dir` — the note renderer can mirror this.
- Live price fetch + caching pattern: `services/prices.py` (Yahoo via httpx).
- Pushed-report/inventory pattern: `services/edgar_health.py`,
  `EdgarIngestHealth`, `/api/ingest/edgar`.
