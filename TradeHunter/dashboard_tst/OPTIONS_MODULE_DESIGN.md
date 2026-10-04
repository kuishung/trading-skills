# Options module — basket, option data, strategy + strike recommendation

**Status: DISCUSSION / not started.** No code written for this module yet. This file
captures the whole design conversation so it can be resumed on any machine
(cross-PC via git + Dropbox; a pointer lives in `CLAUDE.md`). Last updated 2026-10-04 (Part II
reconciled; the integrator rulings R1-R13 applied, see II.5 and the Changelog).

How to resume: read this file top to bottom, then answer the **Open decisions** at
the end; the build order follows from them. Nothing here is implemented except
where the "Exists today" table says so.

---

## 1. The vision (user, 2026-10-03, verbatim)

> I want a dashboard for Option trade. In this option module, I will have a basket
> of ticker that is added by user and I will want the system to capture the tickers
> under the option study module. I will need the option data to do the analysis.
> Where I can get the data and store in tradehunter. Please let me know what data I
> can get from the market. I will want the Delta, Vega, Gamma, Theta, HV, IV, IV
> Rank and all the option data. I want to keep the data to be stored Tradehunter db
> so that the system will be more responsible [responsive].
>
> Based on the chart trend, i want the system to be able to recommend to user
> 1. What strategy is suitable for the particular chart setup
> 2. Based on the option data, i need the system to be able to calculate for me
>    what strike to choose with the best return, in doing this the user can present
>    the value of the greeks which you would choose from the option chain. Each
>    user can set their greek value to choose from.
> 3. The system also have to detect based on the IV and IV rank whether it is good
>    to sell option.
> 4. User who uses this system will not be a professional or technical person, the
>    layout should be easy to navigate and should be user friend and easy to
>    understand.
> 5. the system must be responsive and cannot be waiting too long for the result,
>    the option data need to be kept in own db?

Later in the same discussion the user added:

- "we are using EMA20 EMA50 and EMA200 as the moving average lines"
- "we also want the system to be able to draw trend line"
- "The dashboard shall also have the risk and reward chart for the option."
- The strategy list (section 5) — ten strategies, exactly those.
- Asked "human intervention is required a lot to come out with the suggest trade?"
  and "the human still need to see and curate the setup?" → answered in section 3:
  the system is the curator; the human only approves and places the order.

---

## 2. Mindmap (the shape of the module)

```
OPTIONS MODULE
│
├── 1. BASKET  (whose tickers, from where)
│   ├── fed by: typed in (= today's IV Rank "My list", prefs.ivscan_universe) · My Watchlist
│   │           (user_watchlist) · the TWS High-IV scanner (bridge /scan) · a "study options
│   │           on this" button on Sector / Curated pages (new) · a SYSTEM basket (see §3)
│   └── store: option_basket(user, symbol, added_on, note, source)  — separate from the
│       watchlist (different cadence, different data cost)
│
├── 2. DATA  (what, from where, how often)             → §4
│   ├── per contract: bid/ask/mid/last · volume · open interest · IV · delta gamma theta vega rho
│   ├── derived: HV20/HV60 · IV rank · IV percentile · IV/HV · term structure · skew ·
│   │            expected move · POP · days-to-earnings
│   ├── sources: Cboe delayed (exists) · IBKR bridge (exists) · Alpaca (fallback) · paid (later)
│   ├── store: option_chain_snapshot · iv_daily · option_signal · user_option_prefs
│   └── cadence: nightly on Hermes → page reads DB in ms; on-demand refresh; "Live" via bridge
│
├── 3. ENGINES  (what the system decides)              → §6
│   ├── 3a strategy recommender  (chart setup + IV regime → strategy, rule table, no LLM)
│   ├── 3b strike picker         (member's greek rules → top-3 strikes, return × POP)
│   ├── 3c premium gauge         (IV rank, IV/HV, term structure → SELL / NEUTRAL / BUY)
│   ├── 3d trend-line engine     (auto trend lines from swing points; a bounce / break level)
│   └── 3e payoff chart          (risk & reward at expiry + today, stop/target marked)
│
├── 4. UI  (for a non-technical member)                → §7
│   ├── ONE page "Options": basket (left) · ticker card (centre) · "My rules" (bottom)
│   ├── card: headline sentence · strategy chips · strike picker · price chart with EMAs,
│   │         trend line, support, strikes · payoff chart · Order ticket · Track this
│   └── replaces IV Rank + Spread + Positions (the three hidden from the nav in v4.126)
│
└── 5. RESPONSIVENESS & RELIABILITY
    ├── read path = DB only; nothing on the page waits on a market call
    ├── every source soft-fails with a stale badge; never a blank page
    └── Hermes job log + health pill (dashboard-visibility rule)
```

---

## 3. Human in the loop — what is automatic, what stays human

```
  ONE-TIME                    AUTOMATIC (nightly on Hermes, nobody present)                  ALWAYS HUMAN
 ┌────────────────────┐  ┌──────────────────────────────────────────────────────────────┐  ┌──────────────┐
 │ ① add tickers      │→ │ fetch chain + greeks → store                                  │→ │ ③ read the   │
 │ ② set greek rules  │  │ HV, IV rank, IV/HV, term structure                             │  │   card,      │
 │   (or keep house   │  │ read the chart: EMA stack, setup (support bounce / trend line /│  │   decide,    │
 │   defaults)        │  │   EMA rebound / range), structure, earnings                    │  │   place the  │
 └────────────────────┘  │ premium gauge → strategy rule table → strike picker → sizing   │  │   order in   │
                         │ write the "today's idea" card into the DB                      │  │   TWS/moomoo │
                         └──────────────────────────────────────────────────────────────┘  └──────────────┘
```

- The system **is the curator**. The setup is found and judged before anyone logs in;
  nothing on the card is typed by a person. The chart is on the card so a member can
  **veto in five seconds**, not so they draw anything.
- The only recurring human act is **"yes" + placing the order**. The platform has
  **no order execution** by design (security posture in `DESIGN.md`); the card hands
  over a ready **order ticket** (strategy, strikes, expiry, limit price, conditional
  trigger on the underlying, stop level) to paste into the broker.
- Two cheap steps that remove even the "look" (both reuse the engine):
  1. **Push, don't pull** — the nightly job sends the card to Telegram (the alert
     path already exists): "LRCX — uptrend, bounced off 340 on volume, IV rank 62 →
     sell the Nov 330/320 put spread for ~2.10, 75% chance of keeping it."
  2. **Auto-track on paper** — every qualifying idea is taken into the Positions
     monitor as a paper position and managed daily (50% profit / 20% loss / 21 DTE),
     building a track record of the system's own calls.
- What should stay human: the order; the **catalyst check** (earnings are detected;
  M&A / offerings / FDA are not — a per-ticker news line, e.g. the Alpaca news API,
  would close most of that later); vetoing a chart read.
- "Curated" (the member's own hand-made calls) stays separate; "Track this" moves a
  system idea into the member's own list.

---

## 4. Data

### 4.1 What the market gives, per contract

bid · ask · mid · last · volume · open interest · implied vol · delta · gamma · theta ·
vega · rho; per underlying: spot, ATM "IV30". **Derived by us:** HV20 / HV60 (from the
daily closes already stored), IV rank, IV percentile, IV / HV premium (the unitless ratio
IV30 ÷ HV20, never "vol points"), term structure
(front-month vs next-month IV, and IV per expiry for calendars/diagonals), put/call
skew, expected move (IV × √DTE), POP (1 − |delta|), days to earnings.
**Not available free:** real-time greeks on the server; historical option prices
beyond what we store ourselves.

### 4.2 Sources, ranked for this job

| | Source | Status | Gives | Caveat |
|---|---|---|---|---|
| A | **Cboe delayed JSON** `cdn.cboe.com/api/global/delayed_quotes/options/<SYM>.json` | **exists** — `app/services/option_quotes.py` | full chain, all greeks, iv30, spot; no key; server-readable | ~15 min delayed; undocumented public endpoint — every reader must survive it vanishing (`ChainError`) |
| B | **IBKR bridge** `bridge/ibkr_bridge.py` (`/chain`, `/iv`, `/scan`, `/account`) | **exists** | live greeks (delta, gamma, theta, vega, IV, OI, volume); 1-year daily IV → rank & percentile; HV series | only while a member sits at a PC with TWS open; the server cannot reach it |
| C | **Alpaca options data** (user holds Alpaca creds) | candidate | server-side snapshots with greeks | the *contracted* fallback if A disappears — wire behind the same interface |
| D | paid analytics (ORATS / Polygon / Tradier) | not now | IV analytics, history | only if we outgrow A + C |

### 4.3 Storage (SQLAlchemy ORM, Alembic migration, Postgres-ready — CLAUDE.md rule)

| Table | Columns (sketch) | Notes |
|---|---|---|
| `option_basket` | user_id, symbol, added_on, note, source (typed / watchlist / scanner / system) | per member |
| `option_chain_snapshot` | symbol, as_of, expiry, strike, right, bid, ask, mid, iv, delta, gamma, theta, vega, oi, volume, spot, source | EOD snapshot per basket ticker, **all expiries** (LEAPS / calendars need the far months); keep 90 days of EOD, only the latest intraday. ~2–3k rows per ticker per day → 30 tickers × 90 days ≈ 6M rows: fine for Postgres, OK on SQLite if EOD-only |
| `iv_daily` | symbol, date, iv30, iv_front, iv_back, term_ratio, hv20, hv60, iv_rank, iv_pct, iv_n, iv_state, skew25, expected_move | grows from `iv_history` (exists: daily iv30 from Cboe, 370-day retention, accumulating since 2026-09-10) → server-side rank/percentile with no TWS |
| `option_signal` | symbol, as_of, trend, setup (kind, level, touches), iv_verdict, strategies (ranked JSON), picks (per strategy, per member-prefs hash), payoff summary | **the cache the UI reads** |
| `user_option_prefs` | user_id, prefs JSON: shared block + one block per strategy family | house defaults merged on read, like `sym_conds` |
| existing, reused | `iv_history`, `spread_candidates` (nightly Cboe screener), `option_spreads` (positions + the management monitor), `user_watchlist`, `iv_scan_items` | |

### 4.4 Cadence (what makes it responsive)

- **Nightly on Hermes, after the close:** chain snapshot + `iv_daily` + signals for every
  basket ticker → the page opens in milliseconds from the DB.
- **On-demand "Refresh"** per ticker: Cboe delayed, ~1–2 s.
- **"Live"** button: the member's own bridge, exact greeks when they are about to trade.
- Every source soft-fails: a stale badge ("as of yesterday 16:00 ET"), never a blank page.

---

## 5. The strategy catalog (user's list, 2026-10-03) and the rules per strategy

Rules come in three layers. **Layer 1** is shared; **layer 2** differs by strategy and is
what "My rules" edits; **layer 3** comes from the chart and is not a preference.

### 5.1 Shared rules (liquidity + safety) — house defaults

| Rule | Default | Why |
|---|---|---|
| Minimum open interest | 500 per leg | so you can get out |
| Max bid/ask width | $0.50 per leg | wide markets eat the edge |
| Earnings inside the expiry | not allowed (or defined-risk only) | the one thing a stop cannot protect |
| Risk per trade | 1% of account | the user's sizing rule |

### 5.2 Per-strategy rules

**Directional, you pay — want cheap options (IV rank low, ideally ≤ 30)**

| Strategy | Chart condition | Member's greek rules | DTE | Picker optimises |
|---|---|---|---|---|
| Buy call | uptrend + fresh setup (bounce / breakout retest / trend-line touch) | long delta 0.60–0.70; theta/day ≤ 1% of premium | 45–90 | stock-like movement per $ of premium; stop/target from the chart (1 ATR / 2R) |
| Buy put | downtrend + failed support / breakdown | same, mirrored | 45–90 | same |
| Bull call spread | uptrend, IV mid (30–50) so a naked call is dear; a clear target (resistance) to cap at | long leg 0.60–0.70, short leg 0.25–0.35 at/above the target | 30–60 | reward ÷ cost, short strike ≥ chart target |
| Bear put spread | downtrend, mirrored | same, mirrored | 30–60 | same |
| Buy LEAPS | long-term uptrend (weekly EMA stack, the W setup) — stock replacement, not a swing | delta 0.70–0.80 (deep ITM); extrinsic ≤ 10% of price | 9–18 months | delta per $ (leverage) with the least time value paid |

**Directional, you are paid — want expensive options (IV rank ≥ 30)**

| Strategy | Chart condition | Member's greek rules | DTE | Picker optimises |
|---|---|---|---|---|
| Bull put spread | uptrend + support holding (the support-bounce setup) — short strike UNDER support (and under the trend line) | short delta 0.20–0.30; width; credit ≥ 25–33% of width | 30–60 | credit ÷ max loss × POP (**exists**: `bull_put.rank_pairs`) |
| Bear call spread | downtrend + resistance holding — short strike ABOVE resistance | same, mirrored | 30–60 | same |

**Neutral, you are paid (IV rank ≥ 50)**

| Strategy | Chart condition | Member's greek rules | DTE | Picker optimises |
|---|---|---|---|---|
| Iron condor | sideways: EMA stack flat, a range with both edges touched ≥ 2 times; no earnings | short delta 0.15–0.20 each side; wing width; credit ≥ 30% of width | 30–45 | credit × POP with both short strikes outside the range |

**Time spreads — the IV term structure decides, not just the level**

| Strategy | Chart condition | Member's greek rules | DTE | Picker optimises |
|---|---|---|---|---|
| Calendar spread | sideways or slow drift; price expected to sit near a strike; front IV ≥ back IV | ATM, delta ≈ 0.50, both legs same strike | front 20–30 / back 50–70 | front-month decay collected per $ of back-month cost |
| Diagonal call spread (long-dated call + short near-term call; "poor man's covered call") | uptrend, slow grind; a resistance to sell the short call under | long leg 0.70–0.80 (LEAPS), short leg 0.20–0.30 | long 6–12 months / short 30–45 | monthly income ÷ long-leg cost, short strike under resistance |

Premium **sellers** care about the short delta, credit-to-width and decay working for
them; premium **buyers** care about the long delta, decay working against them, and
buying when IV is cheap. The same number means opposite things on the two sides.

### 5.3 Chart-derived rules (not a preference)

Where strikes sit relative to the level: short put strike under support / trend line;
short call strike above resistance; long-call stop at 1 ATR and target at 2R (the
Curated setup convention); iron-condor shorts outside the range. These come from the
setup detector.

### 5.4 What the catalog implies

- **Three IV gates, not one** — buy (rank low), sell directional (≥ 30), sell neutral
  (≥ 50) — plus **term structure** (front vs back IV). So `iv_daily` stores IV per
  expiry, not just IV30 (computation on the Cboe feed, not new data).
- **The chart reader needs a "sideways" verdict** with a defined range (both edges
  with touches): the support-bounce detector finds the lower edge; its mirror finds
  the upper. That unlocks iron condor and calendar.
- **Two-expiry strategies** (calendar, diagonal, LEAPS) need the snapshot to keep all
  expiries including 1–2 years out.
- **Exits differ by family** and the Positions monitor must know which it manages:
  credit spreads by the 50% / 20% / 21-DTE rules (exist); debit trades by the chart
  stop and target; LEAPS and diagonals by delta drift and roll dates.
- **Two chips per card, sometimes**: uptrend with IV rank 45 is honestly "bull put
  spread OR bull call spread" — the recommender ranks, it does not force one.
- Not on the list, deliberately left for later: **covered call** and **cash-secured
  put** (same picker as the credit spreads, different capital calculation; members
  will ask once they hold stock).

---

## 6. Engines

### 6a. Strategy recommender — rules, explainable, no LLM in the decision path

Inputs: trend (EMA 20 > 50 > 200 stack; `ema_setup`), setup (support bounce —
`support_bounce.py`, shipped v4.124/v4.125; EMA rebound; breakout retest; trend-line
touch; range), structure (HH/HL), IV regime (rank), IV vs HV, term structure, earnings
inside the expiry. Output: the catalog rows that fit, ranked, each with a one-sentence
WHY and "what has to happen for this to work"; the ones that do not fit are shown
greyed with the reason ("options too expensive", "trending, not sideways").

### 6b. Strike picker — "best return under MY greek rules"

Enumerates the stored chain, filters by the member's prefs for the chosen strategy
(delta band, DTE, OI, bid/ask, credit/width or theta/premium), applies the chart-derived
constraint (§5.3), scores (return on risk × POP, penalising wide markets), returns the
top 3 with the greeks **in words** ("delta 0.25 ≈ 1-in-4 chance of being in the money"),
framed as *you collect / you risk / chance of keeping it*. Generalises
`bull_put.rank_pairs` to every strategy.

### 6c. Premium gauge — "is it a good time to SELL options?"

IV rank ≥ 50 → sell · 30–50 → either · < 30 → buy premium; IV / HV premium, the ratio
IV30 ÷ HV20 (≥ 1.10 = IV above realised = sellers are paid; ≤ 0.90 = buy); term structure
(front > back = an event is priced). One
dial per ticker: SELL / NEUTRAL / BUY with the two numbers behind it.

### 6d. Automatic trend lines (new engine)

Same approach as the horizontal support detector, tilted:
- **Candidates**: swing lows in an uptrend (swing highs in a downtrend) over the last
  ~120–250 sessions — the same tie-tolerant, reaction-tested pivots `support_bounce.py`
  finds.
- **A line** = any two pivots with the slope the right way; every other pivot within
  0.35 ATR of it is a touch. **Valid** only if price never *closed* more than 0.5 ATR
  through it between its first touch and today — a broken line is history, not a
  trend line.
- **Choice**: most touches (3+ preferred), then longest span, then most recent touch.
  Everything ATR-relative (CLAUDE.md: no absolute thresholds).
- **It becomes a level**: the line's value today = dynamic support; the line's value
  at the option's expiry = where the short strike should sit under. The setup detector
  gets a second kind of bounce (pin bar / engulfing AT the trend line) and a warning
  (a close through the line).
- **Drawn read-only** on the chart with its touch count, like the support line —
  never into the member's own drawings. A parallel line through the highs (a
  channel) is a cheap next step and gives the iron condor its upper edge.

### 6e. Risk / reward (payoff) chart (new component)

- **At expiry**: the P&L line across stock prices, built generically from the legs
  (strike, call/put, buy/sell, quantity, price) — one piece of code for all ten
  strategies.
- **Today's line** (T+0) from Black-Scholes at current IV (`app/services/black_scholes.py`
  exists). Essential for the time strategies: a calendar's or diagonal's expiry payoff is
  meaningless, so their chart is drawn at the front leg's expiry with the back leg
  valued by the model.
- **Marked**: current price, breakeven(s), max profit, max loss, POP, and — tying it to
  the chart — the setup's **stop and target** and the **support / resistance / trend
  line** as vertical markers, so a member sees that the short strike sits under support
  and that the stop is hit long before the max loss ("stop 336.2 · you'd lose about $121" vs
  max loss $790). Both stops - the chart stop and the family's rule stop - are drawn for
  EVERY strategy (II.2.9).
- **Units toggle**: $ per contract or R-multiples.
- **Where**: under the strike picker on the card (redraws when another candidate is
  clicked) and on the Positions page with a dot for the open trade's current P&L.
- Palette (validated for CVD, light + dark): expiry line teal `#1D9E75`, today line
  purple `#7F77DD` dashed, profit zone teal-50, loss zone coral-50, text in text tokens.

---

## 7. UI — one page, three zones (mocked up 2026-10-03)

