---
name: guidance
version: 1.0.0
description: Capture company forward GUIDANCE from SEC 8-K earnings releases into TradeHunter and the shared Obsidian vault. Use when asked to "refresh guidance", "run guidance", "update guidance notes", or on the scheduled cron. For each ticker it finds the latest 8-K tagged item 2.02, reads the press-release exhibit, separates FORWARD GUIDANCE from reported RESULTS, writes a per-quarter note plus a company hub note into the vault, and POSTs the structured figures to TradeHunter's /api/guidance. Every figure must be quoted from the filing -- never inferred, never estimated.
---

# Guidance — forward outlook from 8-K earnings releases

The LLM half of TradeHunter's company knowledge base. The platform stores and
displays; this skill does the reading and judgement.

## Why this needs an agent (and not a parser)

Guidance is **not** in XBRL and **not** in the 10-Q corpus. It is prose in the
8-K item-2.02 press release, printed next to the quarter's actual results in the
same tables and the same sentence shapes:

- "Revenue **was** $89.0 billion, up 18% from the previous quarter"  ← RESULT
- "Revenue **is expected to be** $108.0 billion, plus or minus 2%"  ← GUIDANCE

A regex extractor was measured at **7/15 with false positives**, grabbing NVDA's
operating-expense results table instead of its outlook and reading WMT's actual
revenue growth as guidance. Telling these apart is a reading task. That is the
whole reason this skill exists.

## Config — READ FROM `~/.hermes/.env` (do NOT ask the user)

```bash
grep -E '^(TRADEHUNTER_URL|TST_INGEST_API_KEY)=' ~/.hermes/.env
```

- `TRADEHUNTER_URL` — e.g. `https://app.tradehunter.net`
- `TST_INGEST_API_KEY` — sent as the `X-API-Key` header

Vault root on this box: **`/mnt/hermes_sync/ObsidianVault`**.

`/mnt/hermes_sync` is the cifs mount of `//192.168.1.162/MarketResearch` — the only
share this box can write to, and it sits inside the Obsidian vault root, so notes
written there appear in Obsidian with no extra setup.

Do **not** write to `HermesSync/Vault`: despite the name that is the CREDENTIALS
folder (`alpaca.env`, `credentials.txt`), not an Obsidian vault.

If the mount is missing, STOP and report it — never fall back to a local folder,
because the dashboard would never see those notes.

## Procedure

### 1. Get the universe

```bash
curl -s -H "X-API-Key: $TST_INGEST_API_KEY" \
  "$TRADEHUNTER_URL/api/guidance/universe?per_sector=50"
```

Returns the top 50 tickers per sector (~550 names) from the same Finviz screens
the app's Sector page uses. When the user names specific tickers, use those
instead and skip this call.

### 2. Find the earnings 8-K

SEC needs a declared User-Agent on every request and allows roughly 10 requests
per second — pace yourself, and never run parallel bursts.

```bash
UA="TradeHunter Financials (contact: admin@tradehunter.net)"
# ticker -> CIK
curl -s -H "User-Agent: $UA" https://www.sec.gov/files/company_tickers.json
# filings
curl -s -H "User-Agent: $UA" https://data.sec.gov/submissions/CIK0001045810.json
```

In `filings.recent`, take entries where `form == "8-K"` **and** the `items`
string contains **`2.02`** ("Results of Operations and Financial Condition").
That item tag is what distinguishes an earnings release from a director change
or a debt issuance. Newest first.

### 3. Open the press-release exhibit

```bash
curl -s -H "User-Agent: $UA" \
  https://www.sec.gov/Archives/edgar/data/1045810/000104581026000073/index.json
```

**Exhibit filenames are not predictable.** NVDA files `q2fy27pr.htm`; Home Depot
files `hd_exhibit991x08022026.htm`. Do not pattern-match the name. Prefer any
document whose name suggests a release (`ex99`, `pr`, `press`, `release`,
`earnings`, `commentary`), skip the `R*.htm` XBRL viewer fragments and the
`<ticker>-<date>.htm` cover page, and if in doubt open the largest HTML document
and look at it. Some issuers file a separate **CFO commentary** exhibit that
repeats the guidance with more context — read it too when present.

### 4. Read out the guidance — the actual judgement

