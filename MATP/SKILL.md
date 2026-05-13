---
name: MATP
version: 1.2.0
description: Build a Median Analyst Target Price (MATP) + Max Buy Price (MBP) table from a Finviz screener URL and push it to Google Sheets. Use this skill when the user wants to run MATP analysis, generate an MATP table, or asks anything like "compute MATP for this Finviz screener", "give me the MATP table", "run MATP on <finviz url>". The skill takes a single Finviz screener URL, extracts every ticker, looks up the latest earnings date on MarketBeat, collects post-earnings analyst price targets, emits a 5-column table (Ticker | Exchange | Last Earnings Date | MATP | MBP) plus a detailed markdown file with all per-ticker evidence, and appends a new dated tab to the configured Google Sheet.
---

# MATP — Median Analyst Target Price + Max Buy Price builder

**Version:** 1.2.0 — 2026-05-13

End-to-end pipeline that turns one Finviz screener URL into a 5-column MATP + MBP table.

## Changelog

- **1.2.0** (2026-05-13) — Added Max Buy Price (MBP) as column 5. Formula: `MBP = MATP / 1.15`. Rounded to 2 dp. CSV header is now `Ticker,Exchange,Last Earnings Date,MATP,MBP`. `push_to_sheets.py` formats both D (MATP) and E (MBP) as USD currency. Detail markdown summary table gains the same column.
- **1.1.2** (2026-05-13) — Docs only. Added Windows `py -m pip` note for fresh installs, clarified manual vs in-pipeline invocation of `push_to_sheets.py`, and documented how to re-run setup or change the target sheet.
- **1.1.1** (2026-05-13) — Improved error reporting in `scripts/setup_sheets.py`: APIError now surfaces the underlying HTTP status + message (so "API not enabled" vs "permission denied" vs "sheet not found" are distinguishable), and unknown exceptions print a full traceback plus errno/filename attributes. No behavior change on the happy path.
- **1.1.0** (2026-05-13) — Added Stage 6: push CSV to Google Sheets via a service-account-backed `scripts/push_to_sheets.py`. New tab per run, named with today's date in YYYY-MM-DD. Adds `scripts/setup_sheets.py` for one-time credential setup and `requirements.txt` for `gspread` + `google-auth`. Skill is still usable without Sheets push if setup hasn't been run.
- **1.0.0** (2026-05-13) — Initial release. Five-stage pipeline: Finviz scrape with `&r=` pagination → MarketBeat earnings lookup → MarketBeat forecast scrape → post-earnings filter (strict `>`, same-day excluded) → median calculation. Emits 4-column chat table plus `MATP_analysis.md` detail file.

## Versioning policy

Bump the version field in the frontmatter **and** add a one-line entry to the Changelog above whenever the skill changes. Use semantic versioning:

- **Patch (x.y.Z)** — typo/wording fixes, clarifications that don't change behaviour.
- **Minor (x.Y.0)** — new optional step, extra output, new edge-case handling that's backward-compatible.
- **Major (X.0.0)** — change in inputs, change in output format/columns, change in MATP formula, or anything that would make a previous run's output incomparable.

Always update both places (frontmatter `version:` and the dated Changelog line) in the same edit so they never drift.

## What to ask the user (once)

> "Please paste the Finviz screener URL."

That is the **only** thing to ask. Do not prompt for tickers, dates, or anything else — derive everything from the URL. Once the URL is in hand, proceed through all five stages without further questions.

If the user has not provided a URL by the time this skill is invoked, ask the question above and wait. Otherwise begin immediately.

## Output deliverables

1. **Final 5-column table** rendered in chat: `Ticker | Exchange | Last Earnings Date | MATP | MBP`.
2. **Detail file** written to `MATP_analysis.md` in the skill directory, containing the summary table plus a per-ticker breakdown (every post-earnings analyst target used, sorted list, median calc shown). The summary table includes MBP.
3. **CSV file** written to `MATP_table.csv` in the skill directory (the same 5 columns, machine-readable). Numeric values unquoted, no `$` sign.
4. **Google Sheets push** — a new tab in the configured sheet, named with today's date (YYYY-MM-DD), containing the same 5 columns. MATP (D) and MBP (E) are formatted as USD currency. Skipped automatically if Sheets setup hasn't been run.

## First-run setup (one time only)

Required before the Google Sheets push will work. If `.env` is missing, run:

```bash
pip install -r requirements.txt
python scripts/setup_sheets.py
```

**Windows note:** On a fresh Python install, `pip` may not be on PATH even though `python` is. If you see `pip : The term 'pip' is not recognized…`, use the Python launcher instead:

