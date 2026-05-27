# intraday-bot — strict rules + cross-PC workflow

**Read this first. This file is the ONLY portable source of truth.**

Memory files at `~/.claude/projects/.../memory/` are LOCAL to whichever
PC built them — they do NOT sync via Dropbox. **CLAUDE.md syncs**.
Everything that must travel across the user's PCs lives here.

**Meta-rule for "remember this" requests:**
- Hard rule, convention, architecture decision, or any policy the user
  expects to outlive this session → **write it to CLAUDE.md** (here)
  first. Optionally mirror to a memory file for local-session
  convenience, but CLAUDE.md is canonical. If the two drift, CLAUDE.md
  wins.
- Personal preference about the user, transient session context, or
  one-off observations → memory file is fine, with an explicit ack
  that it won't travel to the other PC.
- If unsure whether something is "hard" — assume yes, write here.

## Cross-PC sync workflow (critical)

The user works on multiple PCs. The `intraday-bot/` folder syncs via
Dropbox. Workflow:

  - On PC A: start session pointing at intraday-bot/, work, **commit
    + push** before leaving.
  - Dropbox propagates the folder (source + state) to PC B.
  - On PC B: start session pointing at the same folder, pull, continue.

**Your responsibilities every session:**

1. **At session start**, before any code work:
   - **`git pull --ff-only` first** — user rule 2026-05-27: *"whenever
     i develop in this laptop, always gitpull here so that the changes
     are automatically made to this laptop"*. The other PC (Hermes)
     may have pushed commits via RDP between laptop sessions (config
     tweaks, IBC bundle edits, hot-fix patches). Skipping the pull
     risks immediate merge conflicts on the first laptop commit. The
     `--ff-only` flag aborts cleanly if a non-trivial merge would be
     needed, so this can never silently create a mess. If the pull
     finds a divergent history, stop and surface that to the user
     before any code work.
   - `git status` to confirm the working tree is clean.
   - `git log --oneline -5` to confirm the most recent commit matches
     what the user expects.
   - If there are uncommitted local changes that look like leftovers
     from another PC, surface them before doing anything else.
   - Verify per-machine tools (Python, pip, ib_insync etc.) are usable
     with a one-line import probe IF the user is starting trade work.

2. **Before the user signs off** (proactively remind them):
   - Run the smoke check: `py execution/orchestrator.py --dry-run
     --fake-now 09:36` ends without an exception.
   - `git status` shows no unintended changes.
   - **Commit + push** any code/doc changes — the next PC won't see
     them otherwise.
   - If config.json or any .env was edited, remind: "config.json is
     gitignored; that edit lives ONLY on this PC. Mirror it on your
     other PC manually, or move the change into config.example.json
     so it travels with the repo."

3. **During a session**, before suggesting "let's commit":
   - Confirm working tree state once more.
   - Do not auto-commit. The user always says "commit" explicitly.

Trigger phrases that activate session-start handoff verification:
  "i continue here", "continue from where we left off", "sync the
  branch", "what's the state", "where did we leave off".

Trigger phrases that activate sign-off prompting:
  "i'm done for today", "ending session", "signing off", "wrap up",
  "going to bed", "see you tomorrow", "switching PCs".

## Per-folder README convention (read carefully)

**Every folder under intraday-bot/ has its own README.md.** Each README has:

1. A one-line role description (what is this folder for?)
2. A "Contents" section listing the files with one-liners
3. A "Changelog" section — folder-scoped history, dated entries

**Every code change must update the relevant folder's README changelog section** as part of the same commit. Treat the README as documentation promoted to first-class source.

**Folder-scoped only.** A README's Changelog records ONLY changes that happened TO files in that folder. If a single piece of work touches multiple folders, EACH folder's README gets its own entry written from THAT folder's perspective:

- File moved from A to B: A's README says "Removed X (moved to B)"; B's README says "Added X (moved from A)".
- A feature that touches `journal/writer.py` AND `execution/orchestrator.py`: journal/README writes about writer.py; execution/README writes about orchestrator.py.
- Never write a single sprawling entry in one README about changes that span multiple folders.

When you edit anything in `<folder>/`:
- Add a dated entry to `<folder>/README.md` → Changelog explaining what changed and **why**.
- Cross-reference SKILL.md only if the change is large enough to warrant a SKILL.md changelog bump too.

Setup folders (`strategy/<FAMILY>/<setup_name>/`) merge their per-strategy versioning into the same README — they don't carry a separate `changelog.md`. The setup README's Changelog section **is** the version history that journal events reference via `__version__`.

