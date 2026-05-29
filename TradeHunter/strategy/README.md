# strategy/ — Layer 2: analysis units

Self-contained strategies the bot fires. Each FAMILY (GUNS, future ORB,
DITP, etc.) is its own subfolder. Each strategy inside a family is its
own subfolder with `impl.py`, `__init__.py`, and `README.md` (the
README's Changelog section holds the per-strategy version history).

Each strategy owns:
- its own risk + sizing policy (NOT a global percentage)
- its own scanner / universe selection (typically at the family level)
- its own resource pulls (which `resources/` modules it asks for)
- its own analysis logic
- its own conviction text written to the journal
- its own `__version__` + Changelog in its README

Two independent LIVE runtime gates per strategy (`scripts/_gating.py`):
- **ON/OFF** — does the pipeline run at all?
- **ARMED** — do plans submit to Execution?

## Contents

- `__init__.py` — Strategy registry. `KNOWN_STRATEGIES` (leaf names) + `_STRATEGY_IMPORT_PATHS` (leaf → `FAMILY.<setup>` dotted package path) + `load_known(cfg)` + the sys.path bootstrap that every layer module relies on.
- `base.py` — `Strategy` dataclass + the interface contract every strategy module must implement (`pick_universe`, `fetch_bars`, `evaluate`, `build`).
- `signals.py` — Shared math helpers: EMA, position_size, split_pm_rth, spread_ok.
- `GUNS/` — Gap Up News Scalp family (Adam Khoo Piranha Profits Lesson 8). Two wired setups today.

## Adding a new family

1. `mkdir strategy/<FAMILY>/`. Drop in: `__init__.py`, `README.md`, `_helpers.py` (family-shared logic), `scanner.py` (pre-market universe builder, if needed).
2. For each setup in the family: `mkdir strategy/<FAMILY>/<setup_name>/` with `__init__.py` (`from .impl import build, __version__`), `impl.py`, and `README.md` (version history).
3. Add the leaf name to `KNOWN_STRATEGIES` and the dotted-path mapping to `_STRATEGY_IMPORT_PATHS` in this folder's `__init__.py`.
4. Add a config block under `cfg.strategies.<leaf_name>` in `config.example.json`.

## Changelog

### 2026-05-21 — `base.py` Strategy gains optional shortlist phase
- Added `shortlist_et: str | None` field — when set, the orchestrator fires the strategy's `shortlist` callable at that ET wall-clock (pre-`entry_et`).
- Added `shortlist: Shortlist | None` callable — `(date_iso, cfg, strategy) -> None`. The strategy is expected to write its own shortlist artifact under `state/` and emit its own journal events.
- Added `Shortlist` type alias.
- `__repr__` includes `shortlist_et` when present.
- Default values added to `take_profit_R` and `max_concurrent` so subclasses can rely on dataclass field-order being flexible (added before/after the new optional fields).
- Existing strategies (guns_setup1, guns_setup5) construct `Strategy` via keyword args, so this is a non-breaking change for them.

### 2026-05-21 — Folder established
- Moved from `scripts/strategies/` into top-level `strategy/`.
- `base.py` and `__init__.py` stayed at this level (interface + registry).
- `signals.py` moved here from `scripts/` (analytical helpers, used only by strategies).
- GUNS-family files moved into `GUNS/` subfolder (see `GUNS/README.md`).
- Added `_STRATEGY_IMPORT_PATHS` mapping so leaf names (used in state flags, journal events, dashboard pills) stay decoupled from the family-nested dotted import path.