Find the outlook section (often headed "Outlook", "Guidance", "Business
Outlook", "Financial Outlook"). For every forward figure record:

| field | meaning |
|---|---|
| `metric` | `revenue`, `gross_margin`, `operating_expenses`, `operating_income`, `eps`, `tax_rate`, `capex`, `free_cash_flow`, or `other` |
| `period` | the fiscal period being GUIDED, e.g. `FY2027-Q3` — not the period reported |
| `basis` | `GAAP`, `non-GAAP`, or omit when the company does not distinguish |
| `unit` | `USD_B`, `USD_M`, `percent`, `USD_per_share` |
| `low` / `mid` / `high` | see the arithmetic rule below |
| `sentence` | the **verbatim** sentence the figure came from |
| `source_url` | the exact exhibit URL |

**Hard rules — the API enforces these and will reject the row:**

1. **Every figure must be quoted.** `sentence` must be the real wording from the
   filing, copied exactly. A row without it is rejected.
2. **Never invent a number.** A midpoint you record must appear literally in its
   own sentence. If the company gives a plain range ("$17.00 to $18.00"), record
   `low` and `high` — both must appear in the sentence — and leave `mid` empty.
3. **Only "±" bands may be computed.** For "$108.0 billion, plus or minus 2%":
   `mid=108.0` (quoted), `low=105.84`, `high=110.16` (derived). The derived band
   must bracket the midpoint.
4. **Never estimate, annualise or convert** a figure the company did not state.
   If guidance is withdrawn or absent, say so — do not carry the prior quarter
   forward.
5. **Guidance only.** If a sentence describes the quarter just reported, it is a
   result. Leave it out. When genuinely unsure, leave it out and note it.

If the release contains no quantified guidance, that is a real and common answer
— many companies guide only on the earnings call, which is not an SEC filing.
Record the quarter with an empty guidance list rather than skipping it, so "no
guidance issued" stays distinguishable from "not fetched yet".

### 5. Push the structured rows

```bash
curl -s -X POST "$TRADEHUNTER_URL/api/guidance" \
  -H "X-API-Key: $TST_INGEST_API_KEY" -H "Content-Type: application/json" \
  -d '{"items":[
        {"symbol":"NVDA","period":"FY2027-Q3","metric":"revenue","unit":"USD_B",
         "mid":108.0,"low":105.84,"high":110.16,
         "sentence":"Revenue is expected to be $108.0 billion, plus or minus 2%.",
         "source_url":"https://www.sec.gov/Archives/edgar/data/1045810/000104581026000073/q2fy27pr.htm",
         "accession":"0001045810-26-000073","filed":"2026-08-26"}
      ]}'
```

The response reports `stored`, `updated`, `rejected` and `problems`. **Read it.**
A rejection means a figure could not be traced to its sentence — fix the row, do
not retry unchanged.

### 6. Write the vault notes

Two files per company, under `/mnt/hermes_sync/ObsidianVault/Companies/`:

- `<TICKER>/<period>.md` — that quarter's guidance table plus the verbatim source
  wording, with YAML frontmatter (`ticker`, `period`, `filed`, `source`, `tags`).
- `<TICKER>.md` — the hub: a guidance-vs-actual row per metric per quarter,
  newest first, linking to each quarter note.

Start every generated file with:

```
<!-- GENERATED by TradeHunter. Rewritten on each run - put your own analysis in a separate note that links here. -->
```

These files are **regenerated**, so anything typed into them is lost. Human
analysis belongs in a separate note that `[[wikilinks]]` to them.

Inside a Markdown table, escape the pipe in a wikilink alias: `[[NVDA/2026-Q3\|2026-Q3]]`.

### 7. Report

Per run, state: tickers processed, quarters with guidance, quarters with none,
rows accepted, rows rejected (and why). Never report success for a ticker whose
POST was rejected.

## Scheduling

Earnings cluster, so a nightly pass is enough:

```bash
hermes cron add guidance --schedule "0 3 * * *" --prompt "run the guidance skill for the standard universe"
```

Backfill is a separate, explicit request ("backfill guidance for the last 12
quarters"). Pace it — ~550 tickers x many quarters is a long SEC crawl, and the
fair-use limit is not optional.

## Changelog

### v1.0.0 — 2026-09-05
Created. Splits from the deterministic half in
`dashboard_tst/app/services/guidance.py`, which locates filings and exhibits but
cannot separate guidance from results (measured 7/15 with false positives). This
skill does that reading; the platform validates every figure against its quoted
sentence and rejects any that cannot be traced.
