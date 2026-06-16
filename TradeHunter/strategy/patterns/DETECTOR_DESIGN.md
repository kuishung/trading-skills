# Pattern detector — design (rule-based geometric first, ML later)

Status: **PLAN** (2026-06-16). Companion to `dashboard_tst/PATTERN_TRAINER_DESIGN.md`
(the teaching UI) and `strategy/patterns/README.md` (the artifact contract). This
doc specifies the **engine behind `detect.py`** — how a pattern like *ascending
triangle* is actually detected — and how it validates and wires to execution.

## Decision: rule-based geometric, not ML (for now)

An ascending triangle has a **closed geometric definition** (flat resistance,
rising support, convergence, multiple touches, contraction). That is a
deterministic, parametric-geometry problem — the same class of detector every
commercial TA scanner uses. So:

- **`detect.py` is a deterministic geometric detector. No machine learning, no
  hardcoded dollar levels.** "Not hardcoded" ≠ "needs AI": the detector fits
  lines to swing points and checks **slope / R² / convergence / touches /
  contraction** in **ticker-relative** terms (ATR multiples, %, bar counts) per
  CLAUDE.md's normalization rule. That single detector generalizes across all
  ~1500 tickers and both timeframes with **no retraining**, and it's fully
  inspectable — when it mis-fires you read the actual slope/R²/touch numbers.
- **The user's saved gallery examples are a CALIBRATION + VALIDATION set, not
  training data.** They tune thresholds and form a regression suite, not a model.
- **ML is deferred and narrow** (see "Where ML earns its place"). It is NOT the
  starting point and NOT required to ship a useful, self-improving detector.

This refines `PATTERN_TRAINER_DESIGN.md` Phase 2: the generated `detect.py` is a
geometric detector. The LLM (when the DeepSeek key is live) may *draft* the
geometry from the taught spec, but the logic it emits is deterministic rules —
and the same detector can be hand-authored without any LLM.

## Four decoupled layers

Keep these separate so only the (rule-scorer) layer encodes "what a triangle is":

```
candles ─▶ 1. swing extractor ─▶ 2. feature engine ─▶ 3. rule scorer ─▶ 4. execution
           (pivots/zigzag)        (geometry numbers)     (detect.py)      (entry/stop/target)
```

1. **Swing extractor** (shared, `strategy/patterns/_geometry.py`): raw bars →
   sequence of pivot highs/lows. Fractal/zigzag: a pivot high is a bar whose high
   exceeds the `k` bars on each side (k ticker-relative-ish: a small fixed window
   like 2–3 is standard; the *prominence* filter is ATR-relative so noise pivots
   are dropped). Output: ordered swing points `[{t, price, kind}]`.
2. **Feature engine** (shared): a window of swings → ~10–15 scale-invariant
   numbers (see "Feature list"). Pure function, no I/O.
3. **Rule scorer** (`strategy/patterns/<slug>/detect.py`): thresholds the
   features into a 0–1 score and returns contract matches. This is the only
   pattern-specific file.
4. **Execution** (existing `strategy/` + `execution/`): a high-score match →
   entry/stop/target + sizing. Separate by design (risk mgmt ≠ recognition).

## Module layout (proposed)

```
strategy/patterns/
  _geometry.py        # shared: swing extraction, line fit (slope/R²), touches,
                      #   convergence, contraction, ATR — all ticker-relative,
                      #   NO lookahead. Pure functions + unit-tested.
  _validate.py        # the detector validation harness (calibration suite,
                      #   walk-forward split helper, held-out-ticker check)
  ascending_triangle/
    pattern.md        # the human spec (shape, rules, thresholds, sources)
    detect.py         # detect(bars)->[{start_t,end_t,score,notes}] using _geometry
    cases/            # frozen calibration cases exported from the gallery:
                      #   positives (your labeled triangles) + hard negatives
                      #   (rising wedge / descending tri / random consolidation)
```

`detect.py` imports `_geometry`; the contract stays exactly as in the folder
README. `cases/` is how gallery examples become a committed regression suite.

## Ascending-triangle spec (the concrete checks)

All thresholds ticker-relative; starting values are calibration seeds, tuned
against `cases/`:

| Check | Rule (seed) | Why |
|---|---|---|
| Flat resistance | line through swing **highs**: \|slope\| ≤ ~0.05·ATR%/bar, R² ≥ 0.6 | the ceiling |
| Rising support | line through swing **lows**: slope ≥ ~0.10·ATR%/bar, R² ≥ 0.6 | higher lows |
| Convergence | support line meets resistance within ~0.5–2.0× window length ahead | it's a triangle, not a channel |
| Touches | ≥ 2–3 swing touches within ~0.25·ATR of each line | not a 2-point fluke |
| Contraction | range (in ATR or %) at window end ≤ ~0.6× range at start | squeeze into apex |
| Duration | window length within a sane band (e.g. 10–60 bars on 3m/5m) | not micro/macro noise |
| Volume (soft) | volume trend flat/declining into apex | classic confirmation (down-weight, not veto) |

**Score** = weighted combination (e.g. geometric mean of the normalized
sub-scores) → 0–1. The threshold is set on the calibration set, not guessed.

`notes` carries the diagnostic numbers (slopes, R², #touches, apex distance) so
a flagged match is explainable and the execution layer can reuse the levels.

## Feature list (the ~10–15 numbers)

resistance slope, resistance R², support slope, support R², apex distance (bars),
apex angle, #touches-resistance, #touches-support, contraction ratio, window
length, volume slope into apex, breakout room (price vs resistance), ATR% of the
window. All computed from candles **available at the window's last bar close** —
see no-lookahead.

## Validation harness (`_validate.py`)

1. **Calibration suite** — run `detect.py` over `cases/`: every saved positive
   must score above threshold, every hard negative below. This is the regression
   gate; it runs on every threshold change. (Gallery → `cases/` exporter is a
   small step in the Trainer / a CLI.)
2. **Hard negatives matter most** — rising wedge, descending triangle, and random
   sideways consolidation look similar; without them the detector learns "any
   squeeze = triangle." Seed `cases/negatives/` deliberately.
3. **Walk-forward only** — when we later score *outcomes*, split train/validate/
   test by **time**, never shuffle. Also hold out a block of **tickers entirely**
   to confirm it generalizes across symbols, not memorized names.
4. **No-lookahead invariant** — the single easiest way to leak the future is in
   swing detection (a pivot "confirmed" using later bars). `_geometry` must mark a
   pivot only once the right-side bars exist, and every feature for a window uses
   only bars ≤ that window's last close. `_validate.py` includes a lookahead probe
   (re-run incrementally bar-by-bar; a detection must never reference a future t).

## Candidate sources for the review queue (user, 2026-06-16)

The trainer's review queue (where the user confirms/rejects/corrects) is fed by
THREE sources, all landing on the same drag-to-label surface:

1. **Pre-filter harvest** (`_harvester`) — loose high-recall sweep → near-triangles.
   Efficient for gathering POSITIVES + the decision boundary.
2. **Active-learning** (`_harvester.active_learning_rank`) — the hardest cases
   (score nearest the cutoff) so limited labelling effort is spent best.
3. **Random sampling** — pick a random ticker + window from the parquet universe,
   run `detect()`, show whatever it found (or "nothing detected — draw it if you
   see one"). A **"Random sample"** button + optional auto-feed in the trainer.

**Why random matters — it catches the detector's FALSE NEGATIVES.** The harvester
only surfaces windows that PASS the pre-filter, so it can never show a clean
triangle the detector *missed*. Random raw charts expose exactly those misses
(recall failures) and give unbiased negatives, reducing the bias of only ever
judging what the detector already likes. **Honest tradeoff:** most random windows
aren't the pattern, so pure-random is LOW-YIELD for positives — it complements,
not replaces, (1)/(2). Use a mix: mostly pre-filter/active-learning, salted with
random for coverage + miss-detection.

Random sampling works in BOTH phases: before `detect()` exists (random chart →
draw a seed positive from scratch), and after (random chart → detector proposes →
correct or reject — now also testing recall). All offline/parquet (calibration),
never live.

## Role of the historical universe — calibrate + evaluate, NOT live-scan

(User intent, 2026-06-16: "use the parquet universe to calibrate and learn from
the actual chart variation… test visually… then mount to live execution." Also:
"I don't need to scan the universe [for live signals] — it's historical.")

So the 1500-ticker × 3m/5m store is repurposed from a *signal source* to a
**calibration + evaluation corpus**:

1. **Harvest** — a loose geometric pre-filter (flat-ish highs + rising-ish lows,
   generous tolerances) sweeps the universe and surfaces every *candidate* window.
   Cheap, high-recall, low-precision on purpose. Yields hundreds–thousands of
   candidates = the "real chart variation."
2. **Label (the bridge)** — **the universe is UNLABELED.** It supplies *variety*,
   not *meaning*. The detector cannot learn "good ascending triangle vs rising
   wedge" from raw history — that definition is the user's. So the user
   confirms/rejects surfaced candidates in the gallery (fast: judging, not
   hunting). **Active learning:** each round surface the candidates the detector
   is least confident about (most informative) + spot-check high-confidence ones.
3. **Fit** — thresholds/score weights are fit to the *distribution* of confirmed
   positives vs confirmed/near-miss negatives (e.g. pick slope/R²/contraction
   cutoffs that best separate the two sets). This is the "learns from variation"
   step — still interpretable parametric geometry, but parameters are
   data-derived, not hand-guessed.
4. **Visually evaluate** — render flagged windows on the parquet chart (the
   existing Pattern Trainer chart) so the user eyeballs precision/recall and feeds
   corrections back to step 2.

**Hard constraint to state plainly:** you cannot make the detector "smart" purely
from unlabeled history with zero human judgment. Without the user's labels the
only teacher is the pre-filter, so the model would just re-learn the pre-filter's
rules (circular). Variety comes from the universe; meaning comes from the user.
A *small* amount of confirmation + active learning is what makes it genuinely
improve. This harvest→label→fit loop also produces exactly the labeled dataset the
(later, optional) ML classifier would train on — same data, more capable model.

Live signals never come from this scan; they come later on **live** data via the
execution setup (below).

## How the gallery improves the detector (the calibration mechanism)

The gallery hands you two labelled clouds in feature space: **confirmed
positives** (real triangles, exact ranges) and **rejected negatives** (the
near-misses you turned down). Everything below squeezes signal from those two
clouds. In rough order of value:

1. **Re-tune each threshold to where the clouds separate.** For every labelled
   window extract its feature vector; per threshold, move the cutoff to the value
   that best splits positives from negatives.
   *Example:* seed `res_r2_min=0.60`; positives sit in [0.70,0.95], rejects in
   [0.40,0.78] → clean split ≈ 0.71, so calibration raises 0.60→0.71. Per feature.
   This is "thresholds derived from data, not guessed." → `_calibrate.fit_thresholds`.

2. **Re-weight the score by which features actually discriminate.** Rank each
   feature by how well it alone separates the labels (per-feature AUC /
   information-gain). Up-weight the separators (e.g. contraction), down-weight the
   overlappers (e.g. apex angle). The score stops caring about what your eye doesn't.

3. **Calibrate the fire-cutoff (`score_min`) to a precision/recall point.** Score
   every labelled window with the re-tuned scorer; pick `score_min` to hit a chosen
   trade-off (e.g. catch 90% of positives while rejecting 90% of negatives) — from
   the score distributions, not a guess.

4. **Mine the hard negatives to discover MISSING rules** (highest value, the step
   most people skip). When a *rejected* example scores *high*, inspect which feature
   let it through. A cluster of high-scoring rejects that are all **rising wedges**
   means v0 checks "support rises" but never "resistance is *flat*, not also rising"
   → add a `res_slope` upper bound. Descending-triangle rejects → add a support-
   flatness guard. Hard negatives don't just move numbers — they reveal new
   discriminating *checks* to add. (Calibration *surfaces* the need — a cluster of
   high-scoring negatives; a human adds the check. See "bright line" below.)

5. **Lock it in with the calibration suite (no regressions).** Every confirmed
   example is a frozen test case; after any tweak, re-run all gallery cases —
   positives must still pass, negatives still fail. Makes improvement MONOTONIC
   instead of whack-a-mole. → `_validate.run_calibration_suite`.

6. **Active learning — label the fuzzy ones next.** After re-tuning, re-harvest and
   surface candidates scoring nearest `score_min` (least certain) → label a handful
   → re-tune → repeat. Sharpens the boundary exactly where it's blurriest, so a few
   hundred well-chosen labels beat thousands of random ones. → `_harvester.active_learning_rank`.

**Bright line — what's automatic vs human:**
- **Automatic (`_calibrate`):** threshold *numbers*, score *weights*, fire *cutoff*
  — all fit from the gallery, no human math.
- **Human-guided (rare):** adding a genuinely *new rule* (the wedge-rejection check)
  when a class of false positives shows the geometry is blind to something.

**Ceiling / ML hand-off:** tuned thresholds can't express interactions ("low R² is
fine *if* contraction is extreme *and* volume collapsed"). When they plateau, the
same labelled feature-vectors train a gradient-boosted model that can (D7). The
gallery IS that training set — nothing labelled is wasted.

## Self-improving — without ML

The useful self-improvement loop at this stage is a **tuning loop, not a training
loop**:

- Add more gallery examples → they join `cases/` → re-run calibration when tuning.
- Run the detector in **shadow/paper** across the universe (Phase 3 scan), log
  every detection's features + score + **what happened next** (broke out? how far?
  would a paper trade at the signal have paid?). Use `data/journal/` + `data/`
  long-term store per the architecture.
- Periodically eyeball the outcome log to retune thresholds. That's measurable,
  reversible, and debuggable.

## Where ML earns its place (later, narrow)

Only after the rule-based detector ships and the outcome log has real volume:

- **Residual-judgment classifier** — for cases where geometry says yes/no but your
  eye disagrees (volume profile, context). Train on gallery labels; it adjusts the
  *score*, it doesn't replace the geometry. Gradient-boosted trees on the feature
  list (LightGBM/XGBoost) — sample-efficient, inspectable feature importance —
  before any sequence model.
- **Separate success-predictor** — "given this pattern + volatility/volume/regime,
  is the *trade* likely to pay?" Keep it distinct from the shape classifier;
  conflating shape and profitability makes the detector chase P&L and go brittle
  on regime shift.

Neither is needed to start, and both consume the same logged data the rule-based
loop already produces.

## Two universes — calibrate broad, execute narrow (user, 2026-06-16)

The detector touches the parquet universe ONLY for offline calibration/eval. At
TRADE TIME it runs on a **small, live, intraday universe** — the day's in-play
names — not 1500 tickers. Same `detect.py`, same calibrated thresholds; only the
bar source and the universe differ.

| | Calibration universe | Execution universe |
|---|---|---|
| size | large — ~1500 × 3m/5m | **small — today's in-play names** |
| when | offline, background (Hermes/R720) | live, intraday, on bar close |
| data | **parquet** (historical) | **live bars** (fetched) — never parquet |
| job | teach/tune the detector | fire the calibrated detector |
| name selection | sweeps everything | the **intraday scanner** picks |

Why narrow-at-execution is *better*, not just cheaper: an ascending triangle only
matters on a ticker with the volume/volatility/catalyst to actually break out.
A clean triangle on a dead name is noise — pre-filtering to in-play names raises
signal quality. And it matches the house **per-family scanner model**: the
scanner picks the watchlist, the detector evaluates each — universe-selection and
pattern-detection stay decoupled.

The generalization payoff: tuned on 1500 names' worth of variation, the detector
works on whatever small live set is in play today, **including tickers absent from
the calibration corpus.**

## Deployment to intraday auto-execution

- **Universe = the intraday scanner's watchlist**, not the parquet store. Reuse the
  existing producers (GUNS scanner → `state/watchlist_guns_<date>.txt`, premarket
  movers, IBKR movers). The detector consumes these names; it does NOT pick them.
- **Incremental, on bar close, on LIVE bars** — per in-play ticker/timeframe, on
  each new 3m/5m bar: fetch live bars (IBKR/Alpaca/yfinance — never parquet, per
  CLAUDE.md's parquet-scope rule), update swings, recompute features, score. A
  handful of names on 3m/5m is trivially cheap.
- **Execution layer is separate and rule-based** (it's risk mgmt): entry on
  confirmed breakout above resistance (+ volume condition), stop below the last
  swing low / triangle base, target = triangle height projected from the breakout,
  sizing under the global risk caps. This is a normal `strategy/<FAMILY>/<setup>/`
  with `build/evaluate/pick_universe/fetch_bars` that *consumes* `detect.py`'s
  matches — not part of the detector.
- **Gating** — wire it through the existing per-strategy ON/OFF + ARM flags so it
  paper-evals before it ever submits.

## Execution pre-flight & risks → resolutions (REQUIRED before real trade)

Going live exposes failure modes that the geometry validation does NOT cover.
The 12 foreseen risks collapse into 8 architectural moves; several reuse existing
TradeHunter machinery. **The five with no current home are the D6 gate.**

| # | Risk | Resolution | New? |
|---|---|---|---|
| 1 | Swing-confirmation **lookahead lag** (live sees a shorter triangle than backtest) | **One `detect_stream(bars_so_far)`** — closed bars + confirmed swings only (pivot at i emits at i+right); calibration REPLAYS parquet bar-by-bar through it. `_validate.assert_no_lookahead` is the replay. | **NEW** |
| 2 | **Bar-construction mismatch** (parquet pre/post + adj vs live feed) | **One `make_bars()` normalizer** both paths share (fixed session/adjust/alignment) + a **reconciliation pre-flight** (parquet vs live OHLCV on an overlap day) gating ARM. | **NEW** |
| 3 | **Labeling hindsight bias** (gallery picks winners) | Review shows chart **only up to the apex** (hide breakout/aftermath); harvest **failed triangles** as positives-by-shape; keep shape-label ≠ outcome. | **NEW** |
| 4,5 | detect() is a region not a signal; **fakeouts/slippage** | **Signal layer in `strategy/<FAMILY>/evaluate`**: confirmed-close breakout + volume, validity window (K bars), per-instance **dedup lock**, stop-limit + max-slippage cap, modeled slippage/commission. | **NEW** |
| 6 | **Untradeable** clean triangles | Min-$-volume + max-spread gate in the **scanner/watchlist producer**, before the detector. | reuse |
| 7 | **Signal clustering → correlated position blow-up** | **Portfolio risk gate**: max_concurrent + total-exposure cap + correlation/sector throttle + breadth guard (many fires in a window = ONE bet → top-K or stand down). | **NEW** |
| 8 | **Shape ≠ profit** | Mandatory hard stop at submit; long ON+DISARMED + outcome log before ARM; success-predictor (D7) gates trades; regime kill-switch (index/VIX). | partly reuse |
| 9 | Autonomy + single owner | Hermes supervisor; single-orchestrator rule; ET/DST schedule. | reuse |
| 10 | Gating | KNOWN_STRATEGIES + enabled=false + ON→ARM. | reuse |
| 11 | Stale/gappy data acted on | Latest-bar-age freshness check before evaluate → skip+journal. | reuse |
| 12 | Reproducibility | Journal `detect.__version__` + threshold-hash + feature vector per detection. | reuse |

**If only three are perfect, make them 1, 2, 6** (lag parity, bar parity,
correlated-signal cap) — those are what separate a working deployment from a
blown account. The through-line: the geometry is the easy part; live fidelity +
correlation risk are where real-trade deployments fail.

## Phased plan

- **D1 — `_geometry.py`** (swing extractor + line fit + touches + convergence +
  contraction + ATR), pure + unit-tested, no-lookahead. *Verifiable now on real
  parquet, no DeepSeek, no labels.*
- **D2 — `ascending_triangle/detect.py`** with seed thresholds + `pattern.md`.
  Run it over a few parquet symbols; eyeball flags.
- **D3 — universe harvester** — the loose geometric pre-filter that sweeps the
  1500 × 3m/5m store and surfaces candidate windows into the gallery for the user
  to confirm/reject (high-recall net; this is the "learn from variation" input).
- **D4 — `_validate.py` + threshold-fitter** — fit the detector's
  thresholds/score to the distribution of confirmed positives vs negatives; the
  calibration suite (gallery `cases/`) becomes the regression gate. Active-learning
  ranking surfaces the most informative next candidates.
- **D5 — visual evaluation** — review flagged windows on the parquet chart
  (existing Pattern Trainer chart) to judge precision/recall; corrections feed D3/D4.
- **D6 — execution setup** under `strategy/<FAMILY>/` consuming matches on **live**
  data; gated (paper-eval before submit). **Gated by the "Execution pre-flight &
  risks" table above** — the 5 NEW items (streaming detector, bar reconciliation,
  future-blind labeling, signal/dedup layer, correlation risk gate) are required
  before ARM, not optional polish.
- **D7+ — outcome logging → optional ML** (residual classifier + separate
  success-predictor), trained on the labeled candidates D3–D4 already produced.

D1–D2 are buildable and verifiable immediately (no labels, no DeepSeek). D3–D5 are
the harvest→label→fit→evaluate loop (need the user's confirmations). D6 mounts to
execution. ML (D7) is explicitly last and optional.
