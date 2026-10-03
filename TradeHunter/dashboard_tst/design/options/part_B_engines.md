## B. Decision engines: premium gauge, strategy recommender, strike picker, sizing, order ticket, exits

Scope of this part: everything between "the data is in the DB" and "the card is on the
screen". Inputs are a stored chain (Part A's `option_chain_snapshot`, or a live chain from
the bridge), the IV history (`iv_history` / `iv_daily`), the daily bars the chart already
reads, and the member's rules. Outputs are plain dicts the nightly job writes into
`option_signal` and the page renders. **No I/O in any engine module**: every function here
takes data in and returns a dict, exactly the discipline `bull_put.py` already keeps
(`app/services/bull_put.py:7-13`), so the whole decision path is unit-testable on
synthetic chains with TWS off and Cboe down.

### B0. Placement, the chain contract, and the one normalisation layer

#### B0.1 Files

| File (all under `dashboard_tst/app/services/`) | Status | Role |
|---|---|---|
| `opt_legs.py` | new | leg normalisation (bridge vs Cboe rows), chain views (`by_expiry`, `dte_of`, `nearest_strike`), mid/width/liquidity helpers lifted from `bull_put._mid/_leg_spread/_abs_delta/_count/oi_needed` (`bull_put.py:129-162`) |
| `premium_gauge.py` | new | B1 — SELL / NEUTRAL / BUY and the three gates |
| `chart_state.py` | new | B2 — one `ChartState` dict per ticker from `ema_setup.analyze`, `support_bounce.find`, `structure.classify`, the range detector, the trend-line engine (another part) and earnings |
| `range_detector.py` | new | B2 — the mirror of `support_bounce` (resistance edge) + the range verdict |
| `strategy_rules.py` | new | B3 — the ten `StrategyRule` rows, `recommend()` |
| `option_prefs.py` | new | B4 — house defaults, per-family schema, `read(user)` merge, `prefs_hash` |
| `strike_picker.py` | new | B4 — enumeration per family, chart constraints, scoring, POP, words, top-3 |
| `payoff.py` | new | the generic legs → P/L at expiry and at time *t* (Black-Scholes). Used by sizing (B5) and by the payoff chart (another part) — one function, two consumers |
| `option_sizing.py` | new | B5 — contracts from risk % of NLV at the chart stop |
| `order_ticket.py` | new | B6 — the ticket dict and its TWS / moomoo text renderings |
| `option_exits.py` | new | B7 — the per-family monitor, extending `bull_put.monitor` |
| `models.py` | modify | `UserOptionPrefs`, `OptionTrade`, `OptionTradeCheck` (B4, B7) |
| `alembic/versions/f0a1b2c3d4e5_option_engines.py` | new | chained off `e2f3a4b5c6d7` (`alembic/versions/e2f3a4b5c6d7_iv_scan_items.py:13`), or off Part A's storage migration if that one lands first — one linear chain, whichever is later becomes this one's `down_revision` |

`bull_put.py` is **not modified**: the old `/options/{symbol}` tab keeps grading against
its constants (`bull_put.py:40-71`) and its tests keep passing. The new engines import its
pure helpers and its management constants (`ROLL_DELTA`, `LOSS_STOP_FRACTION`,
`PROFIT_TARGET_FRACTION`, `DTE_FLOOR`, `ADJUST_DTE`, `DELTA_ADJUST`, `DELTA_CLOSE` at
`bull_put.py:615-624, 69-71`) rather than restating them.

#### B0.2 The chain contract the engines consume

Two shapes exist today and they disagree on units and key names:

| | Cboe (`option_quotes.fetch_chain`, `option_quotes.py:103-181`) | Bridge (`bridge/ibkr_bridge.py:_row`, `ibkr_bridge.py:321-345`) |
|---|---|---|
| container | `chain["legs"][(expiry, right, strike)]` | `chain["puts"]` / `chain["calls"]` lists per ONE expiry (`chain["expiry_label"]`, `chain["dte"]`) |
| `iv` | **fraction** (0.46) — `SpreadCandidate.short_iv` comment "fraction as the chain gives it" (`models.py:1013`) | **percent** (46.0) — `round(g.impliedVol * 100, 1)` (`ibkr_bridge.py:330`) |
| open interest | `open_interest` | `oi` |
| theta | per day, per share | per day, per share (`g.theta`) |
| missing quote | `bid`/`ask` None, or 0.0 ("Cboe sends 0.0 for no quote", `option_quotes.py:93-100`) | None |

`opt_legs.norm_leg(row, *, expiry=None, right=None) -> dict` produces ONE shape and every
engine reads only that:

```python
{
  "expiry": "2026-11-20", "right": "P", "strike": 320.0,
  "bid": 9.90, "ask": 10.25, "mid": 10.075, "last": None,
  "iv": 0.46,            # ALWAYS a fraction: bridge values > 3.0 are divided by 100
  "delta": -0.262,       # signed, as the feed gives it; abs() at the use site
  "gamma": 0.011, "theta": -0.186, "vega": 0.412,   # per share per day / per vol point
  "oi": 1840, "volume": 212,                         # int or None (= "not reported")
  "spread": 0.35,        # ask - bid, None when either side is missing
  "quote_ok": True,      # bid and ask both present and > 0
}
```

Rules in `norm_leg`: `iv` > 3.0 → divide by 100 (no equity option has 300% IV on a
delayed feed; a bridge percent always does); `oi` = `row.get("oi", row.get("open_interest"))`
through `bull_put._count` (`bull_put.py:148-156`); a 0.0 bid with a 0.0 ask → `quote_ok
False`, `mid None`. `opt_legs.chain_view(chain) -> {"spot", "iv30", "as_of", "source",
"by_expiry": {expiry: {"P": [legs sorted by strike], "C": [...]}, "dte": {expiry: int}}}`
accepts either raw shape (a bridge chain has one expiry; Part A's snapshot rows are the
Cboe shape re-read from the DB).

`dte_of(expiry, today)` = `(date.fromisoformat(expiry) - today).days`, the same arithmetic
as `spread_monitor._dte` (`spread_monitor.py:35-39`), with `today` = `spread_monitor.et_today()`
(`spread_monitor.py:204-212`) so a Malaysian evening does not count one day too few.

#### B0.3 Named constants (one module, `opt_constants.py`, imported everywhere)

