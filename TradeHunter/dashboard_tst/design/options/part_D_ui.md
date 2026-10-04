## D. The Options page: routes, templates, HTMX flows, My rules, Telegram push, and the migration of the three old pages

Scope of this part: everything a member touches. The data layer, storage, the nightly job and
the signal cache (A), the engines, the prefs schema, sizing, the order ticket and the exits (B)
and the trend line, range box and payoff engine + SVG (C) are referenced by the names listed in
**D0** and in the cross-part needs at the end; where this part calls a function another part
owns, the signature it expects is written out so the names stay reconciled.

Reconciled 2026-10-03 against `CRITIQUE.md` and the unified contract: this part no longer
carries its own positions tables, its own job table, its own prefs module, a client-drawn payoff, a
persisted live chain or a contract-count formula. Where an earlier draft said otherwise, the
contract's name, shape, unit or rule is what stands below.

Conventions used below, all taken from the existing code:

| Convention | Where it is already done | Used here for |
|---|---|---|
| Page shell renders instantly, every slow panel is a lazy HTMX fragment | `routes/portfolio.py:149-161`, `templates/portfolio.html:8-12`, `routes/curated.py:165-179` | the basket, the card, the positions board, the status strip |
| One `_list_context()`-style builder shared by the GET and every POST that re-renders the same fragment | `routes/ivscan.py:179-233`, `routes/portfolio.py:88-146`, `routes/curated.py:37-162` | `_basket_context`, `_card_context`, `_picks_context`, `_rules_context` |
| A fragment is wrapped in a class (`.pf-panel`, `.sp-panel`) and swapped by `outerHTML`, so its forms target `closest .<class>` | `templates/_portfolio_list.html:5-8`, `templates/_spreads_list.html:6-8` | `.opt-basket`, `.opt-card`, `.opt-picks`, `.opt-rules` |
| Member preferences live in a sparse JSON and are read through a cleaner that fills defaults (`es.clean_enabled`) | `services/ema_setup.py:455-462`, `services/trade_prefs.py:71-91`, `routes/ivscan.py:139-155` | the rules merge (`option_prefs.read`, B4.1 with this part's presentation columns, D3) |
| Browser talks to the member's own bridge on `127.0.0.1:9224` and POSTs the result to the server; the server only grades | `templates/_options_tab.html:24-128`, `templates/ivscan.html:163-213`, `routes/options.py:103-148` | `POST /options/live/{symbol}` (graded in-request through `BridgePayloadSource`, never stored), the scanner import |
| A nightly job on Hermes writes the DB directly through `SessionLocal`, leaves a dated log, is registered by a `setup_*_task.ps1` | `deploy/portfolio_daily_check.py:1-80`, `deploy/spread_scan.py:1-60`, `deploy/setup_portfolio_check_task.ps1` | the Telegram push (step 5 of A's nightly job), the `option_jobs` record the status strip reads |
| ONE lightweight-charts instance in the DOM at a time; overlays arrive as `chart_*` context variables of `_price_chart.html` | `templates/_curated_list.html:25-28`, `templates/_sector_chart.html:52-54`, `templates/_portfolio_chart.html:29`, `templates/_price_chart.html:415-428` | the card chart (`chart_bounce`, `chart_trendline`, `chart_range`, `chart_spread`, `chart_levels`) and the Ideas/Positions tab switch |
| The invisible-until-hover scrollbar is wildcard CSS in `base.html:15-22`; nothing may override it | `templates/base.html:15-22`, `templates/_options_analysis.html:350` | every scroll area on `/options` |
| Scrolling `tabular-nums`, 11px secondary text, emerald = action/up, rose = down, amber = warning, sky = watch | `templates/base.html:69-260` (the TradingView token layer) | all new markup |

### D0. Names this part assumes from parts A-C

| Name (owner) | What this part calls it for | Signature / shape this part expects |
|---|---|---|
| `OptionChainSnapshot` model (A2.1) | the full stored chain behind the expander (`GET /options/chain/{symbol}`) | columns `symbol, snap_on, kind, source, expiry, dte, right, strike, bid, ask, mid, last, bid_size, ask_size, iv (FRACTION), delta, gamma, theta, vega, rho, theo, oi, volume, prev_close`; rows are replaced per `(symbol, snap_on, kind)` |
| `option_store.latest_chain(db, symbol)` / `latest_snap_on(db, symbol)` (A5.3) | the expander, the in-request grade on Track | the stored rows (~3k, < 50 ms) |
| `option_store.card_for(db, symbol, user) -> dict` and `option_store.basket_rows_for(db, user) -> dict[symbol, dict]` (A5.3) | **the ONLY read path for `option_signal`**: the card, the picks, the ticket, the payoff, the basket | the latest row per symbol for the member's `prefs_hash`, else the house hash (`user_id IN (NULL, me)`); on a hash miss `card_for` computes lazily from the stored chain (no market call); each dict carries `id, symbol, snap_on, kind, as_of, status, trend, headline, setup, iv, strategies, picks, stale, age_h` where `stale` is computed there from `snap_on` vs the ET session date (`spread_monitor.et_today()`); `basket_rows_for` is ONE batched query, never a per-row lookup |
| `option_nightly.refresh_symbol(db, sym, user)` (A4.7) | `POST /options/refresh/{symbol}` | the per-symbol pipeline with `kind='intraday'`, computes the house signal AND the caller's hash, writes an `option_jobs(job='refresh')` row |
| `option_data.BridgePayloadSource(payload)` (A1.5) | `POST /options/live/{symbol}` | built from the posted dict, no network; yields `Chain(kind="live", source="bridge")` of that one expiry with `iv` divided by 100 (the one place percent is converted); rows capped at 400, strikes within 50% of spot |
| `option_store.bootstrap_iv(db, symbol, series, *, source="ibkr")` (A4.6) | inside `POST /options/live/{symbol}` | `series = [{"on": "YYYY-MM-DD", "iv": 31.2}, ...]` **PERCENT**, as bridge 1.6 sends it (`round(close*100, 1)`, `bridge/ibkr_bridge.py:559-563`), stored as-is bounded `0.1..1000`, <= 400 points; never overwrites a day the server read itself; marks the house signal `status='stale_iv'` so the next `card_for` recomputes the gauge |
| `option_prefs` (B4.1 schema + D3's presentation columns, one module `app/services/option_prefs.py`) | the rules drawer, every pick, the hash | blocks `shared, credit_vertical, debit_vertical, long, leaps, condor, time`; `read(db, user)`, `write(db, user, tab, form)`, `reset(db, user, tab)`, `for_strategy(prefs, key)`, `prefs_hash(prefs)` (pick-relevant fields only, D3.1), `family_of(key)`, `STRATEGY_KEYS`, `TABS`, `FIELDS` |
| `strike_picker.pick(strategy_key, chain_view, chart, gauge, prefs, *, nlv=None, today=None) -> PickResult` (B4.2, B4.8) | run **in-request only on Live** (the nightly job and `card_for` fill `option_signal.picks` for every distinct saved `prefs_hash`); otherwise picks are read from the signal | `PickResult = {strategy, family, prefs_hash, status: ok|degenerate, picks: [Pick x3], considered, degenerate: {reason_key, text, nearest, fix}|None, rules_line}`; a `Pick` = `{strategy, legs: [Leg], expiry, dte, back_expiry?, net (negative = credit), width, max_profit (+$), max_loss (+$), breakevens: [], pop (0..1), pop_kind: keep|profit, greeks, liquidity: {tier: clean|limit|wide|thin|unknown, widest, min_oi, vol_ok, notes}, constraint, score, why: [], words: {}, chart_stop, chart_stop_pl, rule_stop_pl, checks: [], sizing (filled at READ time, D1.4)}` |
| `Leg` (A/B/C, one shape) | every leg this part renders | `{expiry, right: C|P, strike, side: sell|buy, qty: positive int, price (mid), bid, ask, iv (FRACTION), delta (signed), oi, volume}`; the key is `oi` (never the longer spelling) once past `opt_legs.norm_leg`; `payoff.Leg.from_dict(leg)` derives the signed quantity |
| `option_sizing.size(pick, nlv, prefs) -> dict` (B5) | called by `_card_context` / `_picks_context` / the ticket / the push at READ time | `{nlv, nlv_source: bridge|prefs|None, risk_pct, risk_budget, loss_at_stop_usd, stop_price, rule_stop_usd, rule_stop_kind, fires_first, by_chart_stop, by_max_loss, by_notional, contracts, capital_at_risk_usd, max_loss_total_usd, note}`; `contracts = min(floor(risk_budget / loss_at_chart_stop_usd), floor(risk_budget x GAP_MULT / max_loss_usd), floor(nlv x 10% / notional))`, `0` is a valid answer, **never rounded up to one** |
| `order_ticket.build(pick, setup, prefs) -> Ticket` + `order_ticket.render(ticket, broker: tws|moomoo) -> str` (B6) | `GET /options/ticket/{symbol}` | the Ticket dict of B6.1 with `condition=None` by default (D2.8); the member's contract count and the dip toggle travel on the pick (`pick["sizing"]["contracts"]`, `pick["enter_on_dip"]`) so the signature stays the shared one |
| `OptionTrade` / `OptionTradeCheck` models (B7.1) and `option_exits.mark / grade / sweep` (B7.2-B7.3) | the Positions tab, Track this, `/options/badge` | `option_trades` = THE positions store for EVERY strategy from step 1 (generic legs cover bear_call now); `OptionTrade.legs` = the Leg shape + `entry_price, entry_delta, entry_iv`; `OptionTradeCheck.legs` per-day `{mid, delta, iv}`; `grade()` returns `{state, action, reasons, urgent, ...}` incl. the "earnings now inside" WATCH-urgent row (D1.14) |
| `OptionSpread` (legacy, `models.py:788-849`) | **read-only** for the legacy `/portfolio` until removal; nothing on this page writes it | `spread_monitor` is NOT changed |
| `payoff.build(legs, *, family, spot, atr, today, contracts=1, chart_stop=None, target=None, rule_frac=None, levels=(), sigma_fallback=None, pl_now=None) -> dict` + `templates/_payoff_chart.html` (C3, C4) | `GET /options/payoff/{symbol}` returns C's partial — **the ONE payoff implementation** | dict `{family, label, symbol, spot, atr, contracts, horizon{expiry, dte}, legs, xs, at_expiry, today (T+0 at t=0), breakevens: [], max_profit, max_loss (POSITIVE), unlimited_*, pop{label, value, basis, model, model_basis}, markers: [{x, label, kind}], hlines: [{y, label, kind}], dot, units{mode: $|R, per, r_dollars, r_basis}, caption, warnings, error, uid, svg{...}, series}`; `pnl / leg_value / curve_at` take `iv_bump: float = 0.0` |
| `payoff.pop(family, legs, spot, sigma_h, T_h, xs, ys)` (C3.8) | not called here; the one function behind every non-credit `pop` on a pick | credit families (`CREDIT_FAMILIES = {bull_put, bear_call, iron_condor}`) use `1 - |delta_short|` (condor two-sided); the model figure is shown as "model estimate {m}%" (D2.7) |
| `trend_line.overlay(tl, tl_bounce)` / `range_box.overlay(rng)` (C1.1, C2.1) | `chart_trendline` / `chart_range` of the card chart | `tl` = `{direction, p1{time,price}, p2, i1, slope_per_bar, slope_atr, touches[{time,price}], n_touches, span_bars, value_today, value_at{date: price}, broken, last_break, warning, residual_atr, atr, channel}`; `rng` = `{low, high, zone_low, zone_high, n_low, n_high, touches_low, touches_high, width_atr, pos_pct, stack_flat, sideways, reasons}`; drawn by C4.4 items 1-6 |
| `option_signal` row (A2.1, A5.2 with the contract's shapes) | the card reads NOTHING else at page time | key `(symbol, snap_on, kind, prefs_hash)`; `trend` is a **String** in `up|down|sideways|unclear`; `headline` stored at WRITE time; `setup = {kind, direction, level, zone, touches:int, quality, close, trend_days, atr, ema{e20,e50,e200}, plan{entry,stop,target,r}, levels{support,resistance}, sup, tl, tl_bounce, rng, evidence[]}` (`sup/tl/tl_bounce/rng` are C's dicts verbatim; `ema_setup.analyze()` stores them and `chart_state.read()` calls `analyze` ONCE); `iv = {iv30, hv20, hv60, iv_hv_premium, iv_rank, iv_pct, iv_n, state: none|forming|pct_only|rank_ok|ok, basis: rank|percentile|provisional|unknown, provisional, iv_front, iv_back, term_ratio (= iv_front / iv_back; TERM_EVENT 1.05, TERM_CONTANGO 0.95), skew25, skew_norm, expected_move, earnings_date, earnings_days, verdict: SELL|NEUTRAL|BUY|UNKNOWN, verdict_why, gates}`; `strategies` = flat list of ALL TEN `{key, label, fit: recommended|also_fits|rejected, score, step, why, must_happen, reasons[], reason_key, shown}`; `picks = {strategy_key: [Pick]}` |
| `OptionJob` / table `option_jobs` (A2.1, + `pushed` int) and `services/job_runs.py` (D6) | the status strip, the "job missed" notice, `/options/badge` | `job_runs.start(db, job, run_on) -> OptionJob`, `finish(db, run, *, ok, errors, rows, pushed, note, detail)`, `latest(db, job) -> OptionJob|None`, `missed(db, job) -> bool` |
| Nightly job entry point `deploy/options_nightly.py` + `TST-Options-Nightly` 07:15 MYT, 30-minute limit (A4.1) | step 5 of A4.2 is this part's push | `telegram_push.run(db, *, as_of: str, dry_run: bool=False) -> dict` (D4); the job also computes picks for every DISTINCT saved `prefs_hash` so the basket is honest on first paint (D2.2), and runs `option_exits.sweep(db)` over `option_trades` before the push (D1.14) |
| `option_words` (D2.7, `app/services/option_words.py`) | every member-facing sentence; `headline()` is called by the engine at WRITE time and stored in `option_signal.headline` | templates print, never compose |

Ownership, stated once: this part owns `app/routes/options_page.py` (A's four data endpoints live in it under this part's `verb/{symbol}` paths); A owns `deploy/options_nightly.py` and `setup_options_nightly_task.ps1`; B owns the engines, `option_prefs.py` (this part contributes the presentation columns), `option_sizing.py`, `order_ticket.py`, `option_exits.py`; C owns `trend_line.py`, `range_box.py`, `payoff.py`, `_payoff_chart.html`.

---

### D1. Routes

#### D1.1 Decision: a new module `app/routes/options_page.py`, registered BEFORE the legacy `options.py` router

Why not extend `routes/options.py`:

1. **Path collision.** `routes/options.py` already owns `prefix="/options"` with a catch-all `GET /{symbol}` (`routes/options.py:85-100`) and a form-based `POST /track` (`:156-184`). Starlette matches routes in registration order, so `GET /options/basket` and `GET /options/rules` would be swallowed by `/{symbol}` unless the fixed paths are registered first. Keeping the page in its own router and including it **before** the legacy one makes the ordering explicit and testable (D8 step 3).
2. **Different guard.** The legacy module is included with no menu gate (`main.py:229-231`, "require_user only, same as the drawings API") because the Watchlist pane's Options tab calls it (`templates/_chart_pane.html:69-73`) and its Track form posts to `/options/track` (`_options_analysis.html:129`). The page must be gated by `menus.require_menu("options")`, which 303-redirects a user lacking the key (`menus.py:141-151`). One router cannot carry two guards - so the new tracking endpoint gets **its own path, `POST /options/track-idea`**, and the legacy `POST /options/track` is left byte-for-byte untouched. A member without the new `options` grant keeps tracking from the Watchlist tab.
3. **Different lifetime.** Decision 5 keeps the old routes reachable until a later release. Leaving `options.py` unchanged is the cheapest way to guarantee the Watchlist tab keeps working while the page replaces the three pages.

`main.py` changes (all cited):

| Line | Change |
|---|---|
| `main.py:40` | add `from .routes import options_page as options_page_routes` |
| `main.py:64-70` | add `options_page_routes` to the tuple that sets `templates.env.globals["version"] / ["nav_for"] / ["gloss"]` - without it `base.html` raises `nav_for is undefined` on the first render |
| `main.py:229-231` | insert **above** `app.include_router(options_routes.router)`: `app.include_router(options_page_routes.router, dependencies=[Depends(menus.require_menu("options"))])` with a comment "fixed paths under /options must precede the legacy /options/{symbol} catch-all" |
| `main.py:241-248` | the three old page routers stay but their guards become `require_menu("positions", "options")`, `require_menu("ivscan", "options")`, `require_menu("spreads", "options")` - `user_can` is an any-of test (`menus.py:93-94`), so a member granted only the new key can still open the legacy pages by URL (decision 5) |

`menus.py` changes:

| Line | Change |
|---|---|
| `menus.py:21-33` | append `("options", "Options", None, "/options")` after `("curated", ...)` - a flat top-level item, no dropdown (the dropdown is what v4.126 removed) |
| `menus.py:47` | `HIDDEN_KEYS` unchanged: `ivscan`, `spreads`, `positions` stay granted-but-hidden (decision 5) |
| `menus.py:76-77` | `LABELS` already derives from `MENUS`; nothing to add |
| `menus.py:67` | `LANDING` unchanged |

`base.html` changes for the nav badge (the exit-line badge is currently rendered only when `/portfolio` is in `MENUS`, `base.html:371-372, 387-398, 788-829`):

| Line | Change |
|---|---|
| `base.html:371`, `:387` | the condition `href == '/portfolio'` becomes `href == '/options'`; the element keeps `id="pfNavBadge"` and the painter at `:797-829` is unchanged |
| `base.html:371`, `:397` | `hx-get="/portfolio/badge"` becomes `hx-get="/options/badge"` (D1.15), whose JSON is exactly `{run_on, finished_at, ok, errors, stale, running, job_missed, ideas_new, urgent, watch}` - `urgent / watch / stale` keep the meaning the painter already reads, now computed from `option_trade_checks` |
| `base.html:816-827` | one more chip: `if (d.job_missed) chip('⚠', 'bg-amber-500/25 text-amber-300', 'Last night\'s options data job did not run')` and `if (d.ideas_new) chip(d.ideas_new, 'bg-emerald-500/20 text-emerald-300', d.ideas_new + ' new idea(s) in your basket')` |

#### D1.2 Module skeleton

```python
"""Options - one page: basket (left), ticker card (centre), My rules (bottom).

Read path = DB only (option_signal through option_store.card_for / basket_rows_for,
option_chain_snapshot, iv_daily): nothing on this page waits on a market call.
On-demand Refresh (Cboe, ~1-2 s) and Live (the member's own IBKR bridge, posted from
the browser and graded in-request, never stored) are explicit buttons.

Replaces IV Rank (/ivscan), Spread (/spreads) and Positions (/portfolio); those
routers stay registered (menus.HIDDEN_KEYS) until a later release. This router is
included BEFORE routes/options.py in main.py so its fixed paths win over that
module's /{symbol} catch-all; the legacy POST /options/track is not touched (the new
one is POST /options/track-idea).

Nothing here places, modifies or cancels an order (DESIGN.md security posture).
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

from fastapi import APIRouter, Body, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import (OptionBasket, OptionIdeaPush, OptionJob, OptionSignal, OptionTrade,
                      OptionTradeCheck, User, _utcnow)
from ..security import require_user
from ..services import (option_prefs, option_words, option_store, option_nightly, option_sizing,
                        order_ticket, option_exits, option_data, payoff, strike_picker,
                        trend_line, range_box, job_runs, telegram_push)
from ..services import user_watchlist as uwl, trade_prefs as tp
from ..services import spread_monitor                      # et_today() only
from . import options as legacy_options                    # BRIDGE_PORT / BRIDGE_SETUP_PATH, nothing else

router = APIRouter(prefix="/options", tags=["options-page"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

BRIDGE_PORT = legacy_options.BRIDGE_PORT          # 9224, one constant
BRIDGE_SETUP_PATH = legacy_options.BRIDGE_SETUP_PATH
BRIDGE_MIN_VERSION = "1.6"  # the /iv?series=1 bootstrap; every member-facing string says 'older than 1.6'
MAX_BASKET = 60            # tickers per member; the nightly job budgets ~2.5 s per ticker (A's import cap is lowered to this)
REFRESH_COOLDOWN_S = 60    # per (member, ticker); Cboe 429s on bursts (option_quotes.py:124-126); never blocks the ticket's first 'Refresh first'
FRESH_HOURS = 20           # same idea as ivscan.IV_FRESH_HOURS: younger than this = not stale (the per-ticker dot only; stale itself comes from card_for)
CHAIN_STRIKES_EACH_SIDE = 12   # the full-chain expander's default window around spot (D1.15)
```

`MAX_BASKET = 60` is justified by the job budget: Cboe at the nightly scan's 1.5 s pause
(`app/services/spread_scan.py:321-323`, `PAUSE = 1.5`; `deploy/spread_scan.py:39` is only the
`--pause` argparse default) plus signal computation ~2.5 s per ticker; 60 tickers ~2.5 minutes per
member, inside the 30-minute execution limit `TST-Options-Nightly` is registered with (A4.1, the
same `-ExecutionTimeLimit` the portfolio check uses, `setup_portfolio_check_task.ps1`). The same
number is the cap on `POST /options/basket/import` (A6.2's 100 is lowered to match).

#### D1.3 Endpoint table

All paths follow the `verb/{symbol}` convention (A's `/options/{sym}/refresh` and
`/options/{sym}/iv/bootstrap` and C's `/options/{symbol}/payoff` are folded into these; the IV
bootstrap is a step inside `/options/live/{symbol}`).

| Method · path | Returns | Purpose | Reads | Writes |
|---|---|---|---|---|
| `GET /options` | `options.html` | page shell; `?symbol=` pre-selects a card, `?tab=positions` opens the Positions tab, `?focus=<trade_id>` (from the badge), `?pause=7` (the Telegram pause link, D4.2) | basket count only | `prefs["options_seen_at"]` (clears `ideas_new`) |
| `GET /options/basket` | `_options_basket.html` | the left column; `?sort=idea|iv|trend|added` | `option_basket`, `option_store.basket_rows_for` | - |
| `POST /options/basket/add` | `_options_basket.html` | add one typed ticker (form `symbol`, `note`) | | `option_basket` |
| `POST /options/basket/remove` | `_options_basket.html` | remove one (form `symbol`) | | `option_basket` |
| `POST /options/basket/import` | `_options_basket.html` | bulk add from `source=paste|watchlist|ivscan_list|ivscan_scan|scanner|screener|positions` (D1.5) | `prefs.ivscan_universe`, `IVScanItem`, `user_watchlist`, `spread_candidates`, open `option_trades` | `option_basket` |
| `GET /options/card/{symbol}` | `_options_card.html` | the centre card; `?strategy=&pick=` | `option_store.card_for`, `option_prefs.read`, `trade_prefs.read` | `option_signal` (lazy fill on a hash miss, inside `card_for`) |
| `GET /options/picks/{symbol}` | `_options_picks.html` | strategy chip click and rule change re-render only the picks + payoff container; `?strategy=` | same as card, no chart | same lazy fill |
| `GET /options/chart/{symbol}` | `_options_chart.html` → `_price_chart.html` | the one chart; `?strategy=&pick=` decides which overlay set | `card_for(...)["setup"]`, pick | - |
| `GET /options/payoff/{symbol}` | `_payoff_chart.html` (C4.3, server-rendered SVG) | the risk & reward pane; `?strategy=&pick=&units=$|R`; `?trade=<option_trades.id>` on the Positions tab (the dot) | pick (or the trade's legs + latest `OptionTradeCheck`), `setup.plan`, `setup.levels`, rules | - |
| `GET /options/chain/{symbol}` | `_options_chain.html` | the full stored chain behind the expander; `?expiry=&all=0|1` (D1.15) | `option_store.latest_chain` | - |
| `GET /options/ticket/{symbol}` | `_options_ticket.html` | order ticket, both broker renderings; `?strategy=&pick=&contracts=&dip=0|1` | pick, rules, setup, `card_for(...)["as_of"]` | - |
| `POST /options/refresh/{symbol}` | `_options_card.html` + `HX-Trigger: options:basket-changed` | on-demand Cboe read, re-signal (`option_nightly.refresh_symbol`) | | snapshot, `iv_daily`, signal, `option_jobs(job='refresh')` |
| `POST /options/live/{symbol}` | `_options_card.html` + `HX-Trigger` | receives the bridge's `/chain` + `/iv?series=1` + `/account` from the browser; grades in-request; persists ONLY the IV series (and the NLV on an explicit click) | | `iv_daily` (bootstrap rows), `option_jobs(job='bootstrap')`; `trade_prefs.nlv` only via `[remember this]` |
| `GET /options/rules` | `_options_rules.html` | My rules drawer; `?tab=shared|credit|debit|condor|time` | `user_option_prefs`, `trade_prefs.read` | - |
| `POST /options/rules` | `_options_rules.html` + `HX-Trigger` | save one tab's fields (D3) | | `user_option_prefs` (+ `prefs_hash`), `trade_prefs.write` for nlv / risk_pct / the four credit exit lines |
| `POST /options/rules/reset` | `_options_rules.html` + `HX-Trigger` | `tab=` clears that tab's overrides; `tab=all` clears the row | | `user_option_prefs` |
| `POST /options/track-idea` | `_options_positions_tab.html` | create the tracked trade from (`symbol, strategy, pick, contracts, note`) - its OWN path; the legacy `POST /options/track` stays untouched | pick (re-read from the signal), `option_store.latest_chain` | `option_trades` (+ the first `option_trade_checks` row, graded on the spot) |
| `GET /options/positions` | `_options_positions_tab.html` | the Positions tab over `option_trades`; `?status=open|closed&focus=` | `option_trades`, latest `option_trade_checks` per trade | - |
| `POST /options/positions/{id}/close` | `_options_positions_tab.html` | mark a trade closed (`close_reason`, `closed_at`) - the shape of `/portfolio/{id}/close` (`routes/portfolio.py:350-363`) | | `option_trades` |
| `GET /options/badge` | JSON | nav badge: `{run_on, finished_at, ok, errors, stale, running, job_missed, ideas_new, urgent, watch}` | `option_jobs` (via `job_runs`), `option_trade_checks`, `option_idea_push` | - |
| `GET /options/status/strip` | `_options_status.html` | the honesty strip, polled every 300 s, rendered from the same dict `/options/badge` returns | same | - |
| `POST /options/telegram` | `_options_rules.html` (shared tab) | opt-in, chat-id handshake (`/start` → 6-digit code), quiet switch, pause (D4.2) | `user_option_prefs.prefs["telegram"]` | same |

Query parameters are validated the same way everywhere: `symbol` through `_clean_symbol()`
(the single-ticker form of `ivscan._clean_symbols`, `routes/ivscan.py:91-102`); `strategy` must
be in `option_prefs.STRATEGY_KEYS` (`buy_call, buy_put, bull_call, bear_put, leaps_call, bull_put,
bear_call, iron_condor, calendar, diagonal_call`) else the recommended one; `pick` is an int index
clamped to the pick list; `tab` must be in `option_prefs.TABS` (`shared, credit, debit, condor,
time`); `units` in `("$", "R")`; `broker` in `("tws", "moomoo")`.

#### D1.4 Shared context builders

```python
def _basket_rows(db: Session, user: User) -> list[OptionBasket]:
    return (db.query(OptionBasket).filter(OptionBasket.owner_key == f"u{user.id}", OptionBasket.active.is_(True))
              .order_by(OptionBasket.pos, OptionBasket.symbol).all())

def _basket_context(db: Session, user: User, *, sort: str = "idea", selected: str = "") -> dict:
    """Every basket row with the three numbers the column shows (trend arrow, IV rank,
    the recommended idea) read from option_signal through ONE batched query - never a
    market call, never a per-row lookup. Sorted best idea first by default: a card with a
    recommendation and picks sorts above one with a recommendation and no strikes, above
    'not checked yet', above 'no setup', above 'no read'."""
    rows = _basket_rows(db, user)
    sigs = option_store.basket_rows_for(db, user)        # A5.3: latest row per symbol for the member's hash, else the house hash; carries stale/age_h
    items = []
    for r in rows:
        s = sigs.get(r.symbol)                            # None = no signal yet (added today)
        strategies = (s or {}).get("strategies") or []
        rec = next((x for x in strategies if x["fit"] == "recommended"), None)
        picks = (s["picks"] or {}).get(rec["key"]) if (s and rec) else None
        # THREE states, not two (CRITIQUE): None = not computed under this hash yet; [] = computed, no strike passes
        pick_state = (None if not rec else "picks" if picks else "none" if picks == [] else "unchecked")
        items.append({"row": r, "sig": s, "iv": (s or {}).get("iv"), "rec": rec, "pick_state": pick_state,
                      "age_h": s["age_h"] if s else None,
                      "stale": s is None or s["stale"],              # computed by card_for/basket_rows_for from snap_on vs et_today()
                      "idea_word": option_words.idea_short(rec) if rec else None})
    rank = {"picks": 0, "unchecked": 1, "none": 2}
    key = {"idea": lambda it: (it["rec"] is None, rank.get(it["pick_state"], 3), -((it["iv"] or {}).get("iv_rank") or 0)),
           "iv":   lambda it: (it["iv"] is None, -((it["iv"] or {}).get("iv_rank") or 0)),
           "trend": lambda it: (it["sig"] is None, {"up": 0, "down": 1, "sideways": 2, "unclear": 3}.get((it["sig"] or {}).get("trend"), 9)),
           "added": lambda it: it["row"].pos}[sort if sort in ("idea","iv","trend","added") else "idea"]
    items.sort(key=key)
    screener = _screener_suggestions(db, user, exclude={r.symbol for r in rows})   # D5.2
    return {"user": user, "items": items, "sort": sort, "selected": selected,
            "n": len(rows), "max_basket": MAX_BASKET, "screener": screener,
            "n_watchlist": len(uwl.symbol_set(db, user)),
            "n_ivscan_list": len(_ivscan_universe(user)), "n_ivscan_scan": _ivscan_scan_count(db, user),
            "n_positions": db.query(OptionTrade).filter(OptionTrade.user_id == user.id, OptionTrade.status == "open").count()}
```

`_age_hours` is the same helper as `routes/ivscan.py:158-167` (naive-UTC both sides; the
column is naive on SQLite and aware on Postgres) - it lives in `option_store` (A owns the row and
its freshness) so the two pages share one copy rather than cloning it; `card_for` /
`basket_rows_for` return `age_h` already computed and `stale` already decided.

```python
def _nlv_for(user: User, live: dict | None) -> tuple[float | None, str | None]:
    """B5.3 order: the Live figure for THIS request -> stored trade_prefs nlv (> 0) -> None.
    Never written here; the '[remember this]' click is a POST /options/rules tab=shared nlv=..."""
    if live and live.get("nlv"):
        return float(live["nlv"]), "bridge"
    stored = tp.read(user)["nlv"]
    return (stored, "prefs") if stored and stored > 0 else (None, None)

def _card_context(db: Session, user: User, symbol: str, *, strategy: str = "",
                  pick: int = 0, note: str = "", live: dict | None = None) -> dict:
    """Everything the card renders. card_for() is the ONLY thing the headline, chips,
    overlays and picks read; a hash miss is filled inside it from the stored chain (ms,
    no network). `live` is the in-request grade of a bridge payload (D1.10): its picks
    replace the stored ones for the live expiry only, and nothing from it is stored.
    `note` carries a one-line status from the POST that re-rendered the card
    ('Refreshed from Cboe 16:04 ET', 'Bridge not reachable - showing delayed data')."""
    sym = _clean_symbol(symbol)
    card = option_store.card_for(db, sym, user)          # None when no signal row exists yet
    prefs = option_prefs.read(db, user)
    in_basket = db.query(OptionBasket).filter(OptionBasket.owner_key == f"u{user.id}",
                                              OptionBasket.symbol == sym).first() is not None
    strategies = list(card["strategies"] or []) if card else []
    rec = next((x for x in strategies if x["fit"] == "recommended"), None)
    strategy = strategy if strategy in option_prefs.STRATEGY_KEYS else (rec["key"] if rec else "")
    chosen = next((x for x in strategies if x["key"] == strategy), None)
    picks = (live["picks"] if live else ((card["picks"] or {}).get(strategy) if card else None))
    nlv, nlv_source = _nlv_for(user, live)
    for p in (picks or []):
        p["sizing"] = option_sizing.size(p, nlv, prefs)   # READ time, microseconds; 0 contracts is an honest answer
        p["sizing"]["nlv_source"] = nlv_source
    pick_i = max(0, min(pick, len(picks) - 1)) if picks else 0
    chips = option_words.chip_row(strategies)            # D2.3: recommended, also_fits, <=2 near-miss rejects (shown=True), rest
    return {"user": user, "sym": sym, "card": card, "prefs": prefs, "in_basket": in_basket,
            "strategy": strategy, "chosen": chosen, "rec": rec, "family": option_prefs.family_of(strategy) if strategy else None,
            "chips": chips, "picks": picks, "pick_i": pick_i, "live": live,
            "headline": card["headline"] if card else None,                 # stored at WRITE time by the engine; never recomposed here
            "gauge": option_words.gauge(card["iv"]) if card else None,     # basis-aware wording (D2.7)
            "must_happen": chosen["must_happen"] if chosen else None,
            "earnings": option_words.earnings_state(card["iv"], chosen, picks, prefs) if card else None,  # {date, days, inside, rule, hide_strikes}
            "age": option_words.age_badge(card, live),                        # D6: 'as of Oct 2, 16:00 ET · delayed' or 'live · TWS 21:42 ET'
            "bridge_port": BRIDGE_PORT, "bridge_setup_path": BRIDGE_SETUP_PATH, "bridge_min_version": BRIDGE_MIN_VERSION,
            "note": note, "nlv": nlv, "nlv_source": nlv_source, "trade_prefs": tp.read(user)}
```

`_picks_context` is `_card_context` minus `headline/gauge/chips/age` (the chip click must not
recompute the sentence; the headline is a stored string anyway); `_rules_context(db, user, tab)` is
in D3. Neither builder ever queries `OptionSignal` directly: `one_or_none()` would raise on the
second member under A's `(symbol, snap_on, kind, prefs_hash)` key.

#### D1.5 Basket endpoints

```python
class BasketImport(BaseModel):
    source: str = "paste"            # paste | watchlist | ivscan_list | ivscan_scan | scanner | screener | positions
    text: str = ""                   # paste: commas / spaces / newlines (ivscan.UniverseIn shape)
    symbols: list[str] = Field(default_factory=list)   # scanner / screener: what the browser chose
    note: str = ""

@router.post("/basket/import", response_class=HTMLResponse)
def basket_import(payload: BasketImport, request: Request,
                  user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Bulk add. Sources (the contract's list; 'ivscan' alone is ambiguous in this repo, so two names):
      paste        - typed text, cleaned exactly as /ivscan/universe cleans it (re, _clean_symbols)
      watchlist    - user_watchlist.symbols(db, user)                       (services/user_watchlist.py:28)
      ivscan_list  - prefs['ivscan_universe'] (the old IV Rank 'My list')    (routes/ivscan.py:85,105-107)
      ivscan_scan  - IVScanItem rows for this user, order pos                (models.py:904-915: the TWS scanner's stored output)
      scanner      - payload.symbols, posted by the browser after it ran the bridge /scan
                     (the SAME browser flow as ivscan.html:268-297; the server never reaches TWS)
      screener     - payload.symbols chosen from the nightly spread_candidates list (D5.2)
      positions    - symbols of this member's OPEN option_trades rows (so a tracked trade's chain is always fresh)
    Existing rows are kept (their signal is not reset); new ones get pos = max+1,
    owner_key = f"u{user.id}", added_on = spread_monitor.et_today(), active = True and
    source = payload.source. Returns the basket fragment; the response carries
    HX-Trigger 'options:basket-changed' so the status strip updates its count."""
```

Rules: cap at `MAX_BASKET` (the response says how many were dropped, as `/ivscan/universe`
reports `dropped`, `routes/ivscan.py:307`); duplicates ignored; `source` stored verbatim from
the allow-list `typed | paste | watchlist | ivscan_list | ivscan_scan | scanner | screener | sector |
positions | system`. `add` is `import(source="typed")` with one symbol. `remove` sets nothing on the
shared tables - it deletes the member's `option_basket` row and nothing else; the ticker's
`option_signal` / snapshot are shared across members and are left for the nightly job's retention
to expire (`option_store.prune`, A2.4). A ticker that is in nobody's basket is simply not refreshed
next night (`option_store.basket_universe` = distinct ACTIVE symbols over all owners, A4.2).

`HX-Trigger` on these responses: `{"options:basket-changed": {"n": <count>}}`. The page's
status strip and the card's "+ add to basket" button listen for it (`hx-trigger="options:basket-changed from:body"`).

#### D1.6 Card, picks, chart

```python
@router.get("/card/{symbol}", response_class=HTMLResponse)
def card(symbol: str, request: Request, strategy: str = "", pick: int = 0,
         user: User = Depends(require_user), db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "_options_card.html",
                                      _card_context(db, user, symbol, strategy=strategy, pick=pick))

@router.get("/picks/{symbol}", response_class=HTMLResponse)
def picks(symbol: str, request: Request, strategy: str = "", pick: int = 0,
          user: User = Depends(require_user), db: Session = Depends(get_db)):
    """The strike table + payoff container for ONE strategy. Served separately from
    the card so a chip click (or a rule change) never remounts the chart. The chosen
    row's fit / reason_key decide the banner, whether the strike table is shown at all
    (earnings_inside hides it) and which button is primary (D2.5)."""

@router.get("/chart/{symbol}", response_class=HTMLResponse)
def chart(symbol: str, request: Request, strategy: str = "", pick: int = 0,
          user: User = Depends(require_user), db: Session = Depends(get_db)):
    """The one chart. Context built by sector._chart_ctx (routes/sector.py:368-388 - MATP
    band, curated call) PLUS the option overlays, all from the SIGNAL's setup dict (C's
    dicts verbatim, stored by the engine - the line on the chart is the line the chip
    talks about, and no detector re-runs on a request):
      chart_bounce    = the dict _bounce_overlay builds (routes/sector.py:352-365) from setup.sup
      chart_trendline = trend_line.overlay(setup.tl, setup.tl_bounce)   (C4.4 items 1-4, 6)
      chart_range     = range_box.overlay(setup.rng)                    (C4.4 RANGE block)
      chart_spread    = {legs: [{strike, right, side, label}], breakevens: pick.breakevens, expiry, label}
                        for the credit_vertical / condor / time families (C4.4 item 5: SPREAD.legs)
      chart_levels    = {entry: plan.entry, stop: plan.stop, target: plan.target} for debit_vertical / long / leaps
                        (read-only; chart_setup_seed deliberately NOT set so the levels are not editable
                        here - the setup detector owns them)."""
```

The chart fragment `_options_chart.html` sets, in this order (mirroring
`_sector_chart.html:6-61` and `_portfolio_chart.html:13-32`):

```jinja
{% set chart_symbol = symbol %} ... {% set chart_fill = true %}
{% set chart_price_min = 'lg:min-h-[300px]' %}       {# same floor as the curated chart #}
{% set chart_band = sel_band %}{% set chart_band_start_closed = true %}
{% set chart_tv_plot = true %}{% set chart_matp_run = true %}{% set chart_curate_setup = false %}
{% set chart_bounce = bounce %}                       {# or none #}
{% set chart_trendline = trendline %}                 {# trend_line.overlay(), C4.4 #}
{% set chart_range = range %}                         {# range_box.overlay(), C4.4 #}
{% if family in ('credit_vertical','condor','time') %}{% set chart_spread = spread %}{% endif %}
{% if family in ('debit_vertical','long','leaps') %}{% set chart_levels = levels %}{% endif %}
{% set chart_refresh = {'url': '/options/chart/...', 'target': '#optChartBody'} %}
{% include "_price_chart.html" %}
```

`chart_curate_setup = false`: the card is where the system has already curated; a "Curate
setup" button here would put a system idea into the member's own Curated list under their name,
which §3 keeps separate ("Track this" is the hand-over, not Curate). The trend line's value at the
pick's expiry (`tl.value_at[expiry]`) is already on the stored dict because the nightly job calls
`trend_line.find(..., at=[every expiry in the snapshot])`; the chart never extrapolates it itself.

#### D1.7 Payoff route (C's engine and partial; no JSON contract of its own)

```python
@router.get("/payoff/{symbol}", response_class=HTMLResponse)
def payoff_pane(symbol: str, request: Request, strategy: str = "", pick: int = 0, units: str = "$",
                trade: int = 0, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Thin: load legs -> payoff.build(...) -> _payoff_chart.html (C3.11). The browser never
    draws a payoff itself; the $|R toggle re-requests this URL with units=R."""
    if trade:                                                   # Positions tab: the dot
        t = db.get(OptionTrade, trade); _own(t, user)
        legs = [payoff.Leg.from_dict(dict(l, price=l["entry_price"], iv=l["entry_iv"])) for l in t.legs]
        chk = t.checks[-1] if t.checks else None
        po = payoff.build(legs, family=t.family, spot=chk.spot if chk else None, atr=_atr_for(db, t.symbol), today=spread_monitor.et_today(),
                          contracts=t.contracts, chart_stop=t.chart_stop, target=t.chart_target,
                          rule_frac=_rule_frac(t.family, option_prefs.read(db, user), t), pl_now=chk.pl if chk else None)
    else:
        ctx = _picks_context(db, user, symbol, strategy=strategy, pick=pick)
        p, setup, iv = ctx["picks"][ctx["pick_i"]], ctx["card"]["setup"], ctx["card"]["iv"]
        legs = [payoff.Leg.from_dict(l) for l in p["legs"]]     # qty = +qty for buy, -qty for sell
        po = payoff.build(legs, family=ctx["family"], spot=setup["close"], atr=setup["atr"], today=spread_monitor.et_today(),
                          contracts=(p["sizing"] or {}).get("contracts") or 0,   # 0 = per-contract numbers (C3.12), never a forced 1
                          chart_stop=p["chart_stop"],                           # = setup.plan.stop (B2.6): 336.2 on the LRCX fixture
                          target=setup["plan"]["target"] if ctx["family"] in ("debit_vertical", "long", "leaps") else None,
                          rule_frac=_rule_frac(ctx["family"], ctx["prefs"]),    # B5.2 table: credit 20% of max loss; long/debit/time premium_stop_pct; leaps None
                          levels=_levels_at(setup, p["expiry"]),               # support / resistance / tl.value_at[expiry] / rng edges
                          sigma_fallback=(iv.get("iv30") or 0) / 100 or None)   # iv_daily is PERCENT; payoff takes a fraction
    po["units"]["mode"] = "R" if units == "R" and po["units"].get("r_dollars") else "$"
    return templates.TemplateResponse(request, "_payoff_chart.html",
                                      {"po": po, "pane_url": request.url.remove_query_params("units")})
```

What the partial shows (C3.9-C3.10, C4.2 - restated here only where the card depends on it):

- `max_loss` and `max_profit` are **positive** magnitudes; the legend prints `max +$210 / −$790`.
- **Both stops are drawn and labelled** (decision 7): the chart stop (`setup.plan.stop`: credit =
  `zone_lo − LEVEL_PAD_ATR × ATR`, debit = `min(entry − STOP_ATR × ATR, zone_lo − pad)`; **336.2**
  for the LRCX fixture - the mockup's 338 was a 0.1-ATR pad and is replaced everywhere) and the rule
  stop for every family that has a $ rule in B5.2 (credit 20% of max loss, long / debit / time
  `premium_stop_pct` of the debit; LEAPS has none, so no rule-stop marker).
- `R` = 20% of max loss for the credit families, `|pnl_today(chart_stop)|` for the debit families
  (C3.9); the server renders `$` or `R`, the client never recomputes.
- `today` is the T+0 curve at `t = 0`; `pnl / leg_value / curve_at` accept `iv_bump` (B5 uses it
  for the stop valuation).
- `pop = {label, value, basis, model, model_basis}`: the title reads `About a 74% chance of keeping
  the credit` and the secondary figure `model estimate 73%` (D2.7).
- Caption, verbatim: **"Dashed line: what the trade would be worth if the stock moved there today, at
  today's implied volatility - an estimate. Solid line: at expiry ({dte} days)."** For calendars /
  diagonals: "Drawn at the near expiry ({front}); the far option is valued by the model, so the solid
  line is an estimate too."
- Failure: `po.error` / `po.warnings` render inside the same pane height ("Could not draw the payoff
  (no stored quotes for these strikes). Press Refresh." in amber); never a blank box.

The client-side painter and the 121-point JSON an earlier draft of this part specified do not
exist; `#optPayoff` is an HTMX target that receives C's partial.

#### D1.8 Rules endpoints - see D3.

#### D1.9 Refresh

```python
@router.post("/refresh/{symbol}", response_class=HTMLResponse)
def refresh(symbol: str, request: Request, strategy: str = "",
            user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Read today's delayed chain now (Cboe, ~1-2 s), store it, recompute the signal
    (house hash AND this member's hash), re-render the card. Rate-limited per (member,
    ticker) to REFRESH_COOLDOWN_S: Cboe answers 429 to bursts (option_quotes.py:124-126)
    and a member double-clicking must not take the feed away from everyone else's page.
    The cooldown is consulted ONLY when the stored as_of is already from the current
    session: when the card is older than the last session close (the ticket's 'Refresh
    first' case, D2.8) the first refresh always goes through."""
    sym = _clean_symbol(symbol)
    card = option_store.card_for(db, sym, user)
    if card and not _older_than_last_close(card["as_of"]) and _cooldown_hit(user.id, sym):
        return Response(status_code=429, headers={"HX-Trigger": json.dumps(
            {"options:toast": {"kind": "err", "msg": "Just refreshed - try again in a minute."}})})
    try:
        option_nightly.refresh_symbol(db, sym, user)         # A4.7: kind='intraday', fresh=True, retries=0; writes option_jobs(job='refresh')
        note = "Refreshed from Cboe · " + option_words.et_clock()
    except option_data.ChainError as exc:                    # the Cboe reader's error (option_quotes.ChainError wrapped by A1.3)
        note = f"Could not refresh: {exc}. Showing the stored data."
    ctx = _card_context(db, user, sym, strategy=strategy, note=note)
    resp = templates.TemplateResponse(request, "_options_card.html", ctx)
    resp.headers["HX-Trigger"] = json.dumps({"options:basket-changed": {}})
    return resp
```

`_cooldown_hit` is an in-process dict `{(user_id, sym): monotonic}`; good enough for one
uvicorn worker (the app runs single-worker on Hermes, `deploy/run_app.ps1`).
`_older_than_last_close(as_of)` = `as_of < 16:00 ET of the last ET trading day`
(`spread_monitor.et_today()` minus the weekend/holiday step the sweep uses, `spread_monitor.py:204-242`).

#### D1.10 Live (the bridge) - graded in-request, never stored

The browser does exactly what `_options_tab.html:93-128` does - `Promise.all([get('/chain?…'), get('/iv?symbol=…&series=1'), get('/account')])` - and POSTs the three results here. The bridge's `/chain` returns one expiry, ±10 strikes or the put side (`bridge/ibkr_bridge.py:419-540`); its `/iv` today returns only the summary (`:541-566`). **Bridge change required (bridge 1.6; `server_version` is `TradeHunterIBKRBridge/1.5` today, `ibkr_bridge.py:631` - 1.4 was open interest per leg, 1.5 no fixed waits):** `/iv?symbol=X&series=1` adds `"series": [{"on": b.date.isoformat(), "iv": round(b.close*100, 1)}, ...]` - **PERCENT**, the unit its own `iv_current / iv_low / iv_high` already use (`ibkr_bridge.py:559-563`), oldest first, <= 400 points - from the same `reqHistoricalDataAsync(... OPTION_IMPLIED_VOLATILITY ...)` call it already makes; the bars are already in `vals`, only the dates are dropped today. Without `series=1` the response is unchanged, so the IV Rank page and the Watchlist tab keep working on either version.

```python
class LiveIn(BaseModel):
    chain: dict = Field(default_factory=dict)   # bridge /chain response, untrusted
    iv: dict = Field(default_factory=dict)      # bridge /iv response: iv_current, iv_rank, iv_percentile (+ series when the bridge is >= 1.6)
    nlv: float | None = None                    # /account net_liquidation - sizes THIS request only
    diag: dict | None = None                    # what the browser saw when the loopback fetch failed

@router.post("/live/{symbol}", response_class=HTMLResponse)
def live(symbol: str, payload: LiveIn, request: Request, strategy: str = "",
         user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Grade what the member's own TWS said, in this request, and show it - store nothing but the IV series.
    1. chain.ok and chain.spot: src = option_data.BridgePayloadSource(payload.chain)   (A1.5: iv/100, caps, no network)
       chain_view = opt_legs views over src.legs(); chart/gauge from card_for (stored); prefs = option_prefs.read
       res = strike_picker.pick(strategy, chain_view, chart, gauge, prefs, nlv=payload.nlv, today=et_today())
       for p in res.picks: p.sizing = option_sizing.size(p, payload.nlv or stored nlv, prefs)
       -> live = {"picks": res.picks, "expiry": the ONE live expiry, "at": 'HH:MM ET', "nlv": payload.nlv}
       The card renders with the badge 'live · TWS 21:42 ET' and the picks table RESTRICTED to the live
       expiry; the stored (delayed) expiries sit behind 'show delayed expiries' (D2.5) so no row ever
       mixes two moments without saying so. NOTHING is written to option_chain_snapshot (decision 1,
       A1.5): the next GET /options/card/{sym} shows the stored card again.
    2. iv.series present (bridge >= 1.6): option_store.bootstrap_iv(db, sym, series, source='ibkr')
       - PERCENT as sent, each point bounded 0.1 <= iv <= 1000 (_b style, routes/ivscan.py:351-356),
       <= 400 points, dates ISO and <= today; a day the server read itself is never overwritten;
       writes option_jobs(job='bootstrap'); marks the house signal status='stale_iv' so the next
       card_for recomputes the gauge from the now-full window (A4.6). This is the decision-4
       bootstrap and the ONLY persisted artefact of a Live press.
       iv.series absent: note += "Your bridge is older than 1.6, so the one-year IV history was not copied."
    3. iv.iv_rank present: shown IN-REQUEST next to the server-side figure ('IV rank 62 (TWS, live) ·
       59 (delayed)'); not stored on the signal.
    4. payload.nlv: used for this request's sizing and labelled 'from TWS · [remember this]'. NEVER
       written here. The [remember this] click posts tab=shared, nlv=<figure> to POST /options/rules
       (D3.3), i.e. trade_prefs.write(db, user, nlv=...) - the same write Curated uses; nothing is
       stored without that click (B5.3).
    5. re-render the card with note='Live from your TWS HH:MM ET' and live=live.
    chain.ok false: the card re-renders unchanged with note = the calm bridge panel text
    (D2.9) and diag shown in a <details>, exactly as _options_analysis.html:6-62 does."""
```

Every number is re-validated (`_b(v, lo, hi)` as in `routes/ivscan.py:351-356`) because the body
comes from a browser. The bridge lives on the member's PC, so on touch / narrow viewports
(`< lg`, or `navigator.maxTouchPoints > 0`) the Live button is **hidden** with the hint
"Live quotes need TWS on your PC" (D2.1, D2.9).

#### D1.11 Track this - `POST /options/track-idea`

```python
@router.post("/track-idea", response_class=HTMLResponse)
def track_idea(request: Request, symbol: str = Form(...), strategy: str = Form(...), pick: int = Form(0),
               contracts: int = Form(..., ge=1, le=500), note: str = Form(""),
               user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Record the chosen pick as a tracked trade in option_trades (B7.1) - for EVERY strategy
    from step 1 (bull_put, bear_call, the debit family, condor, time: generic legs). Its own
    path: the legacy POST /options/track (routes/options.py:156-184, the Watchlist pane's form)
    is untouched and keeps writing option_spreads for that tab.

    The legs are NOT trusted from the browser: the pick is re-read from the signal row for this
    member's prefs hash (_picks_context) so what is tracked is what was shown. If the row no
    longer holds it (rules changed in another tab, the nightly job wrote a new day)
    -> 409 + toast 'The strikes changed - look at the card again before tracking.'

    A REJECTED strategy (chosen.fit == 'rejected'):
      reason_key == 'earnings_inside' and not (prefs.shared.earnings_rule == 'defined_risk_only'
        and option_prefs.defined_risk(strategy)) -> 409 + toast 'Earnings {date} fall inside this
        trade and your rule says no - nothing to track.' (the same gate hides the strikes, D2.5)
      any other reason -> allowed, and the rejection sentence (chosen.reasons[0]) is the FIRST line
        of the trade's note: 'Not recommended: options are expensive (IV rank 62). ' + the member's note.

    contracts: the form value, bounded; the box was pre-filled from pick.sizing.contracts and the
    form cannot submit at 0 (the button is disabled with the sizing note, D2.5). Never recomputed here.

    Row (B7.1 shape, nothing added): user_id, symbol, strategy, family = option_prefs.family_of(strategy),
      legs = [leg | {entry_price: leg.price, entry_delta: leg.delta, entry_iv: leg.iv}] (the Leg keys, `oi` spelled as opt_legs.norm_leg leaves it),
      front_expiry / back_expiry, net_entry = pick.net (negative = credit), contracts, max_loss = pick.max_loss,
      chart_stop = pick.chart_stop (= setup.plan.stop), chart_target = setup.plan.target (debit families) else None,
      roll_dte = prefs.leaps.roll_dte for leaps_call / diagonal_call, paper = False,
      signal_id = card.id (so a review can say what the system saw: that row carries prefs_hash and as_of),
      note as above; the per-trade exit overrides stay NULL (= the family's rule).
    Then option_exits.mark(trade, option_store.latest_chain(db, sym), today) + grade(...) write the first
    option_trade_checks row from the STORED chain (no market call), so the Positions tab shows the row
    graded immediately.
    Response: the Positions tab fragment with focus=<new id> and
    HX-Trigger {"options:tracked": {"id": ..}, "options:toast": {...}}; the page JS switches to the
    Positions tab on options:tracked."""
```

Where the row goes, stated once (the contract): **every** tracked strategy → `option_trades` +
`option_trade_checks`, graded by `option_exits` (generic legs cover bear_call now - it marks against
the CALL chain because the legs say `right='C'`; no change to `spread_monitor.snapshot` is needed,
and `spread_monitor` is NOT changed). `option_spreads` stays read-only for the legacy `/portfolio`
until removal; the migration's data step copies its OPEN rows into `option_trades` once (D1.12).

#### D1.12 Storage this part owns (SQLAlchemy sketches, portable types only)

This part owns two tables and one column; the rest it reads are A's and B's.

```python
class OptionBasket(Base):
    """One ticker one member (or the system) studies on the Options page (§2.1) - A2.1's shape
    (owner_key keeps the system basket without a NULL in a UNIQUE) plus this part's pos column.
    Separate from user_watchlist: different cadence (nightly chain + signal) and data cost."""
    __tablename__ = "option_basket"
    __table_args__ = (UniqueConstraint("owner_key", "symbol", name="uq_option_basket_owner_symbol"),)
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    owner_key = Column(String(16), nullable=False, index=True)        # f"u{user_id}" | "system"
    symbol = Column(String(20), nullable=False, index=True)
    source = Column(String(12), nullable=False, default="typed")      # typed|paste|watchlist|ivscan_list|ivscan_scan|scanner|screener|sector|positions|system
    note = Column(Text, nullable=True)
    active = Column(Boolean, nullable=False, default=True)             # off = kept, not fetched
    added_on = Column(String(10), nullable=False)                      # ET date string (spread_monitor.et_today())
    pos = Column(Integer, nullable=False, default=0)                   # the member's order (D)
    created_at = Column(DateTime, default=_utcnow)
    user = relationship("User")

class OptionIdeaPush(Base):
    """One Telegram push per (member, idea). The key is what makes 'once per new idea' mean
    something (D4): idea_key = 'LRCX|bull_put|2026-11-20' (symbol, strategy, front expiry);
    short_strike / atr let run() re-push ONLY when the short strike moved > 1 ATR, by updating
    this row (sent_at, short_strike) rather than inserting a second one."""
    __tablename__ = "option_idea_push"
    __table_args__ = (UniqueConstraint("user_id", "idea_key", name="uq_option_idea_push"),)
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    symbol = Column(String(20), nullable=False, index=True)
    idea_key = Column(String(80), nullable=False)
    short_strike = Column(Float, nullable=True)
    atr = Column(Float, nullable=True)
    score = Column(Float, nullable=True)
    sent_at = Column(DateTime, default=_utcnow)
    ok = Column(Boolean, nullable=False, default=True)
    error = Column(Text, nullable=True)                                # 'dry-run' on a --telegram-dry-run
```

`OptionJob` (`option_jobs`, A2.1) gains one column, `pushed = Column(Integer, nullable=False,
default=0)` (Telegram messages sent by that run); `services/job_runs.py` (D6) is the service over
it. `UserOptionPrefs` is A's shape (`prefs` JSON sparse overrides, `prefs_hash`, `schema_version`,
`updated_at`) - this part adds nothing to it; the Telegram settings live inside `prefs["telegram"]`
(not a schema block, never hashed, D4.2). `OptionTrade` / `OptionTradeCheck` are B7.1's.

Alembic: **ONE migration for the whole module, `alembic/versions/f4a5b6c7d8e9_options_module.py`,
`revision = "f4a5b6c7d8e9"`, `down_revision = "e2f3a4b5c6d7"`** (verified head: only
`e2f3a4b5c6d7_iv_scan_items.py` revises `d1e2f3a4b5c6`; `app/db.py:94` runs `upgrade head` at
startup and would raise on a branched head). A's skeleton with the per-table
`get_table_names()` guard (`e2f3a4b5c6d7_iv_scan_items.py:21-22` style, because `init_db()` may run
against the legacy `create_all` DB on Hermes), creating **nine tables**: `option_basket`,
`option_chain_snapshot`, `iv_daily`, `option_signal`, `user_option_prefs`, `option_jobs`,
`option_trades`, `option_trade_checks`, `option_idea_push`, plus B7.1's data step that copies every
OPEN `option_spreads` row into `option_trades` (`strategy="bull_put"`, `family="credit_vertical"`,
two put legs from `short_strike/long_strike/short_price/long_price`, `net_entry = -credit`, overrides
carried over, `note="migrated from option_spreads #<id>"`). No other migration id exists for this
module; every part cites `f4a5b6c7d8e9`.

#### D1.13 Ticket

```python
@router.get("/ticket/{symbol}", response_class=HTMLResponse)
def ticket(symbol: str, request: Request, strategy: str = "", pick: int = 0, contracts: int | None = None,
           dip: int = 0, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Both broker renderings of the order ticket for the chosen pick (D2.8). Server-rendered
    so the numbers are the pick's, not whatever the DOM held.
      ctx = _picks_context(...); p = ctx.picks[pick_i]; chosen = ctx.chosen
      rejected for earnings_inside (and not allowed by defined_risk_only on a defined-risk strategy)
        -> the panel renders ONE line: 'No ticket: earnings {date} fall inside this trade and your rule
           says no.' - no legs, no prices (test: a rejected-for-earnings strategy produces no ticket)
      p.sizing.contracts = bounded form value or the sizing figure (0 -> the panel shows the sizing
        note and no orders); p.enter_on_dip = bool(dip)             # the explicit toggle, default OFF
      t = order_ticket.build(p, ctx.card.setup, ctx.prefs)            # B6.1; condition None unless enter_on_dip
      if chosen.fit == 'rejected': t.first_line = chosen.reasons[0]  # the rejection sentence is the ticket's first line
      refresh_first = _older_than_last_close(ctx.card.as_of) and _us_session_open()
      render _options_ticket.html with tws = order_ticket.render(t, 'tws'), moomoo = order_ticket.render(t, 'moomoo'),
        as_of, refresh_first, dip, chosen, the footer lines (D2.8)."""
```

#### D1.14 Positions tab

```python
@router.get("/positions", response_class=HTMLResponse)
def positions(request: Request, status: str = "open", focus: int = 0,
              user: User = Depends(require_user), db: Session = Depends(get_db)):
    """The Positions tab, inside /options, over option_trades (B7.1) - every strategy, one
    template. The shape of portfolio._list_context (routes/portfolio.py:88-146) rebuilt on the
    generic rows: the board, a chart pane for the focused trade (chart_spread.legs from
    trade.legs), a per-row drawer with the latest OptionTradeCheck (mark, P/L, Δ, theta, dte,
    state, action, reasons) and the check history, and 'Mark closed'. No market call: the
    sweep wrote the checks; a trade tracked today carries the check written on Track."""
    trades = (db.query(OptionTrade).filter(OptionTrade.user_id == user.id, OptionTrade.status == status)
                .order_by(OptionTrade.front_expiry, OptionTrade.symbol).all())
    latest = _latest_checks(db, [t.id for t in trades])          # one query: newest OptionTradeCheck per trade
    rows = [{"trade": t, "check": latest.get(t.id),
             "verdict": _verdict(t, latest.get(t.id), db, user)}  # the check's state/action/reasons + the earnings-now-inside row (below)
            for t in trades]
    ctx = {"user": user, "rows": rows, "status": status, "focus": focus or (rows[0]["trade"].id if rows else None),
           "inside_options": True, "n_urgent": sum(r["verdict"]["urgent"] for r in rows)}
    return templates.TemplateResponse(request, "_options_positions_tab.html", ctx)

@router.post("/positions/{trade_id}/close", response_class=HTMLResponse)
def close_trade(trade_id: int, request: Request, reason: str = Form(""), status: str = Form("open"),
                user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Same shape as /portfolio/{id}/close (routes/portfolio.py:350-363): status='closed',
    closed_at=_utcnow(), close_reason=reason[:24]; re-renders the tab. Tracking only."""
```

The nightly grading is `option_exits.sweep(db)` (B7.3: `spread_monitor.sweep`'s loop over
`option_trades`, one chain fetch per underlying, per-member prefs, `record_check` upsert per ET
day), run as a step of `deploy/options_nightly.py` between the per-symbol loop and the push (A4.2);
`spread_monitor.sweep` keeps grading the legacy `option_spreads` rows for `/portfolio` unchanged.
`_verdict` adds, for every family, the **"earnings now inside"** row the exits table carries
(B7): when `card_for(sym).iv.earnings_date <= trade.front_expiry` and the member's rule does not
allow it → `WATCH` (urgent) with the text *"Earnings {date} now fall inside this trade (the date
was unknown or later when you entered). Decide before the close that day."* - evaluated at read
time from the stored signal (no market call) so a date that appeared after entry is surfaced the
morning it appears; the same row counts in `/options/badge`'s `urgent`.

#### D1.15 Status, badge, chain

```python
@router.get("/status/strip", response_class=HTMLResponse)
def status_strip(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """The honesty line (§7): data age, delayed/live, job health. Polled every 300 s. Renders
    _options_status.html from the SAME dict badge() returns (D6)."""

@router.get("/badge")
def badge(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Nav badge JSON, exactly {run_on, finished_at, ok, errors, stale, running, job_missed,
    ideas_new, urgent, watch}: run_on..errors from job_runs.latest(db, 'nightly') (option_jobs);
    stale = run_on < et_today(); running = finished_at is None and started_at < 40 min ago;
    job_missed = job_runs.missed(db, 'nightly') (D6); urgent / watch = counts over the newest
    option_trade_checks row of each OPEN option_trades row of this member (state CLOSE/ROLL with
    urgent, or WATCH) plus the earnings-now-inside rows; ideas_new = OptionIdeaPush rows for this
    member with sent_at >= prefs['options_seen_at'] (cleared when GET /options renders).
    Stored checks only, never a quote (the /portfolio/badge principle, routes/portfolio.py:430-434)."""

@router.get("/chain/{symbol}", response_class=HTMLResponse)
def chain(symbol: str, request: Request, expiry: str = "", all: int = 0,
          user: User = Depends(require_user), db: Session = Depends(get_db)):
    """The full stored chain for the expander (§7: 'nobody sees a raw chain by default').
    Defaults: the pick's expiry (the ?expiry= the picks fragment passes) and
    CHAIN_STRIKES_EACH_SIDE = 12 strikes either side of spot (<= 25 rows, calls | strike | puts);
    an expiry picker (every expiry in the snapshot, DTE beside it) and a 'show all strikes'
    link per expiry (?all=1) - never more than ~60 rows render without a click (A2.3: MSFT
    3,710, SPY ~15k contracts per snapshot would otherwise ship to a phone). Table markup lifted
    from _options_analysis.html:349-411, with the chosen pick's legs highlighted as that
    template highlights _cs/_cl (:305-306). Gamma is shown ONLY here (D2.7)."""
```

---

### D2. Templates

All new templates live in `app/templates/`. Context variables are listed per template; HTMX
attributes are written as they appear in the markup. The light-theme ink additions and the
scrollbar rule are in D2.10.

#### D2.1 `options.html` - the page shell

Extends `base.html`; `{% block main_class %}flex-1 w-full px-4 py-4 overflow-hidden{% endblock %}`
(as `ivscan.html:3`). Context: `user, n_basket, symbol (pre-selected or ''), tab ('ideas'|'positions'), focus, max_basket, pause_days`.

```
┌ header strip  #optStatus (hx-get /options/status/strip, load, every 300s) ─────────────────────┐
│ Options · 5 tickers · Data as of Oct 2, 16:00 ET · delayed · job ✓ 07:17 MYT  [Refresh] [Live (TWS)] │
├──────────┬──────────────────────────────────────────────────────────────────────────────────────┤
│ #optBasket│ tabs: [Ideas] [Positions ●2]                        #optPane                        │
│ 200px     │ #optCard  (Ideas)  OR  #optPositions (Positions) - only one is in the DOM          │
│ lg:w-[200px]│                                                                                   │
├──────────┴──────────────────────────────────────────────────────────────────────────────────────┤
│ <details id="optRules">  MY RULES  tabs: Shared · Credit spreads · Buy call/put · Iron condor · Time spreads  [Reset] │
└─────────────────────────────────────────────────────────────────────────────────────────────────┘
```

Markup skeleton:

```jinja
<div class="lg:flex lg:flex-col lg:h-[calc(100vh-6rem)]">
  <div id="optStatus" class="shrink-0 mb-2" hx-get="/options/status/strip" hx-trigger="load, every 300s" hx-swap="innerHTML"></div>
  <div class="lg:flex lg:gap-3 lg:flex-1 lg:min-h-0">
    <aside class="lg:w-[200px] lg:shrink-0 flex flex-col mb-3 lg:mb-0 lg:min-h-0">
      <div id="optBasket" class="opt-basket flex-1 min-h-0 overflow-y-auto rounded-xl border border-slate-800 bg-slate-900/40 p-1.5"
           hx-get="/options/basket" hx-trigger="load, options:basket-changed from:body" hx-swap="innerHTML">
        <div class="text-xs text-slate-500 py-6 text-center">Loading&hellip;</div>
      </div>
    </aside>
    <section class="flex-1 min-w-0 rounded-xl border border-slate-800 bg-slate-900/40 p-3 flex flex-col lg:min-h-0">
      <div class="flex items-center gap-1 mb-2 shrink-0">
        <button type="button" class="optTab ..." data-tab="ideas">Ideas</button>
        <button type="button" class="optTab ..." data-tab="positions">Positions <span id="optPosCount" class="..."></span></button>
        <span id="optCurSym" class="ml-2 text-[11px] text-slate-500"></span>
      </div>
      <div id="optPane" class="flex-1 min-h-0 overflow-y-auto flex flex-col">
        {# filled by the tab script: /options/card/<sym> or /options/positions #}
      </div>
    </section>
  </div>
  <details id="optRules" class="shrink-0 mt-3 rounded-xl border border-slate-800 bg-slate-900/40">
    <summary class="cursor-pointer select-none px-3 py-2 text-[11px] text-slate-300">My rules <span class="text-slate-500" id="optRulesSummary">— house defaults</span></summary>
    <div id="optRulesBody" class="opt-rules max-h-[40vh] overflow-y-auto px-3 pb-3"
         hx-get="/options/rules?tab=shared" hx-trigger="toggle from:closest details once" hx-swap="innerHTML"></div>
  </details>
</div>
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
{% include "_at_modal.html" %}
```

**HTMX gotcha (CRITIQUE):** the DOM `toggle` event is dispatched on the `<details>` element and
does not bubble to its children, so a bare toggle trigger on the body `<div>` never fires
(no template in the repo uses a toggle trigger; there is no precedent proving otherwise). Every
lazy load inside a `<details>` on this page uses **`hx-trigger="toggle from:closest details once"`**:
`#optRulesBody` here and the full-chain expander in D2.5. D8.1 step 2 and step 12 check both.

Page script (one IIFE, ~120 lines), responsibilities:

| Concern | Behaviour | Precedent |
|---|---|---|
| Tab switch Ideas / Positions | swaps `#optPane` with `htmx.ajax('GET', '/options/card/'+sym)` or `'/options/positions'`; remembered in `localStorage['optPaneTab']`; **only the visible pane is loaded** (a chart drawn in a hidden box measures 0 wide) | `ivscan.html:300-336`, `_chart_pane.html:44-83` |
| Basket row click | `.opt-row[data-sym]` → `htmx.ajax('GET','/options/card/'+sym,{target:'#optPane'})`, `history.replaceState` to `/options?symbol=SYM`, highlights the row (`.opt-on`) | `ivscan.html:344-353` |
| Bridge calls | `window.thOptionsLive(sym)` = the `Promise.all` from `_options_tab.html:106-117` (`/chain`, `/iv?symbol=…&series=1`, `/account`) posting to `/options/live/<sym>`; `window.thStartBridge` reused verbatim (`_options_tab.html:183-209`); `window.thScanImport()` runs the bridge `/scan` with the stored pair floors and posts `source:'scanner'` to `/options/basket/import` (`ivscan.html:268-297`). On touch / `< lg` the Live button is not rendered at all; the strip shows "Live quotes need TWS on your PC" in its place | |
| Chart strike swap on pick click | `window.thChartSetStrikes(spec)` (D2.4) called with the row's `data-strikes` JSON | new hook in `_price_chart.html` |
| Payoff repaint | `htmx.ajax('GET', '/options/payoff/'+sym+'?strategy='+s+'&pick='+i+'&units='+u, {target:'#optPayoff', swap:'innerHTML'})` - the server renders C's SVG partial; no client painter exists | `_payoff_chart.html` (C4.3) |
| Toasts | `options:toast` HX-Trigger → `window.thToast(msg, kind)` | `base.html:639-656` |
| Chart expand | the `pfChartExpand` handler from `portfolio.html` (bottom of that file) ported as-is for the Positions tab's chart pane | `portfolio.html`, `spreads.html:44-67` |
| Failure reporting | `htmx:responseError / sendError / timeout` on `#optPane` paint the "could not load … HTTP n … /admin/log" box | `portfolio.html:21-47` |
| Telegram pause link | `?pause=7` on load → confirm "Pause Telegram ideas for 7 days?" → `htmx.ajax('POST','/options/telegram',{values:{pause_days:7}})` (D4.2) | |

Mobile (`< lg`): the layout is a single column - status strip, then the basket rendered as a
**horizontal chip strip** (`_options_basket.html` switches on a `compact` flag the shell sets via
`hx-vals='{"compact":1}'` when `window.innerWidth < 1024`; the strip is `flex overflow-x-auto gap-1 pb-1`),
then the card full-width (chart `h-[300px]`, chip row wraps, the strike table becomes three stacked
rows with the "collect / risk / chance" line on top, the Live button hidden with its hint), then the
My rules `<details>` as a bottom sheet with the debit tab's three sub-sections collapsed (D3.2). No
fixed heights on `< lg`; the page scrolls as a whole (`overflow-hidden` is on `lg:` only, as
`ivscan.html:14` does with `lg:h-[calc(100vh-6rem)]`).

#### D2.2 `_options_basket.html`

Context: `items [{row, sig, iv, rec, pick_state, age_h, stale, idea_word}], sort, selected, n, max_basket, screener, n_watchlist, n_ivscan_list, n_ivscan_scan, n_positions, compact`.

Column grid (`display:grid; grid-template-columns: 1fr 1.1rem 2rem 4.2rem`): ticker · trend arrow · IV rank · idea.

| Cell | Rendering | Rule |
|---|---|---|
| ticker | `text-[13px] font-semibold text-slate-200`; a 6px dot before it: emerald `< 20 h`, amber `20–72 h`, rose `> 72 h` or no signal; `title` = "data as of …" | the per-ticker data-age badge (D6), from `sig.age_h` / `sig.stale` |
| trend | `↗` (`text-emerald-300`) `up`, `↘` (`text-rose-300`) `down`, `↔` (`text-slate-400`) `sideways`, `·` `unclear` or no read | `sig.trend` (a String) |
| IV | `basis == 'rank'`: the rank as an integer, **amber** (`text-amber-300`) when `iv_rank >= 50` = sell premium, **grey** (`text-slate-400`) 30–50, **teal** (`text-teal-300`) `< 30` = buy. `basis in ('percentile', 'provisional')`: `~62` with a **dotted underline** (`decoration-dotted underline`), in grey only - **never amber on a provisional read**. `basis == 'unknown'`: `–`. `title` = `option_words.iv_rank_words(sig.iv)` with the day count | §7 "IV number colour" + CRITIQUE (the day count behind every rank) |
| idea | `idea_word`: `sell put` / `sell call` / `buy call` / `buy put` / `call sprd` / `put sprd` / `condor` / `calendar` / `diagonal` / `LEAPS`. **Three states**: `pick_state == 'picks'` → normal; `'none'` → faded (`opacity-60`) with `title` "recommended, but no strike passes your rules today" (this is only said when the picker actually ran under your hash); `'unchecked'` → a grey dot `·` after the word with `title` "not checked under your rules yet - open the card to check" ; `none` in `text-slate-600` when no setup; `no read` when no signal | the nightly job computes picks for every DISTINCT saved `prefs_hash` (A5.1 + contract), so `'unchecked'` is rare: a member whose rules changed after last night's run |

Row attributes: `class="opt-row cursor-pointer …{% if row.symbol == selected %} opt-on{% endif %}" data-sym="{{ row.symbol }}" hx-get="/options/card/{{ row.symbol }}" hx-target="#optPane" hx-swap="innerHTML" hx-push-url="/options?symbol={{ row.symbol }}" title="{{ sig.headline }}"`. A `×` on hover: `hx-post="/options/basket/remove" hx-vals='{"symbol":"…"}' hx-target="closest .opt-basket" hx-swap="innerHTML" hx-confirm="Remove {{ sym }} from your basket? Its tracked positions are kept."`.

Header line: `{{ n }}/{{ max_basket }} tickers` · sort buttons `Idea · IV · Trend · Added` (`hx-get="/options/basket?sort=…"`, same pattern as `_ivscan_list.html:43-46`).

Footer: `[+ Add ticker]` opens an inline `<form hx-post="/options/basket/add" hx-target="closest .opt-basket" hx-swap="innerHTML">` with one `uppercase` input; `[Import ▾]` is a `<details>` with the import buttons: **Paste tickers** (textarea → `/options/basket/import` `source=paste`), **My Watchlist ({{ n_watchlist }})**, **My IV Rank list ({{ n_ivscan_list }})** (`source=ivscan_list`, shown only when `n_ivscan_list > 0`, labelled "from the old IV Rank page"), **Last TWS scan ({{ n_ivscan_scan }})** (`source=ivscan_scan`, the stored `IVScanItem` rows), **My open trades ({{ n_positions }})** (`source=positions`), **Run my TWS scanner** (`onclick="window.thScanImport()"`, hidden on touch, with the three pair-floor inputs defaulting to `ivscan.DEFAULT_CRITERIA` - IV rank 30, price 50, volume 200 000, the only absolute numbers on the page besides the liquidity rules).

"Suggested by last night's screener" section (D5.2): up to 8 rows from `spread_candidates` not in the basket, each `SYM · credit 28% · IV pct 71 [+]`; the `[+]` posts `source=screener`.

Empty state (no rows): the D2.9 text.

#### D2.3 `_options_card.html`

Context: everything `_card_context` returns (D1.4). Wrapped in `<div class="opt-card" data-sym="{{ sym }}" data-strategy="{{ strategy }}">`.

```
┌ LRCX · 349.20 · ATR 11.5 ───────── earnings Oct 22 · inside expiry ─── [as of Oct 2 16:00 ET · delayed] [Refresh] [Live] [★ in basket] ┐
│ #optHeadline  "Uptrend: EMA 20 above 50 above 200 for 34 days, and price is riding a trend line with 3 touches. It bounced   │
│  off support at 340 on high volume (1.6× normal). Options are expensive (IV rank 62), so you're paid to sell a put spread     │
│  below that support."                                                                                                        │
│ #optGauge   SELL ● NEUTRAL ○ BUY ○   IV rank 62 over the last year · IV 41% vs realised 29%                                   │
│ #optChips   [✓ Bull put spread] [Bull call spread · also fits] [Buy call · expensive] [Iron condor · trending, not sideways]  │
│             other strategies ▾                                                                                               │
│ #optMust    What has to happen: LRCX stays above 330 until Nov 20. You keep the credit if it does nothing, drifts up, or     │
│             even dips a little.                                                                                              │
│ #optChartBody  (hx-get /options/chart/LRCX?strategy=bull_put&pick=0, load)                                                   │
│ #optPicks      (hx-get /options/picks/LRCX?strategy=bull_put, load; also options:rules-changed from:body)                    │
│   ├ strike table (3 rows) · nearest miss · the sizing line (both figures)                                                    │
│   ├ #optPayoff  (hx-get /options/payoff/LRCX?strategy=bull_put&pick=0&units=$, load → C's SVG partial)                        │
│   └ #optActions [Order ticket] [Track this]            Show full chain ▾                                                     │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

Header line, left to right: `sym` · last close (`card.setup.close`) · `ATR {{ atr }}` (`card.setup.atr`) · earnings chip (`text-amber-300` when `earnings.inside` for the chosen expiry, `text-slate-500` otherwise, `title` = D2.7 wording; from `card.iv.earnings_date / earnings_days`) · the age badge (D6: `as of Oct 2, 16:00 ET · delayed`, or after a Live press `live · TWS 21:42 ET`) · `[Refresh]` `hx-post="/options/refresh/{{ sym }}?strategy={{ strategy }}" hx-target="closest .opt-card" hx-swap="outerHTML" hx-indicator="#optRefreshSpin"` · `[Live (TWS)]` `onclick="window.thOptionsLive('{{ sym }}')"` (desktop only; on touch / `< lg` the slot reads "Live quotes need TWS on your PC") · `[+ Add to basket]` when `not in_basket` (`hx-post="/options/basket/add"`).

`note` (from a POST) renders as a one-line strip under the header: emerald for "Refreshed …" / "Live from your TWS …", amber for "Could not …", with the bridge diagnostics `<details>` when `diag` is present (`_options_analysis.html:22-44`, copied).

`#optHeadline` prints `card.headline` - the sentence the engine composed at WRITE time with
`option_words.headline()` (D2.7) and stored in `option_signal.headline`; the chip click never
changes it. `#optGauge` prints `option_words.gauge(card.iv)`, whose wording carries the basis and
the day count (D2.7). `#optMust` is the **"What has to happen"** line: `chosen.must_happen` (B3.2's
sentence for the chosen strategy, e.g. *"LRCX stays above 330 until Nov 20. You keep the credit if it
does nothing, drifts up, or even dips a little."*); the same line goes into the Telegram block (D4.4).

Chip row rules (decision 9, written as code so the wording cannot drift):

```python
# services/option_words.py
def chip_row(strategies: list[dict]) -> dict:
    """{"first": rec|None, "also_fits": [...], "greys": [<=2 rejected with shown=True], "rest": [...]}.
    Input: the signal's flat list of ALL TEN rows {key, label, fit, score, step, why, must_happen,
    reasons, reason_key, shown}. Order: the recommended one first (filled chip, check mark); every
    'also_fits' next (outlined) - including an unbuilt strategy, which the engine can never mark
    recommended and which carries reason_key 'not_available_yet'; then the rejected rows the
    engine flagged shown=True (at most TWO near misses, ordered by score), greyed, each WITH its
    reason; everything else behind 'other strategies'."""
```

Chip markup: recommended `border-emerald-500/60 bg-emerald-500/15 text-emerald-300 font-semibold` with `✓`; also-fits `border-slate-600 text-slate-300` + `· also fits`, or `· not available yet` when `reason_key == 'not_available_yet'` (the build-step wording never reaches a member); rejected `border-slate-800 text-slate-500 opacity-70` + `· {{ chip_text }}` where `chip_text` is the fixed string for the row's `reason_key` in D2.7 (e.g. `expensive`, `cheap options`, `trending, not sideways`, `no range`, `no setup`, `earnings inside`, `near-term not dearer`). Every chip is `hx-get="/options/picks/{{ sym }}?strategy={{ key }}" hx-target="#optPicks" hx-swap="innerHTML"` and sets `data-strategy` so the page script updates `.opt-card[data-strategy]` and the hidden `strategy` inputs. The "other strategies" `<details>` lists the rest as plain text links with the same `hx-get`.

A greyed chip, when clicked, re-renders the picks fragment with the amber banner "Not recommended today: {{ reasons[0] }}." and then one of two behaviours decided by `reason_key` (D2.5): `earnings_inside` → **no strike table at all** (unless the member's rule is `defined_risk_only` and the strategy is defined-risk); any other reason → the strikes are shown "so you can see what it would cost", and the primary button becomes the ghost **Order ticket (not recommended)**. A `not_available_yet` chip shows the banner "The long-term chart would suit a long-dated call; that strategy is not in TradeHunter yet." and no strikes.

The sizing line under the strike table (always both figures, CRITIQUE blocker): **"{n} contracts: about ${loss_at_stop} if the stop fires, up to ${max_loss_total} ({pct}% of your account) if the stock gaps past it"** - `option_words.sizing_line(pick.sizing)`; when `contracts == 0`: "Not even one contract fits your 1% - lower the risk or choose a narrower spread"; when `nlv is None`: "sized once you tell us the account value (My rules → Shared)".

#### D2.4 `_options_chart.html` and the chart-include changes

Context: `symbol, sel, sel_band, sel_patterns, curated, bounce, trendline, range, spread, levels, family, strategy, pick, user`. Sets the `chart_*` variables as in D1.6; the overlay dicts come from the stored `setup` (`trend_line.overlay(setup.tl, setup.tl_bounce)`, `range_box.overlay(setup.rng)`) - no detector runs on a request.

Changes to `_price_chart.html` - the trend line, the channel and the range are **C4.4 items 1-6**, owned by C and listed here only as the hooks this page relies on; the strike-line hook is this part's:

1. **`chart_trendline`** (read-only auto trend line, §6d) - C4.4 items 1, 2, 3, 4, 6: `var TL = {{ (chart_trendline if (chart_trendline is defined and chart_trendline) else None)|tojson }};` next to `BOUNCE` (`_price_chart.html:415`), the dict `trend_line.overlay()` returns (`{direction, p1{time,price}, p2, slope_per_bar, touches[{time,price}], n_touches, value_today, value_at{date:price}, broken, last_break, channel, bounce, label}`); drawn as a **line series** (`chart.addLineSeries`, amber `#f59e0b`, 2 px, dashed when `broken`, title `Trend ×n`) from `p1` to the last candle and on to the EXP badge through `window.__paintTL(far, slots)` using `value_at[expiry]` when the tail ends on that date; `snapTime(t)` (hoisted from the BOUNCE IIFE, `:2573-2579`) snaps `p1` and every touch onto the weekly candle on W; the touches become `belowBar` circle markers `TL 1/3 …` **merged into `candleMarks`** next to the BOUNCE markers (`:2562-2596`); the trend-line bounce candle merges its label with an existing support-bounce arrow on the same candle ("pin bar at trend line + …"); the channel is a second thin dashed series when `channel.n_touches >= 2`; `TL.value_today` and the channel join the candle series' `autoscaleInfoProvider` list (`:2419-2431`). Never hit-tested, never saved: it is a series, not a drawing, so the debounced PUT to `/drawings/<sym>` never sees it (the §6d rule "never into the member's own drawings"). The names `slope_per_bar` and `n_touches` are the only slope / count fields; nothing on the page reads a per-day slope or an `{a, b}` pair.
2. **`chart_range`** - C4.4's RANGE block: `var RANGE = …` (`range_box.overlay()` → `{low, high, n_low, n_high, touches_low, touches_high, sideways, label}`), two price lines (`Range low ×n` cyan `#22d3ee`, `Range high ×n` fuchsia `#e879f9`) and `low N` / `high N` circle markers through the same `snapTime`.
3. **`chart_spread` generalised** (this part, = C4.4 item 5; `:2666-2690` hard-codes three put lines and the title `'Short ' + K + 'P'`). New shape `{legs:[{strike, right, side, label}], breakevens:[...], expiry, label}` (the old `{short,long,breakeven}` still accepted: mapped to two P legs, so `_spreads_chart.html:17` and `_portfolio_chart.html:29` keep working). Loop: `side=='sell'` → rose solid width 2, `side=='buy'` → slate dashed width 1, breakevens amber dotted; the title is the leg label (`Short 330P`, `Long 320P`, `Short 370C` …). The handles are kept in `spreadHandles` and a new `window.thChartSetStrikes = function (spec) { … removePriceLine each; redraw }` is exposed - the exact pattern `paintCurated` uses with `curatedHandles` (`:2702-2712`). The autoscale hint (`spreadLevels`, `:2683-2689`) is refreshed in the same call. This is what lets a pick click move the strike lines without remounting the chart.

Debit families (`debit_vertical`, `long`, `leaps`): `chart_levels = {entry: plan.entry, stop: plan.stop, target: plan.target, label: ''}` → the existing read-only Entry/SL/PT lines (`:2652-2657`); `chart_setup_seed` is not set, so the drawing layer stays in normal mode and the lines are not draggable. `plan.stop` is B2.6's one chart stop (`min(entry − STOP_ATR × ATR, zone_lo − LEVEL_PAD_ATR × ATR)`), the same number the payoff pane, the sizing and the ticket quote.

#### D2.5 `_options_picks.html`

Context: `sym, strategy, chosen, family, picks (list of Pick with sizing), degenerate {reason_key, text, nearest, fix}|None, rules_line, considered, pick_i, prefs, earnings {date, days, inside, rule, hide_strikes}, live, card, nlv, nlv_source`. Wrapped in `<div class="opt-picks" hx-get="/options/picks/{{ sym }}?strategy={{ strategy }}" hx-trigger="options:rules-changed from:body" hx-swap="outerHTML">` - so a rule save re-renders exactly this box.

Heading (sentence case): `Strikes under your rules` followed by `rules_line` (B4.8, the picker's own line):
"delta 0.20–0.30 · 30–60 days · width 0.5–1.5 ATR ($6–17) · credit ≥ 25% · under support 340.9 + trend line" - one line, no jargon beyond the words the rules drawer itself uses.

Banner rules, before anything else renders:

| `chosen.fit` / `reason_key` | Banner | Strike table | Primary button |
|---|---|---|---|
| `recommended` / `also_fits` | none | shown | **Order ticket** (filled) |
| `also_fits` with `not_available_yet` | slate: "The long-term chart would suit a long-dated call; that strategy is not in TradeHunter yet." | hidden | none |
| `rejected`, `earnings_inside` and NOT (`prefs.shared.earnings_rule == 'defined_risk_only'` and the strategy is defined-risk) | amber: "Not recommended today: earnings {{ date }} fall inside every expiry in your window and your rule says no." | **hidden entirely** (no "see what it would cost"; the one thing a stop cannot protect) - the D2.9 earnings empty-state shows instead | none (no ticket, no track) |
| `rejected`, any other reason | amber: "Not recommended today: {{ reasons[0] }}. Showing the strikes anyway so you can see what it would cost." | shown | ghost **Order ticket (not recommended)**; the rejection sentence is the ticket's first line and the tracked trade's note (D1.11, D1.13) |

After a Live press (`live` set): the table shows ONLY the live expiry's picks, each legs cell carrying `live 21:42 ET`; a `<details>` "show delayed expiries" below it re-requests `/options/picks/{sym}?strategy=…` (the stored picks, with their own `as of` label). Rows from two moments never sit in one table unlabelled.

Row (one per pick, max 3; the recommended one first with `★`):

| Column | Wording | Source |
|---|---|---|
| legs | `Nov 20 · 330/320 put` (`expiry_label · short/long right`); condor `Nov 20 · 320/310 put + 380/390 call`; calendar `Nov 20 → Dec 18 · 350 call`; single `Dec 18 · 340 call` | `pick.legs` |
| you collect / you pay | credit families **collect $300–$317** (worst likely fill to mid; the POP and max loss are quoted at the mid); debit **pay $640**; per contract, `× contracts` when > 1 | `-pick.net*100` / `pick.net*100`; the floor from the ticket's `net.floor` (B6.1) |
| you risk | **risk $790** (credit: width − credit; debit: the debit; calendar: the net debit) | `pick.max_loss` (positive $ per contract) |
| chance | credit **about 74% chance of keeping it**; debit **about 46% chance of profit**; the number is `round(pop*100)`, the word from `pop_kind`; `title` = the full `pop_words` sentence (D2.7) and, when present, `model estimate {m}%` | `pick.pop`, `pick.pop_kind` |
| return | `27% on risk` (credit ÷ max loss) or `2.1× reward/cost` (debit verticals) | `pick.score` inputs via `pick.words` |
| tag | `← best fit to your rules` on row 1, `highest chance` on the row with the highest pop, `most credit per $ risked` on the highest credit, by `pick.why[0]` | B4.7 |
| liquidity | one word from `pick.liquidity.tier`: `clean`, `at the limit` (`limit`, amber), `wide` (amber), `thin` (rose, OI below rule), `unknown` (slate, "open interest unknown - check in TWS") with `title` = OI and width per leg | `bull_put.rank_pairs` tiers (`services/bull_put.py:51-52, 64-66`), generalised in B4.4 |

Row attributes: `class="opt-pick … {% if loop.index0 == pick_i %}ring-1 ring-emerald-500/60{% endif %}" data-pick="{{ loop.index0 }}" data-strikes='{{ pick.chart_spec|tojson }}'`; click = page script: highlight, `thChartSetStrikes(JSON.parse(data-strikes))`, the payoff re-request into `#optPayoff` (D2.1), and update the hidden `pick` inputs in `#optActions`. **No `/options/chart` round trip** for a pick click.

The sizing line (D2.3) sits directly under the table: `{{ option_words.sizing_line(picks[pick_i].sizing) }}` - both figures, every time; it re-renders when another pick is clicked (the page script swaps the text from the row's `data-sizing`).

Degenerate (`picks` empty): `degenerate.text` from B4.8 with the `nearest` candidate greyed and the one number that failed ("Closest that didn't pass: Nov 20 335/325 at delta 0.33 - fails *short strike delta* 0.30"), then `degenerate.fix` as "Loosen that rule in My rules → {{ tab_label }}" - so "no trade today" reads as a decision, not a blank. `considered` is printed as "{{ considered }} combinations looked at".

Earnings line (when `earnings.inside` and the strikes are shown): amber "Earnings {{ date }} falls inside this expiry. Your rule: {{ 'not allowed' | 'defined-risk trades only - this trade qualifies' }}."

Then the payoff container and `#optActions`:

```jinja
<div id="optPayoff" hx-get="/options/payoff/{{ sym }}?strategy={{ strategy }}&pick={{ pick_i }}&units={{ units }}"
     hx-trigger="load" hx-swap="innerHTML" class="mt-2 min-h-[300px]"></div>   {# C's _payoff_chart.html lands here #}
<div id="optActions" class="flex flex-wrap items-center gap-2 mt-2">
  {% if not earnings.hide_strikes and chosen.reason_key != 'not_available_yet' %}
  <button type="button" class="{% if chosen.fit == 'rejected' %}border border-slate-700 text-slate-400{% else %}bg-emerald-600 text-white{% endif %}"   {# ONE filled action per card #}
          hx-get="/options/ticket/{{ sym }}" hx-vals='js:{strategy: thCardStrategy(), pick: thCardPick(), contracts: thCardQty()}'
          hx-target="#optTicket" hx-swap="innerHTML">Order ticket{% if chosen.fit == 'rejected' %} (not recommended){% endif %}</button>
  <form hx-post="/options/track-idea" hx-target="#optPane" hx-swap="innerHTML" class="flex items-center gap-1">
    <input type="hidden" name="symbol" value="{{ sym }}"><input type="hidden" name="strategy" value="{{ strategy }}">
    <input type="hidden" name="pick" value="{{ pick_i }}">
    <label class="text-[11px] text-slate-500">contracts <input type="number" name="contracts" min="1" max="500"
           value="{{ picks[pick_i].sizing.contracts or 0 }}" class="w-14 …"></label>
    <button type="submit" class="… border border-slate-700 text-slate-300" {% if not picks[pick_i].sizing.contracts %}disabled{% endif %}
            title="Track this idea in Positions so it is checked every day. Does not place an order.">Track this</button>
  </form>
  {% endif %}
  <details class="ml-auto text-[11px]"><summary class="cursor-pointer text-slate-500">Show full chain ▾</summary>
    <div hx-get="/options/chain/{{ sym }}?expiry={{ picks[pick_i].expiry if picks else '' }}"
         hx-trigger="toggle from:closest details once" hx-swap="innerHTML" class="overflow-auto max-h-[50vh]"></div>
  </details>
</div>
<div id="optTicket"></div>
```

The contracts box is **pre-filled from `pick.sizing.contracts`** (B5.3, computed at read time by
`option_sizing.size`) and the page never computes a quantity of its own: `0` stays `0` with the
"Not even one contract fits your 1% - lower the risk or choose a narrower spread" note and a
disabled Track button; `None` (no account value) shows "set your account value in My rules → Shared
to size this" (NLV 0 = "not told yet", `trade_prefs.DEFAULT_NLV`). A member may type a lower number;
a higher one is accepted by the form but the sizing line beside it still states the gap figure for
what they typed.

#### D2.6 The payoff pane - C's `_payoff_chart.html`, nothing of this part's own

`#optPayoff` (D2.5) receives `GET /options/payoff/{symbol}` = `templates/_payoff_chart.html`
(C4.3): a server-rendered inline SVG (`viewBox="0 0 640 300"`), its hover tooltip script reading its
own `<script type="application/json" id="po-data-…">`, the legend line and the `[$ | R]` toggle,
which re-requests the pane's own URL with `units=R` (`hx-target` the pane) and remembers the choice in
`localStorage['th.payoff.units']` (a per-viewer convenience only). Colours are the four `--po-*`
tokens C4.2 adds to `base.html` (dark + light), so the theme toggle recolours the pane live; the
price chart stays the one lightweight-charts instance on the page.

What the member sees (C4.2), restated only where the card's checklist depends on it:

| Element | Paint |
|---|---|
| profit / loss zones | two filled `<path>`s split exactly at the breakevens (they are grid points) |
| expiry line | 2 px solid `--po-exp`; today line 1.5 px dashed `--po-today` (omitted when no σ) |
| **chart stop** | vertical marker `kind=stop` in `--po-loss`, label `stop 336.2 · ≈ −$102 today` (the T+0 read; the stop is hit within days) |
| **rule stop** | vertical marker `kind=rule_stop` + horizontal dashed hline `rule stop −$158 (20% of max loss)`; drawn for every family with a $ rule in B5.2 (credit 20% of max loss; long / debit / time `premium_stop_pct` of the debit); none for LEAPS |
| target | debit families: vertical `target 372 · +$640`; credit families: the horizontal `take profit +$105 (50% of credit)` line |
| levels | `support 340`, `trend line at expiry 336.1`, range edges - vertical, labelled, cyan / amber |
| strikes, breakevens | dotted verticals `short 330`, `long 320`; `breakeven 327.90` |
| max profit / max loss | hlines `max profit` / `max loss` with the POSITIVE magnitudes in the legend: `max +$210 / −$790` |
| chance | legend: `About a 74% chance of keeping the credit · model estimate 73%` |
| the dot | Positions tab only (`?trade=`): `(spot, pl_now)` from the latest `OptionTradeCheck` |

Caption (decision 8), always visible under the chart, verbatim: **"Dashed line: what the trade would be worth if the stock moved there today, at today's implied volatility - an estimate. Solid line: at expiry ({{ dte }} days)."** For calendars/diagonals: **"Drawn at the near expiry ({{ front }}); the far option is valued by the model, so the solid line is an estimate too."**

Units: `$` (default) or `R` (R = 20% of max loss for the credit families, `|pnl_today(chart_stop)|` for the debit families); the server renders either, the client never recomputes.

Failure: `po.error` → the pane renders "Could not draw the payoff (no stored quotes for these strikes). Press Refresh." in amber at the same height; never a blank box.

#### D2.7 Plain-language strings (`services/option_words.py`) - written out

Every number a member sees that is a greek, a rate or a rule is accompanied by one of these sentences (as `title`, as the chance column, or inline). All are functions so the numbers are filled in; the templates never compose them. Inputs are the signal's `iv` dict (A5.2 with the contract's names: `iv_n`, `state`, `basis`, `provisional`, `skew25`, `term_ratio`, `earnings_date`, `earnings_days`, `verdict`) and the pick.

**Greeks and rates**

| Function | Text (credit side) | Text (debit side) |
|---|---|---|
| `delta_words(d, side, right)` | "Delta 0.25 - about a 1-in-4 chance the stock is below this strike at expiry (above it, for a call); put another way, roughly a 75% chance of keeping the credit." | "Delta 0.65 - the option moves about 65 cents for every $1 the stock moves; one contract behaves like about 65 shares." |
| `theta_words(t_per_day_usd, side)` | "Theta +$6/day - time is paying you about $6 a day while the stock sits still." | "Theta −$9/day - waiting costs about $9 a day; the stock has to move enough to pay for that." |
| `vega_words(v_usd)` | "Vega $18 - if implied volatility rises one point this position loses about $18 (you are short volatility)." | "Vega $22 - if implied volatility rises one point this position gains about $22." |
| `gamma_words(g)` | "Gamma 0.02 - the delta changes by about 0.02 for each $1 move; small means the trade's risk changes slowly." **Shown only inside the full-chain expander** (D1.15), never on the card. | same |
| `iv_words(iv, spot, dte)` | "Implied volatility 41% - the market's guess at how much the stock will move in a year; about ±{{ spot·iv·sqrt(dte/365) }} ({{ pct }}%) over this trade." | same |
| `iv_rank_words(iv)` | `basis == 'rank'`: "IV rank 62 over the last year ({{ iv_n }} days) - today's IV sits 62% of the way from the year's lowest to its highest. Above 50 options are expensive (sellers are paid); below 30 they are cheap by this stock's own standards." `basis == 'percentile'`: "IV percentile 71 over the last 118 days (not a full year yet) - IV was lower than today on 71% of those days." `basis == 'provisional'`: "IV 46% against 34 days of history - too short to rank; nothing here is firm yet." `basis == 'unknown'`: "No IV history yet ({{ iv_n }} of 60 days). It fills in by itself; if you run TWS on this PC, Live loads a year at once." | |
| `iv_pct_words(iv)` | "IV percentile 71 - IV was lower than today on 71% of the past {{ iv_n }} trading days." | |
| `hv_words(iv)` | "Realised volatility 29% - how much the stock actually moved over the last 20 days, annualised." | |
| `iv_hv_words(iv)` | "IV 41% vs realised 29% - options are priced for 41% more movement than the stock has actually shown; sellers are paid for that gap." / "… less movement … buyers are getting it cheap." | |
| `term_words(iv)` | `term_ratio >= 1.05`: "Near-term options are dearer than later ones (front IV 45% vs back 38%) - the market expects an event before the first expiry." `term_ratio <= 0.95`: "Later options are dearer than near ones - nothing special is priced in soon; calendars are not paid here." between: "Near and later options are priced alike." `term_ratio is None`: omitted. | |
| `oi_words(oi, rule)` | "Open interest 2,300 - contracts outstanding at this strike. You need enough to get out again; your rule is at least 500." | |
| `width_words(w, rule)` | "Bid/ask $0.20 wide - the cost of getting in and out. Your rule allows up to $0.50; wider than that eats the edge." | |
| `pop_words(pop, pop_kind)` | `keep`: **"About a 74% chance of keeping the credit - an estimate from today's option prices (the short strike's delta), not a promise. Earnings, news and gaps are not in that number."** | `profit`: **"About a 46% chance of profit if held to expiry, at today's volatility; this trade is managed by the chart stop and target, so the real odds depend on the move, not this number."** The model figure (C3.8 `pop.model`) is shown as "model estimate {m}%". |
| `max_loss_words(x, family)` | "The most you can lose: $790 per contract (the width minus the credit), if the stock is below both strikes at expiry." | "The most you can lose: $640 per contract - what you paid." |
| `breakeven_words(be, spot)` | "Breakeven 327.90 - the stock can fall 6.1% and this still makes money at expiry." | "Breakeven 356.40 - the stock must rise 2.1% by expiry just to get your money back." |
| `dte_words(dte, family)` | "49 days to expiry - inside your 30–60 day window; long enough for time to work for you, short enough to manage." | "70 days - inside your 45–90 day window; enough time for the move, before decay bites." |
| `expected_move_words(em, spot, dte)` | "Expected move ±$18 (5.2%) by expiry - one standard deviation at today's IV." | |
| `earnings_words(e)` | "Earnings Oct 22 falls INSIDE this expiry - the one thing a stop cannot protect you from." / "Earnings Oct 22 is after this expiry." / "No earnings date on file - check before you trade." | |
| `sizing_line(sizing)` | **"{n} contracts: about ${loss_at_stop} if the stop fires, up to ${max_loss_total} ({pct}% of your account) if the stock gaps past it"** (`capital_at_risk_usd`, `max_loss_total_usd`, `max_loss_total_usd / nlv`); `contracts == 0` → "Not even one contract fits your 1% - lower the risk or choose a narrower spread"; `nlv is None` → "sized once you tell us the account value (My rules → Shared)" | |

**Trend / setup / gauge (used by `headline`, which the ENGINE calls at write time and stores in `option_signal.headline`)**

| Piece | Text |
|---|---|
| uptrend | "Uptrend: EMA 20 above 50 above 200 for {{ trend_days }} days" |
| downtrend | "Downtrend: EMA 20 below 50 below 200 for {{ trend_days }} days" |
| sideways | "Sideways: the EMAs are flat and price has held between {{ rng.low }} and {{ rng.high }} ({{ n_low }} touches below, {{ n_high }} above)" |
| unclear | "No clear trend: the EMAs are tangled" |
| support bounce | "It bounced off support at {{ level }} on high volume ({{ vol }}× normal)" |
| trend-line touch | "price is riding a trend line with {{ n_touches }} touches" / "it bounced at the trend line ({{ n_touches }} touches)" |
| EMA rebound | "it rebounded off the {{ ema }}-day average" |
| breakout retest | "it broke out above {{ level }} and is retesting it" |
| failed support | "support at {{ level }} gave way and price is back under it" |
| no setup | "No fresh setup today" |
| gauge SELL, `basis == 'rank'` | "Options are expensive (IV rank {{ r }} over the last year)" |
| gauge SELL, `basis in (percentile, provisional)` | "Options look expensive against the last {{ iv_n }} days (not a full year yet)" |
| gauge NEUTRAL | "Options are fairly priced (IV rank {{ r }})" / "… against the last {{ iv_n }} days" |
| gauge BUY | "Options are cheap (IV rank {{ r }})" / "Options look cheap against the last {{ iv_n }} days (not a full year yet)" |
| gauge UNKNOWN | "We cannot yet say whether options are expensive - {{ iv_n }} of 60 days of history. If you have TWS on this PC, press Live to load a year." |
| conclusion per strategy | bull_put "so you're paid to sell a put spread below that support" · bear_call "so you're paid to sell a call spread above that resistance" · buy_call "so a call is cheap enough to buy here, with the stop just under the setup" · buy_put "so a put is cheap enough to buy on the breakdown" · bull_call "so a call spread capped at the target costs less than a plain call" · bear_put "so a put spread capped at the target costs less than a plain put" · leaps_call "so a long-dated deep call can stand in for the stock" · iron_condor "so you're paid to sell both sides of the range" · calendar "so selling the near month against a later one collects the difference" · diagonal_call "so a long-dated call can fund selling monthly calls under the resistance" |
| nothing fits | "so there is nothing to do today - check again tomorrow" |
| unbuilt fits best | "The long-term chart would suit a long-dated call; that strategy is not in TradeHunter yet." |

`headline(setup, iv, strategies)` = `"{trend}, and {setup_clause}. {setup_sentence}. {gauge}, {conclusion}."` with the clauses dropped when absent (so "Uptrend … for 34 days. No fresh setup today. Options are expensive (IV rank 62 over the last year), so there is nothing to do today - check again tomorrow."). It is called by the engine (B) at write time; `_options_card.html` prints the stored string; a chip click never changes the sentence.

**Rejection reasons** - `reason_key` is the fixed vocabulary the engine writes on every rejected row (and `not_available_yet` on an unbuilt `also_fits` row); `chip_text` is the chip's short form; the long form is `reasons[0]` (B3.3's `TREND_REASON / IV_REASON / NEED_REASON` text):

| `reason_key` | chip text | long (example) |
|---|---|---|
| `expensive` | expensive | "options are expensive (IV rank {{ r }} ≥ 30): paying for a long option here fights the premium" |
| `cheap_options` | cheap options | "options are cheap (IV rank {{ r }} < 30): selling premium is not paid enough" |
| `not_rich_enough` | premium not rich enough | "IV rank {{ r }} is under 50: a condor needs richer premium" |
| `trending_not_sideways` | trending, not sideways | "the chart is trending; a range strategy wants flat EMAs and a range with both edges touched" |
| `no_range` | no range | "no range with both edges touched at least twice" |
| `wrong_direction` | wrong direction | "the chart is in a {{ trend }}; this strategy needs the opposite" |
| `no_setup` | no setup | "no fresh setup to anchor the entry or the stop" |
| `earnings_inside` | earnings inside | "earnings {{ date }} fall inside every expiry in the window and your rule says no" |
| `front_iv_under_back` | near-term not dearer | "near-term IV is below later IV (ratio {{ term_ratio }}); a calendar is not paid here" |
| `no_long_dated` | no 9–18 month options stored | "no expiry 9–18 months out is stored for this ticker" |
| `no_weekly_trend` | no weekly trend | "the weekly EMAs are not stacked; LEAPS want the long-term trend" |
| `not_available_yet` | not available yet | "that strategy is not in TradeHunter yet" (never the build-step wording) |

**Idea short words** (basket column): `sell put`, `sell call`, `buy call`, `buy put`, `call sprd`, `put sprd`, `LEAPS`, `condor`, `calendar`, `diagonal`.

**Glossary additions** (`services/glossary.py`, one more `_add({...})` group so `T.tip('Delta')` works in table headers): Delta, Theta, Vega, Gamma, Implied volatility, IV rank, IV percentile, Realised volatility, Open interest, Bid/ask, Breakeven, Max loss, Max profit, Chance of keeping it, Chance of profit, Expected move, Days to expiry, Credit, Debit, Width - each the first sentence of the matching row above, without the numbers.

#### D2.8 `_options_ticket.html`

Context: `sym, strategy, label, pick, contracts, tws (str), moomoo (str), ticket (the B6.1 dict), chosen, refresh_first, dip, as_of, source, prefs`. Rendered into `#optTicket` as a small panel with two tabs **TWS** / **moomoo** (each a `<pre>` holding `order_ticket.render(ticket, broker)`, B6.2 / B6.3 - this part prints B's text and adds only the lines marked ► below), a `[Copy]` button (`navigator.clipboard.writeText` of the visible `<pre>`), `[Close]`.

Lines this part puts around B's rendering, in order:

1. ► **Refresh-first banner** (amber, when `refresh_first`: `as_of` older than the last session close AND the US session is open): "These prices are from {{ as_of }} and the market is open now. **Refresh first**, then re-open the ticket." with the Refresh button inline (`hx-post="/options/refresh/{{ sym }}"` - the 60 s cooldown never blocks this first refresh, D1.9).
2. ► **Rejected first line** (when `chosen.fit == 'rejected'`): "Not recommended: {{ chosen.reasons[0] }}." as the first line inside the `<pre>` too.
3. ► **Header line**, verbatim: "Prices are from {{ as_of }}. Press Refresh after 21:30 Malaysia time (US open) and re-open the ticket before sending; the credit will have moved."
4. B's rendering (B6.2 for TWS, B6.3 for moomoo).
5. ► **Footer lines**: "You collect $…(worst likely fill)–$…(mid) · you risk $… · breakeven … · {{ earnings line }}" / "Tracking only: TradeHunter never sends an order."

What B's rendering must carry, and what this panel asserts in tests (D8.2):

- **Entry: NO condition by default.** `ticket.condition` is `None` for every family unless the member switched on the **"Enter on the dip"** toggle (`?dip=1`, a checkbox in the panel). With it on, the condition is B6.1's `last <= level × (1 + offset_pct/100)` (341.92 on the LRCX fixture) and the panel prints, verbatim: *"This order will also fire if {{ sym }} crashes through {{ price }} on bad news. Only use it while you are watching."* The entry is a DAY limit order placed while the market is open.
- **The ONE conditional order the ticket pushes hard is the chart-stop EXIT** (ORDER 2 in B6.2): `last <= 336.20` on the LRCX fixture (`setup.plan.stop`, the engine's value - never a hand-typed number), with the two sentences, verbatim: *"This order fires on the live price during regular hours, so it can fire on an intraday dip the close would have survived. If you prefer the close-based rule, leave this order off and act on the Positions tab's verdict instead."* and **"Trigger outside RTH: No"** printed in BOTH renderings.
- **TWS combo stop:** Market recommended; Limit (the model's mark at the stop) as the secondary option with "may not fill in a fast market".
- **moomoo stop, leg order:** (1) "Buy to close the SHORT leg first (325 Put) - market order, or limit = model × 1.15"; (2) "Then sell the LONG leg (315 Put)"; and the sentence *"Never sell the long leg before the short leg is closed - you would be short a naked put."* ("naked call" for a bear call). Same ordering rule for TWS when the combo cannot be conditional.
- Jargon: "(you are paid; the most you can lose is fixed)" and "worst likely fill 2.00" are the only wordings; the trader-jargon forms an earlier draft used for the same two lines are gone.

Example (the TWS tab for the LRCX fixture - B6.2's text, 10 contracts, with this part's lines marked ►):

```
► Prices are from 02 Oct 16:00 ET. Press Refresh after 21:30 Malaysia time (US open) and re-open the ticket before sending; the credit will have moved.
LRCX - Bull put spread (you are paid; the most you can lose is fixed) - 10 contracts - paste into TWS
ORDER 1 · ENTRY (combo, DAY)
  Strategy Builder → Vertical: SELL 10 LRCX 20 NOV 26 325 P / BUY 10 LRCX 20 NOV 26 315 P
  Limit CREDIT 3.17 (mid; worst likely fill 2.95). Work it: if not filled in a few minutes, lower 0.05 at a time, never below 2.50.
  No condition - place it during the session.
  [Enter on the dip - only if you ticked it: Conditional tab → Add → Price → LRCX (STK, SMART) → Last ≤ 341.92.
   This order will also fire if LRCX crashes through 341.92 on bad news. Only use it while you are watching.]
ORDER 2 · CHART STOP (combo, GTC)
  BUY 10 LRCX 20 NOV 26 325 P / SELL 10 LRCX 20 NOV 26 315 P (close the spread) - Market (recommended), OR Limit DEBIT 4.16 (the model's mark at the stop; may not fill in a fast market)
  Conditional tab → Add → Price → LRCX (STK, SMART) → Last ≤ 336.20 → Trigger outside RTH: No → transmit when true
  This order fires on the live price during regular hours, so it can fire on an intraday dip the close would have survived.
  If you prefer the close-based rule, leave this order off and act on the Positions tab's verdict instead.
  (this is the chart stop; you would lose ≈ $990 here, against the max loss of $6,830)
ORDER 3 · TAKE PROFIT (combo, GTC)
  BUY 10 LRCX 20 NOV 26 325 P / SELL 10 LRCX 20 NOV 26 315 P - Limit DEBIT 1.59 (half the credit kept). No condition.
Rule stop (no order - the monitor watches it): if the spread is marked at 4.54 or more (20% of max loss), close it.
Time stop: close or roll with 21 days left (Oct 30), whatever the P/L.
What has to happen: LRCX stays above 325 until Nov 20.
► 10 contracts: about $990 if the stop fires, up to $6,830 (6.8% of your account) if the stock gaps past it
► You collect $2,950–$3,170 (worst likely fill to mid) · you risk $6,830 · breakeven 321.83 · earnings Oct 22 is inside this expiry (defined-risk trades only)
► Tracking only: TradeHunter never sends an order.
```

Debit variant lines (B6.1/B6.2): "Buy to open … / (Sell to open … for a spread)", "LIMIT 6.40 debit (mid 6.35 · worst likely fill 6.60)", plan "target: sell at the chart target 372 (2R) · stop: sell if LRCX trades under {{ plan.stop }} (ORDER 2, the same two sentences) or the option loses 50% · time: review at 21 days left". Condor: four legs, "one iron condor, LIMIT 3.10 credit", the stop on either range edge ± pad. Calendar/diagonal: two expiries on separate lines, "one calendar spread, LIMIT 2.40 debit", no entry condition.

Broker mapping notes in a `<details>` under the text: TWS - "Strategy Builder → Vertical → enter both legs → Limit price = credit → (ORDER 2) Conditional tab → Price of LRCX ≤ stop → Trigger outside RTH: No"; moomoo - "Options → Strategy → Bull Put → both legs → (stop) Conditional order → Price condition on LRCX → then buy to close the short leg first, sell the long leg second". No screenshots, no claims about either broker's order types beyond those two paths (the design discussion recorded that these were worked out; this part only prints them).

#### D2.9 Empty states and failure wording (every case the page can be in)

| State | Where | Text (verbatim) |
|---|---|---|
| no basket | `#optBasket` + `#optPane` | "Your basket is empty. Add the tickers you trade options on - paste them, pull in My Watchlist, or run your TWS scanner. Tonight's job reads each one's option chain; you'll see a card tomorrow morning. In a hurry? Press **Refresh** on a ticker to read today's delayed chain now (about 2 seconds)." |
| basket, nothing selected | `#optPane` | "Click a ticker on the left. Best ideas sort to the top: ↗ ↘ ↔ is the trend, the number is IV rank (amber = sell premium, teal = buy; ~62 with a dotted line = not a full year of history yet), the last column is tonight's suggestion." |
| no signal yet (added today) | card | "No read yet for {{ sym }} - it was added after last night's job. Press **Refresh** to read today's delayed chain now, or wait for tonight." (chips, picks, payoff hidden; chart still shows) |
| signal, no setup | card | headline ends "…so there is nothing to do today - check again tomorrow." All chips greyed with reasons; picks area: "No strategy fits today. The chart is for checking that read - nothing to pick." |
| recommended, picks not computed under your rules yet | basket cell + picks | basket: grey dot, "not checked under your rules yet - open the card to check"; opening the card computes them (`card_for`, lazy, no market call) |
| recommended, no strike passes (computed) | picks | "No strike passes your rules for {{ label }} today ({{ rules_line }}). {{ degenerate.text }} Closest: {{ nearest }} - fails *{{ rule_label }}*. {{ degenerate.fix }} (My rules → {{ tab_label }})." |
| earnings blocks | picks | "Earnings on {{ date }} fall inside every expiry in your {{ dte_lo }}–{{ dte_hi }} day window, and your rule says no earnings inside the trade. Either wait, or allow defined-risk trades through earnings in My rules → Shared." (no strike table, no ticket, no track) |
| not available yet | picks | "The long-term chart would suit a long-dated call; that strategy is not in TradeHunter yet." |
| IV history unknown | gauge + basket `–` | "We cannot yet say whether options are expensive - {{ iv_n }} of 60 days of history. If you have TWS on this PC, press Live to load a year." |
| IV history short | gauge + basket `~62` | "Options look expensive against the last {{ iv_n }} days (not a full year yet)" (never amber) |
| sizing: no account value | picks | "set your account value in My rules → Shared to size this" |
| sizing: zero | picks | "Not even one contract fits your 1% - lower the risk or choose a narrower spread" |
| data stale (≥ 20 h, < 3 trading days) | age badge amber | "as of {{ when }} ET - the nightly job hasn't run since. Refresh reads today's delayed chain." |
| data very stale (≥ 3 trading days) or job missed twice | banner over the card, rose | "This card is {{ n }} trading days old. The nightly job on Hermes has not run - an administrator can check `/admin/log`. Refresh still works for one ticker at a time." |
| ticket while the market is open on last night's prices | ticket panel, amber | "These prices are from {{ as_of }} and the market is open now. **Refresh first**, then re-open the ticket." |
| Cboe down on Refresh | note strip, amber | "Could not refresh: {{ error }}. Showing the stored data from {{ when }}." (`ChainError` text, e.g. "LRCX: Cboe HTTP 429") |
| Live on a phone / touch device | where the button would be | "Live quotes need TWS on your PC" |
| bridge not running on Live | note strip + `<details>` diag | "Could not reach your IBKR bridge on this PC (127.0.0.1:9224). Start TWS and `bridge\start_ibkr_bridge.bat`, then press Live again. The card still shows the delayed data." + `[Start the bridge]` `[Retry]` (`_options_analysis.html:45-61` reused) |
| bridge up, no greeks | note strip | "Your TWS sent quotes but no greeks (no option model yet). Delayed data kept for the picks; try Live again in a minute." |
| bridge older than 1.6 (no IV series) | note strip, slate | "Live quotes shown. Your bridge is older than 1.6, so the one-year IV history was not copied - restart `start_ibkr_bridge.bat` to get the new file." |
| Live chain shown | age badge + picks | badge "live · TWS {{ time }} ET"; the picks table holds the live expiry only, each row "live {{ time }} ET"; "show delayed expiries ▾" below it. Nothing from Live is stored except the IV series. |
| NLV from TWS | picks | "from TWS · [remember this]" - nothing stored until the click |
| ticker unknown to Cboe | card | "{{ sym }}: no Cboe option chain - check the ticker. Removed from the nightly job until it is fixed." (`ChainError` 403/404 text, `option_quotes.py:143-150`) |
| positions tab, none | `_options_positions_tab.html` | "Nothing tracked yet. Track an idea from its card, or enter a trade by hand below." |
| positions: earnings now inside | row, amber urgent | "Earnings {{ date }} now fall inside this trade (the date was unknown or later when you entered). Decide before the close that day." |
| rules save out of range | drawer | the field's own "{{ label }} must be between {{ min }} and {{ max }}." (`trade_prefs.write` style, `services/trade_prefs.py:106-117`) |
| safety switch unticked (before Save) | drawer, amber | "Without this, the short strike can sit inside the zone the chart says must hold." (D3.2) |
| track refused (cache miss) | toast, rose | "The strikes changed - look at the card again before tracking." |
| track refused (earnings) | toast, rose | "Earnings {{ date }} fall inside this trade and your rule says no - nothing to track." |
| refresh cooldown | toast, slate | "Just refreshed - try again in a minute." |
| Telegram chat id without code | drawer, rose | "Enter the 6-digit code the bot sent you after /start." |

#### D2.10 `base.html` ink additions and the scrollbar rule

Light-theme block (`base.html:275-324`) gets, in the same style:

```css
.text-teal-300, .text-teal-200 { color:#0f766e !important; }        /* the basket's 'buy premium' IV number */
.hover\:text-teal-300:hover { color:#0f766e !important; }
.border-teal-500\/60 { border-color:#0f766e !important; }
.text-violet-200 { color:#6d28d9 !important; }                     /* the payoff legend's 'Today' swatch text */
```

Dark theme needs nothing: `text-teal-300` and `text-violet-*` are native Tailwind. The payoff
pane's own colours are C4.2's four `--po-*` tokens (`--po-exp #1D9E75`, `--po-today #7F77DD` /
`#6A62C9` light, the teal-50 / coral-50 fills, `--po-loss`) declared on `:root` and remapped under
`.light`, validated for CVD on both themes; the SVG reads them as `var(--po-*)` and text in
`--tv-text` / `--tv-muted`, so the theme toggle recolours it without a repaint. No client painter, no
fixed hex in JSON.

Scrollbar: every scroll area on the page (`#optBasket overflow-y-auto`, `#optPane overflow-y-auto`,
`#optRulesBody overflow-y-auto max-h-[40vh]`, the picks table wrapper `overflow-x-auto`, the
full-chain expander `overflow-auto max-h-[50vh]`, the mobile basket strip `overflow-x-auto`, the
Positions drawer's check history) inherits `base.html:15-22`. The reviewer's check (D7 item 10): no
`scrollbar-*` property and no `::-webkit-scrollbar` rule anywhere in the new templates; the payoff
pane never scrolls.

---

### D3. The My rules drawer (`_options_rules.html`)

#### D3.1 Model - ONE module, `app/services/option_prefs.py` (B4.1's schema, this part's presentation columns, in the SAME table)

B's `SCHEMA` is what the picker reads: its blocks, its field names, ATR widths, the extrinsic cap as a
% of the STOCK price, the two-value `earnings_rule`. This part adds the columns the drawer needs -
`label, help, plain, step, unit` - to the same `Field` so one table serves both, plus the tab mapping
and the thin wrappers the routes call. House defaults are constants (decision 3; admin-editable later
= move the dict to a table, the merge code does not change).

```python
# app/services/option_prefs.py  (B4.1 + D3)
Field = namedtuple("Field", "default lo hi kind label help plain step unit")   # kind: num | int | bool | choice

BLOCKS = ("shared", "credit_vertical", "debit_vertical", "long", "leaps", "condor", "time")
TABS = ("shared", "credit", "debit", "condor", "time")
TAB_LABELS = {"shared": "Shared", "credit": "Credit spreads", "debit": "Buy call/put",
              "condor": "Iron condor", "time": "Time spreads"}
TAB_BLOCKS = {"shared": ("shared",), "credit": ("credit_vertical",),
              "debit": ("long", "debit_vertical", "leaps"),      # three sub-sections on one tab (D3.2)
              "condor": ("condor",), "time": ("time",)}
STRATEGY_KEYS = ("bull_put", "bear_call", "buy_call", "buy_put", "bull_call", "bear_put",
                 "leaps_call", "iron_condor", "calendar", "diagonal_call")
FAMILY_OF = {"bull_put": "credit_vertical", "bear_call": "credit_vertical",
             "bull_call": "debit_vertical", "bear_put": "debit_vertical",
             "buy_call": "long", "buy_put": "long", "leaps_call": "leaps",
             "iron_condor": "condor", "calendar": "time", "diagonal_call": "time"}
CREDIT_FAMILIES = {"bull_put", "bear_call", "iron_condor"}
DEFINED_RISK = set(STRATEGY_KEYS) - set()          # every catalog strategy is defined-risk (a bought option's loss is the premium); used by the earnings gate

# Constants, NOT fields (B0.3): the chart stop / target / pad are the engine's convention, not a preference.
STOP_ATR = 1.0          # debit chart stop = entry - 1 ATR
TARGET_R = 2.0          # debit chart target = entry + 2R
LEVEL_PAD_ATR = 0.25    # credit chart stop = zone_lo - 0.25 ATR (336.2 on the LRCX fixture)

# Absolute numbers are allowed ONLY for the member's own liquidity rules (OI, bid/ask width, volume);
# everything else is a ratio, a delta, a count of days / months, a % or an ATR multiple.
SCHEMA = {
 "shared": {
  "min_oi":            Field(500, 0, 100000, "int", "Minimum open interest per leg", "Contracts outstanding at a strike. Below this you may not get out.", "so you can get out", 50, "contracts"),
  "oi_per_contract":   Field(10, 1, 100, "int", "… and at least this many times your contracts", "", "a 10-lot wants 100 open", 1, "×"),
  "max_leg_spread":    Field(0.50, 0.01, 5.00, "num", "Widest bid/ask allowed per leg", "The cost of getting in and out. Wider markets eat the edge.", "wide markets eat the edge", 0.05, "$"),
  "min_leg_volume":    Field(20, 0, 10000, "int", "Traded today per leg (warning only)", "Never vetoes a strike; it only adds a note.", "a quiet strike is flagged, not dropped", 10, "contracts"),
  "earnings_rule":     Field("none_inside", None, None, "choice", "Earnings inside the trade", "An earnings report before expiry is the one thing a stop cannot protect you from.", "not allowed / defined-risk trades only", None, ""),   # choices: none_inside | defined_risk_only - there is NO 'allowed'
  "monthly_only":      Field(False, None, None, "bool", "Monthly expiries only", "Third-Friday expiries have the deepest markets.", "", None, ""),
  "chart_constraint":  Field(True, None, None, "bool", "Strikes must respect the chart", "Short strikes under support / above resistance / outside the range, and under the trend line when there is one.", "the chart-derived rule (safety switch)", None, ""),
  "max_position_pct":  Field(10.0, 1, 100, "num", "Max of account in one trade (%)", "Contracts × max loss never above this.", "the 10% cap", 1, "%"),
  "GAP_MULT":          Field(2.0, 1.0, 5.0, "num", "Worst case allowed, as a multiple of your risk budget", "A gap through the stop may cost this many times what you planned to risk - never seven times.", "a gap may cost twice the budget, not seven times", 0.5, "×"),
  # nlv / risk_pct are READ from trade_prefs (nlv, risk_pct) and edited in the same drawer row; telegram lives in prefs['telegram'] (D4.2). Neither is in SCHEMA nor in the hash.
 },
 "credit_vertical": {   # bull_put, bear_call
  "short_delta_lo":    Field(0.20, 0.05, 0.50, "num", "Short strike delta, from", "≈ chance the stock is past the strike at expiry. 0.20 ≈ 1-in-5.", "≈ 70–80% chance it expires worthless", 0.01, ""),
  "short_delta_hi":    Field(0.30, 0.05, 0.50, "num", "… to", "", "", 0.01, ""),
  "width_atr_lo":      Field(0.5, 0.1, 5, "num", "Spread width, in ATRs, from", "Ticker-relative: ≈ $6–17 on LRCX (ATR 11.5), $1–3 on a $40 name. The $ figure is shown beside it.", "≈ ${lo_usd}–{hi_usd} on {sym}", 0.1, "× ATR"),
  "width_atr_hi":      Field(1.5, 0.1, 5, "num", "… to", "", "", 0.1, "× ATR"),
  "long_offset_max":   Field(3, 1, 6, "int", "Long strike at most this many listed strikes below", "3 lets the ATR width be met on $5-spaced chains.", "", 1, "strikes"),
  "credit_pct_min":    Field(25, 5, 60, "int", "Minimum credit, % of width", "The reward per dollar risked. 25–33% is the playbook's floor.", "you collect at least $0.25 per $1 risked", 1, "%"),
  "dte_lo":            Field(30, 7, 180, "int", "Days to expiry, from", "30–60 is the sweet spot: enough decay, still manageable.", "", 1, "days"),
  "dte_hi":            Field(60, 7, 180, "int", "… to", "", "", 1, "days"),
  "iv_gate_min":       Field(30, 0, 100, "int", "Sell only when IV rank is at least", "Below this, selling is not paid enough.", "", 1, ""),
  # the four EXIT lines of this tab (take profit 50% · stop 20% of max loss · 21 days · roll delta) are trade_prefs keys, not SCHEMA (D3.2)
 },
 "debit_vertical": {    # bull_call, bear_put
  "long_delta_lo":     Field(0.60, 0.3, 0.95, "num", "Long strike delta, from", "How much the option moves per $1 of stock. 0.60–0.70 moves like the stock without paying for deep ITM.", "moves about 60–70 cents per $1 of the stock", 0.01, ""),
  "long_delta_hi":     Field(0.70, 0.3, 0.95, "num", "… to", "", "", 0.01, ""),
  "short_delta_lo":    Field(0.25, 0.05, 0.6, "num", "Short strike delta, from (soft - the chart target decides)", "The leg you sell to cheapen the trade; the cap sits where the setup says the move ends.", "the strike you give the upside away at", 0.01, ""),
  "short_delta_hi":    Field(0.35, 0.05, 0.6, "num", "… to", "", "", 0.01, ""),
  "reward_cost_min":   Field(1.0, 0.2, 5, "num", "Minimum reward ÷ cost", "1.0 = you can make what you pay.", "the most you can make is at least what you pay", 0.1, "×"),
  "dte_lo":            Field(30, 7, 180, "int", "Days to expiry, from", "", "", 1, "days"),
  "dte_hi":            Field(60, 7, 180, "int", "… to", "", "", 1, "days"),
 },
 "long": {              # buy_call, buy_put
  "delta_lo":          Field(0.60, 0.3, 0.95, "num", "Delta, from", "", "stock-like, with ~35% less capital", 0.01, ""),
  "delta_hi":          Field(0.70, 0.3, 0.95, "num", "… to", "", "", 0.01, ""),
  "theta_pct_max":     Field(1.0, 0.1, 5, "num", "Daily decay, at most % of the premium", "What waiting costs you per day. 1% = a $500 option loses ≈ $5 a day.", "a $500 option may lose up to about $5 a day", 0.1, "%/day"),
  "dte_lo":            Field(45, 14, 365, "int", "Days to expiry, from", "45–90 gives the move time without buying a year of decay.", "", 1, "days"),
  "dte_hi":            Field(90, 14, 365, "int", "… to", "", "", 1, "days"),
  "premium_stop_pct":  Field(50, 10, 100, "int", "Rule stop: close at this % of the premium lost", "If the stock stop is not hit but the option bleeds. Shared with the spreads and the time strategies.", "the rule stop; the chart stop usually fires first", 5, "%"),
 },
 "leaps": {
  "delta_lo":          Field(0.70, 0.5, 0.95, "num", "Delta (deep in the money), from", "", "behaves like 70–80 shares per contract", 0.01, ""),
  "delta_hi":          Field(0.80, 0.5, 0.95, "num", "… to", "", "", 0.01, ""),
  "extrinsic_pct_max": Field(10, 1, 40, "int", "Max time value, % of the STOCK price", "What you pay for time rather than stock, measured against the share price.", "you pay at most 10% of the share price for time", 1, "%"),
  "months_lo":         Field(9, 6, 36, "int", "Months to expiry, from", "", "", 1, "months"),
  "months_hi":         Field(18, 6, 36, "int", "… to", "", "", 1, "months"),
  "roll_dte":          Field(180, 60, 365, "int", "Roll out when this many days remain", "", "the roll date", 30, "days"),
  "delta_floor":       Field(0.55, 0.3, 0.7, "num", "Roll down-and-out if delta falls under", "", "delta drift", 0.01, ""),
  "premium_stop_pct":  Field(40, 10, 100, "int", "Rule stop: close at this % of what you paid", "The weekly trend may hold but the position has not. The diagonal's long leg inherits it.", "down 40% of what you paid = out", 5, "%"),
 },
 "condor": {
  "short_delta_lo":    Field(0.15, 0.05, 0.35, "num", "Short strike delta each side, from", "0.15–0.20 ≈ an 80–85% chance each side expires worthless.", "≈ 1-in-6 chance on each side", 0.01, ""),
  "short_delta_hi":    Field(0.20, 0.05, 0.35, "num", "… to", "", "", 0.01, ""),
  "wing_atr_lo":       Field(0.5, 0.1, 5, "num", "Wing width, in ATRs, from", "The $ figure is shown beside it.", "", 0.1, "× ATR"),
  "wing_atr_hi":       Field(1.5, 0.1, 5, "num", "… to", "", "", 0.1, "× ATR"),
  "credit_pct_min":    Field(30, 5, 60, "int", "Minimum credit, % of the wider wing", "", "", 1, "%"),
  "dte_lo":            Field(30, 7, 120, "int", "Days to expiry, from", "", "", 1, "days"),
  "dte_hi":            Field(45, 7, 120, "int", "… to", "", "", 1, "days"),
  "roll_delta":        Field(0.30, 0.1, 0.6, "num", "Act when either short delta reaches", "", "", 0.01, ""),
  "loss_stop_pct_credit": Field(100, 25, 300, "int", "Rule stop: loss as % of the credit", "", "close when the loss equals the credit you took", 5, "%"),
 },
 "time": {              # calendar, diagonal_call
  "cal_front_lo":      Field(20, 7, 60, "int", "Calendar: near expiry days, from", "", "", 1, "days"),  "cal_front_hi": Field(30, 7, 60, "int", "… to", "", "", 1, "days"),
  "cal_back_lo":       Field(50, 30, 180, "int", "Calendar: far expiry days, from", "", "", 1, "days"), "cal_back_hi": Field(70, 30, 180, "int", "… to", "", "", 1, "days"),
  "cal_delta_tol":     Field(0.05, 0.01, 0.2, "num", "How far from delta 0.50 the strike may sit", "At the money.", "", 0.01, ""),
  "cal_take_pct":      Field(25, 5, 100, "int", "Calendar: take profit at this % of the debit", "Calendars pay in small steps.", "", 5, "%"),
  "diag_long_delta_lo": Field(0.70, 0.5, 0.95, "num", "Diagonal: long call delta, from", "", "", 0.01, ""), "diag_long_delta_hi": Field(0.80, 0.5, 0.95, "num", "… to", "", "", 0.01, ""),
  "diag_long_dte_lo":  Field(180, 90, 730, "int", "Diagonal: long call days, from", "6–12 months.", "", 30, "days"), "diag_long_dte_hi": Field(365, 90, 730, "int", "… to", "", "", 30, "days"),
  "diag_short_delta_lo": Field(0.20, 0.05, 0.5, "num", "Diagonal: short call delta, from", "", "", 0.01, ""), "diag_short_delta_hi": Field(0.30, 0.05, 0.5, "num", "… to", "", "", 0.01, ""),
  "diag_short_dte_lo": Field(30, 7, 90, "int", "Diagonal: short call days, from", "", "", 1, "days"), "diag_short_dte_hi": Field(45, 7, 90, "int", "… to", "", "", 1, "days"),
 },
}
FIELDS = SCHEMA     # the one table; the drawer iterates TAB_BLOCKS[tab] over it
```

Every default is the §5 catalog row, verbatim. The four credit **exit** lines on the Credit tab
(`take_pct`, `loss_stop_pct`, `dte_floor`, `roll_delta` as the drawer labels them) are the existing
`trade_prefs` keys (`spread_profit_target_pct`, `spread_loss_stop_pct`, `spread_dte_floor`,
`spread_roll_delta`, `services/trade_prefs.py:50-58, 121-125`), written through `tp.write()` so the
legacy Positions monitor (`spread_monitor.snapshot_rows`, which reads `tp.read`,
`routes/portfolio.py:96, 102`), `option_exits.grade` and this drawer can never disagree. Account
value and risk per trade are `tp.read(user)["nlv"]` / `["risk_pct"]` for the same reason. The
width-in-strikes field of an earlier draft does not exist: widths are **ATR multiples** (CLAUDE.md:
every threshold ticker-relative) with the `$` translation rendered beside them from the selected
ticker's ATR (`rule_words`).

```python
def read(db, user) -> dict:
    """House defaults with this member's overrides on top, per field - the SAME merge
    ema_setup.clean_enabled does for sym_conds (missing key -> default; a stored value
    outside [lo, hi] -> default, never raise). Returns the seven blocks plus
    {"overridden": {block: [keys]}, "hash": prefs_hash(merged), "nlv": ..., "risk_pct": ...}
    (nlv / risk_pct from trade_prefs.read, B4.1)."""

PICK_FIELDS = {   # the ONLY fields prefs_hash() hashes: what changes a pick. Sizing inputs, exit lines and telegram never do.
  "shared": ("min_oi", "oi_per_contract", "max_leg_spread", "min_leg_volume", "earnings_rule", "monthly_only", "chart_constraint"),
  "credit_vertical": ("short_delta_lo", "short_delta_hi", "width_atr_lo", "width_atr_hi", "long_offset_max", "credit_pct_min", "dte_lo", "dte_hi", "iv_gate_min"),
  "debit_vertical": ("long_delta_lo", "long_delta_hi", "short_delta_lo", "short_delta_hi", "reward_cost_min", "dte_lo", "dte_hi"),
  "long": ("delta_lo", "delta_hi", "theta_pct_max", "dte_lo", "dte_hi"),
  "leaps": ("delta_lo", "delta_hi", "extrinsic_pct_max", "months_lo", "months_hi"),
  "condor": ("short_delta_lo", "short_delta_hi", "wing_atr_lo", "wing_atr_hi", "credit_pct_min", "dte_lo", "dte_hi"),
  "time": ("cal_front_lo", "cal_front_hi", "cal_back_lo", "cal_back_hi", "cal_delta_tol",
           "diag_long_delta_lo", "diag_long_delta_hi", "diag_long_dte_lo", "diag_long_dte_hi",
           "diag_short_delta_lo", "diag_short_delta_hi", "diag_short_dte_lo", "diag_short_dte_hi"),
}

def prefs_hash(merged: dict) -> str:
    """First 12 hex of sha1 over canonical_json({block: {k: merged[block][k] for k in PICK_FIELDS[block]}})
    (sorted keys, floats rounded to 4 dp). NEVER nlv, risk_pct, max_position_pct, GAP_MULT, premium_stop_pct,
    roll_*, delta_floor, loss_stop_pct_credit, cal_take_pct, the trade_prefs exit lines or telegram - so a
    changed account value never invalidates a cached pick, and members on house defaults share ONE signal row."""

def for_strategy(prefs: dict, strategy: str) -> dict:
    """The flat dict strike_picker.pick reads: SCHEMA[FAMILY_OF[strategy]] merged values + 'shared'."""

def defined_risk(strategy: str) -> bool: return strategy in DEFINED_RISK

def write(db, user, tab: str, form: dict) -> tuple[dict, str]:
    """Store ONLY fields that differ from the house default, for every block of TAB_BLOCKS[tab]
    (so a later admin change to a default flows through to everyone who never touched it); drop a
    field back to 'no override' when the posted value equals the default. Out-of-range input is
    REPORTED, not clamped (trade_prefs.write, services/trade_prefs.py:94-134). A checkbox that is
    absent from the form is False (spreads_filter, routes/spreads.py:99-104). Recomputes and stores
    user_option_prefs.prefs_hash and schema_version. nlv / risk_pct / the four credit exit lines on
    the posted form go to trade_prefs.write."""

def reset(db, user, tab: str | None) -> dict: ...
```

#### D3.2 The drawer template

Context: `tab, tabs (list of (key, label, n_overridden)), sections [(block, block_label, fields)] (TAB_BLOCKS[tab], each field with the merged value, overridden flag, kind, choices, words, is_safety), prefs, err, msg, nlv, risk_pct, exit_lines (credit tab: the four trade_prefs values), telegram (shared tab: D4.2), translation (one line per tab), atr, sym (the selected ticker, for the $ beside ATR widths)`.

```jinja
<div class="opt-rules" id="optRulesPanel">
  <div class="flex flex-wrap items-center gap-1 mb-2">
    {% for key, label, n in tabs %}
    <button type="button" hx-get="/options/rules?tab={{ key }}" hx-target="#optRulesBody" hx-swap="innerHTML"
            class="text-[11px] px-2.5 py-1 rounded border {% if key == tab %}border-emerald-500/60 text-emerald-300 bg-emerald-500/10{% else %}border-slate-700 text-slate-400{% endif %}">
      {{ label }}{% if n %} <span class="text-amber-300" title="{{ n }} rule(s) changed from the house default">·{{ n }}</span>{% endif %}</button>
    {% endfor %}
    <span class="ml-auto flex items-center gap-1">
      <button type="button" hx-post="/options/rules/reset" hx-vals='{"tab":"{{ tab }}"}' hx-target="#optRulesBody" hx-swap="innerHTML"
              hx-confirm="Put the {{ label }} rules back to the house defaults?" class="text-[10px] text-slate-500 hover:text-rose-300">Reset this tab</button>
      <button … hx-vals='{"tab":"all"}' hx-confirm="Put EVERY rule back to the house defaults?">Reset all</button>
    </span>
  </div>
  <p class="text-[11px] text-slate-400 mb-2">{{ translation }}</p>   {# the one-line translation FIRST, e.g. "Sell a put spread with the short strike at delta 0.20–0.30 (≈ 70–80% chance it expires worthless), 30–60 days out, 0.5–1.5 ATR wide (≈ $6–17 on LRCX), for at least 25% of the width, only when IV rank ≥ 30, with the short strike under support and the trend line. Take profit at 50%, stop at 20% of max loss, out by 21 days." #}
  {% if err %}<div class="… rose">{{ err }}</div>{% endif %}{% if msg %}<div class="… emerald">{{ msg }}</div>{% endif %}
  <form hx-post="/options/rules" hx-target="#optRulesBody" hx-swap="innerHTML">
    <input type="hidden" name="tab" value="{{ tab }}">
    {% for block, block_label, fields in sections %}
    <details class="opt-rules-sec" {% if tab != 'debit' %}open{% endif %}>      {# the debit tab's three sub-sections are collapsed by default on < lg (the page script opens them on lg+) #}
      <summary class="text-[11px] text-slate-300 cursor-pointer">{{ block_label }} <span class="text-slate-500">· change these</span></summary>
      <div class="grid gap-x-4 gap-y-1.5 sm:grid-cols-2 lg:grid-cols-3">
      {% for f in fields %}
      <label class="text-[10px] text-slate-500 flex flex-col" title="{{ f.help }}">
        <span>{{ f.label }}{% if f.overridden %} <span class="{% if f.is_safety %}text-rose-300{% else %}text-amber-300{% endif %}" title="changed from the house default {{ f.default }}">·</span>{% endif %}</span>
        {% if f.kind == 'bool' %}<input type="checkbox" name="{{ block }}.{{ f.key }}" {% if f.value %}checked{% endif %} class="accent-emerald-500 mt-1"
             {% if f.is_safety %}data-safety="Without this, the short strike can sit inside the zone the chart says must hold."{% endif %}>
        {% elif f.kind == 'choice' %}<select name="{{ block }}.{{ f.key }}" class="…"
             {% if f.is_safety %}data-safety="Without this, a trade can run through an earnings report - the one thing a stop cannot protect."{% endif %}>{% for v, lbl in f.choices %}<option value="{{ v }}" {% if v == f.value %}selected{% endif %}>{{ lbl }}</option>{% endfor %}</select>
        {% else %}<input type="number" name="{{ block }}.{{ f.key }}" value="{{ f.value }}" min="{{ f.lo }}" max="{{ f.hi }}" step="{{ f.step }}" class="w-24 …"> <span class="text-slate-600">{{ f.unit }}</span>{% endif %}
        <span class="text-slate-600">{{ f.words }}</span>     {# option_words.rule_words(block, key, value, atr): the one-line translation, re-rendered after save #}
      </label>
      {% endfor %}
      </div>
    </details>
    {% endfor %}
    {% if tab == 'credit' %} …the four exit lines (take profit % · stop % of max loss · days left · roll delta), same names as the Positions page's default form (trade_prefs keys)… {% endif %}
    {% if tab == 'shared' %} …account value (nlv) / risk per trade (risk_pct) inputs, same names as /curated/prefs (routes/curated.py:356-377); the Telegram rows (D4.2)… {% endif %}
    <div class="flex items-center gap-2 mt-1">
      <button type="submit" class="px-3 py-1 rounded bg-emerald-600 text-white text-[11px]">Save {{ label }} rules</button>
      <span class="text-[10px] text-slate-600">Saving re-reads the strikes on the card from the stored chain - no market call.</span>
    </div>
  </form>
</div>
```

`choice` labels for `earnings_rule`: `none_inside` → "not allowed", `defined_risk_only` → "defined-risk trades only". There is no third value - an undefined-risk trade through earnings is not something the house offers.

**Safety switches** (`is_safety`: `shared.chart_constraint`, `shared.earnings_rule`): unticking the box / choosing `defined_risk_only` shows the `data-safety` sentence inline in amber **before** Save (page script on `change`), and once saved the override dot is **rose** instead of amber. A non-technical member never turns a safety off with one click and no consequence shown.

`rule_words` examples: `short_delta_lo/hi` → "≈ 70–80% chance the short strike expires worthless"; `width_atr_lo/hi` 0.5–1.5 → "≈ $6–17 wide on LRCX (ATR 11.5)"; `credit_pct_min` 25 → "you collect at least $0.25 per $1 risked"; `theta_pct_max` 1.0 → "a $500 option may lose up to about $5 a day"; `extrinsic_pct_max` 10 → "at most 10% of the share price pays for time"; `GAP_MULT` 2.0 → "a gap through the stop may cost up to twice what you planned to risk"; `premium_stop_pct` 40 → "out when the option is down 40% of what you paid"; `min_oi` 500 → "at least 500 contracts open at each strike". The chart stop and target are not fields: the help text on the debit tab says "The stop sits 1 ATR under the entry (and under the level that must hold); the target at 2R - the Curated convention, not a setting."

#### D3.3 Save flow and re-render

1. `POST /options/rules` (one form, one tab) → `option_prefs.write(db, user, tab, form)` → re-render `_options_rules.html` for the same tab (target `#optRulesBody`, so the drawer stays open and shows the override dots and the new translation line).
2. The response sets `HX-Trigger: {"options:rules-changed": {"tab": "credit", "hash": "…"}}`.
3. `#optPicks` (`hx-trigger="options:rules-changed from:body"`) re-GETs `/options/picks/{sym}?strategy=…` → `card_for` misses on the new hash and computes the picks from `option_store.latest_chain` (DB) - **no chain fetch**; `option_sizing.size` runs again at read time; the picks table, nearest miss, the sizing line, the `#optPayoff` container and the hidden `pick` inputs re-render; the page script then calls `thChartSetStrikes` for pick 0. When only `nlv` / `risk_pct` / exit lines / `GAP_MULT` changed, the hash is unchanged (D3.1 `PICK_FIELDS`) and the re-render is a cache hit that only re-sizes.
4. `#optBasket` listens to the same event and re-renders (the idea column's three states can change).
5. The headline, chips and the chart do **not** re-render: the recommender's verdict depends on the gates in `shared` and `iv_gate_min` - when the saved tab is `shared` (or `credit`'s `iv_gate_min` changed), the trigger payload carries `"recompute": true` and the card's `#optChips` also re-GETs `/options/card/{sym}` (whole card, chart remount accepted; it is the rare case). The headline is still the stored sentence.
6. The summary in the `<summary>` (`#optRulesSummary`) updates via an OOB swap in the same response: "- 3 rules changed" / "- house defaults".
7. The `[remember this]` click after a Live press (D1.10) is this same POST with `tab=shared&nlv=<figure>`; it writes `trade_prefs.nlv` and nothing else.

"Reset" semantics: `tab=credit` deletes the `credit_vertical` block from the row (the four exit lines
are reset through `tp.write` to their `DEFAULT_*`); `tab=debit` deletes `long`, `debit_vertical` and
`leaps`; `tab=all` deletes the whole `prefs` dict except `telegram` and resets the four `trade_prefs`
spread keys; `nlv`/`risk_pct` are **never** reset by this drawer (they are shared with Curated).
Both answer with the same `HX-Trigger` so the picks re-render; `prefs_hash` is recomputed on every
write and reset.

---

### D4. Telegram push - the ONE implementation

`services/telegram.py` (sender) + `services/telegram_push.py::run(db, as_of, dry_run)` + table
`option_idea_push`, called by step 5 of A's nightly job. A push timestamp on the signal row, a
second push module on A's side and an exit-notify function on B's side do not exist; the exit-line
push for open trades stays on the existing Discord path (`deploy/portfolio_daily_check.py:120-129`).

#### D4.1 Sender - reuse the credential lookup, not a copy of it

`scripts/_common.py:562-574` (`telegram_env`) resolves `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`
through `_env_lookup("telegram.env")` → `INTRADAY_ENV_DIR` → `TradeHunter/.env` →
`cfg["vault_dir"]/telegram.env` → auto-discovered VAULT (`:154-219`), with `matp.env` as the
legacy filename. `send_telegram(cfg, html)` (`:577-618`) chunks at 4000 chars and POSTs
`parse_mode=HTML`. It has no `chat_id` parameter, and a multi-member platform needs one. So:

```python
# app/services/telegram.py
"""Telegram for the Options page. Credentials come from the intraday bot's lookup
(scripts/_common.telegram_env) so the token lives in the vault once; the chat id is
per member (prefs['telegram']) with the vault's TELEGRAM_CHAT_ID as the admin's default."""
from . import resources_bridge            # puts TradeHunter/ on sys.path (resources_bridge.py:19-21)
from scripts import _common as thc        # stdlib-only module import; no alpaca at import time

def configured() -> bool:
    token, _ = thc.telegram_env(None); return bool(token)

def send(html: str, *, chat_id: str | None = None) -> tuple[bool, str]:
    """One message (chunked like thc.send_telegram at 4000 chars) to chat_id, or to the
    vault's default chat when chat_id is None. Returns (ok, error)."""

def answer_starts(db) -> int:
    """getUpdates (offset kept in option_jobs(job='telegram_poll').detail): for every /start seen,
    reply 'Your chat id is {id}. Enter it on the Options page → My rules → Shared and press
    Send code.' Returns the number answered. Called by run() and by POST /options/telegram."""

def send_code(chat_id: str) -> str:
    """Sends a 6-digit code to chat_id and returns it (the handshake, D4.2)."""
```

The 20-line POST is repeated here because `send_telegram` cannot take a chat id; the chunker
and the headers are copied from `:581-607` so the two behave identically. Import side effects:
`scripts/_common.py:30-36` inserts the TradeHunter layer folders into `sys.path` at import - harmless
for the web app (it already imports `resources.*` through `resources_bridge`).

#### D4.2 Opt-in, the chat-id handshake, quiet and pause

`user_option_prefs.prefs["telegram"] = {"enabled": bool, "chat_id": str|None, "verified": bool, "quiet": bool, "paused_until": "YYYY-MM-DD"|None, "pending": {"chat_id", "code", "expires"}|None}` - its own key in the JSON, never hashed (D3.1). Rows on the Shared tab, all posting to `POST /options/telegram`:

1. "Send me each new idea on Telegram (once, the morning it appears)" - checkbox `enabled`.
2. "My Telegram chat id" - text box + **[Send code]**. Help: "Open a chat with the TradeHunter bot and send /start (Telegram only lets a bot message you after that); the bot replies with your chat id. Type it here and press Send code."
3. "Code from the bot" - 6 digits + **[Verify]**. The drawer **accepts the chat id only with that code**: `send_code` stores `pending = {chat_id, code, expires: now + 10 min}`; `Verify` compares and sets `verified = True`; a mistyped id can never receive ideas because the code went to the typed id and the member cannot read it. Enabled + unverified → "enter the 6-digit code the bot sent you after /start" and not saved. An administrator with a blank chat id falls back to the vault's id (`verified` implied).
4. "Quiet - keep the ideas on the page, do not message me" - the `quiet` switch (the opt-in stays, nothing is sent).
5. "Pause for 7 days" - `paused_until`; the same action sits as a link in every message (`settings.public_url + "/options?pause=7"`, D2.1), so a member can stop the flow from the phone without finding the drawer.

#### D4.3 When, once, and the guards

Inside the nightly job (A4.2 step 5), after every basket ticker's signal is written, after
`option_exits.sweep`, and before the `option_jobs` row is finished:
`telegram_push.run(db, as_of=run_on, dry_run=args.telegram_dry_run)`.

```python
def idea_key(symbol: str, strategy: str, front_expiry: str) -> str:
    """'LRCX|bull_put|2026-11-20' - (symbol, strategy, front expiry) defines the idea. The same
    thesis with the short strike drifting one listed strike a day is NOT a new idea; a new expiry
    or a new strategy is. as_of is deliberately NOT in the key."""

def run(db, *, as_of: str, dry_run: bool = False) -> dict:
    """For every member with telegram.enabled and verified and not quiet and not paused:
    for every basket ticker, the latest signal row for THAT member's prefs hash (option_store.basket_rows_for):
      GUARDS - skip the ticker (and log why) when:
        signal.status != 'ok'                                  -> 'skipped: {status}'
        snapshot partial (iv_daily.partial)                    -> 'skipped: partial chain'
        signal.iv.provisional / basis == 'unknown'             -> 'skipped: IV history too short'
        signal.iv.earnings_date is None                        -> 'skipped: earnings date unknown'
        the recommended rule's step > current_step             -> 'skipped: not available yet'
        signal.as_of older than the last session close         -> 'skipped: stale'
        no 'recommended' strategy or no pick under this hash   -> (silent)
      DEDUPE: key = idea_key(...); the OptionIdeaPush row for (user, key), if any, holds short_strike + atr;
        push only if no row, or |pick.short_strike - row.short_strike| > 1 x atr (then UPDATE that row:
        sent_at, short_strike, score). Keys older than 45 days are pruned so a repeated setup months
        later is pushed again.
      CAP: at most 5 ideas per member per message, ordered by the recommended row's score; the rest
        become 'and N more on the page'.
    Send ONE message per member (chunked); record one OptionIdeaPush row per idea (ok / error;
    error='dry-run' on a dry run so the second run still dedupes). Soft-fail: an exception for one
    member is logged and the next member is processed; the job's exit code never depends on Telegram
    (portfolio_daily_check.py:120-129 does the same with Discord). Writes job.pushed.
    Returns {"members": n, "ideas": n, "sent": n, "failed": n, "skipped": {reason: n}}."""
```

The badge's `ideas_new` counts `OptionIdeaPush` rows since the last run that this member has not yet
opened (`GET /options` writes a per-member `prefs["options_seen_at"]` marker). The push never states a
contract count (sizing is a read-time, per-PC figure).

#### D4.4 Text (HTML parse mode)

```
<b>Ideas for tonight's US session (opens 21:30 Malaysia). Prices are last night's close.</b>
Options · 3 new ideas · Oct 3

<b>LRCX</b> ↗ uptrend · IV rank 62 over the last year → <b>sell a put spread</b>
Bounced off support 340 on high volume; riding a trend line with 3 touches.
Nov 20 330/320 put · collect ≈ $210 · risk $790 · about 74% chance of keeping it (estimate)
What has to happen: LRCX stays above 330 until Nov 20.
Stop: LRCX under 336.2 (≈ −$102 today) · earnings Oct 22 inside — defined-risk trades only
<a href="https://app.tradehunter.net/options?symbol=LRCX">open the card</a>

<b>ISRG</b> ↗ uptrend · IV rank 41 → <b>buy a call</b>
…
and 2 more on the page · <a href="https://app.tradehunter.net/options?pause=7">pause for 7 days</a>
```

First line, verbatim, every message: **"Ideas for tonight's US session (opens 21:30 Malaysia). Prices
are last night's close."** Then one line per element of the card's headline, the first pick in
"collect / risk / chance" wording with the chance as *"about {p}% chance of keeping it (estimate)"*
(debit: *"about {p}% chance of profit (estimate)"*), the **What has to happen** line, the chart stop
(`setup.plan.stop`, 336.2 on the fixture) with its T+0 dollar cost from C's `build()` dict, the
earnings flag, the deep link (`settings.public_url + "/options?symbol=…"`, `app/config.py:143-145`),
and the pause link. No greeks by name, no contract count - the same words as the page. Dry run
(`deploy/options_nightly.py --telegram-dry-run`) prints the message instead of sending.

---

### D5. The Positions migration (and the other two pages)

#### D5.1 Positions → the Positions tab of `/options`

| What | Moves / stays |
|---|---|
| the store | **`option_trades` + `option_trade_checks` for every strategy from step 1** (B7.1). The migration `f4a5b6c7d8e9` copies every OPEN `option_spreads` row into `option_trades` once (`strategy='bull_put'`, `family='credit_vertical'`, `note='migrated from option_spreads #<id>'`); `option_spreads` stays in place, read-only, for the legacy `/portfolio` until removal |
| the board | the new `_options_positions_tab.html` rendered by `GET /options/positions` (D1.14): the `_portfolio_list.html` layout (board, chart pane `#pfChartBody`, per-row drawer, close form, `focus=` ring, `:54`) rebuilt over generic legs; forms post to `/options/positions/{id}/close`; a "Track a trade by hand" `<details>` writes an `option_trades` row with `signal_id=None` |
| the per-row drawer | the latest `OptionTradeCheck` (mark, P/L, Δ, θ, vega, dte, state, action, reasons) + the check history; the payoff pane with the dot (`GET /options/payoff/{symbol}?trade=<id>`) |
| the chart pane | stays inside the board; because the Positions tab swaps the whole `#optPane`, it is the only chart in the DOM while that tab is open; `chart_spread.legs` from `trade.legs` (any family) |
| the expand button `#pfChartExpand` | its handler moves from `portfolio.html` into the page script (D2.1) |
| the default exit-lines form (`_portfolio_list.html:480-501`) | its four fields are the same `trade_prefs` keys the Credit tab edits (D3.1), so the two are one setting; the new tab shows them on the Credit tab only. Removed with the old pages in the later release |
| the nav badge | `/options/badge` (D1.15) reads `option_trade_checks`; `base.html` points at it (D1.1); the old `/portfolio/badge` stays for the legacy page |
| the nightly grading | **`option_exits.sweep(db)`** (B7.3) over `option_trades`, a step of `deploy/options_nightly.py` (D1.14). `deploy/portfolio_daily_check.py` + `spread_monitor.sweep` keep grading `option_spreads` for `/portfolio`, **unchanged** - `spread_monitor` is NOT modified (no `right='C'` change is needed: generic legs carry the right) |
| Discord push of positions at a line | unchanged (the legacy sweep); `option_exits.sweep`'s `actionable` set feeds the same Discord sender |
| "Track this" on the card | `POST /options/track-idea` creates the row (graded on the spot from the stored chain) and switches to the Positions tab with `focus=<id>` so the member lands on the trade they just tracked |
| the Options tab of the Watchlist pane (`_chart_pane.html:69-73`, `routes/options.py`) | **untouched**, including its `POST /options/track` (`routes/options.py:156-184`), which keeps writing `option_spreads` for that tab; those rows show on `/portfolio` until the old pages go |

#### D5.2 IV Rank → the basket; Spread → the screener suggestions

| Old page | Where its engine lives now |
|---|---|
| IV Rank "My list" (`prefs.ivscan_universe`) | `POST /options/basket/import source=ivscan_list` copies it in once; the import button shows how many there are and disappears once the basket has them all |
| IV Rank's stored TWS scan (`IVScanItem` rows, `models.py:904-915`) | `POST /options/basket/import source=ivscan_scan` |
| IV Rank "Whole market" (bridge `/scan`) | the basket's **Run my TWS scanner** button: same browser flow (`ivscan.html:268-297`), the symbols land as basket rows with `source="scanner"`; the three pair floors (IV rank > 30, price > 50, volume > 200k) are the scanner's own inputs and are remembered under the same `prefs.ivscan_criteria` key (`routes/ivscan.py:78-83`) so the two pages agree. Hidden on touch |
| the per-ticker IV rank read from TWS (`fillIV`, `ivscan.html:163-213`, `POST /ivscan/iv`) | replaced by (a) the server-side rank from `iv_daily` for every basket ticker, no TWS needed, with the basis and day count shown honestly (D2.2), and (b) the **Live** button's `/iv?series=1` bootstrap (D1.10, bridge 1.6, PERCENT), which stores the whole year instead of three numbers |
| the setup grading / switches (`sym_conds`, `_sector_conds.html`) | not on this page: the card's chart read comes from the signal (`chart_state.read` → one `ema_setup.analyze` call). `t1` (trend-line bounce) and `r1` (range) join `COND_KEYS` in `ema_setup`, so every `sym_conds` reader (Sector & Industry, Curated) sees two more switches; a note on the Shared tab says so |
| Spread screener (`spread_candidates`, nightly `deploy/spread_scan.py`) | the basket's "Suggested by last night's screener" section (D2.2): top candidates by `credit_pct` that pass the member's shared liquidity rules (`short_oi`, `long_oi ≥ min_oi`; both legs' `ask−bid ≤ max_leg_spread`) and the pair floors, excluding tickers already in the basket; `[+]` adds one (`source="screener"`), after which tonight's job gives it a full card. The Spread page's filter bar is not carried over - the card's rules replace it |
| Spread "Track" (`routes/spreads.py:147-168`) | the card's Track (D1.11) writes `option_trades`; the old form keeps writing `option_spreads` for `/spreads` until removal |

Both old routers stay registered with widened guards (D1.1) and their templates untouched. The
later removal release deletes `routes/ivscan.py`, `routes/spreads.py`, `routes/portfolio.py`'s
page shell (`GET /portfolio` only), the three page templates, the `HIDDEN_KEYS` entries, the
`iv_scan_items` table and `option_spreads` (drop migrations, after a final copy of any row still open).

---

### D6. Dashboard-visibility items

| Item | Surface | Source | States |
|---|---|---|---|
| **Nightly job health pill** | `#optStatus` strip (D2.1), left side: `job ✓ 07:17 MYT · 5/5 tickers` | `job_runs.latest(db, "nightly")` over `option_jobs` (A2.1 + `pushed`), `finished_at` localised via `localtime()` (`_time.html`) | emerald: `finished_at` set, `errors == 0`, `run_on == last ET trading day` (`spread_monitor.et_today()` minus weekend/holiday via the same helper the sweep uses, `services/spread_monitor.py:204-242`); amber: `errors > 0` ("5/7 tickers - KO: Cboe HTTP 403, …" from `detail`) or `run_on` one trading day behind; rose: no run for ≥ 2 trading days, or `started_at` set and `finished_at` null for > 2 h (crashed) |
| **Data-age badge** | card header (per ticker) and the basket dot | `card_for(...)["stale"]`, `["age_h"]`, `as_of`, `source`; the in-request `live` dict | `as of Oct 2, 16:00 ET · delayed` (emerald, < 20 h) / amber (20 h–3 trading days) / rose (older); `live · TWS 21:42 ET` for the live expiry only, in-request (nothing persisted); the strip's own age is the **oldest** basket ticker's |
| **"Hermes job missed" notice** | amber banner above the card, and the nav badge's `⚠` chip (`job_missed` in `/options/badge`) | `job_runs.missed(db, "nightly")`: no FINISHED run for the last ET trading day by 08:00 MYT (`latest.run_on < et_today() and now_myt.hour >= 8`) | "Last night's data job did not run (last run {{ when }}). The cards show {{ as_of }} data. Press Refresh on a ticker for today's delayed data; an administrator can read `/admin/log`." |
| **New-ideas count** | nav badge (`ideas_new`) and the Ideas tab label `Ideas ●3` | `OptionIdeaPush` since last run, not yet seen | cleared when `GET /options` renders |
| **Positions at a line** | nav badge (`urgent` / `watch` / stale `!`) and the Positions tab label `Positions ●2` | newest `option_trade_checks` row per open `option_trades` row + the earnings-now-inside rows (D1.14) | same meaning the `/portfolio/badge` painter already has |
| **IV history basis** | basket IV cell (`62` / `~62` dotted / `–`), the gauge line, the push guard | `signal.iv.basis`, `iv_n` | a provisional read is never amber and never pushed |
| **Bridge state** | the `[Live (TWS)]` button turns emerald with `· up` after a successful `/health` probe (the page probes once on load, `_options_tab.html:190-194` pattern); on touch the slot reads "Live quotes need TWS on your PC" | | `[Live (TWS)]` grey = not probed / down; `[Live (TWS) · up]` emerald; bridge version from `/health` - `< 1.6` shows "· older than 1.6 (no IV history)" |
| **Refresh in flight** | `hx-indicator` spinner on the button; the card dims (`htmx-request` class) | | |
| **Telegram** | Shared tab: verified ✓ / unverified / quiet / paused until …; the strip shows `· ideas paused` while paused | `prefs["telegram"]` | |

`_options_status.html` is rendered by `GET /options/status/strip` from the SAME dict `GET /options/badge`
returns (`{run_on, finished_at, ok, errors, stale, running, job_missed, ideas_new, urgent, watch}`) plus
`state ('ok'|'warn'|'bad'|'none'), as_of_oldest, n_basket, n_stale, bridge_port, delayed_or_live, paused`.
Markup is one `flex flex-wrap items-center gap-x-3 text-[11px]` line: `Options · {{ n_basket }} tickers ·
Data as of {{ as_of }} ET · {{ 'delayed' | 'live · TWS HH:MM ET' }} · <pill job> · [Refresh all stale ({{ n_stale }})]`
- the last button posts `/options/refresh/{sym}` for each stale ticker sequentially from the page script
with the cooldown respected (the server refuses bursts anyway).

The job that writes `option_jobs` is A's `deploy/options_nightly.py`; this part supplies
`services/job_runs.py` with `start(db, job, run_on) -> OptionJob`, `finish(db, run, *, ok, errors, rows, pushed, note, detail)`,
`latest(db, job) -> OptionJob | None`, `missed(db, job) -> bool` so the status strip, the badge
and the job all use one definition of "missed". There is no second job table.

---

### D7. Reviewer's checklist (non-technical member, 12 items)

A reviewer opens `/options` as a member with the house defaults and checks:

1. **Reading level.** Every sentence on the card (headline, picks, payoff caption, empty states) reads at or below a grade-8 level: no sentence over 25 words, no word a non-trader would look up without a hover. Test: read the LRCX card aloud to someone who does not trade.
2. **No raw greek without words, no chance without its limits.** Every delta, theta, vega, IV, IV rank, OI and bid/ask number on screen has the D2.7 sentence as its `title` or beside it (gamma only inside the full-chain expander). The strike table's chance column says "about … chance of keeping it" / "about … chance of profit" and its hover carries "an estimate … not a promise"; never "POP" or "1−Δ". Every IV rank says over how many days.
3. **No jargon in buttons.** Button labels are verbs a non-technical person understands: `Refresh`, `Live (TWS)`, `Order ticket`, `Track this`, `Add ticker`, `Import`, `Save Credit spreads rules`, `Reset this tab`, `Send code`. Not "Analyze", "Rescan", "Ingest", "Grade", and never a build-step number.
4. **One primary action per card.** Exactly one filled button on the card - `Order ticket` - and only for a recommended / also-fits strategy; on a rejected one it is the ghost `Order ticket (not recommended)`, and on an earnings-inside rejection there is no ticket button at all. `Track this`, `Refresh`, `Live` are ghost buttons.
5. **The headline does the work.** Covering the chips, picks and chart with a hand, the headline sentence alone tells the member what the chart is doing, why, and what to do. Trend + evidence, setup, IV regime (with its basis), conclusion - all four present or honestly absent ("No fresh setup today"). Under the chips, the **What has to happen** line says the one thing the trade needs.
6. **Rejections explain themselves.** Every greyed chip carries a reason in plain words; clicking it shows the amber "Not recommended today: …" banner, the strikes for every reason except earnings inside (then nothing to price), and "not available yet" for an unbuilt strategy. "Other strategies" hides the rest; nothing is silently missing.
7. **Honesty strip always present.** Data age, delayed/live, and the job pill are visible on every state of the page, including the empty basket, and the age colour matches the badge rules in D6. Pull the network and press Refresh: the card says what failed and keeps the old data. After a Live press only the live expiry is shown and each row says so.
8. **Both stop lines, labelled.** The payoff chart shows the chart stop (the engine's 336.2 on the fixture, not a hand number) and the rule stop, each with its price and dollar cost, and the caption under the chart says the dashed line is an estimate in one sentence.
9. **Numbers are in money first, and the gap is never hidden.** Collect / risk / chance before delta or ratio; `$` units by default, `R` only on the toggle; the sizing line always shows both figures ("about $990 if the stop fires, up to $6,830 (6.8% of your account) if the stock gaps past it"), and `0 contracts` is said plainly, never rounded up to one.
10. **Layout rules.** No horizontal page scroll at 375 px; the basket becomes a chip strip; the Live button is absent on a phone with its one-line hint; one chart in the DOM at a time (switch Ideas/Positions and check `document.querySelectorAll('#priceChart').length === 1`); every scroll area's scrollbar is invisible until hovered (no `scrollbar-*` / `::-webkit-scrollbar` in the new templates); light theme: the teal IV number, the amber and rose override dots and the payoff legend are legible on white.
11. **The ticket says when its prices are from.** The header line names the price time and tells a Malaysian member to Refresh after 21:30; while the US session is open on last night's prices the "Refresh first" banner shows; the entry order has no condition unless "Enter on the dip" was ticked (and then the crash sentence is printed); the chart-stop order carries the live-vs-close sentence and "Trigger outside RTH: No" in both renderings; the moomoo stop closes the short leg first.
12. **Nothing provisional looks firm.** A ticker with 34 days of IV history shows `~62` in grey, the gauge says "against the last 34 days (not a full year yet)", and no Telegram idea goes out for it.

---

### D8. Test plan

#### D8.1 Browser walkthrough (dev-check config)

Uses `.claude/launch.json` → `tst-devcheck`: password auth, the admin from `TST_ADMIN_EMAIL`/`TST_ADMIN_PASSWORD` in that entry, `TST_DATABASE_URL=sqlite:///…/tst_dev_check.db`, port **8011**. Machine: **Laptop**, two PowerShell windows (one for the server, one for the job), both with the same `TST_*` env vars from the launch entry so the job writes the same dev DB.

| # | Step | Expected |
|---|---|---|
| 1 | Start `tst-devcheck`; open `http://127.0.0.1:8011/`, sign in as the dev admin | lands on Calendar; the nav reads `Calendar · Sector & Industry · Watchlist · Curated · Options`; no Options dropdown; `/ivscan`, `/spreads`, `/portfolio` still open by URL; `alembic current` prints `f4a5b6c7d8e9` and the nine tables exist |
| 2 | Click **Options** | shell renders in < 200 ms; status strip: `0 tickers · no data yet · job: never run` (slate); basket empty-state text (D2.9); pane empty-state text; My rules closed with "- house defaults"; opening My rules fires ONE request to `/options/rules?tab=shared` (the `toggle from:closest details once` trigger) |
| 3 | `curl -s -o /dev/null -w "%{http_code}" -b <cookie> http://127.0.0.1:8011/options/basket` and `/options/rules?tab=credit` | both `200` with the fragment, **not** the legacy `_options_tab.html` shell (proves the router order, D1.1) |
| 4 | Import → Paste `LRCX, MA, ISRG, NVDA, KO, lrcx, XX1234567890123` → Save | basket shows 5 rows (duplicate and junk dropped; toast "5 tickers added, 2 dropped"); each row has a rose dot, `·`, `–`, `no read`; strip says `5 tickers`; `option_basket` rows carry `owner_key='u1'`, `added_on` today's ET date, `active=1` |
| 5 | Click LRCX | card: header `LRCX · no read yet` state text; chart mounts with EMA 20/50/200 and no overlays; no chips/picks/payoff; `[Refresh]` enabled |
| 6 | Press **Refresh** | ≤ 3 s: note strip "Refreshed from Cboe HH:MM ET"; the stored headline sentence; gauge with its basis ("against the last N days" on a short history); chips in the decision-9 order; the What-has-to-happen line; chart gains the support line (if the detector found one), the strike lines, the trend line as a line series with `TL n/n` markers and the range lines when found; picks table (≤ 3 rows) with collect (range) / risk / chance; the sizing line with both figures; `#optPayoff` holds C's SVG with both stop markers labelled and the caption; basket row's dot turns emerald and its idea word fills in; pressing Refresh again inside 60 s → toast "Just refreshed - try again in a minute."; `option_jobs` has a `job='refresh'` row |
| 7 | Click the second pick row | strike lines on the chart move without the chart remounting (the EMA legend does not flicker; `window.__thSetup` identity unchanged); `#optPayoff` re-requests `/options/payoff/...&pick=1`; the sizing line changes; `#optActions` hidden `pick` = 1; **no** request to `/options/chart` in the network tab |
| 8 | Click a greyed chip (e.g. `Buy call · expensive`) | picks re-render with the amber "Not recommended today: options are expensive (IV rank …)" banner; strikes shown; the primary button reads `Order ticket (not recommended)` in the ghost style; chart lines switch to the Entry/SL/PT read-only set (long family); payoff shows target + both stops. Then set the dev DB's `earnings_date` inside the window and click a chip rejected for `earnings_inside`: banner, **no strike table, no buttons** |
| 9 | Open My rules → Credit spreads; set `short_delta_hi` 0.30 → 0.22; Save | drawer re-renders with an amber dot on that field and "·1" on the tab; picks re-render (watch the server log: `card_for` computes under the new hash, **no** `fetch_chain` line); degenerate text names the rule when nothing passes; basket idea column may fade for tickers that lost their picks |
| 10 | Reset this tab | override gone; picks back to the 0.30 set; `#optRulesSummary` "- house defaults" |
| 11 | Shared tab → set account value 100 000, risk 1% → Save | the contracts box re-fills from `pick.sizing.contracts` (`min(by_chart_stop, by_max_loss, by_notional)`), the sizing line shows both figures; set account value 5 000 → `0` contracts and "Not even one contract fits your 1% - lower the risk or choose a narrower spread", Track disabled; the Curated page shows the same NLV (`/curated` prefs strip); the `prefs_hash` in `user_option_prefs` did NOT change |
| 12 | Untick "Strikes must respect the chart" | the amber sentence appears before Save; after Save the dot is rose; re-tick and Save |
| 13 | **Order ticket** | panel with the TWS and moomoo tabs, the header line "Prices are from … Press Refresh after 21:30 Malaysia time …", correct expiry label, limit = credit at the mid rounded to 0.05, **no entry condition**; tick "Enter on the dip" → the condition line and the crash sentence appear; ORDER 2 carries both sentences and "Trigger outside RTH: No" in BOTH tabs; the moomoo tab closes the short leg first with the naked-put sentence; the ticket's first line is the rejection sentence when opened from a rejected chip; `[Copy]` puts the visible text on the clipboard; opening the full-chain expander fires ONE request with the pick's expiry and ≤ 25 rows, "show all strikes" loads the rest |
| 14 | **Track this** (the sized count) | pane switches to Positions with the new row ringed; `option_trades` has one row with `strategy='bull_put'`, `family='credit_vertical'`, `signal_id` set, legs carrying `entry_price/entry_delta/entry_iv` and `oi`; one `option_trade_checks` row written from the stored chain (Δ and P/L present, no network); nav badge unchanged (nothing at a line); `option_spreads` unchanged |
| 15 | Press **Live (TWS)** with no bridge running | ≤ 2 s: note strip's bridge text with `[Start the bridge]` `[Retry]` and the diagnostics `<details>`; card otherwise unchanged |
| 16 | With TWS + bridge 1.6 running (laptop only): **Live** | note "Live from your TWS HH:MM ET"; age badge `live · TWS HH:MM ET`; the picks table holds the live expiry only with `live HH:MM ET` per row and "show delayed expiries" below; `iv_daily` gains ~250 `source='ibkr'` rows for LRCX in PERCENT (values 10–150, not 0.1–1.5); `option_chain_snapshot` row count for LRCX **unchanged**; the card shows both IV ranks ("62 (TWS, live) · 59 (delayed)"); the sizing line says "from TWS · [remember this]" and `trade_prefs.nlv` is unchanged until the click |
| 17 | Run the nightly job once by hand, same env: `py deploy\options_nightly.py --telegram-dry-run` (**Laptop**, the job window) | log: 5 tickers done / 0 failed; `option_jobs` row with `job='nightly'`, `pushed`; the dry-run Telegram text printed with the first line "Ideas for tonight's US session (opens 21:30 Malaysia). Prices are last night's close.", one block per ticker that has a recommendation and picks, "about …% chance of keeping it (estimate)", no contract count; tickers with no earnings date logged "skipped: earnings date unknown"; `option_idea_push` rows written with `error='dry-run'` - re-running prints nothing new (dedupe) |
| 18 | Reload `/options` | strip pill emerald `job ✓ <time> · 5/5 tickers`; Ideas tab label `Ideas ●n`; nav badge shows the `n` chip; opening the page clears it on the next poll |
| 19 | In SQLite set the job row's `run_on` two trading days back and `option_signal.snap_on` to 4 days ago; reload | amber "job missed" banner text (D6); age badge rose; nav badge `⚠` chip |
| 20 | Positions tab: Mark closed → Closed filter | board posts to `/options/positions/{id}/close` and re-renders inside `#optPane`; the row shows under Closed; `/portfolio` by URL still shows only the legacy `option_spreads` rows |
| 21 | Theme toggle to light | teal IV number legible; amber and rose dots legible; the payoff SVG recolours through the `--po-*` tokens without a reload; chips readable (no pale-on-pale) |
| 22 | Resize to 375 px (or `resize_window mobile`) | basket is a horizontal chip strip; card full-width; chart 300 px tall; strike rows stack; the Live button is absent and "Live quotes need TWS on your PC" shows; no horizontal page scroll; My rules a bottom `<details>` with the debit tab's three sub-sections collapsed and the translation line first |
| 23 | Hover each scroll area | scrollbar appears only on hover (basket, pane, rules body, full-chain expander, the Positions drawer) |
| 24 | Watchlist → a ticker → Options tab → "Track this trade" (legacy form) | `POST /options/track` is answered by `routes/options.py` exactly as before (no 303, no 422); the row lands in `option_spreads` and shows under the tab and on `/portfolio` |
| 25 | Sign in as a non-admin member granted only `options` (Admin console) | `/options` opens; `/options/positions` opens; the Telegram checkbox refuses to save without a verified chat id; Send code → the bot message arrives at the typed id; Verify with a wrong code → rose line; with the right code → ✓ |
| 26 | With the US session open (or the dev clock patched) and a card older than the last close: **Order ticket** | the "Refresh first" banner with the inline Refresh; pressing it is never blocked by the 60 s cooldown; the ticket re-renders on the new prices |

#### D8.2 Synthetic cases (unit tests, `dashboard_tst/tests/test_options_page.py`; the tree `dashboard_tst/tests/` + `tests/fixtures/options/` and `requirements-dev.txt` (pytest) are created in step 1 and shared with A8 / B9 / C6)

| Case | Asserts |
|---|---|
| `option_prefs.read` with no row | every block equals the house defaults; `hash` stable across calls; `overridden == {}` |
| `write("credit", {"credit_vertical.short_delta_hi": "0.30"})` (equals default) | stores no override; `"0.22"` stores one; `"0.9"` returns the error "Short strike delta, … must be between 0.05 and 0.50." and stores nothing |
| `write("shared", {"nlv": "50000"})` / `write("credit", {"take_pct": "40"})` | `tp.read(user)["nlv"] == 50000`, `["profit_target_pct"] == 40` (written through `trade_prefs`); `prefs_hash` unchanged by either |
| `prefs_hash` | changes on `short_delta_hi`, `width_atr_lo`, `earnings_rule`, `chart_constraint`; unchanged on `GAP_MULT`, `premium_stop_pct`, `max_position_pct`, `telegram.*`, `nlv` |
| `reset("all")` | prefs row holds only `telegram`; `trade_prefs` spread keys back to defaults; `nlv` untouched |
| `chip_row` with 1 recommended, 2 also_fits, 7 rejected (2 with `shown=True`) | first = recommended; `also_fits` in order; exactly the 2 `shown` greys with their `reason_key`; 5 in `rest` |
| `chip_row` with 0 recommended and an `also_fits` row with `reason_key='not_available_yet'` | `first is None`; that chip's text is "not available yet"; the rendered card HTML never contains "step " |
| `headline` with every clause present / with no setup / with sideways / with `basis='percentile'` | the exact D2.7 sentences, punctuation included; "against the last 118 days (not a full year yet)" on the percentile case |
| `delta_words(0.25, "sell", "P")` | contains "below this strike at expiry" and "75% chance of keeping"; never "in the money" |
| `pop_words(0.74, "keep")` / `pop_words(0.46, "profit")` | start with "About a 74% chance of keeping the credit" / "About a 46% chance of profit if held to expiry"; both contain "estimate" or "not this number"; the profit one never says "keeping" |
| `sizing_line({"contracts": 10, "capital_at_risk_usd": 990, "max_loss_total_usd": 6830, "nlv": 100000})` | "10 contracts: about $990 if the stop fires, up to $6,830 (6.8% of your account) if the stock gaps past it"; with `contracts == 0` the "Not even one contract fits your 1%" sentence; the page never rounds a count up to one |
| `GET /options/payoff/LRCX?strategy=bull_put&pick=0` on the LRCX fixture | response is `_payoff_chart.html` (an `<svg viewBox="0 0 640 300">`), `po.max_loss == 790` (positive), `po.breakevens == [327.9]`, a marker `kind='stop'` at `x == 336.2`, a marker `kind='rule_stop'`, the caption with "an estimate"; `units=R` divides by `158.0` |
| the payoff route for a calendar / for `buy_call` | horizon = the front expiry; rule-stop marker present for `buy_call` (premium_stop_pct) and absent for `leaps_call` |
| `idea_key` / `telegram_push` dedupe | `('LRCX','bull_put','2026-11-20')` → one key for 330/320 and 325/315; a second run with the short strike moved 0.5 ATR sends nothing; moved 1.2 ATR re-sends and UPDATES the row |
| `telegram_push.run` guards | a ticker with `earnings_date None` is skipped and logged "skipped: earnings date unknown"; `provisional=True`, `status='no_setup'`, a stale `as_of`, a partial chain each skip; 7 ideas → 5 in the message and "and 2 more on the page"; the message's first line is the fixed sentence; it never contains "contracts" |
| `telegram_push.run` with two members, one opted out, one verified, `dry_run=True` | one message built; `OptionIdeaPush` rows only for the opted-in member; second run sends nothing; a member with `quiet=True` or `paused_until` in the future gets nothing |
| `POST /options/telegram` handshake | `chat_id` without a code → rejected; wrong code → rejected; right code → `verified=True` |
| `POST /options/track-idea` with a stale `pick` index | 409 + the toast trigger; no row |
| `POST /options/track-idea` for a strategy rejected for `earnings_inside` under `none_inside` | 409; no row; under `defined_risk_only` → 201 and the note begins "Not recommended:" |
| `POST /options/track-idea` for `bear_call` | row with `family='credit_vertical'`, legs `right='C'`, the first `option_trade_checks` row marks against the call chain (`mark > 0`) |
| `GET /options/ticket/LRCX` default / `?dip=1` / rejected for earnings | no condition line in either rendering; with `dip=1` the `Last ≤ 341.92` line and "will also fire if LRCX crashes through 341.92"; "Trigger outside RTH: No" appears in BOTH renderings; the moomoo text has "SHORT leg first" before "LONG leg"; the earnings-rejected case returns the one-line "No ticket" panel with no leg |
| `POST /options/live/LRCX` with a bridge payload | the response holds the live badge and only the live expiry's rows; `option_chain_snapshot` count unchanged; `iv_daily` gains rows with `iv30` in percent, each bounded 0.1..1000; a server-read day is not overwritten; `trade_prefs.nlv` unchanged |
| the legacy `POST /options/track` | still handled by `routes/options.py` (the new router has no `/track`); row in `option_spreads` |
| basket import of 70 symbols | 60 kept, `dropped == 10`, response says so; `source='positions'` imports the open `option_trades` symbols |
| `_basket_context` three states | a recommended ticker with `picks == []` → `pick_state 'none'`; with no entry under the hash → `'unchecked'`; the template text differs ("no strike passes" vs "not checked under your rules yet") |
| `GET /options/basket` as user A after user B adds LRCX | A's basket empty (scoping by `owner_key`) |
| router order | `app.routes` index of `GET /options/basket` < index of `GET /options/{symbol}`; the new router carries `require_menu("options")`, the legacy one does not |
| `job_runs.missed` at 07:59 MYT with no run today | `False`; at 08:00 → `True` |
| `/options/badge` | keys exactly `{run_on, finished_at, ok, errors, stale, running, job_missed, ideas_new, urgent, watch}`; an open trade whose newest check is `WATCH` with the earnings-now-inside reason counts in `urgent` |
| the chart fragment for a `long` strategy | context has `chart_levels` and **no** `chart_setup_seed`; for `credit_vertical` it has `chart_spread.legs` with two entries and `chart_levels` absent; `chart_trendline` carries `slope_per_bar` and `n_touches` |
| the templates | `options.html` and `_options_picks.html` contain `toggle from:closest details once`; no template contains `scrollbar-`, `::-webkit-scrollbar` or a client-side payoff painter (the SVG partial is the only payoff markup) |

#### D8.3 Live tickers (README "Tested:" line, to be filled in when built)

Tested: dev DB with LRCX, MA, ISRG, NVDA, KO (the §7 mock basket); the LRCX card after Refresh
(the stored headline with trend + support bounce + IV regime and its basis, bull put recommended,
the What-has-to-happen line, ≤ 2 greys with reasons, three picks in collect/risk/chance wording, the
sizing line with both figures, C's payoff SVG with the chart stop 336.2 and the rule stop both
labelled, the estimate caption); a pick click moving the strike lines without a chart remount; a
rule change re-rendering the picks with no Cboe call in the log; Reset; sizing from account value
incl. the 0-contract case; the order ticket in both renderings (no entry condition, the dip toggle,
ORDER 2 with the RTH line, the moomoo leg order); a rejected chip's ghost ticket and the
earnings-inside chip's empty state; Track → Positions tab with the `option_trades` row graded from
the stored chain; Live with the bridge down (calm panel) and up (live expiry only, IV series stored
in percent, snapshot untouched, `[remember this]`); the nightly job by hand with the Telegram dry
run (the fixed first line, guards logged, dedupe on the second run); the status pill in all four
states; the job-missed banner; light theme; 375 px with the Live hint; the legacy Watchlist Options
tab's Track still answered by the old router; `/ivscan`, `/spreads`, `/portfolio` still reachable by
URL for a member granted only `options`.

---

### D9. Files to create / modify (summary)

| Action | Path |
|---|---|
| create | `app/routes/options_page.py` (every route in D1.3, incl. A's four data endpoints under this part's paths) |
| create | `app/services/option_words.py`, `app/services/telegram.py`, `app/services/telegram_push.py`, `app/services/job_runs.py` |
| create (with B) | `app/services/option_prefs.py` - B4.1's SCHEMA with this part's `label/help/plain/step/unit` columns, `TABS`, `PICK_FIELDS`, the `write/reset/for_strategy` wrappers (D3.1) |
| create | `app/templates/options.html`, `_options_basket.html`, `_options_card.html`, `_options_picks.html`, `_options_chart.html`, `_options_rules.html`, `_options_ticket.html`, `_options_status.html`, `_options_chain.html`, `_options_positions_tab.html` |
| create (C) | `app/services/payoff.py`, `app/templates/_payoff_chart.html` - this part only targets the partial into `#optPayoff` |
| create (shared, A's skeleton) | `alembic/versions/f4a5b6c7d8e9_options_module.py` - the ONE migration, nine tables + the `option_spreads` copy step |
| create | `dashboard_tst/requirements-dev.txt` (pytest), `dashboard_tst/tests/test_options_page.py`, `tests/fixtures/options/` (shared with A8 / B9 / C6) |
| modify | `app/models.py` (`OptionBasket.pos`, `OptionIdeaPush`, `OptionJob.pushed`; the rest of the nine models are A's and B's), `app/main.py` (D1.1), `app/menus.py` (D1.1), `app/templates/base.html` (badge → `/options/badge`, href check, two chips, light-theme teal/violet inks; C4.2's `--po-*` tokens), `app/templates/_price_chart.html` (C4.4 items 1-6 + `window.thChartSetStrikes`), `app/services/glossary.py` (option group), `app/__init__.py` (`4.126` → `4.127`), `bridge/ibkr_bridge.py` (`/iv?series=1` in PERCENT, `server_version` 1.6), `README.md` (changelog entry per the folder convention + the Contents line for `tests/`), `OPTIONS_MODULE_DESIGN.md` (status line → "building, step 1") |
| untouched on purpose | `app/routes/options.py` (incl. its `POST /track`), `routes/ivscan.py`, `routes/spreads.py`, `routes/portfolio.py` (guards only change in `main.py`), their templates, `app/services/spread_monitor.py`, `app/services/bull_put.py` |
