# Macro study — design

Turning each `/macro` topic from a written snapshot into a **continuous study**: a
tracked trend, framed outcomes, stated implications, and mechanical monitoring
that tells you when the story changes.

Status: **DESIGN** (2026-08-16). Phase 1 of the board itself shipped in
dashboard_tst v3.94 (two-pane, six canonical topics, live cross-asset strip,
moderator/agent-written analysis). This doc covers what comes next.

Companion docs: `COMPANY_ANALYSIS_DESIGN.md` (same provenance/confidence pattern,
per-ticker), `COMPANY_INTELLIGENCE_DESIGN.md` (in-app alerting decision),
`../strategy/patterns/DETECTOR_DESIGN.md` (the no-lookahead + measured-outcome
discipline this borrows).

## The problem this solves

A "continuous study" decays into a weekly essay unless something **mechanical**
anchors it. If the agent re-reads the world each week and re-decides what it
thinks, the result is drift dressed as analysis — fluent, plausible, and
untethered from what it said last month. Worse, an LLM asked for a probability
will produce one, and a confident "65%" rendered on a dashboard reads as a
measurement when it is an invention.

Two rules follow, and everything below is downstream of them:

1. **Scenarios carry NUMERIC trigger conditions tied to tracked indicators.**
   Not *"if inflation stays sticky"* but *"core PCE YoY > 3.0% for two
   consecutive prints"*. Once the condition is a number, the system evaluates it
   — that is the whole difference between monitoring and re-reading.
2. **Probabilities are MEASURED from history or not shown.** See "Episode
   studies" below. This is the same refusal as the Intrinsic Value page
   declining to fake GuruFocus's undisclosed OracleValue™.

## The loop

```
Track ──▶ Read ──▶ Frame ──▶ Imply ──▶ Watch ──▶ Review ──┐
  ▲    (indicators) (trend)  (scenarios) (exposure) (triggers) (snapshot)
  └──────────────────────────────────────────────────────────┘

Track  · mechanical — named indicators, stored as a series
Read   · trend vs 3m / 12m, never a bare snapshot
Frame  · judgement — 2-4 scenarios, each with trigger levels
Imply  · judgement — exposure, sector tilt, what to avoid
Watch  · mechanical — evaluate triggers against new readings
Review · judgement — snapshot the stance so drift stays visible
```

Teal/mechanical steps (Track, Watch) are deterministic code and are the parts
decisions should hang off. Purple/judgement steps (Frame, Imply, Review) are
written by a moderator or the agent. **A bad model week should degrade the
reading experience, not the signal.**

## Decisions locked

| # | Decision | Choice |
|---|---|---|
| 1 | Left-rail taxonomy | **Fixed six topics** (`models.MACRO_SECTIONS`), not user-created. Confirmed with user 2026-08-16. Free-form research stays on `/research?kind=macro`. |
| 2 | Scenario probabilities | **Measured base rates from historical episodes, or ordinal bands (likely / possible / tail).** Never an LLM-asserted percentage. |
| 3 | Trigger conditions | **Numeric, against a tracked indicator.** A scenario without a machine-checkable trigger is a note, not a scenario. |
| 4 | Indicator series storage | **App database**, not parquet — small (~75k rows total), queried constantly by the web app, must stay Postgres-portable. |
| 5 | Live vs historical data path | **Two paths, same metric** — see below. |
| 6 | Alerts | **In-app only**, consistent with `COMPANY_INTELLIGENCE_DESIGN.md`. |
| 7 | Computed values | **Never stored as analysis.** Live tiles derive on render; the agent push API cannot set them (already true in v3.94). |

### Decision 5 — the two-path rule (important, will otherwise be re-litigated)

The same metric resolves differently depending on the question being asked:

- **"What is breadth today?"** → a live/operational view → **fetch live**
  (`resources/yf_daily_bars`, in-memory, no disk). Never parquet.
