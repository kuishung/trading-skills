---
name: guns-bot
version: 0.3.0
description: Automated paper-trading bot that runs Adam Khoo's Gap Up News Scalp (GUNS) strategy on Alpaca paper, with pluggable data feed (Alpaca IEX free, or IBKR real-time via the user's own TWS in paper mode). Executes Setup 1 (break of pre-market high) and Setup 5 (break of first 1-min candle) on a curated pre-market watchlist of gap-up stocks with a news catalyst. Submits OCO bracket exits at 2R take-profit, moves stop to breakeven at 1R, cancels unfilled entries by 10:30 ET, and reports via Telegram. The user manually opens TWS each morning and curates the watchlist; the bot does the rest. Trigger phrases include "run guns", "start guns bot", "guns paper trade today", "kick off the gap-up bot".
---

# GUNS — Gap Up News Scalp (Alpaca paper-trading bot)

**Version:** 0.3.0 — 2026-05-19

Automation layer for Adam Khoo's GUNS strategy (Lesson 8). Reads a pre-market watchlist, places Setup 1 / Setup 5 entries on Alpaca paper, manages OCO exits at 2R, moves stop to breakeven at 1R, and reports the day's activity to Telegram.

**Paper-only.** Trades route through the `alpaca-trader-paper` sibling skill, which hard-refuses any non-paper base URL. Going live requires a deliberate code change there — not a config flip in this skill.

## Strategy reference (verbatim from Lesson 8)

Pre-market window (9:00–9:25 ET):
- **Universe**: stocks gapping up pre-market.
- **Watchlist filters**: price ≥ $1.50, pre-market volume ≥ 30,000, strong news catalyst (earnings beat, FDA approval, analyst upgrade, clinical-trial pass — avoid acquisitions or low-impact news), low float (< 100M, ideally 10M–20M).
- **Chart filters**: gap is above 9 EMA / 20 EMA / 50 SMA / 200 SMA on daily; pre-market chart consolidating within ~20% of the pre-market high; price above 9 / 20 / 50 EMA on 5-min PM chart.

Entry setups (5 in the deck — this bot automates the two rule-based ones):

| # | Name | Trigger | Order placed | Status |
|---|------|---------|--------------|--------|
| 1 | Break of Pre-Market High | PM consolidates near PM high | Buy-stop-limit 1¢ above PM high, max 3–5¢ limit slip | **Automated** |
| 2 | Break of Pre-Market Pivot | PM consolidates at pivot ≥ 1R below PM high | Same mechanic, around the pivot | Manual / future |
| 3 | New High of PM Bull Flag | PM bull-flag exists, 1R fits before PM high | Buy-stop-limit above last pullback candle | Manual / future |
| 4 | First Bull Flag (intraday) | After open, M1/M2/M5 bull flag pulls back to 9/20 EMA | Buy-stop-limit above pullback candle high | Manual / future |
| 5 | Break of First 1-Min Candle | First 1-min candle is bullish, closes above 9/20 EMA, not abnormally large | Buy-stop-limit 1¢ above 1-min high | **Automated** |

Exit mechanics (uniform across all 5 setups):
- **Stop loss** (sell-stop) — for setups 1/2/3 placed by price band:
  - < $20 → 10–15¢
  - $20–30 → 15–20¢
  - $30–50 → 20–30¢
  - $50–100 → 30–50¢
  - Or 1× ATR of the 1-min candle
  - Setup 4 / 5 → 1¢ below the trigger candle's low
- **Take profit** (sell-limit) — 2R to 2.5R above entry. This bot uses 2R (the conservative end).
- **Trade management** — once 1R unrealised, move stop to entry (breakeven).
- **Time stop** — cancel any unfilled entry by 10:30 ET; close any open position by 11:00 ET (intraday scalp, not a swing).

Hardware/account in the deck: $25K min (PDT), Level 2 quotes, hot buttons. The bot runs on Alpaca paper (PDT-exempt) and **skips Level 2 gates** — Alpaca paper doesn't expose a usable depth-of-book feed. This is a known accuracy gap; flagged below.

## What this bot does NOT do (and why)

These are intentional gaps. The bot prints the reason in the EOD report so the user knows what was skipped.

