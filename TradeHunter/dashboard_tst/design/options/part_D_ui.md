## D. The Options page: routes, templates, HTMX flows, My rules, Telegram push, and the migration of the three old pages

Scope of this part: everything a member touches. The data layer (A), the engines (B) and
the signal cache / nightly job (C) are referenced by the names listed in **D0** and in the
cross-part needs at the end; where this part calls a function the other parts own, the
signature it expects is written out so the names can be reconciled.

Conventions used below, all taken from the existing code:

| Convention | Where it is already done | Used here for |
|---|---|---|
| Page shell renders instantly, every slow panel is a lazy HTMX fragment | `routes/portfolio.py:149-161`, `templates/portfolio.html:8-12`, `routes/curated.py:165-179` | the basket, the card, the positions board, the status strip |
| One `_list_context()`-style builder shared by the GET and every POST that re-renders the same fragment | `routes/ivscan.py:179-233`, `routes/portfolio.py:88-146`, `routes/curated.py:37-162` | `_basket_context`, `_card_context`, `_picks_context`, `_rules_context` |
| A fragment is wrapped in a class (`.pf-panel`, `.sp-panel`) and swapped by `outerHTML`, so its forms target `closest .<class>` | `templates/_portfolio_list.html:5-8`, `templates/_spreads_list.html:6-8` | `.opt-basket`, `.opt-card`, `.opt-picks`, `.opt-rules` |
| Member preferences live in `User.prefs` JSON and are read through a cleaner that fills defaults (`es.clean_enabled`) | `services/ema_setup.py:455-462`, `services/trade_prefs.py:71-91`, `routes/ivscan.py:139-155` | the rules merge (`option_rules.read`) |
| Browser talks to the member's own bridge on `127.0.0.1:9224` and POSTs the result to the server; the server only grades | `templates/_options_tab.html:24-128`, `templates/ivscan.html:163-213`, `routes/options.py:103-148` | `POST /options/live/{symbol}`, the scanner import |
| A nightly job on Hermes writes the DB directly through `SessionLocal`, leaves a dated log, is registered by a `setup_*_task.ps1` | `deploy/portfolio_daily_check.py:1-80`, `deploy/spread_scan.py:1-60`, `deploy/setup_portfolio_check_task.ps1` | the Telegram push (runs inside the nightly job), the job-run record the status strip reads |
| ONE lightweight-charts instance in the DOM at a time; overlays arrive as `chart_*` context variables of `_price_chart.html` | `templates/_curated_list.html:25-28`, `templates/_sector_chart.html:52-54`, `templates/_portfolio_chart.html:29`, `templates/_price_chart.html:415-428` | the card chart (`chart_bounce`, `chart_trendline`, `chart_spread`, `chart_levels`) and the Ideas/Positions tab switch |
| The invisible-until-hover scrollbar is wildcard CSS in `base.html:15-22`; nothing may override it | `templates/base.html:15-22`, `templates/_options_analysis.html:350` | every scroll area on `/options` |
| Scrolling `tabular-nums`, 11px secondary text, emerald = action/up, rose = down, amber = warning, sky = watch | `templates/base.html:69-260` (the TradingView token layer) | all new markup |

### D0. Names this part assumes from parts A–C

| Name (owner) | What this part calls it for | Signature this part expects |
|---|---|---|
| `OptionChainSnapshot` model (A) | the full stored chain a pick is computed from; the full-chain expander | columns per OPTIONS_MODULE_DESIGN §4.3: `symbol, as_of, expiry, strike, right, bid, ask, mid, iv, delta, gamma, theta, vega, oi, volume, spot, source` |
| `snapshot_store.write(db, chain: dict, *, source: str, as_of: datetime, partial: bool=False) -> int` (A) | `POST /options/refresh/{symbol}` (Cboe) and `POST /options/live/{symbol}` (bridge rows, `partial=True`: one expiry) | `chain` in the `option_quotes.fetch_chain` shape (`services/option_quotes.py:103-116`): `{"symbol","spot","iv30","as_of","legs": {(expiry,right,strike): {...}}}`; the live path converts bridge rows to that shape first (D1.11) |
| `snapshot_store.latest(db, symbol) -> dict | None` (A) | the card, the picks, the ticket | same dict shape as `fetch_chain`, plus `"as_of"`, `"source"`, `"partial"` |
| `iv_daily.upsert_series(db, symbol, series: list[dict], *, source: str) -> int` (A) | the one-time IBKR bootstrap on "Live" (decision 4) | `series = [{"on": "YYYY-MM-DD", "iv": 33.7}, ...]` percent, like `IVHistory.iv30` (`models.py:948`) |
| `iv_daily.latest(db, symbol) -> dict | None` (A) | basket IV column, headline, gauge | `{"on","iv30","hv20","hv60","iv_rank","iv_pct","skew","term_slope","n"}` |
| `ChainSource` + `TST_OPTIONS_SOURCE` (A) | not called directly; `/refresh` goes through `snapshot_store.refresh(db, symbol)` which picks the source | `snapshot_store.refresh(db, symbol, *, force: bool=True) -> dict` |
| `recommender.rank(signal_inputs, rules) -> list[dict]` (B) | not called directly; read from `option_signal.strategies` | each `{"key","label","fit": "recommended"|"also"|"rejected","why","needs","reason"}` |
| `strike_picker.pick_for(db, symbol, strategy: str, rules: dict, signal: dict) -> dict` (B) | `/options/picks`, `/options/payoff`, `/options/ticket`, `/options/track`, re-pick on rule change | `{"picks": [pick...], "nearest_miss": pick|None, "rules_used": {...}, "as_of": ..., "n_considered": int}`; a `pick` = `{"legs":[{"expiry","right","strike","side": "sell"|"buy","qty","price","bid","ask","delta","iv","oi","volume"}], "credit"|"debit", "width", "max_profit","max_loss","breakevens":[...],"pop","return_on_risk","score","why":[...],"ba_tier","chart_ok": bool}` |
| `payoff.build(legs, spot, *, iv_by_leg, dte_front, r, contracts, stop_chart, stop_rule_fraction, target, levels) -> dict` (B) | `GET /options/payoff/{symbol}` | returns the JSON in D1.8 |
| `trendline.find(bars, *, direction) -> dict | None` (B) | read from `option_signal.setup.trendline`, drawn as `chart_trendline` | `{"a":{"t","p"},"b":{"t","p"},"touches":[{"time","price"}],"slope_per_day","value_today","value_at":{date: price},"broken": bool}` |
| `option_signal` row (C) | the card reads NOTHING else at page time | `symbol, as_of, trend (JSON), setup (JSON), iv (JSON), strategies (JSON list), picks (JSON: {strategy: {prefs_hash: pick_result}}), earnings (JSON {date, inside_expiry}), stale (bool)` |
| `signals.compute(db, symbol, *, rules_for: list[dict] | None=None) -> OptionSignal` (C) | after `/refresh` and `/live` | recomputes trend/setup/iv/strategies; picks are filled lazily per prefs hash by `strike_picker.pick_for` |
| `OptionJobRun` + `job_runs.record(...)` (C writes, D reads) | the status strip, the "job missed" notice, `/options/badge` | defined in D6; **D defines the table, C's nightly job writes it** |
| Nightly job entry point `deploy/options_nightly.py` (C) | the Telegram push step is a function this part supplies and C calls at the end of its run | `telegram_push.run(db, *, as_of: str, dry_run: bool=False) -> dict` (D4) |

---

### D1. Routes

#### D1.1 Decision: a new module `app/routes/options_page.py`, registered BEFORE the legacy `options.py` router

Why not extend `routes/options.py`:

1. **Path collision.** `routes/options.py` already owns `prefix="/options"` with a catch-all `GET /{symbol}` (`routes/options.py:85-100`) and a form-based `POST /track` (`:156-184`). Starlette matches routes in registration order, so `GET /options/basket` and `GET /options/rules` would be swallowed by `/{symbol}` unless the fixed paths are registered first. Keeping the page in its own router and including it **before** the legacy one makes the ordering explicit and testable (D8 step 3).
2. **Different guard.** The legacy module is included with no menu gate (`main.py:229-231`, "require_user only, same as the drawings API") because the Watchlist pane's Options tab calls it (`templates/_chart_pane.html:69-73`). The page must be gated by `menus.require_menu("options")`. One router cannot carry two guards.
3. **Different lifetime.** Decision 5 keeps the old routes reachable until a later release. Leaving `options.py` byte-for-byte unchanged (except nothing) is the cheapest way to guarantee the Watchlist tab keeps working while the page replaces the three pages.

`main.py` changes (all cited):

| Line | Change |
|---|---|
| `main.py:40` | add `from .routes import options_page as options_page_routes` |
| `main.py:64-70` | add `options_page_routes` to the tuple that sets `templates.env.globals["version"] / ["nav_for"] / ["gloss"]` — without it `base.html` raises `nav_for is undefined` on the first render |
| `main.py:229-231` | insert **above** `app.include_router(options_routes.router)`: `app.include_router(options_page_routes.router, dependencies=[Depends(menus.require_menu("options"))])` with a comment "fixed paths under /options must precede the legacy /options/{symbol} catch-all" |
| `main.py:241-248` | the three old page routers stay but their guards become `require_menu("positions", "options")`, `require_menu("ivscan", "options")`, `require_menu("spreads", "options")` — `user_can` is an any-of test (`menus.py:93-94`), so a member granted only the new key can still reach the fragments the new page reuses (`/portfolio/list`, `/portfolio/chart`, `/portfolio/{id}/close`, …) |

`menus.py` changes:

| Line | Change |
|---|---|
| `menus.py:21-33` | append `("options", "Options", None, "/options")` after `("curated", ...)` — a flat top-level item, no dropdown (the dropdown is what v4.126 removed) |
| `menus.py:47` | `OFF_NAV_KEYS` unchanged: `ivscan`, `spreads`, `positions` stay granted-but-hidden (decision 5) |
| `menus.py:76-77` | `LABELS` already derives from `MENUS`; nothing to add |
| `menus.py:67` | `LANDING` unchanged |

`base.html` changes for the nav badge (the exit-line badge is currently rendered only when `/portfolio` is in `MENUS`, `base.html:371-372, 387-398, 788-829`):

| Line | Change |
|---|---|
| `base.html:371`, `:387` | the condition `href == '/portfolio'` becomes `href == '/options'`; the element keeps `id="pfNavBadge"` and the painter at `:797-829` is unchanged |
| `base.html:371`, `:397` | `hx-get="/portfolio/badge"` becomes `hx-get="/options/badge"` (D1.17), whose JSON is a superset of `/portfolio/badge` (`routes/portfolio.py:425-455`): `{urgent, watch, checked_on, stale, job_missed, ideas_new}` |
| `base.html:816-827` | one more chip: `if (d.job_missed) chip('⚠', 'bg-amber-500/25 text-amber-300', 'Last night\'s options data job did not run')` and `if (d.ideas_new) chip(d.ideas_new, 'bg-emerald-500/20 text-emerald-300', d.ideas_new + ' new idea(s) in your basket')` |

#### D1.2 Module skeleton

```python
"""Options — one page: basket (left), ticker card (centre), My rules (bottom).

Read path = DB only (option_signal, option_chain_snapshot, iv_daily): nothing on
this page waits on a market call. On-demand Refresh (Cboe, ~1-2 s) and Live (the
member's own IBKR bridge, posted from the browser) are explicit buttons.

Replaces IV Rank (/ivscan), Spread (/spreads) and Positions (/portfolio); those
routers stay registered (menus.OFF_NAV_KEYS) until a later release. This router is
included BEFORE routes/options.py in main.py so its fixed paths win over that
module's /{symbol} catch-all.

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
from ..models import (OptionBasket, OptionIdeaPush, OptionJobRun, OptionPosition,
                      OptionPositionLeg, OptionSignal, OptionSpread, User, _utcnow)
from ..security import require_user
from ..services import option_rules, option_words, strike_picker, payoff, snapshot_store
from ..services import iv_daily, signals, job_runs, user_watchlist as uwl
from ..services import spread_monitor, trade_prefs as tp
from . import options as legacy_options          # _positions() for the legacy form fallback
from . import portfolio as portfolio_routes      # _list_context() for the Positions tab

router = APIRouter(prefix="/options", tags=["options-page"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

BRIDGE_PORT = legacy_options.BRIDGE_PORT          # 9224, one constant
BRIDGE_SETUP_PATH = legacy_options.BRIDGE_SETUP_PATH
MAX_BASKET = 60            # tickers per member; the nightly job budgets ~2 s per ticker
REFRESH_COOLDOWN_S = 60    # per (member, ticker); Cboe 429s on bursts (option_quotes.py:124-126)
FRESH_HOURS = 20           # same idea as ivscan.IV_FRESH_HOURS: younger than this = not stale
```

`MAX_BASKET = 60` is justified by the job budget: Cboe at the nightly scan's 1.5 s pause
(`deploy/spread_scan.py:323`, `PAUSE = 1.5`) plus signal computation ≈ 2–3 s per ticker; 60
tickers ≈ 3 minutes per member, well inside the 30-minute task limit the portfolio check uses
(`setup_portfolio_check_task.ps1`, `-ExecutionTimeLimit 30`).

#### D1.3 Endpoint table

| Method · path | Returns | Purpose | Reads | Writes |
|---|---|---|---|---|
| `GET /options` | `options.html` | page shell; `?symbol=` pre-selects a card, `?tab=positions` opens the Positions tab, `?focus=<spread_id>` (from the badge) | basket count only | — |
| `GET /options/basket` | `_options_basket.html` | the left column; `?sort=idea|iv|trend|added` | `option_basket`, `option_signal`, `iv_daily.latest` | — |
| `POST /options/basket/add` | `_options_basket.html` | add one typed ticker (form `symbol`, `note`) | | `option_basket` |
| `POST /options/basket/remove` | `_options_basket.html` | remove one (form `symbol`) | | `option_basket` |
| `POST /options/basket/import` | `_options_basket.html` | bulk add from `source=paste|watchlist|ivscan_list|scanner|screener` (D1.5) | `prefs.ivscan_universe`, `user_watchlist`, `spread_candidates` | `option_basket` |
| `GET /options/card/{symbol}` | `_options_card.html` | the centre card; `?strategy=&pick=` | `option_signal`, `snapshot_store.latest`, `iv_daily.latest`, `option_rules.read` | `option_signal.picks[strategy][hash]` (cache fill only) |
| `GET /options/picks/{symbol}` | `_options_picks.html` | strategy chip click and rule change re-render only the picks + payoff container; `?strategy=` | same as card, no chart | same cache fill |
| `GET /options/chart/{symbol}` | `_options_chart.html` → `_price_chart.html` | the one chart; `?strategy=&pick=` decides which overlay set | `option_signal.setup`, pick | — |
| `GET /options/payoff/{symbol}` | JSON (D1.8) | the risk & reward chart's data; `?strategy=&pick=&units=usd|r&contracts=` | pick, `snapshot_store.latest` (IV per leg), `option_signal.setup` (stop/target/levels), rules | — |
| `GET /options/rules` | `_options_rules.html` | My rules drawer; `?family=shared|credit|debit|condor|time` | `user_option_prefs`, `trade_prefs.read` | — |
| `POST /options/rules` | `_options_rules.html` + `HX-Trigger` | save one family's fields (D3) | | `user_option_prefs` (and `trade_prefs.write` for the four shared exit lines) |
| `POST /options/rules/reset` | `_options_rules.html` + `HX-Trigger` | `family=` clears that family's overrides; `family=all` clears the row | | `user_option_prefs` |
| `POST /options/refresh/{symbol}` | `_options_card.html` + `HX-Trigger: options:basket-changed` | on-demand Cboe read, re-signal | | snapshot, signal |
| `POST /options/live/{symbol}` | `_options_card.html` + `HX-Trigger` | receives the bridge's `/chain` (+ `/iv?series=1`) from the browser; stores live rows + the IV bootstrap; re-picks | | snapshot (`partial`), `iv_daily`, `iv_history`, signal |
| `POST /options/track` | `_options_positions_tab.html` (new) or, for the legacy form, `_options_positions.html` | create the tracked position from (`symbol, strategy, pick, contracts, note`) | pick | `option_spreads` (credit verticals) or `option_positions` + `_legs` (everything else) |
| `GET /options/ticket/{symbol}` | `_options_ticket.html` | order ticket text; `?strategy=&pick=&contracts=` | pick, rules, setup | — |
| `GET /options/positions` | `_portfolio_list.html` (unchanged template) | the Positions tab; `?status=open|closed&focus=` | `portfolio._list_context` | `SpreadCheck` (same as today) |
| `GET /options/status` | `_options_status.html` | the honesty strip, polled every 300 s | `OptionJobRun`, basket as-of | — |
| `GET /options/badge` | JSON | nav badge (replaces `/portfolio/badge`) | `SpreadCheck`, `OptionJobRun`, `OptionIdeaPush` | — |
| `GET /options/chain/{symbol}` | `_options_chain.html` | the full stored chain behind the expander; `?expiry=` | snapshot | — |

