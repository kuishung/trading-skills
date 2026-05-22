# review/ — Layer 5: enrichment + self-improvement loop

This is the **enrichment program** — the systematic way the bot gets
smarter over time. Reads the journal, computes per-strategy and
per-ticker statistics, surfaces proposals for parameter changes, and
(eventually) closes the loop by applying versioned edits with full
audit trail.

The user explicitly rejected the failure mode of "fixed code that
doesn't evolve". This folder is what makes the bot adapt to what the
market actually does, not what the original PDF author thought.

## The 7 enrichment tracks (target state)

1. **Rich journaling** — every decision carries full evidence + version. *(track 1, foundation, in progress)*
2. **Fixture library** — labeled real-world bar sequences for regression-testing pattern detection. *(track 2, needs TradingView MCP, not yet built)*
3. **Weekly review CLI** — stats → propose → (optional) LLM critique. *(track 3, stats + propose live, llm_review not yet)*
4. **Per-strategy versioning** — `__version__` + per-folder Changelog. *(track 4, in place)*
5. **Per-ticker behavior memory** — accumulated outcome stats per symbol. *(track 5, scaffolded in stats.py output, no persistent profile yet)*
6. **Market regime awareness** — VIX / SPY context tagged per day. *(track 6, not yet built)*
7. **Long-term memory + rules** — discovered facts persist via CLAUDE.md / READMEs. *(track 7, convention in place)*

## Contents

- `stats.py` — **The analyzer.** Reads `data/journal/journal_*.jsonl` across a date window (and legacy `state/journal_*.jsonl` for pre-migration days). Computes per-strategy event counts, rejection-reason breakdown, per-symbol metrics, and R-multiple distribution for completed trades. Pure stats, no LLM. CLI:
  ```
  py review/stats.py                    # last 7 days, all strategies
  py review/stats.py --window 30d
  py review/stats.py --window all --strategy guns_setup1
  py review/stats.py --symbol NVDA
  py review/stats.py --json             # machine-readable output
  py review/stats.py --save             # also writes data/review/stats_<today>.json
  ```
- `propose.py` — **The proposer.** Consumes stats output, runs threshold-based heuristics, emits structured proposals when patterns clearly warrant change. Cold-start safe: refuses to propose with sample size below `--min-sample-size` (default 30). CLI:
  ```
  py review/propose.py                  # last 7d, default min sample 30
  py review/propose.py --window 30d
  py review/propose.py --min-sample-size 50
  py review/propose.py --json
  py review/propose.py --save           # also writes data/review/proposals_<today>.json
  ```

## What's intentionally NOT here yet

- `llm_review.py` — qualitative narrative review via Claude API. Needs a token budget conversation first. ~$0.50-$1/week when wired.
- `apply.py` — autonomous patcher that bumps a strategy's `__version__`, edits its `impl.py`, and appends a Changelog entry. Only safe after enough manual proposal-review cycles to trust the propose step.
- `regime.py` — daily snapshot of VIX / SPY trend / sector rotation. Cheap to add when needed.
- `ticker_profiles/` — per-ticker behavior accumulation. Scaffolded in `stats.py` output today; persistent JSON profile to be added when 30+ days of data exist.
- `fixtures/` — TradingView-sourced labeled bar sequences. Blocked on TV MCP connection.

## The feedback loop (target state)

```
Detection ─► Plan ─► Order ─► Fill ─► Outcome ─► Journal
                                                    │
                                                    ▼
                                          stats.py / propose.py
                                                    │
                                                    ▼
                                          (human review)
                                                    │
                                                    ▼
                              versioned edit (bump + Changelog entry)
                                                    │
                                                    ▼
                              ON+DISARMED paper-eval N days
                                                    │
                                                    ▼
                                          ARM when validated
```

Today we have everything except the (human review) step's safety net
(`apply.py`). For now, the user is the human review step: read
`propose.py` output, decide whether to apply, do the version bump +
Changelog entry by hand.

## Cold-start reality

Until 30+ days of journal data exist, `propose.py` mostly returns
"insufficient data" warnings. That's correct behavior. The way out
is to run the bot **ON + DISARMED** daily — paper-eval mode
accumulates real journal events at the same rate as live trading
without any capital risk. Once data is in, the enrichment program
has fuel.

## Replay workflow (the new substrate for long-term study)

The orchestrator can re-run any past trading day against the bars we stored in `data/price_history/`:

```
py execution/orchestrator.py --replay-date 2026-05-21 --fake-now 09:36
```