```
┌ Options · 5 tickers ───────────────────── Data as of Oct 2, 16:00 ET · delayed  [Refresh] [Live (TWS)] ┐
│ BASKET (left)              │ TICKER CARD (centre)                                                       │
│ ticker  trend  IV   idea   │ LRCX · 349.20 · ATR 11.54                     earnings Oct 22 · inside expiry│
│ LRCX    ↗     62  sell put │ "Uptrend: EMA 20 above 50 above 200 for 34 days, and price is riding a     │
│ MA      ↗     55  sell put │  trend line with 3 touches. It bounced off support at 340 on high volume.  │
│ ISRG    ↗     41 bull call │  Options are expensive (IV rank 62), so you're paid to sell a put spread   │
│ NVDA    ↔     57  condor   │  below that support."                                                      │
│ KO      ↘     24  buy put  │ [✓ Bull put spread] [Bull call spread · also fits] [Buy call · expensive]  │
│ [+ Add ticker]             │ PRICE CHART: candles · EMA 20/50/200 · trend line (3 touches) · support    │
│                            │              340 · 330 short · 320 long                                    │
│                            │ STRIKES under your rules (delta 0.20–0.30, 30–60 d, under support + line)  │
│                            │   Nov 20 330/320 put  collect $210  risk $790  chance 75%   ← best return │
│                            │   Nov 20 325/315 put  collect $317  risk $683  chance 71%   safer         │
│                            │   Nov 20 335/325 put  collect $270  risk $730  chance 68%                 │
│                            │ RISK & REWARD: expiry line · today line · breakeven 327.90 · max +$210 /   │
│                            │   −$790 · now 349 · stop 336.2 (you'd lose ≈ $121) · support 340   [$|R]  │
│                            │ [Order ticket] [Track this]                         Show full chain ▾     │
├────────────────────────────┴────────────────────────────────────────────────────────────────────────────┤
│ MY RULES  tabs: Shared · Credit spreads · Buy call/put · Iron condor · Time spreads        [Reset]      │
│   short strike delta 0.20–0.30 (≈ 70–80% chance it expires worthless) · DTE 30–60 · width $10 ·        │
│   min credit 25% of width · sell only when IV rank ≥ 30 · short strike must be under support + line    │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

- **IV number colour** in the basket: amber = sell premium, grey = neutral, teal = buy.
- **The headline sentence** does the work: trend + evidence, setup, IV regime, conclusion,
  in plain words. No jargon menu; rejected strategies are greyed with the reason.
- **Strike picker wording**: "you collect / you risk / chance of keeping it" (delta,
  credit and max loss translated).
- **Order ticket** = strategy, strikes, expiry, limit price, the conditional trigger on
  the underlying, the stop level — paste into TWS / moomoo (conditional-order mechanics
  for both were worked out in the same discussion).
- **The full option chain** sits behind an expander; nobody sees a raw chain by default.
- **My rules**: shared block + one tab per strategy family, house defaults pre-filled
  with a one-line translation per rule; untouched tabs still work.
- **Status strip** = the honesty line: data age, delayed or live, refresh buttons.
- This page **replaces** IV Rank (`/ivscan`), Spread (`/spreads`) and Positions
  (`/portfolio`) — all three were taken off the nav in v4.126 (user: "disable the
  Company, macro, options"); their engines move inside it.

---

## 8. Exists today vs new

| Exists today (reuse) | New |
|---|---|
| Cboe delayed chain with full greeks, server-side (`option_quotes.py`) | chain **snapshot store** + nightly Hermes job for the basket |
| IBKR bridge: live greeks, 1-yr IV → rank/percentile, TWS scanner | **HV** + server-side IV rank from the growing `iv_history`; IV per expiry |
| `iv_history` (daily IV30, 370-day), `spread_candidates`, `option_spreads` + management monitor (50% / 20% / 21 DTE; delta 0.35 adjust / 0.40 close) | `option_basket`, `iv_daily`, `option_signal`, `user_option_prefs` |
| trend / setup detection: EMA stack, support bounce (v4.124/125), EMA rebound, structure HH/HL, weekly setup | **strategy recommender** rule table; **sideways / range** verdict; **trend-line engine** |
| `bull_put.select` / `rank_pairs` (delta band, liquidity, credit, sizing from NLV) | **generic strike picker** driven by per-member prefs, for all ten strategies |
| chart with EMA 20/50/200 (D + W), strike overlay (`chart_spread`), support overlay (`chart_bounce`); Positions page; Telegram alert path; `black_scholes.py` | **one consolidated Options page**, **payoff chart**, "My rules" drawer, order ticket; optional Telegram push + paper auto-track |

---

## 9. Build order (proposed)

1. **Bull put spread + bear call spread** + the **payoff chart** + the page skeleton
   (basket → nightly data → card → track). Engine exists; mirror it. Payoff chart is
   cheap and every strategy needs it.
2. **Buy call / buy put / bull call / bear put** (the debit family) + the **trend-line
   engine** ("buy a call at the trend line" is its natural first use) + chart-based exits.
3. **Iron condor** — needs the sideways / range detector.
4. **Calendar, diagonal, LEAPS** — needs term structure and long-dated expiries in the store.
5. Later: covered call / cash-secured put; Telegram push; paper auto-track; news line.

---

## 10. Open decisions (unanswered as of 2026-10-03)

From the mindmap turn:
1. **Data source of record**: Cboe delayed (free, working, undocumented) as primary with
   Alpaca as the contracted fallback behind the same interface — or Alpaca primary from
   day one? (My pick: Cboe primary, Alpaca wired as a config switch.)
2. **Catalog scope for v1**: the user later gave the full list of ten; the build order
   in §9 phases it. Confirm the phasing.
3. **House defaults**: a default rule set maintained by the admin that members start
   from — yes/no? (My pick: yes.)
4. **IV rank source**: the bridge's 1-year IBKR series is exact but needs TWS; the
   server-side one needs ~60 trading days of `iv_history` before the rank means anything
   (accumulating since 2026-09-10). Accept "rank after ~3 months, percentile earlier", or
   bootstrap each ticker's history from IBKR once via the bridge?
5. **Replace or add**: the new page replaces IV Rank / Spread / Positions outright, or sits
   alongside for a while?
6. **LLM**: none in the decision path (recommendations deterministic and explainable);
   optional later: a plain-English explainer paragraph per ticker. Agree?

From the mockup turn:
7. **The stop on the payoff chart**: drawn as the stock-level stop from the setup (e.g.
   336.2, under the bounce low) — for a credit spread that is tighter than the engine's
   "20% of max loss" rule. Show both lines, or only the one the member picks in rules?
8. **The "today" curve**: keep it with a one-line caption ("what the trade is worth if
   the stock moves tomorrow"), or hide it behind the full-chain expander?
9. **Wording**: is "chance of keeping it" the right way to say POP for the members? Should
   the chip row show rejected strategies at all, or only the recommended one with a "see
   other strategies" link?
10. **Automation level**: see-and-approve by default; add the Telegram push and the paper
    auto-track to v1, or later?

---

---

## Part II — full design (reconciled 2026-10-04)

The buildable spec. A developer builds from II.2-II.4 without re-reading §1-§10 or the four
parts; the parts (`design/options/part_A_data.md` data + nightly, `part_B_engines.md` engines,
`part_C_chart_engines.md` trend line / range / payoff, `part_D_ui.md` page) are the long-form
reference for the *why* and the worked examples. Where a part and this Part II disagree, Part II
wins (the integrator calls are listed in II.5).

### II.1 Status + how to resume

| Item | State (2026-10-04) |
|---|---|
| The four parts | reconciled against `CRITIQUE.md` and the unified contract (II.2): every blocker and major applied, the minors folded into the wording tables; residual spelling differences resolved in II.5; the verifier's second pass (2026-10-04) produced the integrator rulings R1-R13 (II.5 #37-49) and the golden fixtures (II.2.19) - the parts are corrected to them |
| The critique | applied; `CRITIQUE.md` is kept as the record of what was changed and why |
| What is built | **nothing** - no code, no migration, no templates, no `dashboard_tst/tests/` tree |
| Repo state the build starts from | app version 4.126; alembic head `e2f3a4b5c6d7` (iv_scan_items); bridge `TradeHunterIBKRBridge/1.5`; nav items `ivscan`, `spreads`, `positions` hidden (`menus.HIDDEN_KEYS`) |
| Next action | build step 1 (II.4), on the laptop, in the order II.4 gives |

How to resume on any PC:

1. Read II.2 (the contract), II.3 (which file does what), then the II.4 step being built.
2. Open the part a file belongs to only for the detail II.2 does not carry (algorithms,
   worked numbers, wording tables): A1-A8, B0-B9, C1-C6, D1-D8.
3. Check `git log` for a `dashboard_tst v4.127` commit: if present, step 1 has started - read
   `README.md`'s changelog entry and `dashboard_tst/tests/` before touching anything.
4. Never re-open a decision in II.5; they were locked on 2026-10-03 with the user's go-ahead
   and reconciled on 2026-10-04.

### II.2 The unified contract (binding on every part)

#### II.2.1 Migration and the nine tables

ONE file `alembic/versions/f4a5b6c7d8e9_options_module.py`, `revision = "f4a5b6c7d8e9"`,
`down_revision = "e2f3a4b5c6d7"`, per-table `get_table_names()` guard (the Hermes DB may have
run `create_all`), creation order below, reversed on downgrade. One data step: copy every OPEN
`option_spreads` row into `option_trades` (`strategy="bull_put"`, `family="credit_vertical"`,
two put legs with `entry_price/entry_delta/entry_iv`, `net_entry = -credit`, overrides carried,
`note="migrated from option_spreads #<id>"`), run only in the branch that just created
`option_trades` (re-runnable, never duplicates), SQLAlchemy Core only (no raw SQL, no
`app.models` import). `init_db()` runs `upgrade head` at startup, so Hermes needs no manual step.
The ids `f0a1b2c3d4e5` / `f9a0b1c2d3e4` and the tables `option_positions`,
`option_position_legs`, `option_job_runs` do not exist.

| # | Table (model) | Purpose | Key / notes | Shape owner |
|---|---|---|---|---|
| 1 | `option_basket` (`OptionBasket`) | the tickers a member (or the system) studies | `UNIQUE(owner_key, symbol)`; `owner_key = "u<id>"` or `"system"`; II.2.18 | A2.1 + D's `pos` |
| 2 | `option_chain_snapshot` (`OptionChainSnapshot`) | one contract row per snapshot, all expiries, `kind in {eod, intraday}` - never live | replaced per `(symbol, snap_on, kind)`; `iv` FRACTION; `oi`/`volume` None = unknown | A2.1 |
| 3 | `iv_daily` (`IVDaily`) | one row per symbol per ET day: chain header + every derived vol statistic (PERCENT) | `UNIQUE(symbol, on)`; `kind in {eod, intraday, history}`; `source in {cboe, alpaca, ibkr, iv_history}` | A2.1 |
| 4 | `option_signal` (`OptionSignal`) | the card cache the page reads | `UNIQUE(symbol, snap_on, kind, prefs_hash)`; `trend` String; `headline` Text; `setup/iv/strategies/picks` JSON; `status in {ok, no_setup, no_chain, no_iv, stale_iv, error}`; `engine_version` | A2.1 (shapes II.2.6) |
| 5 | `user_option_prefs` (`UserOptionPrefs`) | a member's sparse rule overrides | `user_id` unique; `prefs` JSON; `prefs_hash` String(16) (holds the first 12 hex); `schema_version`; `updated_at`; `User` gets the one-to-one relationship `option_prefs` (`UserOptionPrefs.user` backref, `uselist=False`) declared in `models.py` | A2.1 / B4.1 |
| 6 | `option_jobs` (`OptionJob`) | one row per nightly / refresh / bootstrap / backfill / telegram_poll run | `job`, `run_on`, `source`, `started_at`, `finished_at`, `symbols`, `ok`, `errors`, `rows`, `pushed`, `detail` JSON, `note` | A2.1 + D's `pushed` |
| 7 | `option_trades` (`OptionTrade`) | THE positions store, every strategy, generic legs | B7.1 columns + the portable JSON column `meta` (II.2.2); the migration creates `meta` with the table | B7.1 |
| 8 | `option_trade_checks` (`OptionTradeCheck`) | one grading row per (trade, ET day) | `UNIQUE(trade_id, checked_on)`; `legs` JSON per-day `{mid, delta, iv}`; `state in {OK, WATCH, ROLL, CLOSE, TAKE, UNKNOWN, EXPIRED}`; `urgent` | B7.1 |
| 9 | `option_idea_push` (`OptionIdeaPush`) | the Telegram dedupe / opt-in record | `UNIQUE(user_id, idea_key)`; `idea_key = "SYM|strategy|front_expiry"`; `short_strike`, `atr`, `score`, `sent_at`, `ok`, `error` | D1.12 |

Retention (`option_store.prune`, end of every nightly run): snapshots 90 EOD days
(`TST_OPTIONS_SNAPSHOT_DAYS`), thinned after 7 days (`TST_OPTIONS_FULL_DAYS`: drop rows with
`delta` None or `|delta| < 0.03` or `> 0.97`; expiries are never thinned), intraday rows of
past days, contracts expired > 7 days; `option_signal` 90 days; `option_trade_checks` 90 days
(rows older than 90 days; the trade row itself is never pruned); `option_jobs` newest 180;
`option_idea_push` 45 days; `iv_daily` never. `prune(db, today)` is step 5 of the nightly order
(II.2.16), after `option_exits.sweep` and before the Telegram push.

#### II.2.2 Positions store

| Rule | Value |
|---|---|
| Store | `option_trades` + `option_trade_checks` for EVERY strategy from step 1 |
| Grader | `services/option_exits.py`: `mark(trade, chain_view, today)`, `grade(trade, snap, chart, prefs, *, earnings=None)`, `sweep(db)` - generic legs, the RIGHT comes from the stored leg (a bear call marks against the call chain) |
| `OptionTrade` columns | `user_id, symbol, strategy, family, legs JSON, front_expiry, back_expiry, net_entry (per share: negative = credit), contracts, max_loss (+$ per contract), chart_stop, chart_target, roll_dte, paper, signal_id, earnings_date_at_entry, roll_delta, loss_stop_pct, profit_target_pct, dte_floor (NULL = the member's default), meta JSON, opened_at, status, closed_at, close_reason, note` |
| `OptionTrade.meta` | a portable JSON column (SQLAlchemy `JSON`, created by the migration with the table) holding whatever the exit rules need at entry and nothing else has a column for: a calendar's entry breakevens, a condor's range edges (`rng.low - pad` / `rng.high + pad`), a diagonal's short-leg roll data; `{}` for a credit vertical; written once by `POST /options/track-idea` (the `option_spreads` copy step writes `{}`), read by `option_exits.grade` |
| `OptionTrade.legs` | the Leg shape (II.2.8) + `entry_price, entry_delta, entry_iv` per leg |
| `OptionTradeCheck` columns | `trade_id, checked_on, spot, mark, pl, loss_pct, profit_pct, dte, back_dte, net_delta, theta, vega, legs JSON {mid, delta, iv}, state, action, reasons JSON, urgent, source, error, created_at` |
| Legacy | `option_spreads` stays READ-ONLY for the legacy `/portfolio` until removal; `spread_monitor.py` and `bull_put.py` are NOT changed; the migration copies its OPEN rows once |
| UI | Positions tab renders `option_trades` rows with an `OptionTradeCheck` drawer; `POST /options/positions/{id}/close`; `/options/badge` counts `urgent` / `watch` from `option_trade_checks` |
| Create | `POST /options/track-idea` (pick re-read from the signal; first check written from the stored chain on the spot) |

#### II.2.3 Prefs: module, blocks, hash

ONE module `app/services/option_prefs.py`. `SCHEMA = {block: {field: Field(default, lo, hi,
kind, label, help, plain, step, unit)}}` - the engines read `default/lo/hi/kind`, the drawer
renders `label/help/plain/step/unit`; `FIELDS` is the same table (no second one). `kind in {num,
int, bool, choice}`.

| Block | Strategies | Fields (house default) |
|---|---|---|
| `shared` | all | `min_oi` 500 · `oi_per_contract` 10 · `max_leg_spread` 0.50 $ · `min_leg_volume` 20 (warning only) · `earnings_rule` `none_inside` (choice: `none_inside` / `defined_risk_only`, NO `allowed`) · `monthly_only` False · `chart_constraint` True (safety switch) · `gap_mult` 2.0 (`GAP_MULT`) |
| `credit_vertical` | bull_put, bear_call | `short_delta_lo/hi` 0.20/0.30 · `width_atr_lo/hi` 0.5/1.5 ATR ($ shown beside) · `long_offset_max` 3 · `credit_pct_min` 25 · `dte_lo/hi` 30/60 · `iv_gate_min` 30 |
| `debit_vertical` | bull_call, bear_put | `long_delta_lo/hi` 0.60/0.70 · `short_delta_lo/hi` 0.25/0.35 (soft - the chart target decides) · `reward_cost_min` 1.0 · `dte_lo/hi` 30/60 (rule stop = the `long` block's `premium_stop_pct`) |
| `long` | buy_call, buy_put | `delta_lo/hi` 0.60/0.70 · `theta_pct_max` 1.0 %/day · `dte_lo/hi` 45/90 · `premium_stop_pct` 50 |
| `leaps` | leaps_call | `delta_lo/hi` 0.70/0.80 · `extrinsic_pct_max` 10 (% of the STOCK price) · `months_lo/hi` 9/18 · `roll_dte` 180 · `delta_floor` 0.55 · `premium_stop_pct` **40** (the diagonal's long leg inherits it) |
| `condor` | iron_condor | `short_delta_lo/hi` 0.15/0.20 · `wing_atr_lo/hi` 0.5/1.5 ATR · `credit_pct_min` 30 · `dte_lo/hi` 30/45 · `roll_delta` 0.30 · `loss_stop_pct_credit` 100 |
| `time` | calendar, diagonal_call | `cal_front_lo/hi` 20/30 · `cal_back_lo/hi` 50/70 · `cal_delta_tol` 0.05 · `cal_take_pct` 25 · `diag_long_delta_lo/hi` 0.70/0.80 · `diag_long_dte_lo/hi` 180/365 · `diag_short_delta_lo/hi` 0.20/0.30 · `diag_short_dte_lo/hi` 30/45 |

| Rule | Value |
|---|---|
| Not fields (constants, `opt_constants.py`) | `STOP_ATR 1.0`, `TARGET_R 2.0`, `LEVEL_PAD_ATR 0.25`, `MAX_POSITION_PCT 10.0` (CLAUDE.md: global, never override), `RISK_FREE 0.04`, `STOP_IV_BUMP 0.10`, `STOP_TIMES (0, 0.5)`, `SLOW_DRIFT_ATR 0.75`, `IV_MIN_OBS 20`, `IV_RANK_MIN_OBS 60`, `IV_FULL_OBS 252` |
| Not in SCHEMA | `nlv`, `risk_pct` and the four credit exit lines (`loss_fraction`, `profit_target`, `dte_floor`, `roll_delta`) live in `trade_prefs` and are edited on the Shared / Credit tabs through `trade_prefs.write`; house values `loss_fraction` 0.20 · `profit_target` 0.50 · `dte_floor` 21 · `roll_delta` **0.35 adjust / 0.40 close** (B7.3's 0.30 is a member-override example, not the default); the Telegram settings live in `prefs["telegram"]` (II.2.15) - its OWN top-level key, never a SCHEMA field, never hashed; `max_position_pct` is NOT a field (`MAX_POSITION_PCT 10.0` is the constant above) |
| Drawer tabs (`TABS`) | `shared` \| `credit` (= `credit_vertical`) \| `debit` (= `debit_vertical` + `long` + `leaps`, three sub-sections collapsed on `< lg`, translation line first, fields under "change these") \| `condor` \| `time` |
| Functions | `read(db, user)` (merge over `HOUSE`, bad / out-of-range -> default), `clean(raw)` (the pure merge; `HOUSE_HASH = prefs_hash(clean({}))`), `write(db, user, tab, form)` (stores only values != default, reports out-of-range, recomputes `prefs_hash`), `reset(db, user, tab)`, `for_strategy(prefs, key)`, `family_of(key)`, `defined_risk(key)` (True ONLY for `bull_put, bear_call, bull_call, bear_put, iron_condor` - II.2.7), `prefs_hash(merged)`, `distinct_hashes(db)`; `STRATEGY_KEYS` are imported from `strategy_rules` (II.2.7), never redefined here |
| `read()` returns | the merged blocks (`shared`, `credit_vertical`, `debit_vertical`, `long`, `leaps`, `condor`, `time`) PLUS three top-level keys that are not blocks and never hashed: `telegram` = `{enabled, chat_id, verified, quiet, paused_until, pending{chat_id, code, expires}}`, `account` = `{nlv, risk_pct, nlv_source}` (from `trade_prefs`; `nlv_source in {live, prefs, None}`), `_overridden` = the set of dotted field names the member changed (`"credit_vertical.short_delta_hi"`), which the drawer turns into override dots |
| `prefs_hash` | first 12 hex of `sha1(canonical_json({block: {k: merged[block][k] for k in PICK_FIELDS[block]}}))`, sorted keys, floats to 4 dp. `PICK_FIELDS` = delta bands, DTE, widths (`width_atr_*`, `wing_atr_*`), credit / reward floors, liquidity (`min_oi`, `oi_per_contract`, `max_leg_spread`, `min_leg_volume`), `earnings_rule`, `monthly_only`, `chart_constraint` - NEVER `nlv`, `risk_pct`, `gap_mult`, any exit line (`premium_stop_pct`, `roll_dte`, `delta_floor`, `roll_delta`, `loss_stop_pct_credit`, `cal_take_pct`), the `trade_prefs` lines or `telegram` |
| Sharing | members on house defaults share ONE signal row per symbol (the house hash); the nightly job writes every DISTINCT saved hash |
| Safety switches | `chart_constraint` off / `earnings_rule = defined_risk_only`: the amber consequence sentence shows before Save; the override dot is rose, not amber |

#### II.2.4 Sizing

ONE function `option_sizing.size(pick, nlv, prefs) -> dict`, run at READ time (card, picks,
ticket, Live); never by the nightly job, never by a template; D never recomputes a quantity.

```
risk_budget      = nlv x risk_pct / 100
loss_at_stop_usd = max(0, -pick.chart_stop_pl)          # computed at WRITE time by the picker (II.2.14)
max_loss_usd     = pick.max_loss                        # positive, per contract
by_chart_stop    = floor(risk_budget / loss_at_stop_usd)             # None when loss_at_stop_usd == 0
by_gap           = floor(risk_budget x GAP_MULT / max_loss_usd)      # GAP_MULT = prefs.shared.gap_mult (house 2.0)
by_notional      = floor(nlv x MAX_POSITION_PCT/100 / max_loss_usd)  # the 10% cap, a constant
contracts        = min(x for x in (by_chart_stop, by_gap, by_notional) if x is not None)
```

| Rule | Value |
|---|---|
| Rounding | `floor`, never round up; **never `max(1, ...)`**; 0 is a valid answer with the note "Not even one contract fits your 1% - lower the risk or choose a narrower spread" |
| NLV order | (1) the Live figure for THIS request (`POST /options/live/{symbol}` payload `nlv`, `nlv_source = "live"`, labelled "from TWS · [remember this]") -> (2) stored `trade_prefs.read(user)["nlv"]` when > 0 (`"prefs"`) -> (3) None -> `contracts = None` + "sized once you tell us the account value (My rules -> Shared)". Never auto-written: only the explicit "[remember this]" click posts `tab=shared&nlv=` to `POST /options/rules` |
| The card's line | ALWAYS both figures: "{n} contracts: about ${loss_at_stop} if the stop fires, up to ${max_loss_total} ({pct}% of your account) if the stock gaps past it" |
| Telegram | never states a contract count |

The dict (the golden LRCX pick of II.2.19 - the Nov 20 330/320 at 2.10 - under house prefs: NLV
100,000, risk 1%, `GAP_MULT` 2.0, `MAX_POSITION_PCT` 10; `stop_iv 0.506 = 0.46 x (1 + STOP_IV_BUMP
0.10)`, the RELATIVE lift of II.2.9):

```json
{"nlv": 100000.0, "nlv_source": "live|prefs|null", "risk_pct": 1.0, "risk_budget": 1000.0, "gap_mult": 2.0,
 "loss_at_stop_usd": 120.7, "stop_price": 336.2, "stop_t_days": 0, "stop_iv": 0.506,
 "rule_stop_usd": 158.0, "rule_stop_kind": "20% of max loss", "fires_first": "chart",
 "by_chart_stop": 8, "by_gap": 2, "by_notional": 10, "contracts": 2,
 "capital_at_risk_usd": 241.4, "max_loss_total_usd": 1580.0, "max_loss_pct_nlv": 1.6,
 "line": "2 contracts: about $242 if the stop fires, up to $1,580 (1.6% of your account) if the stock gaps past it",
 "note": null}
```

#### II.2.5 Order ticket rules

`order_ticket.build(pick, setup, prefs, *, dip=False, rejection=None, now=None) -> Ticket`;
`order_ticket.render(ticket, broker in {"tws", "moomoo"}) -> str`. `_options_ticket.html`
prints BOTH renderings in `<pre>` plus the footer lines; it holds no ticket text of its own.

| Rule | Value |
|---|---|
| Entry | by default NO condition: a DAY limit order placed while the market is open (limit = mid, work down 0.05 at a time, floor = the worst net still inside `credit_pct_min` / `reward_cost_min`) |
| "Enter on the dip" | an explicit toggle (`?dip=1`); condition `last <= level x (1 + offset_pct/100)` (341.92 on the LRCX fixture) only where a dip level exists (B6.1 table); printed WITH: "This order will also fire if {SYM} crashes through {price} on bad news. Only use it while you are watching." |
| The ONE conditional order pushed hard | the chart-stop EXIT (GTC): trigger `{SYM} last <= {plan.stop}` (bull side; `>=` mirror), with "This order fires on the live price during regular hours, so it can fire on an intraday dip the close would have survived. If you prefer the close-based rule, leave this order off and act on the Positions tab's verdict instead." and "Trigger outside RTH: No" in BOTH renderings |
| TWS combo stop | Market recommended; Limit (the model's mark at the stop) secondary with "may not fill in a fast market" |
| moomoo stop | two orders in a fixed order: (1) BUY TO CLOSE the SHORT leg first - market, or limit = model x 1.15; (2) then SELL TO CLOSE the LONG leg; plus "Never sell the long leg before the short leg is closed - you would be short a naked put." ("naked call" for a call spread; once per side for a condor). Same ordering for TWS when the combo cannot be conditional |
| Take profit | GTC limit at the family's rule (credit: half the credit); rule stop and time stop printed as "no order - the Positions tab watches it" |
| Header, verbatim | "Prices are from {as_of}. Press Refresh after 21:30 Malaysia time (US open) and re-open the ticket before sending; the credit will have moved." |
| "Refresh first" | when `as_of` is older than the last session close AND `_us_session_open()` is True: an amber banner with the Refresh button inline; the 60 s refresh cooldown never blocks that first refresh. `_us_session_open()` lives in `app/services/clock.py`: weekdays 09:30-16:00 ET, excluding the NYSE holidays the calendar service already knows (falling back to a static holiday list when it does not); `_older_than_last_close` uses the same helper for its "last session" step |
| Golden text | the TWS and moomoo renderings of the golden LRCX ticket are ONE text (B9's), II.2.19: 2 contracts, the jargon line below, the stop line with both figures, "no order - the Positions tab watches it" on the rule and time stops |
| Rejected strategy | `reason_key = earnings_inside` (and not `defined_risk_only` on a defined-risk strategy): strike table hidden entirely; `build()` raises `TicketRefused`; the panel renders the one line "No ticket: earnings {date} fall inside this trade and your rule says no." - any other rejection: strikes shown, the button is "Order ticket (not recommended)" in the ghost style, the rejection sentence is line 1 of both renderings and the tracked trade's `note` |
| Unbuilt strategy | never `recommended`; `also_fits` with the chip "{label} · not available yet"; no picks, no ticket; the words "step N" / "phase" never reach a member |
| Footer (D's lines) | the sizing line (both figures); "You collect $a-$b (worst likely fill to mid) · you risk $m · breakeven x · {earnings line}"; "Tracking only: TradeHunter never sends an order." |
| Jargon | "(you are paid; the most you can lose is fixed)", "worst likely fill 2.00" - never "defined risk", "natural" |

#### II.2.6 `option_signal` - the row and its JSON shapes

Keyed `(symbol, snap_on, kind, prefs_hash)`. The ONLY read path is
`option_store.card_for(db, symbol, user) -> dict | None` (latest row for the member's hash,
else the house hash; lazy compute from the stored chain on a hash miss or `status = stale_iv`,
no market call; fills `sizing` at read time; adds `stale`, `age_h`, `kind`, `as_of`) and
`option_store.basket_rows_for(db, user) -> dict[symbol, dict]` (ONE batched query keyed by symbol;
per symbol `{symbol, trend, iv: {iv_rank, basis, iv_n}, idea, pick_state, stale, age_h}`; it is
`basket_rows_for` that decides `pick_state in {has_picks, no_strike_passes, not_checked}` - the
route never recomputes it). `stale` = `snap_on` older than the previous ET trading day
(`spread_monitor.et_today()`), never a wall-clock age. `trend` is a String in `{up, down,
sideways, unclear}` (`sideways` iff `setup.rng.sideways`). `headline` is composed at WRITE time by
`option_words.headline(setup, iv, strategies) -> str` (one spelling; B's `headline(chart, gauge,
strategies)` is this function). The row is produced by `option_engine.compute(chain, metrics,
state, prefs) -> {status, headline, setup, iv, strategies, picks, computed_ms, engine_version}`.
`engine_version` is `option_engine.ENGINE_VERSION`; an older row is treated as missing.

`setup` (B2.7; `sup / tl / tl_bounce / rng` are Part C's dicts verbatim as `ema_setup.analyze()`
stores them; `chart_state.read()` calls `analyze()` ONCE and never re-runs a detector; `plan` is
the ONE source of the chart stop, `stop` / `target` are flat aliases of `plan.stop` /
`plan.target`; the golden LRCX fixture of II.2.19: ATR 11.54, zone low 339.1 -> stop 336.2, target
375.2; `quality` is an int 0-100, `tl.warning` a bool, `rng.pos_pct` 0..1):

```json
{"kind": "support_bounce", "direction": "long", "level": 340.9, "zone": [339.1, 342.0],
 "touches": 3, "quality": 80, "close": 349.2, "trend_days": 34, "atr": 11.54,
 "ema": {"e20": 346.1, "e50": 335.8, "e200": 301.2},
 "plan": {"entry": 349.2, "stop": 336.2, "target": 375.2, "r": 13.0}, "stop": 336.2, "target": 375.2,
 "levels": {"support": 340.9, "resistance": 372.0},
 "sup": {"...": "support_bounce.find() dict verbatim"},
 "tl": {"direction": "up", "p1": {"time": "2026-07-08", "price": 318.6}, "p2": {"time": "2026-09-12", "price": 334.1},
        "i1": 188, "slope_per_bar": 0.33, "slope_atr": 0.03,
        "touches": [{"time": "2026-07-08", "price": 318.6}, {"time": "2026-08-14", "price": 326.9}, {"time": "2026-09-12", "price": 334.1}],
        "n_touches": 3, "span_bars": 61, "value_today": 338.4,
        "value_at": {"2026-10-17": 339.8, "2026-11-20": 341.9, "2026-12-19": 343.8},
        "broken": false, "last_break": null, "warning": false, "residual_atr": 0.2, "atr": 11.54, "channel": null},
 "tl_bounce": null,
 "rng": {"low": 318.6, "high": 372.0, "zone_low": [317.1, 320.4], "zone_high": [370.2, 373.9],
         "n_low": 2, "n_high": 2, "touches_low": [{"time": "...", "price": 0, "kind": "low"}], "touches_high": [],
         "width_atr": 4.6, "pos_pct": 0.57, "stack_flat": false, "sideways": false,
         "reasons": ["EMA stack rising", "EMA20 moved 1.4 ATR in 10 bars"]},
 "evidence": ["EMA20 > EMA50 > EMA200 for 34 sessions", "bounce candle 2026-10-01 on 1.7x volume"]}
```

`iv` (B1.4's gauge dict verbatim + A's informational `iv30_src`, `atm_iv30`, `lo`, `hi`; every
vol statistic in PERCENT; `iv_hv_premium` is the unitless RATIO `iv30 / hv20` - never vol points -
with `IV_HV_RICH 1.10` / `IV_HV_CHEAP 0.90`; the numbers are the golden LRCX fixture, II.2.19):

```json
{"iv30": 46.0, "hv20": 38.0, "hv60": 36.1, "iv_hv_premium": 1.21,
 "iv_rank": 62.0, "iv_pct": 71.0, "iv_n": 252, "state": "ok", "basis": "rank", "provisional": false,
 "iv_front": 50.0, "iv_back": 45.0, "term_ratio": 1.11, "skew25": 4.1, "skew_norm": 0.08,
 "expected_move": 24.1, "earnings_date": "2026-10-22", "earnings_days": 19,
 "verdict": "SELL",
 "verdict_why": "IV rank 62 (>= 50) and priced for 21% more movement than the stock has actually shown",
 "gates": {"buy": false, "sell_directional": true, "sell_neutral": true, "mid": false},
 "iv30_src": "cboe", "atm_iv30": 45.6, "lo": 28.0, "hi": 57.0}
```

(`iv_front` 50.0 is the Oct 31 expiry - the one nearest 30 DTE with `dte >= 7`; `iv_back` 45.0 is
Dec 19 - the one nearest 75 DTE with `dte >= 45`; `lo` / `hi` 28.0 / 57.0 put 46.0 at rank 62.)

| `iv` field | Values / rule |
|---|---|
| `state` | `none` (n = 0) · `forming` (0 < n < 20) · `pct_only` (20 <= n < 60) · `rank_ok` (60 <= n < 252) · `ok` (n >= 252) |
| `basis` | `rank` (state ok) · `percentile` (rank_ok, pct_only) · `provisional` (forming with HV20: the verdict rests on IV vs HV alone, gates all False) · `unknown` (none, or forming without HV) |
| `provisional` | `basis in (provisional, unknown)` - never pushed to Telegram, never coloured amber |
| `iv_front` / `iv_back` | the ATM IV of the expiry nearest 30 DTE with `dte >= 7` / nearest 75 DTE with `dte >= 45` (A3.4; "60-90 DTE" in any part means this rule) |
| `term_ratio` | `iv_front / iv_back`; `TERM_EVENT 1.05` (an event is priced), `TERM_CONTANGO 0.95`; `term_slope` does not exist; the term reason lives in `verdict_why` - `term_event` is NOT a gate |
| `gates` | exactly `{buy, sell_directional, sell_neutral, mid}` (bools); the recommender reads `gates["mid"]` for `mid_or_buy` |
| `verdict` | `SELL` (measure >= 50) · `NEUTRAL` (30-50; IV/HV ratio >= 1.10 -> SELL, <= 0.90 -> BUY) · `BUY` (< 30) · `UNKNOWN` |
| Wording when `basis != rank` | carries the day count: "Options look expensive against the last 34 days (not a full year yet)"; basket cell `~62` dotted-underlined, grey, never amber; `unknown`: cell `-` and the ONE string (no second variant anywhere): "We cannot yet say whether options are expensive - {n} of 60 days of history. If you have TWS on this PC, press Live to load a year." (`{n}` = `iv_n`, 12 on the example) |

`strategies` - a flat list of ALL TEN rows, ordered recommended -> also_fits -> rejected:

```json
[{"key": "bull_put", "label": "Bull put spread", "fit": "recommended", "score": 90.1, "step": 1,
  "why": "Uptrend for 34 days. It bounced off support at 340 on high volume. Options are expensive (IV rank 62 over the last year), so you are paid to sell a put spread below that support.",
  "must_happen": "LRCX stays above 330 until Nov 20. You keep the credit if it does nothing, drifts up, or even dips a little.",
  "reasons": [], "reason_key": null, "shown": true},
 {"key": "leaps_call", "label": "Buy LEAPS", "fit": "also_fits", "score": 61.0, "step": 4, "why": "...", "must_happen": "...",
  "reasons": ["not available yet"], "reason_key": "not_available_yet", "shown": true},
 {"key": "buy_call", "label": "Buy call", "fit": "rejected", "score": null, "step": 2, "why": null, "must_happen": null,
  "reasons": ["options too expensive to buy (IV rank 62)"], "reason_key": "expensive", "shown": true},
 {"...": "the other seven rows in the same shape - all TEN are always stored"}]
```

| Field | Values |
|---|---|
| `fit` | `recommended` (at most one, and only a BUILT rule) · `also_fits` · `rejected` |
| a `rejected` row | `score` null, `why` null, `must_happen` null - only `reasons` / `reason_key` carry text; `label` for `leaps_call` is "Buy LEAPS" (never "Buy LEAPS call") |
| `score` | `priority + iv_fit (0-20) + setup_quality / 5 + term bonus 10 + structure bonus 5 - 10 if unbuilt` (90.1 for `bull_put` on the fixture); ties break in `STRATEGY_KEYS` order (II.2.7) |
| `shown` | True on the <= 2 near-miss rejects (single-fail rows, catalog order); recommended / also_fits chips always render |
| `reason_key` (fixed vocabulary) | `expensive` · `cheap_options` · `not_rich_enough` · `trending_not_sideways` · `no_range` · `wrong_direction` · `no_setup` · `earnings_inside` · `front_iv_under_back` · `no_long_dated` · `no_weekly_trend` · `not_available_yet` (null when not rejected) |
| chip text per key (`option_words`) | expensive · cheap options · premium not rich enough · trending, not sideways · no range · wrong direction · no setup · earnings inside · near-term not dearer · no 9-18 month options stored · no weekly trend · not available yet |
| `step` | the build phase of the rule (1-4); `step > CURRENT_STEP` -> `also_fits` + `not_available_yet`, never `recommended`, no picks |

`picks = {strategy_key: [Pick]}` - top 3 by score for every recommended / also_fits rule with
`step <= CURRENT_STEP`, computed per DISTINCT saved `prefs_hash`; when nothing passes, ONE
Pick-shaped stub with `status = "nearest"` (the nearest failing candidate) or `"none"`:

```json
{"bull_put": [
  {"symbol": "LRCX", "strategy": "bull_put", "family": "credit_vertical",
   "legs": [{"expiry": "2026-11-20", "right": "P", "strike": 330.0, "side": "sell", "qty": 1, "price": 5.70, "bid": 5.65, "ask": 5.75, "iv": 0.46, "delta": -0.250, "oi": 2140, "volume": 412},
            {"expiry": "2026-11-20", "right": "P", "strike": 320.0, "side": "buy",  "qty": 1, "price": 3.60, "bid": 3.55, "ask": 3.65, "iv": 0.47, "delta": -0.174, "oi": 1630, "volume": 230}],
   "expiry": "2026-11-20", "dte": 48, "net": -2.10, "width": 10.0,
   "max_profit": 210.0, "max_loss": 790.0, "breakevens": [327.90],
   "pop": 0.75, "pop_kind": "keep", "pop_model": 0.73,
   "greeks": {"delta": 0.076, "theta": 0.021, "vega": -0.048, "gamma": -0.010},
   "liquidity": {"tier": "clean", "widest": 0.10, "min_oi": 1630, "vol_ok": true, "worst_fill": 2.00, "notes": []},
   "constraint": {"ok": true, "detail": "330 sits under support 340.9 (pad 0.25 ATR = 336.2) and under the trend line at expiry (341.9)"},
   "chart_stop": 336.2, "chart_stop_pl": -120.7, "rule_stop_pl": -158.0,
   "checks": [{"name": "open interest >= 500", "ok": true}, {"name": "earnings inside expiry", "ok": false, "blocking": false, "detail": "Oct 22 is inside Nov 20"}],
   "score": 0.328, "why": ["best fit to your rules", "most credit per $ risked"],
   "words": {"delta": "...", "theta": "...", "vega": "...", "pop": "...", "collect": "you collect $200-$210 (worst likely fill to mid)", "risk": "you risk $790, but the chart stop at 336.2 would lose about $121"},
   "sizing": null,
   "status": "ok", "rules_line": "delta 0.20-0.30 · 30-60 days · width 0.5-1.5 ATR ($6-17) · credit >= 25% · under support 340.9",
   "considered": 23, "degenerate": null},
  {"...": "row 2 = the Nov 20 325/315 (net -3.17, max_loss 683, chart_stop_pl -99, rule_stop_pl -137, breakevens [321.83], pop 0.71) - tagged 'safer'; row 3 the 335/325. ONLY the 330/320 above is the pick the sizing, ticket, payoff and tests use (II.2.19)"}]}
```

`payoff` is NOT stored - `GET /options/payoff/{symbol}?strategy=&pick=&units=$|R` runs
`payoff.build()` over the pick (II.2.9); the dict it renders:

```json
{"strategy": "bull_put", "family": "credit_vertical", "label": "Nov 20 330/320 put", "symbol": "LRCX",
 "spot": 349.20, "atr": 11.54, "as_of": "2026-10-03", "horizon": {"expiry": "2026-11-20", "dte": 48},
 "legs": [{"expiry": "2026-11-20", "right": "P", "strike": 330, "side": "sell", "qty": 1, "price": 5.70, "iv": 0.46, "delta": -0.250, "iv_source": "leg"},
          {"expiry": "2026-11-20", "right": "P", "strike": 320, "side": "buy",  "qty": 1, "price": 3.60, "iv": 0.47, "delta": -0.174, "iv_source": "leg"}],
 "xs": [297.0, "..."], "at_expiry": [-790.0, "..."], "today": [-612.4, "..."],
 "breakevens": [327.90], "max_profit": 210.0, "max_loss": 790.0, "unlimited_profit": false, "unlimited_loss": false,
 "pop": {"label": "chance of keeping it", "value": 0.75, "basis": "1 - short delta 0.25", "model": 0.73, "model_basis": "lognormal, sigma 46%, 48 d"},
 "markers": [{"x": 349.20, "label": "now 349.20", "kind": "now"}, {"x": 327.90, "label": "breakeven 327.90", "kind": "breakeven"},
             {"x": 336.20, "label": "chart stop 336.2 · about -$121 today", "kind": "stop", "y_today": -120.7, "y_expiry": 210.0},
             {"x": 332.72, "label": "rule stop about 332.7", "kind": "rule_stop", "y_today": -158.0},
             {"x": 340.00, "label": "support 340", "kind": "level"}, {"x": 336.10, "label": "trend line at expiry 336.1", "kind": "level"},
             {"x": 330.00, "label": "short 330", "kind": "strike"}, {"x": 320.00, "label": "long 320", "kind": "strike"}],
 "hlines": [{"y": -158.0, "label": "rule stop -$158 (20% of max loss)", "kind": "rule_stop"}, {"y": 105.0, "label": "take profit +$105 (50% of credit)", "kind": "target"},
            {"y": 210.0, "label": "max profit", "kind": "max_profit"}, {"y": -790.0, "label": "max loss $790", "kind": "max_loss"}],
 "units": {"mode": "$", "r_dollars": 158.0, "r_basis": "20% of max loss"},
 "caption": "Dashed line: what the trade would be worth if the stock moved there today, at today's implied volatility - an estimate. Solid line: at expiry (48 days).",
 "warnings": [], "error": null, "uid": "...", "svg": {"...": "server-built path strings"}, "series": {"...": "xs/at_expiry/today/units for the hover script"}}
```

`ticket` is NOT stored - `GET /options/ticket/{symbol}?strategy=&pick=&contracts=&dip=0|1` runs
`order_ticket.build()` at request time (read-time sizing, the `as_of`-vs-session check, the dip
toggle); the dict (B6.1):

```json
{"symbol": "LRCX", "strategy": "bull_put", "label": "Bull put spread", "contracts": 2,
 "header": "Prices are from 02 Oct 16:00 ET. Press Refresh after 21:30 Malaysia time (US open) and re-open the ticket before sending; the credit will have moved.",
 "refresh_first": false, "rejection": null,
 "legs": [{"action": "SELL", "qty": 2, "right": "P", "strike": 330.0, "expiry": "2026-11-20", "ref_mid": 5.70, "ref_bid": 5.65, "ref_ask": 5.75},
          {"action": "BUY",  "qty": 2, "right": "P", "strike": 320.0, "expiry": "2026-11-20", "ref_mid": 3.60, "ref_bid": 3.55, "ref_ask": 3.65}],
 "net": {"kind": "credit", "limit": 2.10, "floor": 2.00, "per_contract_usd": 210.0, "total_usd": 420.0, "work": "enter at the mid (2.10); if unfilled in a few minutes step down 0.05 at a time, never below 2.00"},
 "condition": null, "tif": "DAY",
 "stop": {"chart": {"level": 336.2, "loss_usd": 241.4, "per_contract_usd": 120.7, "gap_usd": 1580.0, "trigger": "LRCX last <= 336.20",
                    "trigger_outside_rth": false, "fill": "market", "close_at": 3.31,
                    "rth_note": "This order fires on the live price during regular hours, so it can fire on an intraday dip the close would have survived. If you prefer the close-based rule, leave this order off and act on the Positions tab's verdict instead."},
          "rule": {"kind": "20% of max loss", "loss_usd": 316.0, "per_contract_usd": 158.0, "close_at": 3.68}},
 "target": {"chart": null, "rule": {"kind": "50% of the credit", "close_at": 1.05, "profit_usd": 210.0}},
 "exits_text": "...", "rationale": "...", "must_happen": "...",
 "warnings": ["earnings Oct 22 are inside this trade - allowed by your rules; the stop is the only protection"],
 "as_of": "2026-10-02T16:00:00-04:00", "source": "cboe"}
```

(`condition` with `dip=True`: `{"on": "LRCX", "field": "last", "op": "<=", "value": 341.92,
"why": "...", "warning": "This order will also fire if LRCX crashes through 341.92 on bad
news. Only use it while you are watching."}`.)

#### II.2.7 Strategy keys and families

| key | label | family | direction / side | trends | setups | IV gate | DTE | step |
|---|---|---|---|---|---|---|---|---|
| `bull_put` | Bull put spread | `credit_vertical` | up / credit | up | support_bounce, trendline_bounce, ema_rebound | sell_directional | 30-60 | 1 |
| `bear_call` | Bear call spread | `credit_vertical` | down / credit | down | resistance_reject, trendline_bounce, failed_support | sell_directional | 30-60 | 1 |
| `buy_call` | Buy call | `long` | up / debit | up | support_bounce, trendline_bounce, ema_rebound, breakout_retest | buy | 45-90 | 2 |
| `buy_put` | Buy put | `long` | down / debit | down | failed_support, resistance_reject, trendline_bounce | buy | 45-90 | 2 |
| `bull_call` | Bull call spread | `debit_vertical` | up / debit | up | as buy_call (+ needs `target_level`) | mid_or_buy | 30-60 | 2 |
| `bear_put` | Bear put spread | `debit_vertical` | down / debit | down | as buy_put (+ needs `target_level`) | mid_or_buy | 30-60 | 2 |
| `iron_condor` | Iron condor | `condor` | neutral / credit | sideways | range | sell_neutral | 30-45 | 3 |
| `leaps_call` | Buy LEAPS | `leaps` | up / debit | up, sideways | any (+ weekly uptrend) | mid_or_buy | 270-540 | 4 |
| `calendar` | Calendar spread | `time` | neutral / debit | sideways, up, down | range or slow_drift; front IV >= back | any | front 20-30 / back 50-70 | 4 |
| `diagonal_call` | Diagonal call spread | `time` | up / debit | up | slow_drift + resistance_level | mid_or_buy | short 30-45 / long 180-365 | 4 |

`STRATEGY_KEYS` = the ten keys above (+ `custom` inside `payoff.build` only, never stored or
shown). Its ONE home is `app/services/strategy_rules.py`, in the user's order: `buy_call,
buy_put, bull_call, bear_put, leaps_call, diagonal_call, bull_put, bear_call, iron_condor,
calendar`; `option_prefs` (and every other module) imports it from there, and the recommender
breaks score ties in that order. `family in {credit_vertical, debit_vertical, long, leaps, condor,
time}`; `CREDIT_FAMILIES = {bull_put, bear_call, iron_condor}` (`pop_kind = keep`); `tab` = the
five drawer tabs. Earnings policy per rule: `defined_risk` - `option_prefs.defined_risk(key)` is
True ONLY for `bull_put, bear_call, bull_call, bear_put, iron_condor` (allowed with earnings
inside only when `earnings_rule = defined_risk_only`); `none_inside` for `buy_call`, `buy_put`,
`calendar` (its BACK expiry) and `diagonal_call` (its SHORT leg) REGARDLESS of the member's rule;
`any` for `leaps_call` (with a reason line). A `DEFINED_RISK = every strategy` set does not exist.
Score = `priority + iv_fit (0-20) + setup_quality/5 + term bonus 10 + structure bonus 5 - 10 if
unbuilt`; `recommended` = the best BUILT fit; a `rejected` row stores `score`, `why` and
`must_happen` as null.

#### II.2.8 Leg and Pick shapes

Leg (stored / API, every part):

```json
{"expiry": "2026-11-20", "right": "P", "strike": 330.0, "side": "sell", "qty": 1,
 "price": 3.10, "bid": 3.00, "ask": 3.20, "iv": 0.43, "delta": -0.26, "oi": 2140, "volume": 412}
```

| Rule | Value |
|---|---|
| `right` / `side` / `qty` | `C` or `P` / `sell` or `buy` / a POSITIVE int; `payoff.Leg.from_dict(leg, *, price=None)` derives the signed quantity (`+qty` buy, `-qty` sell); `price=` overrides the mid (the Positions tab passes `entry_price`) |
| `iv` | FRACTION, always, by the time anything reads it (II.2.12) |
| `delta` | signed, as the feed gives it |
| `oi` | the key is `oi`, never `open_interest`, once past `opt_legs.norm_leg(row, *, unit, expiry=None, right=None)`; None = unknown (never 0) |
| engine-only extras | `last, gamma, theta, vega, spread, quote_ok` - stripped by `opt_legs.stored_leg()` before a leg is written anywhere |
| `OptionTrade.legs` | + `entry_price, entry_delta, entry_iv`; `OptionTradeCheck.legs` per-day `{mid, delta, iv}` |

Pick (B4.2 Candidate; the JSON in II.2.6): `max_loss` / `max_profit` POSITIVE $ per contract;
`net` per share, negative = credit; `breakevens` a list (even for one value); `pop` 0..1 +
`pop_kind in {keep, profit}` + `pop_model` (no `pop_wording` / `pop_word` / `pop_text`);
`chart_stop` (= `plan.stop`), `chart_stop_pl`, `rule_stop_pl` (negative $ per contract, write
time); `sizing` null in the store, filled at read time; `liquidity.tier in {clean, limit, wide,
thin, unknown}`; `checks[]`; `status in {ok, nearest, none}`; `degenerate` null when `status =
ok`, else a dict whose `reason_key` is one of the fixed vocabulary `no_chain · no_expiry · no_band
· constraint · credit_floor · thin · theta_cap · extrinsic_cap · no_term · safety` (B4.8; the
same list in every part, and the member sees `option_words`' sentence for it, never the key). POP:
credit families
`1 - |delta_short|` (condor `1 - |d_sp| - |d_sc|`); every other family
`payoff.pop(family, legs, spot, sigma_h, T_h, xs, ys)` - ONE function. Wording
(`option_words.pop_words(pop, pop_kind)`): keep -> "About a {p}% chance of keeping the credit -
an estimate from today's option prices (the short strike's delta), not a promise. Earnings, news
and gaps are not in that number."; profit -> "About a {p}% chance of profit if held to expiry,
at today's volatility; this trade is managed by the chart stop and target, so the real odds
depend on the move, not this number."; the model figure as "model estimate {m}%"; Telegram
"about {p}% chance of keeping it (estimate)" / "about {p}% chance of profit (estimate)".

#### II.2.9 Payoff

| Rule | Value |
|---|---|
| The ONE implementation | `services/payoff.py` + `templates/_payoff_chart.html` (server-rendered inline SVG, `viewBox 0 0 640 300`, theme tokens `--po-exp`, `--po-today`, `--po-profit-fill`, `--po-loss-fill`, `--po-loss`, HTMX-swapped into `#optPayoff`); no canvas, no `thPayoffLoad`, no client formulas |
| Route | `GET /options/payoff/{symbol}?strategy=&pick=&units=$|R` (+ `?trade=<option_trades.id>` on the Positions tab: the trade's legs at `entry_price`, sigma from the latest check's mids, the `now` marker carrying `y_today = pl_now` - the dot) |
| `build()` | `build(legs, *, strategy, spot, atr, as_of, chart_stop=None, target=None, levels=(), sigma_fallback=None, pl_now=None, premium_stop_pct=None, loss_fraction=0.20, units="$") -> dict` (II.2.6 JSON); always per ONE contract (no `contracts=`); family via `option_prefs.family_of(strategy)`; `units` passed in, never patched on the result. The dict carries `strategy, family, label, symbol, spot, atr, as_of, horizon, legs, xs, at_expiry, today, markers, hlines, breakevens, max_profit, max_loss (positive), unlimited_profit, unlimited_loss, pop{label, value, basis, model, model_basis}, units{mode, r_dollars, r_basis}, caption, warnings, error, uid, svg{...}, series{...}` - no `contracts`, no `dot` (the dot is the `now` marker's `y_today`) |
| `levels=` | a tuple of `{x: price, label: str, kind in {support, resistance, trend_line, target}}` (what the route's `_levels_at(setup, expiry)` returns: support / resistance from `setup.levels`, the trend line's `tl.value_at[expiry]`, the range edges); each becomes a `level` marker (`target` kind -> the `target` marker) |
| Functions | `normalise_iv(v, *, unit)`, `implied_vol`, `calibrate` (sigma solved from the dealt price -> leg.iv -> sibling -> iv30/100 -> HV20/100), `horizon` (front expiry), `leg_value / pnl / curve_at` with `iv_bump: float = 0.0`, `grid`, `expiry_curve`, `breakevens` (always a list), `extremes` (positive magnitudes), `pop`, `price_at_pnl`, `svg_paths` |
| `iv_bump` | a RELATIVE lift, defined ONCE in `payoff.leg_value`: `sigma_used = leg.iv x (1 + iv_bump)`; `STOP_IV_BUMP = 0.10`, so the sizing dict's `stop_iv 0.506 = 0.46 x 1.10` (never an absolute shift of sigma) |
| Grid | 201 points over `[min(strikes, spot, markers) - 2 ATR, max(...) + 2 ATR]` plus every strike, marker x and breakeven (the kinks are exact) |
| T+0 | `curve_at(legs, xs, 0)` - today at t = 0, `RISK_FREE 0.04`, q = 0; two-expiry structures valued at the FRONT expiry with the back leg model-priced |
| Markers (`kind`) | `now`, `breakeven`, `stop` (chart stop), `rule_stop`, `target`, `level` (support / resistance / trend line at the horizon expiry via `tl.value_at[expiry]` / range edges), `strike`; both stops are always drawn and labelled where both exist |
| Rule stop drawn for | EVERY family, no exception (R1): credit families (`loss_fraction` x max loss, house 20%), `leaps_call` / `diagonal_call` (the leaps block's `premium_stop_pct`, house 40), `buy_call` / `buy_put` / `bull_call` / `bear_put` / `calendar` (the long block's `premium_stop_pct`, house 50); hline + vertical marker, and `rule_stop_pl` is computed for every family (II.2.14). A `buy_call` build therefore HAS a `rule_stop` marker and hline (C3.9 / C5.2 / C6.3 / D8.2 read "present") |
| R (`units.r_dollars`) | credit families `0.20 x max_loss`; every other family `|pnl_today(chart_stop)|` (or `max_loss` without a chart stop); the server renders `$` or `R`, the client never recomputes; the choice is remembered in `localStorage['th.payoff.units']` |
| Caption, verbatim | "Dashed line: what the trade would be worth if the stock moved there today, at today's implied volatility - an estimate. Solid line: at expiry ({dte} days)." (+ for calendars / diagonals: "Drawn at the near expiry ({front}); the far option is valued by the model, so the solid line is an estimate too.") |
| Legend | "breakeven 327.90 · max +$210 / -$790 · chance of keeping it 75% · model estimate 73% · per contract" |
| Full-chain expander | `GET /options/chain/{symbol}?expiry=&all=0|1`: defaults to the pick's expiry and +/- 12 strikes around spot (`CHAIN_STRIKES_EACH_SIDE = 12`), an expiry picker and "show all strikes" per expiry; never more than ~60 rows without a click; gamma shown ONLY here; lazy-loaded with `hx-trigger="toggle from:closest details once"` |
| Failure | `today: null` + "today line needs a quote" (expiry line only); `error: "expired"`; `error: "no edge: the spread pays nothing"`; `r_dollars == 0` -> R toggle disabled; always rendered inside the same pane height |

#### II.2.10 Trend line

`services/trend_line.py`: `find(bars, direction="up", *, at=()) -> dict | None`,
`value_on(line, times, date)`, `bounce(bars, line)`, `overlay(line, bounce)`.

| Rule | Value |
|---|---|
| `tl` dict | `{direction, p1{time,price}, p2{time,price}, i1, slope_per_bar, slope_atr, touches[{time,price}], n_touches, span_bars, value_today, value_at{date: price}, broken, last_break, warning, residual_atr, atr, channel{offset, n_touches, touches, value_today, width_atr}|null}` - the names `slope_per_day` and `{a, b}` do not exist; `touches` is a LIST |
| Constants (ATR-relative) | `LOOKBACK 252`, `MAX_PIVOTS 20`, `MIN_SPAN 15`, `TOUCH_ATR 0.35`, `BREAK_ATR 0.5`, `PIERCE_MAX_ATR 1.0`, `MIN_SLOPE_ATR 0.01`, `MIN_RISE_ATR 2.0`, `MAX_SLOPE_ATR 0.25`, `RECENT 5`, `MIN_BARS 60`; pivots from `support_bounce._swings`; validity by closes (`BREAK_ATR`), touches by pivots; choice = most touches, longest span, most recent touch, smallest residual, older p1 |
| `at=` | the nightly job passes every expiry in the snapshot (`chart_state.read(symbol, *, bars, long_bars, today, expiries)` -> `ema_setup.analyze(..., at=expiries)` -> `find(..., at=)`), so `value_at` is filled in the ONE detector run; a cold page read fills the one drawn expiry with `value_on` (pure arithmetic). `chart_state.read`'s keyword is `expiries=` (one spelling; `at=` belongs to `analyze` / `find` only) |
| `ema_setup.analyze()` | gains `at: tuple[str, ...] = ()`, stores `tl`, `tl_bounce` (and `rng`, II.2.11) and `times`; condition key `t1` ("Trend-line bounce", weight 110, needs `n_touches >= 3`, a pin / engulfing at the line, high volume) joins `COND_KEYS` - every `sym_conds` reader sees two more switches (`t1`, `r1`) |
| Drawing | `chart_trendline = trend_line.overlay(tl, tl_bounce)` -> `_price_chart.html` C4.4: a read-only `addLineSeries` (amber `#f59e0b`, 2 px, dashed when broken, title `Trend xN`), extended to the EXP badge through `window.__paintTL(far, slots)`, touch markers `TL i/n` merged into `candleMarks`, weekly snapping through the hoisted `snapTime(t)`, the channel as a thin dashed series; never into the member's drawings; chips `tl` / `tlx` / `tlb` in the list templates |
| Recommender | `broken` -> a credit idea under it is rejected (`reason_key no_setup`, the text names the date); `warning` -> headline suffix "- but today closed under the line" |
| Cost | < 2 ms on 500 bars (budget 5 ms) |

#### II.2.11 Range

`services/range_box.py`: `find(bars, *, emas=None) -> dict | None`, `resistance_only(bars)`,
`overlay(rng)` - the ONE range / sideways module (negation-based reuse of
`support_bounce._swings/_members/_touches`).

| Rule | Value |
|---|---|
| `rng` dict | `{low, high, zone_low: [lo, hi], zone_high: [lo, hi], atr, width_atr, touches_low[{time, price, kind}], touches_high[...], n_low, n_high, since, age_bars, last_touch, pos_pct (0 = on the low, 1 = on the high), inside_bars, stack_flat, ema200_inside, sideways, reasons[]}` |
| Constants | `MIN_TOUCHES 2` per edge, `MIN_WIDTH_ATR 2.0`, `MAX_WIDTH_ATR 10.0`, `ACTIVE_BARS 60`, `INSIDE_BARS 15`, `STACK_TOL_ATR 1.0`, `SLOPE_BARS 10` / `SLOPE_TOL_ATR 0.5`, `MIN_BARS 60`; `sideways = stack_flat and inside >= INSIDE_BARS` (None when EMAs not supplied) |
| `chart_state.trend` | `== "sideways"` iff `rng.sideways`; every other trend value from the EMA stack |
| Iron condor | fits only when `sideways`; constraint `short_put <= rng.zone_low[0] - pad`, `short_call >= rng.zone_high[1] + pad`; else `trending_not_sideways` / `no_range` |
| Calendar | "sits" when `sideways` or (`stack_flat` and `0.35 <= pos_pct <= 0.65`) |
| `mirror_setups.py` | B's former `range_detector.py`, holding ONLY `mirror_bars`, `find_resistance_reject(bars, d_emas, w_emas)` and `find_breakdown(bars)` (the bear-side setup candles); B's `SIDEWAYS_*` / `RANGE_*` constants do not exist |
| `ema_setup.analyze()` | stores `rng` (`range_box.find(bars, emas=(e20, e50, e200))` in its own `try`); condition key `r1` ("Range", default True, weight 0, chip `rng`) joins `COND_KEYS` with `t1`; `range_box`'s 5-10 ms per ticker is budgeted inside `setups_for_many` (cold cache: <= 10 ms x symbols) |
| Drawing | `chart_range = range_box.overlay(rng)` -> two price lines (`Range low xN` cyan `#22d3ee`, `Range high xN` fuchsia `#e879f9`) + touch markers (`S->R` for a flipped support) |

#### II.2.12 IV units

| Quantity | Unit | Rule |
|---|---|---|
| `ContractRow.iv`, snapshot `iv`, Leg `iv` | FRACTION (0.3585) | the SOURCE normalises; Cboe and Alpaca send fractions; ONLY `BridgePayloadSource` divides by 100; no `> 3.0` magnitude heuristic anywhere (a deep-ITM Cboe row legitimately prints 3.1099); `opt_legs.norm_leg(row, unit=)` and `payoff.normalise_iv(v, unit=)` take `unit in {"fraction", "percent"}` from the chain's source and never guess (`"percent"` only for the legacy raw bridge payload of `routes/options.analyze`) |
| `iv_daily` per-day stats (`iv30, atm_iv30, hv20, hv60, iv_front, iv_back, iv_lo, iv_hi`) and `signal.iv.*` | PERCENT (32.03) | the `iv_history.iv30` unit; `premium_gauge` forms ratios only and never converts |
| `term_ratio`, `iv_hv_premium`, `skew_norm` | unitless ratios | `iv_front / iv_back`, `iv30 / hv20`, `skew25 / atm_iv_front` |
| `skew25` | vol points | `(iv_put25 - iv_call25) x 100` |
| bridge `/iv?series=1` daily series (bridge 1.6) | PERCENT | `round(close * 100, 1)` (ibkr_bridge.py:559-563, the unit its own `iv_current / iv_low / iv_high` use); `option_store.bootstrap_iv` stores it AS-IS bounded `0.1 <= iv <= 1000`, <= 400 points, dates `<= today` and `>= today - 400d`, and NEVER overwrites a day the server read itself |
| the bridge's raw per-row `iv` | percent | divided by 100 by `BridgePayloadSource`; `bull_put.pl_profile`'s own `/ 100` stays for the legacy tab |
| greeks, prices | per share, signed as the feed gives them | |
| `oi`, `volume` | int contracts; None = the feed did not say | |

#### II.2.13 Live

`POST /options/live/{symbol}` (body `{chain, iv: {series, iv_current, iv_rank, iv_percentile},
nlv, diag}` posted by the browser after `Promise.all([/chain, /iv?series=1, /account])` against
`127.0.0.1:9224`):

| Step | Rule |
|---|---|
| 1 | `option_data.BridgePayloadSource(payload["chain"])` - no network, every number re-validated, rows capped at 400, strikes within 50% of spot, `iv / 100`, `kind="live"`, `source="bridge"`; `strike_picker.pick` + `option_sizing.size` run IN-REQUEST against the member's prefs (the chart / gauge come from the stored card) |
| 2 | the card re-renders with the badge "live · TWS {HH:MM} ET", the picks table RESTRICTED to the live expiry (every leg row carries the live time); the stored expiries sit behind "show delayed expiries" - two moments never share a table unlabelled |
| 3 | persists ONLY the IV series through `option_store.bootstrap_iv(db, sym, series, source="ibkr")` (+ an `option_jobs(job="bootstrap")` row; every signal row for the symbol on the latest day -> `status="stale_iv"` so the next read recomputes the gauge); `series` absent -> "Your bridge is older than 1.6 - restart `bridge\start_ibkr_bridge.bat`" on the IV line only, the chain still grades live |
| 4 | `nlv` sizes THIS request only ("from TWS · [remember this]"); written through `trade_prefs.write` ONLY on the explicit click (`POST /options/rules tab=shared&nlv=`) |
| Never | a live row written to `option_chain_snapshot`; a live grade cached in `option_signal` |
| Touch / `< lg` | the Live button is hidden; the slot reads "Live quotes need TWS on your PC" |
| Bridge down | the calm bridge panel ("Could not reach your IBKR bridge on this PC (127.0.0.1:9224). Start TWS and `bridge\start_ibkr_bridge.bat`, then press Live again. The card still shows the delayed data.") + `[Start the bridge]` `[Retry]` + a diagnostics `<details>` |
| Bridge | `TradeHunterIBKRBridge/1.6`: `/iv?symbol=X&series=1` adds `series` (PERCENT, oldest first, <= 400 points); without `series=1` the reply is unchanged; `BRIDGE_MIN_VERSION = "1.6"`; every member-facing string says "older than 1.6" |

#### II.2.14 Chart stop and exits

| Rule | Value |
|---|---|
| ONE source | `setup.plan` (B2.6): credit `stop = zone_lo - LEVEL_PAD_ATR x ATR`; debit `stop = min(entry - STOP_ATR x ATR, zone_lo - LEVEL_PAD_ATR x ATR)`, `target = entry + TARGET_R x (entry - stop)` capped at a resistance between 1.5R and 2R; neutral (range) stop = the range edges +/- pad. LRCX fixture: 339.1 - 0.25 x 11.54 = **336.2** (the mockup's 338 is replaced everywhere); ISRG: 405.81 - 11.54 = **394.27** / 405.81 + 2 x 11.54 = **428.89** (II.2.19). `setup.stop = plan.stop`, `setup.target = plan.target` |
| Who reads it | the picker (`chart_stop`), sizing, the ticket, the payoff marker, the monitor - the same number everywhere |
| `chart_stop_pl` (write time) | `-max over d in STOP_TIMES x front_dte of -payoff.pnl(legs, S=stop, days_ahead=d, iv_bump=STOP_IV_BUMP)` - the larger loss of "now" (sellers) and "mid-life" (buyers); calendar / diagonal use `d = front_dte`; `iv_bump` is the RELATIVE lift of II.2.9 (`sigma x 1.10`), which is how the golden 330/320 reproduces **-120.7** (and the 325/315 row -99) |
| `rule_stop_pl` (write time) | computed for EVERY family (and drawn by the payoff pane, II.2.9): credit `loss_fraction x max_loss` (20%; -158 on the golden pick); condor `loss_stop_pct_credit/100 x credit`; long / debit_vertical `premium_stop_pct/100 x premium` (long block, 50); leaps `premium_stop_pct/100 x premium` (leaps block, 40); calendar / diagonal `premium_stop_pct/100 x net_debit` (diagonal inherits the leaps figure); `fires_first` = the smaller of the two |
| Monitor | `option_exits.grade()` with the `bull_put.monitor` contract (`state, action, reasons, urgent, *_breach, loss_pct, profit_pct`); losing-side lines win ties; `WATCH` at 80% of a losing line, 50% of the loss line, within 3 days of a time line |
| Every family, first row | earnings now inside: `earnings.date <= front_expiry` (the SHORT leg for a diagonal, the BACK expiry for a calendar), not allowed by `earnings_rule`, and the date was unknown or later at entry (`earnings_date_at_entry`) -> **WATCH (urgent)**: "Earnings {date} now fall inside this trade (the date was unknown or later when you entered). Decide before the close that day." (also re-evaluated at read time by the Positions tab from the stored signal) |
| credit_vertical | chart stop (underlying CLOSE beyond `chart_stop`) CLOSE · short delta `>= roll_delta` ROLL (dte > 30) / CLOSE - house `roll_delta` **0.35 adjust / 0.40 close** (B7.3's 0.30 is a member override) · loss `>= loss_fraction` (20% of max loss) ROLL / CLOSE · profit `>= profit_target` (50% of credit) TAKE · time `dte <= dte_floor` (21) CLOSE |
| debit_vertical | chart stop CLOSE · chart target or `mark >= 0.75 x width` TAKE · premium stop (50%) CLOSE · time `dte <= 21`: CLOSE if under water, WATCH if in profit |
| long | chart stop CLOSE · chart target (2R) TAKE · premium stop CLOSE · theta over the member's ceiling WATCH · time `dte <= 21` CLOSE / WATCH |
| leaps | weekly stack broken two weeks CLOSE · loss `>= premium_stop_pct` (40) CLOSE · delta `< delta_floor` and `dte > roll_dte` ROLL · delta `>= 0.90` TAKE (partial) · `dte <= roll_dte` ROLL |
| condor | either short delta `>= roll_delta` ROLL (that side) / CLOSE · close outside `rng.low - pad` / `rng.high + pad` (the edges stored in `OptionTrade.meta` at entry) CLOSE · loss `>= loss_stop_pct_credit` CLOSE · profit 50% TAKE · time 21 CLOSE |
| calendar | close outside the entry breakevens (stored in `OptionTrade.meta` at entry) CLOSE · profit `>= cal_take_pct` TAKE · `front_dte <= 5` ROLL · loss `>= premium_stop_pct` CLOSE |
| diagonal_call | short delta `>= 0.50` ROLL (up and out) · `short_dte <= 7` ROLL · the LEAPS rows on the long leg (incl. the 40% loss line) · loss `>= premium_stop_pct` CLOSE |
| `sweep(db)` | `spread_monitor.sweep`'s shape over `option_trades`: one chain fetch per underlying (`option_data.fetch_chain`, 15-min cache shared with Refresh), per-member prefs, `record_check` upsert per ET day; `ema_setup.setup_for(sym, deep=True)` once per underlying holding a LEAPS / diagonal (for `w_uptrend`); paper trades swept identically; urgent verdicts -> the nav badge, the Positions row, the existing Discord post; the Telegram push carries ideas, not exit lines |

#### II.2.15 Telegram

| Rule | Value |
|---|---|
| Modules | `services/telegram.py` (sender: `configured()`, `send(html, *, chat_id=None)` reusing `scripts._common.telegram_env` for the vault token, chunked at 4000 chars, `answer_starts(db)`, `send_code(chat_id)`) + `services/telegram_push.py::run(db, *, as_of, dry_run=False) -> {members, ideas, sent, failed, skipped}` + `idea_key(symbol, strategy, front_expiry)` + table `option_idea_push`; called by the nightly job's step 6 (after `option_exits.sweep` step 4 and `prune` step 5, so the push sees tonight's checks); `option_signal.pushed_at`, `option_push.py` and `option_exits.notify` do not exist |
| Opt-in (`prefs["telegram"]`) | `{enabled, chat_id, verified, quiet, paused_until, pending{chat_id, code, expires}}` - its own top-level key returned by `option_prefs.read()`, never a SCHEMA field, never hashed; rows on the Shared tab posting to `POST /options/telegram` with the body `{action in {request_code, verify, quiet, pause, disable}, chat_id?, code?, pause_days?}` (`request_code` needs `chat_id`; `verify` needs `code`; `pause` needs `pause_days`; the reply is the re-rendered shared tab) |
| Handshake | the member sends `/start` to the bot -> the bot replies with the chat id -> the member types it and presses Send code -> a 6-digit code (10-minute expiry) goes to THAT id -> Verify; the drawer accepts the id only with the code; enabled + unverified is not saved ("Enter the 6-digit code the bot sent you after /start.") |
| Guards (skip + log) | `signal.status != ok` · `iv_daily.partial` · `iv.provisional` / `basis = unknown` · `earnings_date is None` ("skipped: earnings date unknown") · the recommended rule's `step > CURRENT_STEP` · `as_of` older than the last session · no recommended strategy or no pick under the member's hash (silent) |
| Dedupe | key `(symbol, strategy, front expiry)` per member; re-push only if `|pick.short_strike - row.short_strike| > 1 x atr` (then UPDATE the row); rows older than 45 days pruned |
| Cap | 5 ideas per member per message by `score`; "and N more on the page" |
| Switches | `quiet` (ideas stay on the page); "pause for 7 days" link in every message (`public_url + "/options?pause=7"` -> confirm -> `POST /options/telegram pause_days=7`) |
| Text | first line, verbatim, every message: "Ideas for tonight's US session (opens 21:30 Malaysia). Prices are last night's close."; per idea: the stored headline, the first pick in collect / risk / chance words ("about {p}% chance of keeping it (estimate)"), the "What has to happen" line, the chart stop with its T+0 cost, the earnings flag, the deep link `public_url + "/options?symbol=SYM"`; NEVER a contract count; HTML parse mode |
| Dry run | `deploy/options_nightly.py --telegram-dry-run` composes and logs, `option_idea_push` rows written with `error="dry-run"` (so the second run still dedupes); `--no-push` skips entirely; not configured -> logged once, skipped |
| Badge | `ideas_new` = the count of `OptionIdeaPush` rows for THIS member with `sent_at >= prefs["options_seen_at"]` (`options_seen_at` is rewritten when `GET /options` renders, so the count clears) |

#### II.2.16 Jobs

| Rule | Value |
|---|---|
| Table / model | `option_jobs` / `OptionJob` (A2.1 columns + `pushed` int); `job in {nightly, refresh, bootstrap, backfill, telegram_poll}` |
| Service | `services/job_runs.py`: `start(db, job, run_on, source=None) -> OptionJob` (commit at once, so a crash leaves "started, never finished"), `finish(db, run, *, ok, errors, rows, pushed, note, detail)`, `latest(db, job) -> OptionJob | None` (newest FINISHED), `missed(db, job) -> bool` (no finished nightly run for the last ET trading day by 08:00 MYT) |
| Nightly order (`option_nightly.run_nightly`) | 0 `job_runs.start` -> 1 `backfill_from_iv_history` (on `--backfill`; idempotent) -> 2 universe = distinct ACTIVE basket symbols over all owners + every symbol with an OPEN `option_trades` row; `hashes = option_prefs.distinct_hashes(db)` -> 3 per symbol (pacing 1.5 s Cboe / 0.4 s Alpaca): fetch chain (`fresh=True, retries=3`), bars 2y + weekly + earnings, `option_metrics.all_for`, `replace_snapshot`, `upsert_iv_daily`, `chart_state.read(sym, bars=bars, long_bars=long_bars, today=run_on, expiries=expiries)` ONCE, `option_engine.compute(chain, metrics, state, prefs)` + `upsert_signal` per hash, commit per symbol -> 4 `option_exits.sweep(db)` -> 5 `option_store.prune(db, today)` (snapshots 90 d, `option_trade_checks` 90 d, `option_idea_push` 45 d, II.2.1) -> 6 `telegram_push.run(db, as_of=run_on, dry_run=...)` (soft-fail; "the push is step 6" everywhere) -> 7 `job_runs.finish` |
| Soft-fail | per ticker at every letter (`ChainError` -> `status=error`, Yahoo missing -> `hv*`/`earnings_*` None, an engine exception -> `option_signal.status="error"` for that hash, data rows kept); exit 0 whenever step 0 succeeded |
| Task | `TST-Options-Nightly` at **07:15 MYT** (after `TST-Portfolio-Check` 06:00 and `TST-Spread-Scan` 06:30 so two jobs never hit Cboe at once), 30-minute execution limit, log `logs\options_nightly.log`; registered by `deploy\setup_options_nightly_task.ps1`; `MAX_BASKET = 60` per member (~2.5 s per ticker; the import cap is lowered to 60) |
| CLI | `deploy\options_nightly.py [SYM ...] [--backfill] [--no-push] [--telegram-dry-run] [--on YYYY-MM-DD] [--source alpaca] [-v]` |
| Refresh | `POST /options/refresh/{symbol}` -> `option_nightly.refresh_symbol(db, sym, user)`: the per-symbol pipeline with `kind="intraday"`, `fresh=True`, `retries=0`, house hash + the caller's hash, an `option_jobs(job="refresh")` row; cooldown 60 s per (member, ticker) except the ticket's "Refresh first" press |
| `GET /options/badge` (JSON) | `{run_on, finished_at, ok, errors, stale (run_on older than the last ET trading day), running (finished_at None and started < 40 min ago), job_missed, ideas_new, urgent, watch}` from `job_runs.latest`, `option_trade_checks`, `option_idea_push` - never a market call |
| `GET /options/status/strip` (HTML) | `_options_status.html` from the same dict + `state (ok|warn|bad|none), as_of_oldest, n_basket, n_stale, bridge_port, delayed_or_live, paused`; polled every 300 s; emerald `job ✓ 07:17 MYT · 5/5 tickers` / amber (errors, or one day behind, or "tonight's run missed") / rose (>= 2 days, or crashed > 2 h) |
| Idempotent re-run | snapshot replaced per `(symbol, snap_on, kind)`; `iv_daily` upsert on `(symbol, on)` (EOD overwrites intraday, the IBKR bootstrap never overwrites a server-read day); `option_signal` upsert per hash; `option_idea_push` dedupes; a new `option_jobs` row per run; `--on` refiles under a date |

#### II.2.17 Basket

| Rule | Value |
|---|---|
| Table | `option_basket(id, user_id nullable, owner_key, symbol, source, note, active, added_on String(10) ET date, pos, created_at)`; `UNIQUE(owner_key, symbol)` |
| `source` | `typed | paste | watchlist | ivscan_list | ivscan_scan | scanner | screener | sector | positions | system` |
| Import (`POST /options/basket/import`, body `{source, text, symbols[], note}`) | `paste` (textarea, `ivscan._clean_symbols`) · `watchlist` (`user_watchlist.symbol_set`) · `ivscan_list` (`prefs.ivscan_universe`, the old "My list") · `ivscan_scan` (`IVScanItem` rows, the TWS scanner's stored output) · `scanner` (the live bridge `/scan` symbols posted by the browser) · `screener` (`spread_candidates`) · `positions` (open `option_trades` symbols); `add` = import with `source="typed"`; duplicates skipped; cap `MAX_BASKET = 60` with `{added, skipped, over_cap, total}` returned (`over_cap` 10 on the 70-symbol case). Encodings: `add` and `remove` are form-encoded (HTMX `hx-vals`: `symbol`, and `note` for add); `import` is a JSON body |
| Remove | deletes the member's row only; shared snapshot / signal rows expire through `prune` |
| Universe | `option_store.basket_universe(db)` = distinct ACTIVE symbols over all owners (+ open trades) |
| Cell states | per row: trend arrow (`sig.trend`), IV cell (`62` amber / grey / teal by basis `rank`; `~62` dotted grey for `percentile` / `provisional`; `-` for `unknown`), idea word, `pick_state in {has_picks, no_strike_passes, not_checked}` (grey dot "open the card to check your rules" when no row exists for the member's hash yet), data-age dot (emerald < 20 h, amber 20-72 h, rose older / no signal) |
| Sort | `?sort=idea|iv|trend|added` (best idea first) |
| Mobile | a horizontal chip strip (`compact=1`) |

#### II.2.18 Routes and menu

All in `app/routes/options_page.py` (`prefix="/options"`), registered in `main.py` BEFORE the
legacy `routes/options.py` router (whose `GET /{symbol}` catch-all would otherwise swallow the
fixed paths) with `dependencies=[Depends(menus.require_menu("options"))]`; the legacy router,
its `POST /options/track` and `_options_tab.html` are untouched (the Watchlist pane needs them).

| Method · path | Returns | Purpose |
|---|---|---|
| `GET /options` | `options.html` | the shell; `?symbol=`, `?tab=positions`, `?focus=<trade_id>`, `?pause=7`; writes `prefs["options_seen_at"]` |
| `GET /options/basket` | `_options_basket.html` | the left column; `?sort=` |
| `POST /options/basket/add` · `remove` · `import` | `_options_basket.html` + `HX-Trigger options:basket-changed` | II.2.17; `add` / `remove` take a form-encoded body (`hx-vals`), `import` a JSON body `{source, text, symbols[], note}` (model `BasketImport`) and returns `{added, skipped, over_cap, total}` |
| `GET /options/card/{symbol}` | `_options_card.html` | the centre card; `?strategy=&pick=` |
| `GET /options/picks/{symbol}` | `_options_picks.html` | strikes + sizing line + `#optPayoff` + `#optActions`; `?strategy=`; re-rendered on `options:rules-changed` |
| `GET /options/chart/{symbol}` | `_options_chart.html` -> `_price_chart.html` | overlays from the STORED `setup` (`card_for(db, symbol, user)["setup"]`): `chart_bounce` from `setup.sup`, `chart_trendline = trend_line.overlay(setup.tl, setup.tl_bounce)`, `chart_range = range_box.overlay(setup.rng)`, `chart_spread {legs, breakevens, expiry, label}` (credit / condor / time) or `chart_levels {entry, stop, target}` (debit / long / leaps); `_chart_overlays` NEVER calls `ema_setup.setup_for` (no detector run on a request); `?strategy=&pick=` |
| `GET /options/payoff/{symbol}` | `_payoff_chart.html` | `?strategy=&pick=&units=$|R`; `?trade=` on the Positions tab |
| `GET /options/chain/{symbol}` | `_options_chain.html` | the expander; `?expiry=&all=0|1` |
| `GET /options/ticket/{symbol}` | `_options_ticket.html` | `?strategy=&pick=&contracts=&dip=0|1` |
| `POST /options/refresh/{symbol}` | `_options_card.html` + `HX-Trigger` | II.2.16 |
| `POST /options/live/{symbol}` | `_options_card.html` + `HX-Trigger` | II.2.13 |
| `GET /options/rules` · `POST /options/rules` · `POST /options/rules/reset` | `_options_rules.html` + `HX-Trigger options:rules-changed {tab, hash, recompute}` | `?tab=shared|credit|debit|condor|time`; `tab=all` on reset; nlv / risk_pct / the credit exit lines go to `trade_prefs.write` |
| `POST /options/track-idea` | `_options_positions_tab.html` + `HX-Trigger options:tracked` | form `symbol, strategy, pick, contracts (1-500), note`; the pick is re-read from the signal (409 on a miss); 409 on `earnings_inside` under `none_inside` |
| `GET /options/positions` | `_options_positions_tab.html` | `?status=open|closed&focus=` |
| `POST /options/positions/{id}/close` | `_options_positions_tab.html` | `reason` |
| `GET /options/badge` | JSON | II.2.16 |
| `GET /options/status/strip` | `_options_status.html` | II.2.16 |
| `POST /options/telegram` | `_options_rules.html` (shared tab) | body `{action in {request_code, verify, quiet, pause, disable}, chat_id?, code?, pause_days?}` (II.2.15): opt-in / Send code (`request_code`), Verify (`verify`), `quiet`, `pause` (`pause_days`), `disable` |

| Convention | Rule |
|---|---|
| Validation | `symbol` through `_clean_symbol()`; `strategy in STRATEGY_KEYS` else the recommended one; `pick` clamped; `tab in TABS`; `units in ("$", "R")`; `broker in ("tws", "moomoo")` |
| Menu | `("options", "Options", None, "/options")` appended to `MENUS` after `curated` (flat, no dropdown); `ivscan`, `spreads`, `positions` stay in `HIDDEN_KEYS`; the three old routers keep their guards widened to `require_menu("positions", "options")` / `("ivscan", "options")` / `("spreads", "options")` so a member granted only `options` can still open them by URL |
| `base.html` | the nav badge condition `href == '/portfolio'` -> `'/options'`, `hx-get="/options/badge"`, two more chips (`job_missed` ⚠, `ideas_new`) |
| HTMX | fragments wrapped in `.opt-basket / .opt-card / .opt-picks / .opt-rules`, swapped by `outerHTML`; lazy loads inside `<details>` use `hx-trigger="toggle from:closest details once"` (the DOM `toggle` event does not bubble); a pick click moves the strike lines through `window.thChartSetStrikes(spec)` and re-requests only `#optPayoff` - no `/options/chart` round trip; only the visible pane (Ideas or Positions) is in the DOM, so one chart at a time |
| Scrollbars | every scroll area inherits `base.html`'s invisible-until-hover rule; no `scrollbar-*` / `::-webkit-scrollbar` in the new templates |

#### II.2.19 Golden fixtures

Two JSON fixtures under `tests/fixtures/options/` carry ONE set of numbers that every part, every
test, every worked example and the §7 mockup use; a figure anywhere that disagrees with these
tables is wrong and is corrected to them (integrator rulings R3 / R4, 2026-10-04).

**`lrcx.json` - the credit-spread fixture (step 1)**

| Item | Value |
|---|---|
| Chart | spot **349.20** · ATR **11.54** · support **340** (level 340.9, zone low 339.1) · chart stop **336.2** (zone low - `LEVEL_PAD_ATR 0.25` x ATR) · target **375.2** (`TARGET_R 2.0`, R = 13.0) · uptrend 34 sessions, support bounce on 1.7x volume, trend line 3 touches (341.9 at Nov 20) · earnings 2026-10-22, inside Nov 20 (the fixture member's `earnings_rule` is `defined_risk_only`, so the check is non-blocking and the ticket warns) |
| IV | IV30 **46.0** · HV20 **38.0** · `iv_hv_premium` **1.21** (the RATIO iv30 / hv20) · `iv_front` **50.0** (Oct 31) · `iv_back` **45.0** (Dec 19) · `term_ratio` **1.11** · IV rank **62** (`basis = rank`, n 252) · verdict **SELL** · `verdict_why` "IV rank 62 (>= 50) and priced for 21% more movement than the stock has actually shown" · `gates {buy: false, sell_directional: true, sell_neutral: true, mid: false}` |
| Recommended | `bull_put` (score 90.1); `bull_call` / `leaps_call` also fit; `buy_call` rejected `expensive` |
| THE golden pick | Nov 20 **330/320** put spread at credit **2.10** (short leg iv 0.46, short delta 0.25): `max_profit` **210** · `max_loss` **790** · `breakevens` **[327.90]** · `pop` **0.75** (`pop_kind` keep) · `chart_stop_pl` **-120.7** (shown "about -$121") · `rule_stop_pl` **-158** (20% of max loss) · worst likely fill 2.00 |
| Sizing (house prefs: NLV 100,000, risk 1%, `GAP_MULT` 2.0, `MAX_POSITION_PCT` 10) | `by_chart_stop` **8** · `by_gap` **2** · `by_notional` **10** -> **2 contracts**; `stop_iv` 0.506 (0.46 x 1.10); the line "2 contracts: about $242 if the stop fires, up to $1,580 (1.6% of your account) if the stock gaps past it" |
| The 325/315 spread | credit 3.17, `max_loss` 683, `chart_stop_pl` -99, `rule_stop_pl` -137, `breakevens` [321.83], pop 0.71: it may remain ONLY as the second row of the picks table ("safer") - never the pick that the sizing, the ticket, the payoff, Telegram or a test uses |
| Golden ticket | ONE text (B9's) rendered for TWS and for moomoo: 2 contracts · "(you are paid; the most you can lose is fixed)" · the stop line with both figures ("about $242 if the stop fires, up to $1,580 ...") · rule stop and time stop "no order - the Positions tab watches it" · limit 2.10 working down to 2.00 · chart-stop exit "LRCX last <= 336.20", "Trigger outside RTH: No" |
| Dip toggle | `dip=True` condition at **341.92** with the crash sentence |
| Payoff | chart stop 336.2 -> **-120.7** today; rule stop at **332.72** -> **-158**; `r_dollars` **158**; legend "chance of keeping it 75% · model estimate 73%" |

**`isrg.json` - the debit fixture (step 2)**

| Item | Value |
|---|---|
| Chart | entry **405.81** · ATR **11.54** · stop **394.27** (entry - `STOP_ATR 1.0` x ATR) · target **428.89** (`TARGET_R 2.0`; the user's sheet said 429.14 - noted, **428.89** is used) · uptrend with a fresh setup |
| Earnings | **Oct 21** - INSIDE the Nov and Dec expiries (the earnings rows of II.2.7 / II.2.14 are exercised on this fixture) |
| IV | IV rank **41** (`basis = rank`) - mid: `gates.mid` True, `gates.buy` False, so `buy_call` is rejected `expensive` while `bull_call` fits `mid_or_buy` |
| Recommended | `bull_call` Dec **395/430** (B8.2; the short strike forced to the chart target's strike, soft note x0.9); sizing 2/1/6 -> 1 contract under house prefs |
| Supplementary worked example (NOT the fixture) | C5.2's single Dec **400 call at 34.30** stays as a `buy_call` worked example under the SAME entry / ATR / stop / target; its old 405.89 / 11.62 / "76 DTE" figures are corrected to **405.81 / 11.54** and the **Dec 18** DTE counted from the fixture's `as_of`; breakeven 434.30; its payoff DOES carry the rule-stop hline and marker at `-0.5 x 34.30 x 100` (the long block's `premium_stop_pct` 50, R1) |

### II.3 Module map

Paths relative to `dashboard_tst/`. "Step" = the II.4 build step the file first lands in; a
file listed with several steps grows in each.

**New files**

| Path | Purpose | Spec | Step |
|---|---|---|---|
| `alembic/versions/f4a5b6c7d8e9_options_module.py` | the ONE migration: nine tables + the `option_spreads` copy step | A2.2, B7.1 | 1 |
| `app/services/option_data.py` | `ChainSource` interface, `Chain` / `ContractRow`, `CboeSource`, `AlpacaSource`, `BridgePayloadSource`, `source()`, `fetch_chain()` with fallback, `Chain.legs()` / `as_legacy()` | A1 | 1 |
| `app/services/option_metrics.py` | pure: `hv`, `atm_iv_by_expiry`, `iv30_constant_maturity`, `iv_rank_pct` (+ `state`), `term_structure`, `skew25`, expected move, days to earnings, `all_for(chain, bars, earnings, iv_series)` | A3 | 1 |
| `app/services/option_store.py` | ORM reads / writes: `replace_snapshot`, `upsert_iv_daily`, `upsert_signal`, `signal`, `bootstrap_iv`, `backfill_from_iv_history`, `prune`, `latest_chain`, `latest_snap_on`, `iv_series`, `basket_universe`, `card_for`, `basket_rows_for` | A2.4, A4.6, A5.3, A6.1 | 1 |
| `app/services/option_prefs.py` | the ONE rules schema + presentation columns, `HOUSE`, `read / clean / write / reset / for_strategy / family_of / defined_risk / prefs_hash / distinct_hashes`, `TABS`, `PICK_FIELDS` | B4.1 + D3.1 | 1 |
| `app/services/option_nightly.py` | `run_nightly(db, ...)`, `refresh_symbol(db, sym, user)`, the per-symbol pipeline (named by A; not in the contract's list - A's name stands) | A4 | 1 |
| `app/services/option_engine.py` | `compute(chain, metrics, state, prefs) -> {status, headline, setup, iv, strategies, picks, computed_ms, engine_version}` (gauge -> `recommend` -> `pick` per built fit -> `headline`) and `ENGINE_VERSION`; B's `signal_for` IS this function and B's file table lists the module (II.5 #14) | A4.2, A5.1, B9 `signal_for` | 1 |
| `app/services/opt_constants.py` | the named constants of II.2.3 (B's name) | B0.3 | 1 |
| `app/services/opt_legs.py` | `norm_leg(row, *, unit, ...)`, `stored_leg`, `chain_view`, `by_expiry`, `dte_of`, `nearest_strike`, mid / width / liquidity helpers lifted from `bull_put` (B's name) | B0.2 | 1 |
| `app/services/premium_gauge.py` | `gauge(...) -> iv dict` with the three gates (B's name) | B1 | 1 |
| `app/services/chart_state.py` | `read(symbol, *, bars, long_bars, today, expiries) -> ChartState` (the ONE spelling - never `at=`, never a positional `atr`; ATR is computed inside from the bars): ONE `ema_setup.analyze(..., at=expiries)` call, `structure.classify`, the bear-side mirrors, earnings, the plan, the stored `setup` projection (B's name) | B2 | 1 |
| `app/services/clock.py` | `_us_session_open(now=None) -> bool`: weekdays 09:30-16:00 ET excluding the NYSE holidays the calendar service already knows (static list fallback); used by the ticket's "Refresh first" check and `_older_than_last_close` (II.2.5, II.6 #12) | II.2.5 | 1 |
| `app/services/mirror_setups.py` | `mirror_bars`, `find_resistance_reject`, `find_breakdown` only | B2.4, C2.3 | 1 (bear_call needs them - II.5) |
| `app/services/strategy_rules.py` | THE home of `STRATEGY_KEYS` (the user's order: `buy_call, buy_put, bull_call, bear_put, leaps_call, diagonal_call, bull_put, bear_call, iron_condor, calendar` - tie-break order), `StrategyRule`, the ten rows, `recommend()`, `earnings_block()`, `CURRENT_STEP`, reason vocabularies (B's name) | B3 | 1 (rows) · 2 / 3 / 4 (`CURRENT_STEP` bumps) |
| `app/services/strike_picker.py` | `pick(strategy_key, chain_view, chart, gauge, prefs, *, today=None) -> PickResult` - pure, no NLV (sizing is `option_sizing.size` afterwards); enumerators per family, constraints, liquidity, scoring, POP, `words()`, the degenerate cases (`reason_key` vocabulary of II.2.8) | B4 | 1 (credit_vertical) · 2 (debit_vertical, long) · 3 (condor) · 4 (leaps, time) |
| `app/services/option_sizing.py` | `size(pick, nlv, prefs) -> {contracts, by_chart_stop, by_gap, by_notional, stop_t_days, stop_iv, max_loss_pct_nlv, nlv_source in {live, prefs, None}, line, ...}` (the II.2.4 dict) | B5 | 1 |
| `app/services/order_ticket.py` | `build(pick, setup, prefs, *, dip=False, rejection=None, now=None) -> Ticket` (`ticket["rejection"]` = line 1 of both renderings), `render(ticket, broker)`, `TicketRefused` | B6 | 1 |
| `app/services/option_exits.py` | `mark`, `grade`, `sweep` + the per-family rule table | B7 | 1 (earnings row, credit_vertical) · 2 (debit_vertical, long) · 3 (condor) · 4 (leaps, calendar, diagonal) |
| `app/services/option_words.py` | every member sentence: `headline(setup, iv, strategies) -> str`, `pop_words(pop, pop_kind)`, `rule_words`, `chip_row`, `gauge`, `iv_rank_words` (the ONE IV-unknown string of II.2.6, no second variant), `sizing_line`, `idea_short`, `earnings_state`, `age_badge`, `et_clock`, the greek / rate words | D2.7 | 1 |
| `app/services/telegram.py` | the sender with a `chat_id`, `answer_starts`, `send_code` | D4.1 | 1 |
| `app/services/telegram_push.py` | `run(db, *, as_of, dry_run)`, `idea_key`, the guards, the dedupe, the message | D4.3-D4.4 | 1 |
| `app/services/job_runs.py` | `start / finish / latest / missed` over `option_jobs` | D6 | 1 |
| `app/services/payoff.py` | the payoff engine (II.2.9) | C3 | 1 |
| `app/templates/_payoff_chart.html` | the SVG pane + hover + `$|R` toggle | C4 | 1 |
| `app/services/trend_line.py` | `find`, `value_on`, `bounce`, `overlay` | C1 | 2 (the line alone may ship in 1) |
| `app/services/range_box.py` | `find`, `resistance_only`, `overlay` | C2 | 3 |
| `app/routes/options_page.py` | every route of II.2.18, the context builders `_basket_context / _card_context / _picks_context / _rules_context`, `_chart_overlays`, `_nlv_for`, `LiveIn`, `BasketImport` | D1 | 1 |
| `app/templates/options.html` | the shell: status strip, basket, Ideas / Positions tabs, `#optPane`, the My rules `<details>`, the page script | D2.1 | 1 |
| `app/templates/_options_basket.html` | the basket column / chip strip, import buttons, screener suggestions | D2.2 | 1 |
| `app/templates/_options_card.html` | header, headline, gauge, chips, "What has to happen", `#optChartBody`, `#optPicks` | D2.3 | 1 |
| `app/templates/_options_picks.html` | banner rules, the strike table, the sizing line, `#optPayoff`, `#optActions`, the chain expander | D2.5 | 1 |
| `app/templates/_options_chart.html` | sets the `chart_*` variables and includes `_price_chart.html` | D2.4, C4.5 | 1 |
| `app/templates/_options_rules.html` | the My rules drawer (five tabs, override dots, safety sentences, the exit lines, the Telegram rows) | D3.2 | 1 |
| `app/templates/_options_ticket.html` | the TWS / moomoo tabs, Refresh-first banner, footer lines, broker notes | D2.8 | 1 |
| `app/templates/_options_status.html` | the honesty strip | D6 | 1 |
| `app/templates/_options_chain.html` | the full-chain expander table (gamma shown only here) | D1.15 | 1 |
| `app/templates/_options_positions_tab.html` | the Positions board over `option_trades`, check drawer, close form (D5.1's "Track a trade by hand" form is OUT of v1 - deleted from the step-1 template, noted for step 2) | D1.14, D5.1 | 1 |
| `deploy/options_nightly.py` | the Hermes script (mirrors `deploy/spread_scan.py`) | A4.1 | 1 |
| `deploy/setup_options_nightly_task.ps1` | registers `TST-Options-Nightly` 07:15 MYT, 30 min (mirrors `setup_spread_scan_task.ps1`) | A4.1 | 1 |
| `requirements-dev.txt` | pytest (dev only) | A8 | 1 |
| `tests/` + `tests/fixtures/options/` | the pytest tree; fixtures `cboe_MSFT_small.json`, `chain_bs()`, `bars_synth()`, the golden `lrcx.json` / `isrg.json` (II.2.19) and the calendar fixture | A8, B9, C6, D8.2 | 1 |
| `tests/test_option_data.py` | A8's synthetic cases | A8 | 1 |
| `tests/test_option_engines.py` | B9's cases (B9 names no file; name chosen here) | B9 | 1-4 |
| `tests/test_payoff.py` | C6.3 identities | C6.3 | 1 |
| `tests/test_options_page.py` | D8.2 cases | D8.2 | 1 |
| `tests/test_trend_line.py` | C6.1 synthetic series | C6.1 | 2 |
| `tests/test_range_box.py` | C6.2 cases | C6.2 | 3 |

**Modified existing files**

| Path | What changes | Spec | Step |
|---|---|---|---|
| `app/models.py` | append the nine models: `OptionBasket`, `OptionChainSnapshot`, `IVDaily`, `OptionSignal`, `UserOptionPrefs`, `OptionJob` (A), `OptionTrade` (with the `meta` JSON column), `OptionTradeCheck` (B), `OptionIdeaPush` (D); declare the one-to-one `User.option_prefs` (`UserOptionPrefs.user` backref, `uselist=False`) | A2.1, B7.1, D1.12 | 1 |
| `app/config.py` | `Settings` gains `options_source`, `options_fallback`, `options_snapshot_days`, `options_full_days`, `alpaca_feed` | A1.2 | 1 |
| `app/.env.example` | the five `TST_OPTIONS_*` / `TST_ALPACA_FEED` keys documented | A0 | 1 |
| `app/services/option_quotes.py` | parser keeps `rho`, `last_trade_price -> last`, `bid_size`, `ask_size`, `prev_day_close -> prev_close` and the non-`options` keys as `out["header"]` (additive, lines 158-166) | A1.3 | 1 |
| `app/services/ema_setup.py` | `analyze(bars, long_bars=None, *, at=())`: `tl` / `tl_bounce` (2) and `rng` (3) fields, `times` kept on the dict, `t1` (2) / `r1` (3) in `COND_KEYS / COND_LABELS / COND_DEFAULT / COND_WEIGHT`, `conditions()` and `rank()` chips (`tl`, `tlx`, `tlb`, `rng`) | C1.7, C2.5 | 2, 3 |
| `app/templates/_price_chart.html` | item 5: `SPREAD.legs` / `SPREAD.breakevens` (old `{short, long, breakeven}` still accepted) + `window.thChartSetStrikes(spec)` (1); items 1-4, 6: `var TL`, autoscale, hoisted `snapTime`, the TL block, `__paintTL` on the tail (2); the RANGE block (3) | C4.4, D2.4 | 1, 2, 3 |
| `app/templates/base.html` | the four `--po-*` tokens dark + light (C4.2); the nav badge -> `/options/badge`, href check, two chips (D1.1); light-theme teal / violet inks (D2.10) (1); the `tl` / `tlx` / `tlb` chip inks (C1.7) (2) | C4.2, D1.1, D2.10 | 1, 2 |
| `app/main.py` | import `options_page`, add it to the `templates.env.globals` tuple, include it ABOVE the legacy options router with `require_menu("options")`, widen the three old routers' guards | D1.1 | 1 |
| `app/menus.py` | append `("options", "Options", None, "/options")`; `HIDDEN_KEYS` unchanged | D1.1 | 1 |
| `app/services/glossary.py` | one `_add({...})` group for the option terms | D2.7 | 1 |
| `app/__init__.py` | `4.126 -> 4.127` (then a bump per step) | D9 | 1-4 |
| `bridge/ibkr_bridge.py` | `/iv?symbol=X&series=1` returns `series` in PERCENT (<= 400 points, oldest first); `server_version -> TradeHunterIBKRBridge/1.6` | A4.6, D1.10 | 1 |
| `app/templates/_ivscan_list.html`, `_curated_list.html`, `_sector_basket.html`, `_sector_symbols.html` | the `tl` / `tlx` / `tlb` chip colours where `supx` is handled (+ `rng` in 3) | C1.7, C2.5 | 2, 3 |
| `README.md` | changelog entry per release (folder convention) + a Contents line for `tests/` and the new modules | all | 1-4 |
| `OPTIONS_MODULE_DESIGN.md` | the status line of this Part II -> "building, step N" | D9 | 1-4 |
| `/finviz` admin Data Ingest page | optional: one "Options nightly" row from `/options/badge` | A4.3 | 1 (if cheap) |

**Untouched on purpose:** `app/routes/options.py` (incl. `POST /options/track` and
`_options_tab.html`), `routes/ivscan.py`, `routes/spreads.py`, `routes/portfolio.py` (guards
change only in `main.py`), their templates, `app/services/spread_monitor.py`,
`app/services/bull_put.py`, `app/services/spread_scan.py` (keeps calling `option_quotes`
directly), the `option_spreads` table, `deploy/iv_seed_ibkr.py`, `deploy/spread_scan.py`.

### II.4 Build plan

Each step ends with: the smoke check, `git status`, the README changelog entry, commit + push
(laptop), the Hermes deploy (the canonical script in CLAUDE.md: `cd C:\trading-skills; git pull
--ff-only; free port 8000; schtasks /End /TN TST-Dashboard-Web; Start-ScheduledTask
TST-Dashboard-Web; Invoke-RestMethod http://localhost:8000/status`), and a README "Tested:" line.
Engines land with their unit tests in the same commit; the member-visible surface (dashboard
rule) lands in the same step as the engine it shows.

#### Step 1 - credit spreads (bull put + bear call), the payoff chart, the page skeleton, tracking, Telegram

| Item | Content |
|---|---|
| Files touched | every "step 1" row of II.3: the migration; `models.py`; `config.py` + `.env.example`; `option_quotes.py`; `option_data`, `option_metrics`, `option_store`, `option_prefs`, `option_nightly`, `option_engine`, `opt_constants`, `opt_legs`, `premium_gauge`, `chart_state` (`tl` / `rng` read as None until steps 2 / 3), `mirror_setups`, `strategy_rules` (all ten rows, `CURRENT_STEP = 1`), `strike_picker` (credit_vertical enumerator + constraints + liquidity + scoring + words), `option_sizing`, `order_ticket`, `option_exits` (earnings row + credit_vertical rows; generic `mark / grade / sweep`), `option_words`, `telegram`, `telegram_push`, `job_runs`, `payoff` + `_payoff_chart.html`; `options_page.py` + the ten templates; `main.py`, `menus.py`, `base.html` (tokens, badge, inks), `_price_chart.html` (item 5 + `thChartSetStrikes`), `glossary.py`, `__init__.py`; `bridge/ibkr_bridge.py` 1.6; `deploy/options_nightly.py` + `setup_options_nightly_task.ps1`; `requirements-dev.txt`, `tests/` + fixtures, `test_option_data.py`, `test_option_engines.py` (credit cases), `test_payoff.py`, `test_options_page.py`; `README.md` |
| Migration | `f4a5b6c7d8e9` - nine tables + the open-`option_spreads` copy (this step ONLY; later steps add no migration) |
| Member can | build a basket (typed / paste / watchlist / IV Rank list / TWS scan / scanner / screener / positions); open a card the morning after the nightly run (headline, gauge with its basis, the ten chips with reasons, "What has to happen", the chart with EMA 20/50/200 + support + strike lines, three bull put / bear call picks in collect / risk / chance words, the sizing line with both figures, the payoff SVG with both stops, the order ticket in both renderings, Track this -> the Positions tab graded from the stored chain); Refresh (Cboe, ~2 s); Live (TWS, in-request, IV bootstrap); edit every rule tab (all five ship now; the condor / time / debit fields are stored but their pickers arrive later and their chips read "· not available yet"); opt into Telegram ideas. Every other strategy is stored, shown and explained, never recommended |
| Tested (synthetic) | A8: the Cboe parser on `cboe_MSFT_small.json` (five new fields; 0.0/0.0 -> None; iv 8.3 -> None; the deep-ITM 3.1099 kept; `as_of` -> UTC; `legs()` key-for-key); `AlpacaSource` on recorded pages (OCC join, `oi`, `expiration_date_lte` present); `BridgePayloadSource` (43.1 -> 0.431, `oi_ok=False` -> None, 401 rows capped, far strike dropped); `hv`, `atm_iv_by_expiry`, `iv30_constant_maturity`, `iv_rank_pct` states for n = 0/19/20/59/60/251/252, `term_structure` 42.0/40.4 = 1.0396, `skew25`; store round trips (identical counts, EOD over intraday, `bootstrap_iv` never overwriting a `cboe` day, percent stored as-is, bounded input rejected); `prune`; `prefs_hash` (house == `{}` == an equal-to-default override; `nlv` / exit-line / telegram edits leave it unchanged; `distinct_hashes`); `card_for` / `basket_rows_for` (sizing differs by NLV with no write; `contracts=0` never 1; `not_checked` vs `no_strike_passes`; `stale` one session later); `backfill_from_iv_history`; the migration on an empty DB, on a `create_all` DB, with 3 open + 2 closed spreads (3 `option_trades`, `option_spreads` byte-identical, re-run still 3, `downgrade -1` clean); basket import 70 -> 60 + `over_cap 10`. B9: `norm_leg` by unit; `premium_gauge` (62 SELL / 24 BUY / 41 x 1.21 SELL / 41 x 0.95 NEUTRAL / n = 19 provisional / n = 40 percentile / n = 118 "over 118 days" / n = 0 UNKNOWN, exact sentences); `mirror_setups`; `chart_state` (one `analyze` call, LRCX 336.2, ISRG 394.27 / 428.89 +/- 0.3); `strategy_rules` (ten rows, `reason_key` vocabulary, <= 2 `shown`, rank 45 / 62 / 24 cases, `defined_risk_only`, an unbuilt fit never `recommended`, no "step" in any string); `option_prefs`; `strike_picker` bull_put = the golden `lrcx.json` table (II.2.19: the 330/320 at 2.10 first, `max_loss 790`, `chart_stop_pl -120.7`, `rule_stop_pl -158`, `breakevens [327.90]`, `pop 0.75`; the 325/315 second as "safer", `max_loss 683`, `chart_stop_pl -99`), the constraint removes a 340P (above the 336.2 pad), `no_band` / `credit_floor` / `thin` (the `degenerate.reason_key` vocabulary), bear_call = the mirror; `option_sizing` (LRCX 8/2/10 -> 2, ISRG 2/1/6 -> 1, notional binds at 5% / x3, 0 by all caps, `nlv None`, `stop == entry`); `order_ticket` = the ONE golden TWS + moomoo text (B9's, II.2.19: 2 contracts; the jargon line; the stop line with $242 / $1,580; "no order - the Positions tab watches it"; no condition by default; `dip=True` 341.92 + the crash sentence; "Trigger outside RTH: No" + the RTH sentence in both; short leg first + naked-put sentence; Market recommended; `refresh_first` through `clock._us_session_open`; `TicketRefused` on `earnings_inside`; rejection as line 1; `contracts 0` -> "-"); `option_exits` (every credit row fires and one tick short does not; the earnings row for every family; a `bear_call` marks against the CALL chain; the migrated spread's verdict == `spread_monitor.snapshot`'s); `telegram_push` (headline + must_happen + "about 75% chance of keeping it (estimate)" + no count; the five guards; dedupe 0.5 ATR vs 1.2 ATR; first line; 7 -> 5 + "and 2 more"; `POST /options/telegram` `request_code` -> `verify` round trip). C6.3: parity, `Leg.from_dict` signs, `normalise_iv` by unit (3.1099 kept; no-unit raises), 790 = width - credit == `spread_math`, the mirrored bear call, T+0 at spot = 0, the open-position mark == `option_exits.mark`, `iv_bump`, breakevens a list, the grid holds every strike, chart stop 336.2 -> -120.7, rule stop 332.72 -> -158, R 158, `R` arrays == `$` / 158, failure modes. D8.2: all rows of that table except the trend-line / range ones |
| Tested (live, dev DB then Hermes) | the D8.1 walkthrough steps 1-26 (steps 6-8 without the trend line / range overlays; Live with bridge 1.6 on the laptop only); `deploy\options_nightly.py NVDA LRCX MSFT KO -v` (MSFT ~3,700 rows / 23 expiries, `iv30_src=cboe`, `atm_iv30` within ~2 pts, HV plausible, one signal row per hash, a job row with `pushed`, `/options/badge stale=False`, re-run unchanged with `pushed=0`); `--source alpaca` on the same four with the paper keys; pacing 30 tickers at 1.5 s -> no 429; `--telegram-dry-run` in a dev chat after the handshake; the failure drills (typo source -> exit 1; `ZZZZ` greys; a killed run -> "did not finish (2 of 4)") |
| Hermes deploy | the canonical script; then, ONCE, as the task user: `cd C:\trading-skills\TradeHunter\dashboard_tst; .\deploy\setup_options_nightly_task.ps1` (registers `TST-Options-Nightly` 07:15 MYT, 30-min limit, `logs\options_nightly.log`); add `TST_OPTIONS_SOURCE=cboe` (and `TST_OPTIONS_FALLBACK`, `TST_ALPACA_FEED` if used) to `app/.env`; put `telegram.env` in the vault for the push (or leave it unconfigured: logged once, skipped); confirm `alembic current` = `f4a5b6c7d8e9` from the app log, the strip green ("job ✓ 07:17 MYT") after the first scheduled run, and amber "tonight's run missed" after stopping the task for a day. Members on their own PCs restart `bridge\start_ibkr_bridge.bat` to get bridge 1.6 (the page says "older than 1.6" until they do) |

#### Step 2 - the debit family (buy call / buy put / bull call / bear put), the trend-line engine, chart-based exits

| Item | Content |
|---|---|
| Files touched | `trend_line.py` (new); `ema_setup.py` (`at=`, `tl` / `tl_bounce`, `times`, `t1`, chips); `_price_chart.html` items 1-4 and 6; `base.html` + the four list templates (chip inks); `chart_state.py` (`trendline_bounce` setup, `tl` read); `strategy_rules.py` (`CURRENT_STEP = 2`); `strike_picker.py` (debit_vertical + long enumerators, the chart-target short strike, the soft band, `theta_pct_max`, `payoff.pop` for `pop_kind = profit`); `option_exits.py` (debit_vertical + long rows); `order_ticket.py` (debit variant: limit debit + ceiling, chart target close_at, the `breakout_retest` / fresh-bounce dip cases); `options_page.py` + `_options_chart.html` (`chart_levels` for debit / long; `chart_trendline`); `_options_picks.html` (pay / reward wording); `tests/test_trend_line.py`, `test_option_engines.py` (debit cases), `test_payoff.py` (ISRG + the `buy_call` rule-stop assertion); `__init__.py`, `README.md` |
| Migration | none |
| Member can | see the automatic trend line (touch count, broken / warning state) on the card chart, the Sector / IV Rank / Curated lists (`tl` / `tlx` / `tlb` chips) and the `t1` condition switch; get buy call / buy put / bull call / bear put recommended with the Entry / SL / PT lines read-only on the chart, the T+0 target read on the payoff pane, the debit ticket (no condition; chart-stop exit; "sell if the option loses 50%"), and the chart-stop / target / theta / time exits on the Positions tab |
| Tested | C6.1: the 3-touch series (4 touches, slope 0.19, channel), broken line -> None, break inside RECENT -> `broken=True`, tie lows merged, flat walk -> None (`MIN_RISE_ATR`), downtrend mirror, bounce at the line / 1 ATR above, `at=` passthrough and exactly one `find` per `analyze`, timing median < 1 ms, the edge probes, `value_on` weekday arithmetic; live names LRCX, MA, NVDA, KO, ISRG eyeballed; B9: `strategy_rules` rank 24 -> buy_call > bull_call with bull_put `cheap_options`; `strike_picker` bull_call (short forced to 430, soft note x0.9, POP from `payoff.pop`), buy_call theta cap, the ISRG picks table; `option_exits` debit / long rows; `order_ticket` debit golden text; C6.3 on the golden `isrg.json` (entry 405.81 / stop 394.27 / target 428.89, the bull_call 395/430) plus the `buy_call` worked example (the Dec 400 call at 34.30 under the same levels: breakeven 434.30, loss at SL -645 today, PT +1,496 today / -516 at expiry, R 645 -> PT +2.32 R, and the rule-stop line PRESENT at -0.5 x 34.30 x 100 - the "no rule-stop line" assertion is struck, R1); D8.2 the chart fragment for a `long` strategy (`chart_levels`, no `chart_setup_seed`), `chart_trendline` carrying `slope_per_bar` / `n_touches`; browser: C6.4 items 1, 2, 4, 7, 10 and D8.1 step 8's chart switch |
| Hermes deploy | the canonical script only. Release note in README: `t1` joins `COND_KEYS`, so every `sym_conds` reader (Sector & Industry, Curated, IV Rank) shows one more switch |

#### Step 3 - iron condor (the range / sideways detector)

| Item | Content |
|---|---|
| Files touched | `range_box.py` (new); `ema_setup.py` (`rng`, `r1`, the `rng` chip); `_price_chart.html` (the RANGE block); `base.html` + the list templates (`rng` chip ink); `chart_state.py` (`trend = sideways` iff `rng.sideways`, the `range` setup, the neutral plan); `mirror_setups.py` (reads the resistance level from `rng` / `resistance_only`); `strategy_rules.py` (`CURRENT_STEP = 3`); `strike_picker.py` (condor enumerator, the `zone_low[0]` / `zone_high[1]` constraint, `POP_both`); `option_exits.py` (condor rows); `order_ticket.py` (four legs, "one iron condor, LIMIT x credit", the stop on either edge, the naked-leg sentence once per side); `options_page.py` (`chart_range`; `chart_spread.legs` for four legs); `tests/test_range_box.py`, `test_option_engines.py` (condor cases), `test_payoff.py` (condor = two verticals); `__init__.py`, `README.md` |
| Migration | none |
| Member can | see a sideways verdict with the range drawn (both edges, touch markers, `S->R` flips) and the `r1` switch; get the iron condor recommended on a flat chart with both short strikes outside the zones, its two-sided chance of keeping it, the condor ticket and the range-stop / either-side-delta exits; the calendar's "sits mid-range" test now has its input |
| Tested | C6.2: the clean range (low ~94, high ~106, `sideways=True` with flat EMAs, `zone_low[0] < low`), S->R flip, breakout -> None, trending -> never `sideways=True`, narrow -> None, the negation identity; `range_box` cost <= 10 ms per ticker inside `setups_for_many` on a cold cache (60 symbols <= +0.6 s); B9: `chart_state` sideways ONLY on the range fixture (DNOW / GILD / HSY read sideways through `rng`), `strike_picker` condor on the range fixture, `option_exits` condor rows; C6.3 condor identity; browser: C6.4 item 10's `r1` switch, a condor card with the range lines, the four strike lines and the two breakevens |
| Hermes deploy | the canonical script only. Release note: `r1` joins `COND_KEYS` (second extra switch) |

#### Step 4 - calendar, diagonal call, LEAPS (term structure + long-dated expiries)

| Item | Content |
|---|---|
| Files touched | `strategy_rules.py` (`CURRENT_STEP = 4` - the last "· not available yet" chip disappears); `strike_picker.py` (leaps enumerator with the `extrinsic <= extrinsic_pct_max/100 x spot` cap; calendar front x back pairs with per-pair front IV >= back IV, the sit strike, `payoff.pop` at the front expiry; diagonal long / short windows, `spot < short_strike <= resistance`, the width + credit >= long cost safety rule); `option_exits.py` (leaps, calendar, diagonal rows; `sweep` fetching `setup_for(sym, deep=True)` for underlyings holding a LEAPS / diagonal); `order_ticket.py` (two expiries per leg, "Calendar" / "Diagonal" presets, no dip toggle); `payoff.py` (the two-expiry caption; already generic); `options_page.py` (`chart_spread.legs` across two expiries; `chart_levels` for leaps); `test_option_engines.py` (leaps / calendar / diagonal cases), `test_payoff.py` (the calendar fixture); `__init__.py`, `README.md`. The data side needs nothing new: the snapshot has kept every expiry and `iv_daily` has carried `iv_front / iv_back / term_ratio / iv_by_expiry` since step 1 |
| Migration | none |
| Member can | get a LEAPS call on a weekly uptrend ("behaves like 79 shares"), a calendar when the near month is dearer than the far one and price sits mid-range, a diagonal call under a resistance; see the dome-shaped expiry line and flat today line on the payoff pane; the roll-date / delta-drift / 40% premium stop / weekly-trend exits on the Positions tab |
| Tested | B9: leaps (10% of SPOT cap passes ISRG 340C, fails LRCX -> `extrinsic_cap`; the ISRG LEAPS sizing -> 0 by all three caps with the exact note), calendar (only front x back pairs with front IV >= back; POP == the lognormal mass between the breakevens +/- 1%), diagonal (the safety rule excludes the B8.3 pair by $2.47 and admits 360C), `option_exits` leaps / calendar / diagonal rows (the 40% loss line fires while the weekly stack holds; the diagonal's long leg inherits it; `sweep` calls `setup_for(deep=True)` only for those symbols); C6.3 calendar (`max_loss == debit x 100`, `min(ys) >= -max_loss - 5`, two breakevens 93.90 / 107.88, POP 54%); D8.2 the payoff route for a calendar (horizon = the front expiry) and for `leaps_call` (the rule-stop hline at `-0.4 x debit x 100`, per II.6 item 16); browser: C6.4 item 8 (calendar card), an ISRG LEAPS card with "not even one contract" at 1% of $100k |
| Hermes deploy | the canonical script only; the nightly job's per-ticker time rises slightly (the extra `setup_for(deep=True)` call per LEAPS / diagonal underlying) - watch `logs\options_nightly.log` stays under the 30-minute limit |

Later (not scheduled): covered call / cash-secured put (the credit picker with a different
capital calculation), paper auto-track (`option_trades.paper` is reserved), a per-ticker news
line, the removal release for `/ivscan`, `/spreads`, `/portfolio` (drop `iv_scan_items` and
`option_spreads` after a final copy), and admin-editable house defaults (`HOUSE` moves to a
single-row table; `read()` does not change).

### II.5 Resolved decisions

The ten of §10, locked 2026-10-03 with the user's go-ahead ("ok now we proceed with the full
design"):

| # | Decision | Resolution |
|---|---|---|
| 1 | Data source of record | Cboe delayed primary (`TST_OPTIONS_SOURCE=cboe`), Alpaca wired behind the same `ChainSource` interface as the config fallback (`TST_OPTIONS_FALLBACK=alpaca`, `TST_ALPACA_FEED`); the IBKR bridge is the member-side Live read only, never persisted |
| 2 | Catalog scope for v1 | all ten strategies in the rule table and the signal from step 1; pickers phased 1-4 (II.4); an unbuilt rule is `also_fits · not available yet`, never recommended |
| 3 | House defaults | yes - `option_prefs.HOUSE` in code (admin-editable later = a single-row table, same merge); members on house defaults share one signal row |
| 4 | IV rank source | server-side from `iv_daily` with `state` / `basis` and the day count said out loud (percentile from 20 days, rank from 60, "over the last year" from 252); the Live press bootstraps a year from bridge 1.6 (PERCENT, as-is, never overwriting a server-read day) |
| 5 | Replace or add | the Options page replaces IV Rank / Spread / Positions; the three routers stay registered, hidden from the nav, with their guards widened to accept the `options` grant, until a later removal release |
| 6 | LLM | none in the decision path; the headline is a deterministic template composed at write time; an optional explainer paragraph is a later idea |
| 7 | The stop on the payoff chart | BOTH drawn and labelled for EVERY family: the chart stop (`plan.stop`, 336.2 on the fixture) and the rule stop (B5.2's table per family - credit 20% of max loss, leaps / diagonal 40% of the premium, long / debit / calendar 50%); the member sees which fires first |
| 8 | The "today" curve | kept, with the one-sentence caption (II.2.9) under every chart |
| 9 | Wording | "about a {p}% chance of keeping the credit / of profit" with the estimate caveat (II.2.8); the chip row shows the recommended, every also-fits, and up to two near-miss rejects with their reason; the rest behind "other strategies" |
| 10 | Automation level | see-and-approve; the Telegram push ships in step 1 (per-member opt-in, handshake, guards, dry-run first); paper auto-track later |

Integrator calls (2026-10-04) - where the parts still differed after reconciliation, the
following stands:

| # | Call | Why |
|---|---|---|
| 11 | Positions store = `option_trades` + `option_trade_checks` from step 1 for every strategy; `option_spreads` read-only legacy; `spread_monitor` untouched | a bear call cannot live in a put-priced monitor; one store, one grader |
| 12 | Entry condition OFF by default; "Enter on the dip" is an explicit toggle with the crash sentence; the chart-stop exit is the one conditional order pushed hard | a conditional entry fires on a crash through support |
| 13 | Routes = D's `verb/{symbol}` convention; `POST /options/track-idea` on its own path; the new router gated by `require_menu("options")` and registered before the legacy one | the legacy `POST /options/track` must stay ungated for the Watchlist pane |
| 14 | `app/services/option_engine.py` exists as the thin composer A calls (`compute(chain, metrics, state, prefs)`, `ENGINE_VERSION`); B's `signal_for(symbol, prefs)` (B9) is this function | A and the contract name it; B's file table listed the pieces but no composer |
| 15 | The field is `shared.gap_mult` (B's key); `GAP_MULT` is its name in prose and formulas | B owns the schema; D3.1's SCHEMA key `GAP_MULT` is the same field and is spelled `gap_mult` in code |
| 16 | `MAX_POSITION_PCT` is a constant (10.0), not a field; D3.1's `shared.max_position_pct` row does not exist | CLAUDE.md: `max_position_pct = 10% of NLV`, global, never override; B0.3 |
| 17 | The sizing dict's third cap is `by_gap` (B5.3); D0's `by_max_loss` is the same figure under B's name | B owns sizing |
| 18 | `option_prefs.read(db, user)` is the merge entry point (D's signature); `clean(raw)` is the pure merge (`HOUSE_HASH = prefs_hash(clean({}))`); A's `for_user(db, user)` and B's `read(user)` are this function | the routes have `db`; one name |
| 19 | `iv_hv_premium = iv30 / hv20` - a RATIO (1.21 on the golden fixture: 46.0 / 38.0), thresholds `IV_HV_RICH 1.10` / `IV_HV_CHEAP 0.90`; A's `iv_daily.iv_hv_premium` column comment ("vol points") and A5.2's example value 12.3 are superseded | B's gauge logic and D's "priced for 21% more movement" wording are ratio-based; a ratio is ticker-relative (CLAUDE.md), vol points are not |
| 20 | `iv_front` / `iv_back` per A3.4: front = the expiry nearest 30 DTE with dte >= 7, back = nearest 75 DTE with dte >= 45; B1.1's "60-90 DTE" wording is this rule | A owns the metrics |
| 21 | `iv.gates` = `{buy, sell_directional, sell_neutral, mid}` (B1.3); A5.2's example key `term_event` is not a gate (the term reason lives in `verdict_why`) | B owns the gauge |
| 22 | The Telegram settings live in `prefs["telegram"]` (D4.2), not in the `shared` block; B4.1's `shared.telegram` row describes the same dict and its drawer rows | D owns Telegram; never hashed either way |
| 23 | `payoff.build()` keeps C's signature (`strategy=`, `as_of=`, `premium_stop_pct=`, no `contracts=`) plus one addendum, `loss_fraction=0.20` (the member's `trade_prefs` credit stop), so the chart's rule stop equals B5.1's `rule_stop_pl`; D1.7's `family=`, `today=`, `contracts=`, `rule_frac=` call shape is replaced | C owns payoff; the pane is always per contract |
| 24 | `sizing.nlv_source in {"live", "prefs", None}` (B5.3); D's `"bridge"` is `"live"` | B owns sizing |
| 25 | `pick_state in {has_picks, no_strike_passes, not_checked}` (A5.3); D1.4's `picks / none / unchecked` are these three | A owns the read path |
| 26 | `basket_rows_for(db, user)` returns `dict[symbol, dict]` (D's usage), not a list | one batched read, keyed by symbol |
| 27 | `option_exits.sweep(db)` is step 4 of the nightly order (after the per-symbol loop, before prune and push) - absent from A4.2's numbering, present in B7.3 and D1.14 | the push must see tonight's checks; the badge counts them |
| 28 | `mirror_setups.py` lands in step 1 | step-1's bear_call needs `resistance_reject` / `failed_support`; without the module it never fits |
| 29 | `option_jobs.job` enum gains `telegram_poll` (D4.1's `answer_starts` offset row) | one job table |
| 30 | `setup.stop` / `setup.target` exist as flat aliases of `plan.stop` / `plan.target` (contract CHART STOP); A5.2's "do not exist as separate keys" sentence is superseded; `plan` stays the source | readers that want the flat names |
| 31 | `setup.quality` is an int 0-100 (B2.3) and `rng.pos_pct` is 0..1 (C2.1); A5.2's example values `"A"` / `57.0` are superseded | the owners' definitions |
| 32 | `order_ticket.build(pick, setup, prefs, *, dip=False, rejection=None, now=None)` (B6); D1.13's `p.enter_on_dip` / `t.first_line` are these two kwargs | B owns the ticket |
| 33 | `pick.chart_legs = [{strike, right, side, label}]` (C4.5) and the row's `data-strikes` JSON (D2.5 `pick.chart_spec`) = `{legs: chart_legs, breakevens, expiry, label}`, i.e. the `chart_spread` spec `thChartSetStrikes` takes | one shape for the strike overlay |
| 34 | `job_runs.start(db, job, run_on, source=None)` | A's call passes `source` |
| 35 | B9's test file is `tests/test_option_engines.py` | B names none |
| 36 | `option_words.pop_words(pop, pop_kind)` (B, C, D); A5.2's `pop_words(pick)` is a shorthand | one signature |

Integrator rulings, second pass (2026-10-04, after the verifier's cross-part read) - binding on all
five documents; where a part or this Part II said otherwise, it is corrected to these:

| # | Ruling | Where it now lives |
|---|---|---|
| 37 | **R1** the rule stop is drawn and `rule_stop_pl` computed for EVERY family (credit `loss_fraction` 0.20 x max loss; `leaps_call` / `diagonal_call` the leaps block's `premium_stop_pct` 40; `buy_call` / `buy_put` / `bull_call` / `bear_put` / `calendar` the long block's 50); II.4 step 2's "no rule-stop line" is struck; C3.9 / C5.2 / C6.3 / D8.2 read "present" | II.2.9, II.2.14, II.4 step 2, II.6 #16 |
| 38 | **R2** `payoff` `iv_bump` is a RELATIVE lift - `sigma_used = leg.iv x (1 + iv_bump)`, `STOP_IV_BUMP = 0.10` (`stop_iv 0.506 = 0.46 x 1.10`), one definition in `payoff.leg_value` | II.2.9, II.2.14, II.2.4 |
| 39 | **R3** the golden LRCX fixture `tests/fixtures/options/lrcx.json` - one set of numbers (spot 349.20, ATR 11.54, stop 336.2, target 375.2, IV30 46.0 / HV20 38.0 / 1.21, front 50.0 / back 45.0 / 1.11, rank 62 SELL; THE pick = Nov 20 330/320 at 2.10: 210 / 790 / [327.90] / pop 0.75 / -120.7 / -158; sizing 8 / 2 / 10 -> 2 contracts, "$242 ... $1,580 (1.6%)"); the 325/315 (683 / -99) only as the "safer" second row; II.2.6's old iv example (41.2 / 28.9 / 1.43 / 42.0 / 40.4 / 1.04 / "43% more") replaced | II.2.19, II.2.4, II.2.6, II.2.9, §3, §6e, §7 |
| 40 | **R4** the golden ISRG fixture `isrg.json`: entry 405.81, ATR 11.54, stop 394.27, target 428.89 (the sheet's 429.14 noted, not used), earnings Oct 21 inside Nov / Dec, IV rank 41, recommended `bull_call` Dec 395/430; C5.2's Dec 400 call at 34.30 stays as a SUPPLEMENTARY `buy_call` worked example (405.89 / 11.62 / 76 DTE corrected to 405.81 / 11.54 / Dec 18 from `as_of`) | II.2.19, II.2.14, II.4 step 2 |
| 41 | **R5** `iv_hv_premium` is the unitless ratio `iv30 / hv20` everywhere (`IV_HV_RICH 1.10`, `IV_HV_CHEAP 0.90`) - never "vol points" | II.2.6, II.2.12, #19 |
| 42 | **R6** `gates = {buy, sell_directional, sell_neutral, mid}`; `term_event` is not a gate | II.2.6 |
| 43 | **R7** `setup` carries the flat aliases `stop` / `target` (= `plan.stop` / `plan.target`) AND `plan = {entry, stop, target, r}`; `quality` int 0-100; `tl.warning` bool; `rng.pos_pct` 0..1 | II.2.6, II.2.10, II.2.11 |
| 44 | **R8** nightly order 1 snapshot, 2 metrics / `iv_daily`, 3 engines per hash, 4 `option_exits.sweep(db)`, 5 `option_store.prune(db, today)` (snapshots 90 d, `option_trade_checks` 90 d, `option_idea_push` 45 d), 6 `telegram_push.run(db, as_of=run_on, dry_run=...)`, 7 `job_runs.finish`; every "the push is step 5" reads step 6 | II.2.1, II.2.15, II.2.16 |
| 45 | **R9** one spelling per signature: `chart_state.read(symbol, *, bars, long_bars, today, expiries)`, `option_engine.compute(chain, metrics, state, prefs) -> {status, headline, setup, iv, strategies, picks, computed_ms, engine_version}`, `headline(setup, iv, strategies) -> str`, `strike_picker.pick(strategy_key, chain_view, chart, gauge, prefs, *, today=None) -> PickResult` (pure, no NLV), `option_sizing.size(pick, nlv, prefs)`, `payoff.build(...)` with its dict and the `levels=` element shape `{x, label, kind in {support, resistance, trend_line, target}}`, `order_ticket.build(pick, setup, prefs, *, dip=False, rejection=None, now=None)`, `card_for(db, symbol, user) -> dict \| None`, `basket_rows_for(db, user) -> dict[symbol, dict]` (decides `pick_state`), `job_runs.start(db, job, run_on, source=None)`, the `option_prefs` functions, `pop_words(pop, pop_kind)` | II.2.3-II.2.10, II.3 |
| 46 | **R10** `option_prefs.read()` returns the merged blocks + `telegram` (own key, never a SCHEMA field, never hashed) + `account {nlv, risk_pct, nlv_source}` + `_overridden`; `Field(default, lo, hi, kind, label, help, plain, step, unit)`, `kind in {num, int, bool, choice}`; the field is `gap_mult`; `max_position_pct` is NOT a field (`MAX_POSITION_PCT 10.0` in `opt_constants.py`); `PICK_FIELDS` liquidity = `(min_oi, oi_per_contract, max_leg_spread, min_leg_volume)`; `STRATEGY_KEYS` live in `strategy_rules.py` in the user's order (`buy_call, buy_put, bull_call, bear_put, leaps_call, diagonal_call, bull_put, bear_call, iron_condor, calendar`), `option_prefs` imports them, ties break in that order | II.2.3, II.2.7, II.3 |
| 47 | **R11** `defined_risk(key)` True only for `bull_put, bear_call, bull_call, bear_put, iron_condor`; `buy_call`, `buy_put`, `calendar`, `diagonal_call` are `none_inside` regardless of the member's rule; `leaps_call` `any`; D's `DEFINED_RISK = every strategy` is wrong | II.2.7, II.2.3 |
| 48 | **R12** `degenerate.reason_key in {no_chain, no_expiry, no_band, constraint, credit_floor, thin, theta_cap, extrinsic_cap, no_term, safety}`; a rejected strategies row has `score` / `why` / `must_happen` null; label "Buy LEAPS"; score = `priority + iv_fit (0-20) + setup_quality/5 + term 10 + structure 5 - 10 if unbuilt` | II.2.6, II.2.8 |
| 49 | **R13** `OptionTrade.meta` JSON (created by the migration) · `User.option_prefs` one-to-one · `option_jobs.job` incl. `telegram_poll` · `note = "migrated from option_spreads #<id>"` · `ideas_new` = this member's `OptionIdeaPush` rows with `sent_at >= prefs["options_seen_at"]` · deep link `public_url + "/options?symbol=SYM"` · house `credit_vertical` `roll_delta` 0.35 adjust / 0.40 close · "Track a trade by hand" OUT of v1 (step 2) · `POST /options/telegram` body `{action in {request_code, verify, quiet, pause, disable}, chat_id?, code?, pause_days?}` · basket add / remove form-encoded (`hx-vals`), import JSON · `_us_session_open()` in `app/services/clock.py` · import returns `{added, skipped, over_cap, total}` · ONE IV-unknown string and ONE bridge-too-old string · `nlv_source in {live, prefs, None}` · `pick_state in {has_picks, no_strike_passes, not_checked}` · ONE golden ticket text (B9's, 2 contracts) · `/options/chart` overlays from the STORED setup via `card_for`, never `setup_for` · `?trade=` payoff sigma from the latest `OptionTradeCheck` mids · `_options_chart.html` / `_payoff_chart.html` · tests `test_payoff.py` 1, `test_trend_line.py` 2, `test_range_box.py` 3, `test_option_engines.py` 1 · `option_engine.py` in B's file table · `spread_scan.py` untouched in every step · the NLV-missing sentence · `iv_front` nearest 30 DTE with dte >= 7, `iv_back` nearest 75 DTE with dte >= 45 | II.2.1, II.2.2, II.2.5, II.2.6, II.2.13-II.2.18, II.3 |

### II.6 Risks and open items

Genuinely unresolved; none blocks step 1 from starting.

| # | Item | Why it matters | Where it lands |
|---|---|---|---|
| 1 | Alpaca source not exercised live: snapshot pagination (`limit=1000`, `page_token`), the `/v2/options/contracts` OI join, the `expiration_date_lte` next-weekend default, the 200/min cap | the fallback must work the night Cboe fails; A8's recorded-response tests cover the shape, not the live service | step 1 live test `--source alpaca` with the paper keys |
| 2 | The Cboe endpoint is undocumented and may vanish or truncate | `ChainError` + the `partial` flag + the stale badge + the fallback are the mitigations; nothing proves truncation never happens | A7 failure drills; watch `option_jobs.detail` |
| 3 | `range_box` cost inside `setups_for_many` | 5-10 ms x symbols on a cold 15-min cache on the Sector / IV Rank list requests (60 symbols <= +0.6 s); fine while hidden behind the 2-year fetch, not fine if the cache warms differently | step 3 timing test; profile the first list request after a restart |
| 4 | The bear-side setup detectors (`mirror_setups`) are not prototyped | `find_resistance_reject` by negation should hold term by term; `find_breakdown` re-uses `support_bounce` internals without the bounce gate - the first real bear_call / buy_put reads need eyeballing | step 1 (reject) and 2 (breakdown) live names |
| 5 | The LEAPS weekly-trend exit needs ~10 years of bars | `sweep` adds one `setup_for(deep=True)` Yahoo call per LEAPS / diagonal underlying; counted in the budget but not measured | step 4; `logs\options_nightly.log` |
| 6 | SQLite size and lock contention | `option_chain_snapshot` is ~550 MB at 30 tickers, ~1.8 GB at 100; readers wait during the per-symbol commit (< 1 s); switch triggers: basket union > 40, `tst.db` > 2 GB, DB phase > 10 min | README note; Postgres via `TST_DATABASE_URL` when a trigger fires |
| 7 | Broker conditional-order mechanics are written from the earlier discussion, not re-verified in TWS / moomoo | B9's paper test ("legs resolve, the chart-stop condition accepted with Trigger outside RTH off") is the only check; moomoo's strategy ticket may not accept a condition at all | step 1 paper test on the laptop with the bridge up |
| 8 | Telegram `getUpdates` polling (`answer_starts`) vs the intraday bot's send-only use of the same token | two pollers on one bot token conflict; if the TradeHunter bot token is shared, `/start` answers may be eaten; a dedicated bot token in `telegram.env` is the clean fix | step 1 handshake test (D8.1 step 25) |
| 9 | Two nightly jobs on one Cboe CDN | `TST-Spread-Scan` 06:30 (~30 min, ~550 symbols) and `TST-Options-Nightly` 07:15 are sequenced, not coordinated; a long spread scan overlaps and the 429 backoff (`retries=3`) absorbs it | watch the first week's logs |
| 10 | The earnings date comes from `prices.fetch_next_earnings` (Yahoo, soft-fail None) | None vetoes the push and warns on the card; a WRONG date is not detected (M&A / offerings / FDA are not detected at all, §3) | later: a per-ticker news line (Alpaca news) |
| 11 | IV30 calibration after a source switch | Cboe's `iv30` vs our `atm_iv30` may drift > 1.5 pts on some names; the footnote is the only surface | A3.2 calibration log; revisit when Alpaca becomes primary |
| 12 | The "Refresh first" check needs a holiday-aware "US session open" helper - SETTLED (R13): `_us_session_open()` in `app/services/clock.py`, weekdays 09:30-16:00 ET minus the NYSE holidays the calendar service knows (static list fallback) | `_older_than_last_close` leaned on `spread_monitor.et_today()`'s weekday arithmetic; a US holiday during a Malaysian evening made it say "open" when it was not | step 1 (`clock.py` in II.3); the residual risk is a stale static list |
| 13 | `t1` / `r1` join `COND_KEYS` for every `sym_conds` reader | Sector & Industry, Curated and IV Rank users see two new switches with default True; `t1` costs nothing extra (same 2y bars), `r1` costs item 3 | release notes in steps 2 and 3 |
| 14 | No `dashboard_tst/tests/` tree, no pytest in the requirements today | the first engine commit must create the tree, the fixtures directory and `requirements-dev.txt`; the Hermes venv does not need pytest | step 1, first commit |
| 15 | The `/finviz` admin Data Ingest row for the options job is optional | the dashboard-visibility rule is satisfied by the strip + badge; the admin row is a nicety | step 1 if cheap |
| 16 | The LEAPS / long-family rule-stop marker - SETTLED (R1): the rule stop is drawn for EVERY family (II.2.9) | C3.9 drew it for `leaps_call` (`premium_stop_pct` 40) while D1.7 / D8.2 said "none for LEAPS" and C5.2 / C6.3 said "no rule-stop line" for `buy_call`; the contract keeps the 40% line for leaps / diagonal and the long block's 50% line for `buy_call` / `buy_put` / `bull_call` / `bear_put` / `calendar` - D8.2's assertion flips to "present at -0.4 x debit x 100", C6.3's to "present at -0.5 x debit x 100" | step 2 (`buy_call`) and step 4 (`leaps_call`) tests |
| 17 | Two arithmetic gaps inside the ruled golden LRCX numbers | (a) the golden credit 2.10 on a $10 width is 21% of width, under the house `credit_pct_min` 25 - the fixture must run with `credit_pct_min` <= 20 (the ticket's `floor` is written as 2.00) or the floor must be re-ruled; (b) `by_notional` is ruled 10 while `floor(100,000 x 10% / 790)` is 12 - either the fixture's `max_loss` basis for the notional cap is the $1,000 width or the ruled figure is 12; (c) a 330P / 320P at leg iv 0.46 and 48 DTE from 349.20 prices nearer 13.5 / 10.3 than 5.70 / 3.60 (credit ~3.2, not 2.10) - the integrator's set is applied as ruled; `lrcx.json` is generated from the ruled prices and the Black-Scholes-solved sigma is whatever reproduces them | step 1, when `lrcx.json` is written - raise before the first `test_option_engines.py` run |

## 11. Related docs

`DESIGN.md` (platform blueprint, security posture: no execution), `app/services/bull_put.py`
(the credit-spread engine and management rules), `app/services/support_bounce.py` (the
setup detector; `README.md` v4.124 / v4.125 entries describe its rules and the walk-forward
harness), `app/services/option_quotes.py` (Cboe feed), `bridge/README.md` (the IBKR bridge),
`README.md` v4.115–v4.118 (IV Rank page, My list, Positions monitor).

## Changelog

- 2026-10-03 — first write-up of the discussion (mindmap, human-in-the-loop, data,
  the ten-strategy catalog with rules, trend-line engine, payoff chart, UI layout,
  exists-vs-new, build order, open decisions). No code.
- 2026-10-04 — Part II (the reconciled full design) added; then the verifier's cross-part
  findings and the integrator rulings R1-R13 applied in place (II.5 #37-49): rule stop for
  every family, `iv_bump` relative, the golden LRCX / ISRG fixtures (new II.2.19; the 330/320
  at 2.10 is THE pick, 338 -> 336.2 and 74% -> 75% in §3 / §6e / §7 / §10), ratio IV/HV, the
  signature spellings, `option_prefs.read()`'s extra keys, `STRATEGY_KEYS`' home and order,
  `defined_risk` keys, the `degenerate.reason_key` vocabulary, `OptionTrade.meta`,
  `option_trade_checks` in the prune, the `POST /options/telegram` body, basket encodings,
  `clock._us_session_open`, house `roll_delta`; II.6 #12 / #16 settled, #17 added. No code.
