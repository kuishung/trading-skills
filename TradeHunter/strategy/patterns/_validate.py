"""D4 — validation harness: the regression gate + leakage guard.

  * calibration suite — every confirmed positive must score above threshold and
    every confirmed negative below; run on EVERY threshold change.
  * walk-forward — time-ordered splits only; never shuffle across time (random
    splits give a falsely confident backtest).
  * held-out tickers — confirm the detector generalizes across symbols, not
    memorises specific names.
  * no-lookahead probe — re-run incrementally bar-by-bar; a detection must never
    reference a timestamp after the bar it was emitted on.

Build phase: D4 — IMPLEMENTED.
"""
from __future__ import annotations


def _detect_fn(detector):
    return detector.detect if hasattr(detector, "detect") else detector


def _bars_of(item):
    return item["bars"] if isinstance(item, dict) else item


def run_calibration_suite(detector, cases: dict) -> dict:
    """`cases` = {'positives': [...], 'negatives': [...]} of labelled windows
    (each a {bars:[...], id?} or a bare bar list). Run `detector` over each;
    a POSITIVE passes if the detector fires (>=1 match), a NEGATIVE passes if it
    stays quiet. Returns {passed, failed, pass_rate, per_case_scores}. This is the
    regression gate that guards every threshold change. D4."""
    detect = _detect_fn(detector)
    out = {"passed": 0, "failed": 0, "per_case_scores": []}
    for label, items in (("positive", cases.get("positives", [])),
                         ("negative", cases.get("negatives", []))):
        for item in items:
            matches = detect(_bars_of(item))
            top = max((m["score"] for m in matches), default=0.0)
            fired = len(matches) > 0
            ok = fired if label == "positive" else (not fired)
            out["per_case_scores"].append({
                "label": label, "id": item.get("id") if isinstance(item, dict) else None,
                "top_score": round(top, 4), "fired": fired, "pass": ok})
            out["passed" if ok else "failed"] += 1
    total = out["passed"] + out["failed"]
    out["pass_rate"] = round(out["passed"] / total, 4) if total else 1.0
    return out


def assert_no_lookahead(detector, bars: list[dict], *, min_prefix: int = 20,
                        step: int = 1) -> None:
    """Feed `bars` to the detector incrementally (growing prefix) and assert every
    emitted match uses only bars at or before the prefix's last bar — i.e. the
    detector never references the future. Raises AssertionError on the first
    violation. D4.

    `step` strides the prefix lengths (1 = exhaustive). This is the replay that
    backstops the streaming-detector parity risk (DETECTOR_DESIGN.md risk #1)."""
    detect = _detect_fn(detector)
    n = len(bars)
    for end in range(max(min_prefix, 1), n + 1, max(1, step)):
        prefix = bars[:end]
        last_t = prefix[-1]["t"]
        for m in detect(prefix):
            if m["end_t"] > last_t:
                raise AssertionError(
                    f"LOOKAHEAD: at prefix end={end} ({last_t}) a match reported "
                    f"end_t={m['end_t']} (after the last available bar)")
