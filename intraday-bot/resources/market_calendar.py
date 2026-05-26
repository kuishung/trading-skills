"""NYSE market calendar — full-closure dates + last/next trading-day helpers.

Strategy scanners (DITP P2 scanner.py + DITP TC tc_scanner.py + future
families) all need to answer the same two questions:

    "What was the last trading day on or before X?"
    "What is the next trading day after X?"

The naive weekday-only answer ("skip Sat/Sun") is wrong on holidays.
Memorial Day 2026 fell on Mon 2026-05-25 — markets closed — and the P2
scanner running EOD Fri 2026-05-22 was writing a watchlist targeted at
Memorial Day, then the TC scanner picking that file up resolved source
date to Mon 2026-05-25 and found zero bars in the parquets. The user
explicitly flagged this 2026-05-26: *"when scanning for the tickers we
will be looking at last trading day setup, skip the holiday"*.

This module is the single source of truth. Hardcoded list (no external
calendar dependency); update annually. Covers full closures only — NYSE
early-close days (Black Friday, Christmas Eve) are still regular trading
days for our purposes, so they're not listed here.

Source: https://www.nyse.com/markets/hours-calendars
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

# (year, month, day) tuples for NYSE full closures. Add a year's worth
# at a time as the calendar is published. Includes NYSE-specific observed
# dates (e.g. Saturday holidays move to the preceding Friday, Sunday
# holidays move to the following Monday — except New Year's Day, which
# is not observed when it falls on Saturday).
_NYSE_FULL_CLOSURES_RAW: tuple[tuple[int, int, int], ...] = (
    # 2024
    (2024, 1, 1),   # New Year's Day
    (2024, 1, 15),  # MLK Day
    (2024, 2, 19),  # Presidents' Day
    (2024, 3, 29),  # Good Friday
    (2024, 5, 27),  # Memorial Day
    (2024, 6, 19),  # Juneteenth
    (2024, 7, 4),   # Independence Day
    (2024, 9, 2),   # Labor Day
    (2024, 11, 28), # Thanksgiving
    (2024, 12, 25), # Christmas
    # 2025
    (2025, 1, 1),
    (2025, 1, 9),   # Day of mourning for Pres. Jimmy Carter
    (2025, 1, 20),
    (2025, 2, 17),
    (2025, 4, 18),
    (2025, 5, 26),
    (2025, 6, 19),
    (2025, 7, 4),
    (2025, 9, 1),
    (2025, 11, 27),
    (2025, 12, 25),
    # 2026
    (2026, 1, 1),
    (2026, 1, 19),
    (2026, 2, 16),
    (2026, 4, 3),
    (2026, 5, 25),  # Memorial Day — the one that triggered this module
    (2026, 6, 19),
    (2026, 7, 3),   # July 4 falls Saturday, observed Friday
    (2026, 9, 7),
    (2026, 11, 26),
    (2026, 12, 25),
    # 2027
    (2027, 1, 1),
    (2027, 1, 18),
    (2027, 2, 15),
    (2027, 3, 26),
    (2027, 5, 31),
    (2027, 6, 18),  # Juneteenth falls Saturday, observed Friday
    (2027, 7, 5),   # July 4 falls Sunday, observed Monday
    (2027, 9, 6),
    (2027, 11, 25),
    (2027, 12, 24), # Christmas falls Saturday, observed Friday
    # 2028
    # 2028-01-01 falls Saturday — NYSE rule: New Year's Day is NOT
    # observed when on Saturday (exchange is closed Saturdays anyway,
    # and Fri 2027-12-31 trades normally).
    (2028, 1, 17),
    (2028, 2, 21),
    (2028, 4, 14),
    (2028, 5, 29),
    (2028, 6, 19),
    (2028, 7, 4),
    (2028, 9, 4),
    (2028, 11, 23),
    (2028, 12, 25),
)

NYSE_FULL_CLOSURES: frozenset[date] = frozenset(
    date(y, m, d) for (y, m, d) in _NYSE_FULL_CLOSURES_RAW
)

# Inclusive year range covered by the hardcoded list. Outside this range
# the helpers fall back to a weekday-only check (and emit a warning via
# the CLI). Update both this constant and the raw list together.
KNOWN_YEAR_RANGE: tuple[int, int] = (2024, 2028)


def is_trading_day(d: date) -> bool:
    """True iff `d` is a normal NYSE trading day.

    Rules: weekday (Mon-Fri) AND not in the hardcoded full-closure list.
    For dates outside KNOWN_YEAR_RANGE the holiday check is skipped (the
    list isn't current); weekday check still applies. Callers that care
    can compare `d.year` against KNOWN_YEAR_RANGE themselves.
    """
    if d.weekday() >= 5:   # 5=Sat 6=Sun
        return False
    return d not in NYSE_FULL_CLOSURES


def last_trading_day(d: date | None = None) -> date:
    """Last NYSE trading day on or before `d` (inclusive). Default `d` =
    today's UTC date. If `d` itself is a trading day, returns `d`.
    Walks backward at most ~10 days (enough to cover any plausible
    consecutive-holiday cluster, including a year-end Christmas /
    New Year's bridge)."""
    if d is None:
        d = datetime.now(timezone.utc).date()
    out = d
    # Bound the walk — if we go more than 14 days without finding a trading
    # day, something's wrong (the holiday list is broken or we're outside
    # KNOWN_YEAR_RANGE in a way that confuses the check).
    for _ in range(14):
        if is_trading_day(out):
            return out
        out -= timedelta(days=1)
    raise RuntimeError(
        f"last_trading_day: walked >14 days back from {d} without finding "
        f"a trading day — holiday list bug or date out of range "
        f"(KNOWN_YEAR_RANGE={KNOWN_YEAR_RANGE})"
    )


def next_trading_day(d: date | None = None) -> date:
    """Next NYSE trading day strictly after `d`. Default `d` = today's UTC
    date. Walks forward at most ~10 days."""
    if d is None:
        d = datetime.now(timezone.utc).date()
    out = d + timedelta(days=1)
    for _ in range(14):
        if is_trading_day(out):
            return out
        out += timedelta(days=1)
    raise RuntimeError(
        f"next_trading_day: walked >14 days forward from {d} without "
        f"finding a trading day — holiday list bug or date out of range "
        f"(KNOWN_YEAR_RANGE={KNOWN_YEAR_RANGE})"
    )


# ---------- CLI ----------

def _main() -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(
        description="NYSE market-calendar helper. Print last/next trading "
                    "day relative to a reference date.",
    )
    ap.add_argument(
        "--date", help="YYYY-MM-DD reference date (default = today UTC)",
    )
    ap.add_argument(
        "--list-year", type=int,
        help="Print the hardcoded NYSE full-closure list for this year.",
    )
    args = ap.parse_args()

    if args.list_year:
        days = sorted(
            d for d in NYSE_FULL_CLOSURES if d.year == args.list_year
        )
        if not days:
            sys.stderr.write(
                f"# no holidays for {args.list_year} — outside "
                f"KNOWN_YEAR_RANGE={KNOWN_YEAR_RANGE} or list is empty\n"
            )
            return 1
        print(f"# NYSE full closures for {args.list_year}:")
        for d in days:
            print(f"  {d.isoformat()}  {d.strftime('%a')}")
        return 0

    ref = (
        date.fromisoformat(args.date) if args.date
        else datetime.now(timezone.utc).date()
    )
    print(f"reference date    : {ref.isoformat()} ({ref.strftime('%a')})")
    print(f"is_trading_day    : {is_trading_day(ref)}")
    print(f"last_trading_day  : {last_trading_day(ref).isoformat()}")
    print(f"next_trading_day  : {next_trading_day(ref).isoformat()}")
    if not (KNOWN_YEAR_RANGE[0] <= ref.year <= KNOWN_YEAR_RANGE[1]):
        print(
            f"WARN: ref.year={ref.year} is outside KNOWN_YEAR_RANGE="
            f"{KNOWN_YEAR_RANGE} — holiday data may be incomplete; "
            f"fix by extending _NYSE_FULL_CLOSURES_RAW in this file."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