- **"What was breadth across 2015-2026?"** → offline historical analysis →
  **parquet is the correct source**, per the carve-out in `CLAUDE.md`.

This is not a loophole; it is exactly the distinction the carve-out draws
("does it show *the present*? → live. Does it study *stored history*? →
parquet"). Write it down here so the next session doesn't re-argue it.

## Data model

Three tables, mirroring patterns already proven in this codebase.

**`MacroIndicator`** — the definition (what we track, where it comes from):
`key` (e.g. `t10y2y`), `section`, `label`, `source` (`fred|yahoo|computed`),
`source_ref` (FRED series id / Yahoo symbol), `unit`, `transform`
(`level|yoy|mom|spread`), `higher_is` (`risk_on|risk_off|neutral`), `active`.

**`MacroReading`** — the series (the trend layer):
`indicator_key` (idx), `as_of` (idx), `value`, `vintage` (nullable — the
as-published date for revisable macro data; null = final/unrevised).
Unique `(indicator_key, as_of, vintage)`. **This is the `MATPHistory` pattern.**

**`MacroScenario`** — the thinking layer:
`section`, `name`, `stance` (`base|alt|tail`), `narrative`, `triggers` (JSON:
`[{indicator, op, value, consecutive}]`), `implications` (JSON: exposure,
sector tilt, avoid), `base_rate` (JSON: measured — `{n_episodes, hit, median,
worst, window}`), `status` (`active|fired|invalidated`), `as_of`, `updated_by`.

**`MacroSnapshot`** — the memory layer: `section`, `stance`, `body`, `as_of`.
One row per review, so "we were bearish in June, bullish in August" is visible.
Without this the study has no memory and no accountability.

`MacroAnalysis` (shipped v3.94) stays as the free-text commentary per section.

## Sourcing

| Source | Covers | Key | History | In repo |
|---|---|---|---|---|
| **Yahoo** | ^VIX, ^GSPC, ^TNX, ^FVX, ^IRX, DX-Y.NYB, TLT, HYG, LQD, GLD, USO | no | decades daily | ✅ `services/prices.py`, `resources/yf_daily_bars.py` |
| **FRED** | CPI, core PCE, payrolls, unemployment, curve, breakevens, HY spreads, balance sheet, TGA, RRP | free key | decades **+ vintages via ALFRED** | ❌ new adapter |
| **Parquet store** | breadth (computed, historical only) | n/a | as deep as the store | ✅ `resources/bars_store.py` |
| **CFTC COT** | futures positioning, weekly | no | decades | ❌ new adapter (later) |

Config: `TST_FRED_API_KEY` in `app/.env` (gitignored, per-PC), same convention
as `TST_SEC_USER_AGENT`. Same etiquette as the SEC adapter: descriptive
User-Agent, cached, soft-fail.

**Known gaps — state them in the UI, don't paper over them:**
- **Fed policy expectations (implied cut count).** CME FedWatch has no free
  historical download. Proxy with the **2y yield** (`DGS2`) and **label it a
  proxy**. Don't scrape FedWatch: brittle and arguably against their terms.
- **Consensus estimates** alongside a release aren't free. FRED's releases API
  gives scheduled *dates* (enough for a "what's due this week" tile); the
  estimate stays with the agent's web research, which the pre-market briefing
  already does.

## Proposed indicator set (PROPOSED — user to edit)

3-5 per topic. **Every FRED id below must be verified against fred.stlouisfed.org
when wiring** — these are from recall, not lookup.

| Section | Indicators |
|---|---|
| Monetary policy & rates | `DGS2` (policy-path proxy), `T10Y2Y` (curve), `DFII10` (10y real), `T10YIE` (breakeven) |
| Growth & inflation | `CPIAUCSL` (YoY), `PCEPILFE` (YoY), `PAYEMS` (MoM), `UNRATE`, `INDPRO` |
| Market internals | `^VIX`, breadth % > 200DMA (computed), `BAMLH0A0HYM2` (HY OAS), SPX vs 200DMA |
| Cross-asset | `DX-Y.NYB`, `^TNX`, `GLD`, `USO`, HYG/LQD ratio (derived) |
| Global & geopolitical | EEM/SPY relative, USDCNY — **thin by nature**; this topic stays mostly narrative and should admit that |
| Liquidity & positioning | `WALCL`, `RRPONTSYD`, `WTREGEN`, net liquidity (derived: WALCL − RRP − TGA) |

ISM is deliberately absent: the headline series is licence-restricted and not
reliably free from FRED. `INDPRO` is the robust free substitute.

## Episode studies — where probabilities actually come from

Define a regime as **indicator conditions**, scan history for matching windows,
measure what happened next:

> Condition: `T10Y2Y < 0` AND `^VIX < 20` AND breadth falling
> Matches: 11 non-overlapping episodes since 1990
> Forward 3m SPX: higher 7/11, median +1.4%, worst −18%

That is a measured base rate. The scenario's likelihood becomes evidence you can
point at, and the **dispersion is the position-sizing argument** — the −18% tail
is more decision-relevant than the median.

### Methodology traps to design against

1. **Overlapping windows are not independent observations.** Fourteen matching
   months inside one 2008-style episode is *one* observation. Cluster by episode,
   report `n`, and treat a base rate from 4 episodes as a hint, not a probability.
2. **Macro data is revised.** Building a signal on final-revised prints is
   lookahead — the macro version of the pivot-confirmation leak in
   `DETECTOR_DESIGN.md`. Use ALFRED vintages, or state plainly that pre-revision
   timing is approximate. The `vintage` column exists for this.
3. **The parquet universe is today's constituents.** Historical breadth computed
   on today's members carries survivorship bias — the failures aren't in the
   file. Not fixable with free data; note it, and prefer breadth measures less
   sensitive to composition.
4. **Regimes change.** A 1970s inflation analogue may say nothing about 2026.
   The sample window must be an explicit, visible choice — never buried in code.

## Phasing

- **Phase A — indicators + history.** FRED adapter, backfill the market-derived
  and FRED series, `MacroReading` store, sparkline + "vs 3m/12m" per topic.
  Entirely mechanical, no agent dependency, ships with **real history on day
  one** rather than an empty chart. Start with market-derived series only: free,
  deep, unrevised, survivorship-free — every trap above avoided in v1.
- **Phase B — scenarios + implications.** `MacroScenario`, moderator-written
  first (as Company Analysis Phase 1 did), agent-assisted later.
- **Phase C — episode scanner.** Regime matcher over the stored series →
  measured base rates attached to scenarios.
- **Phase D — trigger evaluation + in-app alerts.** Fires when a condition is met
  or a thesis is contradicted. The pure-function evaluator is small and testable;
  it must not be an LLM.
- **Phase E — breadth history** (parquet batch) and **CFTC positioning**.

## Open decisions

1. **Who owns the thesis** — moderator writes and the agent only feeds data, or
   the agent drafts and a moderator approves? Changes the trust model more than
   any other choice here. *(asked 2026-08-16, unanswered)*
2. **Indicator set** — accept the proposal above, or user-specified? It's a
   trading judgement, so the user's call. *(asked, unanswered)*
3. **Review cadence** — weekly per topic, or event-driven (a print lands, a
   trigger fires)? Event-driven is cheaper and more meaningful; weekly is more
   predictable.
4. **Sample window default** for episode studies — 1990+ (covers several cycles,
   avoids the structurally different 1970s-80s) is the suggested default.

## Changelog
- 2026-08-16 — initial design. Written after the user asked for continuous study
  + trend monitoring + outcomes/implications per macro topic, and then for the
  data sourcing. Records the two-path parquet/live rule and the
  measured-not-asserted probability decision before any code is written.