The folders that should have a README: `resources/`, `strategy/`, `strategy/<FAMILY>/`, `strategy/<FAMILY>/<setup>/`, `execution/`, `journal/`, `review/`, `data/`, `dashboard/`, `scripts/`. Skip `ibc/`, `state/`, `__pycache__/`.

The intraday-bot/ root uses SKILL.md (skill manifest) + this CLAUDE.md instead of a README.

## Architecture (8 top-level folders)

Every change must map cleanly to one of these:

| Folder | Role | What lives here |
|---|---|---|
| `resources/` | Layer 1 — data sources | IBKR adapter, yfinance float/news, pattern primitives, ticker profiles, `bars_store.py` (parquet read/write), vendored TradingView MCP. Stateless. |
| `strategy/` | Layer 2 — analysis | Per-family subfolders (`GUNS/`, future `ORB/`, ...). Each strategy is its own folder with `impl.py` + `__init__.py` + `changelog.md`. |
| `execution/` | Layer 3 — Alpaca | Submits, manages positions with strategy-aware exit policies. |
| `journal/` | Layer 4 — logs | `writer.py` (decisions), `events.py` (event stream). Writes JSONL to `data/journal/`. |
| `review/` | Layer 5 — self-improvement | Reads `data/journal/`, proposes strategy edits with version bumps. Writes snapshots to `data/review/`. |
| `data/` | **the cream — accumulated artifacts** | Historical bars (`data/price_history/{1min,5min,15min,daily}/<SYM>.parquet`), decision journals (`data/journal/journal_<date>.jsonl`), review snapshots (`data/review/`), pattern fixtures (`data/fixtures/`). Bars are gitignored (bulk binary, regeneratable, Dropbox-synced); journals + review + fixtures are **committed**. |
| `dashboard/` | operational UI | `server.py` + `web/index.html` + the `*.bat` launchers + `setup_launcher.py`. Real-time observer + ON/OFF + ARM control. Everything dashboard-related lives in this one folder. |
| `scripts/` | operational glue | `_common.py` (config + env + ET clock + data dispatch), `_gating.py`, `setup_*.py`. |

**`data/` vs `state/`** — `state/` is *session-ephemeral* (flags, today's
watchlist, caches, today's fills/equity snapshots; mostly gitignored,
regenerated every session). `data/` is the *long-term memory* — decision
journals, ingested historical bars, review reports, test fixtures. If
the bot's "self-improvement loop" needs to read it weeks later, it lives
in `data/`. If it's just session bookkeeping that the next run can
rebuild, it lives in `state/`.

## Bars storage = Parquet (locked decision)

**Historical OHLCV bars are stored as Parquet, full stop.** Don't
re-evaluate CSV / JSONL / SQLite / DuckDB / W&B / MLflow as alternatives
— that conversation already happened, Parquet won. Reasons:

- ~10× smaller than CSV / JSONL, columnar, native to pandas + DuckDB,
  no server to run.
- Immutable once written (past months never change) → safe under
  Dropbox sync (no concurrent-write hazard like SQLite has).
- Append-friendly: a new symbol-month is a new file; old files never
  rewritten.

Read/write happens **only** through `resources/bars_store.py`. Strategy
code, `patterns.py`, `ticker_profile.py`, future `review/backtest.py`
all go through that module so the parquet detail stays in one place.
Bar dict shape is `{t, o, h, l, c, v}` everywhere — the same shape
`patterns.py` consumes.

Layout (one file per symbol per timeframe):

```
data/price_history/1min/<SYM>.parquet      full history per symbol
data/price_history/5min/<SYM>.parquet
data/price_history/15min/<SYM>.parquet
data/price_history/daily/<SYM>.parquet
```

**One-file-per-symbol tradeoff**: Parquet files are immutable, so
"appending" a new bar means read + concat + rewrite the whole file.
At ~3-5 MB per symbol-year that's ~50ms per write — fine. Only revisit
(go back to month-partitioning) if we ever ingest tick-level data or
push past ~10 years × 100+ symbols. The simplicity of `AAPL.parquet IS
AAPL's full history` is worth the rewrite cost at our scale.

`pyarrow` is in `requirements.txt`. It's lazy-imported inside
`bars_store.py` so modules that don't touch bars never pay the import
cost; only the actual read/write functions trigger it.

**First-run bulk seed** (`resources/yfinance_history.py` + `resources/sp500.py`):
- yfinance is the right tool for the INITIAL bulk seed because IBKR's
  60-per-600s pacing cap makes a 500-symbol fetch take ~9 hours.
  yfinance does it in ~93 seconds for 2 years of daily.
