# Option Pricing Calculator (Excel, Melvin Chan) — study notes + parked flaws

**Source:** `D:\Downloads\Option Pricing - Clean Sample 20261017.xlsm` (user's laptop,
not synced). Single sheet `Bull Call Spread Sample`, lifted from a larger OneDrive
workbook `Option Strategies.xlsm` (tabs: Bull n Bear Spreads, Naked, Backspread,
Ratio Backspread, Ladders, Condor, Calendar Spread, Diagonal Calendar, Straddle,
Strangle, Straddle Calendar, Hypothetical Testing). Author Melvin Chan, created
2015-06-05, last saved 2026-10-17.

**Studied:** 2026-10-17. Math replicated in Python and matched every cached cell
(Black-Scholes legs, grid, interpolated target / worst / breakeven) to 1e-14.

**Status:** understood in full. **Flaws are PARKED on purpose** (user, 2026-10-17:
"keep the flaws and we come back later to the flaws"). Do not fix the workbook or
port it until the user picks that up again.

## What it does

Four-leg European option strategy pricer. Reprices the whole position on a chosen
*target date* (today's DTE minus "Time Decay (days)") at a chosen target price, a
worst-case price, and across a price ladder, holding IV and rates constant.

| Area | Cells | Logic |
|---|---|---|
| Inputs | C4 spot, C5 target px, C6 worst px, C7 decay days | scenario definition |
| Legs (entry) | H:K rows 4–13 | c/p flag, strike, DTE, r, q, IV, weight (+1 long / -1 short) |
| Legs (target date) | M:P rows 4–13 | same legs, DTE minus C7 |
| Pricing | rows 15–19 | BSM with continuous dividend, days/365; d1, d2, price, delta |
| Entry debit | C10 = SUM(H23:K23) | price × weight, entry columns |
| Post-decay at spot | C14 = SUM(M23:P23) | same at target date |
| Price ladder | G37:G82 | spot-anchored; MajorUnit 5 far from spot, MinorUnit 2 near spot; `ChartSkew` (Upward/Neutral/Downward) moves the fine region; row 82 = spot itself |
| Grid values | H:K / M:P rows 37–82 | VBA `BlackScholes(...)*weight`, entry vs target date |
| Net curves | Q (value, target date), W (delta, entry date) | SUM × R13 mult factor |
| Decision cells | C11, C12, C24 (array formulas) | VBA `InterpLinear` on the Q curve: value at target px, value at worst px, breakeven = price where Q equals C10 |
| Ratios | C25/C33 R:R, C26/E11 ROI, C29–C31 $ totals | totals assume 100 multiplier × C28 sets |
| Chart | `ChartValue` scatter | x = G37:G82, y = M..P and Q (target-date curves) |

## VBA modules

- `ThisWorkbook`: 7 button macros that rescale the chart axes (x to grid min/max;
  y to min/max of M37:Q81 at 100/80/60/40/20 %, or to the net Q column). Two
  commented-out subs once nudged C7 from a spin button.
- `QuantFinance`: `BlackScholes`, `DeltaOptions` (correct, used by the grid);
  `GammaOptions`, `VegaOptions` (author-flagged wrong, unused).
- `Interpolation`: `InterpLinear(x, xs, ys, match_type)` — bracket via MATCH, linear.
- `Module1` / `Module2`: recorded junk (`Macro2` selects a sheet that no longer exists).
- Saved Solver model: maximise C25 by changing H5, s.t. D11 ≥ 0, D12 ≤ -1, I13 ≤ 100.
  Stale, not wired to a button.

## Parked flaws (come back to these)

1. **Dividend bug, row 18 (H18:P18).** Call branch uses `EXP(-H9*H107)`; H107 is
   empty so the stock-term dividend discount is always 1. Wrong whenever q > 0.
   The grid (VBA) is right, so headline C10/C14 would silently disagree with L82.
   Fix: `H107` → `H7` in all eight cells.
2. **GammaOptions / VegaOptions wrong.** Use N(d1) (CDF) where n(d1) (PDF) is
   needed. Unused on the sheet. Fix: `n(d1) = EXP(-d1²/2)/SQRT(2π)`;
   gamma = e^(-qT)·n(d1)/(S·σ·√T), vega = S·e^(-qT)·n(d1)·√T.
3. **InterpLinear assumes a monotonic curve.** Breakeven C24 inverts Q→G; only
   valid for single-crossing payoffs (verticals). Butterfly / condor / straddle /
   calendar give one arbitrary crossing or an error. Needs a root-finder over all
   crossings.
4. **Grid edges are absolute dollars.** Ladder = spot ± 80 (12×5 + 10×2 each
   side). Covers 80 % of a $100 stock but 16 % of a $500 stock. A target / worst
   price outside the ladder, or exactly on the last rung (MATCH + 1 overflow),
   breaks the lookup. Should scale units to spot (ATR-/percent-relative, per the
   TradeHunter normalised-parameter rule).
5. **No early exercise, no IV scenario.** European only; IV frozen at entry value;
   no IV-change input for the target date.
6. **Loose labels / duplicates.** "Total Theta" (H24) is total decay over the
   whole window, not per day. "Cost of carry" (row 10) displayed but unused.
   ROI (C26 = E11) and R:R (C33 = C25) duplicated. `Dim MinValue, MaxValue As
   Double` leaves MinValue a Variant (harmless).
7. **External link.** Chart title points at `'[1]Ratio Backspread'!$H$36` in the
   OneDrive parent workbook → "update links" prompt on open. Workbook-level
   `ChartSkew` / `MajorUnit` / `MinorUnit` / `SpotPrice` names are `#REF!`
   duplicates, shadowed by the sheet-level names (harmless).
8. **Legs 3–4 inert in the sample** (blank weights) but carry a $510 call and a
   $200 put; nothing warns when a leg has a strike but no weight.

## Sample tie-out (for checking a port later)

Long 100 C / short 150 C, 120 DTE, 20 % IV, r 4.8 %, q 0, spot 100, decay 85 d,
target 175, worst 50:

| Item | Value |
|---|---|
| Entry debit C10 | 5.3611 |
| Post-decay at spot C14 (35 DTE) | 2.7011 |
| Value at 175 on target date C11 | 49.7539 |
| Value at 50 on target date C12 | ≈ 0 |
| Breakeven on target date C24 | 104.0164 |
| Reward over risk C25 | 8.2805 |

## Changelog

- 2026-10-17 — Created. Full study of the workbook; flaws recorded and parked at
  the user's request.