Query parameters are validated the same way everywhere: `symbol` through `_clean_symbol()`
(the single-ticker form of `ivscan._clean_symbols`, `routes/ivscan.py:91-102`); `strategy` must
be in `option_rules.STRATEGY_KEYS` else the recommended one; `pick` is an int index clamped to
the pick list; `family` must be in `option_rules.FAMILIES`.

#### D1.4 Shared context builders

```python
def _basket_rows(db: Session, user: User) -> list[OptionBasket]:
    return (db.query(OptionBasket).filter(OptionBasket.user_id == user.id)
              .order_by(OptionBasket.pos, OptionBasket.symbol).all())

def _basket_context(db: Session, user: User, *, sort: str = "idea", selected: str = "") -> dict:
    """Every basket row with the three numbers the column shows (trend arrow, IV rank,
    the recommended idea) read from option_signal / iv_daily - never a market call.
    Sorted best idea first by default: a card with a recommendation and picks sorts
    above one with a recommendation and no strikes, above 'no setup', above 'no read'."""
    rows = _basket_rows(db, user)
    sigs = {s.symbol: s for s in db.query(OptionSignal)
                              .filter(OptionSignal.symbol.in_([r.symbol for r in rows])).all()} if rows else {}
    ivs = {r.symbol: iv_daily.latest(db, r.symbol) for r in rows}
    rules = option_rules.read(db, user)
    items = []
    for r in rows:
        s = sigs.get(r.symbol)
        rec = next((x for x in (s.strategies or []) if x["fit"] == "recommended"), None) if s else None
        has_picks = bool(rec and strike_picker.cached(s, rec["key"], rules["hash"]))
        items.append({"row": r, "signal": s, "iv": ivs.get(r.symbol), "rec": rec,
                      "has_picks": has_picks,
                      "age_h": _age_hours(s.as_of) if s else None,
                      "stale": s is None or _age_hours(s.as_of) > FRESH_HOURS,
                      "idea_word": option_words.idea_short(rec) if rec else None})
    key = {"idea": lambda it: (it["rec"] is None, not it["has_picks"], -(it["iv"]["iv_rank"] or 0) if it["iv"] else 0),
           "iv":   lambda it: (it["iv"] is None, -(it["iv"]["iv_rank"] or 0) if it["iv"] else 0),
           "trend": lambda it: (it["signal"] is None, (it["signal"].trend or {}).get("order", 9) if it["signal"] else 9),
           "added": lambda it: it["row"].pos}[sort if sort in ("idea","iv","trend","added") else "idea"]
    items.sort(key=key)
    screener = _screener_suggestions(db, user, exclude={r.symbol for r in rows})   # D5.2
    return {"user": user, "items": items, "sort": sort, "selected": selected,
            "n": len(rows), "max_basket": MAX_BASKET, "screener": screener,
            "n_watchlist": len(uwl.symbol_set(db, user)),
            "n_ivscan": len(_ivscan_universe(user))}
```

`_age_hours` is the same helper as `routes/ivscan.py:158-167` (naive-UTC both sides; the
column is naive on SQLite and aware on Postgres) — move it to `services/option_rules.py` so the
two pages share one copy rather than cloning it.

```python
def _card_context(db: Session, user: User, symbol: str, *, strategy: str = "",
                  pick: int = 0, note: str = "") -> dict:
    """Everything the card renders. The signal row is the ONLY thing the headline,
    chips and overlays read; the picks come from the signal's cache for this
    member's rules hash, or are computed now from the stored chain (ms, no network).
    `note` carries a one-line status from the POST that re-rendered the card
    ('Refreshed from Cboe 16:04 ET', 'Bridge not reachable - showing delayed data')."""
    sym = _clean_symbol(symbol)
    sig = db.query(OptionSignal).filter(OptionSignal.symbol == sym).one_or_none()
    rules = option_rules.read(db, user)
    snap = snapshot_store.latest(db, sym)
    iv = iv_daily.latest(db, sym)
    in_basket = db.query(OptionBasket).filter(OptionBasket.user_id == user.id,
                                              OptionBasket.symbol == sym).first() is not None
    strategies = list(sig.strategies or []) if sig else []
    rec = next((x for x in strategies if x["fit"] == "recommended"), None)
    strategy = strategy if strategy in option_rules.STRATEGY_KEYS else (rec["key"] if rec else "")
    chosen = next((x for x in strategies if x["key"] == strategy), None)
    picks = strike_picker.pick_for(db, sym, strategy, rules, sig) if (sig and strategy and snap) else None
    pick_i = max(0, min(pick, len(picks["picks"]) - 1)) if picks and picks["picks"] else 0
    chips = option_words.chip_row(strategies)          # D2.3: recommended, also-fits, <=2 greys, rest
    return {"user": user, "sym": sym, "sig": sig, "rules": rules, "snap": snap, "iv": iv,
            "in_basket": in_basket, "strategy": strategy, "chosen": chosen, "rec": rec,
            "chips": chips, "picks": picks, "pick_i": pick_i,
            "headline": option_words.headline(sig, chosen) if sig else None,
            "gauge": option_words.gauge(iv) if iv else None,
            "earnings": (sig.earnings or {}) if sig else {},
            "age": option_words.age_badge(sig.as_of if sig else None, snap.get("source") if snap else None,
                                          snap.get("partial") if snap else False),
            "bridge_port": BRIDGE_PORT, "bridge_setup_path": BRIDGE_SETUP_PATH,
            "note": note, "prefs": tp.read(user)}
```

`_picks_context` is `_card_context` minus `headline/gauge/chips/age` (the chip click must not
recompute the sentence); `_rules_context(db, user, family)` is in D3.

#### D1.5 Basket endpoints

```python
class BasketImport(BaseModel):
    source: str = "paste"            # paste | watchlist | ivscan_list | scanner | screener
    text: str = ""                   # paste: commas / spaces / newlines (ivscan.UniverseIn shape)
    symbols: list[str] = Field(default_factory=list)   # scanner: what the browser got from the bridge /scan
    note: str = ""

@router.post("/basket/import", response_class=HTMLResponse)
def basket_import(payload: BasketImport, request: Request,
                  user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Bulk add. Sources:
      paste        - typed text, cleaned exactly as /ivscan/universe cleans it (re, _clean_symbols)
      watchlist    - user_watchlist.symbols(db, user)                       (services/user_watchlist.py:28)
      ivscan_list  - prefs['ivscan_universe'] (the old IV Rank 'My list')    (routes/ivscan.py:85,105-107)
      scanner      - payload.symbols, posted by the browser after it ran the bridge /scan
                     (the SAME browser flow as ivscan.html:268-297; the server never reaches TWS)
      screener     - payload.symbols chosen from the nightly spread_candidates list (D5.2)
    Existing rows are kept (their signal is not reset); new ones get pos = max+1 and
    source = payload.source. Returns the basket fragment; the response carries
    HX-Trigger 'options:basket-changed' so the status strip updates its count."""
```

