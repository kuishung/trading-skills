# journal/ — Layer 4: structured logs

Every interesting moment becomes a structured record. The journal is
the substrate for post-trade analytics, the dashboard event stream,
and the future Review layer's self-improvement loop.

Two flows:
1. **Shortlist + conviction journaling** — scanner output + reasoning
   per candidate. ("Why is this on the list, and how confident am I?")
2. **Setup-execution journaling** — full plan, entry, fills, exits,
   R-multiple, post-mortem.

Goal: someone reading the journal weeks later can reconstruct exactly
what was seen, what was decided, why, and what happened next.

## Contents

- `writer.py` — `journal(strategy, event, **fields)` appends one JSONL line per call to `data/journal/journal_<date>.jsonl` (the cream). `summarize_by_strategy(date_iso)` prints an EOD per-strategy roll-up (shortlisted / rejected / planned / submitted / filled / BE / TP / SL / cancel / EOD-closed counts). `read_journal()` transparently falls back to legacy `state/journal_*.jsonl` so old files stay readable.
- `events.py` — `emit(type, payload)` appends to `state/events_<date>.jsonl`. Used by the dashboard event stream + the `auto_start` lifecycle event.

## Canonical event vocabulary (additive)

```
shortlisted          symbol entered the strategy's candidate pool
rejected             symbol rejected from the pool (with reason)
entry_planned        plan computed, no order yet, includes `plan`
entry_submitted      order sent to Alpaca, includes order_id + qty
entry_disarmed       plan computed but strategy is DISARMED — no submit
entry_filled         fill received, includes avg_price + filled_qty
oco_attached         OCO bracket attached after fill
breakeven_moved      stop moved to entry at 1R unrealised
exit_filled          TP or SL hit, includes leg + filled_avg_price
entry_cancelled      unfilled entry order cancelled (with reason)
force_closed         EOD sweep liquidated open position
strategy_started     strategy entry phase began
strategy_finished    strategy entry phase ended (with submit count)
strategy_off_skipped strategy was OFF when its entry_et fired
universe_picked      pick_universe finished, includes count
watchlist_missing    strategy expected a watchlist file that wasn't there
watchlist_loaded     strategy loaded its watchlist, includes count
shortlist_built      shortlist phase finished, includes per-source counts
shortlist_loaded     entry phase loaded the shortlist file
shortlist_load_failed entry phase tried to load shortlist, failed
shortlist_failed     shortlist callable raised
data_provider_selected   orchestrator startup probe: which feed will be used
data_provider_fallback   IBKR probe failed, falling back to Alpaca for the session
ibc_autolaunch_started   bot spawned IBC launcher .bat because TWS wasn't logged in
```

Add new event names as needed. The vocabulary is open.

## Changelog

### 2026-05-22 — Replay-mode routing in `writer.py`
- New module-level hook `set_replay_target(path)` lets the orchestrator redirect journal writes to a per-replay file under `data/replay/` (e.g. `data/replay/journal_2026-05-21_20260522T013122Z.jsonl`). One file per `--replay-date` run; multiple runs of the same date are diffable.
- When a replay target is set, the dashboard bridge to `events.emit()` is **skipped** so today's `state/events_*.jsonl` (and therefore today's dashboard) isn't polluted with historical decisions.
- `journal()` now mkdirs the target's parent (instead of always `JOURNAL_DIR`) so the replay path works without a separate setup step.
- See `review/README.md` → "Replay workflow" for the full mechanics.

### 2026-05-21 — `writer.py` bridges every record to `events.py`
- `journal/writer.py`: after appending to `data/journal/journal_<date>.jsonl`, `journal()` now also calls `events.emit(f"strategy.{strategy}.{event}", payload)`. Net effect: every strategy decision (`shortlist_built`, `planned`, `rejected`, `entry_submitted`, …) appears in real time in the dashboard event stream and the new dashboard Strategy Analysis panel — previously the dashboard tailed only `state/events_*.jsonl` and was blind to per-symbol strategy decisions written to `data/journal/`.
- Best-effort: the bridge is wrapped in `try/except`; if `events.emit` fails (e.g. permission denied), the canonical `data/journal/` write still happens and `journal()` returns normally.
- Absolute import (`from events import emit`) intentionally — `writer.py` is loaded as a top-level module by orchestrator bootstrap (siblings on `sys.path`), not via the `journal` package, so the relative form would fail.
- One-shot replay for today's session: a separate driver re-emitted today's accumulated `data/journal/journal_2026-05-21.jsonl` lines through `events.emit` so the new dashboard panel renders today's already-fired decisions without an orchestrator restart (which would have re-fired Setup 1's entry phase mid-day — the `fired` set is in-memory only).

### 2026-05-21 — Output path moved to `data/journal/` (the cream)
- `journal/writer.py` now writes to `data/journal/journal_<date>.jsonl` instead of `state/journal_<date>.jsonl`. Reason: journals are *accumulated artifacts*, not session-ephemeral state — they're the substrate the `review/` layer reads weeks/months later. They belong in `data/` (committed, long-term memory), not `state/` (gitignored, regenerated every session).
- `read_journal()` checks the new location first, then falls back to the legacy `state/journal_*.jsonl` path so historical files keep loading without re-migration. Existing files were `git mv`-ed so commit history is preserved.
- No event-vocabulary changes. No caller of `journal(...)` had to change — the path is encapsulated.

### 2026-05-21 — Convention: events carry `strategy_version`
- No code change in this folder. **Discipline change**: strategies should include `strategy_version=plan.get("strategy_version")` in every relevant `journal(...)` call so downstream analytics (`review/stats.py`) can bucket outcomes per rule-set version.
- Currently in place: `guns_setup1` v1.1.0 + `guns_setup5` v1.0.0 set `plan["strategy_version"]` after building the plan; the orchestrator forwards it into `entry_submitted`.
- Future events to add the field to (as needed): `entry_filled`, `breakeven_moved`, `exit_filled`, `force_closed`. The orchestrator emits these; passing `strategy_version=tr.plan.get("strategy_version")` at those sites is a clean follow-up.

### 2026-05-21 — Vocabulary additions: shortlist events
- Added `shortlist_built`, `shortlist_loaded`, `shortlist_load_failed`, `shortlist_failed` to the canonical event vocabulary. Emitted by strategies that wire a shortlist phase (Setup 1 is the first).
- No code change in this folder — purely a vocabulary documentation update. The events are written via the existing `journal(...)` API from `writer.py`.

### 2026-05-21 — Folder established
- Moved from `scripts/`:
  - `scripts/_journal.py` → `writer.py`
  - `scripts/_events.py` → `events.py`
- Bootstrap-free: these modules don't import sibling layers, just resolve `SKILL_DIR = Path(__file__).resolve().parent.parent`.
- New events added during the gating work: `entry_disarmed` (ARM gate skipped submit) and `strategy_off_skipped` (ON/OFF gate skipped entire pipeline).