- Yahoo lookback caps per interval: **1min = 7d, 5min = 60d,
  15min = 60d, daily = unlimited**. Intraday pulls include pre-market +
  after-hours (`prepost=True`) so GUNS strategies have the data.
- `py resources/yfinance_history.py seed-sp500 --years 2`
  → 503 symbols × ~500 daily bars × Parquet = **~12 MB on disk**.
- Add `--include-5min --include-15min` for 60 days of intraday at
  those timeframes (each ~2-3 min, ~150 MB combined).
- Add `--include-1min` for 7 days of 1-min (~1-2 min, ~250 MB).
- `--include-all-intraday` is the shortcut for all three.
- `sp500.py` scrapes Wikipedia (`List_of_S%26P_500_companies`),
  7-day cache. The ingest auto-maps share classes (BRK.B <-> BRK-B).
- Resume-by-default: re-running `seed-sp500` only fetches symbols that
  aren't already in `bars_store`. `--force` overrides.

**Ongoing ingest pipeline** (`resources/ibkr_history.py`):
- Pulls OHLCV from IB Gateway / TWS via `ib_insync`, writes through
  `bars_store.write_bars()`. Two flows: SEED (one-shot bulk lookback)
  and UPDATE (incremental, only bars after the last stored timestamp).
- Pacing: 7s between requests by default (under IB's 60-per-600s cap).
- Universe = symbols seen in `data/journal/` last 30d + today's GUNS
  watchlist + `cfg.history_universe` manual additions.
- **Auto-runs post-EOD** when `cfg.eod_history_ingest=true` (default).
  After the 15:58 close-all sweep, the orchestrator updates today's
  universe so tomorrow's review/backtest sees fresh bars without
  manual action. Soft-fail by design — bot success doesn't depend on
  IBKR being up. User can always rerun manually:
  `py resources/ibkr_history.py update --universe`.
- One-shot seed for a fresh PC: `py resources/ibkr_history.py ingest
  NVDA MSFT TSLA --timeframes 1min,daily --days 60`.
- clientId 83 (non-collision with: 71 live bot, 80 observer, 82 GUNS
  scanner, 98 probe, 99 dashboard).

## Self-contained portability

**Every dependency lives inside intraday-bot/.** NO sibling-folder
reads (no `../alpaca-trader-paper/.env`, no `../MATP/.env`).

Credential resolution order (in `scripts/_common.py::_vault_root()` + `_env_lookup()`):

1. `$INTRADAY_ENV_DIR/<vendor>.env` — manual override via environment variable
2. `intraday-bot/.env` — in-folder fallback
3. `cfg["vault_dir"]/<vendor>.env` — per-PC absolute path from config.json (added 2026-05-24)
4. `<auto-discovered VAULT/Claude Credential>/<vendor>.env` — walk up from SKILL_DIR (back-compat)

The `cfg["vault_dir"]` setting (added 2026-05-24) mirrors `cfg["data_root"]` — per-PC absolute path supporting Resilio-synced credentials. Current usage:
- Laptop: `vault_dir = "D:\\HermesSync\\Vault"` (Resilio root)
- Hermes: `vault_dir = "C:\\HermesSync\\Vault"` (Resilio peer)

This lets credentials sync peer-to-peer via Resilio (encrypted LAN) instead of via Dropbox cloud, mirroring the data-folder architecture. Both files (`alpaca.env`, `intraday-premarket.env`) are tiny so the bandwidth concern was minor; the real motivation was removing the Dropbox dependency on Hermes (device-limit constraints) while keeping cross-PC credential availability.

`ibkr_secrets_path` is configured SEPARATELY because IBC (the IBKR Gateway controller) reads it as a direct file path, not via the bot's `_env_lookup()` mechanism. Set it to wherever your IBC creds file lives — typically alongside the bot's other secrets in HermesSync/Vault/.

On a new PC: clone the repo (or sync via Dropbox if device slots permit), connect to Resilio share, set `cfg["vault_dir"]` + `cfg["data_root"]` to local Resilio paths. Done.

## Per-strategy gating (two independent live flags)

Each strategy has TWO independent filesystem flags in
`scripts/_gating.py`:

  - `state/enabled_<name>.flag` — **ON/OFF** (does the pipeline run?)
  - `state/armed_<name>.flag` — **ARM** (do plans submit?)

Three states:
- **OFF** — nothing runs; one `strategy_off_skipped` journal entry per scheduled fire
- **ON + DISARMED** — full pipeline + journal, no orders (paper-eval)
- **ON + ARMED** — full pipeline AND orders submit

`is_enabled()` is checked at TOP of `_fire_strategy_entries` (cheap skip). `is_armed()` is checked at the submit site (toggling takes effect on next entry). `--dry-run` CLI flag overrides ARM globally.

`cfg.strategies.<name>.enabled` is a FIRST-RUN seed only.

## Normalized strategy parameters (Option A — ticker-relative thresholds)

**No absolute dollar / share / percent thresholds in strategy code.** Every numeric threshold must be ticker-relative — ATR multiples, volume z-scores, R-multiples — so the same code adapts to NVDA's $4 ATR and HIMS's $1.50 ATR without hand-tuning.

| Forbidden | Required |
|---|---|
| "Stop 1% below entry" | "Stop 0.3 × ATR below entry" |
| "Volume > 3M shares" | "Volume > 2σ above this ticker's typical 1-min volume" |
| "Gap > 2%" | "Gap > 0.7 × ATR%" |
| "Target = +$3" | "Target = entry + N × R (R = stop distance)" |
| "Reject if extended > $5" | "Reject if extended > 0.5 × ATR past trigger" |

Per-ticker behavioral baselines live in a cached JSON profile per ticker (e.g. `strategy/<FAMILY>/profiles/<TICKER>.json`) refreshed pre-market daily. Fields the brain reads: `atr_14d`, `atr_pct`, `avg_minute_vol_rth`, `minute_vol_stddev`, `premkt_range_avg`, `prev_close`, `daily_trend`.

Violation = strategy works on the ticker it was tuned for, fails everywhere else. This is the trap that kills most retail intraday systems.

Per-ticker performance tracking still happens — the journal accumulates per-ticker stats (win-rate, avg R, total R) so the user can prune low-edge names from each strategy's whitelist over time. Normalization removes hand-tuning, it does NOT guarantee universal applicability.

## Strategy reference docs convention (`strategies-reference/`)

When the user shares a new trading strategy (PDF / video / chat):

1. **Save the reference doc** as `strategies-reference/<NAME>.md` at the worktree root (the parent of `intraday-bot/`, since multiple skills could reference the same doc). Use the framework's canonical name (`DITP.md`, `GUNS.md`) — not `dynamic_intraday_trading.md`.
2. **Structure consistently** so every framework doc is navigable the same way:
   - Source attribution (path / URL / who taught it)
   - Methodology type (mechanical / discretionary / hybrid)
   - Top-level rules
   - Pattern / setup catalog
   - Key level hierarchy
   - Entry / exit rules per pattern
   - Stock screening criteria
   - Catalyst guidance
   - Risk + sizing
   - What's DISCRETIONARY (resists mechanization)
   - Implementation status table
   - Glossary of framework-specific jargon
3. **Strategy code MUST cite the source doc** in its `impl.py` top docstring: `"""Source: strategies-reference/GUNS.md, Setup 1"""`.
4. **Never blend rules** from multiple frameworks into one strategy file. Hybrids must explicitly cite both sources and annotate each rule's origin.

Why: trading frameworks reuse overlapping terminology with different meanings ("rebound on EMA20" means different things in different methods). Strict separation prevents cross-contamination under time pressure.

## Per-strategy versioning

Every `strategy/<family>/<name>/impl.py` declares `__version__`. Every rule edit bumps the version and adds a `changelog.md` entry. Journal events carry the version so post-trade analytics can attribute outcomes.

Bump rules: **MAJOR** = plan-dict shape change. **MINOR** = new gating filter or condition. **PATCH** = retuning, bug fixes.

## Per-family scanner

NO shared/continuous scanner. Each strategy family has its own pre-market scanner CLI that writes its own watchlist file:

- `strategy/GUNS/scanner.py` → `state/watchlist_guns_<date>.txt`
- (future) `strategy/ORB/scanner.py` → `state/watchlist_orb_<date>.txt`

## Strict risk rules (orchestrator startup gate)

```
- risk_per_trade_pct ≤ 1% of NLV     (global, never override)
- max_position_pct  = 10% of NLV     (global notional cap)
- At least one strategy WIRED        (config block + importable module)
- Each wired strategy: take_profit_R > 0 and max_concurrent > 0
```

All-OFF runtime state is allowed — bot starts, journals `strategy_off_skipped` per scheduled fire.

## When adding a new strategy

User teaches strategies one at a time. When that happens:

1. Save the doc as `strategies-reference/<NAME>.md`.
2. If new family: `mkdir strategy/<FAMILY>/`, add `_helpers.py`, add `scanner.py` if the family needs a universe builder.
3. `mkdir strategy/<FAMILY>/<setup_name>/` with:
   - `__init__.py`: `from .impl import build, __version__`
   - `impl.py`: `__version__ = "1.0.0"`, `build(cfg)`, `evaluate`, `pick_universe`, `fetch_bars`
   - `changelog.md`: seed with v1.0.0 entry
4. Add the leaf name to `KNOWN_STRATEGIES` in `strategy/__init__.py` AND add the dotted-path mapping to `_STRATEGY_IMPORT_PATHS`.
5. Add a config block under `cfg.strategies.<name>`.
6. Ask the user: **which RESOURCES** does this need? **Which EXIT POLICY**? **What SHORTLIST CONVICTION** should be journaled?
7. Smoke-test before declaring done.
8. **Verify dashboard visibility** per the "Dashboard visibility rule" above. The family tab auto-appears once journal events flow, but check: (a) is there a watchlist-style view needed if the strategy produces a scanner output, (b) does the Gating drawer show the new strategy's ON/ARM controls, (c) is there any state the user needs to see that isn't yet visible. Add UI in the same turn if needed.

## When asked to change an existing strategy

Bump `__version__`. Add a `changelog.md` entry explaining what changed and **why**. Commit the changelog edit alongside the rule edit.

## User context (relevant to bot behavior + scheduling)

The user is a self-directed intraday US-equities trader operating from **Malaysia (UTC+8)**. The bot runs against US market hours; the user typically works on the system during Malaysia daytime and live trading happens in their evening.

**Time-zone arithmetic** (anchor schedules to these):
- US market open (09:30 ET) = **21:30 MYT** during US EDT (Mar–Nov) / **22:30 MYT** during US EST (Nov–Mar).
- The bot's auto-start (08:30 ET, T-60 BMO) = **20:30 MYT** EDT / **21:30 MYT** EST.
- EOD close (15:58 ET) = **03:58 MYT** EDT / **04:58 MYT** EST — the user is usually asleep.
- DST transitions shift the entire schedule by 1 hour twice a year; call this out when proposing schedules.

**Traps the user has flagged as hard exclusions** (already enforced by the GUNS scanner; replicate for any new family):
- **M&A names** are dropped from intraday lists — an announced deal anchors price, no R:R left.
- **Post-extended gappers (>4–5%)** are usually consumed catalysts where profit-taking dominates. Penalize, don't favor.
- **Convertible-note offerings / secondaries / dilution events** drift to the offering reference price all morning. Drop.

**Workflow preferences:**
- Prefers ritualized, twice-daily confirmation patterns over single-shot analysis.
- Delivers alerts to himself via Telegram (creds resolved via VAULT lookup, see above).
- Uses TradingView Desktop with paid sub for chart analysis alongside the bot.
- Will not auto-commit — the user always says "commit" explicitly.

## Vendored MCP servers and tools

**The day-one rule wins**: every dependency, including MCP servers, lives **inside `intraday-bot/`**. No "I'll put it in `~/mcp-servers/` because it's a dev tool" exceptions — that breaks the cross-PC sync invariant and the user has rejected it explicitly.

### TradingView MCP — `resources/tradingview-mcp/`

Vendored from https://github.com/tradesdontlie/tradingview-mcp. Original `.git` stripped; current upstream commit recorded in `resources/tradingview-mcp/_UPSTREAM.md`. Updates are manual re-vendoring (see that file for the procedure).

**Purpose**: AI-assisted TradingView Desktop chart access via Chrome DevTools Protocol. Lets me read chart state, navigate symbols/timeframes, draw levels, manage alerts, run Pine Script, stream tick data — all from the user's locally running TradingView Desktop, which they pay for separately.

**Requires**: TradingView Desktop launched with `--remote-debugging-port=9222`. The MCP has a `tv_launch` tool that auto-relaunches with the flag.

### Per-PC install steps (after fresh Dropbox sync)

```bash
cd intraday-bot/resources/tradingview-mcp
npm install
```

`node_modules/` and `package-lock.json` are gitignored — per-OS, per-PC.

Then register the MCP with Claude Code by writing `%USERPROFILE%\.claude\.mcp.json` (per-PC paths — Dropbox path differs by drive letter):

```json
{
  "mcpServers": {
    "tradingview": {
      "command": "node",
      "args": ["<dropbox root>\\Claude\\claude-skills\\trading-skills\\intraday-bot\\resources\\tradingview-mcp\\src\\server.js"]
    }
  }
}
```

Restart Claude Code → `mcp__tradingview__*` tools become available.

## Dashboard visibility rule (HARD RULE — set 2026-05-23)

**Anything with observable runtime state that the user might want to watch MUST be surfaced in the dashboard before the feature is considered complete.** The bot is operated through the dashboard — if a new piece of work only lives at the CLI, the user can't see it, can't trust it, can't act on it without context-switching.

The user explicitly asked for this rule on 2026-05-23: *"after you have build anything which ought to be reflected in the dashboard please do so so that is can be visualized. make it a rule and memorise it"*.

### What qualifies as needing dashboard reflection

| Category | Examples | Dashboard surface required |
|---|---|---|
| **New strategy / setup** | OS Breakout, DITP P2, GUNS Setup 1 | Auto: family-tab in Strategy Analysis panel (driven by journal events) + Gating drawer (driven by `/bot/status`) |
| **New scanner / watchlist producer** | `strategy/OS/scanner.py`, `strategy/DITP/scanner.py` | Watchlist view in the strategy panel's family tab, OR per-symbol decision rows |
| **New data source / ingest** | `resources/sp_smallcap600.py`, ibkr 1m ingest | Health pill (`/data/health`) + audit trail via `data/ingest_log.jsonl` |
| **New scoring / ranking dimension** | DITP universes filter, tier/score columns | Inline UI control in the relevant tab (filter pills, columns in the table) |
| **New integration** | TradingView chart, IBKR movers, future TV-Desktop CDP | Visible pill / panel / drawer reflecting connection state + data freshness |
| **New runtime indicator** | "data fresh", "TWS connected", "scanner ran at HH:MM" | Pill in status bar, with click-through to a details modal where useful |
| **CLI-only utilities** (genuinely one-shot dev tools, no recurring state) | Migration scripts, one-off backfills, smoke tests | Exempt — no UI required |

### Definition of done

A feature isn't complete until **the user can see its state in the dashboard without dropping to the CLI**. If the dashboard work lags the backend by a turn (e.g., the backend lands first, the UI catches up next turn), the next turn MUST close the gap before moving to the next feature.

### Auto-surfaces already wired (use these by default)

- **Strategy family tabs** (Strategy Analysis panel) — auto-appear when `strategy.<name>.<event>` journal events flow. `name.split('_')[0].toUpperCase()` becomes the tab label. GUNS, DITP, OS all picked up automatically.
- **Gating drawer** (left sidebar) — auto-lists every `KNOWN_STRATEGIES` entry with ON/OFF + ARM/DISARM controls.
- **Event log + Bot log drawers** (left sidebar) — tail `state/events_<today>.jsonl` and `state/bot_<today>.log`.
- **Status-bar health pills** — IBKR, Alpaca, Bot, **Historical price data health**, gating summary, auto-start hint.

### When a new auto-surface is needed

If the existing auto-surfaces don't cover the new feature, add a dedicated UI piece in the same turn:
- New `/strategy/<family>/watchlist` endpoint + tab content (DITP pattern)
- New `/data/<thing>` endpoint + pill or drawer
- New CSS + JS that fits the console aesthetic (44px sidebar, drawers from the left, click-outside-to-close)

Server-side endpoints land in `dashboard/server.py`; frontend renders + handlers in `dashboard/web/index.html`. Both files' READMEs get a changelog entry per the per-folder convention.

### Cost of skipping this rule

The user has explicitly stated they don't want to learn CLI commands to operate their own bot. CLI-only features quietly degrade trust in the system because they can't be observed in the daily workflow. **Skip the rule once → the user has to remember "oh, that lives in a CLI", and the cognitive overhead of running the bot grows. Don't.**

## Hermes — autonomous worker VM (Hyper-V on R720)

Set 2026-05-24 as a hard architectural decision. The user owns a Dell R720 office server running Windows Server 2019 with Hyper-V role. A dedicated VM named **Hermes** runs there for jobs that are too long to tie up the laptop.

### Division of labor

| Job | Hermes | Laptop |
|---|---|---|
| Continuous data ingest (3min / 1min / daily) | ✅ | ❌ |
| Long backtest runs (parameter sweeps, multi-day windows) | ✅ | ❌ |
| Quick backtest iterations | Either | ✅ preferred (fast feedback) |
| Strategy code editing | ❌ | ✅ |
| Dashboard during US market hours | ✅ (recommended) or laptop | Either |
| Live trading bot (when it goes live) | ✅ (future, post-v0.2.0 of any setup) | ❌ |
| TradingView Desktop chart review | ❌ | ✅ |

**Mental model:** Hermes = autonomous worker. Laptop = developer cockpit.

### Setup reference

Step-by-step install + verification lives in **`intraday-bot/HERMES_SETUP.md`** at the repo root — follow that file when standing Hermes up from a fresh Server 2019 install. Don't duplicate setup instructions in folder READMEs.

VM specs: Windows Server 2019 Standard (Desktop Experience), 16 GB static RAM, 4 vCPU, 200 GB dynamic VHDX, external switch, Gen 2, auto-start with host (60s delay), production checkpoints.

### IBKR clientId allocation (CRITICAL — don't collide)

Each component needs a unique clientId per concurrent IBKR session. The full allocation across all machines:

| ClientId | Used by | Where |
|---|---|---|
| 71 | Live trading bot | Laptop (current) → Hermes (future) |
| 80 | Observer / dashboard streamer | Laptop |
| 83 | Ingest (legacy / current laptop-based) | Laptop |
| **84** | **Ingest (production target)** | **Hermes** |
| **85** | **Hermes health-check probes** | **Hermes** |
| 98 | Probe / handshake test | Either |
| 99 | Dashboard probe | Laptop |

Don't reuse these. Append next available number for any new component.

### Cross-machine sync — Dropbox for code, Resilio for data (set 2026-05-24)

**Two separate sync mechanisms** for two very different kinds of data:

| Layer | Mechanism | Location | Why this mechanism |
|---|---|---|---|
| Code, READMEs, configs | **Dropbox + git** | `intraday-bot/` inside Dropbox | Small, low-conflict, want git history. Dropbox makes cross-PC reads instant. |
| Bulk data (parquets, journals, reviews, ticker profiles, ingest logs) | **Resilio Sync (P2P over LAN)** | `D:\HermesSync\MarketData\` on laptop / `C:\HermesSync\MarketData\` on Hermes | ~10GB+ and growing — too heavy for Dropbox bandwidth + cloud-storage cost. Resilio = LAN-speed P2P, no cloud middleman. Both peers writable. |

Bot code resolves the data location through `scripts._common.get_data_root()`, which reads `cfg["data_root"]` from `config.json`. Per-PC config sets the local Resilio path:

```jsonc
// Laptop config.json
"data_root": "D:\\HermesSync\\MarketData"

// Hermes config.json
"data_root": "C:\\HermesSync\\MarketData"
```

Resilio sync of `D:\HermesSync\` ↔ `C:\HermesSync\` handles the actual file transfer. Inside HermesSync, `MarketData/` is the intraday-bot's data; other projects can use sibling folders.

**Implications for code:**
- All file I/O for data uses `get_data_root() / "<subfolder>"` — never hardcoded `SKILL_DIR / "data" / ...`
- `bars_store.PRICE_HISTORY_ROOT`, `journal.writer.JOURNAL_DIR`, `review.stats.JOURNAL_DIR / REVIEW_DIR`, `ticker_profile.profile_path()`, `bars_store.INGEST_LOG_PATH`, `review.backtest` output, `scripts.wait_and_ingest` log dir — all honour the cfg override
- Default (when `data_root` is empty / not set) is `SKILL_DIR / "data"` — preserves the self-contained behaviour for any fresh PC that hasn't customised
- `state/` is unchanged — still per-PC ephemeral session bookkeeping

**Sync targets summary:**
- **Dropbox-synced** (per existing Dropbox folder): all of `intraday-bot/` EXCEPT data/. The `data/` folder is no longer Dropbox-synced (excluded via Selective Sync on every PC).
- **Resilio-synced** (peer-to-peer): the entire `HermesSync/MarketData/` tree, including `price_history/`, `journal/`, `review/`, `ticker_profile/`, `fixtures/`, `ingest_log.jsonl`, ingest log files.
- **Not synced** (per-PC only): `state/*.flag`, `state/watchlist_*.{txt,json}`, `state/cache/`, `config.json`. **Never run the orchestrator on both Hermes and laptop simultaneously** — they would compete for the same flags + watchlists.

**Git tracking note:** Before 2026-05-24, `data/journal/*.jsonl`, `data/review/*.json`, `data/fixtures/`, `data/ingest_log.jsonl` were committed to git. Post-move, they live at `HermesSync/MarketData/` which is NOT git-tracked. Historical commits preserve the pre-move state; ongoing journal/review writes are now sync-only via Resilio. If we ever want long-term archival back in git, we'd need to copy/symlink specific journal files back into the bot folder.

### Python version — `py -3.12` for ALL IBKR workloads (hard rule)

`ib_insync` depends on `eventkit` which calls `asyncio.get_event_loop()` at import time. **Python 3.14 removed the implicit event-loop creation**, so `import ib_insync` raises `RuntimeError: There is no current event loop in thread 'MainThread'` and the module cannot be loaded at all. We discovered this on 2026-05-24 when a watcher launched via the default `py` (which resolved to 3.14) was about to crash the 180-day re-seed.

**Rule:** any script that touches IBKR (`resources/ibkr_history.py`, `resources/ibkr_data.py`, `resources/ibkr_smoke.py`, `scripts/wait_and_ingest.py`, `execution/orchestrator.py` when configured for IBKR) MUST be invoked with `py -3.12` explicitly. Don't rely on the default `py` launcher — different machines have different defaults, and a user upgrading to 3.14 silently breaks everything IBKR-related.

Non-IBKR code (`strategy/`, `review/backtest.py`, `dashboard/`) runs fine on either Python version.

`scripts/hermes_health.py` detects this specific failure mode and emits a WARN with the remedy. Run it before any long IBKR job as the pre-flight gate.

### Operational rules

- **Don't manually run TWS / IB Gateway on Hermes.** IBC manages it. Manual launches break IBC's lifecycle assumptions.
- **Long ingests on Hermes go through `scripts/wait_and_ingest.py`** (gives auto-reconnect + log file) OR Task Scheduler. Never naked `py resources/ibkr_history.py ...` inside an interactive RDP session — RDP disconnect kills it.
- **Only one machine runs the live orchestrator at a time.** The state flags don't coordinate across machines. Either Hermes OR laptop, not both.
- **Dropbox conflict files** (`*.conflicted.*`) on parquets mean both machines wrote the same parquet near-simultaneously. If you ever see one, investigate — shouldn't happen with our architecture but is a red flag if it does.
- **Don't push to git from Hermes** unless you've already committed all laptop work. Hermes can pull/clone freely; commits should originate from wherever you actually authored the change.
- **Patch Hermes host conservatively.** User explicitly flagged the concern (2026-05-24): they don't want to update the R720 host because it might break existing infra. Server 2019 guest VM means we don't need to update the host either. Status quo holds.

### When to RDP into Hermes

Routine: never. Hermes runs autonomously.

Reasons to RDP in:
- IBC reports "not connected" in a smoke test
- Disk space approaching limit (>180 GB used)
- Need to check why a specific ingest log shows errors
- Quarterly hygiene: Windows Update review (with the host-patch caveat above), Dropbox sync health, disk cleanup

Never RDP in JUST to "check on things." If something needs checking, it should be visible from the laptop dashboard or via the parquet sync.

## What NOT to do

- Don't add sibling-folder dependencies.
- Don't reintroduce a single global ARM flag.
- Don't merge ON/OFF and ARM back into one gate.
- Don't reintroduce `scanner_observe.py` or any ambient scanner.
- Don't put strategy-specific logic in `resources/` or `execution/`.
- Don't put global risk numbers in a strategy module.
- Don't skip the `strategy_off_skipped` journal event.
- Don't auto-commit. User always says "commit" or "push" explicitly to initiate the sequence. **But once they do, commit + push are bundled — never stop at commit and wait for a separate "push" instruction.** The user added this rule on 2026-05-23: *"after i confirm and commit you to perform certain things, always push to git after you have done so"*. Translation: when an explicit `commit` / `push` request lands, run smoke check → `git status` → stage → `git commit` → `git push` as ONE operation. Don't make the user say "push" after "commit", and don't make them say "commit" after authorising work — the explicit request covers both halves.
- Don't break the cross-PC sync invariant: if you put a file outside `intraday-bot/`, the user's other PCs won't see it.
- **Don't carve "external tools" exceptions to the day-one rule.** MCP servers, helper utilities, third-party libraries — they all go inside `intraday-bot/` (typically vendored into `resources/`). If a fresh clone needs `npm install` or `pip install`, that's fine — those are per-PC artifacts and gitignored. But the SOURCE must travel with the folder. (Mistake made + corrected on 2026-05-21: TradingView MCP was first cloned to `~/Dropbox/Claude/mcp-servers/`; user rejected the location and the code was moved to `resources/tradingview-mcp/`.)