```powershell
py -m pip install -r requirements.txt
py scripts/setup_sheets.py
```

`setup_sheets.py` walks the user through creating a Google Cloud service account, downloading the JSON key, sharing the target sheet with the service-account email, and persisting the key path + Sheet ID to a gitignored `.env`. The user pastes the JSON path themselves — never ask them to paste key contents into chat.

If `.env` already exists, skip setup and proceed.

### Re-running setup / changing the target sheet

Just run `python scripts/setup_sheets.py` again. It overwrites `.env` with whatever new path + Sheet ID you provide. There is no separate "edit config" command — re-run setup. To switch sheets, you'll also need to share the new sheet with the same service-account email (or generate a new key for a new project).

---

## Pipeline

### Stage 1 — Pull the ticker list from Finviz

Fetch the user-supplied URL with `WebFetch`. Ask the model to return all visible tickers with their exchange (NASDAQ / NYSE / AMEX) and **the total result count** plus the current page indicator (e.g. "Page 1 of 4").

Finviz paginates 20 rows per page using a `&r=` offset. If the result count exceeds 20, fetch additional pages **in parallel** by appending `&r=21`, `&r=41`, `&r=61`, etc. to the same URL:

```
<original-url>&r=21
<original-url>&r=41
<original-url>&r=61
```

Stop fetching when you have ≥ total result count tickers.

**Exchange caveat.** Finviz's default v=111 view does not always render the exchange column. WebFetch may infer the exchange from convention (most are NASDAQ; a few well-known names like CAT, LLY, V are NYSE). This inference is good enough because **MarketBeat tolerates either `/NASDAQ/<ticker>/` or `/NYSE/<ticker>/` in the URL** — both resolve to the same page. So even if the exchange tag is wrong, the next two stages still work. Use the inferred value in the final table without re-validating.

### Stage 2 — Latest earnings date per ticker

For every ticker, fetch in parallel:

```
https://www.marketbeat.com/stocks/<EXCHANGE>/<TICKER>/earnings/
```

Prompt:

> "What is the most recent past earnings date reported for this stock? Return just the date in YYYY-MM-DD format, or 'Unknown' if not found."

Batch the calls — issue 15-16 `WebFetch` tool uses in a **single message** to parallelize. Don't sequence them one at a time.

### Stage 3 — Analyst forecast targets per ticker

For every ticker, fetch in parallel:

```
https://www.marketbeat.com/stocks/<EXCHANGE>/<TICKER>/forecast/
```

Prompt:

> "List every analyst rating in the price target history table. For each row with a NUMERIC price target, output: YYYY-MM-DD | Brokerage | $TARGET. If the target is a 'boost' like $440 → $480, use the new value ($480). Skip rows without a numeric target. List all rows. End with: TOTAL_NUMERIC_ROWS = N."

**Pagination check.** MarketBeat's forecast page lists *all* historical targets on a single page — there is no "next page" control. (Verified on heavy-coverage names like NVDA with 40+ ratings.) So one fetch per ticker is sufficient; do not look for a page 2.

**Boost-target rule.** Many rows are formatted `$260.00 → $350.00` (a target raise). Always use the **new** (right-side) value. The prompt above already instructs this; trust it.

**Skip rows without a numeric target.** "Reiterated Rating" with no number, "Upgrade" with no listed target, "Initiated Coverage" with `(no target listed)`, etc. — drop these. The MATP is computed only over numeric targets.

### Stage 4 — Filter, compute median, derive max buy price

For each ticker:

1. Keep only rows where `target_date > latest_earnings_date`. **Strictly greater-than** — same-day-as-earnings targets are excluded by spec.
2. Sort the surviving targets numerically.
3. Compute the median (MATP):
   - **Odd n:** the middle value.
   - **Even n:** the average of the two middle values: `(v[n/2 - 1] + v[n/2]) / 2`.
   - **n = 0:** report `N/A` (no post-earnings coverage yet).
   - **n = 1:** the single value (note this in the per-ticker section).

   Do **not** use the mean. MATP is explicitly a median.
4. Compute the Max Buy Price (MBP): `MBP = MATP / 1.15`. This is the price below which a buy still leaves at least 15% headroom to the median target. If MATP is `N/A`, MBP is also `N/A`.

Round both MATP and MBP to 2 decimals.

### Stage 5 — Emit local outputs

Write **both** files in the skill directory:

**`MATP_analysis.md`** — full detail. Structure:

