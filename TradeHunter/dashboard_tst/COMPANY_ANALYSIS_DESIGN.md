# Company Analysis — design

A structured, per-ticker **company dossier** in TradeHunter (`dashboard_tst`).
Distinct from the existing free-form **Company** research page (`/research?kind=company`,
an LLM chat workspace) — this is a *fixed-section, reusable, sourced* analysis of a
single ticker.

Nav: **Investing → Company Analysis** (`/company-analysis`). Route module
`app/routes/company_analysis.py`, guarded by `require_menu("company_analysis")`.

## Sections

1. **Business Model** — how the company makes money.
2. **Business Segment** — revenue by segment/geography.
3. **Competitive Analysis** — moat/positioning narrative **+ an explicit competitor list**.
4. **Suppliers** — the value chain, **tiered + confidence-labeled** (see below).
5. **Key Metrics (KPI)** — an **industry scorecard**: the ticker vs its industry peers.

## The layering (why sections aren't one thing)

| Layer | Sections | Generator | Cadence |
|---|---|---|---|
| Qualitative research | Business Model, Segment, Competitive, Suppliers | Nous agent (LLM + EDGAR) *or* moderator edit | quarterly / on-demand |
| Quantitative | Key Metrics (KPI) | Finviz industry screen (agent) + live quote | daily–weekly |
| Meta / provenance | (cross-cutting) sources + confidence | every section emits its own | accumulates |

Everything is **primary-sourced where possible** and carries provenance. Qualitative
prose is grounded in filings (cited) or clearly flagged as industry inference.

## Sourcing per section

- **Business Model** ← 10-K **Item 1 (Business)** from the EDGAR corpus (`EDGAR Seeder`
  → `QuarterlyReport` on AI-Hermes, read by the Nous agent). Agent reads → writes →
  cites the accession #.
- **Business Segment** ← 10-K **segment footnote (ASC 280)** + **Item 7 MD&A** →
  revenue-by-segment table.
- **Competitive Analysis** ← 10-K competition subsection (named competitors) **+** the
  **industry peer set** from a Finviz industry screen (the agent already runs Finviz for
  MATP). Competitor list carries tickers for click-through.
- **Suppliers** ← 10-K Item 1 (supply/manufacturing) + Item 1A (supplier concentration
  risk) for **tier-1**; **industry knowledge (LLM)** for **tier-2+**, news-checked.
- **Key Metrics (KPI)** ← Finviz industry screen (peers) → median/quartiles/percentile
  per metric; the ticker's own metrics from the live quote / financial data.

## Suppliers = tiered value chain (industry-knowledge)

The core decision (user, 2026): the supplier map is **industry-knowledge-driven**, so the
agent maps the *deep* chain, not only what a filing literally names. To stay honest:

- **Tier-1 (direct)** — filing-cited, high confidence.
- **Tier-2+ (inferred)** — the agent's industry reasoning (e.g. `NVDA → TSMC → ASML DUV/EUV`),
  **medium/low confidence**, labeled "industry inference", cross-checked against recent news
  for staleness. NEVER presented as a filing fact.

Example — **NVDA** (fabless-semiconductor template):

| Layer | Vendor | Tier | Source |
|---|---|---|---|
| EDA | Synopsys, Cadence | 1–2 | industry |
| Foundry | TSMC | 1 | filing |
| Lithography | ASML (DUV/EUV) | 2 | industry |
| Dep/etch | Applied Materials, Lam, TEL | 2 | industry |
| Memory | SK Hynix, Micron, Samsung (HBM) | 1 | filing/reported |
| Packaging | TSMC CoWoS | 1 | industry |
| Substrates | Ibiden, Unimicron | 2 | industry |

Caveat recorded in-product: a *complete* multi-tier map with % of spend needs paid
supply-chain data (Bloomberg SPLC / FactSet). Free sources give tier-1 solidly and
tier-2 as reasoned inference.

## The generalizable pipeline (one method, every ticker)

```
classify industry → apply that industry's supply-chain / KPI template
   → fill each layer with the company's vendors/peers (filings + industry knowledge + news)
   → label provenance + confidence per node → push to the platform
```

- **One reasoning pipeline** (agent prompt/method) + **reusable per-industry templates**
  (semi, bank, SaaS, retail, REIT, …) covers the whole universe — no per-ticker hand-crafting.
- The same "what industry / who are the peers" backbone drives **Suppliers, the Competitor
  list, and the industry-KPI benchmark**.
- **Reliability is not uniform:** deep + reliable for well-documented names/industries,
  degrades for obscure ones. Mitigations: per-node confidence, news cross-check, a per-ticker
  **coverage-confidence** signal, and a "limited public/industry data" fallback instead of
  fabricating.

## Data model (ORM, Postgres-ready)

`CompanyAnalysis` — one row per (symbol, section):
- `symbol` (idx), `section` (business_model | segment | competitive | suppliers | kpi)
- `body` (Text — long-form prose), `content` (JSON — structured: tables, lists, tiers)
- `sources` (JSON — `[{title,url,accession,kind}]`)
- `source_kind` (manual | agent | feed), `confidence` (high|medium|low), `industry` (str)
- `as_of`, `updated_by`
- unique (symbol, section)

(Later) `CompanyAnalysisHistory` for dated snapshots (thesis evolution, like MATPHistory).

## Generation + editing

- **Agent-generated** (Phase 2): mirror the MATP refresh queue — request analysis →
  enqueue → agent reads EDGAR + runs the industry screen + applies templates → pushes via
  `POST /api/company-analysis/{symbol}/{section}` (X-API-Key). Refreshes per-section on its
  own cadence.
- **Moderator-edited** (Phase 1): moderators can write/paste each qualitative section
  directly, so the page is useful **before** the agent is back. (The Nous agent was down
  from 2026-08-06 to 2026-08-16 — a disk-full cleanup deleted uv's managed Python out from
  under the agent's venv. **Fixed 2026-08-16**; root cause + runbook in
  `nous_hermes/README.md` → "Troubleshooting".)
- **KPI** (Phase 1): the ticker's own metrics + industry label render immediately from live
  data; the **peer benchmark** (percentile/median) is agent-computed (Phase 2).

## UI

- `/company-analysis` — ticker search box (reuses MATP's ticker-search) + recently-viewed.
- `/company-analysis/{symbol}` — the dossier: 5 collapsible section cards, each with
  `as_of` + source/confidence badges + (moderators) an edit/refresh control.
- Linked from MATP rows (ticker → its analysis).
- Scrollbars invisible-until-hover (inherited from `base.html`).

## Access & framing

- **View** = approved members with the `company_analysis` menu; **edit/generate** = moderators.
- The KPI/valuation content is *analysis, not advice* — scenarios/inputs with sources, never
  "buy/sell". (Not licensed advice.)

## Phasing

- **Phase 1 (now):** nav + route + `CompanyAnalysis` model + dossier page + KPI (own metrics +
  industry label, live) + moderator-editable qualitative sections + the agent-push API
  endpoint (ready for later) + provenance/confidence UI.
- **Phase 2 (when the Nous agent is back):** agent auto-generates the qualitative sections
  from EDGAR, the industry KPI benchmark from the peer screen, and the tiered supplier chain;
  per-industry template library; coverage-confidence signal; history snapshots.

## Changelog
- 2026-07-18 — initial design (sections, tiered industry-knowledge suppliers, generalizable
  industry-template pipeline, phasing). Agreed with user before scaffolding Phase 1.