| Constant | Value | Why |
|---|---|---|
| `RISK_FREE` | 0.04 | the rate `bull_put.bs_put` already prices the 15-DTE curve with (`bull_put.py:79`); one rate for every model number on the page |
| `STOP_IV_BUMP` | 0.10 | relative IV lift applied when valuing a position at the chart STOP: a drop to the stop comes with higher IV (spot-vol correlation). 10% is the lower end of what a 1-ATR down day does to a liquid name's IV30; it makes credit-spread stop losses honest and never flatters |
| `STOP_TIMES` | (0, 0.5) | the loss at the stop is evaluated at *t* = now and at half the DTE and the LARGER is used (B5) — one rule that picks the worst regime for sellers (now) and for buyers (later) |
| `SIDEWAYS_STACK_ATR` | 1.0 | max spread of EMA20/50/200 (max − min), in ATRs, for "flat" |
| `SIDEWAYS_SLOPE_ATR` | 0.5 | max absolute change of EMA50 over 20 sessions, in ATRs, for "flat" |
| `SLOW_DRIFT_ATR` | 0.75 | max absolute change of EMA20 over 10 sessions, in ATRs: a "slow grind" (calendar / diagonal) |
| `RANGE_LOOKBACK` | 120 | sessions the range edges may come from (six months; a year-old edge is history) |
| `RANGE_MIN_TOUCHES` | 2 | touches each edge needs (the design's "both edges touched ≥ 2 times") |
| `RANGE_MIN_WIDTH_ATR` / `RANGE_MAX_WIDTH_ATR` | 2.0 / 8.0 | a range narrower than 2 ATR leaves no room for condor wings; wider than 8 is not one range but the year's high and low |
| `LEVEL_PAD_ATR` | 0.25 | a strike that must sit "under support" sits at least this far under the zone's low edge: the same 0.25 ATR `support_bounce.REACH_ATR` lets a bounce stop short of a level (`support_bounce.py:110`) |
| `TARGET_RR` | 2.0 | the chart target for debit trades = entry + 2R (the Curated / trade-tool convention: "stop 1×ATR(14) away, target at 2R", `_price_chart.html:1283`) |
| `STOP_ATR` | 1.0 | the chart stop for debit trades = entry − 1 ATR (same convention) |
| `MAX_POSITION_PCT` | 10.0 | notional cap: contracts × max loss ≤ 10% of NLV (CLAUDE.md strict risk rule `max_position_pct = 10% of NLV`) — the second, rarely-binding cap in B5 |
| `IV_MIN_OBS` | 20 | `spread_scan.IV_MIN_OBS` (`spread_scan.py:69`): below this many IV observations no percentile is shown; the gauge reports "unknown" |
| `IV_RANK_MIN_OBS` | 60 | observations before the RANK (min/max based) is trusted over the percentile — ~3 months, the design's "rank after ~3 months, percentile earlier" |

### B1. The premium gauge

`premium_gauge.gauge(*, iv30, iv_series, hv20, hv60, iv_front, iv_back, front_dte,
back_dte, earnings_days=None) -> dict`. Pure. All IV / HV inputs are **fractions**
(Part A's `iv_daily` stores `iv30` in percent like `iv_history.iv30` — `models.py:948` —
so the caller divides by 100 once; HV20/HV60 are computed by Part A from the stored
daily closes as `std(log returns, last N) × sqrt(252)`).

#### B1.1 Inputs

| Input | Source | Note |
|---|---|---|
| `iv30` | chain `iv30` (Cboe `data.iv30`, `option_quotes.py:174`) or bridge `/iv` `iv_current / 100` | today's reading |
| `iv_series` | last 252 rows of `iv_history.iv30` before today (the query in `spread_scan.iv_percentile`, `spread_scan.py:222-236`) ∪ the IBKR bootstrap rows (decision 4) | oldest first |
| `hv20`, `hv60` | `iv_daily.hv20 / hv60` | realised vol of the stock, 20 and 60 sessions |
| `iv_front`, `iv_back` | the ATM IV of the expiry nearest 30 DTE and of the expiry nearest 60-90 DTE: ATM = the strike nearest spot, IV = mean of that strike's put and call `iv` | Part A stores `iv_by_expiry` JSON `{expiry: iv_atm}`; the gauge picks the two expiries |
| `earnings_days` | `prices.fetch_next_earnings(sym)["days"]` (`prices.py:178-191`) | explains a front-month bump |

#### B1.2 Formulas

```
iv_rank = (iv30 - min(series)) / (max(series) - min(series)) * 100      # None if max == min or n < IV_RANK_MIN_OBS
iv_pct  = spread_scan.percentile(series, iv30)                           # share of past days BELOW today, None if n < IV_MIN_OBS
iv_hv_premium = iv30 / hv20                                              # ratio; > 1 = options priced richer than the stock has moved
term_slope    = iv_front / iv_back                                       # > 1 = front dearer than back (an event is priced / backwardation)
```

`iv_rank` is the same formula the bridge computes from IBKR's series
(`ibkr_bridge.py:559-561`), so the server-side rank and the "Live" rank agree by
construction once the bootstrap has filled `iv_daily`.

#### B1.3 Gates and thresholds (named constants in `premium_gauge.py`)

| Constant | Value | Meaning |
|---|---|---|
| `BUY_MAX_RANK` | 30 | buy premium when the rank is at or under this (design §5.2: "ideally ≤ 30") |
| `SELL_DIR_MIN_RANK` | 30 | sell directional premium (credit spreads) from here up (§5.2: "IV rank ≥ 30") |
| `SELL_NEUTRAL_MIN_RANK` | 50 | sell neutral premium (condor) from here up (§5.2: "IV rank ≥ 50") |
| `MID_LO`, `MID_HI` | 30, 50 | the band where a debit vertical beats a naked long (bull call: "IV mid (30–50)") |
| `IV_HV_RICH` | 1.10 | IV at least 10% above realised: sellers are paid for more movement than the stock delivers |
| `IV_HV_CHEAP` | 0.90 | IV at least 10% under realised: buyers get movement the market is not charging for |
| `TERM_EVENT` | 1.05 | front ≥ 5% dearer than back: an event is priced in the front month |
| `TERM_CONTANGO` | 0.95 | front ≤ 95% of back: the calendar's natural shape |

The three gates are booleans on the result, evaluated on **whichever of rank / percentile
is trusted** (`measure` below):

```
gates = {
  "buy":             measure <= BUY_MAX_RANK,
  "sell_directional": measure >= SELL_DIR_MIN_RANK,
  "sell_neutral":    measure >= SELL_NEUTRAL_MIN_RANK,
  "mid":             MID_LO <= measure <= MID_HI,
}
```

Note that at exactly 30 both `buy` and `sell_directional` are True — intended: the
design's "two chips" case (§5.4) is the band 30–50 where the recommender ranks rather
than forces (B3).

#### B1.4 The verdict algorithm

```
def gauge(...):
    reasons = []
    n = len(iv_series)
    rank = iv_rank(...) if n >= IV_RANK_MIN_OBS else None
    pct  = spread_scan.percentile(iv_series, iv30)        # None under IV_MIN_OBS
    if rank is not None:   measure, basis = rank, "rank"
    elif pct is not None:  measure, basis = pct, "percentile"   # reasons: "IV rank needs 60 days of history (have n); using the percentile"
    else:                  measure, basis = None, None
    prem = iv30 / hv20 if (iv30 and hv20) else None
    term = iv_front / iv_back if (iv_front and iv_back) else None

    if measure is None:
        # no history: fall back to IV vs HV alone, flagged provisional
        if prem is None:   verdict = "UNKNOWN"; reasons += ["no IV history and no realised vol — press Live to bootstrap a year of IV"]
        elif prem >= IV_HV_RICH:  verdict = "SELL";    reasons += [f"IV {iv30:.0%} is {prem:.2f}x the stock's realised {hv20:.0%} (provisional — no IV history yet)"]
        elif prem <= IV_HV_CHEAP: verdict = "BUY";     reasons += [...]
        else:                     verdict = "NEUTRAL"; reasons += [...]
        provisional = True; gates = {k: False for k in gates}   # gates stay CLOSED without a rank
    else:
        if measure >= SELL_NEUTRAL_MIN_RANK:   verdict = "SELL"
        elif measure >= SELL_DIR_MIN_RANK:     verdict = "NEUTRAL"      # 30-50: either side, rank decides in B3
        else:                                  verdict = "BUY"
        reasons.append(f"IV {basis} {measure:.0f}: {'expensive' if verdict=='SELL' else 'cheap' if verdict=='BUY' else 'middling'} against its own year")
        # IV vs HV only MOVES the verdict inside the 30-50 band; outside it, it is a reason
        if verdict == "NEUTRAL" and prem is not None:
            if prem >= IV_HV_RICH:  verdict = "SELL";  reasons.append(f"options priced {prem:.2f}x the stock's realised move — sellers are paid")
            elif prem <= IV_HV_CHEAP: verdict = "BUY"; reasons.append(...)
        provisional = False
    if term is not None:
        if term >= TERM_EVENT:     reasons.append(f"front month {term:.2f}x the back — an event is priced" + (f" (earnings in {earnings_days}d)" if earnings_days is not None and earnings_days <= front_dte else ""))
        elif term <= TERM_CONTANGO: reasons.append("front month cheaper than the back — calendar shape")
    return {"verdict": verdict, "iv_rank": rank, "iv_pct": pct, "basis": basis, "obs": n,
            "iv30": iv30, "hv20": hv20, "hv60": hv60,
            "iv_hv_premium": prem, "term_slope": term,
            "iv_front": iv_front, "iv_back": iv_back,
            "gates": gates, "provisional": provisional, "reasons": reasons}
```

Why IV−HV only moves the verdict inside the band: a rank of 70 with IV at 0.95× HV is
still expensive *against its own year*, which is the question a seller asks; the ratio
is shown as a reason so the member sees that the stock has actually been moving as much
as the options say. Inside 30–50 the rank cannot decide, so the ratio does.

#### B1.5 Output and failure modes

| Situation | `verdict` | `gates` | UI (basket IV colour / card line) |
|---|---|---|---|
| ≥ 60 obs, rank 62 | SELL | sell_* True | amber "62 · sell premium" |
| 20–59 obs, pct 71 | SELL | from pct | amber "71st pct · rank in 23 days", hover: the reason |
| < 20 obs, HV present | provisional | all False | grey "IV 46% vs HV 38% · history too short" + the Live-to-bootstrap hint; no sell strategy is recommended (B3 treats closed gates as fail) |
| no `iv30` (chain missing) | UNKNOWN | all False | grey "no IV today" — the stale badge from Part A |
| `hv20` None (fewer than 21 closes) | by rank only | by rank | the IV−HV chip is omitted, not invented |
| `iv_back` None (no expiry 60–90 DTE listed) | by rank | by rank | term chip omitted; calendar / diagonal rules see `term=None` → "needs a back month" |

### B2. The chart-state contract

`chart_state.read(symbol, *, bars=None, long_bars=None, trendline=None, today=None) ->
ChartState` builds one dict per ticker. In the nightly job the bars come from Part A's
cached daily history (the same `/prices`-shaped dicts `services.prices.fetch_daily_ohlc`
returns, `prices.py:50`); on the page the same function runs on the live bars — so the
nightly card and a "Refresh" never disagree about what a candle is.

#### B2.1 The dict

```python
ChartState = {
  "symbol": "LRCX", "as_of": "2026-10-03", "close": 349.20, "atr": 11.54,     # ATR(14) Wilder, BEFORE today's bar (support_bounce.atr_series, support_bounce.py:150-166)
  "ema": {"e20": 345.1, "e50": 331.8, "e200": 298.4},                            # ema_setup.analyze(...)["ema20"/"ema50"/"ema200"] (ema_setup.py:428-429)
  "w_ema": {"e20": 322.0, "e50": 290.1, "e200": 231.5} | None,                   # ["w_ema20".."w_ema200"]; None until 200 weekly candles (ema_setup.py:354-361)
  "trend": "up" | "down" | "sideways" | "unclear",
  "trend_days": 34,                      # consecutive sessions the current stack has held
  "w_uptrend": True | False | None,      # ema_setup "w_uptrend" (ema_setup.py:357)
  "slow_drift": False,                   # |EMA20 now - EMA20 10 bars ago| <= SLOW_DRIFT_ATR x ATR
  "structure": {"state": "bullish"|"decelerated"|"unclear", "reason": "..."},   # structure.classify (structure.py:68-132)
  "setup": {...} | None,                 # the PRIMARY setup (B2.3), direction-bearing
  "setups": [ {...}, ... ],              # every setup found, primary first
  "range": {...} | None,                 # B2.4
  "trendline": {...} | None,             # B2.5 (another part computes it; this is the shape consumed)
  "levels": {"support": 340.0 | None, "resistance": 371.5 | None, "target_up": ..., "target_dn": ...},
  "earnings": {"date": "2026-10-22", "days": 19} | None,                         # prices.fetch_next_earnings
  "plan": {"direction": "up", "entry": 349.20, "stop": 338.0, "target": 371.6, "r": 11.2} | None,   # B2.6
  "evidence": ["EMA 20 > 50 > 200 for 34 sessions", "bounced off 340 on 1.8x volume, 3 touches", ...],
}
```

#### B2.2 Trend

| `trend` | Rule | Source |
|---|---|---|
| `up` | `e20 > e50 > e200` on the last close | `ema_setup.analyze(...)["uptrend"]` (`ema_setup.py:278`) |
| `down` | `e20 < e50 < e200` | mirror, computed in `chart_state` from the same EMA series (`ema_setup.ema`, `ema_setup.py:160-168`) |
| `sideways` | `(max(e20,e50,e200) - min(...)) <= SIDEWAYS_STACK_ATR x ATR` AND `abs(e50[-1] - e50[-21]) <= SIDEWAYS_SLOPE_ATR x ATR` AND `range is not None` | the design's "EMA stack flat within x ATR, a range whose two edges each have ≥ 2 touches" |
| `unclear` | anything else (a mixed stack, or flat EMAs with no range) | — |

`trend_days` counts back from today while the stack ordering is unchanged (an uptrend
that is 3 days old is a different thing from one 90 days old; the headline sentence
says which). `slow_drift` is computed for every trend; it is only read by the time-spread
rules.

#### B2.3 Setup kinds

Each setup dict has the common fields `{kind, direction: "up"|"down"|"neutral", level,
zone: [lo, hi], touches, vol_high, candle: {time, kind, low|high}, quality: 0..100,
summary}` plus kind-specific extras. Detection order and sources:

| `kind` | Direction | Detector | Level / extras | Quality (0–100) |
|---|---|---|---|---|
| `support_bounce` | up | `support_bounce.find(bars, d_emas, w_emas)` (`support_bounce.py:374-482`) — the shipped v4.124/125 detector, unchanged | `level`, `zone`, `touches` (`n_touches`, `n_low`, `n_flip`), `vol_high`, `d_ema`, `w_ema`, `bounce` | 50 + 10 per touch past the first (cap 80) + 10 if `vol_high` + 5 `d_ema` + 5 `w_ema`; a `vol_high` of False or None caps quality at 45 (the "near miss" of `ema_setup.rank`, `ema_setup.py:532-539`) |
| `resistance_reject` | down | `range_detector.find_resistance_reject(bars, ...)` = `support_bounce.find` run on **mirrored bars** (B2.4) | the same fields, prices un-mirrored; `candle.kind` = "pin" (shooting star) / "engulf" (bearish engulfing) | same scale |
| `trendline_bounce` | up (down) | the trend-line engine's own bounce read (another part): a pin bar / engulfing whose low (high) is within `support_bounce.TOL_ATR` (0.35 ATR) of the line's value on that bar | `line` (B2.5), `level` = line value today | 40 + 10 per touch past the second (cap 70) + 10 if `vol_high` |
| `ema_rebound` | up | `ema_setup.analyze`: `rebound` in ("EMA20","EMA50") with `fresh` or `held` (`ema_setup.py:283-297`), or `pin_d` at an EMA (`ema_setup.py:340`) | `level` = that EMA's value today, `ema` = "EMA20"/"EMA50", `fresh`, `held`, `pin` | fresh 45 / held 30 / pin only 35 (+10 if a pin bar AND a rebound) |
| `breakout_retest` | up | `support_bounce.find` result with `n_low == 0 and n_flip >= 1` (a level made ONLY of old resistance highs — "a first retest, not a defended support", `support_bounce.py:65-68`) AND the breakout close above the zone happened within the last 20 sessions | same as support_bounce + `broke_on` (the first close > zone hi + tol) | 35 + 10 if `vol_high`; never above 50 (one retest is thin evidence) |
| `failed_support` | down | `range_detector.find_breakdown(bars)`: a level with ≥ 2 "low" touches (the support detector's level search WITHOUT the bounce-candle gate, over `RANGE_LOOKBACK`), the latest close `< level - support_bounce.BREAK_ATR x ATR` (`support_bounce.py:112`), the previous close `>= level - BREAK_ATR x ATR` (it broke TODAY or yesterday, `BREAK_RECENT = 2` sessions) and `volume_read` says high (`support_bounce.py:319-371`) | `level`, `zone`, `touches`, `broke_on`, `vol_high` | 40 + 10 per touch past the second (cap 60) + 10 if `vol_high` |
| `range` | neutral | `range_detector.find_range(bars)` (B2.4) | `lower`, `upper`, `lower_touches`, `upper_touches`, `width_atr`, `mid` | 40 + 5 per touch past 2 on each edge (cap 70) |

**Primary setup** = the highest-quality setup whose direction agrees with the trend
(`up` setups in an uptrend, `down` in a downtrend, `range` when sideways). When nothing
agrees (an uptrend with only a `resistance_reject`), `setup` is None and `setups` still
lists what was found — the recommender then rejects every setup-dependent strategy with
"no setup on the chart today" and the card says so.

#### B2.4 The range detector — `range_detector.py`, the mirror of `support_bounce`

The design (§5.4) asks for the support-bounce detector's mirror. Rather than re-deriving
a second set of pivot / touch / invalidation rules for highs, the module **mirrors the
bars and reuses `support_bounce`'s internals unchanged**, so both edges obey exactly the
same ATR-relative rules and any future fix to `support_bounce` fixes both edges.

```python
def mirror_bars(bars: list[dict]) -> list[dict]:
    """Price-negated bars: o'=-o, h'=-l, l'=-h, c'=-c, volume kept. A swing HIGH becomes a
    swing LOW, 'close held above the level' becomes 'close held below', the true range is
    unchanged so ATR is identical. session_frac is kept."""

def find_resistance_reject(bars, d_emas=(), w_emas=()) -> dict | None:
    r = support_bounce.find(mirror_bars(bars), [(n, -v) for n, v in d_emas], [(n, -v) for n, v in w_emas])
    return None if r is None else _unmirror(r)      # level -> -level, zone -> [-hi, -lo], touches' price -> -price, bounce.low -> candle.high
```

`support_bounce.is_pin_bar` on mirrored OHLC is exactly a shooting star (lower wick ↔
upper wick); `is_engulfing` on mirrored bars is exactly a bearish engulfing (a red candle
whose body swallows a prior green body) — no new candle code.

The range itself needs the LEVEL search without the "latest candle bounced" gate. `support_bounce.find`
fuses the two; the detector therefore calls the internals directly (they are module
functions, `support_bounce.py:169-280`):

```python
def _levels(bars, lo_i, end) -> list[dict]:
    """Every defended level in [lo_i, end]: seeds from _swings, members from _members,
    distinct still-valid touches from _touches — support_bounce.find's loop (support_bounce.py:434-463)
    minus the bounce test and the 'established / approached' gates. Returns
    [{level, zone, touches, n_low, n_flip}] with n_low >= RANGE_MIN_TOUCHES."""

def find_range(bars) -> dict | None:
    n = len(bars); atr = support_bounce.atr_series(...)[-2]
    lo_i = max(support_bounce.PIVOT + 14, n - 1 - RANGE_LOOKBACK); end = n - 1 - support_bounce.MIN_SEP
    lows  = _levels(bars, lo_i, end)                      # candidate LOWER edges (support)
    highs = [_unmirror(x) for x in _levels(mirror_bars(bars), lo_i, end)]   # candidate UPPER edges
    close = bars[-1]["close"]
    pairs = [(L, U) for L in lows for U in highs
             if L["zone"][1] < close < U["zone"][0]                                   # price is INSIDE
             and RANGE_MIN_WIDTH_ATR * atr <= U["level"] - L["level"] <= RANGE_MAX_WIDTH_ATR * atr
             and L["n_low"] >= RANGE_MIN_TOUCHES and U["n_low"] >= RANGE_MIN_TOUCHES]  # n_low on the mirrored side = touches of the high
    if not pairs: return None
    L, U = max(pairs, key=lambda p: (p[0]["n_low"] + p[1]["n_low"], -(p[1]["level"] - p[0]["level"])))   # most touches, then tightest
    # the range must not have been LEFT recently: no close beyond either edge by BREAK_ATR in the last BREAK_BARS
    ...
    return {"kind": "range", "direction": "neutral", "lower": L["level"], "upper": U["level"],
            "lower_zone": L["zone"], "upper_zone": U["zone"],
            "lower_touches": L["n_low"], "upper_touches": U["n_low"],
            "width_atr": round((U["level"] - L["level"]) / atr, 2), "mid": (L["level"] + U["level"]) / 2,
            "atr": atr, "touches": [...both edges' touch lists with a side field...]}
```

Both edges therefore carry `support_bounce`'s full definition of a touch — arrived from
≥ 1 ATR away, left by ≥ 1 ATR, distinct events ≥ `MIN_SEP` bars apart, invalidated by
`BREAK_BARS` closes beyond the level and re-validated by a reclaim — and every number is
an ATR multiple (CLAUDE.md).

#### B2.5 Trend line (consumed, not computed here)

The trend-line engine is another part (design §6d). This part reads:

```python
"trendline": {"direction": "up", "slope_per_day": 0.42, "value_today": 341.9,
              "touches": 3, "first_touch": "2026-06-12", "last_touch": "2026-09-29",
              "broken": False,                                 # a close > 0.5 ATR through it since the first touch
              "value_at": {"2026-11-20": 362.1, "2026-12-19": 374.3, ...}}   # pre-computed for every listed expiry in the snapshot
```

`value_at[expiry]` is what B4's chart constraint reads ("short strike under the trend
line's value at expiry"). If the engine is not yet built (step 1 of the phasing), the key
is None and every rule degrades to the horizontal level alone — stated on the card as
"trend line: not yet".

#### B2.6 The plan (stop / target the chart implies)

Every directional setup produces a stock-level plan, the same convention the chart's trade
tool seeds (`_price_chart.html:1283`: "stop 1×ATR(14) away, target at 2R"):

| Direction | entry | stop | target |
|---|---|---|---|
| up | the close (the nightly card) or `level x (1 + trade_prefs.offset_pct/100)` when the member has the Curated offset habit (`trade_prefs.read(user)["offset_pct"]`, default 0.3, `trade_prefs.py:30`) | `min(entry - STOP_ATR x ATR, setup.zone[0] - LEVEL_PAD_ATR x ATR)` — 1 ATR under the entry, and in any case under the level that must hold | `entry + TARGET_RR x (entry - stop)`, capped at `levels.resistance` when a resistance sits between 1.5R and 2R (a bull call spread's short strike then sits there) |
| down | mirror | `max(entry + STOP_ATR x ATR, setup.zone[1] + LEVEL_PAD_ATR x ATR)` | `entry - 2R`, floored at `levels.support` |
| neutral (range) | — | the range edges ± `LEVEL_PAD_ATR x ATR` (a close beyond either edge is the stop for a condor / calendar) | — |

For a credit spread the **chart stop** is the stock price at which the thesis is wrong
(the level failed), not 1 ATR from entry: `stop = setup.zone[0] - LEVEL_PAD_ATR x ATR` for
a bull put (`336.2` for LRCX: zone low 339.1 − 2.9), the mirror for a bear call. B5 sizes
from that; B6 writes it on the ticket; B7 watches it.

The ISRG numbers in the brief are exactly this convention: entry 405.81, stop 394.27 (=
405.81 − 11.54, one ATR), target 429.14 (= 405.81 + 2 × 11.54 ... + 0.08 rounding of the
level-offset entry) — the engine reproduces them from `atr = 11.54`.

### B3. The strategy rule table

#### B3.1 `StrategyRule` — one row per strategy, a data structure, no code per strategy

```python
@dataclass(frozen=True)
class StrategyRule:
    key: str                 # "bull_put"
    label: str               # "Bull put spread"
    family: str              # "credit_vertical" | "debit_vertical" | "long" | "leaps" | "condor" | "time"
    direction: str           # "up" | "down" | "neutral"
    side: str                # "credit" | "debit"
    trends: tuple            # ChartState.trend values that fit
    setups: tuple            # setup kinds that fit ("*" = no setup needed)
    iv_gate: str             # gauge gate name that must be True: "buy" | "sell_directional" | "sell_neutral" | "mid_or_buy" | "any"
    term: str                # "any" | "front_ge_back" | "front_le_back"
    earnings: str            # "none_inside" | "defined_risk" | "any"   (what the earnings rule allows for this family)
    weekly: bool             # requires w_uptrend (LEAPS)
    needs: tuple             # extra ChartState facts: "target_level" | "resistance_level" | "support_level" | "slow_drift" | "range"
    dte: tuple               # (lo, hi) in days; for two-expiry strategies the FRONT leg's window
    step: int                # build phase from the design §9 (1..4)
    priority: int            # base rank when several fit (higher first)
    why: str                 # template, .format(**ctx)
    must_happen: str         # template: what has to happen for this to work
```

#### B3.2 The ten rows (design §5.2, verbatim conditions)

| key | label | family / dir / side | trends | setups | iv_gate | term | earnings | weekly | needs | dte | step | prio |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `buy_call` | Buy call | long / up / debit | up | support_bounce, trendline_bounce, ema_rebound, breakout_retest | buy | any | none_inside | no | — | 45–90 | 2 | 60 |
| `buy_put` | Buy put | long / down / debit | down | failed_support, resistance_reject, trendline_bounce | buy | any | none_inside | no | — | 45–90 | 2 | 60 |
| `bull_call` | Bull call spread | debit_vertical / up / debit | up | same as buy_call | mid_or_buy | any | defined_risk | no | target_level | 30–60 | 2 | 55 |
| `bear_put` | Bear put spread | debit_vertical / down / debit | down | same as buy_put | mid_or_buy | any | defined_risk | no | target_level | 30–60 | 2 | 55 |
| `leaps_call` | Buy LEAPS | leaps / up / debit | up, sideways | * (any; a pullback setup adds quality) | mid_or_buy | any | any | **yes** | — | 270–540 | 4 | 40 |
| `bull_put` | Bull put spread | credit_vertical / up / credit | up | support_bounce, trendline_bounce, ema_rebound | sell_directional | any | defined_risk | no | support_level | 30–60 | 1 | 60 |
| `bear_call` | Bear call spread | credit_vertical / down / credit | down | resistance_reject, trendline_bounce, failed_support | sell_directional | any | defined_risk | no | resistance_level | 30–60 | 1 | 60 |
| `iron_condor` | Iron condor | condor / neutral / credit | sideways | range | sell_neutral | any | none_inside | no | range | 30–45 | 3 | 60 |
| `calendar` | Calendar spread | time / neutral / debit | sideways, up, down | range, * (with slow_drift) | any | front_ge_back | none_inside (inside the BACK expiry) | no | slow_drift | front 20–30 / back 50–70 | 4 | 45 |
| `diagonal_call` | Diagonal call spread (poor man's covered call) | time / up / debit | up | * | mid_or_buy (the long leg is bought) | front_ge_back preferred (soft) | none_inside (inside the SHORT leg) | no | slow_drift, resistance_level | short 30–45 / long 180–365 | 4 | 45 |

`iv_gate = "mid_or_buy"` = `gates["mid"] or gates["buy"]` (rank ≤ 50). `earnings =
"defined_risk"` means: allowed through earnings only when the member's shared rule
`earnings_rule` is `"defined_risk_only"` (B4.1); with the default `"none_inside"` every
row behaves as `none_inside`. `bull_put.trends` deliberately excludes the old
`_trend`'s "neutral — price holds above EMA50 and EMA200" case (`routes/options.py:59-82`):
the design's condition is "uptrend + support holding"; the neutral case comes back as
`iron_condor` / `calendar` when the range exists.

`why` / `must_happen` templates (ctx = ChartState + gauge + pick):

| key | why | must_happen |
|---|---|---|
| bull_put | "{trend_sentence} It {setup_sentence}. Options are expensive (IV {basis} {measure:.0f}), so you are paid to sell a put spread below that {level_name}." | "{symbol} stays above {short_strike:g} until {expiry_label}. You keep the credit if it does nothing, drifts up, or even dips a little." |
| bear_call | mirror ("...above that resistance") | "...stays below {short_strike:g}..." |
| buy_call | "{trend_sentence} It {setup_sentence}. Options are cheap (IV {basis} {measure:.0f}), so you buy the move rather than sell insurance." | "{symbol} reaches {target:g} (2R) before {expiry_label}; the stop is {stop:g}." |
| buy_put | mirror | mirror |
| bull_call | "... IV is middling ({measure:.0f}): a bare call is dear, so the spread sells a call at your target {target:g} to pay for part of it." | "{symbol} closes above {breakeven:g} by {expiry_label}; above {short_strike:g} you have the whole {max_profit:,.0f}." |
| bear_put | mirror | mirror |
| leaps_call | "Long-term uptrend (weekly EMA 20 > 50 > 200). A deep-in-the-money call replaces the stock with {leverage:.1f}x the exposure per dollar and only {extrinsic_pct:.0f}% of the price paid for time." | "{symbol} keeps its weekly uptrend over the next {months} months; you roll it out when {roll_dte} days remain." |
| iron_condor | "Sideways: EMAs flat, price between {lower:g} (touched {lt}x) and {upper:g} ({ut}x). Options are expensive ({measure:.0f}), so you sell both sides outside the range." | "{symbol} stays between {short_put:g} and {short_call:g} until {expiry_label}." |
| calendar | "Price is sitting near {strike:g} and the front month is priced {term:.2f}x the back: you sell the dear near-term option and own the cheaper later one." | "{symbol} is near {strike:g} on {front_expiry_label} (between {be_lo:g} and {be_hi:g})." |
| diagonal_call | "Slow uptrend with a resistance at {resistance:g}: own a long-dated call (delta {long_delta:.2f}) and rent out a near-term call under that resistance each month." | "{symbol} grinds up but stays under {short_strike:g} by {short_expiry_label}; you re-sell the short call every cycle." |

#### B3.3 `recommend(chart: ChartState, gauge: dict, prefs: dict, *, snapshot_expiries: list[str]) -> dict`

```python
def recommend(chart, gauge, prefs, *, snapshot_expiries):
    fits, rejected = [], []
    for rule in RULES:                                   # catalog order = tie-break order
        fails = []                                       # (reason_key, text) in CHECK ORDER
        if chart["trend"] not in rule.trends:             fails.append(("trend", TREND_REASON[rule.direction][chart["trend"]]))   # "trending, not sideways" / "not an uptrend"
        if rule.setups != ("*",) and (chart["setup"] is None or chart["setup"]["kind"] not in rule.setups):
                                                          fails.append(("setup", "no %s on the chart today" % SETUP_WORD[rule.key]))
        if rule.weekly and not chart["w_uptrend"]:        fails.append(("weekly", "no weekly uptrend (EMA 20 > 50 > 200 on the weekly chart)"))
        g = gauge["gates"]
        if rule.iv_gate != "any" and not _gate(g, rule.iv_gate):
                                                          fails.append(("iv", IV_REASON[rule.side][gauge["verdict"]]))   # "options too expensive to buy (IV rank 62)" / "options too cheap to sell (IV rank 24)" / "IV history too short to say"
        if rule.term == "front_ge_back" and not (gauge["term_slope"] and gauge["term_slope"] >= 1.0):
                                                          fails.append(("term", "front month is not dearer than the back"))
        for need in rule.needs:
            if not _has(chart, need):                     fails.append(("needs", NEED_REASON[need]))   # "no resistance to cap the target at" / "EMAs are not flat" ...
        e = earnings_block(rule, chart["earnings"], snapshot_expiries, prefs)   # B3.4
        if e: fails.append(("earnings", e))
        if fails:
            rejected.append({"key": rule.key, "label": rule.label, "reasons": [t for _, t in fails], "n_fail": len(fails), "step": rule.step})
        else:
            fits.append({"key": rule.key, "label": rule.label, "score": _score(rule, chart, gauge), "step": rule.step, "why": ..., "must_happen": ...})
    fits.sort(key=lambda f: (-f["score"], RULES_INDEX[f["key"]]))
    near = sorted([r for r in rejected if r["n_fail"] == 1], key=lambda r: RULES_INDEX[r["key"]])[:2]
    others = [r for r in rejected if r not in near]
    return {"recommended": fits[0] if fits else None, "also_fits": fits[1:],
            "rejected_shown": near,          # up to 2, greyed WITH the reason (decision 9)
            "others": others,                # behind "other strategies"
            "chips": [...recommended, also_fits..., near...]}
```

**Ranking when several fit** (`_score`, deterministic, explainable, every term shown on hover):

```
score = rule.priority
      + iv_fit        # how far inside its gate the measure sits: credit rows (measure - 30)/70*20, buy rows (30 - measure)/30*20, mid rows 20 - |measure - 40|; clipped 0..20
      + setup_quality / 5                      # 0..20 from ChartState.setup.quality (B2.3)
      + (10 if rule.term == "front_ge_back" and term >= TERM_EVENT else 0)
      + (5 if chart["structure"]["state"] == ("bullish" if rule.direction == "up" else "decelerated") else 0)
      - (10 if rule.step > current_step else 0)  # a strategy whose picker is not built yet never outranks one that is; it is still SHOWN with "coming in step N"
```

Worked case (design §5.4): uptrend, support bounce quality 80, rank 45 → `bull_put`: 60 +
(45−30)/70×20 = 4.3 + 16 = 80.3; `bull_call`: 55 + (20 − 5) = 15 + 16 = 86 → bull call
spread first, bull put spread "also fits"; at rank 62: bull_put 60 + 9.1 + 16 = 85.1 vs
bull_call rejected (mid_or_buy fails) → one chip, bull_call greyed "options expensive (IV
rank 62) — a spread you pay for is dear" as a near-miss. At rank 24: buy_call 60 + 4 + 16 =
80, bull_call 55 + 4 + 16 = 75 → buy call first, bull call also fits, bull put greyed
"options too cheap to sell".

#### B3.4 Earnings

`earnings_block(rule, earnings, expiries, prefs)` returns a reason string or None:

```
if earnings is None: return None                       # unknown: never blocks; the card shows "earnings: not known — check" (bull_put.select's non-blocking check, bull_put.py:395-397)
window = the listed expiries inside rule.dte (for two-expiry rules: the BACK/long expiry window for calendar, the SHORT window for diagonal — the leg whose life earnings must not cross)
inside = [x for x in window if earnings.date <= x]      # earnings on or before expiry = inside (bull_put.select gate 2, bull_put.py:382-390: ok = e > x)
allowed = rule.earnings == "any" or (rule.earnings == "defined_risk" and prefs["shared"]["earnings_rule"] == "defined_risk_only")
if inside and not allowed and len(inside) == len(window):
    return f"earnings {earnings.date} ({earnings.days}d) sits inside every {lo}-{hi} day expiry"
return None                                            # some expiry clears it: the picker will skip the ones that do not (B4.3)
```

`leaps_call.earnings = "any"`: a 9–18 month option crosses several reports by construction;
the LEAPS rule instead adds the reason-line "crosses earnings on {date}: the stop is the
weekly trend, not the print".

### B4. The strike picker

#### B4.1 Member preferences — schema, house defaults, plain-language labels

Storage: table `user_option_prefs` (design §4.3) — one row per member, portable JSON:

```python
class UserOptionPrefs(Base):
    __tablename__ = "user_option_prefs"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    prefs = Column(JSON, nullable=False, default=dict)     # ONLY the fields the member changed, per block
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)
```

Only overrides are stored (the `sym_conds` pattern: `routes/curated.py:104` passes the raw
stored dict through `ema_setup.clean_enabled`, which fills every missing key from
`COND_DEFAULT`, `ema_setup.py:455-462`). House defaults live in code
(`option_prefs.HOUSE`) for v1 — admin-editable later means moving `HOUSE` into a
single-row table with the same shape, and `read()` does not change.

**Sizing inputs are NOT duplicated here.** `risk_pct` and `nlv` are read from
`trade_prefs.read(user)` (`trade_prefs.py:71-91`; defaults `DEFAULT_RISK_PCT = 1.0`,
`DEFAULT_NLV = 0.0` = "not told yet") — the same two numbers that size a Curated share
trade, so one account value sizes everything (`trade_prefs.py:15-23`).

Schema: `option_prefs.SCHEMA = {block: {field: Field(default, lo, hi, kind, label, plain)}}`.
`kind` ∈ {"num", "int", "bool", "choice"}. The "plain" column is the one-line translation
the My-rules tab shows next to each field (design §7).

**shared**

| field | default | bounds | label | plain |
|---|---|---|---|---|
| `min_oi` | 500 | 0–100000 | Minimum open interest per leg | so you can get out — `bull_put.MIN_OPEN_INTEREST` (`bull_put.py:64`) |
| `oi_per_contract` | 10 | 1–100 | ... and at least this many times your contracts | `bull_put.OI_PER_CONTRACT` (`bull_put.py:65`) |
| `max_leg_spread` | 0.50 | 0.01–5.00 | Max bid/ask width per leg ($) | wide markets eat the edge — `bull_put.MAX_LEG_SPREAD`; `ideal` tier at 0.40 (`bull_put.py:51-52`) |
| `min_leg_volume` | 20 | 0–10000 | Traded today per leg (warning only) | `bull_put.MIN_LEG_VOLUME` (`bull_put.py:66`) — never vetoes |
| `earnings_rule` | `"none_inside"` | choice: none_inside / defined_risk_only | Earnings inside the trade | "not allowed" / "allowed for spreads and bought options, never for a trade whose loss is open" |
| `monthly_only` | False | bool | Monthly expiries only | third-Friday expiries (`spread_scan.is_monthly`, `spread_scan.py:74-81`) have the deepest markets |
| `chart_constraint` | True | bool | Strikes must respect the chart | short strikes under support / above resistance / outside the range |
| `max_position_pct` | 10.0 | 1–100 | Max of account in one trade (%) | `MAX_POSITION_PCT` — contracts × max loss never above this |

**credit_vertical** (bull put / bear call) — the mockup's line: "short strike delta
0.20–0.30 (≈ 70–80% chance it expires worthless) · DTE 30–60 · width · min credit 25% of
width · sell only when IV rank ≥ 30 · short strike must be under support + line"

| field | default | bounds | label | plain |
|---|---|---|---|---|
| `short_delta_lo` / `short_delta_hi` | 0.20 / 0.30 | 0.05–0.50 | Short strike delta band | "≈ 70–80% chance it expires worthless" (the old tab keeps `bull_put` 0.20–0.25; the house default follows the design) |
| `width_atr_lo` / `width_atr_hi` | 0.5 / 1.5 | 0.1–5 | Spread width, in ATRs | ticker-relative: ≈ $6–17 on LRCX (ATR 11.5), $1–3 on a $40 name. The $ figure is shown next to it |
| `long_offset_max` | 3 | 1–6 | Long strike at most this many listed strikes below | `bull_put.LONG_OFFSET_MAX` is 2 (`bull_put.py:42`); 3 lets the ATR width be met on $5-spaced chains |
| `credit_pct_min` | 25 | 5–60 | Minimum credit, % of width | "you must be paid at least a quarter of what you risk" |
| `dte_lo` / `dte_hi` | 30 / 60 | 7–180 | Days to expiry | design §5.2 (the playbook tab keeps 45–60) |
| `iv_gate_min` | 30 | 0–100 | Sell only when IV rank is at least | mirrors `SELL_DIR_MIN_RANK`; a member may raise it |

**debit_vertical** (bull call / bear put)

| field | default | bounds | label | plain |
|---|---|---|---|---|
| `long_delta_lo` / `long_delta_hi` | 0.60 / 0.70 | 0.3–0.95 | Long strike delta | "moves about 60–70 cents per $1 of the stock" |
| `short_delta_lo` / `short_delta_hi` | 0.25 / 0.35 | 0.05–0.6 | Short strike delta (SOFT when the chart target decides, B4.3) | "the strike you give the upside away at" |
| `reward_cost_min` | 1.0 | 0.2–5 | Minimum reward ÷ cost | "the most you can make is at least what you pay" |
| `dte_lo` / `dte_hi` | 30 / 60 | 7–180 | Days to expiry | |

**long** (buy call / buy put)

| field | default | bounds | label | plain |
|---|---|---|---|---|
| `delta_lo` / `delta_hi` | 0.60 / 0.70 | 0.3–0.95 | Delta | "stock-like, with ~35% less capital" |
| `theta_pct_max` | 1.0 | 0.1–5 | Max daily time decay, % of premium | "losing more than 1% a day while nothing happens is too fast" |
| `dte_lo` / `dte_hi` | 45 / 90 | 14–365 | Days to expiry | |
| `premium_stop_pct` | 50 | 10–100 | Rule stop: close at this % of premium lost (B7) | the rule stop; the chart stop usually fires first |

**leaps**

| field | default | bounds | label | plain |
|---|---|---|---|---|
| `delta_lo` / `delta_hi` | 0.70 / 0.80 | 0.5–0.95 | Delta (deep in the money) | "behaves like 70–80 shares per contract" |
| `extrinsic_pct_max` | 10 | 1–40 | Max time value, % of the STOCK price | "you pay at most 10% of the share price for time" (B4.5 explains the unit) |
| `months_lo` / `months_hi` | 9 / 18 | 6–36 | Months to expiry | |
| `roll_dte` | 180 | 60–365 | Roll out when this many days remain | the roll date (B7) |
| `delta_floor` | 0.55 | 0.3–0.7 | Roll down-and-out if delta falls under | delta drift (B7) |

**condor**

| field | default | bounds | label | plain |
|---|---|---|---|---|
| `short_delta_lo` / `short_delta_hi` | 0.15 / 0.20 | 0.05–0.35 | Short strike delta, each side | "≈ 1-in-6 chance on each side" |
| `wing_atr_lo` / `wing_atr_hi` | 0.5 / 1.5 | 0.1–5 | Wing width, in ATRs | |
| `credit_pct_min` | 30 | 5–60 | Minimum credit, % of the wider wing | |
| `dte_lo` / `dte_hi` | 30 / 45 | 7–120 | Days to expiry | |
| `roll_delta` | 0.30 | 0.1–0.6 | Act when either short delta reaches | B7 |
| `loss_stop_pct_credit` | 100 | 25–300 | Rule stop: loss as % of the credit | "close when the loss equals the credit you took" |

**time** (calendar / diagonal)

| field | default | bounds | label | plain |
|---|---|---|---|---|
| `cal_front_lo` / `cal_front_hi` | 20 / 30 | 7–60 | Calendar front leg DTE | |
| `cal_back_lo` / `cal_back_hi` | 50 / 70 | 30–180 | Calendar back leg DTE | |
| `cal_delta_tol` | 0.05 | 0.01–0.2 | How far from delta 0.50 the strike may sit | ATM |
| `cal_take_pct` | 25 | 5–100 | Take profit at this % of the debit | calendars pay in small steps |
| `diag_long_delta_lo` / `_hi` | 0.70 / 0.80 | 0.5–0.95 | Diagonal long leg delta | |
| `diag_long_dte_lo` / `_hi` | 180 / 365 | 90–730 | Diagonal long leg DTE (6–12 months) | |
| `diag_short_delta_lo` / `_hi` | 0.20 / 0.30 | 0.05–0.5 | Diagonal short leg delta | |
| `diag_short_dte_lo` / `_hi` | 30 / 45 | 7–90 | Diagonal short leg DTE | |

**Merge on read** — `option_prefs.read(user) -> dict`:

```python
def read(user) -> dict:
    raw = (user.option_prefs.prefs if getattr(user, "option_prefs", None) else {}) or {}
    out = {}
    for block, fields in SCHEMA.items():
        src = raw.get(block) if isinstance(raw.get(block), dict) else {}
        out[block] = {k: _coerce(src.get(k), f) for k, f in fields.items()}   # trade_prefs._num semantics: bad / out-of-range -> default (trade_prefs.py:61-68)
    tp = trade_prefs.read(user)
    out["shared"]["risk_pct"], out["shared"]["nlv"] = tp["risk_pct"], tp["nlv"]
    return out

def write(db, user, block, **fields) -> tuple[dict, str]:   # trade_prefs.write pattern: report out-of-range, never clamp silently (trade_prefs.py:94-134)
def prefs_hash(prefs) -> str:   # sha1(json.dumps(prefs, sort_keys=True))[:12] — the key under which option_signal.picks caches per-member picks
def family_of(strategy_key) -> str   # bull_put -> "credit_vertical" ...
```

#### B4.2 Candidate enumeration

`strike_picker.pick(strategy_key, chain_view, chart, gauge, prefs, *, nlv=None, today=None) ->
PickResult`. Internally: `enumerate_<family>()` → `apply_constraints()` → `liquidity()` →
`score()` → `top3()`. Every candidate is a dict:

```python
Candidate = {
  "strategy": "bull_put", "legs": [Leg, ...],          # Leg = norm_leg(...) + {"side": "sell"|"buy", "qty": 1}
  "expiry": "2026-11-20", "dte": 48,                   # the (front) expiry; two-expiry rows add "back_expiry", "back_dte"
  "net": -2.84,                                        # per share: NEGATIVE = credit received, POSITIVE = debit paid (sign convention used by payoff.py)
  "width": 10.0, "max_profit": 284.0, "max_loss": 716.0, "breakeven": [317.16],   # per contract $, breakevens as stock prices
  "pop": 0.74, "pop_kind": "keep" | "profit",          # B4.6
  "greeks": {"delta": 0.062, "theta": 0.023, "vega": -0.054, "gamma": ...},   # per share, position-signed (sold leg negated)
  "liquidity": {"tier": "clean"|"limit"|"wide"|"thin"|"unknown", "widest": 0.35, "min_oi": 1840, "vol_ok": True|False|None, "notes": [...]},
  "constraint": {"ok": True, "detail": "320 sits under support 340.9 and under the trend line at expiry (362.1)"},
  "score": 0.293, "why": [...], "words": {...},        # B4.7
  "contracts": 11, "sizing": {...},                     # B5 (None without NLV)
}
```

Expiry selection (shared): `window(expiries, dte_lo, dte_hi)` = every listed expiry with
`dte_lo <= dte <= dte_hi`, monthly-only filtered when `shared.monthly_only`, and with
earnings inside removed when the rule's earnings policy forbids (B3.4). If the window is
empty but an expiry lies within ±7 days of it, that one is admitted with the note "48 DTE,
just outside your 30–45" (the `bull_put.select` DTE check is non-blocking for the same
reason, `bull_put.py:399-402`).

| family | legs | strikes enumerated | expiries |
|---|---|---|---|
| credit_vertical | sell short (P for bull_put, C for bear_call), buy long further OTM | shorts: every strike with `short_delta_lo <= abs(delta) <= hi` (else the `bull_put.ALT_NEAR_SHORTS` = 2 nearest, flagged `in_band False`, `bull_put.py:197, 288-290`); longs: the next 1..`long_offset_max` listed strikes beyond the short whose width is within `[width_atr_lo, width_atr_hi] x ATR` | every expiry in the window (the old tab took ONE expiry from the bridge; the snapshot has all) |
| debit_vertical | buy long (C for bull_call, P for bear_put) at `long_delta` band, sell short at `short_delta` band further OTM | longs in band; shorts: the strike AT or just beyond the chart target (`levels.target_up` rounded UP to the next listed strike for calls, DOWN for puts) — the chart decides the short; the delta band is checked and reported, not enforced (B4.3) | window |
| long | buy one option | strikes in `delta` band; a leg fails when `abs(theta)/mid x 100 > theta_pct_max` | window |
| leaps | buy one call (LEAPS puts are not in the catalog) | strikes in `delta` band AND `extrinsic <= extrinsic_pct_max/100 x spot` where `extrinsic = mid - max(0, spot - strike)` | every expiry with `months_lo x 30 <= dte <= months_hi x 30` (270–540 days) |
| condor | sell put + buy lower put, sell call + buy higher call | each side exactly as credit_vertical with the condor band; wings in `[wing_atr_lo, wing_atr_hi] x ATR`; the four legs share one expiry | window |
| calendar | sell front, buy back, same strike, same right (calls when `close >= strike` else puts — the OTM side is cheaper to run) | the strike nearest the "sit" level: `range.mid` when sideways, else the close; keep the strike if `abs(abs(delta_front) - 0.5) <= cal_delta_tol` | front ∈ `[cal_front_lo, cal_front_hi]`, back ∈ `[cal_back_lo, cal_back_hi]`, **every front×back pair**, requirement `iv_front_atm >= iv_back_atm` per pair (term is per pair, not only the gauge's global slope) |
| diagonal_call | buy long-dated call (LEAPS band), sell near-term call | long: `diag_long_delta` band in the long window; short: `diag_short_delta` band in the short window, AND `strike_short <= resistance.level` (under the resistance — the design's rule) AND `strike_short > spot`; hard safety rule `(K_short - K_long) + mid_short >= mid_long` ("if it rips through the short strike you still come out ahead": the width plus the credit covers the long's cost) | long window × short window |

#### B4.3 Chart-derived constraints (design §5.3 — hard, not a preference, switchable off only by `shared.chart_constraint`)

| strategy | constraint on strikes | source fields |
|---|---|---|
| bull_put | `short_strike <= min(sup_lo, line_at_expiry) - LEVEL_PAD_ATR x ATR` where `sup_lo` = `setup.zone[0]` for a support bounce, the EMA value for an `ema_rebound` (`setup.level`), the line's value today for a `trendline_bounce`; `line_at_expiry = trendline.value_at[expiry]` when a trend line exists and is not broken | `chart.setup`, `chart.trendline` |
| bear_call | `short_strike >= max(res_hi, line_at_expiry) + LEVEL_PAD_ATR x ATR` | mirror |
| bull_call / bear_put | `short_strike >= plan.target` (calls) / `<= plan.target` (puts): the short leg caps at the chart target, never before it. The short delta band becomes a **soft** check: if the forced strike's delta is outside the band the pick carries the note "short delta 0.41, above your 0.35 — the chart target sits closer than your band" and loses `SOFT_BAND_PENALTY = 0.9` in score | `chart.plan.target` |
| buy_call / buy_put | none on the strike; `plan.stop` and `plan.target` feed sizing (B5) and the ticket (B6) | `chart.plan` |
| leaps_call | none; the weekly trend is the stop (B7) | — |
| iron_condor | `short_put <= range.lower_zone[0] - PAD` and `short_call >= range.upper_zone[1] + PAD` | `chart.range` |
| calendar | strike within `LEVEL_PAD_ATR x ATR` of the sit level (`range.mid` or close) | `chart.range` / close |
| diagonal_call | `spot < short_strike <= resistance.level`; resistance = `range.upper` or `levels.resistance` (the nearest resistance_reject / flip level above) | `chart.levels.resistance` |

When the constraint removes every candidate the result is the degenerate case `constraint`
(B4.8), and the nearest candidate that fails it is kept as `nearest` so the card can say
by how much.

#### B4.4 Liquidity (shared, from `bull_put._pair`, `bull_put.py:217-238`)

Per leg: `spread = ask - bid`; tier `clean` ≤ `IDEAL_LEG_SPREAD` (0.40), `limit` ≤
`max_leg_spread`, else `wide` (→ excluded). OI: `oi >= bull_put.oi_needed(contracts,
min_open_interest=min_oi, oi_per_contract=oi_per_contract)` on every leg (`bull_put.py:159-162`);
`None` OI = "unknown" (warn, never exclude — "TWS did not say is not evidence",
`bull_put.py:229-230`); known-thin → excluded. Volume: warn only. `liquidity_factor` =
1.0 clean / 0.9 limit / 0.85 when OI is unknown. For multi-leg candidates the position's
tier is its WORST leg.

#### B4.5 Scoring per family

All scores are dimensionless and only compared within one strategy; `why` strings say what
each pick is best at (the `rank_pairs` pattern, `bull_put.py:311-336`).

| family | score | notes |
|---|---|---|
| credit_vertical | `(credit / max_loss_per_share) x POP x liquidity_factor` where `credit / max_loss_per_share = credit / (width - credit)` — the return on the money at risk | `rank_pairs` sorts lexicographically (liquid, in-band, clean, ratio: `bull_put.py:308-309`); the product is the design's "credit ÷ max loss × POP" and keeps the same winners on the cases in `bull_put`'s tests (a wider pair pays more credit for more max loss and loses on ratio; a lower short wins POP and loses ratio). Hard floor `credit >= credit_pct_min/100 x width` |
| debit_vertical | `(reward / cost) x POP x liquidity_factor x (SOFT_BAND_PENALTY if short delta out of band)` with `reward = width - debit`, `cost = debit` | hard floor `reward / cost >= reward_cost_min` |
| long | `(delta / mid) x (1 - theta_drag / theta_max)` with `theta_drag = abs(theta) / mid` per day, `theta_max = theta_pct_max / 100` | "stock-like movement per $ of premium", discounted by how close the decay sits to the member's ceiling. Legs over the ceiling are already out |
| leaps | `(delta / mid) x (1 - extrinsic / (extrinsic_pct_max/100 x spot))` | leverage per $ with the least time value paid; the discount is 0 at the cap, 1 at zero extrinsic |
| condor | `(credit / max_loss_per_share) x POP_both x liquidity_factor`, `max_loss_per_share = max(width_put, width_call) - credit`, `POP_both = 1 - abs(delta_sp) - abs(delta_sc)` | hard floor `credit >= credit_pct_min/100 x max(width_put, width_call)` |
| calendar | `front_mid / net_debit` (front-month premium collected per $ of back-month cost) `x POP_model x liquidity_factor` | `net_debit = back_mid - front_mid` is the max loss |
| diagonal_call | `(short_mid x cycles / long_mid) x liquidity_factor` with `cycles = long_dte / short_dte` (how many short cycles the long leg can host) | "monthly income ÷ long-leg cost", annualised to the long leg's life; the safety rule in B4.2 is a hard filter |

`extrinsic_pct_max` is "% of the stock price", not of the option: at IV 32% and 15 months
even an ISRG call 85 points in the money (delta 0.84) carries 27% of ITS price as time
value, so "≤ 10% of the option's price" would never pass and the 0.70–0.80 band could never
be met. Measured against the share price the rule does what the design means — the member
pays at most 10% of the stock for time — and it is ticker-relative.

#### B4.6 POP definitions

The card word is fixed by side (decision 9): credit → "chance of keeping it", debit →
"chance of profit".

| strategy | POP | basis |
|---|---|---|
| bull_put / bear_call | `1 - abs(short_delta)` | the chain's own delta, the "~75–80% win probability" of the playbook (`bull_put.py:21, 251`) |
| iron_condor | `1 - abs(delta_short_put) - abs(delta_short_call)` | both sides, same approximation |
| buy_call / buy_put | `black_scholes.black_scholes(S=spot, K=breakeven, T=dte/365, r=RISK_FREE, sigma=leg.iv, kind).prob_itm` (`black_scholes.py:43-70`: `N(d2)` for a call, `N(-d2)` for a put) | P(S_T beyond the breakeven) under the leg's IV — the risk-neutral caveat in that module's docstring is shown as the one-line caption on the payoff chart |
| bull_call / bear_put | the same with `K = breakeven` (long strike + debit for calls) | |
| leaps_call | the same with `T = dte/365` (long) — honest and low; the card ALSO shows `delta` as "behaves like N shares" because the LEAPS thesis is the trend, not a one-expiry bet | |
| calendar / diagonal | `POP_model`: value the position at the FRONT expiry across S (`payoff.curve(legs, S_grid, t=front_dte)`, the back leg priced by Black-Scholes at its own IV with `front_dte` elapsed), find the breakevens where P/L crosses 0, then the lognormal mass between them: `N(d2(hi)) - N(d2(lo))` with `sigma = iv30`, `T = front_dte/365` | the only POP that needs the model, because a calendar's expiry payoff does not exist (design §6e) |

#### B4.7 Greeks in words and the `why` list

`strike_picker.words(candidate, chart) -> dict` renders every number a member sees:

| key | template | example |
|---|---|---|
| `delta` (credit) | "delta {d:.2f} ≈ a 1-in-{n} chance of finishing in the money" with `n = round(1/d)` | "delta 0.26 ≈ 1-in-4 chance of finishing in the money" |
| `delta` (debit) | "delta {d:.2f}: moves about {d*100:.0f} cents for every $1 the stock moves (like {d*100:.0f} shares)" | "delta 0.63: moves about 63 cents per $1 (like 63 shares)" |
| `theta` | sellers: "earns about ${t:.0f} a day while nothing happens"; buyers: "loses about ${t:.0f} a day ({pct:.1f}% of what you paid)" | "earns about $2 a day" / "loses about $22 a day (0.8%)" |
| `vega` | "a 1-point rise in IV {costs|gains} you about ${v:.0f}" | "a 1-point IV rise costs you about $5" (credit spread, short vega) |
| `pop` | "{p:.0%} chance of keeping it" / "{p:.0%} chance of profit" | |
| `collect` / `pay` | "you collect ${c:,.0f}" / "you pay ${c:,.0f}" per contract | |
| `risk` | "you risk ${m:,.0f}" (max loss) + ", but the chart stop at {stop:g} would lose ≈ ${l:,.0f}" when sizing exists | |

`why` per pick, from the top-3 set (the `rank_pairs` vocabulary extended): "best fit to
your rules" (rank 1), "most credit per $ risked", "highest chance", "most room below the
price" / "most room above", "smallest max loss", "cheapest per delta", "least time value",
"bid/ask at the limit", "open interest unknown — check in TWS", "barely traded today".

#### B4.8 Output shape and degenerate cases

```python
PickResult = {
  "strategy": "bull_put", "family": "credit_vertical", "prefs_hash": "9f1c2a7b4d0e",
  "status": "ok" | "degenerate",
  "picks": [Candidate, Candidate, Candidate],       # top 3 by score; picks[0].recommended = True; each with words, why, contracts
  "considered": 23,                                  # candidates before constraints / liquidity
  "degenerate": None | {"reason_key": ..., "text": ..., "nearest": Candidate | None, "fix": "..."},
  "rules_line": "delta 0.20–0.30 · 30–60 days · width 0.5–1.5 ATR ($6–17) · credit ≥ 25% · under support 340.9 + trend line",
}
```

| `reason_key` | when | card text (`text`) | `fix` (what the member can change) |
|---|---|---|---|
| `no_chain` | snapshot missing / stale beyond Part A's limit | "No option data for LRCX (as of —). Press Refresh." | — |
| `no_expiry` | window empty after earnings / monthly filters | "Every 30–60 day expiry has earnings Oct 22 inside it." | "wait until after earnings, or allow defined-risk trades through earnings in My rules → Shared" |
| `no_band` | no strike in the delta band in any admitted expiry | "No strike sits in your delta 0.20–0.30 band; nearest: 310P delta 0.18 and 325P delta 0.34." (the two `ALT_NEAR_SHORTS` shown, greyed, not recommended) | "widen the band" |
| `constraint` | the chart constraint removed every in-band candidate | "No strike in your band sits under support 340.9: the nearest under it is 325 (delta 0.29) — the market is paying you to sell closer than the chart allows." | "widen the band downward, or switch off the chart rule (not recommended)" |
| `credit_floor` | every pair pays under `credit_pct_min` | "The best pair pays 18% of its width; your minimum is 25%." | "lower the minimum, or wait for IV to rise" |
| `thin` | liquidity removed everything | "The strikes under your rules are too thin (open interest under 500)." | "lower the OI floor, or pick a more liquid name" |
| `theta_cap` (long) | every in-band leg decays faster than `theta_pct_max` | "Every 45–90 day call in your band loses more than 1% a day." | "go further out in time, or raise the ceiling" |
| `extrinsic_cap` (leaps) | no strike under the time-value cap | "No 9–18 month call is deep enough: the least time value is 12% of the share price (cap 10%)." | "raise the cap, or wait for IV to fall" |
| `no_term` (calendar) | no front×back pair with front IV ≥ back IV | "The front month is cheaper than the back in every pair — no calendar edge today." | — |
| `safety` (diagonal) | no long/short pair passes the width + credit ≥ long cost rule | "No short call under resistance 371.5 covers the long call's cost if the stock rips." | "a nearer resistance, or a lower long delta" |

The card always shows `rules_line`, the `considered` count and, for every degenerate reason
except `no_chain`, the `nearest` candidate greyed with the one number that failed — so "no
trade today" reads as a decision, not a blank.

### B5. Sizing — contracts from risk % of NLV at the CHART stop

`option_sizing.size(candidate, chart, prefs, *, nlv, nlv_source) -> dict`. Pure.

#### B5.1 The loss at the chart stop

`payoff.value(legs, S, t_years, *, iv_bump=0.0) -> float` prices the position per share at
stock price `S` with `t_years` elapsed since now: each leg's remaining life is
`(leg_dte/365 - t_years)`; a leg at or past its expiry is intrinsic; otherwise
`black_scholes.black_scholes(S, K, T_left, RISK_FREE, sigma=leg.iv x (1+iv_bump), kind).price`
(`black_scholes.py:43`) signed by side (`+` long, `-` short) and multiplied by `qty`. The
position's entry value is `-net` (so a credit of 2.84 enters at −2.84; a debit at +12.60).

```
loss_at_stop (per share) = max over t in STOP_TIMES x (front_dte/365):   entry_value - value(legs, S=stop, t, iv_bump=STOP_IV_BUMP)
loss_at_stop_usd         = max(0, loss_at_stop) x 100
```

Per family the max picks the honest regime automatically:

| family | at the stop the loss is largest… | STOP_TIMES picks | LRCX 325/315P at 336.2 (IV 46%→50.6%) | ISRG 395/430C Dec at 394.27 (IV 34%→37.4%) |
|---|---|---|---|---|
| credit_vertical / condor | now — the short legs still carry all their extrinsic | t = 0 | $99 (t=0) vs $51 (t=24d) → **$99** | — |
| long / debit_vertical / leaps | later — the long leg has decayed before the stock gets there | t = DTE/2 | — | $230 (t=0) vs $348 (t=38d) → **$348** |
| calendar / diagonal | at the front expiry edge: `t = front_dte` is used instead of DTE/2 | front expiry | | |

The `STOP_IV_BUMP` lifts IV by 10% on the way down for calls AND puts (a stop on a put trade
is a rally, and IV usually falls; the bump still applies — it over-states the loss on the
mirror side by a few dollars and never under-states it, which is the direction a sizing
rule must err).

#### B5.2 Contracts

```
risk_budget   = nlv x risk_pct / 100                         # trade_prefs.size: "what the account is willing to lose" (trade_prefs.py:165)
by_chart_stop = floor(risk_budget / loss_at_stop_usd)        # the sizing rule (decision in the brief)
by_notional   = floor(nlv x max_position_pct/100 / max_loss_usd)   # the MAX_POSITION_PCT cap (CLAUDE.md 10% rule)
contracts     = max(0, min(by_chart_stop, by_notional))
```

`floor`, never round up (`trade_prefs.size`: "rounding UP would spend more of the risk
budget than the member allowed", `trade_prefs.py:144-146`). When `loss_at_stop_usd == 0`
(a stop that cannot lose — e.g. a credit spread whose chart stop sits above every strike at
t=0 is impossible, but a degenerate `stop == entry` is), `by_chart_stop` is None and the
notional cap alone sizes it, with the note "the chart stop loses nothing on the model;
sized by the 10% cap".

The rule stop is computed alongside, for the ticket and the chart:

| family | rule stop | source |
|---|---|---|
| credit_vertical | `LOSS_STOP_FRACTION x max_loss_usd` = 20% of max loss (or the member's `spread_loss_stop_pct`, `trade_prefs.read()["loss_fraction"]`) | `bull_put.LOSS_STOP_FRACTION` (`bull_put.py:616`) |
| condor | `loss_stop_pct_credit/100 x credit_usd` | prefs |
| long | `premium_stop_pct/100 x premium_usd` | prefs |
| debit_vertical | `premium_stop_pct/100 x debit_usd` (same field, shared with long) | prefs |
| leaps | none in $ — the weekly trend (B7) | — |
| calendar / diagonal | `premium_stop_pct/100 x net_debit_usd` | prefs |

`fires_first` = whichever of `loss_at_stop_usd` and `rule_stop_usd` is smaller — the ticket
says "the chart stop (336.2) fires first: ≈ $99 against the rule stop's $137" or the reverse.

#### B5.3 Output and NLV source

```python
{"nlv": 100000.0, "nlv_source": "bridge" | "prefs" | None,
 "risk_pct": 1.0, "risk_budget": 1000.0,
 "loss_at_stop_usd": 99.0, "stop_price": 336.2, "stop_t_days": 0, "stop_iv": 0.506,
 "rule_stop_usd": 137.0, "rule_stop_kind": "20% of max loss", "fires_first": "chart",
 "by_chart_stop": 10, "by_notional": 14, "contracts": 10,
 "capital_at_risk_usd": 990.0,          # contracts x loss_at_stop_usd
 "max_loss_total_usd": 6830.0,          # contracts x max_loss_usd
 "note": None | "sized once you tell us the account value (My rules → Shared)" | "not even 1 contract fits your 1% — the stop is too far for this strike" }
```

NLV resolution, in order: (1) the `/account` figure the browser POSTs after a "Live" press
(`bridge/ibkr_bridge.py:604-614` returns `net_liquidation`; `routes/options.analyze`
already accepts `payload["nlv"]` + `nlv_source`, `routes/options.py:113-124`) — used for
that request only and labelled "from TWS"; (2) `trade_prefs.read(user)["nlv"]` when > 0
("your stored account value"); (3) None → `contracts` None and the note above. A "remember
this" button next to the TWS figure writes it through `trade_prefs.write(db, user, nlv=...)`;
it is never stored silently.

The nightly job sizes with (2) only — it runs with nobody at a PC — so the Telegram card
and the first paint carry the stored-NLV size, and a "Live" press re-sizes in place.

### B6. The order ticket

`order_ticket.build(candidate, chart, sizing, prefs, *, symbol, today) -> Ticket` and
`order_ticket.render(ticket, broker="tws" | "moomoo") -> str`. The ticket is what the card
hands over (design §3: "a ready order ticket ... to paste into the broker"); the platform
places nothing (DESIGN.md security posture; `routes/options.py:17-18, 151-154`).

#### B6.1 Structure

```python
Ticket = {
  "symbol": "LRCX", "strategy": "bull_put", "label": "Bull put spread",
  "contracts": 10,
  "legs": [  # OCC-style, one row per leg, qty already multiplied by contracts
    {"action": "SELL", "qty": 10, "right": "P", "strike": 325.0, "expiry": "2026-11-20", "ref_mid": 11.74, "ref_bid": 11.55, "ref_ask": 11.95},
    {"action": "BUY",  "qty": 10, "right": "P", "strike": 315.0, "expiry": "2026-11-20", "ref_mid": 8.57,  "ref_bid": 8.40,  "ref_ask": 8.75},
  ],
  "net": {"kind": "credit", "limit": 3.17, "floor": 2.50,          # limit = mid-mid; floor = the worst net still inside credit_pct_min (25% of the $10 width) — for a debit: the worst debit still inside reward_cost_min
          "per_contract_usd": 317.0, "total_usd": 3170.0,
          "work": "enter at the mid (3.17); if unfilled in a few minutes step down 0.05 at a time, never below 2.50"},
  "condition": None | {"on": "LRCX", "field": "last", "op": "<=", "value": 341.92,
                       "why": "the close (349.20) has run 2.4% past the level: enter on the dip to 0.3% above support 340.9 rather than chase"},
  "tif": "DAY",                                                     # entry orders are DAY; the exit orders below are GTC
  "stop":   {"chart": {"level": 336.2, "loss_usd": 990.0, "per_contract_usd": 99.0, "trigger": "LRCX last <= 336.20", "close_at": 4.16},   # close_at = the model's spread mark at the stop (t=0, IV x 1.1)
             "rule":  {"kind": "20% of max loss", "loss_usd": 1370.0, "per_contract_usd": 137.0, "close_at": 4.54}},   # close_at = credit + 20% of (width - credit)
  "target": {"chart": None,                                         # credit trades have no chart target
             "rule":  {"kind": "50% of the credit", "close_at": 1.59, "profit_usd": 1585.0}},
  "exits_text": "Close if LRCX closes under 336.20 (the 340 support failed) — or if the spread is marked at 4.54 (20% of max loss). Take profit by buying it back at 1.59 (half the credit). Close or roll with 21 days left (Oct 30) whatever the P/L.",
  "rationale": "...the why sentence (B3.2)...", "must_happen": "...",
  "warnings": ["open interest unknown on 315P — check the OI column in TWS before ordering", "quotes are 15-min delayed — re-check the mid after the open", "earnings Oct 22 are inside this trade — allowed by your rules; the stop is the only protection"],
  "as_of": "2026-10-02T16:00:00-04:00", "source": "cboe" | "bridge",
}
```

Condition rules (`condition` is None unless one applies):

| strategy / setup | entry condition | why |
|---|---|---|
| credit spread after a `support_bounce` whose close is more than `FRESH_MAX` (0.5%, `ema_setup.py:46`) above the level | `last <= level x (1 + offset_pct/100)` | the Curated habit: enter near the level, not after the bounce has run; the credit is larger there |
| credit spread on a `fresh` bounce / rebound (close within 0.5% of the level) | None — the price IS at the level | |
| debit / long after a bounce | same as credit: `last <= entry` when the close has run; None when fresh | |
| `breakout_retest` | `last >= zone_hi + LEVEL_PAD_ATR x ATR` | enter when the retest holds and price turns up, not while it is still testing |
| `failed_support` (bear trades) | `last <= broke_level - LEVEL_PAD_ATR x ATR` | confirm the break |
| condor / calendar / leaps | None | |

Chart target for debit trades: `target.chart = {"level": plan.target, "close_at": value(legs, S=target, t=DTE/2)}` — the model's estimate of what the position is worth if the stock reaches the target around mid-life, so the member has a limit price to place, not just a stock level.

#### B6.2 Rendering — TWS (conditional orders on the underlying)

The mechanics the user and I settled on earlier in this project: TWS attaches a
**condition** to an order from the order ticket's Conditional tab — a *Price* condition
on another contract (here the underlying stock), *Trigger method* Last, operator ≤ / ≥, and
the order transmits only when the condition is true. **One condition set per order**, so
the ticket renders three orders: the entry, the chart-stop exit, the profit exit.

```
LRCX — Bull put spread — 10 contracts — paste into TWS
ORDER 1 · ENTRY (combo, DAY)
  Strategy Builder → Vertical: SELL 10 LRCX 20 NOV 26 325 P / BUY 10 LRCX 20 NOV 26 315 P
  Limit CREDIT 3.17 (mid). Work it: if not filled in a few minutes, lower 0.05 at a time, never below 2.50.
  [Condition — only if shown] Conditional tab → Add → Price → LRCX (STK, SMART) → Last ≤ 341.92 → submit
ORDER 2 · CHART STOP (combo, GTC)
  BUY 10 LRCX 20 NOV 26 325 P / SELL 10 LRCX 20 NOV 26 315 P (close the spread) — Market, OR Limit DEBIT 4.16 (the model's mark at the stop)
  Conditional tab → Add → Price → LRCX (STK, SMART) → Last ≤ 336.20 → transmit when true
  (this is the chart stop; you would lose ≈ $990 here, against the max loss of $6,830)
ORDER 3 · TAKE PROFIT (combo, GTC)
  BUY 10 LRCX 20 NOV 26 325 P / SELL 10 LRCX 20 NOV 26 315 P — Limit DEBIT 1.59 (half the credit kept). No condition.
Rule stop (no order — the monitor watches it): if the spread is marked at 4.54 or more (20% of max loss), close it.
Time stop: close or roll with 21 days left (Oct 30), whatever the P/L.
```

For a two-expiry strategy the legs are listed per expiry; Strategy Builder's "Calendar" /
"Diagonal" presets are named. For a single long option the "combo" wording is dropped.

#### B6.3 Rendering — moomoo (price condition on another symbol)

moomoo's conditional order (Trade → Conditional Order → *Price condition*) lets the
trigger watch a **different symbol** from the one being traded — the underlying for an
option order — with a ≥ / ≤ price and then submits a limit order. Also **one condition per
order**, and moomoo places multi-leg strategies as separate legs or via its Options
Strategy ticket (no condition on the strategy ticket), so the ticket renders the entry
as the strategy ticket WITHOUT a condition when `condition` is None, and as per-leg
conditional orders when a condition exists:

```
LRCX — Bull put spread — 10 contracts — paste into moomoo
ENTRY (today)
  Options → Strategy → Bull Put Spread: sell LRCX 2026/11/20 325 Put, buy LRCX 2026/11/20 315 Put, qty 10, limit net credit 3.17 (floor 2.50). DAY.
  [If an entry condition is shown: Conditional Order → Price condition → symbol LRCX → last ≤ 341.92 → then the same strategy ticket]
CHART STOP (GTC, conditional)
  Conditional Order → Price condition → monitor symbol LRCX → trigger when last ≤ 336.20 → order: buy to close 325 Put qty 10 limit 18.20 / sell to close 315 Put qty 10 limit 14.04
  (two legs = two conditional orders, both on the same LRCX ≤ 336.20 trigger; net ≈ 4.16 debit)
TAKE PROFIT (GTC)
  Strategy ticket: close the spread at net debit 1.59. No condition.
```

Per-leg limit prices for the moomoo stop = the model's leg values at the stop price
(`payoff.value` per leg at S = stop, t = 0, IV bumped), rounded to the tick, with the note
"these are estimates — at the trigger, use the market's mid".

### B7. Exits per family — the Positions monitor

#### B7.1 Storage: `option_trades` (generic legs) and `option_trade_checks`

`OptionSpread` (`models.py:788-849`) holds exactly two put legs. The new positions table
holds any legs and keeps the per-trade override convention (NULL = the member's default):

```python
class OptionTrade(Base):
    __tablename__ = "option_trades"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    symbol = Column(String(20), nullable=False, index=True)
    strategy = Column(String(24), nullable=False)         # the StrategyRule.key
    family = Column(String(24), nullable=False)           # credit_vertical | debit_vertical | long | leaps | condor | time
    legs = Column(JSON, nullable=False)                   # [{side, right, strike, expiry, qty, entry_price, entry_delta, entry_iv}]  (portable JSON, as User.prefs)
    front_expiry = Column(String(10), nullable=False, index=True)   # earliest leg expiry (DTE grading)
    back_expiry = Column(String(10), nullable=True)                 # latest leg expiry when they differ
    net_entry = Column(Float, nullable=False)             # per share: negative = credit received, positive = debit paid
    contracts = Column(Integer, nullable=False, default=1)
    max_loss = Column(Float, nullable=True)               # per contract $, None for a long-only trade = premium
    chart_stop = Column(Float, nullable=True)             # stock level
    chart_target = Column(Float, nullable=True)           # stock level (debit trades)
    roll_dte = Column(Integer, nullable=True)             # LEAPS / diagonal long leg roll date
    paper = Column(Boolean, nullable=False, default=False)    # the auto-tracked system idea (step 2 of the phasing)
    signal_id = Column(Integer, nullable=True)            # the option_signal row it came from (Part A), for the track record
    roll_delta = Column(Float, nullable=True); loss_stop_pct = Column(Float, nullable=True)
    profit_target_pct = Column(Float, nullable=True); dte_floor = Column(Integer, nullable=True)   # the OptionSpread override set, same NULL convention (models.py:819-831)
    opened_at = Column(DateTime, default=_utcnow); status = Column(String(12), nullable=False, default="open")
    closed_at = Column(DateTime, nullable=True); close_reason = Column(String(24), nullable=True); note = Column(Text, nullable=True)
    checks = relationship("OptionTradeCheck", back_populates="trade", cascade="all, delete-orphan", order_by="OptionTradeCheck.checked_on")

class OptionTradeCheck(Base):          # SpreadCheck (models.py:852-901) generalised: one row per (trade, ET day), upsert like spread_monitor.record_check
    __tablename__ = "option_trade_checks"
    __table_args__ = (UniqueConstraint("trade_id", "checked_on", name="uq_option_trade_check_day"),)
    id, trade_id (FK option_trades CASCADE, index), checked_on (String(10), index)
    spot, mark (Float: per-share value, cost to close), pl (Float $), loss_pct, profit_pct, dte (front), back_dte
    net_delta (shares), theta ($/day), vega ($/vol pt), legs = Column(JSON)    # per-leg {mid, delta, iv} as seen that day
    state (String(10): OK|WATCH|ROLL|CLOSE|TAKE|UNKNOWN|EXPIRED), action (Text), reasons = Column(JSON)
    source (String(12), default "cboe"), error (Text), created_at
```

Migration `f0a1b2c3d4e5_option_engines.py` (`down_revision = "e2f3a4b5c6d7"` or Part A's
id): creates `user_option_prefs`, `option_trades`, `option_trade_checks` with the
`if table in inspector.get_table_names(): return` guard the existing migrations use
(`e2f3a4b5c6d7_iv_scan_items.py:20-21`), and a data step that **copies every open
`option_spreads` row into `option_trades`** (`strategy="bull_put"`, `family="credit_vertical"`,
two legs from `short_strike`/`long_strike`/`short_price`/`long_price`, `net_entry = -credit`,
overrides carried over, `note="migrated from option_spreads #<id>"`). `option_spreads` is
left in place (the old `/portfolio` stays reachable until removed, decision 5); the new
Positions panel reads only `option_trades`.

#### B7.2 Marking a trade

`option_exits.mark(trade, chain_view, today) -> snap`: per leg `option_quotes.leg(chain,
expiry, right, strike)` (`option_quotes.py:184-188`) → `norm_leg`; `mark = Σ side × qty ×
mid` (cost to close per share, the `spread_monitor` convention "mid is what a broker marks
at", `spread_monitor.py:17-24`); `pl = (-(net_entry) - mark) x 100 x contracts` for
credits is the existing `(credit - mark)`, and for debits `(mark - debit)`; both are the one
expression `pl = (mark_position_now - value_at_entry) x 100 x contracts` where
`value_at_entry = net_entry` and `mark_position_now = -mark` — one formula, sign carried by
`net_entry`. `mark_worst` pays every ask / hits every bid, as today. Position greeks:
`net_delta = Σ side x qty x delta x 100 x contracts`, `theta`, `vega` likewise
(`spread_monitor.py:134-143`). A missing leg → `error` naming it, with the listed-expiry
hint (`spread_monitor.py:111-126`); the geometry still renders.

#### B7.3 The rule table the monitor evaluates

`option_exits.grade(trade, snap, chart, prefs) -> verdict` with the `bull_put.monitor`
contract (`{state, action, reasons, urgent, *_breach, loss_pct, profit_pct}`,
`bull_put.py:673-675`) plus `stop_breach` / `target_breach` / `roll_breach`. Losing-side
lines win ties (`bull_put.py:669-671`); the first matching row in each family block decides
the state; `WATCH` fires at 80% of any losing line, 50% of the loss line and within 3 days of
a time line (the near-miss rule, `bull_put.py:739-754`).

| family | line | condition | state | action text (template) |
|---|---|---|---|---|
| **credit_vertical** (bull_put / bear_call) | chart stop | underlying CLOSE beyond `chart_stop` (≤ for bull put, ≥ for bear call) | CLOSE | "{sym} closed at {close} — under the {level} support the trade was sold against. Close it; the thesis is gone, whatever the P/L." |
| | delta | `abs(short_delta) >= roll_delta` (member 0.30, `ROLL_DELTA`; playbook 0.35–0.40 in `review`) | ROLL if `dte > ADJUST_DTE` (30) else CLOSE | existing `bull_put.monitor` text (`bull_put.py:765-774`) |
| | loss | `loss_pct >= loss_fraction` (20% of max loss) | ROLL / CLOSE | existing |
| | profit | `profit_pct >= profit_target` (50% of credit) | TAKE | existing (`bull_put.py:721-727`) |
| | time | `dte <= dte_floor` (21) | CLOSE | existing (`bull_put.py:728-737`) |
| **debit_vertical** (bull_call / bear_put) | chart stop | underlying close beyond `chart_stop` | CLOSE | "{sym} closed at {close}, through the stop {stop}. Sell the spread." |
| | chart target | underlying close beyond `chart_target` OR `mark >= 0.75 x width` (the spread has 75% of its max value) | TAKE | "Target {target} reached / spread worth {mark} of {width}: the last quarter comes slowest. Sell it." |
| | premium stop | `loss_pct_of_debit >= premium_stop_pct` (50%) | CLOSE | "Down {x}% of what you paid (your line is 50%)." |
| | time | `dte <= dte_floor` (21) and `pl < 0` | CLOSE | "{dte}d left and under water: time is now against you faster than the stock can help." |
| | time | `dte <= dte_floor` and `pl >= 0` | WATCH | "{dte}d left, in profit: decide — the short leg's gamma grows from here." |
| **long** (buy_call / buy_put) | chart stop | underlying close beyond `chart_stop` | CLOSE | "{sym} closed through the stop {stop}. Sell the {right}." |
| | chart target | underlying close beyond `chart_target` | TAKE | "Target {target} (2R) reached. Sell, or sell half and move the stop to the entry." |
| | premium stop | `loss_pct_of_premium >= premium_stop_pct` | CLOSE | |
| | theta | `abs(theta)/mark > theta_pct_max/100` (decay faster than the member's ceiling, now that the premium has shrunk) | WATCH | "Now losing {pct}% a day — faster than your {max}% ceiling. Hold only if the move is close." |
| | time | `dte <= dte_floor` (21) | CLOSE (if pl < 0) / WATCH (if pl ≥ 0) | "Inside three weeks the option bleeds fastest. Close, or roll out to the next cycle." |
| **leaps** | trend stop | `chart.w_uptrend` False on two consecutive weekly closes (the weekly stack broke) | CLOSE | "The weekly EMA stack has broken for two weeks — the stock-replacement thesis is over. Sell." |
| | delta drift | `delta < delta_floor` (0.55) and `dte > roll_dte` | ROLL | "Delta has fallen to {d:.2f} (bought at {d0:.2f}): the call no longer behaves like stock. Roll down to the {new_delta} strike in the same expiry." |
| | delta up | `delta >= 0.90` | TAKE (partial) | "Delta {d:.2f}: the leverage is gone, it is stock now. Roll UP to a {delta_lo}–{delta_hi} strike and take the difference." |
| | roll date | `dte <= roll_dte` (180) | ROLL | "{dte}d left — at your roll date. Roll out to the {months} month expiry while time value is still cheap." |
| **condor** | either side's delta | `abs(delta_short_put) >= roll_delta` or `abs(delta_short_call) >= roll_delta` (0.30) | ROLL (that side) if `dte > ADJUST_DTE` else CLOSE | "The {put/call} side's short delta is {d:.2f}: roll that side {further out/closer}, or close the condor." |
| | range stop | underlying close beyond `range.lower - PAD` or `range.upper + PAD` | CLOSE | "{sym} closed outside the range ({lower}–{upper}). The sideways read is wrong — close." |
| | loss | `loss_usd >= loss_stop_pct_credit/100 x credit_usd` (100% of credit) | CLOSE | |
| | profit | `profit_pct >= profit_target` (50% of credit) | TAKE | |
| | time | `dte <= dte_floor` (21) | CLOSE | |
| **calendar** | range stop | underlying close outside the model breakevens at entry (`be_lo`, `be_hi` stored in `legs` meta) | CLOSE | "{sym} at {close} is outside {be_lo}–{be_hi}: a calendar only pays when the stock sits still." |
| | profit | `profit_pct_of_debit >= cal_take_pct` (25%) | TAKE | "Up {x}% of the debit — calendars pay in small steps; take it." |
| | roll date | `front_dte <= 5` | ROLL | "Front leg expires in {d}d: buy it back and sell the next cycle at the same strike (or close both)." |
| | loss | `loss_pct_of_debit >= premium_stop_pct` (50%) | CLOSE | |
| **diagonal_call** | short leg ITM | `delta_short >= 0.50` | ROLL (short up and out) | "The short call is in the money (delta {d:.2f}): roll it up and out, keep the long." |
| | short roll date | `short_dte <= 7` | ROLL | "Short call expires in {d}d: let it expire or buy it back, sell next month's under {resistance}." |
| | long leg | the LEAPS rows above apply to the long leg (`roll_dte`, `delta_floor`, trend stop) | ROLL / CLOSE | |
| | loss | `loss_pct_of_debit >= premium_stop_pct` | CLOSE | |

The nightly `option_exits.sweep(db)` is `spread_monitor.sweep` (`spread_monitor.py:283-344`)
over `option_trades`: one chain fetch per underlying, per-member prefs, `record_check`
upsert per ET day, `actionable` = every `urgent` verdict → the Telegram push (B7.4) and the
nav badge. Paper trades (`paper=True`) are swept identically; their checks build the
system's track record (design §3).

#### B7.4 Telegram push (decision 10)

`scripts/_common.py` already has `telegram_env()` (`scripts/_common.py:562-574`: looks up
`telegram.env` through `_env_lookup`, falling back to `matp.env`) and `send_telegram(cfg,
html)` (`scripts/_common.py:577-620`: HTML parse mode, 4000-char chunking). The web app
cannot import `scripts/` directly without the TradeHunter root on `sys.path` —
`services/resources_bridge.py` already does exactly that for `resources.patterns`
(`structure.py:35`), so `option_exits.notify(cards)` does
`from . import resources_bridge; from scripts._common import send_telegram, load_config`
and sends one message per new `option_signal` card: the headline sentence, the
recommended pick's line ("sell the Nov 325/315 put spread for ~3.17, 71% chance of keeping
it, 10 contracts"), the stop line, and the card URL. Failure = a logged line, never an
exception (the function returns False on any error).

### B8. Worked examples

Numbers below are Black-Scholes at `RISK_FREE` 0.04 on the stated IVs (the test fixtures
in B9 are generated the same way), so every figure is reproducible; a live chain will
differ by the skew and the bid/ask. Today = 2026-10-03 (Friday). Listed expiries used: Oct 17
(14 DTE), Oct 31 (28), Nov 20 (48, monthly), Dec 19 (77, monthly), Jan 16 2027 (105).

#### B8.1 LRCX — spot 349.20, ATR 11.54, support 340 (zone 339.1–340.9, 3 touches, pin bar on 1.8x volume), IV rank 62, IV30 46%, HV20 38%, earnings Oct 22

**Gauge.** rank 62 (obs 252 after the bootstrap) → measure 62 ≥ 50 → `SELL`; `iv_hv_premium
= 0.46 / 0.38 = 1.21` → reason "options priced 1.21× the stock's realised move — sellers
are paid"; `term_slope`: Oct 31 ATM IV 0.50 / Dec 19 ATM IV 0.45 = 1.11 ≥ `TERM_EVENT` →
"front month 1.11× the back — an event is priced (earnings in 19d)". Gates: buy False,
sell_directional True, sell_neutral True, mid False.

**Chart state** (EMA values are the fixture's inputs). trend `up` (e20 345.1 > e50 331.8 >
e200 298.4, 34 days); structure bullish; setup `support_bounce` quality 50 + 20 (3 touches)
+ 10 (vol_high) = 80, level 340.0, zone [339.1, 340.9]; trend line (another part) 3
touches, value today 341.9, at Nov 20 = 362.1 — above the support, so the constraint
`min(sup_lo, line_at_expiry)` = 339.1: the horizontal level binds and the card says "under
support". Plan: direction up, entry 349.20; the credit stop is where the level has failed,
`zone_lo − LEVEL_PAD_ATR × ATR` = 339.1 − 2.89 = **336.2**; the debit plan's stop
`min(349.20 − 11.54, 336.2)` = 336.2 as well; target 349.20 + 2 × 13.0 = 375.2, capped by
nothing (no resistance within 1.5R–2R). The design mockup wrote 338 for this stop — that
is a pad of 0.1 ATR; the pad is a named constant and 0.25 is the default because it equals
the detector's own `REACH_ATR` tolerance (a spring 0.25 ATR under the zone is still a
test, not a break). Sizing at 338 instead of 336.2 differs by about $10 a contract.
Earnings {date: 2026-10-22, days: 19}.

**Recommender** (prefs = house; `earnings_rule = none_inside`):

| rule | checks | result |
|---|---|---|
| bull_put | trend up ✓, setup support_bounce ✓, gate sell_directional ✓, support_level ✓, earnings: window Nov 20 (48d) and Dec 19 (77d) [Oct 31 = 28 < 30] — Oct 22 ≤ both → inside every expiry, `defined_risk` not allowed under `none_inside` → **fails** | rejected (1 fail): "earnings Oct 22 (19d) sits inside every 30–60 day expiry" |
| bull_call | gate mid_or_buy ✗ (62), earnings ✗ | rejected (2) |
| buy_call | gate buy ✗, earnings ✗ | rejected (2) |
| iron_condor | trend sideways ✗, range ✗ | rejected |
| calendar | slow_drift? EMA20 moved 6.1 in 10 bars = 0.53 ATR ≤ 0.75 ✓; term front_ge_back ✓ (1.11); earnings inside the back expiry (Dec 19) ✗ | rejected (1): "earnings Oct 22 sits inside the back month" |
| others | trend ✗ | rejected |

`recommended = None`; `rejected_shown` = bull_put, calendar (the two single-fail rows);
card headline: "Uptrend for 34 days, bounced off support at 340 on high volume, options are
expensive (IV rank 62) — but earnings land on Oct 22 inside every expiry in your window.
Nothing to do until after earnings; the post-earnings idea will appear on Oct 23. (Allow
defined-risk trades through earnings in My rules to see the bull put spread now.)"

**With `earnings_rule = defined_risk_only`:** bull_put fits, score 60 + (62−30)/70×20 =
9.1 + 80/5 = 16 + 5 (structure bullish) = **90.1**; `recommended = bull_put`; chips:
[✓ Bull put spread] [Bull call spread · options too expensive to buy] [Buy call · options
too expensive to buy] [other strategies ▾].

**Picker** (Nov 20, 48 DTE; IV 0.46; band 0.20–0.30; width 0.5–1.5 ATR = $5.8–17.3; under
339.1 − 2.9 = 336.2):

| short | delta | long | width | credit (mid) | % width | max loss | POP | breakeven | constraint | score |
|---|---|---|---|---|---|---|---|---|---|---|
| 325P | 0.293 | 315P | 10 | 3.17 | 32% | $683 | 71% | 321.83 | 325 ≤ 336.2 ✓ | **0.328** |
| 325P | 0.293 | 310P | 15 | 4.51 | 30% | $1,049 | 71% | 320.49 | ✓ | 0.304 |
| 320P | 0.262 | 310P | 10 | 2.84 | 28% | $716 | 74% | 317.16 | ✓ | 0.293 |
| 320P | 0.262 | 305P | 15 | 4.03 | 27% | $1,097 | 74% | 315.97 | ✓ | 0.271 |
| 315P | 0.232 | 305P | 10 | 2.53 | 25% | $747 | 77% | 312.47 | ✓ | 0.260 |
| 330P | 0.325 | — | | | | | | | out of band (nearest above) | shown greyed |

Top 3 by score: **325/315** 0.328 ("best fit to your rules", "most credit per $ risked"),
**325/310** 0.304 ("most credit per contract" — $451 for $1,049 at risk), **320/310**
0.293 ("more room below the price", "smallest max loss of the three"). 315/305 (0.260,
"highest chance" 77%) is fourth and sits behind "show more". Words for 325/315: "delta
0.29 ≈ 1-in-3 chance of finishing in the money · you collect $317 · you risk $683 · 71%
chance of keeping it · earns about $2 a day · a 1-point IV rise costs you about $5". Rules
line: "delta 0.20–0.30 · 30–60 days · width 0.5–1.5 ATR ($6–17) · credit ≥ 25% · under
support 340.9".

**Sizing** (stored NLV $100,000, risk 1% = $1,000) for 325/315: loss at stop 336.2, IV
0.46 × 1.1 = 0.506: t=0 mark 4.16 → loss (4.16 − 3.17) × 100 = **$99**; t=24d mark 3.68 →
$51; max = $99. `by_chart_stop = floor(1000/99) = 10`; `by_notional = floor(10000/683) =
14`; **contracts 10**; capital at risk $990; max loss total $6,830; rule stop 20% × 683 =
$137/contract → "the chart stop fires first ($99 vs $137)".

**Ticket.** The B6.1 / B6.2 / B6.3 renderings ARE this trade: SELL 10 LRCX 20 NOV 26 325 P
/ BUY 10 LRCX 20 NOV 26 315 P; limit credit 3.17, floor 2.50 (25% of width); condition:
the close 349.20 is 2.4% above the level → beyond `FRESH_MAX` → entry condition `LRCX last
≤ 341.92` (340.9 × 1.003); chart stop order: GTC, conditional `LRCX last ≤ 336.20`, buy
the spread back (limit debit 4.16); take profit GTC at debit 1.59; rule stop line "close if
marked ≥ 4.54" (3.17 + 0.2 × 6.83); time stop Oct 30 (21 DTE). Warning: "earnings Oct 22
are inside this trade — allowed by your rules; the stop is the only protection".

**Exits** on a hypothetical Oct 20 check: spot 344, 325P delta 0.21, mark 2.40 → pl =
(3.17 − 2.40) × 100 × 10 = +$770, profit_pct 24%, loss_pct 0, dte 31 → `OK` "Inside every
line (delta 0.21, 0% of max loss used, 24% of credit captured). Hold." On Oct 23 after a
gap to 335 (close under 336.2): `CLOSE` by the chart stop — "LRCX closed at 335.0, under
the 340 support the trade was sold against" — fires before the delta line (0.31 ≥ 0.30
would also fire; the chart stop is listed first).

#### B8.2 ISRG — entry 405.81 / SL 394.27 / PT 429.14, ATR 11.54, earnings Oct 21, IV rank 41, IV30 34%, HV20 31%

**Gauge.** rank 41 → `NEUTRAL` band; `iv_hv_premium` 0.34/0.31 = 1.10 ≥ `IV_HV_RICH` → the
band tie-break moves the verdict to **SELL** with reason "options priced 1.10× realised —
sellers are (just) paid"; gates: buy False, sell_directional True, sell_neutral False,
**mid True**. Term 1.08 (earnings in 18d).

**Chart state.** trend up (e20 398.2 > e50 388.0 > e200 361.5, 21 days); setup `ema_rebound`
on EMA20 (`fresh`: close 0.4% above it... the level 404.60 = EMA20; entry = 404.60 × 1.003
= 405.81 — the brief's figures are exactly the Curated offset entry); plan: stop 405.81 −
11.54 = **394.27** ✓, target 405.81 + 2 × 11.54 = **428.89** (the brief's 429.14 is the
same plan with the offset applied before the ATR — within rounding; the engine reports
428.89 and the test asserts ±0.3). `levels.resistance` 431.0 (a flip level, 2 touches) —
within 1.5R–2R of entry, so `target_level` exists and the target is capped at 428.89 (the
nearer). Earnings {2026-10-21, 18d}.

**Recommender** (house prefs, `none_inside`): every 30–90 day expiry contains Oct 21 →
bull_put (defined_risk, not allowed) rejected (1 fail), bull_call (mid ✓, target_level ✓,
earnings ✗) rejected (1), buy_call (gate buy ✗ at 41, earnings ✗) rejected (2), leaps_call
(weekly ✓, mid ✓, earnings any ✓ — but `w_uptrend` requires 200 weekly candles; ISRG has
them → **fits**, score 40 + (20 − |41−40|) = 19 + 35/5 (ema_rebound quality 35) = 7 + 5 = 71
− 10 (step 4 not built) = 61). Result: `recommended = leaps_call` labelled "coming in step
4 — shown for the read", `rejected_shown` = bull_put ("earnings Oct 21 inside every 30–60
day expiry"), bull_call (same). Headline: "Uptrend, fresh rebound on the EMA20, IV
middling (rank 41). Earnings Oct 21 sit inside every swing expiry; the long-term read
supports a LEAPS call (step 4). Swing ideas return after earnings."

**With `defined_risk_only`:** bull_call fits: 55 + 19 + 7 + 5 = **86**; bull_put: 60 +
(41−30)/70×20 = 3.1 + 7 + 5 = 75.1 → **bull call spread recommended, bull put spread
"also fits"** — the design's two-chip case. buy_call greyed: "options not cheap enough to
buy outright (IV rank 41 > 30)".

**Picker — bull_call** (Dec 19, 77 DTE, since Nov 20's 48 DTE is inside 30–60 as well:
both enumerated; Dec shown): long in 0.60–0.70: 390C (δ 0.66, 32.5... at 77 DTE: 395C δ
0.62 mid 32.52, 390C δ 0.66); short forced by the chart target 428.89 → next listed strike
up = **430C** (δ 0.41 at 77 DTE — outside 0.25–0.35 → soft note, ×0.9):

| long | short | debit | reward | reward/cost | breakeven | POP (profit) | loss at stop 394.27 (t=38d, IV×1.1) | score |
|---|---|---|---|---|---|---|---|---|
| 395C (δ 0.62) | 430C | 15.62 | 19.38 | 1.24 | 410.62 | 46% | $348 | 1.24×0.46×0.9 = **0.513** |
| 390C (δ 0.66) | 430C | 18.47 | 21.53 | 1.17 | 408.47 | 47% | $382 | 0.495 |
| 400C (δ 0.59 — 0.01 under the band) | 430C | 12.93 | 17.07 | 1.32 | 412.93 | 45% | $309 | not enumerated (out of band); it would score 0.535 and is the `nearest` only when nothing is in band |

Top pick **395/430 Dec**. Words: "delta 0.62 − 0.41 = 0.21 net: moves about 21 cents per
$1 · you pay $1,562 · you risk $1,562 (the chart stop would lose ≈ $348) · 46% chance of
profit · loses about $4 a day". Sizing: `floor(1000/348) = 2`, notional `floor(10000/1562)
= 6` → **2 contracts**; rule stop 50% of debit = $781/contract → chart stop fires first.

**Picker — bull_put** (also fits; Nov 20 48 DTE; under the EMA20 level 404.6 − 2.9 =
401.7 — and under the stop 394.27 since the EMA is the level): band 0.20–0.30 → 385P (δ
0.30) / 380P (0.26) / 375P (0.23); pairs 385/375 credit 3.02 (30%) POP 70% score 0.304
(top), 380/370 2.66 POP 74% 0.268; loss at stop 394.27 (t=0): $100 → 10 contracts, notional
cap 14 → **10**.

**Ticket (bull_call 395/430 Dec, 2 contracts).** BUY 2 ISRG 19 DEC 26 395 C / SELL 2 ISRG
19 DEC 26 430 C, limit debit 15.62, ceiling 17.50 = `min(mid + 0.5 × widest_spread,
width / (1 + reward_cost_min))` = min(15.87, 35/2) → 15.87 is the working ceiling, 17.50
the absolute one; condition: fresh rebound → None; chart stop GTC conditional `ISRG last ≤
394.27`, sell the spread (model value at the stop today 13.32; the sizing used the
mid-life 12.14); chart target `ISRG last ≥ 428.89` → the spread is worth ≈ 21.97 at
mid-life → take-profit limit 21.95 (+$635 a contract), or hold toward 430 for the full
35.00 at expiry; warning "earnings Oct 21 inside — allowed by your rules".

**Exits.** Nov 3 check, spot 418, mark 20.4 → pl +$956 (27% of the $3,876 max profit), no
line → `OK`. Nov 24, spot 431.5 (close ≥ 428.89) → `TAKE` "Target 428.89 reached: the last
quarter comes slowest. Sell it." Alternative path: Oct 22 post-earnings gap to 388 →
`CLOSE` by the chart stop; the model marks the spread at 11.54 there (58 days left, IV ×
1.1) → loss (15.62 − 11.54) × 200 = $816, more than the $696 the sizing budgeted (2 × $348)
because a gap lands beyond the stop; the ticket's earnings warning said so.

#### B8.3 The other families, briefly, on the same two charts (what each engine returns)

| strategy | LRCX | ISRG |
|---|---|---|
| iron_condor | rejected: trend up, no range | rejected: trend up |
| calendar | with the earnings rule relaxed: 350 strike (close 349.2), front Oct 31 C350 19.41 (IV 0.50) / back Dec 19 C350 29.74 (IV 0.45), debit 10.33, front/debit 1.88, model breakevens ≈ 319–392, POP_model 54%; but `slow_drift` ✓ and term ✓ → fits only when earnings allowed; the gauge's "event priced" reason is the honest flag that the front IV is earnings, not edge | rejected: earnings inside the back month |
| leaps_call | w_uptrend ✓, but IV rank 62 fails `mid_or_buy` → rejected "the long leg would be bought at IV rank 62". For the picker alone (Jan 2028, 470 DTE, IV 0.42): 280C δ 0.79 extrinsic 39.3 = 11.3% of 349.2 → over the 10% cap; 270C δ 0.81 and 260C δ 0.83 out of band → degenerate `extrinsic_cap`: "the least time value in your band (280C) is 11.3% of the share price (cap 10%)" | Jan 2028 (IV 0.32) 340C δ 0.79, extrinsic 38.0 / 405.81 = 9.4% ✓ → pick; 320C δ 0.84 out of band; words "behaves like 79 shares · you pay $10,383 for exposure to $40,581 of stock (3.9x) · 9.4% of the share price is time value · 32% chance of profit by Jan 2028 — the thesis is the weekly trend, not this number"; sizing by the weekly-trend stop: stop = weekly EMA50 (290.1), model value there at mid-life 18.03 → loss ≈ $8,580/contract → `by_chart_stop` 0 at 1% of $100k, `by_notional` floor(10,000 / 10,383) = 0 → note "not even 1 contract fits: the stop is $8,580 away and one contract costs $10,383, over your 10% cap" |
| diagonal_call | rejected: `slow_drift` ✓ but resistance_level: 371.5 exists → passes; mid_or_buy ✗ (62) → rejected "the long leg would be bought at IV rank 62" | mid ✓, slow_drift (EMA20 moved 0.6 ATR in 10 bars) ✓, resistance 431 ✓ → fits at 45 + 19 + 7 + 5 − 10 = 66 (step 4): long Jan 2028 340C 103.83 δ 0.79, short Nov 20 430C (δ 0.36 at 48 DTE; ≤ 431 ✓) 11.36; safety (430−340) + 11.36 = 101.36 ≥ 103.83 ✗ → fails by 2.47 → degenerate `safety`: "no short call under resistance 431 covers the long call's cost if the stock rips (short of it by $2.47)"; `nearest` shown |

### B9. Test plan

Pure modules get synthetic-chain tests (the `bull_put` discipline); the chart modules get
synthetic bar series plus the live names already used in the README; the whole path gets
one end-to-end fixture per worked example. Style: the README changelog "Tested:" line.

**Fixtures** (`tests/fixtures/options/`): `chain_bs(spot, iv, expiries, strikes,
skew=0)` — a Cboe-shaped chain priced by `black_scholes` with bid/ask = mid ± spread/2,
OI/volume from a seed; `bars_synth(kind)` — daily bars for uptrend+bounce, downtrend+
breakdown, range (two edges × 3 touches), flat-no-range, slow-grind; the LRCX and ISRG
fixtures as in B8 (chain + bars + iv_series + earnings).

| module | synthetic cases | live / integration |
|---|---|---|
| `opt_legs.norm_leg` | bridge row (iv 46.0, `oi`) and Cboe row (iv 0.46, `open_interest`) normalise to the same dict; 0.0/0.0 quote → `quote_ok False`; NaN → None; negative OI → None | one real Cboe chain (MSFT) and one bridge chain (saved payload from the old tab) agree on every leg's `mid`, `iv`, `oi` |
| `premium_gauge` | rank 62 → SELL + both sell gates; 24 → BUY; 41 with IV/HV 1.21 → SELL, 41 with 0.95 → NEUTRAL, 41 with 0.85 → BUY; n=19 → provisional (gates all False); n=40 → percentile basis, reason text; max==min series → rank None; term 1.11 with earnings 19d → the "event priced (earnings in 19d)" reason; term None → no term reason | the 7 pre-listed IV Rank tickers: server rank vs the bridge's `/iv` rank after the bootstrap agree within 1 point |
| `range_detector` | `mirror_bars` round-trips; `find_resistance_reject` on an inverted bounce series returns the mirrored level and a "pin" candle; `find_range` on the range fixture returns both edges with 3 touches, `width_atr` in bounds; a 1.5-ATR-wide range → None; price outside → None; a close 0.6 ATR through the upper edge in the last 3 bars → None; the uptrend fixture → None | DNOW / GILD / HSY (the consolidation charts already in the worktree, `consol_candidate_*.jpg`) print a range; NVDA at the 2026-09 highs does not |
| `chart_state` | trend up / down / sideways / unclear on the four fixtures; `trend_days` counts; `slow_drift` true on the grind fixture, false on the bounce fixture; `breakout_retest` from a flips-only support result within 20 bars; `failed_support` fires on the breakdown fixture only on the break day and the day after; plan stop/target reproduce ISRG 394.27 / 428.89 ± 0.3 from entry 405.81 and ATR 11.54; primary setup picks the trend-agreeing one | LRCX week of 2026-09-29 reads up + support_bounce 340 (the README v4.125 verification set: CVX, WFC, TGT bounces still detected through `chart_state`) |
| `strategy_rules` | all ten rows evaluated on each fixture: counts of fits/rejects as in B8 tables; rank 45 → bull_call 86 > bull_put 80.3; rank 62 → bull_put only; rank 24 → buy_call > bull_call, bull_put greyed "too cheap to sell"; `rejected_shown` ≤ 2 and only single-fail rows; earnings inside every expiry → the exact reason string; `defined_risk_only` flips bull_put to fit and buy_call stays rejected; a `step > current_step` fit is shown with the −10 and the "coming in step N" label; `why`/`must_happen` templates format with every ctx key (no KeyError on any fixture) | — |
| `option_prefs` | defaults fill every block; a stored `{"credit_vertical": {"short_delta_hi": "0.35"}}` reads as 0.35; `"abc"` → default; out-of-range `write` returns the error text and stores nothing; `prefs_hash` stable under key order; `risk_pct`/`nlv` come from `trade_prefs` and are absent from the stored JSON | — |
| `strike_picker` | bull_put on the LRCX chain → the B8.1 table (scores to 3 dp, 325/315 first); constraint removes 330P; `no_band` on a chain with deltas all < 0.15 shows the 2 nearest; `credit_floor` when `credit_pct_min` = 40; `thin` when every OI < 500; bear_call = the mirror on mirrored bars + a call chain; bull_call: the short is forced to 430 and carries the soft-band note, ×0.9; buy_call: theta cap excludes 14-DTE legs; leaps: 10% of SPOT cap passes 340C on ISRG and fails on LRCX (`extrinsic_cap`); condor on the range fixture: both shorts outside the edges, POP_both; calendar: only front×back pairs with front IV ≥ back IV, `POP_model` between the breakevens equals the lognormal mass (±1%); diagonal: the safety rule excludes the B8.3 ISRG pair by $2.47 and admits it when the long is 360C; every `words` string renders for every family | the old tab's `bull_put.select` and the new picker agree on the top pair for the same single-expiry bridge chain when prefs = `bull_put`'s constants (delta 0.20–0.25, DTE 45–60, offsets 1–2) |
| `payoff` / `option_sizing` | `value()` at `t=0`, `S=spot` equals `-net` within the bid/ask (round trip); at expiry equals intrinsic; LRCX 325/315 at 336.2: $99 (t=0) > $51 (t=24) → $99; ISRG 395/430 at 394.27: $230 (t=0) < $348 (t=38) → $348; contracts 10 and 2 as in B8; `by_notional` binds when risk_pct = 5 (floor(5000/99)=50 vs 14 → 14); ISRG LEAPS → 0 by both caps with the exact note; nlv None → contracts None + note; stop == entry → chart-stop size None, notional-only note; `fires_first` flips when `spread_loss_stop_pct` = 10 | the user's live NVDA 205/195P × 6 (README v4.62: mark 1.975 vs credit 1.85 = −$75) marks to the same −$75 through `option_exits.mark` |
| `order_ticket` | the B8.1 TWS text and moomoo text match the golden files; condition None on a fresh bounce, `≤ 341.92` when the close has run; three orders for a credit spread, two expiries listed per leg for a calendar; floor/ceiling arithmetic; every warning string present when OI is None / quotes delayed | copy-pasted into TWS's Strategy Builder on paper (bridge up) — legs resolve, condition accepted |
| `option_exits` | every row of the B7.3 table has a case that fires it and a case one tick short that returns WATCH or OK; losing-side wins over TAKE on the same day; chart stop before delta when both fire; `UNKNOWN` only when delta, mark AND the chart are all missing; `EXPIRED` for dte < 0; the migration copies an open `option_spreads` row and the new monitor's verdict on it equals `spread_monitor.snapshot`'s state for the same chain | the nightly `sweep` on the dev DB with the migrated NVDA spread + one paper trade per family; Telegram push on a dev chat (one message per card, chunking exercised by a 12-ticker basket) |
| end-to-end | `signal_for(symbol, prefs)` on the LRCX and ISRG fixtures returns the B8 cards byte-stable (golden JSON), with and without `defined_risk_only`; prefs with a widened band change only `picks` and `prefs_hash` | the full basket from the IV Rank "My list" (7 tickers) in under 2 s from the stored snapshot; a "Live" press re-sizes from `/account` without re-running the recommender |

Edge probes carried from `support_bounce`'s "Tested:" line (`README.md:335-339`) apply to
every chart function: 30-bar history, None volume, a still-open session at 10% and 50%, a
zero-range candle, non-numeric prices — all return None / the honest read, never raise.

