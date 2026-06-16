"""D4 — validation harness: the regression gate + leakage guard.

  * calibration suite — every confirmed positive must score above threshold and
    every confirmed negative below; run on EVERY threshold change.
  * walk-forward — time-ordered splits only; never shuffle across time (random
    splits give a falsely confident backtest).
  * held-out tickers — confirm the detector generalizes across symbols, not
    memorises specific names.
  * no-lookahead probe — re-run incrementally bar-by-bar; a detection must never
    reference a timestamp after the bar it was emitted on.

Build phase: D4. Stub.
"""
from __future__ import annotations


def run_calibration_suite(detector, cases: dict) -> dict:
    """`cases` = {'positives': [...], 'negatives': [...]} of labelled windows.
    Run `detector` over each; return {passed, failed, per_case_scores}. This is
    the regression gate that guards every threshold change. D4."""
    raise NotImplementedError("D4: calibration suite")


def assert_no_lookahead(detector, bars: list[dict]) -> None:
    """Feed `bars` to the detector incrementally (growing prefix) and assert
    every emitted match uses only bars at or before its own end_t. Raises if the
    detector ever 'sees' the future. D4."""
    raise NotImplementedError("D4: no-lookahead probe")