- **No Setup 2 / 3 / 4** — these require pivot detection or bull-flag pattern recognition on intraday data, which is fragile to automate without first dialing in on backtests. Setup 1 + Setup 5 are deterministic single-level rules with clean entry/exit math.
- **No Level 2 gate** — Alpaca paper feeds a top-of-book quote, not a usable order-book depth. The deck uses L2 to confirm tight spreads and absence of large asks before entering Setup 4/5. The bot substitutes a spread check (bid-ask spread ≤ 10¢) using the top-of-book quote.
- **No automated catalyst classification** by default — out of the box, the bot reads a manually-curated watchlist file (`state/watchlist_YYYY-MM-DD.txt`, one ticker per line) which the user prepares 9:00–9:25 ET. Optional auto-scan via Alpaca's free News API is available behind `--auto-scan` (see below) but errs toward more candidates rather than fewer — manual review still recommended.
- **No float check** — Alpaca doesn't expose shares-outstanding. To approximate the "low float" rule, the auto-scanner cross-references Finviz only if the user has set `FINVIZ_*` credentials (most users will not); otherwise the float rule is skipped. The Finviz MCP could be wired in here as a follow-up.

### Known bugs / sharp edges

- **Position size has no notional cap.** `signals.position_size()` currently only caps by risk (`equity × risk_pct / risk_per_share`), not by notional exposure. For high-priced stocks like NVDA ($200+) with $1 stop distances, this can produce qtys whose notional value exceeds the entire account (e.g., 500 shares × $220 = $110K on a $100K account). Alpaca's risk guard in `alpaca-trader-paper/scripts/risk.py` will reject these at submission time (`max_position_pct` default 10% of equity), so live orders just won't fill — but the dry-run output looks misleadingly fine. **Fix planned:** cap `qty` at `(equity × max_position_pct) / entry_price` as a second constraint inside `position_size()`. Until that lands, treat the dry-run qty as an over-estimate.

## File layout

```
guns-bot/
├── SKILL.md                       # this file
├── requirements.txt               # alpaca-py, python-dotenv, pytz, ib_insync (optional)
├── config.example.json            # copy to config.json to override risk knobs
├── .gitignore
├── scripts/
│   ├── _common.py                 # env loader, ET clock, paper-only guard, data abstraction, Telegram
│   ├── _ibkr_data.py              # IBKR data adapter (ib_insync); read-only enforced
│   ├── _events.py                 # tiny emit() helper -> state/events_*.jsonl (dashboard feeds off this)
│   ├── _smoke_ibkr.py             # bare-socket TWS handshake smoke test
│   ├── _dryrun_ibkr.py            # exercise the data adapter against SPY/AAPL/NVDA
│   ├── signals.py                 # gap detection, PM-high finder, first-1min eval
│   ├── scan_premarket.py          # build today's watchlist (manual or --auto-scan)
│   ├── trade_day.py               # the session orchestrator — main entrypoint
│   ├── dashboard.py               # FastAPI + WebSocket local dashboard (port 8000)
│   ├── setup_schedule.py          # register a Windows Task Scheduler job (bot)
│   ├── setup_ibkr.py              # one-time IBKR wizard (API toggle + IBC config + smoke test)
│   └── setup_gateway_autostart.py # register Windows scheduled tasks for IBC start/stop
├── web/                           # dashboard UI (vanilla HTML + Tailwind CDN)
│   └── index.html
├── ibc/                           # IBC binaries + config; credentials.txt gitignored
└── state/                         # gitignored
    ├── watchlist_YYYY-MM-DD.txt   # one ticker per line; written by scan_premarket
    ├── plan_YYYY-MM-DD.json       # per-ticker entry/stop/target levels for today
    ├── fills_YYYY-MM-DD.jsonl     # append-only: every fill / cancel / BE-move event
    ├── equity_YYYY-MM-DD.json     # opening + closing equity snapshot
    └── events_YYYY-MM-DD.jsonl    # append-only event log (scanner emits, order events, errors)
```

## Live dashboard