Mechanics:
- `--replay-date YYYY-MM-DD` forces `cfg["data_provider"] = "parquet"`, sets `cfg["replay_date"]`, and switches `date_iso` to the replay date.
- `--dry-run` is forced True. No Alpaca submission, ever.
- `fetch_bars()` calls inside Setup 1 / Setup 5 go through `scripts/_common.get_pm_bars` → new `_parquet_minute_bars()` which reads `data/price_history/1min/<SYM>.parquet` between 04:00 ET and `--fake-now` ET of the replay date.
- Journal events are routed to `data/replay/journal_<date>_<run_id>.jsonl` via `journal.writer.set_replay_target()`. Live `data/journal/` and `state/events_*.jsonl` are untouched, so today's dashboard isn't polluted.
- The shortlist phase is **skipped** in replay mode — `do_shortlist()` scrapes live PM movers / GUNS scanner output, which is TODAY's state, not the replay date's. The entry phase loads whatever shortlist artifact existed at `state/shortlist_guns_setup1_<replay-date>.json`; if missing, the universe is empty. Each replay run also writes its own journal file so multiple runs of the same day are diffable.

Coverage requirements:
- 1-minute parquet bars must exist for the symbols in the replay-day's shortlist. The S&P 500 1m ingest covers index members; GUNS tiny-cap targets (WHLR / ATPC / PCLA / …) need their own ingest via `py resources/ibkr_history.py update --universe --timeframes 1min`.
- The historical `state/shortlist_guns_setup1_<date>.json` artifact must exist. The journal's `shortlist_built` event payload contains the symbols if a polluted artifact needs reconstruction.

Output files:
- `data/replay/journal_<date>_<run_id>.jsonl` — one file per replay run. `run_id` is `YYYYMMDDTHHMMSSZ` of the wall-clock when the orchestrator started, so multiple replays of the same day produce separate files.
- Can be analyzed by `stats.py` / `propose.py` exactly like live journals (point them at `data/replay/` via the existing CLI args once they're plumbed in — first cut still reads only `data/journal/`).

Known gaps (next iterations):
- Shortlist replay: today's `--replay-date` uses the historical shortlist artifact. Future: derive the shortlist from `shortlist_built` events in the live journal so reconstruction is automatic.
- `fetch_bars` in Setup 5 calls `get_rth_minute_bars` (the full-day variant). The parquet provider handles both via `_parquet_minute_bars(..., pm_only=False)`.
- Setup-version differential replay: re-run the *same* date with a *different* strategy version to see which rule-set would have planned/rejected differently. Mechanics work today (just edit `impl.py`, bump `__version__`, re-run replay); the diff tool is the missing piece.

## Changelog

### 2026-05-22 — Replay foundation: `--replay-date` reads bars from `data/price_history/`
- The orchestrator can now re-run past sessions against stored bars instead of going to IBKR/Alpaca. This is the substrate for the self-improvement loop: change a rule, replay yesterday, see how the new ruleset would have decided.
- New CLI flag (in `execution/orchestrator.py`): `--replay-date YYYY-MM-DD`. Pair with `--fake-now HH:MM` to set the wall-clock cutoff. Forces `--dry-run`.
- Replay events route to `data/replay/journal_<date>_<run_id>.jsonl` so today's live journal isn't touched. One file per replay run; multiple runs of the same day are diffable.
- See "Replay workflow" section above for mechanics, coverage requirements, and known gaps.

### 2026-05-21 — `--save` flag wired in stats + propose; reads from `data/journal/`
- Both CLIs now write JSON snapshots into `data/review/` when `--save` is passed:
  - `stats.py --save` → `data/review/stats_<today>.json`
  - `propose.py --save` → `data/review/proposals_<today>.json`
- This makes the bot's review output a first-class **committed artifact**, not an ephemeral terminal print. Snapshots accumulate over time so we can diff "what did the proposer think a week ago vs today" and see whether tuning intuitions are stabilizing or whipsawing.
- `stats.py` reads journals from `data/journal/journal_*.jsonl` first, then falls back to legacy `state/journal_*.jsonl` so historical days stay analyzable through the migration.

### 2026-05-21 — Folder operationalised
- New `stats.py` (~500 LOC): per-strategy + per-symbol journal analyzer. Reconstructs trade life cycles (`entry_submitted` → `entry_filled` → `breakeven_moved` → `exit_filled` / `force_closed` / `entry_cancelled`) by joining events. Computes R-multiples, win rates, rejection breakdowns. Cold-start tolerant; works on whatever data exists. Emits text or JSON.
- New `propose.py` (~270 LOC): threshold-based proposal generator consuming `stats.py` output in-process. Heuristics: dominant-rejection-reason flagging, outcome-skew flagging (under/over-performing strategies), per-ticker whitelist/blacklist candidates. Refuses to propose under `--min-sample-size` (default 30).
- Verified end-to-end against existing 160 events from 2 days of journal data: surfaced a real pattern (`guns_setup1` rejections are 65% `pm_volume_below_min`) that warrants human review. Also caught a data-shape gap (`orb_5min` legacy events have entries but no R outcomes — pre-existing test entries from before the gating split, expected).

### 2026-05-21 — Folder established as placeholder
- Created empty `review/` with this README to reserve the slot.
- No implementation yet.
