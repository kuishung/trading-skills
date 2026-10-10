"""Named constants of the Options module - ONE module, imported everywhere.

Every number an engine, the data layer or the page would otherwise bury in a
function body lives here with a one-line reason (OPTIONS_MODULE_DESIGN.md
II.2.3, design/options/part_B_engines.md B0.3). None of these is a member
preference (the member rules lived in ``opt_rules``, removed with the v2 page in
v4.135); what is here is the platform's convention (the chart stop is 1 ATR, the notional cap is
10 % of the account) or a data-quality bound (an IV of 8.3 is not a quote).

Everything a strike or a stop is measured against is ticker-relative - ATR
multiples, R multiples, ratios, counts of days - never a dollar figure (CLAUDE.md,
"Normalized strategy parameters"). The only absolute numbers are the data-quality
bounds and the contract multiplier.

The management constants of the credit-spread playbook (``ROLL_DELTA``,
``LOSS_STOP_FRACTION``, ``PROFIT_TARGET_FRACTION``, ``DTE_FLOOR``, ``ADJUST_DTE``,
``DELTA_ADJUST``, ``DELTA_CLOSE``, the bid/ask tiers and the liquidity floors) are
NOT restated: the engines import them from ``bull_put`` (B0.1), which stays
untouched. The few re-exported below are re-exported, not redefined.
"""
from __future__ import annotations

from .bull_put import (  # noqa: F401  - re-exports, one home for the playbook numbers
    ADJUST_DTE,
    ALT_NEAR_SHORTS,
    DELTA_ADJUST,
    DELTA_CLOSE,
    DTE_FLOOR,
    IDEAL_LEG_SPREAD,
    LOSS_STOP_FRACTION,
    MAX_LEG_SPREAD,
    MIN_LEG_VOLUME,
    MIN_OPEN_INTEREST,
    OI_PER_CONTRACT,
    PROFIT_TARGET_FRACTION,
    ROLL_DELTA,
)

# ---- pricing ----------------------------------------------------------------
MULT = 100                  # shares per US equity option contract: every $ figure on the page is per contract
RISK_FREE = 0.04            # the one rate behind every model number (bull_put.bs_put prices its 15-DTE curve with it, black_scholes.implied_vol solves with it)

# ---- the chart stop / target / pad (conventions, never fields) --------------
STOP_ATR = 1.0              # debit chart stop = entry - 1 ATR (the Curated / trade-tool convention "stop 1xATR(14) away")
TARGET_R = 2.0              # debit chart target = entry + 2R (same convention, "target at 2R")
LEVEL_PAD_ATR = 0.25        # a strike that must sit "under support" sits at least this far under the zone's low edge; the credit chart stop sits there too (336.2 on the LRCX fixture) - support_bounce.REACH_ATR lets a spring reach this far and still count as a test
SLOW_DRIFT_ATR = 0.75       # max absolute move of EMA20 over 10 sessions, in ATRs, for a "slow grind" (calendar / diagonal)

# ---- sizing ---------------------------------------------------------------
MAX_POSITION_PCT = 10.0     # notional cap: contracts x max loss <= 10 % of NLV (CLAUDE.md strict risk rule, global, never overridden, never a field)
STOP_IV_BUMP = 0.10         # RELATIVE IV lift when valuing a position at the chart stop: sigma_used = leg.iv x 1.10 (a 1-ATR down day lifts a liquid name's IV30 by about this much; it never flatters a seller's stop loss)
STOP_TIMES = (0, 0.5)       # the loss at the stop is taken at t = now AND at half the DTE and the LARGER is used - the worst regime for sellers (now) and for buyers (later)
GAP_MULT_HOUSE = 2.0        # house value of the member field shared.gap_mult: a gap through the stop may cost twice the risk budget, never seven times (the field lives in option_prefs; this is only its house figure)

# ---- IV history: how much before a rank means anything ---------------------
IV_MIN_OBS = 20             # spread_scan.IV_MIN_OBS: under this many readings no percentile is shown
IV_RANK_MIN_OBS = 60        # readings before the RANK (min/max based) is trusted over the percentile - about three months
IV_FULL_OBS = 252           # a full trading year: the rank is "over the last year"; under it the day count is said out loud