```markdown
# Median Analyst Target Price (MATP) Analysis

**Generated:** <YYYY-MM-DD>
**Source:** Finviz screener (<url>) + MarketBeat
**Methodology:** <one-paragraph summary>

## Summary Table
| # | Ticker | Exchange | Last Earnings Date | MATP | MBP |
| --- | --- | --- | --- | ---: | ---: |
...

## Per-Ticker Detail

### <TICKER> — <EXCHANGE> | Earnings <DATE> | n=<N> | MATP = $<X> | MBP = $<Y>
| Date | Brokerage | Target |
| ... |
Sorted: a, b, c, ... → median calc = $X. MBP = X / 1.15 = $Y.
```

**`MATP_table.csv`** — same 5 columns, machine-readable. Header row is `Ticker,Exchange,Last Earnings Date,MATP,MBP`. MATP and MBP values must be unquoted numeric (no `$`, no thousands separator) so Sheets and downstream tools parse them as numbers.

### Stage 6 — Push to Google Sheets

After writing the CSV, push it to the configured sheet:

```bash
python scripts/push_to_sheets.py
```

This step has **two valid invocation modes**:

- **Inside the skill pipeline** — Claude runs the command via the Bash tool as the last automated step of a MATP run.
- **Standalone by the user** — if the user has manually refreshed `MATP_table.csv` (e.g., re-ran an earlier MATP without push, or hand-edited the CSV), they can run the same command themselves from PowerShell/bash to push that CSV up. No coordination with Claude needed.

The script auto-detects today's date and creates a new tab named `YYYY-MM-DD`. It will fail loudly if:

- `.env` is missing → tell the user to run `python scripts/setup_sheets.py` first, then continue without the push (still render the chat table + report local files).
- A tab with today's name already exists → re-run with `python scripts/push_to_sheets.py --overwrite` only after asking the user (this destroys the existing tab's contents).
- The sheet is no longer shared with the service account → surface the email from `.env`'s JSON key and tell the user to re-share.

On success, capture the printed sheet URL (with the new tab's `#gid=`) and include it in the final chat message so the user can click straight to the new tab.

### Stage 7 — Final chat reply

Render the 4-column summary table inline, then in 1-2 sentences mention:

- Where the local files were written.
- The Google Sheets URL of the new tab (or, if push was skipped/failed, why and what to do).
- Any flagged edge cases (tickers with n=0 / n=1 post-earnings targets, unusually stale earnings dates).

---

## Performance & token budget

A typical 60-ticker screener requires roughly:

- 4 `WebFetch` calls for Finviz pages (parallel)
- 60 `WebFetch` calls for earnings pages (parallel, in batches of ~15)
- 60 `WebFetch` calls for forecast pages (parallel, in batches of ~15)

That's ~120 `WebFetch` calls. Batch them aggressively — putting 15 calls in a single message is fine and runs them concurrently. Do **not** sequence them one per turn or the run will take far too long.

## Quality checks before declaring done

- Every ticker from Finviz appears in the output (count matches Finviz's reported total).
- For each ticker, the per-ticker detail's `n` matches the row count actually listed in that section.
- The sorted list and median calculation are shown explicitly so the user can spot-check.
- "Latest earnings date" is a real past date (YYYY-MM-DD), not a future scheduled-earnings date. MarketBeat's earnings page lists both — extract only the most recent **past** row from the history table.

## Edge cases worth flagging in the final message

- Tickers with **n=0** post-earnings targets — MATP is N/A; tell the user.
- Tickers with **n=1** — the "median" is just that single number. Mention this so the user doesn't over-weight it.
- Tickers whose latest earnings date is more than ~3 months stale — the post-earnings window is unusually long; surface this so the user can decide whether the data is still relevant.

## What this skill does NOT do

- It does not validate Finviz filter syntax — just fetches whatever URL the user provides.
- It does not refresh prices, compute upside, or rank tickers. Pure data extraction + median.
- It does not produce xlsx. CSV + Google Sheets cover the export need. If the user explicitly asks for xlsx later, that's a separate task.

## File layout

- `SKILL.md` — this file.
- `scripts/setup_sheets.py` — one-time interactive setup for Google Sheets credentials.
- `scripts/push_to_sheets.py` — CSV → Sheets uploader, dated-tab-per-run.
- `requirements.txt` — `gspread`, `google-auth`.
- `.gitignore` — keeps `.env` and any service-account JSONs out of version control.
- `.env` — created by setup; holds `GOOGLE_SA_KEY_PATH` and `MATP_SHEET_ID`. Gitignored.
- `MATP_analysis.md` — generated each run (overwritten).
- `MATP_table.csv` — generated each run (overwritten).
