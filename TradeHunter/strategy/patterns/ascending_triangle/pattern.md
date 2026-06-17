# Ascending triangle — spec

The human-readable specification the detector (`detect.py`) implements. Edit this
when the *definition* changes; edit `SEED_THRESHOLDS` / run `_calibrate` when the
*numbers* change. Canonical definition supplied by the user (2026-06-17); this file
is the source of truth the geometry must satisfy.

## What it is
A bullish **continuation** (occasionally reversal) pattern formed by two converging
trendlines:
- **Upper boundary (resistance):** effectively **horizontal** — swing highs cluster
  in a tight band (≤1–2% on daily, *much* tighter intraday, often a few ticks). A
  flat ceiling = persistent supply stepping in at the same price.
- **Lower boundary (support):** **positive slope** — each swing low higher than the
  last. A rising floor = buyers paying progressively higher prices (accumulating
  demand). Demand is the *increasing* variable → bullish bias.
- **Touches:** minimum **2 reaction highs + 2 reaction lows**; **3 per line** is the
  textbook standard and far more reliable.
- **Apex:** where the lines would converge. Price should **resolve (break out) before
  the apex — typically 50–75% of the base→apex distance**. Drifting all the way into
  the apex without resolving kills the predictive power (dissolves into chop).

## Volume signature (part of the definition, not optional)
- **Into the apex:** volume **contracts** — lighter as the range narrows (a spring
  winding up).
- **On the breakout candle:** volume **expands** — a spike well above the recent
  average (e.g. > the 20-period volume mean). A break on **thin** volume is the
  single most common false-breakout tell.

## Confirmation (pick one, be mechanical)
- **Close-based:** a full candle **closes** above resistance (a wick poke that closes
  back inside is a rejection, not a break).
- **Volume-based:** breakout candle volume > recent average.
- Many intraday traders require **both**.

## Measured-move target
Height of the triangle at its widest (left base = resistance − support there),
projected **up from the breakout point** (conservative variant: project from the apex
level). A **minimum** objective, not a ceiling.

## Intraday adjustments (more failures than daily — screen harder)
- **Proportion:** on 1/3/5/15-min, a tradeable triangle spans **~8–15+ candles** with
  ≥2 (prefer 3) clean touches per line. 3–4 candles = noise, not a pattern.
- **Flat-top tolerance is tighter** intraday — highs within a few ticks. A "flat" line
  that actually drifts is a **channel or wedge**, not this pattern.
- **Time of day matters:** the first **15–30 min** (opening whip) and the **lunch
  lull** give the least-reliable breaks; mid-morning (after the open settles) and the
  afternoon trend-resumption are best. Treat a break into the close cautiously.
- **Higher-timeframe context:** most reliable when the HTF trend is already **up** and
  the triangle is a pause within it. The same shape after a downtrend, or right under
  daily resistance / VWAP that caps the move, is less trustworthy.

## Entry / stop / target (execution layer, not detection)
- **Entry:** aggressive = breakout candle close above resistance; conservative = wait
  for the **retest** of broken resistance (now support) — fewer fills, better R:R,
  filters fakeouts.
- **Stop:** below the most recent higher low / under the rising support line; on a
  retest, just under the retest low.
- **Target/manage:** scale at the measured move or next intraday resistance / prior
  swing high / VWAP band; trail the rest under successive higher lows.

## False-breakout filters
- Break that **re-enters the triangle within 1–2 candles** = bull trap → require price
  to **hold above** resistance for N candles or void it.
- Break on **thin volume** → suspect.
- Break in the **first 15–30 min / lunch / into the close** → down-weight.
- Triangle into **major HTF resistance / under VWAP** → cap risk.

## Live screening checklist
Flat top within a tick band · ≥2–3 ascending lows · contracting volume into the apex ·
breakout before ~75% to the apex · volume expansion on the break · a **close** (not a
wick) beyond the line · supportive HTF trend. Most aligned → take it; several missing →
the classic intraday fakeout.

## NOT an ascending triangle (hard negatives to reject)
- **Rising wedge** — *both* lines rise (support AND resistance), converging up.
- **Descending triangle** — flat support, *falling* resistance.
- **Channel** — parallel lines, no convergence.
- **Random consolidation** — low R² on either fit, no clean touches.

---

## Detector coverage (what `detect.py` + the rules panel enforce today)
| Spec criterion | Status | Where |
|---|---|---|
| Flat horizontal resistance | enforced | `res_slope_max_atrpct_per_bar`, `res_r2_min` |
| Rising support, higher lows | enforced | `sup_slope_min_atrpct_per_bar`, `sup_r2_min` |
| ≥2 touches/line (3 preferred) | partial | `min_touches_each` = 2 (not yet 3 / adjustable) |
| Range contraction into apex | enforced | `contraction_max` |
| Resolve before apex (50–75%) | enforced | `apex_frac_range` |
| Duration 8–15+ bars | enforced | `window_bars_range` (10–60) |
| Close (not wick) beyond resistance | enforced | breakout = first **close** above the wick-level ceiling |
| Support not breached (valid) | enforced | `_filter_valid_triangle` |
| HTF / prevailing uptrend | enforced | EMA-stack gate (`6>18>50` / `20>50>200`) |
| RTH-only detection | enforced | `_rth_bars` |
| Min height ≥ N ×ATR | enforced | rules panel (`_filter_min_height`) |
| **Volume contracts into apex** | GAP | `vol_slope` feature exists but is not gated |
| **Breakout volume expansion** | GAP | breakout candle volume vs avg — not checked |
| **Time-of-day filter** | GAP | first 15–30 min / lunch / close not down-weighted |
| Entry / stop / target / retest | n/a | execution layer, not the detector's job |

**Open gaps to wire as rules** (tickable + adjustable, like the others): volume
contraction into the apex, breakout-volume expansion, min-touches = 3, and a
time-of-day filter.

## Sources
Classical TA definition + the user's canonical write-up (2026-06-17). Implemented as
parametric geometry per `DETECTOR_DESIGN.md`; thresholds are calibration seeds tuned
by `_calibrate.fit_thresholds()` from confirmed vs rejected examples.

## Status
- **v0.0.1 (skeleton, 2026-06-16)** — spec + seed thresholds recorded; detector body
  built (D2), calibratable (D4).
- **2026-06-17** — spec replaced with the user's canonical definition + a detector
  coverage table flagging the volume / time-of-day gaps.
