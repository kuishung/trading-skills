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

Build phase: D3 — IMPLEMENTED (harvest + random_sample + loose_prefilter);
active_learning_rank is the D4 ranker that lives here.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path
from typing import Callable, Iterator

from . import _features as feat

# Make resources.bars_store importable when run from the repo (TradeHunter root
# is two parents up from this file: strategy/patterns/_harvester.py).
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Default bar-window sizes the sweep tries (kept modest; the detector itself
# re-windows finely — harvest only needs to surface candidate REGIONS).
_HARVEST_WINDOWS = (20, 30, 45)


def loose_prefilter(f: dict) -> bool:
    """A LOOSE, high-recall predicate over a window's features — generous
    tolerances so a near-miss is shown rather than a real triangle missed.
    Callers may pass their own `prefilter` to `harvest`; this is the default."""
    return (
        f.get("res_r2", 0) > 0 and f.get("sup_r2", 0) > 0
        and abs(f.get("res_slope", 9)) <= 0.15          # flat-ish ceiling (generous)
        and f.get("sup_slope", -9) >= 0.03              # rising-ish support (generous)
        and f.get("contraction", 9) <= 0.95             # at least a hint of squeeze
    )


def _triangleness(f: dict) -> float:
    """A cheap 0..1 "how triangle-ish" score used to rank/threshold harvest hits
    (independent of the detector's calibrated score, on purpose)."""
    if f.get("res_r2", 0) <= 0 or f.get("sup_r2", 0) <= 0:
        return 0.0
    flat = max(0.0, 1.0 - abs(f["res_slope"]) / 0.15)
    rise = min(1.0, max(0.0, f["sup_slope"] / 0.10))
    squeeze = max(0.0, 1.0 - f["contraction"])
    fit = (f["res_r2"] + f["sup_r2"]) / 2.0
    return (flat * rise * squeeze * fit) ** 0.25


def harvest(symbols: list[str], timeframe: str, *,
            prefilter: Callable[[dict], bool] | None = None,
            max_per_symbol: int = 5) -> Iterator[dict]:
    """Yield candidate windows across `symbols`:
        {symbol, timeframe, start_t, end_t, features, prefilter_score}
    `prefilter` is a LOOSE predicate over a window's features (defaults to
    `loose_prefilter`). Incremental + no lookahead (each window uses only its own
    bars). Bounded per symbol so one noisy name can't flood the review queue. D3."""
    from resources import bars_store  # lazy — only this path needs pyarrow

    pf = prefilter or loose_prefilter
    for sym in symbols:
        try:
            bars = bars_store.load_bars(sym, timeframe=timeframe)
        except Exception:
            continue
        if not bars:
            continue
        hits: list[dict] = []
        for w in _HARVEST_WINDOWS:
            if w > len(bars):
                continue
            step = max(1, w // 3)
            for end in range(w, len(bars) + 1, step):
                window = bars[end - w:end]
                f = feat.window_features(window)
                if pf(f):
                    hits.append({
                        "symbol": sym, "timeframe": timeframe,
                        "start_t": window[0]["t"], "end_t": window[-1]["t"],
                        "features": f, "prefilter_score": _triangleness(f),
                    })
        # keep the best, non-overlapping (by end_t), up to the cap
        hits.sort(key=lambda h: h["prefilter_score"], reverse=True)
        kept: list[dict] = []
        for h in hits:
            if any(h["start_t"] <= k["end_t"] and k["start_t"] <= h["end_t"] for k in kept):
                continue
            kept.append(h)
            if len(kept) >= max_per_symbol:
                break
        yield from kept


def _scorer(detector):
    """Return (score_fn(features)->float, cutoff) from a detector module/object.
    Falls back to the harvest triangleness + 0.5 cutoff if the detector doesn't
    expose `_score`/`SEED_THRESHOLDS`."""
    score_fn = getattr(detector, "_score", None)
    thr = getattr(detector, "SEED_THRESHOLDS", None)
    if callable(score_fn) and isinstance(thr, dict):
        return (lambda f: score_fn(f, thr)), thr.get("score_min", 0.6)
    return _triangleness, 0.5


def active_learning_rank(candidates: list[dict], detector) -> list[dict]:
    """Order candidates by INFORMATIVENESS so the user's limited labelling budget
    is spent well: those the detector is least sure about (score nearest the
    decision threshold) first, plus a sample of high-confidence ones to catch
    silent failures. D4.

    Returns the candidates (each annotated with `_score`) reordered: most
    uncertain first, then a few highest-confidence as a spot-check tail."""
    score_fn, cutoff = _scorer(detector)
    scored = []
    for c in candidates:
        s = score_fn(c.get("features", {}))
        scored.append({**c, "_score": s, "_uncertainty": -abs(s - cutoff)})
    # most uncertain (score nearest cutoff) first
    by_uncertainty = sorted(scored, key=lambda c: c["_uncertainty"], reverse=True)
    # spot-check tail: a few highest-confidence ones moved to the end
    high_conf = sorted(scored, key=lambda c: c["_score"], reverse=True)
    spot = {id(c) for c in high_conf[:max(1, len(scored) // 10)]}
    main = [c for c in by_uncertainty if id(c) not in spot]
    tail = [c for c in by_uncertainty if id(c) in spot]
    return main + tail


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
    from resources import bars_store  # lazy

    rng = random.Random(rng_seed)
    syms = list(symbols)
    rng.shuffle(syms)
    for sym in syms:
        try:
            bars = bars_store.load_bars(sym, timeframe=timeframe)
        except Exception:
            continue
        if len(bars) >= window_bars:
            start = rng.randint(0, len(bars) - window_bars)
            window = bars[start:start + window_bars]
            detection = detector(window) if detector else None
            return {"symbol": sym, "timeframe": timeframe,
                    "start_t": window[0]["t"], "end_t": window[-1]["t"],
                    "bars": window, "detection": detection}
    return {"symbol": None, "timeframe": timeframe, "start_t": None,
            "end_t": None, "bars": [], "detection": None}