Rules: cap at `MAX_BASKET` (the response says how many were dropped, as `/ivscan/universe`
reports `dropped`, `routes/ivscan.py:307`); duplicates ignored; `source` stored verbatim from
the allow-list. `add` is `import(source="typed")` with one symbol. `remove` deletes the row and
nothing else — the ticker's `option_signal` / snapshot are shared across members and are left
for the nightly job's retention to expire (90 days of EOD, §4.3). A ticker that is in nobody's
basket is simply not refreshed next night (C's universe = union of baskets).

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
def picks(symbol: str, request: Request, strategy: str = "", pick: int = 0, reason: str = "",
          user: User = Depends(require_user), db: Session = Depends(get_db)):
    """The strike table + payoff container for ONE strategy. Served separately from
    the card so a chip click (or a rule change) never remounts the chart. `reason`
    is set by a greyed chip ('expensive') so the banner can say why this strategy
    was not recommended while still showing its strikes."""

@router.get("/chart/{symbol}", response_class=HTMLResponse)
def chart(symbol: str, request: Request, strategy: str = "", pick: int = 0,
          user: User = Depends(require_user), db: Session = Depends(get_db)):
    """The one chart. Context built by sector._chart_ctx (routes/sector.py:368-388 - MATP
    band, curated call) PLUS the option overlays: chart_bounce from the signal's setup
    (same dict _bounce_overlay builds, routes/sector.py:352-365), chart_trendline from
    setup.trendline, chart_spread from the chosen pick (credit/condor/time) or
    chart_levels {entry, stop, target} for the debit family (read-only; chart_setup_seed
    deliberately NOT set so the levels are not editable here - the setup detector owns them)."""
```

The chart fragment `_options_chart.html` sets, in this order (mirroring
`_sector_chart.html:6-61` and `_portfolio_chart.html:13-32`):

```jinja
{% set chart_symbol = symbol %} ... {% set chart_fill = true %}
{% set chart_price_min = 'lg:min-h-[300px]' %}       {# same floor as the curated chart #}
{% set chart_band = sel_band %}{% set chart_band_start_closed = true %}
{% set chart_tv_plot = true %}{% set chart_matp_run = true %}{% set chart_curate_setup = false %}
{% set chart_bounce = bounce %}                       {# or none #}
{% set chart_trendline = trendline %}                 {# NEW, D2.4 #}
{% if family in ('credit','condor','time') %}{% set chart_spread = spread %}{% endif %}
{% if family == 'debit' %}{% set chart_levels = levels %}{% endif %}
{% set chart_refresh = {'url': '/options/chart?...', 'target': '#optChartBody'} %}
{% include "_price_chart.html" %}
```

`chart_curate_setup = false`: the card is where the system has already curated; a "Curate
setup" button here would put a system idea into the member's own Curated list under their name,
which §3 keeps separate ("Track this" is the hand-over, not Curate).

#### D1.7 Payoff JSON

`GET /options/payoff/{symbol}?strategy=bull_put&pick=0&units=usd&contracts=1` →

```json
{
  "symbol": "LRCX", "strategy": "bull_put", "family": "credit", "label": "Bull put spread",
  "as_of": "2026-10-02T20:00:00Z", "source": "cboe", "stale": false,
  "spot": 349.20, "contracts": 1, "units": "usd", "r_value": 220.0,
  "legs": [{"expiry": "2026-11-20", "right": "P", "strike": 330, "side": "sell", "qty": 1, "price": 3.40, "iv": 0.41, "delta": -0.26},
           {"expiry": "2026-11-20", "right": "P", "strike": 320, "side": "buy",  "qty": 1, "price": 1.30, "iv": 0.44, "delta": -0.17}],
  "x": [300.0, 301.0, "... 121 points from spot-3*expected_move to spot+3*expected_move ..."],
  "expiry": [-790, -790, "...", 210, 210],
  "today":  [-612, -604, "...", 131, 152],
  "today_caption": "what the trade is worth if the stock moved there tomorrow (modelled, Black-Scholes at today's IV)",
  "front_expiry_dte": 49, "today_is_front_expiry": false,
  "breakevens": [327.90], "max_profit": 210.0, "max_loss": 790.0,
  "pop": 0.74, "pop_word": "chance of keeping it", "pop_text": "74% chance of keeping it",
  "markers": {
    "spot":        {"price": 349.20, "label": "now 349.20"},
    "stop_chart":  {"price": 338.0, "pl_today": -220.0, "label": "chart stop 338 · you'd lose ≈ $220"},
    "stop_rule":   {"price": 334.1, "pl_today": -158.0, "label": "rule stop ≈ 334 · 20% of max loss (−$158)"},
    "target":      null,
    "support":     {"price": 340.0, "label": "support 340"},
    "resistance":  null,
    "trendline":   {"today": 341.6, "at_expiry": 346.9, "label": "trend line (3 touches)"},
    "short_strikes": [330.0], "long_strikes": [320.0]
  },
  "palette": {"expiry": "#1D9E75", "today": "#7F77DD", "profit": "rgba(29,158,117,0.18)", "loss": "rgba(239,83,80,0.18)"}
}
```

Formulas (all in part B's `payoff.build`; stated here because the JSON exposes them):

- `x` grid: 121 points over `[spot − 3·EM, spot + 3·EM]`, `EM = spot · iv30 · sqrt(dte_front/365)` (§4.1 "expected move IV × √DTE"); clamped to `[0.5·spot, 1.5·spot]`.
- `expiry[i] = 100 · contracts · Σ_legs sign · qty · (intrinsic(x_i) − price)` with `sign = −1` for sell, `+1` for buy; `intrinsic = max(0, x−K)` for C, `max(0, K−x)` for P. For calendars/diagonals the curve is drawn at the **front** leg's expiry and the back leg is valued with `black_scholes()` (`services/black_scholes.py:43`) at its own IV with `T = (back_dte − front_dte)/365` — §6e.
- `today[i]`: every leg valued with `black_scholes(S=x_i, K, T=dte/365 − 1/365, r=0.04, sigma=leg.iv)`; `r = 0.04` is the same constant `bull_put.bs_put` uses (`services/bull_put.py:79`).
- `pop`: credit families `1 − |Δ_short|` (and for a condor `1 − |Δ_short_put| − |Δ_short_call|`); debit families `P(S_T beyond breakeven)` from the lognormal with `sigma = iv30`, which is what "chance of profit" means for a buyer. Both are `pop_est`-style approximations and the caption says "about".
- `stop_rule` for a credit family: the first `x` **below** spot (above, for a bear call) where `today[i] ≤ −rules.credit.loss_stop_pct/100 · max_loss` (the member's own line, default 20 — `trade_prefs.DEFAULT_LOSS_STOP_PCT`, `services/trade_prefs.py:51`). For the debit family: where `today[i] ≤ −rules.debit.loss_stop_pct/100 · debit_paid` (default 50). For condor/time: same as credit on each wing.
- `stop_chart` = `signal.setup.stop` (1 ATR under the bounce low / the trend line for a long; mirrored for a short) — not a preference; `pl_today` is `today` interpolated at that price.
- `r_value` = `|pl_today(stop_chart)|` so the `units=r` view divides every curve by it ("+0.95 R / −1 R").

#### D1.8 Rules endpoints — see D3.

#### D1.9 Refresh

```python
@router.post("/refresh/{symbol}", response_class=HTMLResponse)
def refresh(symbol: str, request: Request, strategy: str = "",
            user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Read today's delayed chain now (Cboe, ~1-2 s), store it, recompute the signal,
    re-render the card. Rate-limited per (member, ticker) to REFRESH_COOLDOWN_S:
    Cboe answers 429 to bursts (option_quotes.py:124-126) and a member double-clicking
    must not take the feed away from everyone else's page."""
    sym = _clean_symbol(symbol)
    if _cooldown_hit(user.id, sym):
        return Response(status_code=429, headers={"HX-Trigger": json.dumps(
            {"options:toast": {"kind": "err", "msg": "Just refreshed — try again in a minute."}})})
    try:
        snapshot_store.refresh(db, sym, force=True)        # fetch_chain(sym, ttl=0) inside
        signals.compute(db, sym)
        note = "Refreshed from Cboe · " + option_words.et_clock()
    except snapshot_store.SourceError as exc:              # wraps option_quotes.ChainError
        note = f"Could not refresh: {exc}. Showing the stored data."
    ctx = _card_context(db, user, sym, strategy=strategy, note=note)
    resp = templates.TemplateResponse(request, "_options_card.html", ctx)
    resp.headers["HX-Trigger"] = json.dumps({"options:basket-changed": {}})
    return resp
```

`_cooldown_hit` is an in-process dict `{(user_id, sym): monotonic}`; good enough for one
uvicorn worker (the app runs single-worker on Hermes, `deploy/run_app.ps1`).

#### D1.10 Live (the bridge)

The browser does exactly what `_options_tab.html:93-128` does — `Promise.all([get('/chain?…'), get('/iv?symbol=…&series=1'), get('/account')])` — and POSTs the three results here. The bridge's `/chain` returns one expiry, ±10 strikes or the put side (`bridge/ibkr_bridge.py:419-540`); its `/iv` today returns only the summary (`:541-566`). **Bridge change required (bridge 1.4):** `/iv?symbol=X&series=1` adds `"series": [{"on": b.date.isoformat(), "iv": round(b.close*100, 1)}, ...]` from the same `reqHistoricalDataAsync(... OPTION_IMPLIED_VOLATILITY ...)` call it already makes — the bars are already in `vals`; only the dates are dropped today.

```python
class LiveIn(BaseModel):
    chain: dict = Field(default_factory=dict)   # bridge /chain response, untrusted
    iv: dict = Field(default_factory=dict)      # bridge /iv response (+ series when the bridge is >= 1.4)
    nlv: float | None = None                    # /account net_liquidation
    diag: dict | None = None                    # what the browser saw when the loopback fetch failed

@router.post("/live/{symbol}", response_class=HTMLResponse)
def live(symbol: str, payload: LiveIn, request: Request, strategy: str = "",
         user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Store what the member's own TWS said and re-pick on it.
    1. chain.ok and chain.spot: convert bridge rows (keys bid/ask/last/delta/iv/theta/vega/oi/volume,
       _row() at bridge/ibkr_bridge.py:321) to the fetch_chain leg shape and
       snapshot_store.write(db, converted, source='ibkr', as_of=now, partial=True) - ONLY the
       (expiry, right, strike) keys the bridge sent are overwritten; every other expiry stays Cboe.
    2. iv.series present (bridge 1.4+): iv_daily.upsert_series(db, sym, series, source='ibkr')
       AND spread_scan.record_iv(db, sym, on, iv30, spot) for each day (services/spread_scan.py:203)
       so the OLD percentile source (iv_history) agrees with the new one. Bounded: <= 400 points,
       each 0 < iv < 1000, dates ISO. This is the decision-4 bootstrap; done once per ticker per
       30 days (OptionJobRun-independent marker: iv_daily rows with source='ibkr' newer than 30 d).
    3. iv.iv_rank present: stored on the signal as iv.live = {rank, pct, current, n, at} so the
       card can show 'IV rank 62 (TWS, live)' next to the server-side 'IV rank 59 (delayed)'.
    4. payload.nlv: tp.write(db, user, nlv=nlv) only if the member's stored nlv is 0 ('not told
       yet', trade_prefs.DEFAULT_NLV) - never overwrite a number they typed.
    5. signals.compute(db, sym); re-render the card with note='Live from your TWS HH:MM'.
    chain.ok false: the card re-renders unchanged with note = the calm bridge panel text
    (D2.9) and diag shown in a <details>, exactly as _options_analysis.html:6-62 does."""
```

Every number is re-validated (`_b(v, lo, hi)` as in `routes/ivscan.py:351-356`) because the body
comes from a browser.

#### D1.11 Track

```python
@router.post("/track", response_class=HTMLResponse)
async def track(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Record the chosen pick as a tracked position. Two bodies, one path:

    NEW (this page): form fields symbol, strategy, pick (index), contracts, note.
      The legs are NOT trusted from the browser: the pick is re-read from the signal's
      cache for this member's rules hash (strike_picker.pick_for) so what is tracked is
      what was shown. If the cache no longer holds it (rules changed in another tab)
      -> 409 + toast 'The strikes changed - look at the card again before tracking.'

    LEGACY (the Watchlist pane's Options tab, _options_analysis.html:129-149): the form has
      short_strike/long_strike/... . Detected by the presence of 'short_strike'. Handled by
      the same code routes/options.py:156-184 ran - an OptionSpread(strategy='bull_put') row -
      and answered with legacy_options._positions(request, user, db, {}) so that tab keeps
      working while routes/options.py stays untouched.

    Where the row goes (decision for this part):
      bull_put / bear_call -> option_spreads (OptionSpread, models.py:788-849), strategy column
        set accordingly, legs mapped short/long, entry greeks from the pick. This keeps the
        daily sweep (spread_monitor.sweep), SpreadCheck history and /options/badge working
        unchanged in step 1. spread_monitor.snapshot must learn right='C' for bear_call
        (cross-part need, part C).
      every other strategy -> option_positions + option_position_legs (D1.12), monitored by
        the generic valuer in step 2+. In step 1 the page can still RECORD them (so a member who
        tracks a buy call is not refused) but the Positions tab shows them under 'Not monitored
        yet - step 2' with their geometry only.
    Both paths store: source='options_page', idea_key (D4 dedupe key), chart_stop, chart_target,
    the rules hash and the signal's as_of, so a review can say what the system saw."""
```

Response: the Positions tab fragment with `focus=<new id>` and `HX-Trigger: {"options:tracked": {"id": ..}, "options:toast": {...}}`; the page JS switches to the Positions tab on `options:tracked`.

#### D1.12 Storage this part owns (SQLAlchemy sketches, portable types only)

```python
class OptionBasket(Base):
    """One ticker in one member's options basket (§2.1). Separate from user_watchlist:
    different cadence (nightly chain + signal) and different data cost."""
    __tablename__ = "option_basket"
    __table_args__ = (UniqueConstraint("user_id", "symbol", name="uq_option_basket_user_symbol"),)
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    symbol = Column(String(20), nullable=False, index=True)
    pos = Column(Integer, nullable=False, default=0)
    added_on = Column(DateTime, default=_utcnow)
    note = Column(Text, nullable=True)
    source = Column(String(16), nullable=False, default="typed")   # typed|paste|watchlist|ivscan_list|scanner|screener|system

class UserOptionPrefs(Base):
    """One row per member: the My-rules overrides (§4.3). Only fields the member changed are
    stored; house defaults are merged on read (option_rules.read), like ema_setup.clean_enabled."""
    __tablename__ = "user_option_prefs"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    prefs = Column(JSON, nullable=False, default=dict)     # {"shared": {...}, "credit": {...}, "debit": {...}, "condor": {...}, "time": {...}}
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

class OptionPosition(Base):
    """A tracked multi-leg option position (every strategy that is not a put/call vertical
    credit spread; those stay in option_spreads so the existing monitor keeps grading them).
    Tracking only - nothing here is an order."""
    __tablename__ = "option_positions"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    symbol = Column(String(20), nullable=False, index=True)
    strategy = Column(String(24), nullable=False)          # option_rules.STRATEGY_KEYS
    family = Column(String(12), nullable=False)            # credit|debit|condor|time
    contracts = Column(Integer, nullable=False, default=1)
    net_price = Column(Float, nullable=True)               # per share; + = credit received, - = debit paid
    opened_at = Column(DateTime, default=_utcnow)
    status = Column(String(12), nullable=False, default="open")    # open|closed
    closed_at = Column(DateTime, nullable=True)
    note = Column(Text, nullable=True)
    source = Column(String(16), nullable=False, default="options_page")   # options_page|manual|paper
    idea_key = Column(String(120), nullable=True, index=True)             # D4 key the idea was pushed under
    signal_as_of = Column(DateTime, nullable=True)
    rules_hash = Column(String(16), nullable=True)
    chart_stop = Column(Float, nullable=True)              # the setup's stop (underlying price)
    chart_target = Column(Float, nullable=True)
    # per-trade exit overrides, NULL = the family's rule (same convention as OptionSpread)
    loss_stop_pct = Column(Float, nullable=True)
    profit_target_pct = Column(Float, nullable=True)
    dte_floor = Column(Integer, nullable=True)
    user = relationship("User")
    legs = relationship("OptionPositionLeg", back_populates="position", cascade="all, delete-orphan",
                        order_by="OptionPositionLeg.id")

class OptionPositionLeg(Base):
    __tablename__ = "option_position_legs"
    id = Column(Integer, primary_key=True)
    position_id = Column(Integer, ForeignKey("option_positions.id", ondelete="CASCADE"), nullable=False, index=True)
    expiry = Column(String(10), nullable=False)            # YYYY-MM-DD
    right = Column(String(1), nullable=False)              # C|P
    strike = Column(Float, nullable=False)
    side = Column(Integer, nullable=False)                 # +1 buy, -1 sell
    qty = Column(Integer, nullable=False, default=1)       # per contract of the position (a condor = 1 each)
    price = Column(Float, nullable=True)                   # per share at entry
    entry_delta = Column(Float, nullable=True)             # signed
    entry_iv = Column(Float, nullable=True)
    position = relationship("OptionPosition", back_populates="legs")

class OptionIdeaPush(Base):
    """One Telegram push per (member, idea). The key is what makes 'once per new idea'
    mean something (D4)."""
    __tablename__ = "option_idea_push"
    __table_args__ = (UniqueConstraint("user_id", "idea_key", name="uq_option_idea_push"),)
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    symbol = Column(String(20), nullable=False, index=True)
    idea_key = Column(String(120), nullable=False)
    sent_at = Column(DateTime, default=_utcnow)
    ok = Column(Boolean, nullable=False, default=True)
    error = Column(Text, nullable=True)

class OptionJobRun(Base):
    """One run of the nightly options job (C writes, the status strip reads). The same
    role SpreadScan plays for the Spread page (models.py:954-968)."""
    __tablename__ = "option_job_runs"
    id = Column(Integer, primary_key=True)
    job = Column(String(32), nullable=False, default="options_nightly", index=True)
    as_of = Column(String(10), nullable=False, index=True)     # ET trading date the data describes
    started_at = Column(DateTime, default=_utcnow)
    finished_at = Column(DateTime, nullable=True)
    tickers = Column(Integer, nullable=False, default=0)
    done = Column(Integer, nullable=False, default=0)
    failed = Column(Integer, nullable=False, default=0)
    pushed = Column(Integer, nullable=False, default=0)        # Telegram messages sent
    note = Column(Text, nullable=True)                         # first few failures, e.g. "KO: Cboe HTTP 403"
```

Alembic: one migration for the whole module, `alembic/versions/f9a0b1c2d3e4_options_module.py`,
`revision = "f9a0b1c2d3e4"`, `down_revision = "e2f3a4b5c6d7"` (verified head: no file in
`alembic/versions/` revises `e2f3a4b5c6d7`; `f9a0b1c2d3e4` is not among the 35 ids in use). Same
idempotent guard as `e2f3a4b5c6d7_iv_scan_items.py:21-22` (`if table in inspect(bind).get_table_names(): return`)
per table, because `init_db()` may run against the legacy `create_all` DB on Hermes. The tables
parts A and C own (`option_chain_snapshot`, `iv_daily`, `option_signal`) go in the **same file**;
if the parts are built in separate commits, this part's tables chain after theirs and this id moves.

#### D1.13 Ticket

```python
@router.get("/ticket/{symbol}", response_class=HTMLResponse)
def ticket(symbol: str, request: Request, strategy: str = "", pick: int = 0, contracts: int = 1,
           user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Plain-text order ticket for the chosen pick (D2.8). Server-rendered so the numbers
    are the pick's, not whatever the DOM held."""
```

#### D1.14 Positions tab

```python
@router.get("/positions", response_class=HTMLResponse)
def positions(request: Request, status: str = "open", focus: int = 0,
              user: User = Depends(require_user), db: Session = Depends(get_db)):
    """The Positions page, inside /options. Renders the UNCHANGED _portfolio_list.html from
    portfolio._list_context (routes/portfolio.py:88-146) - the board, the chart pane, the
    per-row drawers and the add form all keep posting to /portfolio/* (still registered,
    guard widened to ('positions','options')). Plus the generic option_positions rows
    (step 2) appended by _generic_positions_context (D5.1)."""
    ctx = portfolio_routes._list_context(db, user, status=status, focus=focus or None)
    ctx["generic"] = _generic_positions_context(db, user, status)
    ctx["inside_options"] = True       # hides the fragment's 'Positions ->' links to /portfolio
    return templates.TemplateResponse(request, "_portfolio_list.html", ctx)
```

#### D1.15 Status, badge, chain

```python
@router.get("/status", response_class=HTMLResponse)
def status_strip(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """The honesty line (§7): data age, delayed/live, job health. Polled every 300 s. D6."""

@router.get("/badge")
def badge(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Nav badge JSON: /portfolio/badge's dict (stored checks only, never a quote - routes/
    portfolio.py:425-455) + job_missed (D6) + ideas_new (OptionIdeaPush rows for this member
    with sent_at >= last job as_of, i.e. what the nightly job pushed that they have not
    opened: cleared when GET /options renders)."""

@router.get("/chain/{symbol}", response_class=HTMLResponse)
def chain(symbol: str, request: Request, expiry: str = "",
          user: User = Depends(require_user), db: Session = Depends(get_db)):
    """The full stored chain for the expander (§7: 'nobody sees a raw chain by default').
    Table markup lifted from _options_analysis.html:349-411 (calls | strike | puts), with
    the chosen pick's legs highlighted as that template highlights _cs/_cl (:305-306)."""
```

---

### D2. Templates

All new templates live in `app/templates/`. Context variables are listed per template; HTMX
attributes are written as they appear in the markup. The light-theme ink additions and the
scrollbar rule are in D2.10.

#### D2.1 `options.html` — the page shell

Extends `base.html`; `{% block main_class %}flex-1 w-full px-4 py-4 overflow-hidden{% endblock %}`
(as `ivscan.html:3`). Context: `user, n_basket, symbol (pre-selected or ''), tab ('ideas'|'positions'), focus, max_basket`.

```
┌ header strip  #optStatus (hx-get /options/status, load, every 300s) ───────────────────────────┐
│ Options · 5 tickers · Data as of Oct 2, 16:00 ET · delayed · job ✓ 06:31 MYT  [Refresh] [Live (TWS)] │
├──────────┬──────────────────────────────────────────────────────────────────────────────────────┤
│ #optBasket│ tabs: [Ideas] [Positions ●2]                        #optPane                        │
│ 200px     │ #optCard  (Ideas)  OR  #optPositions (Positions) — only one is in the DOM          │
│ lg:w-[200px]│                                                                                   │
├──────────┴──────────────────────────────────────────────────────────────────────────────────────┤
│ <details id="optRules">  MY RULES  tabs: Shared · Credit spreads · Buy call/put · Iron condor · Time spreads  [Reset] │
└─────────────────────────────────────────────────────────────────────────────────────────────────┘
```

Markup skeleton:

```jinja
<div class="lg:flex lg:flex-col lg:h-[calc(100vh-6rem)]">
  <div id="optStatus" class="shrink-0 mb-2" hx-get="/options/status" hx-trigger="load, every 300s" hx-swap="innerHTML"></div>
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
         hx-get="/options/rules?family=shared" hx-trigger="toggle[this.parentElement.open] once" hx-swap="innerHTML"></div>
  </details>
</div>
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
{% include "_at_modal.html" %}
```

Page script (one IIFE, ~120 lines), responsibilities:

| Concern | Behaviour | Precedent |
|---|---|---|
| Tab switch Ideas / Positions | swaps `#optPane` with `htmx.ajax('GET', '/options/card/'+sym)` or `'/options/positions'`; remembered in `localStorage['optPaneTab']`; **only the visible pane is loaded** (a chart drawn in a hidden box measures 0 wide) | `ivscan.html:300-336`, `_chart_pane.html:44-83` |
| Basket row click | `.opt-row[data-sym]` → `htmx.ajax('GET','/options/card/'+sym,{target:'#optPane'})`, `history.replaceState` to `/options?symbol=SYM`, highlights the row (`.opt-on`) | `ivscan.html:344-353` |
| Bridge calls | `window.thOptionsLive(sym)` = the `Promise.all` from `_options_tab.html:106-117` posting to `/options/live/<sym>`; `window.thStartBridge` reused verbatim (`_options_tab.html:183-209`); `window.thScanImport()` runs the bridge `/scan` with the stored pair floors and posts `source:'scanner'` to `/options/basket/import` (`ivscan.html:268-297`) | |
| Chart strike swap on pick click | `window.thChartSetStrikes(spec)` (D2.4) called with the row's `data-strikes` JSON | new hook in `_price_chart.html` |
| Payoff repaint | `window.thPayoffLoad(sym, strategy, pick, units)` fetches `/options/payoff/...` and paints (D2.6) | |
| Toasts | `options:toast` HX-Trigger → `window.thToast(msg, kind)` | `base.html:639-656` |
| Chart expand | the `pfChartExpand` handler from `portfolio.html` (bottom of that file) ported as-is, because the Positions tab renders `_portfolio_list.html` which has that button | `portfolio.html`, `spreads.html:44-67` |
| Failure reporting | `htmx:responseError / sendError / timeout` on `#optPane` paint the "could not load … HTTP n … /admin/log" box | `portfolio.html:21-47` |

Mobile (`< lg`): the layout is a single column — status strip, then the basket rendered as a **horizontal chip strip** (`_options_basket.html` switches on a `compact` flag the shell sets via `hx-vals='{"compact":1}'` when `window.innerWidth < 1024`; the strip is `flex overflow-x-auto gap-1 pb-1`), then the card full-width (chart `h-[300px]`, chip row wraps, the strike table becomes three stacked rows with the "collect / risk / chance" line on top), then the My rules `<details>` as a bottom sheet. No fixed heights on `< lg`; the page scrolls as a whole (`overflow-hidden` is on `lg:` only, as `ivscan.html:14` does with `lg:h-[calc(100vh-6rem)]`).

#### D2.2 `_options_basket.html`

Context: `items [{row, signal, iv, rec, has_picks, age_h, stale, idea_word}], sort, selected, n, max_basket, screener, n_watchlist, n_ivscan, compact`.

Column grid (`display:grid; grid-template-columns: 1fr 1.1rem 2rem 4.2rem`): ticker · trend arrow · IV rank · idea.

| Cell | Rendering | Rule |
|---|---|---|
| ticker | `text-[13px] font-semibold text-slate-200`; a 6px dot before it: emerald `< 20 h`, amber `20–72 h`, rose `> 72 h` or no signal; `title` = "data as of …" | the per-ticker data-age badge (D6) |
| trend | `↗` (`text-emerald-300`) uptrend, `↘` (`text-rose-300`) downtrend, `↔` (`text-slate-400`) sideways, `·` no read | `signal.trend.kind` |
| IV | the rank as an integer; **amber** (`text-amber-300`) when `iv_rank ≥ 50` = sell premium, **grey** (`text-slate-400`) 30–50, **teal** (`text-teal-300`) `< 30` = buy; `–` unknown; `title` = `option_words.iv_rank_words(iv)` | §7 "IV number colour" |
| idea | `idea_word`: `sell put` / `sell call` / `buy call` / `buy put` / `call sprd` / `put sprd` / `condor` / `calendar` / `diagonal` / `LEAPS`; faded (`opacity-60`) when `not has_picks` with `title` "recommended, but no strike passes your rules today"; `none` in `text-slate-600` when no setup; `no read` when no signal | |

Row attributes: `class="opt-row cursor-pointer …{% if row.symbol == selected %} opt-on{% endif %}" data-sym="{{ row.symbol }}" hx-get="/options/card/{{ row.symbol }}" hx-target="#optPane" hx-swap="innerHTML" hx-push-url="/options?symbol={{ row.symbol }}" title="{{ signal.headline_short }}"`. A `×` on hover: `hx-post="/options/basket/remove" hx-vals='{"symbol":"…"}' hx-target="closest .opt-basket" hx-swap="innerHTML" hx-confirm="Remove {{ sym }} from your basket? Its tracked positions are kept."`.

Header line: `{{ n }}/{{ max_basket }} tickers` · sort buttons `Idea · IV · Trend · Added` (`hx-get="/options/basket?sort=…"`, same pattern as `_ivscan_list.html:43-46`).

Footer: `[+ Add ticker]` opens an inline `<form hx-post="/options/basket/add" hx-target="closest .opt-basket" hx-swap="innerHTML">` with one `uppercase` input; `[Import ▾]` is a `<details>` with four buttons: **Paste tickers** (textarea → `/options/basket/import` `source=paste`), **My Watchlist ({{ n_watchlist }})**, **My IV Rank list ({{ n_ivscan }})** (shown only when `n_ivscan > 0`, labelled "from the old IV Rank page"), **Run my TWS scanner** (`onclick="window.thScanImport()"`, with the three pair-floor inputs defaulting to `ivscan.DEFAULT_CRITERIA` — IV rank 30, price 50, volume 200 000, the only absolute numbers on the page besides the liquidity rules).

"Suggested by last night's screener" section (D5.2): up to 8 rows from `spread_candidates` not in the basket, each `SYM · credit 28% · IV pct 71 [+]`; the `[+]` posts `source=screener`.

Empty state (no rows): the D2.9 text.

#### D2.3 `_options_card.html`

Context: everything `_card_context` returns (D1.4). Wrapped in `<div class="opt-card" data-sym="{{ sym }}" data-strategy="{{ strategy }}">`.

```
┌ LRCX · 349.20 · ATR 11.5 ───────── earnings Oct 22 · inside expiry ─── [as of Oct 2 16:00 ET · delayed] [Refresh] [Live] [★ in basket] ┐
│ #optHeadline  "Uptrend: EMA 20 above 50 above 200 for 34 days, and price is riding a trend line with 3 touches. It bounced   │
│  off support at 340 on high volume (1.6× normal). Options are expensive (IV rank 62), so you're paid to sell a put spread     │
│  below that support."                                                                                                        │
│ #optGauge   SELL ● NEUTRAL ○ BUY ○   IV rank 62 · IV 41% vs realised 29%                                                     │
│ #optChips   [✓ Bull put spread] [Bull call spread · also fits] [Buy call · expensive] [Iron condor · trending, not sideways]  │
│             other strategies ▾                                                                                               │
│ #optChartBody  (hx-get /options/chart/LRCX?strategy=bull_put&pick=0, load)                                                   │
│ #optPicks      (hx-get /options/picks/LRCX?strategy=bull_put, load; also options:rules-changed from:body)                    │
│   ├ strike table (3 rows) · nearest miss                                                                                     │
│   ├ #optPayoff  (canvas + legend + [$|R] toggle + the one-line caption)                                                      │
│   └ #optActions [Order ticket] [Track this]            Show full chain ▾                                                     │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

Header line, left to right: `sym` · last close (`sig.trend.close`) · `ATR {{ atr }}` (`sig.trend.atr14`) · earnings chip (`text-amber-300` when `earnings.inside_expiry`, `text-slate-500` otherwise, `title` = D2.7 wording) · the age badge (D6) · `[Refresh]` `hx-post="/options/refresh/{{ sym }}?strategy={{ strategy }}" hx-target="closest .opt-card" hx-swap="outerHTML" hx-indicator="#optRefreshSpin"` · `[Live (TWS)]` `onclick="window.thOptionsLive('{{ sym }}')"` · `[+ Add to basket]` when `not in_basket` (`hx-post="/options/basket/add"`).

`note` (from a POST) renders as a one-line strip under the header: emerald for "Refreshed …", amber for "Could not …", with the bridge diagnostics `<details>` when `diag` is present (`_options_analysis.html:22-44`, copied).

Chip row rules (decision 9, written as code so the wording cannot drift):

```python
# services/option_words.py
def chip_row(strategies: list[dict]) -> dict:
    """{"first": rec|None, "also": [...], "greys": [<=2 rejected], "rest": [...]}.
    Order: the recommended one first (filled chip, check mark); every 'also' fit next
    (outlined); then at most TWO rejected ones, greyed, each WITH its reason; everything
    else behind 'other strategies'. Rejected ones are ordered by how close they came
    (recommender score), so the greys are the near misses, not the absurd ones."""
```

Chip markup: recommended `border-emerald-500/60 bg-emerald-500/15 text-emerald-300 font-semibold` with `✓`; also-fits `border-slate-600 text-slate-300` + `· also fits`; rejected `border-slate-800 text-slate-500 opacity-70` + `· {{ reason_short }}` where `reason_short` is one of the fixed strings in D2.7 (e.g. `expensive`, `cheap options`, `trending, not sideways`, `no range`, `no setup`, `earnings inside`, `front IV under back`). Every chip is `hx-get="/options/picks/{{ sym }}?strategy={{ key }}&reason={{ reason_short|urlencode }}" hx-target="#optPicks" hx-swap="innerHTML"` and sets `data-strategy` so the page script updates `.opt-card[data-strategy]` and the hidden `strategy` inputs. The "other strategies" `<details>` lists the rest as plain text links with the same `hx-get`.

A greyed chip, when clicked, shows the picks with a banner (amber): "Not recommended today: {{ reason_long }}. Showing the strikes anyway so you can see what it would cost."

#### D2.4 `_options_chart.html` and the two chart-include changes

Context: `symbol, sel, sel_band, sel_patterns, curated, bounce, trendline, spread, levels, family, strategy, pick, user`. Sets the `chart_*` variables as in D1.6.

Two additions to `_price_chart.html`, both small and both in the style of what is there:

1. **`chart_trendline`** (read-only auto trend line, §6d). Declared next to `BOUNCE` (`_price_chart.html:415`):
   `var TLINE = {{ (chart_trendline if (chart_trendline is defined and chart_trendline) else None)|tojson }};`
   shape `{a:{t,p}, b:{t,p}, touches:[{time,price}], value_today, value_at:{date:price}, label, broken}`. Painted by the drawing overlay (`setupDrawing`, `:678`) as a **read-only shape list** `RO_SHAPES = TLINE ? [{type:'tline', a:{t:TLINE.a.t,o:0,p:TLINE.a.p}, b:{t:TLINE.b.t,o:0,p:TLINE.b.p}, ro:true}] : []`, iterated by the same `paint()` branch that draws a `tline` (`:928-945`) before the member's `drawings`, in cyan `#22d3ee` dotted, extended to the right edge (so its value at expiry is visible), never hit-tested (`:1899` ignores `ro`), never saved (not in `drawings`, so the debounced PUT to `/drawings/<sym>` never sees it — the §6d rule "never into the member's own drawings"). Touches become `belowBar` circle markers `TL 1/3 …` merged into `candleMarks` next to the BOUNCE markers (`:2562-2596`); a `broken` line is drawn rose and labelled "trend line · broken".
2. **`chart_spread` generalised** (`:2666-2690` hard-codes three put lines and the title `'Short ' + K + 'P'`). New shape `{legs:[{strike, right, side, label}], breakevens:[...], expiry, label}` (the old `{short,long,breakeven}` still accepted: mapped to two P legs). Loop: `side=='sell'` → rose solid width 2, `side=='buy'` → slate dashed width 1, breakevens amber dotted; the title is the leg label (`Short 330P`, `Long 320P`, `Short 370C` …). The handles are kept in `spreadHandles` and a new `window.thChartSetStrikes = function (spec) { … removePriceLine each; redraw }` is exposed — the exact pattern `paintCurated` uses with `curatedHandles` (`:2702-2712`). The autoscale hint (`spreadLevels`, `:2683-2689`) is refreshed in the same call. This is what lets a pick click move the strike lines without remounting the chart.

Debit family: `chart_levels = {entry: setup.entry, stop: setup.stop, target: setup.target, label: ''}` → the existing read-only Entry/SL/PT lines (`:2652-2657`); `chart_setup_seed` is not set, so the drawing layer stays in normal mode and the lines are not draggable.

#### D2.5 `_options_picks.html`

Context: `sym, strategy, chosen, family, picks {picks, nearest_miss, rules_used, n_considered}, pick_i, rules, reason, earnings, prefs, snap, sig`. Wrapped in `<div class="opt-picks" hx-get="/options/picks/{{ sym }}?strategy={{ strategy }}" hx-trigger="options:rules-changed from:body" hx-swap="outerHTML">` — so a rule save re-renders exactly this box.

Heading: `STRIKES under your rules` followed by `option_words.rules_line(family, rules)`:
"delta 0.20–0.30 · 30–60 days · min OI 500 · bid/ask ≤ $0.50 · under support + trend line" — one line, no jargon beyond the words the rules drawer itself uses.

Row (one per pick, max 3; the recommended one first with `★`):

| Column | Wording | Source |
|---|---|---|
| legs | `Nov 20 · 330/320 put` (`expiry_label · short/long right`); condor `Nov 20 · 320/310 put + 380/390 call`; calendar `Nov 20 → Dec 18 · 350 call`; single `Dec 18 · 340 call` | `pick.legs` |
| you collect / you pay | credit families **collect $210**; debit **pay $640**; per contract, `× contracts` when > 1 | `pick.credit*100` / `pick.debit*100` |
| you risk | **risk $790** (credit: width − credit; debit: the debit; calendar: the net debit) | `pick.max_loss` |
| chance | credit **74% chance of keeping it**; debit **46% chance of profit**; the number is `round(pop*100)`; `title` = the delta sentence (D2.7) | decision 9 |
| return | `27% on risk` (credit ÷ max loss) or `2.1× reward/cost` (debit verticals) | `pick.return_on_risk` |
| tag | `← best return` on row 1, `safer` on the row with the highest pop, `more credit` on the highest credit, by `option_words.pick_tags` | §7 mock |
| liquidity | one word: `clean` (`ba_tier=='clean'`), `at the limit` (amber), `thin` (rose, OI below rule) with `title` = OI and width per leg | `bull_put.rank_pairs` tiers (`services/bull_put.py:51-52, 64-66`) |

Row attributes: `class="opt-pick … {% if loop.index0 == pick_i %}ring-1 ring-emerald-500/60{% endif %}" data-pick="{{ loop.index0 }}" data-strikes='{{ pick.chart_spec|tojson }}'`; click = page script: highlight, `thChartSetStrikes(JSON.parse(data-strikes))`, `thPayoffLoad(sym, strategy, i, units)`, and update the hidden `pick` inputs in `#optActions`. **No server round trip** for a pick click.

Nearest miss (when `picks.picks` is empty, or always under the table in `text-[11px] text-slate-500`): "Closest that didn't pass: Nov 20 335/325 at delta 0.33 — fails *short delta ≤ 0.30*. Loosen that rule in My rules → Credit spreads to include it." (`picks.nearest_miss.why[0]` is the one failing rule, named with the drawer's own label.)

Earnings line (when `earnings.inside_expiry`): amber "Earnings {{ date }} falls inside this expiry. Your rule: {{ 'not allowed' | 'defined-risk only — this trade qualifies' | 'allowed' }}." When the rule is "not allowed" the picks are hidden and the D2.9 earnings empty-state shows instead.

Then `{% include "_options_payoff.html" %}` and `#optActions`:

```jinja
<div id="optActions" class="flex flex-wrap items-center gap-2 mt-2">
  <button type="button" class="… bg-emerald-600 text-white"            {# the ONE primary action on the card #}
          hx-get="/options/ticket/{{ sym }}" hx-vals='js:{strategy: thCardStrategy(), pick: thCardPick(), contracts: thCardQty()}'
          hx-target="#optTicket" hx-swap="innerHTML">Order ticket</button>
  <form hx-post="/options/track" hx-target="#optPane" hx-swap="innerHTML" class="flex items-center gap-1">
    <input type="hidden" name="symbol" value="{{ sym }}"><input type="hidden" name="strategy" value="{{ strategy }}">
    <input type="hidden" name="pick" value="{{ pick_i }}">
    <label class="text-[11px] text-slate-500">contracts <input type="number" name="contracts" min="1" max="500" value="{{ qty }}" class="w-14 …"></label>
    <button type="submit" class="… border border-slate-700 text-slate-300" title="Track this idea in Positions so it is checked every day. Does not place an order.">Track this</button>
  </form>
  <details class="ml-auto text-[11px]"><summary class="cursor-pointer text-slate-500">Show full chain ▾</summary>
    <div hx-get="/options/chain/{{ sym }}?expiry={{ picks.picks[pick_i].legs[0].expiry if picks and picks.picks else '' }}" hx-trigger="toggle once" hx-swap="innerHTML" class="overflow-auto max-h-[50vh]"></div>
  </details>
</div>
<div id="optTicket"></div>
```

`qty` = `max(1, floor(risk_budget / max_loss))` with `risk_budget = nlv · risk_pct/100` from `tp.read(user)` (`services/trade_prefs.py:71-91`, the "1% of account" rule); blank → 1 and a hint "set your account value in My rules → Shared to size this" (NLV 0 = "not told yet", `trade_prefs.DEFAULT_NLV`).

#### D2.6 `_options_payoff.html`

Context: `sym, strategy, pick_i, family, units ('usd' default)`. Markup: a header row (`RISK & REWARD` · `[$ | R]` toggle · the caption), `<canvas id="optPayoffCanvas" class="w-full h-[220px] lg:h-[260px]">`, a legend line, and `<p id="optPayoffNote" class="text-[11px] text-slate-500">`.

Inline script (`window.thPayoffLoad`): fetch the JSON (D1.7), size the canvas to `devicePixelRatio`, and paint:

| Element | Paint | Colour |
|---|---|---|
| profit zone | fill between the expiry curve and 0 where `expiry > 0` | `palette.profit` (teal-50) |
| loss zone | same where `< 0` | `palette.loss` (coral-50) |
| expiry line | 2px solid | `#1D9E75` |
| today line | 1.5px dashed | `#7F77DD` |
| zero line | 1px, text token at 35% | `--tv-muted` |
| spot | vertical dotted + label `now 349.20` | `--tv-text` |
| **chart stop** | vertical solid rose + label `chart stop 338 · −$220` (decision 7) | `--tv-down` |
| **rule stop** | vertical dashed amber + label `rule stop ≈ 334 · 20% of max loss` (decision 7) | `#f59e0b` |
| target (debit) | vertical dashed teal `target 372 · +$640` | `#1D9E75` |
| support / resistance / trend line at expiry | thin cyan ticks on the x-axis with labels | `#22d3ee` |
| short strikes | small rose triangles on the x-axis; long strikes slate | |
| breakevens | `BE 327.90` on the x-axis | `#fbbf24` |
| max profit / max loss | labelled at the plateau ends: `max +$210`, `max −$790` | |
| POP | top-right text `74% chance of keeping it` | |

Caption (decision 8), always visible under the chart, verbatim: **"Dashed line: what the trade is worth if the stock moved there tomorrow. Solid line: at expiry ({{ dte }} days)."** For calendars/diagonals: **"Drawn at the near expiry ({{ front }}); the far option is valued by the model, so the solid line is an estimate too."**

Units toggle: `$` (default) or `R`; in `R` every y value is `/ r_value` and labels read `+0.95 R`; the choice is remembered in `localStorage['optPayoffUnits']` (per-viewer convenience only).

Failure: fetch error → the canvas is replaced by the one-line "Could not draw the payoff (no stored quotes for these strikes). Press Refresh." in amber; never a blank box.

#### D2.7 Plain-language strings (`services/option_words.py`) — written out

Every number a member sees that is a greek, a rate or a rule is accompanied by one of these sentences (as `title`, as the chance column, or inline). All are functions so the numbers are filled in; the templates never compose them.

**Greeks and rates**

| Function | Text (credit side) | Text (debit side) |
|---|---|---|
| `delta_words(d, side)` | "Delta 0.25 — about a 1-in-4 chance this strike finishes in the money; put another way, roughly a 75% chance of keeping the credit." | "Delta 0.65 — the option moves about 65 cents for every $1 the stock moves; one contract behaves like about 65 shares." |
| `theta_words(t_per_day_usd, side)` | "Theta +$6/day — time is paying you about $6 a day while the stock sits still." | "Theta −$9/day — waiting costs about $9 a day; the stock has to move enough to pay for that." |
| `vega_words(v_usd)` | "Vega $18 — if implied volatility rises one point this position loses about $18 (you are short volatility)." | "Vega $22 — if implied volatility rises one point this position gains about $22." |
| `gamma_words(g)` | "Gamma 0.02 — the delta changes by about 0.02 for each $1 move; small means the trade's risk changes slowly." | same |
| `iv_words(iv, spot, dte)` | "Implied volatility 41% — the market's guess at how much the stock will move in a year; about ±{{ spot·iv·sqrt(dte/365) }} ({{ pct }}%) over this trade." | same |
| `iv_rank_words(iv)` | "IV rank 62 — today's IV sits 62% of the way from the year's lowest to its highest. Above 50 options are expensive (sellers are paid); below 30 they are cheap (buyers get a deal)." | |
| `iv_pct_words(iv)` | "IV percentile 71 — IV was lower than today on 71% of the past year's trading days." | |
| `hv_words(iv)` | "Realised volatility 29% — how much the stock actually moved over the last 20 days, annualised." | |
| `iv_hv_words(iv)` | "IV 41% vs realised 29% — options are priced for more movement than the stock has been delivering; sellers are paid for that gap." / "… less movement … buyers are getting it cheap." | |
| `term_words(iv)` | "Near-term options are dearer than later ones (front IV 45% vs back 38%) — the market expects an event before the first expiry." / "Later options are dearer than near ones — nothing special is priced in soon; calendars are not paid here." | |
| `oi_words(oi, rule)` | "Open interest 2,300 — contracts outstanding at this strike. You need enough to get out again; your rule is at least 500." | |
| `width_words(w, rule)` | "Bid/ask $0.20 wide — the cost of getting in and out. Your rule allows up to $0.50; wider than that eats the edge." | |
| `pop_words(pop, family)` | "74% chance of keeping it (1 minus the short strike's delta)." | "46% chance of profit (the odds the stock is past the breakeven at expiry, at today's volatility)." |
| `max_loss_words(x, family)` | "The most you can lose: $790 per contract (the width minus the credit), if the stock is below both strikes at expiry." | "The most you can lose: $640 per contract — what you paid." |
| `breakeven_words(be, spot)` | "Breakeven 327.90 — the stock can fall 6.1% and this still makes money at expiry." | "Breakeven 356.40 — the stock must rise 2.1% by expiry just to get your money back." |
| `dte_words(dte, family)` | "49 days to expiry — inside your 30–60 day window; long enough for time to work for you, short enough to manage." | "70 days — inside your 45–90 day window; enough time for the move, before decay bites." |
| `expected_move_words(em, spot, dte)` | "Expected move ±$18 (5.2%) by expiry — one standard deviation at today's IV." | |
| `earnings_words(e)` | "Earnings Oct 22 falls INSIDE this expiry — the one thing a stop cannot protect you from." / "Earnings Oct 22 is after this expiry." / "No earnings date on file." | |

**Trend / setup / gauge (used by `headline`)**

| Piece | Text |
|---|---|
| uptrend | "Uptrend: EMA 20 above 50 above 200 for {{ n }} days" |
| downtrend | "Downtrend: EMA 20 below 50 below 200 for {{ n }} days" |
| sideways | "Sideways: the EMAs are flat and price has held between {{ lo }} and {{ hi }} ({{ n_lo }} touches below, {{ n_hi }} above)" |
| mixed | "No clear trend: the EMAs are tangled" |
| support bounce | "It bounced off support at {{ level }} on high volume ({{ vol }}× normal)" |
| trend-line touch | "price is riding a trend line with {{ touches }} touches" / "it bounced at the trend line ({{ touches }} touches)" |
| EMA rebound | "it rebounded off the {{ ema }}-day average" |
| breakout retest | "it broke out above {{ level }} and is retesting it" |
| failed support | "support at {{ level }} gave way and price is back under it" |
| no setup | "No fresh setup today" |
| gauge SELL | "Options are expensive (IV rank {{ r }})" |
| gauge NEUTRAL | "Options are fairly priced (IV rank {{ r }})" |
| gauge BUY | "Options are cheap (IV rank {{ r }})" |
| conclusion per strategy | bull_put "so you're paid to sell a put spread below that support" · bear_call "so you're paid to sell a call spread above that resistance" · buy_call "so a call is cheap enough to buy here, with the stop just under the setup" · buy_put "so a put is cheap enough to buy on the breakdown" · bull_call "so a call spread capped at the target costs less than a plain call" · bear_put "so a put spread capped at the target costs less than a plain put" · leaps_call "so a long-dated deep call can stand in for the stock" · iron_condor "so you're paid to sell both sides of the range" · calendar "so selling the near month against a later one collects the difference" · diagonal_call "so a long-dated call can fund selling monthly calls under the resistance" |
| nothing fits | "so there is nothing to do today — check again tomorrow" |

`headline(sig, chosen)` = `"{trend}, and {setup_clause}. {setup_sentence}. {gauge}, {conclusion}."` with the clauses dropped when absent (so "Uptrend … for 34 days. No fresh setup today. Options are expensive (IV rank 62), so there is nothing to do today — check again tomorrow.").

**Rejection reasons** (`reason_short` → `reason_long`), fixed list from the recommender:

| short | long |
|---|---|
| expensive | "options are expensive (IV rank {{ r }} ≥ 30): paying for a long option here fights the premium" |
| cheap options | "options are cheap (IV rank {{ r }} < 30): selling premium is not paid enough" |
| not rich enough | "IV rank {{ r }} is under 50: a condor needs richer premium" |
| trending, not sideways | "the chart is trending; a range strategy wants flat EMAs and a range with both edges touched" |
| no range | "no range with both edges touched at least twice" |
| wrong direction | "the chart is in a {{ trend }}; this strategy needs the opposite" |
| no setup | "no fresh setup to anchor the entry or the stop" |
| earnings inside | "earnings {{ date }} fall inside every expiry in the window and your rule says no" |
| front IV under back | "near-term IV is below later IV; a calendar is not paid here" |
| no long-dated | "no expiry 9–18 months out is stored for this ticker" |
| no weekly trend | "the weekly EMAs are not stacked; LEAPS want the long-term trend" |

**Idea short words** (basket column): `sell put`, `sell call`, `buy call`, `buy put`, `call sprd`, `put sprd`, `LEAPS`, `condor`, `calendar`, `diagonal`.

**Glossary additions** (`services/glossary.py`, one more `_add({...})` group so `T.tip('Delta')` works in table headers): Delta, Theta, Vega, Gamma, Implied volatility, IV rank, IV percentile, Realised volatility, Open interest, Bid/ask, Breakeven, Max loss, Max profit, Chance of keeping it, Chance of profit, Expected move, Days to expiry, Credit, Debit, Width — each the first sentence of the matching row above, without the numbers.

#### D2.8 `_options_ticket.html`

Context: `sym, strategy, label, pick, contracts, rules, setup, as_of, source, prefs`. Rendered into `#optTicket` as a small panel with a `[Copy]` button (`navigator.clipboard.writeText` of the `<pre>`), `[Close]`. The text, exactly:

```
LRCX — Bull put spread (you sell; defined risk)
  Sell to open   1 × LRCX 20 Nov 2026 330 PUT      (bid 3.30 / ask 3.50)
  Buy to open    1 × LRCX 20 Nov 2026 320 PUT      (bid 1.20 / ask 1.40)
Order: one vertical spread, LIMIT 2.10 credit (mid 2.15 · natural 2.00), day order, while the market is open
Why: uptrend · bounced off support 340 on volume · IV rank 62 · 74% chance of keeping it
Trigger (optional): send only while LRCX is at or above 340 (support)  — TWS: Condition → Price ≥ 340; moomoo: Trigger price 340
Plan:  take profit  buy back at 1.05 (50% of the credit)
       stop         close if the spread costs 3.68 to buy back (20% of max loss) OR LRCX closes under 338 (the setup's stop)
       time         close or roll by 30 Oct (21 days left), whatever the P/L
You collect $210 · you risk $790 · breakeven 327.90 · earnings Oct 22 is inside this expiry (defined risk only)
Data as of 02 Oct 16:00 ET (Cboe, ~15 min delayed) — check the mark in your broker before sending.
Tracking only: TradeHunter never sends an order.
```

Debit variant lines: "Buy to open … / (Sell to open … for a spread)", "LIMIT 6.40 debit (mid 6.35 · natural 6.60)", plan "target: sell at the chart target 372 (2 R) · stop: sell if LRCX closes under 338 (1 ATR) or the option loses 50% · time: review at 21 days left". Condor: four legs, "one iron condor, LIMIT 3.10 credit". Calendar/diagonal: two expiries on separate lines, "one calendar spread, LIMIT 2.40 debit".

Broker mapping notes in a `<details>` under the text: TWS — "Strategy Builder → Vertical → enter both legs → Limit price = credit → Attach a Conditional: Price of LRCX ≥ trigger"; moomoo — "Options → Strategy → Bull Put → both legs → Conditional order → Trigger when last ≥ …". No screenshots, no claims about either broker's order types beyond those two paths (the design discussion recorded that these were worked out; this part only prints them).

#### D2.9 Empty states and failure wording (every case the page can be in)

| State | Where | Text (verbatim) |
|---|---|---|
| no basket | `#optBasket` + `#optPane` | "Your basket is empty. Add the tickers you trade options on — paste them, pull in My Watchlist, or run your TWS scanner. Tonight's job reads each one's option chain; you'll see a card tomorrow morning. In a hurry? Press **Refresh** on a ticker to read today's delayed chain now (about 2 seconds)." |
| basket, nothing selected | `#optPane` | "Click a ticker on the left. Best ideas sort to the top: ↗ ↘ ↔ is the trend, the number is IV rank (amber = sell premium, teal = buy), the last column is tonight's suggestion." |
| no signal yet (added today) | card | "No read yet for {{ sym }} — it was added after last night's job. Press **Refresh** to read today's delayed chain now, or wait for tonight." (chips, picks, payoff hidden; chart still shows) |
| signal, no setup | card | headline ends "…so there is nothing to do today — check again tomorrow." All chips greyed with reasons; picks area: "No strategy fits today. The chart is for checking that read — nothing to pick." |
| recommended, no strikes pass | picks | "No strike passes your rules for {{ label }} today ({{ rules_line }}). Closest: {{ nearest_miss }} — fails *{{ rule_label }}*. Loosen that rule in My rules → {{ family_tab }} to include it." |
| earnings blocks | picks | "Earnings on {{ date }} fall inside every expiry in your {{ dte_lo }}–{{ dte_hi }} day window, and your rule says no earnings inside the trade. Either wait, or allow defined-risk trades through earnings in My rules → Shared." |
| data stale (≥ 20 h, < 3 trading days) | age badge amber | "as of {{ when }} ET — the nightly job hasn't run since. Refresh reads today's delayed chain." |
| data very stale (≥ 3 trading days) or job missed twice | banner over the card, rose | "This card is {{ n }} trading days old. The nightly job on Hermes has not run — an administrator can check `/admin/log`. Refresh still works for one ticker at a time." |
| Cboe down on Refresh | note strip, amber | "Could not refresh: {{ error }}. Showing the stored data from {{ when }}." (`ChainError` text, e.g. "LRCX: Cboe HTTP 429") |
| bridge not running on Live | note strip + `<details>` diag | "Could not reach your IBKR bridge on this PC (127.0.0.1:9224). Start TWS and `bridge\start_ibkr_bridge.bat`, then press Live again. The card still shows the delayed data." + `[Start the bridge]` `[Retry]` (`_options_analysis.html:45-61` reused) |
| bridge up, no greeks | note strip | "Your TWS sent quotes but no greeks (no option model yet). Delayed data kept for the picks; try Live again in a minute." |
| bridge older than 1.4 (no IV series) | note strip, slate | "Live quotes stored. Your bridge is older than 1.4, so the one-year IV history was not copied — restart `start_ibkr_bridge.bat` to get the new file." |
| Live chain is one expiry | age badge | "live (TWS) for {{ expiry }} · other expiries delayed" |
| ticker unknown to Cboe | card | "{{ sym }}: no Cboe option chain — check the ticker. Removed from the nightly job until it is fixed." (`ChainError` 403/404 text, `option_quotes.py:143-150`) |
| positions tab, none | `_portfolio_list.html:355-359` unchanged | (existing text) |
| rules save out of range | drawer | the field's own "{{ label }} must be between {{ min }} and {{ max }}." (`trade_prefs.write` style, `services/trade_prefs.py:106-117`) |
| track refused (cache miss) | toast, rose | "The strikes changed — look at the card again before tracking." |
| refresh cooldown | toast, slate | "Just refreshed — try again in a minute." |

#### D2.10 `base.html` ink additions and the scrollbar rule

Light-theme block (`base.html:275-324`) gets, in the same style:

```css
.text-teal-300, .text-teal-200 { color:#0f766e !important; }        /* the basket's 'buy premium' IV number */
.hover\:text-teal-300:hover { color:#0f766e !important; }
.border-teal-500\/60 { border-color:#0f766e !important; }
.text-violet-200 { color:#6d28d9 !important; }                     /* the payoff 'today' legend swatch text */
```

Dark theme needs nothing: `text-teal-300` and `text-violet-*` are native Tailwind. The payoff
palette is NOT Tailwind — it is fixed hex in the JSON (`#1D9E75`, `#7F77DD`, teal-50/coral-50
fills) because §6e validated it for CVD on both themes; the canvas reads only the text colour
from `getComputedStyle(document.documentElement).getPropertyValue('--tv-text')`.

Scrollbar: every scroll area on the page (`#optBasket overflow-y-auto`, `#optPane overflow-y-auto`,
`#optRulesBody overflow-y-auto max-h-[40vh]`, the picks table wrapper `overflow-x-auto`, the
full-chain expander `overflow-auto max-h-[50vh]`, the mobile basket strip `overflow-x-auto`)
inherits `base.html:15-22`. The reviewer's check (D7 item 10): no `scrollbar-*` property and no
`::-webkit-scrollbar` rule anywhere in the new templates.

---

### D3. The My rules drawer (`_options_rules.html`)

#### D3.1 Model

`option_rules.py` holds the house defaults as constants (decision 3; admin-editable later = move
the dict to a table, the merge code does not change):

```python
FAMILIES = ("shared", "credit", "debit", "condor", "time")
FAMILY_LABELS = {"shared": "Shared", "credit": "Credit spreads", "debit": "Buy call/put",
                 "condor": "Iron condor", "time": "Time spreads"}
STRATEGY_KEYS = ("bull_put", "bear_call", "buy_call", "buy_put", "bull_call", "bear_put",
                 "leaps_call", "iron_condor", "calendar", "diagonal_call")
FAMILY_OF = {"bull_put": "credit", "bear_call": "credit", "buy_call": "debit", "buy_put": "debit",
             "bull_call": "debit", "bear_put": "debit", "leaps_call": "debit",
             "iron_condor": "condor", "calendar": "time", "diagonal_call": "time"}

# (key, label, help, default, min, max, step, unit) - the ONLY place a rule is described.
# Absolute numbers are allowed ONLY for the member's own liquidity rules (OI, bid/ask width);
# everything else is a ratio, a delta, a count of strikes or days, or an ATR multiple.
FIELDS = {
 "shared": [
  ("min_oi",            "Minimum open interest", "Contracts outstanding at a strike. Below this you may not get out. Per leg.", 500, 0, 20000, 50, "contracts"),
  ("max_ba_width",      "Widest bid/ask allowed", "The cost of getting in and out. Wider markets eat the edge. Per leg.", 0.50, 0.05, 5.00, 0.05, "$"),
  ("earnings",          "Earnings inside the trade", "An earnings report before expiry is the one thing a stop cannot protect you from.", "no", ["no", "defined_risk", "yes"], None, None, ""),
  ("sell_iv_rank_min",  "Sell premium only when IV rank is at least", "Below this, selling is not paid enough.", 30, 0, 100, 1, ""),
  ("buy_iv_rank_max",   "Prefer buying when IV rank is at most", "Above this a long option is dear; the recommender greys it ('expensive').", 30, 0, 100, 1, ""),
  # account value + risk per trade are READ from trade_prefs (nlv, risk_pct) and edited in the
  # same drawer row - one setting across Curated, the chart's setup editor and here.
 ],
 "credit": [   # bull_put, bear_call
  ("short_delta_lo", "Short strike delta, from", "≈ chance the strike finishes in the money. 0.20 ≈ 1-in-5.", 0.20, 0.05, 0.50, 0.01, ""),
  ("short_delta_hi", "… to", "", 0.30, 0.05, 0.50, 0.01, ""),
  ("dte_lo",         "Days to expiry, from", "30-60 is the sweet spot: enough decay, still manageable.", 30, 7, 365, 1, "days"),
  ("dte_hi",         "… to", "", 60, 7, 365, 1, "days"),
  ("width_strikes",  "Width, in strikes", "How many listed strikes between the short and the long leg. 1-2 keeps the max loss small.", 2, 1, 5, 1, "strikes"),
  ("credit_pct_min", "Minimum credit, % of width", "The reward per dollar risked. 25-33% is the playbook's floor.", 25, 5, 60, 1, "%"),
  ("under_level",    "Short strike must be under support (over resistance)", "And under the trend line when there is one. The chart-derived rule, switchable.", True, None, None, None, ""),
  ("take_pct",       "Take profit at % of the credit", "Buy it back once this much is captured.", 50, 0, 100, 5, "%"),      # -> trade_prefs spread_profit_target_pct
  ("loss_stop_pct",  "Stop at % of max loss", "Close when the loss reaches this share of the most you could lose.", 20, 0, 100, 5, "%"),  # -> spread_loss_stop_pct
  ("dte_floor",      "Close or roll at days left", "Whatever the P/L.", 21, 0, 365, 1, "days"),                              # -> spread_dte_floor
  ("roll_delta",     "Roll when the short delta reaches", "The playbook's 0.35-0.40; yours may be tighter.", 0.30, 0.05, 1.0, 0.01, ""),  # -> spread_roll_delta
 ],
 "debit": [   # buy_call, buy_put, bull_call, bear_put, leaps_call
  ("long_delta_lo",   "Long strike delta, from", "How much the option moves per $1 of stock. 0.60-0.70 moves like the stock without paying for deep ITM.", 0.60, 0.30, 0.95, 0.01, ""),
  ("long_delta_hi",   "… to", "", 0.70, 0.30, 0.95, 0.01, ""),
  ("theta_pct_max",   "Daily decay, at most % of the premium", "What waiting costs you per day. 1% = a $500 option loses ≈ $5 a day.", 1.0, 0.1, 5.0, 0.1, "%/day"),
  ("dte_lo",          "Days to expiry, from", "45-90 gives the move time without buying a year of decay.", 45, 7, 365, 1, "days"),
  ("dte_hi",          "… to", "", 90, 7, 365, 1, "days"),
  ("spread_short_delta_lo", "Spread: short strike delta, from", "For bull call / bear put spreads: the leg you sell to cheapen the trade.", 0.25, 0.05, 0.50, 0.01, ""),
  ("spread_short_delta_hi", "… to", "", 0.35, 0.05, 0.50, 0.01, ""),
  ("spread_short_at_target", "Spread: short strike at or beyond the chart target", "The cap sits where the setup says the move ends.", True, None, None, None, ""),
  ("reward_cost_min", "Spread: minimum reward ÷ cost", "1.0 = you can make what you pay.", 1.0, 0.2, 5.0, 0.1, "×"),
  ("stop_atr",        "Stop, ATR below the setup", "The Curated convention: 1 ATR under the bounce low / trend line.", 1.0, 0.25, 3.0, 0.25, "× ATR"),
  ("target_r",        "Target, in R", "R = the stop distance. 2R = twice the risk.", 2.0, 0.5, 5.0, 0.5, "R"),
  ("loss_stop_pct",   "Stop on the option at % of the premium", "If the stock stop is not hit but the option bleeds.", 50, 0, 100, 5, "%"),
  ("leaps_delta_lo",  "LEAPS delta, from", "Deep in the money: the option behaves like 70-80 shares.", 0.70, 0.50, 0.95, 0.01, ""),
  ("leaps_delta_hi",  "… to", "", 0.80, 0.50, 0.95, 0.01, ""),
  ("leaps_extrinsic_pct_max", "LEAPS: time value at most % of the option price", "What you pay for time rather than stock.", 10, 1, 50, 1, "%"),
  ("leaps_dte_lo",    "LEAPS days to expiry, from", "9-18 months.", 270, 180, 900, 30, "days"),
  ("leaps_dte_hi",    "… to", "", 540, 180, 900, 30, "days"),
 ],
 "condor": [
  ("short_delta_lo",  "Short strike delta each side, from", "0.15-0.20 ≈ an 80-85% chance each side expires worthless.", 0.15, 0.05, 0.40, 0.01, ""),
  ("short_delta_hi",  "… to", "", 0.20, 0.05, 0.40, 0.01, ""),
  ("wing_strikes",    "Wing width, in strikes", "", 2, 1, 5, 1, "strikes"),
  ("credit_pct_min",  "Minimum credit, % of wing width", "", 30, 5, 60, 1, "%"),
  ("dte_lo",          "Days to expiry, from", "", 30, 7, 120, 1, "days"),
  ("dte_hi",          "… to", "", 45, 7, 120, 1, "days"),
  ("iv_rank_min",     "Only when IV rank is at least", "A condor needs rich premium on both sides.", 50, 0, 100, 1, ""),
  ("outside_range",   "Both short strikes outside the range", "Under the range low and over the range high the detector found.", True, None, None, None, ""),
  ("take_pct", …50…), ("loss_stop_pct", …20…), ("dte_floor", …21…),
 ],
 "time": [   # calendar, diagonal_call
  ("cal_front_dte_lo", "Calendar: near expiry days, from", "", 20, 7, 90, 1, "days"), ("cal_front_dte_hi", "… to", "", 30, 7, 90, 1, "days"),
  ("cal_back_dte_lo",  "Calendar: far expiry days, from", "", 50, 20, 365, 1, "days"), ("cal_back_dte_hi", "… to", "", 70, 20, 365, 1, "days"),
  ("cal_delta_lo",     "Calendar strike delta, from", "At the money: 0.45-0.55.", 0.45, 0.30, 0.70, 0.01, ""), ("cal_delta_hi", "… to", "", 0.55, 0.30, 0.70, 0.01, ""),
  ("cal_front_over_back", "Calendar only when near IV ≥ far IV", "The term structure decides, not just the level.", True, None, None, None, ""),
  ("diag_long_delta_lo", "Diagonal: long call delta, from", "", 0.70, 0.50, 0.95, 0.01, ""), ("diag_long_delta_hi", "… to", "", 0.80, 0.50, 0.95, 0.01, ""),
  ("diag_long_dte_lo",   "Diagonal: long call days, from", "6-12 months.", 180, 90, 730, 30, "days"), ("diag_long_dte_hi", "… to", "", 365, 90, 730, 30, "days"),
  ("diag_short_delta_lo","Diagonal: short call delta, from", "", 0.20, 0.05, 0.50, 0.01, ""), ("diag_short_delta_hi", "… to", "", 0.30, 0.05, 0.50, 0.01, ""),
  ("diag_short_dte_lo",  "Diagonal: short call days, from", "", 30, 7, 90, 1, "days"), ("diag_short_dte_hi", "… to", "", 45, 7, 90, 1, "days"),
  ("diag_short_under_resistance", "Short call under the resistance", "", True, None, None, None, ""),
 ],
}
```

Every default is the §5 catalog row, verbatim. The four credit exit lines map onto the existing
`trade_prefs` keys (`spread_profit_target_pct`, `spread_loss_stop_pct`, `spread_dte_floor`,
`spread_roll_delta`, `services/trade_prefs.py:50-58, 121-125`) and are written through
`tp.write()` so the Positions monitor (`spread_monitor.snapshot_rows`, which reads `tp.read`,
`routes/portfolio.py:96, 102`) and this drawer can never disagree. Account value and risk per
trade are `tp.read(user)["nlv"]` / `["risk_pct"]` for the same reason.

```python
def read(db, user) -> dict:
    """House defaults with this member's overrides on top, per field - the SAME merge
    ema_setup.clean_enabled does for sym_conds (missing key -> default; a stored value
    outside [min, max] -> default, never raise). Returns
    {"shared": {...}, "credit": {...}, "debit": {...}, "condor": {...}, "time": {...},
     "overridden": {family: [keys]}, "hash": <12-hex of the merged dict, excluding exit lines
     that do not change a pick>, "nlv": ..., "risk_pct": ...}."""

def for_strategy(rules: dict, strategy: str) -> dict:
    """The flat dict strike_picker.pick_for reads: the strategy's family block + 'shared'."""

def write(db, user, family: str, form: dict) -> tuple[dict, str]:
    """Store ONLY fields that differ from the house default (so a later admin change to a
    default flows through to everyone who never touched it); drop a field back to 'no
    override' when the posted value equals the default. Out-of-range input is REPORTED,
    not clamped (trade_prefs.write, services/trade_prefs.py:94-134). A checkbox that is
    absent from the form is False (spreads_filter, routes/spreads.py:99-104)."""

def reset(db, user, family: str | None) -> dict: ...
```

#### D3.2 The drawer template

Context: `family, families (list of (key, label, n_overridden)), fields (FIELDS[family] with the merged value and overridden flag per row), rules, err, msg, nlv, risk_pct, translation (one-line per family)`.

```jinja
<div class="opt-rules" id="optRulesPanel">
  <div class="flex flex-wrap items-center gap-1 mb-2">
    {% for key, label, n in families %}
    <button type="button" hx-get="/options/rules?family={{ key }}" hx-target="#optRulesBody" hx-swap="innerHTML"
            class="text-[11px] px-2.5 py-1 rounded border {% if key == family %}border-emerald-500/60 text-emerald-300 bg-emerald-500/10{% else %}border-slate-700 text-slate-400{% endif %}">
      {{ label }}{% if n %} <span class="text-amber-300" title="{{ n }} rule(s) changed from the house default">·{{ n }}</span>{% endif %}</button>
    {% endfor %}
    <span class="ml-auto flex items-center gap-1">
      <button type="button" hx-post="/options/rules/reset" hx-vals='{"family":"{{ family }}"}' hx-target="#optRulesBody" hx-swap="innerHTML"
              hx-confirm="Put the {{ label }} rules back to the house defaults?" class="text-[10px] text-slate-500 hover:text-rose-300">Reset this tab</button>
      <button … hx-vals='{"family":"all"}' hx-confirm="Put EVERY rule back to the house defaults?">Reset all</button>
    </span>
  </div>
  <p class="text-[11px] text-slate-400 mb-2">{{ translation }}</p>   {# e.g. "Sell a put spread with the short strike at delta 0.20–0.30 (≈ 70–80% chance it expires worthless), 30–60 days out, 2 strikes wide, for at least 25% of the width, only when IV rank ≥ 30, with the short strike under support and the trend line. Take profit at 50%, stop at 20% of max loss, out by 21 days." #}
  {% if err %}<div class="… rose">{{ err }}</div>{% endif %}{% if msg %}<div class="… emerald">{{ msg }}</div>{% endif %}
  <form hx-post="/options/rules" hx-target="#optRulesBody" hx-swap="innerHTML" class="grid gap-x-4 gap-y-1.5 sm:grid-cols-2 lg:grid-cols-3">
    <input type="hidden" name="family" value="{{ family }}">
    {% for f in fields %}
    <label class="text-[10px] text-slate-500 flex flex-col" title="{{ f.help }}">
      <span>{{ f.label }}{% if f.overridden %} <span class="text-amber-300" title="changed from the house default {{ f.default }}">·</span>{% endif %}</span>
      {% if f.kind == 'bool' %}<input type="checkbox" name="{{ f.key }}" {% if f.value %}checked{% endif %} class="accent-emerald-500 mt-1">
      {% elif f.kind == 'choice' %}<select name="{{ f.key }}" class="…">{% for v, lbl in f.choices %}<option value="{{ v }}" {% if v == f.value %}selected{% endif %}>{{ lbl }}</option>{% endfor %}</select>
      {% else %}<input type="number" name="{{ f.key }}" value="{{ f.value }}" min="{{ f.min }}" max="{{ f.max }}" step="{{ f.step }}" class="w-24 …"> <span class="text-slate-600">{{ f.unit }}</span>{% endif %}
      <span class="text-slate-600">{{ f.words }}</span>     {# option_words.rule_words(family, key, value): the one-line translation, re-rendered after save #}
    </label>
    {% endfor %}
    {% if family == 'shared' %} …account value (nlv) / risk per trade (risk_pct) inputs, same names as /curated/prefs (routes/curated.py:356-377)… {% endif %}
    <div class="sm:col-span-2 lg:col-span-3 flex items-center gap-2 mt-1">
      <button type="submit" class="px-3 py-1 rounded bg-emerald-600 text-white text-[11px]">Save {{ label }} rules</button>
      <span class="text-[10px] text-slate-600">Saving re-reads the strikes on the card from the stored chain — no market call.</span>
    </div>
  </form>
</div>
```

`choice` labels for `earnings`: `no` → "not allowed", `defined_risk` → "defined-risk trades only", `yes` → "allowed".

`rule_words` examples: `short_delta_lo/hi` → "≈ 70–80% chance the short strike expires worthless"; `credit_pct_min` 25 → "you collect at least $0.25 per $1 risked"; `theta_pct_max` 1.0 → "a $500 option may lose up to about $5 a day"; `stop_atr` 1.0 → "the stop sits one normal day's range under the setup"; `target_r` 2 → "aim for twice what you risk"; `min_oi` 500 → "at least 500 contracts open at each strike".

#### D3.3 Save flow and re-render

1. `POST /options/rules` (one form, one family) → `option_rules.write` → re-render `_options_rules.html` for the same family (target `#optRulesBody`, so the drawer stays open and shows the amber override dots and the new translation line).
2. The response sets `HX-Trigger: {"options:rules-changed": {"family": "credit", "hash": "…"}}`.
3. `#optPicks` (`hx-trigger="options:rules-changed from:body"`) re-GETs `/options/picks/{sym}?strategy=…` → `strike_picker.pick_for` runs on `snapshot_store.latest` rows (DB) under the new hash — **no chain fetch**; the picks table, nearest miss, payoff container and the hidden `pick` inputs re-render; the page script then calls `thPayoffLoad` and `thChartSetStrikes` for pick 0.
4. `#optBasket` listens to the same event and re-renders (the idea column's `has_picks` can change).
5. The headline, chips and the chart do **not** re-render: the recommender's verdict depends on the IV gates in `shared` — when the saved family is `shared`, the trigger payload carries `"recompute": true` and the card's `#optChips` also re-GETs `/options/card/{sym}` (whole card, chart remount accepted; it is the rare case).
6. The summary in the `<summary>` (`#optRulesSummary`) updates via an OOB swap in the same response: "— 3 rules changed" / "— house defaults".

"Reset" semantics: `family=credit` deletes the `credit` block from the row (the four exit lines
are reset through `tp.write` to their `DEFAULT_*`); `family=all` deletes the whole `prefs` dict and
resets the four `trade_prefs` spread keys; `nlv`/`risk_pct` are **never** reset by this drawer
(they are shared with Curated). Both answer with the same `HX-Trigger` so the picks re-render.

---

### D4. Telegram push

#### D4.1 Sender — reuse the credential lookup, not a copy of it

`scripts/_common.py:562-574` (`telegram_env`) resolves `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`
through `_env_lookup("telegram.env")` → `INTRADAY_ENV_DIR` → `TradeHunter/.env` →
`cfg["vault_dir"]/telegram.env` → auto-discovered VAULT (`:154-219`), with `matp.env` as the
legacy filename. `send_telegram(cfg, html)` (`:577-618`) chunks at 4000 chars and POSTs
`parse_mode=HTML`. It has no `chat_id` parameter, and a multi-member platform needs one. So:

```python
# app/services/telegram.py
"""Telegram for the Options page. Credentials come from the intraday bot's lookup
(scripts/_common.telegram_env) so the token lives in the vault once; the chat id is
per member (prefs) with the vault's TELEGRAM_CHAT_ID as the admin's default."""
from . import resources_bridge            # puts TradeHunter/ on sys.path (resources_bridge.py:19-21)
from scripts import _common as thc        # stdlib-only module import; no alpaca at import time

def configured() -> bool:
    token, _ = thc.telegram_env(None); return bool(token)

def send(html: str, *, chat_id: str | None = None) -> tuple[bool, str]:
    """One message (chunked like thc.send_telegram at 4000 chars) to chat_id, or to the
    vault's default chat when chat_id is None. Returns (ok, error)."""
```

The 20-line POST is repeated here because `send_telegram` cannot take a chat id; the chunker
and the headers are copied from `:581-607` so the two behave identically. Import side effects:
`scripts/_common.py:30-36` inserts the TradeHunter layer folders into `sys.path` at import — harmless
for the web app (it already imports `resources.*` through `resources_bridge`).

#### D4.2 Opt-in and address

`user_option_prefs.prefs["shared"]["telegram"] = {"enabled": bool, "chat_id": str|None}` — two
more rows on the Shared tab: a checkbox "Send me each new idea on Telegram (once, the morning it
appears)" and a text box "My Telegram chat id" with help "Open a chat with the TradeHunter bot and
send /start; the id is the number the bot replies with — or leave blank if you are the
administrator and the vault already holds yours." Enabled + blank chat id is allowed only for
`user.is_admin` (falls back to the vault id); for a member it is reported as "enter your chat id
first" and not saved.

#### D4.3 When, and once

Inside the nightly job (part C), after every basket ticker's signal is written and before the
`OptionJobRun` row is finished: `telegram_push.run(db, as_of=…)`.

```python
def idea_key(user_id: int, symbol: str, strategy: str, pick: dict) -> str:
    """'LRCX|bull_put|2026-11-20|330|320' - the legs define the idea. The SAME strikes
    tomorrow are the same idea (not pushed again); a new expiry or a different short strike
    is a new idea. as_of is deliberately NOT in the key."""

def run(db, *, as_of: str, dry_run: bool = False) -> dict:
    """For every member with telegram.enabled: for every basket ticker whose signal has a
    'recommended' strategy AND >= 1 pick under THAT member's rules hash, build the line;
    skip keys already in option_idea_push; send ONE message per member (all new ideas,
    chunked); record one OptionIdeaPush row per idea (ok / error). Soft-fail: an exception
    for one member is logged and the next member is processed; the job's exit code never
    depends on Telegram (portfolio_daily_check.py:120-129 does the same with Discord).
    Returns {"members": n, "ideas": n, "sent": n, "failed": n}."""
```

Keys older than 45 days are pruned by the same job so a repeated setup months later is pushed
again. The badge's `ideas_new` counts `OptionIdeaPush` rows since the last run that this member
has not yet opened (`GET /options` clears a per-member `prefs["options_seen_at"]` marker).

#### D4.4 Text (HTML parse mode)

```
<b>Options · 3 new ideas · Oct 3</b>

<b>LRCX</b> ↗ uptrend · IV rank 62 → <b>sell a put spread</b>
Bounced off support 340 on high volume; riding a trend line with 3 touches.
Nov 20 330/320 put · collect ≈ $210 · risk $790 · 74% chance of keeping it
Stop: LRCX under 338 (≈ −$220) · earnings Oct 22 inside — defined risk only
<a href="https://app.tradehunter.net/options?symbol=LRCX">open the card</a>

<b>ISRG</b> ↗ uptrend · IV rank 41 → <b>buy a call</b>
…
```

One line per element of the card's headline, the first pick in "collect / risk / chance"
wording, the chart stop with its dollar cost, the earnings flag, and the deep link
(`settings.public_url + "/options?symbol=…"`, `app/config.py:143-145`). No greeks by name — the
same words as the page. Dry run (`deploy/options_nightly.py --telegram-dry-run`) prints the
message instead of sending.

---

### D5. The Positions migration (and the other two pages)

#### D5.1 Positions → the Positions tab of `/options`

| What | Moves / stays |
|---|---|
| the board (`_portfolio_list.html`) | **stays as a template, rendered by `GET /options/positions`** through `portfolio._list_context` (D1.14); every form inside keeps posting to `/portfolio/*` (`:76-79, 96-102, 225-230, 278-305, 374-375, 480-481`), which stay registered. `inside_options=True` hides the two `href="/portfolio"` links (`_options_analysis.html:147`, `ivscan.html:114`) and the "Track a bull put spread" `<details>` keeps working for a hand-entered trade |
| the chart pane `#pfChartBody` | stays inside the board; because the Positions tab swaps the whole `#optPane`, it is the only chart in the DOM while that tab is open |
| the expand button `#pfChartExpand` | its handler moves from `portfolio.html` into the page script (D2.1) |
| the default exit-lines form (`_portfolio_list.html:480-501`) | stays for now; its four fields are the same `trade_prefs` keys the Credit tab edits (D3.1), so the two are one setting. Removed with the old pages in the later release |
| the nav badge | `/portfolio/badge` logic is **included** in `/options/badge` (D1.15); `base.html` points at the new one (D1.1); the old endpoint stays |
| the nightly sweep (`deploy/portfolio_daily_check.py`, `spread_monitor.sweep`) | **unchanged**; it grades `option_spreads` rows — which is why step-1 credit verticals are still written there (D1.11) |
| Discord push of positions at a line | unchanged |
| generic positions (`option_positions`, step 2+) | appended under the board as "Other tracked trades" with geometry only (legs, debit/credit, max loss, DTE) and a `Mark closed` that posts to `POST /options/positions/{id}/close` (new, same shape as `/portfolio/{id}/close`, `routes/portfolio.py:350-363`); daily valuation and verdicts (chart stop / target for debit, delta drift and roll dates for LEAPS and diagonals, §5.4) are the step-2 generic valuer's job and are shown as "not monitored yet" until it lands |
| "Track this" on the card | creates the row and switches to the Positions tab with `focus=<id>` (`_portfolio_list.html:54`) so the member lands on the trade they just tracked |
| the Options tab of the Watchlist pane (`_chart_pane.html:69-73`, `routes/options.py`) | **untouched**; its `POST /options/track` form is answered by the new handler's legacy branch (D1.11) |

#### D5.2 IV Rank → the basket; Spread → the screener suggestions

| Old page | Where its engine lives now |
|---|---|
| IV Rank "My list" (`prefs.ivscan_universe`) | `POST /options/basket/import source=ivscan_list` copies it in once; the import button shows how many there are and disappears once the basket has them all |
| IV Rank "Whole market" (bridge `/scan`) | the basket's **Run my TWS scanner** button: same browser flow (`ivscan.html:268-297`), the symbols land as basket rows with `source="scanner"`; the three pair floors (IV rank > 30, price > 50, volume > 200k) are the scanner's own inputs and are remembered under the same `prefs.ivscan_criteria` key (`routes/ivscan.py:78-83`) so the two pages agree |
| the per-ticker IV rank read from TWS (`fillIV`, `ivscan.html:163-213`, `POST /ivscan/iv`) | replaced by (a) the server-side rank from `iv_daily` for every basket ticker, no TWS needed, and (b) the **Live** button's `/iv?series=1` bootstrap (D1.10), which stores the whole year instead of three numbers |
| the setup grading / switches (`sym_conds`, `_sector_conds.html`) | not on this page: the card's chart read comes from the signal (part C), which uses the support-bounce / EMA detectors directly. The member's `sym_conds` switches keep governing Sector & Industry and Curated; a note on the Shared tab says so |
| Spread screener (`spread_candidates`, nightly `deploy/spread_scan.py`) | the basket's "Suggested by last night's screener" section (D2.2): top candidates by `credit_pct` that pass the member's shared liquidity rules (`short_oi`, `long_oi ≥ min_oi`; both legs' `ask−bid ≤ max_ba_width`) and the pair floors, excluding tickers already in the basket; `[+]` adds one (`source="screener"`), after which tonight's job gives it a full card. The Spread page's filter bar is not carried over — the card's rules replace it |
| Spread "Track" (`routes/spreads.py:147-168`) | the card's Track (D1.11) writes the same `OptionSpread` fields |

Both old routers stay registered with widened guards (D1.1) and their templates untouched. The
later removal release deletes `routes/ivscan.py`, `routes/spreads.py`, `routes/portfolio.py`'s
page shell (`GET /portfolio` only — the fragment endpoints move into `options_page.py`), the three
page templates, `OFF_NAV_KEYS` entries and the `iv_scan_items` table (a drop migration).

---

### D6. Dashboard-visibility items

| Item | Surface | Source | States |
|---|---|---|---|
| **Nightly job health pill** | `#optStatus` strip (D2.1), left side: `job ✓ 06:31 MYT · 5/5 tickers` | latest `OptionJobRun` (`job='options_nightly'`), `finished_at` localised via `localtime()` (`_time.html`) | emerald: `finished_at` set, `failed == 0`, `as_of == last ET trading day` (`spread_monitor.et_today()` minus weekend/holiday via the same helper the sweep uses, `services/spread_monitor.py:204-242`); amber: `failed > 0` ("5/7 tickers — KO: Cboe HTTP 403, …" from `note`) or `as_of` one trading day behind; rose: no run for ≥ 2 trading days, or `started_at` set and `finished_at` null for > 2 h (crashed) |
| **Data-age badge** | card header (per ticker) and the basket dot | `option_signal.as_of`, `snapshot.source`, `snapshot.partial` | `as of Oct 2, 16:00 ET · delayed` (emerald, < 20 h) / amber (20 h–3 trading days) / rose (older); `· live (TWS) for Nov 20` when `partial`; the strip's own age is the **oldest** basket ticker's |
| **"Hermes job missed" notice** | amber banner above the card, and the nav badge's `⚠` chip (`job_missed` in `/options/badge`) | `OptionJobRun` absent for the last ET trading day by 08:00 MYT (`job_missed = latest.as_of < et_today() and now_myt.hour >= 8`) | "Last night's data job did not run (last run {{ when }}). The cards show {{ as_of }} data. Press Refresh on a ticker for today's delayed data; an administrator can read `/admin/log`." |
| **New-ideas count** | nav badge (`ideas_new`) and the Ideas tab label `Ideas ●3` | `OptionIdeaPush` since last run, not yet seen | cleared when `GET /options` renders |
| **Positions at a line** | nav badge (`urgent` / `watch` / stale `!`) and the Positions tab label `Positions ●2` | `/portfolio/badge` logic (`routes/portfolio.py:425-455`) | unchanged meaning |
| **Bridge state** | the `[Live (TWS)]` button turns emerald with `· up` after a successful `/health` probe (the page probes once on load, `_options_tab.html:190-194` pattern) | | `[Live (TWS)]` grey = not probed / down; `[Live (TWS) · up]` emerald |
| **Refresh in flight** | `hx-indicator` spinner on the button; the card dims (`htmx-request` class) | | |

`_options_status.html` context: `run (OptionJobRun|None), state ('ok'|'warn'|'bad'|'none'), as_of_oldest, n_basket, n_stale, bridge_port, delayed_or_live`. Markup is one `flex flex-wrap items-center gap-x-3 text-[11px]` line: `Options · {{ n_basket }} tickers · Data as of {{ as_of }} ET · {{ 'delayed' | 'live for n' }} · <pill job> · [Refresh all stale ({{ n_stale }})]` — the last button posts `/options/refresh/{sym}` for each stale ticker sequentially from the page script with the cooldown respected (the server refuses bursts anyway).

The job that writes `OptionJobRun` is part C's `deploy/options_nightly.py`; this part supplies
`services/job_runs.py` with `start(db, job, as_of) -> OptionJobRun`, `finish(db, run, *, done, failed, pushed, note)`,
`latest(db, job) -> OptionJobRun | None`, `missed(db, job) -> bool` so the status strip, the badge
and the job all use one definition of "missed".

---

### D7. Reviewer's checklist (non-technical member, 10 items)

A reviewer opens `/options` as a member with the house defaults and checks:

1. **Reading level.** Every sentence on the card (headline, picks, payoff caption, empty states) reads at or below a grade-8 level: no sentence over 25 words, no word a non-trader would look up without a hover. Test: read the LRCX card aloud to someone who does not trade.
2. **No raw greek without words.** Every delta, theta, vega, gamma, IV, IV rank, OI and bid/ask number on screen has the D2.7 sentence as its `title` or beside it. The strike table's chance column says "chance of keeping it" / "chance of profit", never "POP" or "1−Δ".
3. **No jargon in buttons.** Button labels are verbs a non-technical person understands: `Refresh`, `Live (TWS)`, `Order ticket`, `Track this`, `Add ticker`, `Import`, `Save Credit spreads rules`, `Reset this tab`. Not "Analyze", "Rescan", "Ingest", "Grade".
4. **One primary action per card.** Exactly one filled (blue) button on the card — `Order ticket`. `Track this`, `Refresh`, `Live` are ghost buttons. On the Positions tab the primary is the board's own `Track` for a hand-entered trade.
5. **The headline does the work.** Covering the chips, picks and chart with a hand, the headline sentence alone tells the member what the chart is doing, why, and what to do. Trend + evidence, setup, IV regime, conclusion — all four present or honestly absent ("No fresh setup today").
6. **Rejections explain themselves.** Every greyed chip carries a reason in plain words; clicking it still shows strikes with the amber "not recommended today: …" banner. "Other strategies" hides the rest; nothing is silently missing.
7. **Honesty strip always present.** Data age, delayed/live, and the job pill are visible on every state of the page, including the empty basket, and the age colour matches the badge rules in D6. Pull the network and press Refresh: the card says what failed and keeps the old data.
8. **Both stop lines, labelled.** The payoff chart shows the chart stop and the rule stop, each with its price and dollar cost, and the caption under the chart explains the dashed line in one sentence.
9. **Numbers are in money first.** Collect / risk / chance before delta or ratio; `$` units by default, `R` only on the toggle; contracts sized from the member's own account value or a hint to set it.
10. **Layout rules.** No horizontal page scroll at 375 px; the basket becomes a chip strip; one chart in the DOM at a time (switch Ideas/Positions and check `document.querySelectorAll('#priceChart').length === 1`); every scroll area's scrollbar is invisible until hovered (no `scrollbar-*` / `::-webkit-scrollbar` in the new templates); light theme: the teal IV number, the amber override dots and the payoff legend are legible on white.

---

### D8. Test plan

#### D8.1 Browser walkthrough (dev-check config)

Uses `.claude/launch.json` → `tst-devcheck`: password auth, the admin from `TST_ADMIN_EMAIL`/`TST_ADMIN_PASSWORD` in that entry, `TST_DATABASE_URL=sqlite:///…/tst_dev_check.db`, port **8011**. Machine: **Laptop**, two PowerShell windows (one for the server, one for the job), both with the same `TST_*` env vars from the launch entry so the job writes the same dev DB.

| # | Step | Expected |
|---|---|---|
| 1 | Start `tst-devcheck`; open `http://127.0.0.1:8011/`, sign in as the dev admin | lands on Calendar; the nav reads `Calendar · Sector & Industry · Watchlist · Curated · Options`; no Options dropdown; `/ivscan`, `/spreads`, `/portfolio` still open by URL |
| 2 | Click **Options** | shell renders in < 200 ms; status strip: `0 tickers · no data yet · job: never run` (slate); basket empty-state text (D2.9); pane empty-state text; My rules closed with "— house defaults" |
| 3 | `curl -s -o /dev/null -w "%{http_code}" -b <cookie> http://127.0.0.1:8011/options/basket` and `/options/rules?family=credit` | both `200` with the fragment, **not** the legacy `_options_tab.html` shell (proves the router order, D1.1) |
| 4 | Import → Paste `LRCX, MA, ISRG, NVDA, KO, lrcx, XX1234567890123` → Save | basket shows 5 rows (duplicate and junk dropped; toast "5 tickers added, 2 dropped"); each row has a rose dot, `·`, `–`, `no read`; strip says `5 tickers` |
| 5 | Click LRCX | card: header `LRCX · no read yet` state text; chart mounts with EMA 20/50/200 and no overlays; no chips/picks/payoff; `[Refresh]` enabled |
| 6 | Press **Refresh** | ≤ 3 s: note strip "Refreshed from Cboe HH:MM ET"; headline sentence; gauge; chips in the decision-9 order; chart gains the support line (if the detector found one), the strike lines and the trend line; picks table (≤ 3 rows) with collect / risk / chance; payoff with both stop lines and the caption; basket row's dot turns emerald and its idea word fills in; pressing Refresh again inside 60 s → toast "Just refreshed — try again in a minute." |
| 7 | Click the second pick row | strike lines on the chart move without the chart remounting (the EMA legend does not flicker; `window.__thSetup` identity unchanged); payoff repaints; `#optActions` hidden `pick` = 1; **no** request to `/options/chart` in the network tab |
| 8 | Click a greyed chip (e.g. `Buy call · expensive`) | picks re-render with the amber "Not recommended today: options are expensive (IV rank …)" banner; strikes shown; chart lines switch to the Entry/SL/PT read-only set (debit family); payoff shows target + both stops |
| 9 | Open My rules → Credit spreads; set `short_delta_hi` 0.30 → 0.22; Save | drawer re-renders with an amber dot on that field and "·1" on the tab; picks re-render (watch the server log: `pick_for` runs, **no** `fetch_chain` line); nearest-miss line names the rule; basket idea column may fade for tickers that lost their picks |
| 10 | Reset this tab | override gone; picks back to the 0.30 set; `#optRulesSummary` "— house defaults" |
| 11 | Shared tab → set account value 50 000, risk 1% → Save | contracts box on the card re-sizes (`floor(500 / max_loss)` ≥ 1); the Curated page shows the same NLV (`/curated` prefs strip) |
| 12 | **Order ticket** | panel with the D2.8 text, correct expiry label, limit = credit at the mid rounded to 0.05, both plan lines, the earnings line if inside; `[Copy]` puts the text on the clipboard |
| 13 | **Track this** (1 contract) | pane switches to Positions with the new row ringed; the row grades against the lines (`spread_monitor.snapshot`), Δ and P/L present (Cboe); nav badge unchanged (nothing at a line); `option_spreads` has one row with `strategy='bull_put'`, `source='options_page'`, `idea_key` set |
| 14 | Press **Live (TWS)** with no bridge running | ≤ 2 s: note strip's bridge text with `[Start the bridge]` `[Retry]` and the diagnostics `<details>`; card otherwise unchanged |
| 15 | With TWS + bridge 1.4 running (laptop only): **Live** | note "Live from your TWS HH:MM"; age badge "live (TWS) for <expiry> · other expiries delayed"; `iv_daily` gains ~250 `source='ibkr'` rows for LRCX; the card shows both IV ranks ("62 (TWS, live) · 59 (delayed)") |
| 16 | Run the nightly job once by hand, same env: `py deploy\options_nightly.py --telegram-dry-run` (**Laptop**, the job window) | log: 5 tickers done / 0 failed; `option_job_runs` row; the dry-run Telegram text printed in the D4.4 format with one block per ticker that has a recommendation and picks; `option_idea_push` rows written with `ok=1` (dry run still records, flagged `error='dry-run'`) — re-running prints nothing new (dedupe) |
| 17 | Reload `/options` | strip pill emerald `job ✓ <time> · 5/5 tickers`; Ideas tab label `Ideas ●n`; nav badge shows the `n` chip; opening the page clears it on the next poll |
| 18 | In SQLite set the job row's `as_of` two trading days back and `option_signal.as_of` to 4 days ago; reload | amber "job missed" banner text (D6); age badge rose; nav badge `⚠` chip |
| 19 | Positions tab: Mark closed → Closed filter → Reopen | board posts to `/portfolio/{id}/close|reopen` and re-renders inside `#optPane`; `/portfolio` by URL shows the same row |
| 20 | Theme toggle to light | teal IV number legible; amber dots legible; payoff legend text dark; chips readable (no pale-on-pale) |
| 21 | Resize to 375 px (or `resize_window mobile`) | basket is a horizontal chip strip; card full-width; chart 300 px tall; strike rows stack; no horizontal page scroll; My rules a bottom `<details>` |
| 22 | Hover each scroll area | scrollbar appears only on hover (basket, pane, rules body, full-chain expander) |
| 23 | Watchlist → a ticker → Options tab → "Track this trade" (legacy form) | `POST /options/track` legacy branch: the Open spreads list under the tab re-renders (`_options_positions.html`) with the row; no 422 |
| 24 | Sign in as a non-admin member granted only `options` (Admin console) | `/options` opens; `/options/positions` opens (widened guard); the Telegram checkbox refuses to save without a chat id |

#### D8.2 Synthetic cases (unit tests, `dashboard_tst/tests/test_options_page.py`)

| Case | Asserts |
|---|---|
| `option_rules.read` with no row | every family block equals the house defaults; `hash` stable across calls; `overridden == {}` |
| `write("credit", {"short_delta_hi": "0.30"})` (equals default) | stores no override; `write(... "0.22")` stores one; `write(... "0.9")` returns the error "Short strike delta, … must be between 0.05 and 0.50." and stores nothing |
| `write("credit", {"take_pct": "40"})` | `tp.read(user)["profit_target_pct"] == 40` (written through `trade_prefs`) |
| `reset("all")` | prefs row empty; `trade_prefs` spread keys back to defaults; `nlv` untouched |
| `chip_row` with 1 recommended, 2 also, 5 rejected | first = recommended; also list in order; exactly 2 greys (the two closest by score) with `reason_short`; 3 in `rest` |
| `chip_row` with 0 recommended | `first is None`; greys still ≤ 2; the picks context's `strategy` is `''` and the template shows the "nothing fits" copy |
| `headline` with every clause present / with no setup / with sideways | the exact D2.7 sentences, punctuation included |
| `delta_words(0.25, "sell")` | contains "1-in-4" and "75% chance of keeping" |
| `pop_words(0.46, "debit")` | "46% chance of profit" and never "keeping" |
| payoff JSON for a 330/320 put spread at credit 2.10, spot 349.2, stop 338 | `max_profit == 210`, `max_loss == 790`, `breakevens == [327.9]`, `expiry` plateaus equal those, `stop_rule.pl_today ≈ −158` (±5), `stop_chart.price == 338`, `today` within $5 of `expiry` at `dte=1` |
| payoff for a calendar | `today_is_front_expiry == False`, curve drawn at the front expiry, `max_loss == net debit` |
| `idea_key` | same strikes on two days → one key; a different long strike → a different key |
| `telegram_push.run` with two members, one opted out, one with a chat id, `dry_run=True` | one message built; `OptionIdeaPush` rows only for the opted-in member; second run sends nothing |
| legacy form POST to `/options/track` | row created with `strategy='bull_put'`, response is `_options_positions.html` |
| new-form POST with a stale `pick` index | 409 + the toast trigger; no row |
| basket import of 70 symbols | 60 kept, `dropped == 10`, response says so |
| `GET /options/basket` as user A after user B adds LRCX | A's basket empty (scoping) |
| router order | `app.routes` index of `GET /options/basket` < index of `GET /options/{symbol}` |
| `job_runs.missed` at 07:59 MYT with no run today | `False`; at 08:00 → `True` |
| the chart fragment for a debit strategy | context has `chart_levels` and **no** `chart_setup_seed`; for credit it has `chart_spread.legs` with two entries and `chart_levels` absent |

#### D8.3 Live tickers (README "Tested:" line, to be filled in when built)

Tested: dev DB with LRCX, MA, ISRG, NVDA, KO (the §7 mock basket); the LRCX card after Refresh
(headline with trend + support bounce + IV regime, bull put recommended, ≤ 2 greys with reasons,
three picks in collect/risk/chance wording, payoff with chart stop and rule stop both labelled,
the T+0 caption); a pick click moving the strike lines without a chart remount; a rule change
re-rendering the picks with no Cboe call in the log; Reset; sizing from account value; the
order ticket text; Track → Positions tab with the row graded; Live with the bridge down (calm
panel) and up (IV series stored, two ranks shown); the nightly job by hand with the Telegram dry
run (one message, dedupe on the second run); the status pill in all four states; the job-missed
banner; light theme; 375 px; the legacy Watchlist Options tab's Track still answered;
`/ivscan`, `/spreads`, `/portfolio` still reachable by URL for a member granted only `options`.

---

### D9. Files to create / modify (summary)

| Action | Path |
|---|---|
| create | `app/routes/options_page.py` |
| create | `app/services/option_rules.py`, `app/services/option_words.py`, `app/services/telegram.py`, `app/services/telegram_push.py`, `app/services/job_runs.py` |
| create | `app/templates/options.html`, `_options_basket.html`, `_options_card.html`, `_options_picks.html`, `_options_payoff.html`, `_options_chart.html`, `_options_rules.html`, `_options_ticket.html`, `_options_status.html`, `_options_chain.html` |
| create | `alembic/versions/f9a0b1c2d3e4_options_module.py` (shared with parts A/C) |
| modify | `app/models.py` (D1.12 classes), `app/main.py` (D1.1), `app/menus.py` (D1.1), `app/templates/base.html` (badge → `/options/badge`, href check, two chips, light-theme teal/violet inks), `app/templates/_price_chart.html` (`chart_trendline`, generalised `chart_spread`, `window.thChartSetStrikes`), `app/templates/_portfolio_list.html` (`inside_options` flag hides two links; appends the generic positions block), `app/services/glossary.py` (option group), `app/__init__.py` (`4.126` → `4.127`), `bridge/ibkr_bridge.py` (`/iv?series=1`, version 1.4), `README.md` (changelog entry per the folder convention), `OPTIONS_MODULE_DESIGN.md` (status line → "building, step 1") |
| untouched on purpose | `app/routes/options.py`, `routes/ivscan.py`, `routes/spreads.py`, `routes/portfolio.py` (guards only change in `main.py`), their templates |
