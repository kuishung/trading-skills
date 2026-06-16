"""D3 — universe candidate harvester (the "learn from real variation" input).

Sweeps the seeded parquet universe (≈1500 tickers × 3m/5m via resources.bars_store)
with a LOOSE, high-recall geometric pre-filter and surfaces candidate windows for
the user to confirm/reject in the Pattern Trainer gallery. High recall / low
precision ON PURPOSE — better to show a near-miss than to miss a real triangle.

This is a BACKGROUND BATCH JOB that runs where the parquet lives (the R720
Hermes Windows VM, which already runs the ingest supervisor; or the Nous Linux
box if the MarketData parquet share is mounted there). It is NOT a live-signal
scanner — the universe is historical and is used only to harvest examples and to
visually evaluate the detector (DETECTOR_DESIGN.md, "Role of the historical universe").

The bridge that turns "saw many charts" into "smart": the universe supplies
VARIETY; the user's confirm/reject supplies MEANING; _calibrate fits thresholds
to the confirmed distribution.

Build phase: D3. Stub.
"""
from __future__ import annotations

from typing import Callable, Iterator


def harvest(symbols: list[str], timeframe: str, *,
            prefilter: Callable[[dict], bool],
            max_per_symbol: int = 5) -> Iterator[dict]:
    """Yield candidate windows across `symbols`:
        {symbol, timeframe, start_t, end_t, features, prefilter_score}
    `prefilter` is a LOOSE predicate over a window's features (generous
    tolerances — flat-ish highs + rising-ish lows). Incremental + no lookahead.
    Bounded per symbol so one noisy name can't flood the review queue. D3."""
    raise NotImplementedError("D3: universe harvester")


def active_learning_rank(candidates: list[dict], detector) -> list[dict]:
    """Order candidates by INFORMATIVENESS so the user's limited labelling
    budget is spent well: those the detector is least sure about (score nearest
    the decision threshold) first, plus a sample of high-confidence ones to
    catch silent failures. D4."""
    raise NotImplementedError("D4: active-learning ranking")


def random_sample(symbols: list[str], timeframe: str, *, window_bars: int = 40,
                  detector=None, rng_seed: int | None = None) -> dict:
    """Pick a RANDOM ticker + window from the parquet universe and (if `detector`
    given) run it, returning {symbol, timeframe, start_t, end_t, bars, detection}.

    Why random in addition to the pre-filter harvest: the harvester only surfaces
    windows that PASS the loose filter, so it can never show a clean pattern the
    detector MISSED. Random raw charts expose those false negatives (recall
    failures) + give unbiased negatives. Low-yield for positives on its own, so
    it's a COMPLEMENT to harvest/active-learning, not a replacement
    (DETECTOR_DESIGN.md "Candidate sources"). Works pre-detector too (random chart
    to draw a seed positive on). Offline/parquet only. D3.

    NB: `rng_seed` is passed in (workflows/replays must be reproducible); this
    module does not call an ambient RNG."""
    raise NotImplementedError("D3: random universe sampling")
