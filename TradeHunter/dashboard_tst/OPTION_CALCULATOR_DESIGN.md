# Option Calculator + Setup Pairing — design (agreed 2026-10-17)

**Status: BUILDING — step 1 (the engine) in progress.** Steps 2–3 not started.
Read this file first when continuing on any PC.

## 1. Where it comes from

The user's Excel workbook *Option Pricing – Clean Sample 20261017.xlsm* (study notes and
parked flaws: `strategies-reference/OPTION_PRICING_CALCULATOR.md`). The sheet is a
four-leg Black-Scholes strategy calculator: type the legs, type a target price, a
worst-case price and a number of decay days, and read off the value of the position on
that *target date* (not at expiry), the breakeven, reward over risk and return on
investment, with a chart of each leg and the net across a price ladder.

The user's words (2026-10-17): *"this workbook is for manual entry of the data and to pair
the leg to come to the decision indicator. with the massive data we have now, i do not want
to manually pairing the legs and to see which strategy give me the best setup."* And:
*"we sole work on the option pricing calculator as per the sheet"* first; the strategy
pairing comes after the calculator is proven.

## 2. The three steps (in order)

| Step | What | Done when |
|---|---|---|
| **1. Engine** | The sheet's math as one callable Python function (`app/services/option_calc.py`): legs + scenario in, indicators + payoff curves out. No page, no DB. | The sheet's sample case reproduces to the cent in an automated test (`tests/test_option_calc.py`). |
| **2. Manual calculator on Hermes** | Member saves a *request* (ticker, legs picked from the live Massive chain, target / worst / days). A Hermes lane recomputes every saved request after each Massive pass and writes a *result* row stamped with the pass. A page reproduces the sheet: legs, indicators, chart. Tray line + Settings → Data Ingest status. | A member can enter the sheet's case against a real chain and see the same figures and chart. |
| **3. Setup pairing** | Member gives the scenario only (target, worst, days ⇒ a direction and a window). The lane enumerates every leg set inside the window per strategy family, scores each with the engine, filters by the member's criteria, ranks, stores. Page: one row per family with its best leg set, alternatives under it, any row opens in the manual page. | Discussed, not designed in detail yet — see §7 for what was agreed. |

The engine is the single scoring function for all three. The manual page is its first
caller, the pairing loop its second.

## 3. Inputs (from the sheet)

**Scenario — always typed by the user**

| Sheet | Field | Notes |
|---|---|---|
| C5 | target price | where the user expects the stock to go |
| C6 | worst-case price | where the user is wrong (support for a bull setup) |
| C7 | decay days | days from today to the *target date*; sets the horizon |
| C28 | number of sets | only for the dollar totals |

**Per leg (max 4) — the user picks the contract and the size; data fills the rest**

| Sheet row | Field | Source in TradeHunter |
|---|---|---|
| 4 | call / put | the picked contract |
| 5 | strike | the picked contract |
| 6 | days to expiry | from the contract's expiry |
| 11 | implied volatility | Massive IV of that exact contract (per-leg IV; the sheet used one flat IV) |
| 13 | weight | typed: + long / − short, number of contracts |

**Market — auto-filled, overridable**

| Sheet | Field | Source |
|---|---|---|
| C4 | spot | `ScrUnderlying.spot` (Massive / parity / close) or a live quote |
| row 8 | risk-free rate | the platform's one rate, `opt_constants.RISK_FREE` (app setting) |
| row 9 | dividend yield | default 0; Yahoo yield later |

**Gone**: chart skew, major/minor unit (J27, G28, G29) and the seven axis-scaling
buttons — a code grid scales itself.

## 4. The engine (step 1) — contract

```
evaluate(legs, scenario) -> Result
  legs:      [Leg(right, strike, dte, iv, qty, q=0)]           1..4 legs
  scenario:  Scenario(spot, target, worst, decay_days, sets=1, multiplier=100,
                      r=RISK_FREE, q=0.0, iv_shift=0.0)
  Result:    per-leg price / delta at entry and on the target date,
             entry_value, decayed_value, target_value, worst_value,
             gain_at_target, loss_at_worst, reward_over_risk, roi,
             breakevens (every crossing on the target date),
             max_profit / max_loss inside the grid on the target date,
             net_delta_entry, net_delta_target, total_decay,
             dollar totals (gains, risk, outlay) for `sets` × `multiplier`,
             curves: grid, per-leg target-date curves, net target curve, net entry curve
```

Math, kept identical to the sheet so the tie-out is exact:

- Black-Scholes-Merton with continuous dividend yield; `T = days / 365`.
- The position is **repriced on the target date** (each leg at `dte − decay_days`), IV
  per leg held constant (`iv_shift` is reserved for a later IV scenario, default 0).