`scripts/dashboard.py` runs a local FastAPI server (http://localhost:8000)
that observes everything the bot does in real time — scanner candidates,
qualified watchlist, pending orders, open positions, fills, today's P&L,
event log. Single browser tab; WebSocket auto-reconnect.

Bot scripts publish events via `_events.emit("type.name", {...})` which
appends to `state/events_YYYY-MM-DD.jsonl`. The dashboard tails that file
and pushes new lines to the browser.

Run it alongside `trade_day.py` in a separate terminal:

    py scripts/dashboard.py

### Desktop shortcuts (Windows)

For one-click launching, run the per-PC installer once:

    py scripts/setup_dashboard_launcher.py

Drops two shortcuts on your Desktop:

  - `GUNS Dashboard` — idempotent. Starts the dashboard (if not already
    running) and opens it in your browser. The dashboard runs in a
    minimised cmd window; close it via the in-app **Exit dashboard**
    button or the stop shortcut.
  - `GUNS Dashboard (stop)` — POSTs `/shutdown` and falls back to killing
    the process owning port 8000.

The `start_dashboard.bat` and `stop_dashboard.bat` launchers are committed
(path-portable via `%~dp0`) and sync across PCs via Dropbox. Only the
per-user `.lnk` shortcuts on Desktop are local to each PC — re-run the
installer on each new machine.

### IBKR API probe details

The dashboard health probe is composite: a cheap bind-probe every 3 s
detects whether something owns the API port, and a full ib_insync
handshake every 30 s verifies the accept loop is responsive. The
handshake uses clientId 99 so it does not collide with the bot's
clientId 71. Earlier versions used a raw TCP connect every 3 s — that
left CloseWait sockets piled up in TWS until its accept loop choked.
Don't reintroduce that pattern.

## Data-feed architecture (recommended: manual TWS)

The bot separates **execution** (always Alpaca paper) from **data** (pluggable). Two supported architectures:

### Architecture A — Manual TWS + bot reads via API (recommended)

You open **TWS in paper mode** each morning. The bot connects to that same TWS over the API (port 7497, Read-Only mode). Same paper account serves both: you trade manually in the GUI, the bot reads bars and submits its own trades through Alpaca paper.

```
                                 ┌────────────────────────────────────────────┐
       You                       │  TWS (paper)                               │
   click buttons   ─────────────>│  - GUI for manual trading                  │
   in the GUI                    │  - API listener on 127.0.0.1:7497 (RO)     │
                                 └──────────────────┬─────────────────────────┘
                                                    │ bars / quotes
                                                    ▼
                                 ┌────────────────────────────────────────────┐
                                 │  guns-bot                                  │
                                 │  - reads TWS data (port 7497, read-only)   │
                                 │  - applies GUNS signals                    │
                                 │  - decides what to trade                   │
                                 └──────────────────┬─────────────────────────┘
                                                    │ bracket order
                                                    ▼
                                 ┌────────────────────────────────────────────┐
                                 │  Alpaca paper (port 443 over HTTPS)        │
                                 │  - $100K simulated equity                  │
                                 │  - executes the bot's orders               │
                                 └────────────────────────────────────────────┘
```

**Why this is the recommended path:**

- You keep **one** paper account that's both the manual-practice playground AND the data source for the bot. No coordinating multiple sessions.
- TWS already exposes a usable API — no extra software needed beyond what you launch for manual trading anyway.
- Read-Only API physically blocks the bot from placing IBKR orders. Even if a bug pointed it the wrong way, IBKR's server would reject. The bot can ONLY submit orders to Alpaca paper.
- No IBC, no scheduled Gateway start, no vault credentials, no auto-relogin scripts. You launch TWS once each morning when you sit down to work; the bot's scheduled task fires later and finds TWS already up.

**Tradeoffs:**

- No auto-start. You manually open TWS each session. If you forget, the bot falls back to Alpaca's IEX feed (built-in fallback in `_common.py`) with a Telegram warning — degraded data but no missed trading day.
- TWS uses ~600 MB RAM (Gateway is ~150 MB). Immaterial on any modern machine.

### Architecture B — IB Gateway + IBC (auto-start, deprecated for this project)

The original plan was IBC-managed Gateway with vault-stored credentials and Windows Task Scheduler auto-start. All that code still exists in the repo (`scripts/setup_ibkr.py`, `scripts/setup_gateway_autostart.py`, `ibc/`) and works, but **Architecture A is recommended** for the user who's both learning manually and running the bot. The IBC path is kept available for headless deployments (e.g., bot running on a separate machine where no human is logging in).

If you're choosing between the two:

| | Architecture A (manual TWS) | Architecture B (IBC + Gateway) |
|---|---|---|
| Same paper account as manual trading | ✓ Yes | ✗ No — Gateway takes the session |
| Auto-start at boot | ✗ No (you open TWS manually) | ✓ Yes (Windows Task Scheduler) |
| Setup complexity | Low — 3 toggles in TWS API settings | High — IBC install, vault, scheduled tasks |
| Bot can run unattended for weeks | Only if TWS stays up | Yes |
| RAM usage | ~600 MB (TWS) | ~150 MB (Gateway) |
| Recommended for | The user who's also learning manually | Headless / remote deployments |

The rest of this document assumes Architecture A unless explicitly stated.

### Data provider fallback

Regardless of architecture, if the bot can't reach IBKR at startup (TWS closed, Gateway not running, network drop) it **falls back to Alpaca's IEX feed** for that session — degraded but not dead. The fallback is logged in the Telegram EOD report so you know data quality was reduced.

## Setup (one-time)

### Step 1 — Install Python dependencies

```bash
cd guns-bot
py -m pip install -r requirements.txt
```

### Step 2 — Alpaca paper credentials

The bot **executes** orders through Alpaca paper. You need API keys:

1. Sign up / log in at https://app.alpaca.markets/paper/dashboard/overview
2. On the right panel, click **"Generate New Keys"** and copy both the Key ID and Secret immediately (secret is shown once).
3. Run the sibling skill's setup wizard:
   ```bash
   cd ../alpaca-trader-paper
   py -m pip install -r requirements.txt
   py scripts/setup.py
   ```
4. Paste the keys when prompted. The wizard verifies them with a test call and saves to `.env` (gitignored).

### Step 3 — Configure TWS API (one-time)

Launch TWS, log in to **paper** account. Then:

1. **File → Global Configuration → API → Settings**
2. Tick: ☑ **Enable ActiveX and Socket Clients**
3. Tick: ☑ **Read-Only API** *(critical — physically blocks IBKR orders, leaving Alpaca paper as the only execution path)*
4. Set **Socket port** = `7497` (TWS paper)
5. **Trusted IPs** → Create → `127.0.0.1` → OK
6. Tick: ☑ **Allow connections from localhost only**
7. Click OK, then **fully restart TWS** for settings to take effect

Verify:

```powershell
py -c "import socket; s=socket.socket(); s.settimeout(2); s.connect(('127.0.0.1', 7497)); print('TWS API listening on 7497 OK'); s.close()"
```

Then run the full bot data adapter end-to-end (requires `ib_insync` installed via Step 1):

```bash
py scripts/_ibkr_data.py
```

Expected output:

```
Connecting to IBKR at 127.0.0.1:7497 ...
Connected. Managed accounts: ['DUN408540']    ← your paper account ID
Probing AAPL bars via the public helper ...
Probe AAPL: fetched <N> 1-min bars today.
```

### Step 4 — `config.json` should look like this

```jsonc
{
  // ... risk knobs ...
  "data_provider": "ibkr",
  "ibkr_host": "127.0.0.1",
  "ibkr_port": 7497,           // TWS paper. (Gateway paper = 4002.)
  "ibkr_client_id": 71,
  "ibkr_allow_live_port": false,
  "ibkr_app_type": "tws"       // or "gateway" if you're on Architecture B
  // ... other keys ...
}
```

If `data_provider` is `"ibkr"` and TWS isn't running, the bot logs the failure and silently falls back to `"alpaca"` for that session.

### Step 5 — Telegram (optional but recommended)

Reuses the `MATP` sibling skill's Telegram setup. If `../MATP/.env` already has `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, this bot picks them up automatically. Otherwise:

```bash
py ../MATP/scripts/setup_telegram.py
```

### Step 6 — Schedule the daily bot run

```bash
py scripts/setup_schedule.py --time 20:55
```

The `--time` value is your **local** wall-clock. See the timezone table in "Session timeline" below for what value to use.

---

### Architecture B setup (legacy — IBC + Gateway, only if you need headless)

Skip this if you're using Architecture A (recommended). The original IBC-based path is documented separately as legacy:

- Install IBC from https://github.com/IbcAlpha/IBC/releases into `guns-bot/ibc/`
- Install offline IB Gateway from https://www.interactivebrokers.com/en/trading/tws-offline-stable.php (the **regular** Gateway installer is auto-updating and won't work with IBC — you need the **offline** one which puts a `jars\` folder under `C:\Jts\ibgateway\<version>\`)
- In IBKR client portal: API → Settings → tick Read-Only API
- Run `py scripts/setup_ibkr.py` — wizard for paths, vault-aware credentials, IBC config patches
- Run `py scripts/setup_gateway_autostart.py` — registers Windows scheduled tasks for IBC start/stop
- Bot connects to Gateway on port 4002

Credentials live in a path you choose (recommend a Cryptomator/VeraCrypt vault). The launcher batch script reads them at runtime via env vars — no secrets in the codebase. See git history of this file for the full IBC security model if you go this route.

## The morning catalyst workflow (the user's job)

The bot can only trade what's in the watchlist. **Curating the watchlist is the human's most important contribution** — bad watchlist, no edge no matter how good the rules are. This routine takes ~25 minutes each weekday morning before US pre-market activity slows down.

### Run timing

Best window is **~19:00–19:30 Malaysia time** (= 7:00–7:30 AM ET). By then US pre-market has been running ~3 hours and most overnight news has hit.

### Step 1 — Pull the raw gapper list (5 min)

Open two tabs:

- **Primary:** https://thestockmarketwatch.com/markets/today.aspx — lists every stock gapping >2% pre-market with the **news catalyst already attached**. Best free source for GUNS.
- **Cross-check:** https://finviz.com/screener.ashx?v=111&s=ta_topgainers — Finviz's pre-market gainers screener.

### Step 2 — Score each gapper on catalyst tier (10 min)

For each gapper above ~5%, classify the news:

| Tier | Catalyst type | Trade it? |
|---|---|---|
| 🟢 **A** | FDA approval, FDA breakthrough designation, Phase 3 trial passes | **Yes — best** |
| 🟢 **A** | Earnings beat WITH raised guidance, or beat + reaffirmed | **Yes** |
| 🟢 **A** | Major contract win (Pentagon, Apple, etc.) | **Yes** |
| 🟡 **B** | Earnings beat (no guidance change), analyst upgrade from major firm | Maybe — only if other criteria are stellar |
| 🟡 **B** | Insider buying, share buyback announcement | Maybe |
| 🔴 **C** | Acquired / being acquired at fixed price | **No** — gap is capped at deal price, no further upside |
| 🔴 **C** | "Strategic partnership" with no revenue impact | **No** — fluff |
| 🔴 **C** | Stock split announcement, dividend hike | **No** — minor catalyst, fades fast |
| 🔴 **C** | "No news" gap (sympathy moves, technical breakouts) | **No** — no fuel |

**Quick disqualifier shortcuts:**

- If the news says "acquired by", "to be acquired", "merger with" → skip
- If you can't find a news article in 30 seconds → assume no news → skip

### Step 3 — Float + chart check on survivors (10 min)

For each Tier A or B catalyst stock, two quick checks.

**Float check** — open `https://finviz.com/quote.ashx?t=<TICKER>`, find the "Shs Float" row:

| Shs Float | Verdict |
|---|---|
| < 20M | 🟢 **Sweet spot** — fast moves, big % swings |
| 20M – 100M | 🟢 Good |
| 100M – 500M | 🟡 OK if catalyst is Tier A |
| > 500M | 🔴 Too heavy — won't move enough on news |

**Chart check** — open daily chart (TradingView or TWS):

| Looking for | What's good | What's bad |
|---|---|---|
| Where today's gap opens | **Above** 9 EMA, 20 EMA, 50 SMA, 200 SMA | Below any of those |
| Recent price action | Multi-day base / consolidation broken by the gap | Coming off a parabolic 100%+ run already |
| Next resistance | Big "window" / open space above current price to next resistance | Strong overhead resistance within 1-2% of pre-market high |

A picture-perfect GUNS chart: stock has been trading sideways for weeks → news drops overnight → gap opens above all moving averages → empty space above to next major level.

### Step 4 — Write the watchlist file (3 min)

By now you've narrowed from ~30 raw gappers to 4-6 finalists. Write them to:

```
guns-bot/state/watchlist_YYYY-MM-DD.txt
```

One ticker per line, optional `# comment` annotation:

```
AVTX  # FDA Phase 3 positive, 18M float, gap +52%
GME   # earnings beat, big float but A catalyst
PLTR  # major Pentagon contract
```

Save. The bot reads this at 20:55 Malaysia time when it starts.

### Worked example

**You see on a typical Monday at 7:00 AM ET:**

| Ticker | Gap | News | Float | Verdict |
|---|---|---|---|---|
| AVTX | +52% | FDA Phase 3 positive readout | 18M | 🟢 PERFECT — A catalyst + sweet-spot float |
| GME | +31% | Earnings beat (Q3) | 305M | 🟡 Float too high for ideal, but A catalyst — include |
| AAPL | +2% | Slight beat, no guidance change | 15B | 🔴 Mega-cap, won't move 5% — skip |
| ZNGA | +18% | Acquired by Take-Two at $9.86/share | 880M | 🔴 Acquisition cap — skip |
| BBBY | +12% | "Reverse split announced" | 800M | 🔴 Technical catalyst, no fuel — skip |

**Watchlist that morning:** just AVTX and GME. Two strong candidates beat six mediocre ones — quality > quantity.

### Step 5 — Let the bot run (passive)

You don't need to be at the keyboard. The scheduled task fires at 20:55 MY, bot runs autonomously to ~23:00 MY. Telegram report arrives when it's done.

---

## Daily run

The bot is one long-running session, **not** a cron loop. Start it once before market open; it sleeps between events and exits after the EOD report.

```bash
# 8:55 ET — start the bot. It blocks until ~11:05 ET.
py scripts/trade_day.py

# Useful flags:
py scripts/trade_day.py --dry-run        # plan + log only, no Alpaca orders
py scripts/trade_day.py --auto-scan      # auto-build watchlist via news+gap scan
py scripts/trade_day.py --watchlist AAA,BBB,CCC   # inline watchlist (skips file)
py scripts/trade_day.py --fake-now 09:25 # advance to a specific ET wall-clock for testing
```

### Session timeline

The bot uses an **ET-aware internal clock** regardless of host machine timezone. So Phase events fire at the right ET time even if your machine is on Malaysia/Asia/London time.

| ET (US DST) | Malaysia (MY) | Phase | What happens |
|------|------|-------|--------------|
| 09:00 | 21:00 | Watchlist build | Read `state/watchlist_YYYY-MM-DD.txt` (or `--auto-scan`). For each ticker, fetch PM bars (1-min, 04:00–current) and compute PM high, PM volume, spread. Reject tickers failing price ≥ $1.50, PM volume ≥ 30K, spread ≤ 10¢. Write `state/plan_<date>.json`. |
| 09:25 | 21:25 | Setup 1 queue | For each surviving ticker classified as "consolidating near PM high" (last 30 min of PM within 1.5% of PM high), submit a buy-stop-limit entry: stop = PM high + 1¢, limit = stop + 3¢. TIF = day. No bracket — exits are attached after the fill. |
| 09:30 | 21:30 | Open | Watch for Setup 1 fills via polling (Alpaca REST, 2s cadence). On fill, immediately submit OCO: sell-limit @ 2R, sell-stop @ stop_loss_distance. |
| 09:31 | 21:31 | Setup 5 evaluation | For each watchlist ticker without a Setup 1 fill or pending entry, evaluate the just-closed 9:30–9:31 1-min candle: bullish (close > open) AND close > both 9 EMA + 20 EMA AND candle range ≤ 1.5× the prior 20-bar median range. If valid, submit buy-stop-limit at 1¢ above the 1-min high. |
| 09:31–10:30 | 21:31–22:30 | Manage | Continue polling fills every 2s. For each filled position, track 1R level; once unrealised P&L crosses 1R, cancel the current sell-stop and resubmit at entry (breakeven). |
| 10:30 | 22:30 | Time cutoff | Cancel any unfilled entry orders. Open positions remain — their OCO legs handle exit. |
| 11:00 | 23:00 | Force close | Market-close any positions still open. (Intraday scalp policy.) |
| 11:00 | 23:00 | EOD report | P&L per ticker, win/loss, R-multiple realised. Send to Telegram. Exit. |

**During US standard time** (Nov 1 → Mar 8), add **+1 hour** to the Malaysia column above (e.g., market open becomes 22:30 MY instead of 21:30 MY).

**Recommended scheduled-task fire time for Malaysia users:** 20:55 local. This is 5 min before the bot's "watchlist build" phase during DST, and ~1 hour early during US standard time. The early start during standard time is harmless — the bot's internal ET clock makes it sleep until the right ET moment.

### Risk parameters (override in `config.json`)

```json
{
  "max_open_concurrent_positions": 3,
  "risk_per_trade_pct": 0.005,
  "take_profit_R": 2.0,
  "max_setup1_candidates": 4,
  "max_setup5_candidates": 4,
  "spread_max_cents": 10,
  "pm_consolidation_window_min": 30,
  "pm_consolidation_band_pct": 1.5,
  "time_cutoff_entry_et": "10:30",
  "time_cutoff_force_close_et": "11:00",
  "alpaca_skill_path": "../alpaca-trader-paper"
}
```

- `risk_per_trade_pct` — fraction of account equity risked per trade (default 0.5%). Position size = `(equity × risk_per_trade_pct) / (entry - stop)`.
- `max_open_concurrent_positions` — hard cap regardless of how many candidates the scan produces.
- `take_profit_R` — 2.0 (deck range: 2.0–2.5). Conservative; raise to 2.5 to match the deck's upper bound.

## Edge cases the bot handles

- **Multiple fills in the same minute** — orders are placed sequentially; before each new entry the bot re-checks `max_open_concurrent_positions`.
- **Buy-stop-limit slipped past the limit** — order stays open until 10:30 cutoff; if never filled, it's cancelled.
- **Partial fills** — the OCO is attached with `qty = filled_qty`; remaining open buy is cancelled at 10:30.
- **Gap-down at the open** (the stock opens below PM high) — Setup 1 buy-stop never triggers; bot evaluates Setup 5 instead at 9:31.
- **Watchlist file missing** — exits cleanly with instructions to either create the file or pass `--auto-scan` / `--watchlist`.
- **Run started after 9:25 ET** — bot logs the missed phases and continues from the current phase. Useful if your scheduler fires late.

## Scheduling

Run by hand the first few days to verify behavior, then automate with Task Scheduler:

```bash
py scripts/setup_schedule.py
```

This registers a daily 8:55 ET Windows scheduled task that invokes `py scripts/trade_day.py`. On macOS/Linux the script prints an equivalent `crontab` line.

## Versioning policy

Bump `version:` in the frontmatter and add a one-line changelog entry whenever this skill changes. Use semver:

- **Patch (x.y.Z)** — doc/wording fixes, no behaviour change.
- **Minor (x.Y.0)** — new setup added, new optional flag, more conservative default that's backward-compatible.
- **Major (X.0.0)** — change in risk math, change in default behaviour, removal of a setup.

## Changelog

- **0.3.0** (2026-05-19) — Architecture pivot. Recommended setup is now "Architecture A": the user manually launches TWS in paper each morning, and the bot reads data via TWS's API (port 7497, Read-Only). Same paper account serves both manual trading (clicks in TWS GUI) and the bot (reads bars + submits Alpaca orders) — no session contention because Read-Only API doesn't block GUI trading. IBC + Gateway path ("Architecture B") still works but is now documented as legacy / headless-only. Verified end-to-end dry-run pulled 431 bars of AAPL through TWS on the test session. Other changes: documented the morning catalyst-finding workflow (thestockmarketwatch + Finviz + A/B/C tier system + float/chart check + watchlist file format) — this is the user's daily 25-min routine. Added Malaysia local-time column to session timeline. Flagged a known bug: `position_size()` lacks a notional cap, so dry-run qtys for high-priced tickers (NVDA, TSLA) exceed account equity and would be rejected by Alpaca's `max_position_pct` guard on live submission. Fix planned for v0.3.1.
- **0.2.0** (2026-05-19) — Added IBKR data feed as an alternative to Alpaca IEX (execution still goes through Alpaca paper). New `data_provider` config key dispatches between `_alpaca_*` helpers and `_ibkr_data.py` (ib_insync-based). IBKR connection is enforced to Read-Only API mode and refuses the live port (7496) without explicit acknowledgement. Added `setup_ibkr.py` wizard (paths + IBC credentials + smoke test) and `setup_gateway_autostart.py` (Windows Task Scheduler entries for IBC start/stop). Auto-fallback to Alpaca on IBKR failure, surfaced in the EOD report. Bot can run end-to-end without IBKR set up — `data_provider="alpaca"` is the default.
- **0.1.0** (2026-05-19) — Initial scaffold. Setup 1 + Setup 5 automated, OCO exits, breakeven move at 1R, 10:30 entry cutoff, 11:00 force close, Telegram EOD report. Manual watchlist with optional `--auto-scan`. Level 2 gates and Setups 2/3/4 deferred.
