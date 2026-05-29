"""Strategy adapter contract for the generic backtest harness.

The backtest harness (review/backtest.py) depends ONLY on this Protocol —
it knows nothing about DITP, GUNS, OS, or any future family. Strategies
plug into the harness by exposing a class that implements the methods
below; the adapter registry (_adapter_registry.py) maps strategy names
to those classes.

Adding a new strategy to the backtester takes 3 things:
  1. A class that implements BacktestAdapter (~30-50 LOC, typically a
     thin wrapper over the strategy's existing scanner + decision engine)
  2. One line in _adapter_registry._ADAPTERS
  3. Whatever the strategy's underlying logic needs (its own scanner,
     decision engine, etc. — outside the scope of this contract)

There is no "framework" beyond this Protocol. The harness loops bars
and calls these methods. That's it.
"""
from __future__ import annotations

from datetime import date
from typing import Protocol, Sequence, runtime_checkable


@runtime_checkable
class BacktestAdapter(Protocol):
    """Contract every backtestable strategy must satisfy.

    Required class attributes
    -------------------------
    name : str
        Registry key. Lower-snake-case. Examples: "ditp_p2", "guns_setup1".
        Must match the key in _adapter_registry._ADAPTERS.

    engine_version : str
        Semver string for the decision-engine the adapter wraps. Stamped
        on every trade dict + summary JSON for reproducibility. Bump when
        the underlying decision logic changes.

    primary_timeframe : str
        Bar timeframe the replay loop walks. Must be a value bars_store
        accepts ("1min" / "3min" / "5min" / "15min" / "daily"). Each
        strategy declares its own — the harness loads the right bars
        per (symbol, date) accordingly.


    Required methods
    ----------------
    """

    name: str
    engine_version: str
    primary_timeframe: str

    def pick_candidates(self, as_of_date: date) -> Sequence[dict]:
        """Return candidates the strategy would have shortlisted at the end
        of `as_of_date`, evaluable on the FOLLOWING trading day.

        Each candidate is a free-form dict, but MUST contain at minimum:
            - 'symbol'    (str)     — ticker to trade
            - 'atr_used'  (float)   — daily ATR for stop/target sizing

        All other fields are preserved verbatim as `candidate_meta` on the
        emitted trade dict, so metrics can bucket by any strategy-specific
        dimension (DITP's confluence_tier, GUNS's gap_pct, etc.).

        IMPORTANT: this method MUST be look-ahead-safe. It should only
        consult data dated ≤ as_of_date. Use scanner.detect_p2(as_of_date=...)
        or equivalent. Violating this destroys backtest honesty.
        """
        ...

    def entry_signal(self, candidate: dict, curr_bar: dict,
                     prev_bar: dict | None,
                     bars_so_far: list[dict]) -> bool:
        """Called once per primary-timeframe bar walked during a candidate's
        evaluation day. Returns True iff THIS bar triggers entry.

        `curr_bar` and `prev_bar` are bar dicts `{t, o, h, l, c, v}` per
        bars_store convention. `prev_bar` is None on the first bar of the
        session. `bars_so_far` is the chronological slice from session open
        through (and including) curr_bar — useful for EMA / pattern
        computation without re-loading.

        First-crossing convention: returning True for bar N should imply
        a NO for bar N-1 — the harness assumes the signal fires at most
        once per candidate per day, on the bar where the condition first
        becomes true. Once the harness records an entry, this method is
        not called again for that candidate-day.
        """
        ...

    def stop_price(self, candidate: dict, entry: float) -> float:
        """Compute the bracket's protective stop given entry. Pure
        arithmetic; no bar inspection. (Trailing stops are a Phase-4 hook,
        not this method.)"""
        ...

    def target_price(self, candidate: dict, entry: float) -> float:
        """Compute the bracket's profit target given entry. Pure
        arithmetic."""
        ...

    def tradeability_ok(self, candidate: dict, entry: float,
                        stop: float, target: float) -> bool:
        """Final post-trigger veto. Returning False discards the setup
        AFTER entry_signal fired (logged as 'rejected_tradeability' rather
        than as a trade). Used by DITP for the 2R ≤ 1×ATR rule; other
        strategies can return True unconditionally."""
        ...


# ---------- Optional hooks (Phase 4+) ----------
#
# These are NOT part of the required Protocol — the harness probes for them
# via hasattr() and only calls them if the adapter provides them. Adapter
# authors implement only what their strategy needs.
#
# Documented here so the eventual full surface is discoverable in one place.

# def update_trailing_stop(self, candidate: dict, current_stop: float,
#                          bars_since_entry: list[dict]) -> float:
#     """Phase 4. Called per bar after entry. Return the (possibly raised)
#     stop level. The harness clamps so the stop never moves DOWN."""

# def early_exit_check(self, candidate: dict,
#                      bars_since_entry: list[dict]) -> bool:
#     """Phase 4. Called per bar after entry. Return True to force an
#     immediate market-close exit (exit_reason='EARLY_WARNING')."""

# def add_to_winner_check(self, candidate: dict, entry: float,
#                         current_stop: float,
#                         bars_since_entry: list[dict]) -> dict | None:
#     """Phase 4. Called per bar after entry. Return a dict to add a
#     second leg to the position, or None for no-op. Single-shot per
#     candidate (harness will not call again once a leg is added).
#     Return shape: {'size_mult': float, 'shared_stop': float}."""
