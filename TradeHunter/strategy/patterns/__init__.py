"""strategy.patterns — the pattern-recognition framework (SKELETON).

This package implements the SYSTEMATIC approach recorded in DETECTOR_DESIGN.md.
It SUPERSEDES the earlier ad-hoc idea (PATTERN_TRAINER_DESIGN.md Phase 2: "the
assistant emits detect.py" — an LLM writing a one-off detector with no shared
structure, no geometry layer, and no calibration story).

Four decoupled layers (only the L3 rule-scorer encodes a specific pattern):
    _geometry          L1  swings + geometric primitives (ticker-relative, no lookahead)
    _features          L2  window -> scale-invariant feature vector
    <slug>/detect.py   L3  rule scorer: features -> [{start_t,end_t,score,notes}]
    (execution lives in strategy/<FAMILY>/ + execution/, NOT here)

Calibration loop (the "smart from examples" part — human-in-the-loop):
    _harvester   D3  sweep the parquet universe -> candidate windows for review
    (gallery)    D3  user confirms/rejects candidates -> labeled set
    _calibrate   D4  fit thresholds to the labeled distribution (not hand-guessed)
    _validate    D4  calibration suite + walk-forward + no-lookahead probe

Everything here is deterministic geometry: NO machine learning, NO LLM, NO
hardcoded dollar levels. ML is a later, optional layer (DETECTOR_DESIGN.md D7)
that trains on the same labeled candidates this loop produces.
"""
