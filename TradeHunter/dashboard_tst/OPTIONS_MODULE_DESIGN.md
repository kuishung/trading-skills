# Options module — basket, option data, strategy + strike recommendation

**Status: DISCUSSION / not started.** No code written for this module yet. This file
captures the whole design conversation so it can be resumed on any machine
(cross-PC via git + Dropbox; a pointer lives in `CLAUDE.md`). Last updated 2026-10-03.

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
│   ├── derived: HV20/HV60 · IV rank · IV percentile · IV−HV · term structure · skew ·
│   │            expected move · POP · days-to-earnings
│   ├── sources: Cboe delayed (exists) · IBKR bridge (exists) · Alpaca (fallback) · paid (later)
│   ├── store: option_chain_snapshot · iv_daily · option_signal · user_option_prefs
│   └── cadence: nightly on Hermes → page reads DB in ms; on-demand refresh; "Live" via bridge
│
├── 3. ENGINES  (what the system decides)              → §6
│   ├── 3a strategy recommender  (chart setup + IV regime → strategy, rule table, no LLM)
│   ├── 3b strike picker         (member's greek rules → top-3 strikes, return × POP)
│   ├── 3c premium gauge         (IV rank, IV−HV, term structure → SELL / NEUTRAL / BUY)
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
 │ ② set greek rules  │  │ HV, IV rank, IV−HV, term structure                             │  │   card,      │
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
     sell the Nov 330/320 put spread for ~2.10, 74% chance of keeping it."
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
daily closes already stored), IV rank, IV percentile, IV − HV premium, term structure
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
| `iv_daily` | symbol, date, iv30, iv_by_expiry (JSON), hv20, hv60, iv_rank, iv_pct, skew, term_slope | grows from `iv_history` (exists: daily iv30 from Cboe, 370-day retention, accumulating since 2026-09-10) → server-side rank/percentile with no TWS |
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

IV rank ≥ 50 → sell · 30–50 → either · < 30 → buy premium; IV − HV premium (IV above
realised = sellers are paid); term structure (front > back = an event is priced). One
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
  and that the stop is hit long before the max loss ("stop 338 · you'd lose ≈ $220" vs
  max loss $790).
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
│ ticker  trend  IV   idea   │ LRCX · 349.20 · ATR 11.5                      earnings Oct 22 · inside expiry│
│ LRCX    ↗     62  sell put │ "Uptrend: EMA 20 above 50 above 200 for 34 days, and price is riding a     │
│ MA      ↗     55  sell put │  trend line with 3 touches. It bounced off support at 340 on high volume.  │
│ ISRG    ↗     41  buy call │  Options are expensive (IV rank 62), so you're paid to sell a put spread   │
│ NVDA    ↔     57  condor   │  below that support."                                                      │
│ KO      ↘     24  buy put  │ [✓ Bull put spread] [Bull call spread · also fits] [Buy call · expensive]  │
│ [+ Add ticker]             │ PRICE CHART: candles · EMA 20/50/200 · trend line (3 touches) · support    │
│                            │              340 · 330 short · 320 long                                    │
│                            │ STRIKES under your rules (delta 0.20–0.30, 30–60 d, under support + line)  │
│                            │   Nov 20 330/320 put  collect $210  risk $790  chance 74%   ← best return │
│                            │   Nov 20 335/325 put  collect $270  risk $730  chance 68%                 │
│                            │   Nov 20 325/315 put  collect $155  risk $845  chance 79%   safer         │
│                            │ RISK & REWARD: expiry line · today line · breakeven 327.90 · max +$210 /   │
│                            │   −$790 · now 349 · stop 338 (you'd lose ≈ $220) · support 340   [$|R]    │
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
   338, under the bounce low) — for a credit spread that is tighter than the engine's
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

## Part II — full design: STATUS 2026-10-03 (drafts written, NOT yet reconciled)

The buildable spec was drafted by a four-designer / two-critic panel and saved under
`design/options/`:

- `part_A_data.md` — ChainSource interface (Cboe / Alpaca / bridge payload), the six tables
  with SQLAlchemy sketches, migration `f4a5b6c7d8e9` off head `e2f3a4b5c6d7`, the derived
  metric formulas, the nightly job (`TST-Options-Nightly`, 07:15 MYT), the `option_signal`
  contract, failure modes, test plan.
- `part_B_engines.md` — premium gauge, chart-state contract, the ten-strategy rule table,
  strike picker + prefs schema, sizing, order ticket, exits, worked LRCX / ISRG examples.
- `part_C_chart_engines.md` — trend-line engine, range/resistance detector, payoff engine
  and chart rendering, worked examples, tests.
- `part_D_ui.md` — routes, templates, HTMX flows, My rules drawer, Telegram push,
  migration of the three old pages, non-technical checklist, browser walkthrough.
- `CRITIQUE.md` — both critics' findings. **Headline blocker:** contracts are sized two
  different ways (B: by the loss at the chart stop; D: by max loss) — one rule needed:
  `contracts = min(by_chart_stop, by_max_loss)` with a shared GAP_MULT pref (house 2.0),
  and the card must show both figures ("$990 if the stop fires, up to $6,830 if it gaps").
  Also: tracked positions proposed in three stores (pick one), two payoff implementations
  (pick one), prefs field names differ between B and D, the push path differs.

**Next session, in order:** (1) apply CRITIQUE.md blockers + majors to the parts;
(2) unify the cross-part names listed under "Reconciliation"; (3) merge the four parts
into this file as Part II; (4) then start build step 1 (credit spreads + payoff chart +
page skeleton). The ten decisions in §10 were locked on 2026-10-03 with the user's
go-ahead ("ok now we proceed with the full design") using the recommended picks.

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
