# Ascending triangle — spec

The human-readable specification the detector (`detect.py`) implements. Edit this
when the *definition* changes; edit `SEED_THRESHOLDS` / run `_calibrate` when the
*numbers* change.

## Shape
A bullish continuation/consolidation pattern:
- **Resistance:** roughly **horizontal** line across the swing **highs** (price
  keeps hitting the same ceiling).
- **Support:** **rising** line across the swing **lows** (higher lows — buyers
  stepping up).
- The two lines **converge** toward an apex; price coils into the apex.
- Typically resolves with a **breakout above resistance**, often on rising volume.

## Rules (ticker-relative — see DETECTOR_DESIGN.md spec table)
| Aspect | Rule (v0 seed) |
|---|---|
| Resistance flatness | \|slope\| ≤ 0.05 ATR%/bar, R² ≥ 0.60 |
| Support rise | slope ≥ 0.10 ATR%/bar, R² ≥ 0.60 |
| Convergence | apex within 0.5–2.0 × window length ahead |
| Touches | ≥ 2–3 swing touches within 0.25 ATR of each line |
| Contraction | end range ≤ 0.60 × start range (in ATR) |
| Duration | 10–60 bars on 3m/5m |
| Volume (soft) | flat/declining into the apex (down-weight, not veto) |

Thresholds above are **calibration seeds**. The detector becomes "smart" by
`_calibrate.fit_thresholds()` fitting them to the user's confirmed examples vs
rejected near-misses (rising wedge, descending triangle, random consolidation).

## NOT an ascending triangle (hard negatives to reject)
- **Rising wedge** — *both* lines rise (support AND resistance), converging up.
- **Descending triangle** — flat support, *falling* resistance.
- **Channel** — parallel lines, no convergence.
- **Random consolidation** — low R² on either fit, no clean touches.

## Sources
Classical TA definition (well-known geometric pattern). No single framework doc;
this is general charting theory, implemented as parametric geometry per
`DETECTOR_DESIGN.md`.

## Status
- **v0.0.1 (skeleton, 2026-06-16)** — spec + seed thresholds recorded; detector
  body is a stub (build phase D2). Not yet calibrated, not wired to execution.