- Indicators as the sheet defines them: `entry_value = Σ price×qty` at spot today;
  `target_value` / `worst_value` = net value on the target date at the target / worst
  price; `gain = target_value − entry_value`; `loss = worst_value − entry_value`;
  `reward_over_risk = −gain / loss`; `roi = gain / entry_value`; `total_decay =
  decayed_value − entry_value` (the sheet's "Total Theta" = decay over the whole window).

Where the engine is allowed to differ from the sheet (free fixes, see the parked list):

- The dividend exponent bug in the sheet's row 18 is not reproduced.
- Breakevens come from a root search across the whole target-date curve (every
  crossing), not from a single-crossing interpolation.
- The price grid is dense and spans the scenario (worst … target, widened by a
  spot-relative buffer) instead of a ±$80 ladder.
- A leg that has reached expiry on the target date is worth its intrinsic value
  (the sheet would divide by zero).
- Vectorised with numpy: every contract's value curve is computed once; a leg set
  is a weighted sum of curves. One leg set is instant; the pairing loop needs this.

Everything else (flat-IV mode for checking against the sheet, 365-day count) is kept.

## 5. Decisions taken (defaults; change here if the user says otherwise)

| # | Decision | Default taken 2026-10-17 |
|---|---|---|
| 1 | rate and dividend | rate = `opt_constants.RISK_FREE` (one app setting); dividend yield 0, Yahoo later |
| 2 | day count | calendar days / 365, as the sheet |
| 3 | IV on the target date | each leg's own IV, unchanged (`iv_shift = 0`) |
| 4 | breakeven | every crossing on the target date, reported as a list |
| 5 | where | `app/services/option_calc.py` + `tests/test_option_calc.py` |

**Decided 2026-10-17 (evening, from the phone) — the user: *"I want the user to pick the
expected price and the worst case price only"*, then *"ok build it"*:**

| # | Decision | Taken |
|---|---|---|
| 6 | user input | **two numbers only: expected (target) price and worst-case price.** Ticker from the page / chart context; spot from Massive; direction read from target vs spot; quantity 1 set; member criteria set once in Settings and reused. |
| 7 | horizon (the time the two prices do not say) | **pairing (step 3): search several horizons and rank across them** — the first three or four monthly expiries on or after today + 21 days, each horizon's target date = that expiry minus a few days, the result row says "best at N days". **manual page (step 2): a default horizon in member settings** (45 days), changeable per request. |
| 8 | range trades | **first version is directional only** (target above spot = bullish families, below = bearish). Condors / butterflies need a second worst price - later, with a toggle. |
| 9 | step 2's role | the manual calculator page is mainly the VIEWER a pairing result opens in (and where a leg or price can be adjusted by hand); its leg picker is secondary, but built. |
| 10 | results DB | **`tst.db`**, beside the member's other saved objects (results are per member and small). |
| 11 | retention | every pass kept for the current and previous session day; older passes thinned to the last pass of each day. |
| 12 | trigger | after every Massive pass (market hours + the end-of-day pass), same cadence as the screener collector. |

## 6. Sample tie-out (the automated test)

Long 100 C / short 150 C, 120 DTE, IV 20 %, r 4.8 %, q 0, spot 100, decay 85 days,
target 175, worst 50, 13 sets:

| Figure | Sheet |
|---|---|
| entry value | 5.361100 |
| decayed value at spot (35 DTE) | 2.701086 |
| value at 175 on target date | 49.753938 |
| value at 50 on target date | ≈ 0 |
| breakeven on target date | 104.016386 |
| reward over risk | 8.280547 |
| ROI | 8.280547 |
| net delta entry / target | 0.576866 / 0.541925 |
| total gains / risk / outlay (13 sets) | 57,710.69 / −6,969.43 / 6,969.43 |

(The sheet's breakeven comes from linear interpolation on a 2-dollar ladder; the engine's
root search agrees to about a cent.)

## 7. Step 3 as agreed so far (pairing)

- **The window comes from the user's inputs** (user: *"it should be windowed because user
  will set the expected move and time for that to happen"*). Expiries = the first on or
  after the target date plus one or two further out. Strikes = every listed strike
  between worst and target widened by a spot-relative buffer (about one IV expected-move
  unit), calls and puts. A blind search over a whole chain is ~22 trillion four-leg
  candidates (weeks); the window brings it to ~3–40 million (seconds); restricting to
  known strategy shapes brings it to ~50 thousand (under 0.1 s). Guards: cap strikes
  per expiry around the levels; drop shapes that fail cheap filters before pricing.
- **Direction → families**: target above spot and worst below ⇒ bullish families (long
  call, bull call / bull put spread, call diagonal / calendar, call ratio); the mirror
  for bearish; target near spot with a worst price on each side ⇒ range families (iron
  condor, butterfly, short strangle, calendar). IV rank leans credit (high) vs debit
  (low).
- **Member criteria as filters**: defined risk only; max loss per set; min reward over
  risk; min open interest per leg; no earnings in the window; debit vs credit
  preference. Then rank the survivors (reward-to-risk blended with probability; pure
  R:R favours lottery tickets).
- **Output**: one row per family with its best leg set and the indicators; the next
  few candidates under it; any row opens in the manual calculator.
- **Runs on Hermes in the background** after each Massive pass, results saved per pass,
  members review on the page.

## 8. Process flow

`design/option_calculator_flow.svg` (+ `.png`): the three steps as one diagram - the
engine (orange) in the middle of each; member pages (blue); stored data (grey); Massive
(green). Checked by the user on 2026-10-17.

## 9. Changelog

- 2026-10-17 — Created after the design discussion; step 1 build started with the
  defaults in §5.
- 2026-10-17 (later) — Step 1 built: `app/services/option_calc.py` + 14 tests reproduce
  the sheet to 1e-9. Flow diagram added. Decisions 6-12 recorded (two-price input,
  horizons searched, directional first, tst.db, retention, trigger). Build of steps 2-3
  started.