# ---- the premium gauge's gates (design 5.2 / 6c) ----------------------------
BUY_MAX_RANK = 30           # buy premium when the rank is at or under this ("ideally <= 30")
SELL_DIR_MIN_RANK = 30      # sell directional premium (credit spreads) from here up ("IV rank >= 30")
SELL_NEUTRAL_MIN_RANK = 50  # sell neutral premium (iron condor) from here up ("IV rank >= 50")
MID_LO, MID_HI = 30, 50     # the band where a debit vertical beats a naked long ("IV mid (30-50)"); at exactly 30 both buy and sell_directional are open on purpose
IV_HV_RICH = 1.10           # iv30 / hv20 at or above: options priced for 10 % more movement than the stock delivers - sellers are paid
IV_HV_CHEAP = 0.90          # iv30 / hv20 at or below: buyers get movement the market is not charging for
TERM_EVENT = 1.05           # term_ratio (iv_front / iv_back) at or above: an event is priced in the front month
TERM_CONTANGO = 0.95        # term_ratio at or below: the calendar's natural shape (front cheaper than back)

# ---- derived-metric windows (part A3) --------------------------------------
HV_SHORT = 20               # HV20: realised vol over the last 20 sessions (the IV/HV ratio's denominator)
HV_LONG = 60                # HV60: the slower realised figure shown beside it
TRADING_DAYS = 252          # annualisation of daily log returns
ATM_MAX_DIST_PCT = 0.15     # the two strikes bracketing spot must sit within 15 % of it, or the expiry has no usable ATM read
CM_TARGET_DAYS = 30         # the constant-maturity IV is 30 calendar days
CM_MIN_DTE = 5              # expiries under 5 DTE are settlement noise and are ignored by the CM interpolation
FRONT_TARGET_DTE = 30       # iv_front = the expiry nearest 30 DTE ...
FRONT_MIN_DTE = 7           # ... with at least 7 days left
BACK_TARGET_DTE = 75        # iv_back = the expiry nearest 75 DTE ...
BACK_MIN_DTE = 45           # ... with at least 45 days left
SKEW_DELTA = 0.25           # the 25-delta put / call pair the skew is read from
SKEW_DELTA_TOL = 0.07       # a leg counts as "25-delta" within this much of it
EXPECTED_MOVE_DAYS = 30     # iv_daily.expected_move is the 30-day one-sigma move

# ---- data-quality bounds (the only absolute numbers in the module) ----------
IV_SANITY_LO = 0.01         # a per-contract IV (fraction) at or under this is not a quote -> None
IV_SANITY_HI = 5.0          # a per-contract IV (fraction) at or over this is a feed artefact (MSFT deep-OTM strikes print 8.3) -> None; a deep-ITM 3.1099 is real and kept
IV_SERIES_LO = 0.1          # the bridge's daily IV series (PERCENT) is stored as-is only inside these bounds ...
IV_SERIES_HI = 1000.0       # ... (ivscan's _b bounding style)
IV_SERIES_MAX_POINTS = 400  # the bootstrap accepts at most this many points, dated <= today and >= today - 400 d
LIVE_MAX_ROWS = 400         # a posted (untrusted) live chain is capped at this many rows
LIVE_STRIKE_WINDOW = 0.50   # a live row whose strike sits more than 50 % from spot is dropped
PARTIAL_MIN_EXPIRIES = 2    # a chain with fewer listed expiries than this looks truncated ...
PARTIAL_MIN_DTE = 20        # ... or with no expiry at least this far out ...
PARTIAL_MIN_DELTA_SHARE = 0.30   # ... or with under 30 % of its rows carrying a delta
THIN_DELTA_LO = 0.03        # snapshot thinning after options_full_days: rows with |delta| under this ...
THIN_DELTA_HI = 0.97        # ... or over this are dropped (no strategy in the catalog picks there)

# ---- the picker's non-member knobs -----------------------------------------
LIQ_FACTOR_CLEAN = 1.0      # score multiplier for a position whose widest leg is inside IDEAL_LEG_SPREAD
LIQ_FACTOR_LIMIT = 0.9      # ... inside max_leg_spread but over the ideal
LIQ_FACTOR_OI_UNKNOWN = 0.85    # ... when the feed did not report open interest ("TWS did not say" is not evidence, but it is not comfort either)
SOFT_BAND_PENALTY = 0.9     # a debit vertical whose chart-forced short strike sits outside the member's delta band keeps the pick and loses this much score
CHAIN_STRIKES_EACH_SIDE = 12    # the full-chain expander shows +/- this many strikes around spot before "show all"

# ---- basket / platform -------------------------------------------------------
MAX_BASKET = 60             # tickers per member: 60 x ~2.5 s of Cboe pacing + engines fits the nightly task's 30-minute limit
BRIDGE_MIN_VERSION = "1.6"  # the IBKR bridge version whose /iv?series=1 feeds the IV bootstrap; every member string says "older than 1.6"
