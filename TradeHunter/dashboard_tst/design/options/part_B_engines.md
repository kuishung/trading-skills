## B. Decision engines: premium gauge, strategy recommender, strike picker, sizing, order ticket, exits

Scope of this part: everything between "the data is in the DB" and "the card is on the
screen". Inputs are a stored chain (Part A's `option_chain_snapshot`, or a live chain from
the bridge posted to `POST /options/live/{symbol}` and wrapped by Part A's
`BridgePayloadSource`), the IV history (`iv_daily`), the daily bars the chart already
reads, and the member's rules. Outputs are plain dicts the nightly job writes into
`option_signal` and the page reads back through `option_store.card_for(db, symbol, user)` /
`basket_rows_for(db, user)` — the ONLY read path (Part A5.3). **No I/O in any engine
module**: every function here takes data in and returns a dict, exactly the discipline
`bull_put.py` already keeps (`app/services/bull_put.py:7-13`), so the whole decision path is
unit-testable on synthetic chains with TWS off and Cboe down.

Ownership (critique reconciliation): this part owns the engines, the prefs schema
(`option_prefs.py`), sizing, the order ticket, the exits and the `option_trades` /
`option_trade_checks` positions store. Part A owns storage, the nightly script and the
signal keys; Part C owns `trend_line.py`, `range_box.py`, `payoff.py` and the payoff SVG;
Part D owns `app/routes/options_page.py`, the templates, the rules drawer, `option_words.py`,
`telegram.py` / `telegram_push.py` and `job_runs.py`. Where this part names one of those, it
consumes it; it does not redefine it.

### B0. Placement, the chain contract, and the one normalisation layer

#### B0.1 Files

| File (all under `dashboard_tst/app/services/` unless stated) | Status | Role |
|---|---|---|
| `opt_constants.py` | new | B0.3 — the named constants every engine imports |
| `opt_legs.py` | new | leg normalisation by SOURCE unit (bridge payload vs Cboe / snapshot rows), chain views (`by_expiry`, `dte_of`, `nearest_strike`), `stored_leg()`, mid/width/liquidity helpers lifted from `bull_put._mid/_leg_spread/_abs_delta/_count/oi_needed` (`bull_put.py:129-162`) |
| `premium_gauge.py` | new | B1 — SELL / NEUTRAL / BUY / UNKNOWN, the four gates `{buy, sell_directional, sell_neutral, mid}`, the `iv` dict the signal stores |
| `chart_state.py` | new | B2 — one `ChartState` dict per ticker: calls `ema_setup.analyze()` ONCE and reads its `sup / tl / tl_bounce / rng` (Part C1.7 / C2.5), adds `structure.classify`, the bear-side mirrors from `mirror_setups`, earnings, the plan |
| `mirror_setups.py` | new | B2.4 — ONLY `find_resistance_reject` and `find_breakdown` (the bear-side setups `support_bounce` has no mirror for). The range and the sideways verdict are Part C's `range_box.py`; nothing range-shaped lives here |
| `strategy_rules.py` | new | B3 — `STRATEGY_KEYS` (the ten keys in the user's order, the catalog / tie-break order; `option_prefs` imports them from here), the ten `StrategyRule` rows, `recommend()` → the flat `strategies` list |
| `option_prefs.py` | new | B4.1 — the ONE rules schema (`SCHEMA` / `FIELDS`, blocks, house defaults, label / help / plain / step / unit per field), `read(db, user)`, `clean(raw)`, `write(db, user, tab, form)`, `reset(db, user, tab)`, `for_strategy`, `family_of`, `defined_risk(key)`, `prefs_hash(merged)`, `distinct_hashes(db)`. Part D's drawer renders it; nothing else defines a rules field |
| `strike_picker.py` | new | B4 — enumeration per family, chart constraints, scoring, POP, words, top-3 |
| `payoff.py` | **Part C's** (consumed) | the generic legs → P/L at expiry and at time *t* (`pnl`, `leg_value`, `curve_at`, `pop`, `price_at_pnl`, `build`, Part C3.2). Its `pnl / leg_value / curve_at` take `iv_bump: float = 0.0` for B5 — a RELATIVE lift, `sigma_used = leg.iv × (1 + iv_bump)`, defined once in `payoff.leg_value`; its `Leg.from_dict()` builds the signed quantity from the stored leg. `build(legs, *, strategy, spot, atr, as_of, chart_stop=None, target=None, levels=(), sigma_fallback=None, pl_now=None, premium_stop_pct=None, loss_fraction=0.20, units="$") -> dict`, per ONE contract, family via `option_prefs.family_of(strategy)`. One function, two consumers (sizing here, the chart in Part C) |
| `option_sizing.py` | new | B5 — `size(pick, nlv, prefs) -> {contracts, by_chart_stop, by_gap, by_notional, stop_t_days, stop_iv, max_loss_pct_nlv, nlv_source ∈ {live, prefs, None}, line, ...}`: contracts from risk % of NLV at the chart stop, capped by the gap rule and the 10 % notional cap; runs at READ time |
| `order_ticket.py` | new | B6 — `build(pick, setup, prefs, *, dip=False, rejection=None, now=None) -> Ticket` and `render(ticket, broker)` for TWS / moomoo; `ticket["rejection"]` is line 1 of both renderings |
| `option_exits.py` | new | B7 — `mark / grade / sweep`, the per-family monitor, extending `bull_put.monitor` |
| `option_engine.py` | new | the composer: `compute(chain, metrics, state, prefs) -> {status, headline, setup, iv, strategies, picks, computed_ms, engine_version}` runs B1 → B2 → B3 → B4 (+ B5.1's two stop losses) once per `(symbol, prefs_hash)` for the nightly job and a Refresh; `ENGINE_VERSION` is stamped on the row. B9's end-to-end test (`tests/test_option_engines.py`, step 1) drives it |
| `option_words.py` | **Part D's** (consumed) | `pop_words(pop, pop_kind)`, `headline(setup, iv, strategies)`, `rule_words` — this part supplies the strings (B3.2, B4.6, B4.7); Part D owns the module |
| `models.py` | modify | `UserOptionPrefs` (Part A's columns) with the one-to-one `User.option_prefs` relationship, `OptionTrade` (incl. the portable JSON `meta` column), `OptionTradeCheck` (B4.1, B7.1) |
| `alembic/versions/f4a5b6c7d8e9_options_module.py` | **one file, Part A's skeleton** | `down_revision = "e2f3a4b5c6d7"` (`alembic/versions/e2f3a4b5c6d7_iv_scan_items.py:13`, the verified head), per-table `get_table_names()` guard, creating NINE tables: `option_basket`, `option_chain_snapshot`, `iv_daily`, `option_signal`, `user_option_prefs`, `option_jobs`, `option_trades` (with its `meta` JSON column), `option_trade_checks`, `option_idea_push`, plus this part's data step (B7.1) copying OPEN `option_spreads` rows into `option_trades`. There is no second migration and no other revision id |
| `dashboard_tst/tests/`, `tests/fixtures/options/`, `dashboard_tst/requirements-dev.txt` | new (shared with A8 / C6 / D8.2) | the pytest tree and fixtures B9 uses (`tests/fixtures/options/lrcx.json`, `isrg.json` — the two golden fixtures; `tests/test_option_engines.py` in step 1); created in step 1 before the first engine lands |

`bull_put.py` is **not modified**: the old `/options/{symbol}` tab keeps grading against
its constants (`bull_put.py:40-71`) and its tests keep passing. The new engines import its
pure helpers and its management constants (`ROLL_DELTA`, `LOSS_STOP_FRACTION`,
`PROFIT_TARGET_FRACTION`, `DTE_FLOOR`, `ADJUST_DTE`, `DELTA_ADJUST`, `DELTA_CLOSE` at
`bull_put.py:615-624, 69-71`) rather than restating them. `spread_monitor.py` is **not
changed** either: it keeps sweeping the legacy `option_spreads` rows for the old
`/portfolio` until that page is removed; every new position — bear calls included — is
graded by `option_exits` over `option_trades` (B7).

#### B0.2 The chain contract the engines consume

Two shapes exist today and they disagree on units and key names:

| | Cboe (`option_quotes.fetch_chain`, `option_quotes.py:103-181`) | Bridge (`bridge/ibkr_bridge.py:_row`, `ibkr_bridge.py:321-345`) |
|---|---|---|
| container | `chain["legs"][(expiry, right, strike)]` | `chain["puts"]` / `chain["calls"]` lists per ONE expiry (`chain["expiry_label"]`, `chain["dte"]`) |
| `iv` | **fraction** (0.46) — `SpreadCandidate.short_iv` comment "fraction as the chain gives it" (`models.py:1013`) | **percent** (46.0) — `round(g.impliedVol * 100, 1)` (`ibkr_bridge.py:330`) |
| open interest | `open_interest` | `oi` |
| theta | per day, per share | per day, per share (`g.theta`) |
| missing quote | `bid`/`ask` None, or 0.0 ("Cboe sends 0.0 for no quote", `option_quotes.py:93-100`) | None |

**Units are decided by the SOURCE, never by magnitude.** Part A's `ContractRow.iv` is
already a fraction for every source it wraps, and `BridgePayloadSource` (A1.5) is the ONLY
place that divides by 100. There is no "a value over 3 must be a percent" heuristic
anywhere: a deep-in-the-money Cboe contract legitimately prints `iv` 3.1099 as a
*fraction* (the MSFT file of the 2026-10-02 session ranges 0.1375..8.3157 and A1.1's sanity
filter keeps IV up to 5.0), so a magnitude rule would silently corrupt real rows.
`opt_legs.norm_leg(row, *, unit, expiry=None, right=None) -> dict` therefore takes the unit
explicitly — `unit="fraction"` for Cboe / Alpaca / snapshot rows and for anything that
came through `BridgePayloadSource`; `unit="percent"` only for the legacy raw bridge payload
the old Watchlist tab still posts (`routes/options.analyze`) — and `normalise_iv(v, unit=)`
in Part C's `payoff.py` takes the same argument from the chain's `source`. It produces ONE
shape and every engine reads only that:

```python
{
  "expiry": "2026-11-20", "right": "P", "strike": 320.0,
  "side": None, "qty": None,     # filled by the enumerators: "sell" | "buy", qty a POSITIVE int
  "price": 7.50,                 # the mid, (bid + ask) / 2 — None without a two-sided quote (the golden LRCX fixture's 320P, the long leg of B8.1's pick)
  "bid": 7.35, "ask": 7.65,
  "iv": 0.46,                    # ALWAYS a fraction — set by `unit`, never guessed
  "delta": -0.172,               # signed, as the feed gives it; abs() at the use site
  "oi": 1840, "volume": 212,     # int or None (= "not reported")
  # engine-only extras — stripped by opt_legs.stored_leg() before a leg is written anywhere:
  "last": None, "gamma": 0.011, "theta": -0.186, "vega": 0.412,   # per share per day / per vol point
  "spread": 0.30,                # ask - bid, None when either side is missing
  "quote_ok": True,              # bid and ask both present and > 0
}
```

The first eleven keys are the **stored / API leg** every part shares (contract LEG):
`{expiry, right, strike, side, qty, price, bid, ask, iv, delta, oi, volume}`. The key is
`oi`, never `open_interest`, once past `norm_leg` (`oi = row.get("oi",
row.get("open_interest"))` through `bull_put._count`, `bull_put.py:148-156`). Part C's
`payoff.Leg.from_dict(leg)` derives the signed quantity (`+qty` for `buy`, `-qty` for
`sell`); `OptionTrade.legs` adds `entry_price, entry_delta, entry_iv` (B7.1);
`OptionTradeCheck.legs` holds the per-day `{mid, delta, iv}` (B7.1). A 0.0 bid with a
0.0 ask → `quote_ok False`, `price None`.

`opt_legs.chain_view(chain) -> {"spot", "iv30", "as_of", "source",
"by_expiry": {expiry: {"P": [legs sorted by strike], "C": [...]}, "dte": {expiry: int}}}`
accepts either raw shape (a bridge chain has one expiry; Part A's snapshot rows are the
Cboe shape re-read from the DB) and passes the right `unit` to `norm_leg` from the chain's
`source`.

`dte_of(expiry, today)` = `(date.fromisoformat(expiry) - today).days`, the same arithmetic
as `spread_monitor._dte` (`spread_monitor.py:35-39`), with `today` = `spread_monitor.et_today()`
(`spread_monitor.py:204-212`) so a Malaysian evening does not count one day too few.

#### B0.3 Named constants (one module, `opt_constants.py`, imported everywhere)

| Constant | Value | Why |
|---|---|---|
| `RISK_FREE` | 0.04 | the rate `bull_put.bs_put` already prices the 15-DTE curve with (`bull_put.py:79`) and Part C's `payoff.RISK_FREE`; one rate for every model number on the page |
| `STOP_IV_BUMP` | 0.10 | relative IV lift applied when valuing a position at the chart STOP: a drop to the stop comes with higher IV (spot-vol correlation). 10% is the lower end of what a 1-ATR down day does to a liquid name's IV30; it makes credit-spread stop losses honest and never flatters. Passed as `iv_bump=` to Part C's `payoff.pnl` |
| `STOP_TIMES` | (0, 0.5) | the loss at the stop is evaluated at *t* = now and at half the DTE and the LARGER is used (B5) — one rule that picks the worst regime for sellers (now) and for buyers (later) |
| `SLOW_DRIFT_ATR` | 0.75 | max absolute change of EMA20 over 10 sessions, in ATRs: a "slow grind" (calendar / diagonal) |
| `LEVEL_PAD_ATR` | 0.25 | a strike that must sit "under support" sits at least this far under the zone's low edge, and the credit chart stop sits this far under it: the same 0.25 ATR `support_bounce.REACH_ATR` lets a bounce stop short of a level (`support_bounce.py:110`). The single pad constant — the mockup's 338 for LRCX was a 0.1-ATR pad and is replaced by the engine's 336.2 everywhere |
| `TARGET_R` | 2.0 | the chart target for debit trades = entry + 2R (the Curated / trade-tool convention: "stop 1×ATR(14) away, target at 2R", `_price_chart.html:1283`). A constant, not a member field |
| `STOP_ATR` | 1.0 | the chart stop for debit trades = entry − 1 ATR (same convention). A constant, not a member field |
| `MAX_POSITION_PCT` | 10.0 | notional cap: contracts × max loss ≤ 10% of NLV (CLAUDE.md strict risk rule `max_position_pct = 10% of NLV`, "global, never override") — the third cap in B5, a constant and never a member field |
| `IV_MIN_OBS` | 20 | `spread_scan.IV_MIN_OBS` (`spread_scan.py:69`): below this many IV observations no percentile is shown |
| `IV_RANK_MIN_OBS` | 60 | observations before the RANK (min/max based) is trusted over the percentile — ~3 months, the design's "rank after ~3 months, percentile earlier" |
| `IV_FULL_OBS` | 252 | a full year of readings: the rank is "over the last year"; below it the day count is said out loud (B1.5) |

The sideways verdict and the range box have no constants here: they are Part C's
`range_box.py` (C2.3) and `chart_state` only reads its result (B2.2). `GAP_MULT` is a
member field (`shared.gap_mult`, house 2.0, B4.1), not a constant.

### B1. The premium gauge

`premium_gauge.gauge(*, iv30, iv_series, hv20, hv60, iv_front, iv_back, front_dte,
back_dte, skew25=None, skew_norm=None, expected_move=None, earnings_date=None,
earnings_days=None) -> dict`. Pure. All IV / HV inputs are **PERCENT** — the unit
`iv_daily` stores its per-day statistics in (`iv30`, `hv20`, `hv60`, `iv_front`, `iv_back`;
contract IV UNITS; `iv_history.iv30` is percent too, `models.py:948`). The gauge only forms
ratios and comparisons, so it never converts; the per-contract `iv` on a leg (a fraction,
B0.2) never enters it. HV20 / HV60 are computed by Part A from the stored daily closes as
`std(log returns, last N) × sqrt(252) × 100` (A3.1). The dict it returns IS the
`option_signal.iv` dict (B1.4) — the writer stores it as-is.

#### B1.1 Inputs

| Input | Source | Note |
|---|---|---|
| `iv30` | `iv_daily.iv30` for today (Cboe `data.iv30`, `option_quotes.py:174`, or the bridge `/iv` `iv_current`, already percent at `ibkr_bridge.py:559`) | today's reading |
| `iv_series` | the last 252 `iv_daily.iv30` rows with `on <= today`, today included (A3.3's window; the formula is `spread_scan.iv_percentile`'s, `spread_scan.py:222-236`) ∪ the IBKR bootstrap rows (decision 4; bridge 1.6 sends the series in PERCENT and `option_store.bootstrap_iv` stores it as-is, bounded 0.1..1000, never overwriting a day the server read itself) | oldest first |
| `hv20`, `hv60` | `iv_daily.hv20 / hv60` | realised vol of the stock, 20 and 60 sessions |
| `iv_front`, `iv_back` | `iv_daily.iv_front / iv_back` (A3.4): the ATM IV of the FRONT expiry = the listed expiry nearest 30 DTE with `dte >= 7`, and of the BACK expiry = the listed expiry nearest 75 DTE with `dte >= 45`; ATM = the strike nearest spot, IV = mean of that strike's put and call `iv` × 100 | the gauge picks nothing itself; Part A stores the two numbers per day |
| `skew25`, `skew_norm`, `expected_move` | `iv_daily` (A3.5, A3.6) | passed through into the `iv` dict unchanged |
| `earnings_date`, `earnings_days` | `prices.fetch_next_earnings(sym)` (`prices.py:178-191`, A3.7) | explains a front-month bump; stored as `iv.earnings_date / iv.earnings_days` |

#### B1.2 Formulas

```
iv_rank       = (iv30 - min(series)) / (max(series) - min(series)) * 100   # None if max == min or n < IV_RANK_MIN_OBS
iv_pct        = spread_scan.percentile(series, iv30)                        # share of past days BELOW today, None if n < IV_MIN_OBS
iv_hv_premium = iv30 / hv20                                                 # ratio; > 1 = options priced richer than the stock has moved
term_ratio    = iv_front / iv_back                                          # > 1 = front dearer than back (an event is priced / backwardation)
```

`iv_rank` is the same formula the bridge computes from IBKR's series
(`ibkr_bridge.py:559-561`), so the server-side rank and the "Live" rank agree by
construction once the bootstrap has filled `iv_daily`. `term_ratio` is the ONE
term-structure number on the platform (the earlier "slope" name and Part A's earlier
difference-over-back form are gone from every part); `iv_daily` stores `iv_front`, `iv_back` and
`term_ratio`, the signal's `iv` dict carries the same three.

#### B1.3 Gates and thresholds (named constants in `premium_gauge.py`)

| Constant | Value | Meaning |
|---|---|---|
| `BUY_MAX_RANK` | 30 | buy premium when the rank is at or under this (design §5.2: "ideally ≤ 30") |
| `SELL_DIR_MIN_RANK` | 30 | sell directional premium (credit spreads) from here up (§5.2: "IV rank ≥ 30") |
| `SELL_NEUTRAL_MIN_RANK` | 50 | sell neutral premium (condor) from here up (§5.2: "IV rank ≥ 50") |
| `MID_LO`, `MID_HI` | 30, 50 | the band where a debit vertical beats a naked long (bull call: "IV mid (30–50)") |
| `IV_HV_RICH` | 1.10 | IV at least 10% above realised: sellers are paid for more movement than the stock delivers |
| `IV_HV_CHEAP` | 0.90 | IV at least 10% under realised: buyers get movement the market is not charging for |
| `TERM_EVENT` | 1.05 | `term_ratio` at or above: front ≥ 5% dearer than back, an event is priced in the front month |
| `TERM_CONTANGO` | 0.95 | `term_ratio` at or below: front ≤ 95% of back, the calendar's natural shape |

The four gates are booleans on the result, evaluated on **whichever of rank / percentile
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

`state` partitions the history by its length `n = iv_n` (the partition Part A's
`iv_rank_pct` writes; the gauge recomputes it from `iv_n` so a bootstrap that just landed
is reflected on the next read): `none` (no series at all), `forming` (0 < n < `IV_MIN_OBS`),
`pct_only` (`IV_MIN_OBS` ≤ n < `IV_RANK_MIN_OBS`), `rank_ok` (`IV_RANK_MIN_OBS` ≤ n <
`IV_FULL_OBS`: a rank, over n days), `ok` (n ≥ `IV_FULL_OBS`: a full year). `basis` says
what the verdict rests on: `rank` | `percentile` | `provisional` (IV vs HV alone) |
`unknown`.

```python
def gauge(...):
    why = []
    n = len(iv_series)
    state = "none" if n == 0 else "forming" if n < IV_MIN_OBS else "pct_only" if n < IV_RANK_MIN_OBS else "rank_ok" if n < IV_FULL_OBS else "ok"
    rank = iv_rank(...) if n >= IV_RANK_MIN_OBS else None
    pct  = spread_scan.percentile(iv_series, iv30)        # None under IV_MIN_OBS
    if rank is not None:   measure, basis = rank, "rank"
    elif pct is not None:  measure, basis = pct, "percentile"
    else:                  measure, basis = None, None
    prem = iv30 / hv20 if (iv30 and hv20) else None      # the unitless RATIO iv30 / hv20 (never vol points)
    term = iv_front / iv_back if (iv_front and iv_back) else None

    if measure is None:
        # no usable history: fall back to IV vs HV alone, flagged provisional; gates stay CLOSED
        if prem is None or iv30 is None:
            verdict, basis = "UNKNOWN", "unknown"
            why.append(f"We cannot yet say whether options are expensive - {n} of {IV_RANK_MIN_OBS} days of history. If you have TWS on this PC, press Live to load a year.")
        else:
            basis = "provisional"
            if prem >= IV_HV_RICH:    verdict = "SELL";    why.append(f"IV {iv30:.0f}% is priced for {(prem-1)*100:.0f}% more movement than the stock has actually shown ({hv20:.0f}%) - provisional, {n} of {IV_RANK_MIN_OBS} days of IV history")
            elif prem <= IV_HV_CHEAP: verdict = "BUY";     why.append(f"IV {iv30:.0f}% is priced for {(1-prem)*100:.0f}% less movement than the stock has shown - provisional, ...")
            else:                     verdict = "NEUTRAL"; why.append(f"IV {iv30:.0f}% is close to the stock's own movement - provisional, ...")
        provisional = True; gates = {k: False for k in gates}
    else:
        if measure >= SELL_NEUTRAL_MIN_RANK:   verdict = "SELL"
        elif measure >= SELL_DIR_MIN_RANK:     verdict = "NEUTRAL"      # 30-50: either side, rank decides in B3
        else:                                  verdict = "BUY"
        word = {"SELL": "expensive", "BUY": "cheap", "NEUTRAL": "middling"}[verdict]
        band = f">= {SELL_NEUTRAL_MIN_RANK}" if verdict == "SELL" else f"<= {BUY_MAX_RANK}" if verdict == "BUY" else f"{SELL_DIR_MIN_RANK}-{SELL_NEUTRAL_MIN_RANK}"
        # the lead clause: a full-year rank names its threshold and nothing else; anything shorter says the day count
        lead = (f"IV rank {measure:.0f} ({band})" + ("" if state == "ok" else f" over {n} days")) if basis == "rank" \
               else f"Options look {word} against the last {n} days (not a full year yet)"
        # IV vs HV only MOVES the verdict inside the 30-50 band; outside it, it is a reason — ONE sentence either way
        if verdict == "NEUTRAL" and prem is not None and (prem >= IV_HV_RICH or prem <= IV_HV_CHEAP):
            verdict = "SELL" if prem >= IV_HV_RICH else "BUY"
            why.append(lead + (f" and priced for {(prem-1)*100:.0f}% more movement than the stock has actually shown - sellers are paid" if verdict == "SELL"
                               else f" and priced for {(1-prem)*100:.0f}% less movement than the stock has shown - buyers are not overcharged"))
        elif prem is not None:
            why.append(lead + f" and priced for {abs(prem-1)*100:.0f}% {'more' if prem > 1 else 'less'} movement than the stock has actually shown")
        else:
            why.append(lead + (f": options look {word} by this stock's own standards" if basis == "rank" else ""))
        provisional = False
    # the term reading is NOT a verdict_why clause: Part D's term chip renders term_words() from term_ratio + earnings_days (below)
    return {"iv30": iv30, "hv20": hv20, "hv60": hv60, "iv_hv_premium": prem,
            "iv_rank": rank, "iv_pct": pct, "iv_n": n, "state": state, "basis": basis, "provisional": provisional,
            "iv_front": iv_front, "iv_back": iv_back, "term_ratio": term,
            "skew25": skew25, "skew_norm": skew_norm, "expected_move": expected_move,
            "earnings_date": earnings_date, "earnings_days": earnings_days,
            "verdict": verdict, "verdict_why": "; ".join(why), "gates": gates}
```

This dict is `option_signal.iv` verbatim: `{iv30, hv20, hv60, iv_hv_premium, iv_rank,
iv_pct, iv_n, state ∈ {none, forming, pct_only, rank_ok, ok}, basis ∈ {rank, percentile,
provisional, unknown}, provisional, iv_front, iv_back, term_ratio, skew25, skew_norm,
expected_move, earnings_date, earnings_days, verdict ∈ {SELL, NEUTRAL, BUY, UNKNOWN},
verdict_why, gates}`. `verdict_why` is one string (the reasons joined by "; "; the card
splits it for the hover list). On the golden LRCX fixture (rank 62, `state ok`, IV 46 / HV 38)
it is exactly **"IV rank 62 (>= 50) and priced for 21% more movement than the stock has
actually shown"** — the one string every part quotes for that fixture.

Two pure helpers sit beside `gauge()` for the words the dict does not carry:
`premium_gauge.span_words(iv) -> str` = "over the last year" (`state ok`) / "over {n} days"
(`rank_ok`) / "against the last {n} days (not a full year yet)" (`pct_only`) — the headline's
`{span}` (B3.2); and `premium_gauge.term_words(term_ratio, earnings_days, front_dte=None) ->
str | None` = "front month {t:.2f}x the back - an event is priced" (+ " (earnings in {d}d)"
when `earnings_days` is known and, when `front_dte` is given, falls inside it) for
`term_ratio >= TERM_EVENT`, "front month cheaper than the back - calendar shape" at or under
`TERM_CONTANGO`, else None — the text of Part D's term chip, rendered from the dict's
`term_ratio` and `earnings_days`, never a `verdict_why` clause.

Why IV−HV only moves the verdict inside the band: a rank of 70 with IV at 0.95× HV is
still expensive *against its own year*, which is the question a seller asks; the ratio
is shown as a reason so the member sees that the stock has actually been moving as much
as the options say. Inside 30–50 the rank cannot decide, so the ratio does. Why the day
count is always said: a rank over 34 days is a different claim from one over a year, and
the honesty rule of the brief is that no number is dressed as firmer than it is.

#### B1.5 Output and failure modes

| Situation | `state` / `basis` | `verdict` | `gates` | UI (basket IV cell / card line — Part D renders from `iv`) |
|---|---|---|---|---|
| ≥ 252 obs, rank 62 | ok / rank | SELL | sell_* True | amber "62 · sell premium"; gauge "IV rank 62 (>= 50) and priced for 21% more movement than the stock has actually shown" (the golden fixture's `verdict_why`); term chip "front month 1.11x the back - an event is priced (earnings in 19d)" from `term_words` |
| 60–251 obs, rank 62 | rank_ok / rank | SELL | sell_* True | "62" with the footnote "over 118 days"; the gauge sentence carries the count: "IV rank 62 (>= 50) over 118 days and priced for ..." |
| 20–59 obs, pct 71 | pct_only / percentile | SELL | from pct | the cell shows **"~71" with a dotted underline, never amber**; gauge: "Options look expensive against the last 34 days (not a full year yet) and priced for ..."; hover: "rank in 26 days" |
| < 20 obs, HV present | forming / provisional | SELL / BUY / NEUTRAL, `provisional True` | all False | grey "~" cell; gauge: "IV 46% is priced for 21% more movement than the stock has actually shown — provisional, 12 of 60 days of IV history"; no sell strategy is recommended (B3 treats closed gates as fail); the hint "If you have TWS on this PC, press Live to load a year" (Live is hidden on phones with "Live quotes need TWS on your PC") |
| no series and no HV, or no `iv30` (chain missing) | none / unknown | UNKNOWN | all False | cell "–"; gauge: "We cannot yet say whether options are expensive - 12 of 60 days of history. If you have TWS on this PC, press Live to load a year." plus the stale badge from Part A when the chain is missing |
| `hv20` None (fewer than 21 closes) | by rank | by rank | by rank | the IV−HV chip is omitted, not invented |
| `iv_back` None (no listed expiry with `dte >= 45` — the back month is the expiry nearest 75 DTE, A3.4) | by rank | by rank | by rank | term chip omitted (`term_words` returns None); calendar / diagonal rules see `term_ratio=None` → "needs a back month" |

### B2. The chart-state contract

`chart_state.read(symbol, *, bars, long_bars, today, expiries) -> ChartState` (the one
spelling every part uses — keyword-only, `expiries=` never `at=`; the ATR is computed inside
from `bars`; `expiries` is every expiry listed in the snapshot, `()` when there is none)
builds one dict per ticker. In the nightly job the bars come from Part A's
cached daily history (the same `/prices`-shaped dicts `services.prices.fetch_daily_ohlc`
returns, `prices.py:50`); on the page the same function runs on the live bars — so the
nightly card and a "Refresh" never disagree about what a candle is.

**One detector run.** `read()` calls `ema_setup.analyze(bars, long_bars, at=expiries)`
ONCE and reads `sup` (the support-bounce result), `tl` (the trend line, Part C1.1),
`tl_bounce` (the candle at the line, C1.1 `bounce`) and `rng` (the range box, C2.1) from
its return dict — Part C1.7 / C2.5 make `analyze()` store all four. `chart_state` never
re-runs `support_bounce.find`, `trend_line.find` or `range_box.find` itself; the only
detectors it calls directly are the two bear-side mirrors in `mirror_setups` (B2.4),
which `analyze()` does not compute. `expiries` (every expiry listed in the snapshot) is
forwarded as the `at=` argument so the single `trend_line.find(bars, direction,
at=expiries)` run answers `value_at` for every listed expiry (C1.1; for a line already
found, `trend_line.value_on(tl, times, x)` is the same arithmetic without a second
search). Release note (C1.7 / C2.6): `t1` (trend-line bounce) and `r1` (range) join
`COND_KEYS` in `ema_setup`, so every `sym_conds` reader sees two more switches, and
`range_box`'s 5–10 ms is budgeted inside `setups_for_many` on the Sector / IV Rank list
requests (its "not on a request path" holds only after the 15-min cache warms).

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
  "sup": {...} | None,                   # analyze()["sup"]: support_bounce.find's dict verbatim
  "tl": {...} | None,                    # analyze()["tl"]: trend_line.find's dict verbatim (B2.5)
  "tl_bounce": {...} | None,             # analyze()["tl_bounce"]: trend_line.bounce's dict verbatim
  "rng": {...} | None,                   # analyze()["rng"]: range_box.find's dict verbatim (B2.4)
  "setup": {...} | None,                 # the PRIMARY setup (B2.3), direction-bearing
  "setups": [ {...}, ... ],              # every setup found, primary first
  "levels": {"support": 340.0 | None, "resistance": 371.5 | None, "target_up": ..., "target_dn": ...},
  "earnings": {"date": "2026-10-22", "days": 19} | None,                         # prices.fetch_next_earnings; stored on the signal as iv.earnings_date / iv.earnings_days
  "plan": {"entry": 349.20, "stop": 336.2, "target": 375.2, "r": 13.0} | None,   # B2.6 — exactly these four keys; the direction is the setup's
  "evidence": ["EMA 20 > 50 > 200 for 34 sessions", "bounced off 340 on 1.8x volume, 3 touches", ...],
}
```

#### B2.2 Trend

| `trend` | Rule | Source |
|---|---|---|
| `sideways` | `rng is not None and rng["sideways"]` — checked FIRST; `chart_state.trend == "sideways"` **iff** `rng.sideways` | Part C2.4: `stack_flat` (EMA20 / EMA50 within `STACK_TOL_ATR`, EMA20 moved ≤ `SLOPE_TOL_ATR` over `SLOPE_BARS`) AND ≥ `INSIDE_BARS` closes inside a range whose two edges each have ≥ `MIN_TOUCHES` touches — the design's "EMA stack flat within x ATR, a range whose two edges each have ≥ 2 touches", computed once in `range_box.find` |
| `up` | `e20 > e50 > e200` on the last close | `ema_setup.analyze(...)["uptrend"]` (`ema_setup.py:278`) |
| `down` | `e20 < e50 < e200` | mirror, computed in `chart_state` from the same EMA series (`ema_setup.ema`, `ema_setup.py:160-168`) |
| `unclear` | anything else (a mixed stack, or flat EMAs with no range) | — |

`trend_days` counts back from today while the stack ordering is unchanged (an uptrend
that is 3 days old is a different thing from one 90 days old; the headline sentence
says which). `slow_drift` is computed for every trend; it is only read by the time-spread
rules.

#### B2.3 Setup kinds

Each setup dict has the common fields `{kind, direction: "up"|"down"|"neutral", level,
zone: [lo, hi], touches: int, vol_high, candle: {time, kind, low|high}, quality: 0..100,
summary}` plus kind-specific extras. Detection order and sources:

| `kind` | Direction | Detector | Level / extras | Quality (0–100) |
|---|---|---|---|---|
| `support_bounce` | up | `analyze()["sup"]` = `support_bounce.find(bars, d_emas, w_emas)` (`support_bounce.py:374-482`) — the shipped v4.124/125 detector, unchanged | `level`, `zone`, `touches` (= `n_touches`; `n_low`, `n_flip` kept as extras), `vol_high`, `d_ema`, `w_ema`, `bounce` | 50 + 10 per touch past the first (cap 80) + 10 if `vol_high` + 5 `d_ema` + 5 `w_ema`; a `vol_high` of False or None caps quality at 45 (the "near miss" of `ema_setup.rank`, `ema_setup.py:532-539`) |
| `resistance_reject` | down | `mirror_setups.find_resistance_reject(bars, ...)` = `support_bounce.find` run on **mirrored bars** (B2.4) | the same fields, prices un-mirrored; `candle.kind` = "pin" (shooting star) / "engulf" (bearish engulfing) | same scale |
| `trendline_bounce` | up (down) | `analyze()["tl"]` + `analyze()["tl_bounce"]` (Part C1.1 `find` / `bounce`): a pin bar / engulfing whose low (high) is within `support_bounce.TOL_ATR` (0.35 ATR) of the line's value on that bar | `line` = `tl` verbatim (B2.5), `level` = `tl["value_today"]`, `touches` = `tl["n_touches"]` | 40 + 10 per touch past the second (cap 70) + 10 if `tl_bounce["vol_high"]` |
| `ema_rebound` | up | `ema_setup.analyze`: `rebound` in ("EMA20","EMA50") with `fresh` or `held` (`ema_setup.py:283-297`), or `pin_d` at an EMA (`ema_setup.py:340`) | `level` = that EMA's value today, `ema` = "EMA20"/"EMA50", `fresh`, `held`, `pin` | fresh 45 / held 30 / pin only 35 (+10 if a pin bar AND a rebound) |
| `breakout_retest` | up | the `sup` result with `n_low == 0 and n_flip >= 1` (a level made ONLY of old resistance highs — "a first retest, not a defended support", `support_bounce.py:65-68`) AND the breakout close above the zone happened within the last 20 sessions | same as support_bounce + `broke_on` (the first close > zone hi + tol) | 35 + 10 if `vol_high`; never above 50 (one retest is thin evidence) |
| `failed_support` | down | `mirror_setups.find_breakdown(bars)`: a level with ≥ 2 "low" touches (the support detector's level search WITHOUT the bounce-candle gate, over `LEVEL_LOOKBACK`), the latest close `< level - support_bounce.BREAK_ATR x ATR` (`support_bounce.py:112`), the previous close `>= level - BREAK_ATR x ATR` (it broke TODAY or yesterday, `BREAK_RECENT = 2` sessions) and `volume_read` says high (`support_bounce.py:319-371`) | `level`, `zone`, `touches`, `broke_on`, `vol_high` | 40 + 10 per touch past the second (cap 60) + 10 if `vol_high` |
| `range` | neutral | `analyze()["rng"]` = `range_box.find(bars, emas=...)` (Part C2.1) | `rng` verbatim; `level` = `rng["low"]`, `zone` = `rng["zone_low"]`, `touches` = `min(rng["n_low"], rng["n_high"])`; the upper edge is read from `rng["high"]` / `rng["zone_high"]` | 40 + 5 per touch past 2 on each edge (cap 70) |

**Primary setup** = the highest-quality setup whose direction agrees with the trend
(`up` setups in an uptrend, `down` in a downtrend, `range` when sideways). When nothing
agrees (an uptrend with only a `resistance_reject`), `setup` is None and `setups` still
lists what was found — the recommender then rejects every setup-dependent strategy with
"no setup on the chart today" (`reason_key = no_setup`) and the card says so.

#### B2.4 The bear-side mirrors — `mirror_setups.py`; the range is Part C's `range_box.py`

The design (§5.4) asks for the support-bounce detector's mirror. Rather than re-deriving
a second set of pivot / touch / invalidation rules for highs, the module **mirrors the
bars and reuses `support_bounce`'s internals unchanged**, so both edges obey exactly the
same ATR-relative rules and any future fix to `support_bounce` fixes both.

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

The breakdown needs the LEVEL search without the "latest candle bounced" gate.
`support_bounce.find` fuses the two; `find_breakdown` therefore calls the internals
directly (they are module functions, `support_bounce.py:169-280`):

```python
LEVEL_LOOKBACK = 120      # sessions a defended level may come from (six months; a year-old level is history)
MIN_TOUCHES = 2           # "low" touches a level needs before its break means anything
BREAK_RECENT = 2          # the break must be today's or yesterday's close

def _levels(bars, lo_i, end) -> list[dict]:
    """Every defended level in [lo_i, end]: seeds from _swings, members from _members,
    distinct still-valid touches from _touches — support_bounce.find's loop (support_bounce.py:434-463)
    minus the bounce test and the 'established / approached' gates. Returns
    [{level, zone, touches, n_low, n_flip}] with n_low >= MIN_TOUCHES."""

def find_breakdown(bars) -> dict | None:
    """The most-touched level in the last LEVEL_LOOKBACK sessions whose break is fresh
    (B2.3's failed_support row). None when no level has MIN_TOUCHES, when the latest
    close is still above level - BREAK_ATR x ATR, or when the break is older than
    BREAK_RECENT sessions (a stale break is a downtrend, not a setup)."""
```

**The range and the sideways verdict are NOT here.** Part C's `range_box.find(bars,
emas=)` (C2.1) is the one range detector — prototyped, negation-based through
`support_bounce._swings/_members/_touches`, stored by `ema_setup.analyze()` as `rng` —
and this part only *reads* its dict:

```
rng = {low, high, zone_low: [lo, hi], zone_high: [lo, hi], atr, width_atr,
       touches_low: [{time, price, kind}], touches_high: [...], n_low, n_high,
       since, age_bars, last_touch, pos_pct (0 = on the low, 1 = on the high), inside_bars,
       stack_flat: bool|None, ema200_inside: bool|None, sideways: bool|None, reasons: [str]}
```

Consumers in this part: `trend == "sideways"` iff `rng["sideways"]` (B2.2); the
iron-condor constraint uses `rng["zone_low"][0]` / `rng["zone_high"][1]` (B4.3); the
calendar's "sit" test uses `rng["pos_pct"]` (C2.5: 0.35–0.65); the condor's range stop
uses `rng["low"]` / `rng["high"]` (B7.3). Both edges therefore carry `support_bounce`'s
full definition of a touch — arrived from ≥ 1 ATR away, left by ≥ 1 ATR, distinct events
≥ `MIN_SEP` bars apart, invalidated by `BREAK_BARS` closes beyond the level and
re-validated by a reclaim — and every number is an ATR multiple (CLAUDE.md).

#### B2.5 Trend line (consumed, not computed here)

The trend-line engine is Part C (`services/trend_line.py`, design §6d). This part reads
its dict verbatim from `analyze()["tl"]`:

```python
"tl": {"direction": "up",
       "p1": {"time": "2026-06-12", "price": 318.4}, "p2": {"time": "2026-08-21", "price": 331.0}, "i1": 61,
       "slope_per_bar": 0.42, "slope_atr": 0.036,
       "touches": [{"time": "2026-06-12", "price": 318.4}, {"time": ..., "price": ...}, ...],   # oldest first, p1 and p2 included
       "n_touches": 3, "span_bars": 79,
       "value_today": 341.9,
       "value_at": {"2026-11-20": 362.1, "2026-12-19": 374.3, ...},   # every listed expiry in the snapshot (the nightly job passes them as at=)
       "broken": False, "last_break": None,                            # a close > BREAK_ATR through it since the first touch
       "warning": False,                                               # latest close through the line, not yet by BREAK_ATR
       "residual_atr": 0.09, "atr": 11.54, "channel": {...} | None}
```

`value_at[expiry]` is what B4's chart constraint reads ("short strike under the trend
line's value at expiry"). `touches` is a LIST and `n_touches` the count; the slope is
`slope_per_bar` (there is no per-day slope field). The chart draws it as Part C4.4's
read-only line series — `chart_trendline = trend_line.overlay(setup.tl, setup.tl_bounce)`,
read from the STORED setup that `card_for()` returns (B2.7), the same way
`range_box.overlay(setup.rng)` and the bounce marker from `setup.sup` are drawn; `GET
/options/chart/{symbol}` never calls `ema_setup.setup_for` or any detector on a request —
with the touch markers merged into `candleMarks` and weekly snapping; nothing in this part draws.
If the engine is not yet built (step 2 of the phasing), the key is None and every rule
degrades to the horizontal level alone — stated on the card as "trend line: not yet".

#### B2.6 The plan (stop / target the chart implies)

Every directional setup produces a stock-level plan, the same convention the chart's trade
tool seeds (`_price_chart.html:1283`: "stop 1×ATR(14) away, target at 2R"):

| Direction | entry | stop | target |
|---|---|---|---|
| up | the close (the nightly card) or `level x (1 + trade_prefs.offset_pct/100)` when the member has the Curated offset habit (`trade_prefs.read(user)["offset_pct"]`, default 0.3, `trade_prefs.py:30`) | `min(entry - STOP_ATR x ATR, setup.zone[0] - LEVEL_PAD_ATR x ATR)` — 1 ATR under the entry, and in any case under the level that must hold | `entry + TARGET_R x (entry - stop)`, capped at `levels.resistance` when a resistance sits between 1.5R and 2R (a bull call spread's short strike then sits there) |
| down | mirror | `max(entry + STOP_ATR x ATR, setup.zone[1] + LEVEL_PAD_ATR x ATR)` | `entry - 2R`, floored at `levels.support` |
| neutral (range) | — | the range edges ± `LEVEL_PAD_ATR x ATR` (`rng.low - pad`, `rng.high + pad`: a close beyond either is the stop for a condor / calendar) | — |

For a credit spread the **chart stop** is the stock price at which the thesis is wrong
(the level failed), not 1 ATR from entry: `stop = setup.zone[0] - LEVEL_PAD_ATR x ATR` for
a bull put (`336.2` for LRCX: zone low 339.1 − 2.9), the mirror for a bear call. **`plan`
is the single source of the chart stop on the platform**: the stored `setup.plan.stop`
is what the picker's `chart_stop` (B4.8), the sizing (B5), the ticket (B6), the payoff
chart's marker (Part C3.9) and the monitor (B7) all read; `setup.stop = plan.stop` and
`setup.target = plan.target` for any reader that wants the flat names. The rules drawer
shows `STOP_ATR` and `LEVEL_PAD_ATR` as fixed conventions, not fields.

The ISRG numbers in the brief are exactly this convention: entry 405.81, stop 394.27 (=
405.81 − 11.54, one ATR), target **428.89** (= 405.81 + 2 × 11.54, `TARGET_R` 2.0; the
user's sheet said 429.14 — the same plan with the offset applied before the ATR, noted and
not used) — the engine reproduces them from `atr = 11.54`, and they ARE the golden ISRG
fixture (`tests/fixtures/options/isrg.json`, B8.2).

#### B2.7 What the signal writer stores — `option_signal.setup`

`ChartState` is the in-memory read; the row the nightly job writes (Part A2.1, keyed
`(symbol, snap_on, kind, prefs_hash)`) projects it to this shape, read back only through
`option_store.card_for` / `basket_rows_for`:

```python
option_signal.trend = ChartState["trend"]                       # a String column: up | down | sideways | unclear
option_signal.setup = {
  "kind": setup["kind"], "direction": setup["direction"], "level": setup["level"], "zone": setup["zone"],
  "touches": setup["touches"],                                  # int
  "quality": setup["quality"],                                  # int 0..100
  "close": ChartState["close"], "trend_days": ChartState["trend_days"], "atr": ChartState["atr"],
  "ema": ChartState["ema"],                                     # {e20, e50, e200}
  "plan": ChartState["plan"],                                   # {entry, stop, target, r} — the source
  "stop": ChartState["plan"]["stop"], "target": ChartState["plan"]["target"],   # the flat aliases (336.2 / 375.2 on the fixture); None when plan is None
  "levels": {"support": ..., "resistance": ...},
  "sup": ChartState["sup"], "tl": ChartState["tl"], "tl_bounce": ChartState["tl_bounce"], "rng": ChartState["rng"],   # Part C's dicts verbatim
  "evidence": ChartState["evidence"],
}
```

When `setup` is None the stored dict keeps every ChartState field above with `kind`,
`direction`, `level`, `zone`, `touches`, `quality` set to None — the card still shows the
trend, the EMAs, the trend line and the range. `stale` is not stored: `card_for(db, symbol,
user) -> dict | None` (None when no signal row exists yet) adds `stale`, `age_h`, `kind` and
`as_of` at read time from `snap_on` against `spread_monitor.et_today()`.

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
    step: int                # build phase from the design §9 (1..4) — an internal number; it never reaches a member
    priority: int            # base rank when several fit (higher first)
    why: str                 # template, .format(**ctx)
    must_happen: str         # template: what has to happen for this to work
    not_yet: str             # the headline sentence when this rule fits but its picker is not built (B3.3)
```

`STRATEGY_KEYS = (buy_call, buy_put, bull_call, bear_put, leaps_call, diagonal_call,
bull_put, bear_call, iron_condor, calendar)` — the ten keys every part uses, in the user's
order; they live HERE (`app/services/strategy_rules.py`), `option_prefs` imports them, and
`RULES` / the B3.2 table are in this order because it is the recommender's tie-break order.
`family` is one of the six above; `CREDIT_FAMILIES = {bull_put, bear_call, iron_condor}`
(Part C3.1) is the set whose POP is "chance of keeping it". `CURRENT_STEP` is a module
constant bumped as each phase of §9 lands.

#### B3.2 The ten rows (design §5.2, verbatim conditions)

| key | label | family / dir / side | trends | setups | iv_gate | term | earnings | weekly | needs | dte | step | prio |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `buy_call` | Buy call | long / up / debit | up | support_bounce, trendline_bounce, ema_rebound, breakout_retest | buy | any | none_inside | no | — | 45–90 | 2 | 60 |
| `buy_put` | Buy put | long / down / debit | down | failed_support, resistance_reject, trendline_bounce | buy | any | none_inside | no | — | 45–90 | 2 | 60 |
| `bull_call` | Bull call spread | debit_vertical / up / debit | up | same as buy_call | mid_or_buy | any | defined_risk | no | target_level | 30–60 | 2 | 55 |
| `bear_put` | Bear put spread | debit_vertical / down / debit | down | same as buy_put | mid_or_buy | any | defined_risk | no | target_level | 30–60 | 2 | 55 |
| `leaps_call` | Buy LEAPS | leaps / up / debit | up, sideways | * (any; a pullback setup adds quality) | mid_or_buy | any | any | **yes** | — | 270–540 | 4 | 40 |
| `diagonal_call` | Diagonal call spread (poor man's covered call) | time / up / debit | up | * | mid_or_buy (the long leg is bought) | front_ge_back preferred (soft) | none_inside (inside the SHORT leg) | no | slow_drift, resistance_level | short 30–45 / long 180–365 | 4 | 45 |
| `bull_put` | Bull put spread | credit_vertical / up / credit | up | support_bounce, trendline_bounce, ema_rebound | sell_directional | any | defined_risk | no | support_level | 30–60 | 1 | 60 |
| `bear_call` | Bear call spread | credit_vertical / down / credit | down | resistance_reject, trendline_bounce, failed_support | sell_directional | any | defined_risk | no | resistance_level | 30–60 | 1 | 60 |
| `iron_condor` | Iron condor | condor / neutral / credit | sideways | range | sell_neutral | any | defined_risk | no | range | 30–45 | 3 | 60 |
| `calendar` | Calendar spread | time / neutral / debit | sideways, up, down | range, * (with slow_drift) | any | front_ge_back | none_inside (inside the BACK expiry) | no | slow_drift | front 20–30 / back 50–70 | 4 | 45 |

`iv_gate = "mid_or_buy"` = `gates["mid"] or gates["buy"]` (rank ≤ 50). `earnings =
"defined_risk"` means: allowed through earnings only when the member's shared rule
`earnings_rule` is `"defined_risk_only"` (B4.1); with the default `"none_inside"` every
row behaves as `none_inside`. The `earnings` column IS `option_prefs.defined_risk(key)`
(B4.1): it is `defined_risk` for exactly the five keys that function returns True for —
`bull_put`, `bear_call`, `bull_call`, `bear_put`, `iron_condor`; `buy_call`, `buy_put`,
`calendar` and `diagonal_call` are `none_inside` regardless of the member's rule, and
`leaps_call` is `any`. `bull_put.trends` deliberately excludes the old
`_trend`'s "neutral — price holds above EMA50 and EMA200" case (`routes/options.py:59-82`):
the design's condition is "uptrend + support holding"; the neutral case comes back as
`iron_condor` / `calendar` when the range exists. `needs = "range"` is `rng is not None`;
the calendar's "price expected to sit near a strike" is `rng["sideways"]` or
(`rng["stack_flat"]` and `0.35 <= rng["pos_pct"] <= 0.65`) (Part C2.5), else `slow_drift`.

`why` / `must_happen` templates (ctx = ChartState + gauge + pick). The `must_happen`
sentence is the brief's "what has to happen for this to work": Part D prints it as the
"What has to happen" line under the chip row (D2.3) and in the Telegram block; it is not
optional text.

| key | why | must_happen |
|---|---|---|
| bull_put | "{trend_sentence} It {setup_sentence}. Options are expensive (IV {basis} {measure:.0f} {span}), so you are paid to sell a put spread below that {level_name}." | "{symbol} stays above {short_strike:g} until {expiry_label}. You keep the credit if it does nothing, drifts up, or even dips a little." |
| bear_call | mirror ("...above that resistance") | "...stays below {short_strike:g}..." |
| buy_call | "{trend_sentence} It {setup_sentence}. Options are cheap (IV {basis} {measure:.0f} {span}), so you buy the move rather than sell insurance." | "{symbol} reaches {target:g} (2R) before {expiry_label}; the stop is {stop:g}." |
| buy_put | mirror | mirror |
| bull_call | "... IV is middling ({measure:.0f}): a bare call is dear, so the spread sells a call at your target {target:g} to pay for part of it." | "{symbol} closes above {breakeven:g} by {expiry_label}; above {short_strike:g} you have the whole {max_profit:,.0f}." |
| bear_put | mirror | mirror |
| leaps_call | "Long-term uptrend (weekly EMA 20 above 50 above 200). A long-dated call bought deep in the money moves almost like {shares:.0f} shares, for about {cost_words} of the money, and only {extrinsic_pct:.0f}% of the share price pays for time." (`shares = delta × 100`; `cost_words` = "a quarter" / "a third" / "half" from `mid / spot`) | "{symbol} keeps its weekly uptrend over the next {months} months; you roll it out when {roll_dte} days remain." |
| diagonal_call | "Slow uptrend with a resistance at {resistance:g}: own a long-dated call (delta {long_delta:.2f}) and rent out a near-term call under that resistance each month." | "{symbol} grinds up but stays under {short_strike:g} by {short_expiry_label}; you re-sell the short call every cycle." |
| iron_condor | "Sideways: EMAs flat, price between {lower:g} (touched {lt}x) and {upper:g} ({ut}x). Options are expensive ({measure:.0f}), so you sell both sides outside the range." | "{symbol} stays between {short_put:g} and {short_call:g} until {expiry_label}." |
| calendar | "Price is sitting near {strike:g} and the front month is priced {term:.2f}x the back: you sell the dear near-term option and own the cheaper later one." | "{symbol} is near {strike:g} on {front_expiry_label} (between {be_lo:g} and {be_hi:g})." |

`{span}` is the gauge's day-count phrase (`premium_gauge.span_words(iv)`: "over the last
year" / "over 118 days" / "against the last 34 days (not a full year yet)", B1.4), so a headline never prints a rank
as if it were a full-year figure — and when `basis` is `provisional` or `unknown` the
templates use the gauge's `verdict_why` sentence in place of the "Options are
expensive/cheap (...)" clause (a provisional read is never dressed as a firm one).

`not_yet` (one per row; shown as the headline's conclusion when the rule fits but
`step > CURRENT_STEP`): `leaps_call` → "The long-term chart would suit a long-dated call;
that strategy is not in TradeHunter yet."; the others follow the pattern "{chart read}
would suit {the label in plain words}; that strategy is not in TradeHunter yet." (e.g.
`iron_condor` → "The sideways chart would suit selling both sides of the range; that
strategy is not in TradeHunter yet."). The words "step", "phase" or a number never appear
in any member-facing string.

#### B3.3 `recommend(chart: ChartState, gauge: dict, prefs: dict, *, snapshot_expiries: list[str]) -> dict`

Every rejection carries a `reason_key` from the fixed vocabulary Part D's chip row
renders (`option_words.chip_row`): `expensive | cheap_options | not_rich_enough |
trending_not_sideways | no_range | wrong_direction | no_setup | earnings_inside |
front_iv_under_back | no_long_dated | no_weekly_trend | not_available_yet`. The long text
(`reasons[]`) is this part's; the key is the vocabulary.

```python
def recommend(chart, gauge, prefs, *, snapshot_expiries):
    rows = []
    for rule in RULES:                                   # catalog order = tie-break order
        fails, warns = [], []                            # fails: (reason_key, text) in CHECK ORDER
        if chart["trend"] not in rule.trends:
            key = "trending_not_sideways" if rule.direction == "neutral" else "wrong_direction"
            fails.append((key, TREND_REASON[rule.direction][chart["trend"]]))        # "trending, not sideways" / "not an uptrend"
        if rule.setups != ("*",) and (chart["setup"] is None or chart["setup"]["kind"] not in rule.setups):
            fails.append(("no_setup", "no %s on the chart today" % SETUP_WORD[rule.key]))
        if rule.weekly and not chart["w_uptrend"]:
            fails.append(("no_weekly_trend", "no weekly uptrend (EMA 20 above 50 above 200 on the weekly chart)"))
        g = gauge["gates"]
        if rule.iv_gate != "any":
            if gauge["basis"] in ("provisional", "unknown"):
                if rule.side == "credit":                # a sell needs a measured rank; a provisional read never opens a sell gate
                    fails.append(("not_rich_enough", f"IV history too short to say options are expensive ({gauge['iv_n']} of {IV_RANK_MIN_OBS} days)"))   # a reason line; the IV-unknown sentence itself is B1.4's ONE string, never restated here
                else:                                    # a buyer is protected by price, not by the gate: pass with a warning, iv_fit = 0
                    warns.append(f"IV history too short to say options are cheap ({gauge['iv_n']} of {IV_RANK_MIN_OBS} days)")
            elif not _gate(g, rule.iv_gate):
                fails.append((IV_KEY[rule.side][rule.iv_gate][gauge["verdict"]], IV_REASON[rule.side][gauge["verdict"]]))
                # debit rows at SELL / NEUTRAL -> "expensive": "options too expensive to buy (IV rank 62)" / "not cheap enough to buy outright (IV rank 41 > 30)"
                # credit rows at BUY -> "cheap_options": "options too cheap to sell (IV rank 24)"; sell_neutral at 30-50 -> "not_rich_enough": "premium not rich enough for a condor (IV rank 41 < 50)"
        if rule.term == "front_ge_back" and not (gauge["term_ratio"] and gauge["term_ratio"] >= 1.0):
            fails.append(("front_iv_under_back", "near-term options are not dearer than the later month"))
        for need in rule.needs:
            if not _has(chart, need):
                fails.append((NEED_KEY[need], NEED_REASON[need]))   # range -> "no_range": "no range with both edges touched"; resistance_level / target_level -> "no_setup": "no resistance to cap the target at" ...
        e = earnings_block(rule, chart["earnings"], snapshot_expiries, prefs)   # B3.4
        if e: fails.append(("earnings_inside", e))
        if rule.step > CURRENT_STEP and not fails:
            fails_soft = ("not_available_yet", "not available yet")          # a fit whose picker is not built
        row = {"key": rule.key, "label": rule.label, "step": rule.step,
               "why": ..., "must_happen": ...,                                 # templates formatted on a fit; None on a reject
               "reasons": [t for _, t in fails] + warns, "reason_key": fails[0][0] if fails else None,
               "score": None if fails else _score(rule, chart, gauge), "fit": "rejected" if fails else "fit", "shown": False}
        rows.append(row)
    fits = sorted([r for r in rows if r["fit"] == "fit"], key=lambda r: (-r["score"], RULES_INDEX[r["key"]]))
    built = [r for r in fits if r["step"] <= CURRENT_STEP]
    for r in fits:
        r["fit"] = "also_fits"
        if r["step"] > CURRENT_STEP:
            r["reason_key"] = "not_available_yet"; r["reasons"] = ["not available yet"]   # chip: "{label} · not available yet"
    if built: built[0]["fit"] = "recommended"
    near = sorted([r for r in rows if r["fit"] == "rejected" and len(r["reasons"]) == 1], key=lambda r: RULES_INDEX[r["key"]])[:2]
    for r in near: r["shown"] = True                                           # up to 2 greyed chips WITH the reason (decision 9)
    strategies = sorted(rows, key=lambda r: (FIT_ORDER[r["fit"]], -(r["score"] or 0), RULES_INDEX[r["key"]]))
    return {"strategies": strategies,                                           # ALL TEN rows, recommended -> also_fits -> rejected
            "recommended": built[0]["key"] if built else None}
```

The returned `strategies` list is stored on the signal row as-is (contract SIGNAL): every
row `{key, label, fit ∈ {recommended, also_fits, rejected}, score, step, why,
must_happen, reasons[], reason_key, shown}`, `shown=True` on the ≤ 2 near-miss rejects.
Part D's `option_words.chip_row(strategies)` builds the chip row from it; nothing is
recomputed on read.

**An unbuilt rule can never be `recommended`.** A row whose `step > CURRENT_STEP` that
passes every check goes to `also_fits` with `reason_key = not_available_yet` and the chip
text "{label} · not available yet"; its picks are not computed, so it has no strike table
and no ticket. When it is the ONLY fit, `recommended` is None and the headline ends with
the rule's `not_yet` sentence. The score keeps the −10 below so such a row sorts under
every built fit inside `also_fits`.

**Ranking when several fit** (`_score`, deterministic, explainable, every term shown on hover):

```
score = rule.priority
      + iv_fit        # how far inside its gate the measure sits: credit rows (measure - 30)/70*20, buy rows (30 - measure)/30*20, mid rows 20 - |measure - 40|; clipped 0..20; 0 when basis is provisional / unknown
      + setup_quality / 5                      # 0..20 from ChartState.setup.quality (B2.3)
      + (10 if rule.term == "front_ge_back" and term_ratio >= TERM_EVENT else 0)
      + (5 if chart["structure"]["state"] == ("bullish" if rule.direction == "up" else "decelerated") else 0)
      - (10 if rule.step > CURRENT_STEP else 0)  # an unbuilt rule sorts under every built one (it is in also_fits regardless)
```

Worked case (design §5.4): uptrend, support bounce quality 80, rank 45 → `bull_put`: 60 +
(45−30)/70×20 = 4.3 + 16 = 80.3; `bull_call`: 55 + (20 − 5) = 15 + 16 = 86 → bull call
spread first, bull put spread "also fits"; at rank 62: bull_put 60 + 9.1 + 16 = 85.1 vs
bull_call rejected (mid_or_buy fails, `reason_key = expensive`) → one chip, bull_call
greyed "options too expensive to buy (IV rank 62) — a spread you pay for is dear" as a
near-miss. At rank 24: buy_call 60 + 4 + 16 = 80, bull_call 55 + 4 + 16 = 75 → buy call
first, bull call also fits, bull put greyed "options too cheap to sell" (`cheap_options`).

#### B3.4 Earnings

`earnings_block(rule, earnings, expiries, prefs)` returns a reason string or None:

```
if earnings is None: return None                       # unknown: never blocks; the card shows "earnings: not known — check" (bull_put.select's non-blocking check, bull_put.py:395-397); the Telegram push SKIPS such ideas (B7.4)
window = the listed expiries inside rule.dte (for two-expiry rules: the BACK/long expiry window for calendar, the SHORT window for diagonal — the leg whose life earnings must not cross)
inside = [x for x in window if earnings.date <= x]      # earnings on or before expiry = inside (bull_put.select gate 2, bull_put.py:382-390: ok = e > x)
allowed = rule.earnings == "any" or (rule.earnings == "defined_risk" and prefs["shared"]["earnings_rule"] == "defined_risk_only")
if inside and not allowed and len(inside) == len(window):
    return f"earnings {earnings.date} ({earnings.days}d) sits inside every {lo}-{hi} day expiry"
return None                                            # some expiry clears it: the picker will skip the ones that do not (B4.3)
```

`leaps_call.earnings = "any"`: a 9–18 month option crosses several reports by construction;
the LEAPS rule instead adds the reason-line "crosses earnings on {date}: the stop is the
weekly trend, not the print". A rejection with `reason_key = earnings_inside` has a
consequence on the card the other keys do not: its strike table is hidden entirely
(B6.4).

#### B3.5 The headline and the signal row

The headline sentence is composed **at write time** by the engine (`option_engine.compute(chain,
metrics, state, prefs)`, B0.1) through Part D's templates (`option_words.headline(setup, iv,
strategies) -> str`, D2.7 moved to services) and stored in `option_signal.headline`;
`_options_card.html` prints `sig.headline` and a chip click never changes it. `compute()`
returns `{status, headline, setup, iv, strategies, picks, computed_ms, engine_version}` —
the signal row's content — and the nightly job writes one row per `(symbol, snap_on, kind,
prefs_hash)`: the house hash always, plus **every DISTINCT `prefs_hash` saved in
`user_option_prefs`** (`option_prefs.distinct_hashes(db)`, B4.1) — so a member with overrides sees their own picks on the
first paint and the basket is never guessing. `strategies` is the same for every hash of a
symbol (the recommender reads `shared.earnings_rule` and nothing else from prefs); `picks`
differ per hash.

### B4. The strike picker

#### B4.1 Member preferences — the ONE schema (`app/services/option_prefs.py`), house defaults, plain-language labels

Storage: table `user_option_prefs` (design §4.3; Part A's columns) — one row per member,
portable JSON holding ONLY the fields the member changed:

```python
class UserOptionPrefs(Base):
    __tablename__ = "user_option_prefs"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    prefs = Column(JSON, nullable=False, default=dict)          # sparse overrides, per block (+ the "telegram" key outside the blocks, below)
    prefs_hash = Column(String(16), nullable=False, index=True)  # prefs_hash(read(db, user)) at write time: the first 12 hex of the sha1 (String(16) leaves room) — the nightly job's DISTINCT list (B3.5)
    schema_version = Column(Integer, nullable=False, default=1)  # bumped when a field is renamed; clean() migrates old keys
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)
    user = relationship("User", backref=backref("option_prefs", uselist=False))   # the one-to-one User.option_prefs read() walks, declared here in models.py
```

Only overrides are stored (the `sym_conds` pattern: `routes/curated.py:104` passes the raw
stored dict through `ema_setup.clean_enabled`, which fills every missing key from
`COND_DEFAULT`, `ema_setup.py:455-462`). House defaults live in code
(`option_prefs.HOUSE`) for v1 — admin-editable later means moving `HOUSE` into a
single-row table with the same shape, and `clean()` / `read()` do not change. Members on
house defaults share one `option_signal` row per symbol (the house hash).

**Sizing inputs are NOT duplicated here.** `risk_pct` and `nlv` are read from
`trade_prefs.read(user)` (`trade_prefs.py:71-91`; defaults `DEFAULT_RISK_PCT = 1.0`,
`DEFAULT_NLV = 0.0` = "not told yet") — the same two numbers that size a Curated share
trade, so one account value sizes everything (`trade_prefs.py:15-23`). `read()` exposes
them under `prefs["account"] = {nlv, risk_pct, nlv_source}` (`nlv_source` `"prefs"` when a
value is stored, else None; `"live"` only for the one request a Live press sizes, B5.3),
which is NOT a schema block and is never hashed. The four credit exit lines
(`loss_fraction`, `profit_target`, `dte_floor`, `roll_delta`) likewise stay in `trade_prefs`
and write through it; sizing (B5.1) and the monitor (B7.3) read them from
`trade_prefs.read(user)` directly — they are not carried in `prefs`.

**One table, two consumers.** `option_prefs.SCHEMA = {block: {field: Field(default, lo,
hi, kind, label, help, plain, step, unit)}}` — `Field` is a namedtuple in exactly that
positional order (`FIELDS` is the same thing flattened to `(block, field, Field)` rows for
the drawer). The engines read `default / lo / hi / kind`;
Part D's drawer renders `label` (the field's name), `help` (the one short line under the
input saying what the number is), `plain` (the one-line translation of the CURRENT value
that `option_words.rule_words` prints in the rules line and the My-rules tab, design §7),
`step` and `unit`. There is no second fields table anywhere. `kind` ∈ {"num", "int",
"bool", "choice"} — nothing else; a setting that is not one of those (Telegram, below) is
not a field. The drawer's five tabs map onto the seven blocks:
**shared** | **credit** (= `credit_vertical`) | **debit** (= `debit_vertical` + `long` +
`leaps`, rendered as the sub-sections "Spreads", "Buy call / put", "LEAPS", collapsed by
default on `< lg`, the translation first and the fields under "change these") |
**condor** | **time**. Widths are in ATR with the $ figure shown beside them (from the
ticker's ATR); `extrinsic_pct_max` is a % of the STOCK price (B4.5); `earnings_rule` has
exactly two values; `STOP_ATR`, `TARGET_R` and `LEVEL_PAD_ATR` are constants shown as
fixed conventions, not fields.

**shared**

| field | default | bounds | step · unit | label | help | plain |
|---|---|---|---|---|---|---|
| `min_oi` | 500 | 0–100000 | 50 · contracts | Minimum open interest per leg | open contracts at that strike — so you can get out | "every leg has at least {v} open contracts" — `bull_put.MIN_OPEN_INTEREST` (`bull_put.py:64`) |
| `oi_per_contract` | 10 | 1–100 | 1 · × | … and at least this many times your contracts | | "and at least {v}× the contracts you trade" — `bull_put.OI_PER_CONTRACT` (`bull_put.py:65`) |
| `max_leg_spread` | 0.50 | 0.01–5.00 | 0.05 · $ | Max bid/ask width per leg | wide markets eat the edge | "no leg quoted wider than ${v}" — `bull_put.MAX_LEG_SPREAD`; `ideal` tier at 0.40 (`bull_put.py:51-52`) |
| `min_leg_volume` | 20 | 0–10000 | 10 · contracts | Traded today per leg (warning only) | | "a warning under {v} traded today" — `bull_put.MIN_LEG_VOLUME` (`bull_put.py:66`); never vetoes |
| `earnings_rule` | `"none_inside"` | choice: `none_inside` / `defined_risk_only` | — | Earnings inside the trade | the one thing a stop cannot protect against | "not allowed" / "defined-risk trades only — never a trade whose loss is open". There is no third value |
| `monthly_only` | False | bool | — | Monthly expiries only | third-Friday expiries have the deepest markets (`spread_scan.is_monthly`, `spread_scan.py:74-81`) | "monthly expiries only" / "any listed expiry" |
| `chart_constraint` | True | bool | — | Strikes must respect the chart | short strikes under support / above resistance / outside the range | "short strikes stay outside the level the chart says must hold". Unticking shows the amber sentence "Without this, the short strike can sit inside the zone the chart says must hold" before Save and marks the override dot rose (Part D3) |
| `gap_mult` | 2.0 | 1–5 | 0.5 · × | Worst case if the stock gaps past the stop, as a multiple of your risk | the stop cannot fire inside a gap; this caps what a gap can cost | "a gap through the stop may cost at most {v}× your risk budget" — `GAP_MULT` is this field's name in prose and formulas only (B5.2) |

`max_position_pct` is NOT a field: `MAX_POSITION_PCT` 10.0 is a constant in
`opt_constants.py` (B0.3, "global, never override"). The first four rows — `min_oi`,
`oi_per_contract`, `max_leg_spread`, `min_leg_volume` — are the `liquidity` four of
`PICK_FIELDS` (below).

**Telegram is not a field either.** The member's Telegram settings live under their OWN
top-level key of the prefs the member reads back, `prefs["telegram"] = {enabled, chat_id,
verified, quiet, paused_until, pending: {chat_id, code, expires} | None}` — stored under
the `telegram` key of the same `user_option_prefs.prefs` JSON, OUTSIDE the blocks, never
in `SCHEMA`, never hashed, never touched by `write()` / `reset()`. It is written only by
Part D's `POST /options/telegram` (body `{action ∈ {request_code, verify, quiet, pause,
disable}, chat_id?, code?, pause_days?}`, D4.2): `request_code` fills `pending` with the
6-digit code the bot replies to `/start`, `verify` moves the chat id into place and sets
`verified` only when the code matches, `quiet` / `pause` (`paused_until` = today +
`pause_days`) / `disable` flip the switches. The drawer shows it as "Telegram: on / quiet /
paused until {date} / off" (`option_words.rule_words`).

**credit_vertical** (bull put / bear call) — the mockup's line: "short strike delta
0.20–0.30 (≈ 70–80% chance it expires worthless) · DTE 30–60 · width · min credit 25% of
width · sell only when IV rank ≥ 30 · short strike must be under support + line" (the
credit floor is measured against what you RISK — `width − credit` — the base the plain
sentence and the B4.5 score already use, so the golden 330/320 at 2.10 (26.6% of its
7.90 risk) passes the house 25)

| field | default | bounds | step · unit | label | help | plain |
|---|---|---|---|---|---|---|
| `short_delta_lo` / `short_delta_hi` | 0.20 / 0.30 | 0.05–0.50 | 0.01 · δ | Short strike delta band | the strike you sell | "≈ {70–80}% chance it expires worthless" (the old tab keeps `bull_put` 0.20–0.25; the house default follows the design) |
| `width_atr_lo` / `width_atr_hi` | 0.5 / 1.5 | 0.1–5 | 0.1 · ATR | Spread width, in ATRs | the distance between your two strikes, in the stock's daily range | "width {lo}–{hi} ATR (≈ ${lo_usd}–{hi_usd} on {sym})" — ticker-relative: ≈ $6–17 on LRCX (ATR 11.5), $1–3 on a $40 name; the $ figure is always shown beside it |
| `long_offset_max` | 3 | 1–6 | 1 · strikes | Long strike at most this many listed strikes below | | `bull_put.LONG_OFFSET_MAX` is 2 (`bull_put.py:42`); 3 lets the ATR width be met on $5-spaced chains |
| `credit_pct_min` | 25 | 5–60 | 1 · % | Minimum credit, % of what you risk | the credit against the max loss (`width − credit`), the same ratio the score ranks by (B4.5) | "you must be paid at least {v}% of what you risk" |
| `dte_lo` / `dte_hi` | 30 / 60 | 7–180 | 1 · d | Days to expiry | | design §5.2 (the playbook tab keeps 45–60) |
| `iv_gate_min` | 30 | 0–100 | 1 · rank | Sell only when IV rank is at least | | mirrors `SELL_DIR_MIN_RANK`; a member may raise it |

**debit_vertical** (bull call / bear put)

| field | default | bounds | step · unit | label | help | plain |
|---|---|---|---|---|---|---|
| `long_delta_lo` / `long_delta_hi` | 0.60 / 0.70 | 0.3–0.95 | 0.01 · δ | Long strike delta | | "moves about 60–70 cents per $1 of the stock" |
| `short_delta_lo` / `short_delta_hi` | 0.25 / 0.35 | 0.05–0.6 | 0.01 · δ | Short strike delta (SOFT when the chart target decides, B4.3) | | "the strike you give the upside away at" |
| `reward_cost_min` | 1.0 | 0.2–5 | 0.1 · × | Minimum reward ÷ cost | | "the most you can make is at least {v}× what you pay" |
| `dte_lo` / `dte_hi` | 30 / 60 | 7–180 | 1 · d | Days to expiry | | |

**long** (buy call / buy put)

| field | default | bounds | step · unit | label | help | plain |
|---|---|---|---|---|---|---|
| `delta_lo` / `delta_hi` | 0.60 / 0.70 | 0.3–0.95 | 0.01 · δ | Delta | | "stock-like, with ~35% less capital" |
| `theta_pct_max` | 1.0 | 0.1–5 | 0.1 · %/day | Max daily time decay, % of premium | | "losing more than {v}% a day while nothing happens is too fast" |
| `dte_lo` / `dte_hi` | 45 / 90 | 14–365 | 1 · d | Days to expiry | | |
| `premium_stop_pct` | 50 | 10–100 | 5 · % | Rule stop: close at this % of premium lost (B7) | the rule stop; the chart stop usually fires first | "close if the option loses {v}% of what you paid". Shared with the debit verticals (B5.2) |

**leaps**

| field | default | bounds | step · unit | label | help | plain |
|---|---|---|---|---|---|---|
| `delta_lo` / `delta_hi` | 0.70 / 0.80 | 0.5–0.95 | 0.01 · δ | Delta (deep in the money) | | "behaves like 70–80 shares per contract" |
| `extrinsic_pct_max` | 10 | 1–40 | 1 · % of the share price | Max time value, % of the STOCK price | not of the option's price (B4.5 explains) | "you pay at most {v}% of the share price for time" |
| `months_lo` / `months_hi` | 9 / 18 | 6–36 | 1 · mo | Months to expiry | | |
| `roll_dte` | 180 | 60–365 | 10 · d | Roll out when this many days remain | the roll date (B7) | |
| `delta_floor` | 0.55 | 0.3–0.7 | 0.01 · δ | Roll down-and-out if delta falls under | delta drift (B7) | |
| `premium_stop_pct` | **40** | 10–100 | 5 · % | Rule stop: close at this % of premium lost (B7) | the weekly trend can hold while the position has not | "close if the call loses {v}% of what you paid". The diagonal's long leg inherits it (B7.3) |

**condor**

| field | default | bounds | step · unit | label | help | plain |
|---|---|---|---|---|---|---|
| `short_delta_lo` / `short_delta_hi` | 0.15 / 0.20 | 0.05–0.35 | 0.01 · δ | Short strike delta, each side | | "≈ 1-in-6 chance on each side" |
| `wing_atr_lo` / `wing_atr_hi` | 0.5 / 1.5 | 0.1–5 | 0.1 · ATR | Wing width, in ATRs | | "wings {lo}–{hi} ATR (≈ ${lo_usd}–{hi_usd})" — the $ figure beside it |
| `credit_pct_min` | 30 | 5–60 | 1 · % | Minimum credit, % of what you risk | the credit against the max loss (the wider wing less the credit) — the same base as the verticals | "you must be paid at least {v}% of what you risk" |
| `dte_lo` / `dte_hi` | 30 / 45 | 7–120 | 1 · d | Days to expiry | | |
| `roll_delta` | 0.30 | 0.1–0.6 | 0.01 · δ | Act when either short delta reaches | B7 | |

The condor has no loss field of its own: it is a credit family, so its rule stop is the
credit-family line — `trade_prefs.loss_fraction` × max loss (20% of max loss), the same
line the verticals use (B5.1, B7.3).

**time** (calendar / diagonal)

| field | default | bounds | step · unit | label | help | plain |
|---|---|---|---|---|---|---|
| `cal_front_lo` / `cal_front_hi` | 20 / 30 | 7–60 | 1 · d | Calendar front leg DTE | | |
| `cal_back_lo` / `cal_back_hi` | 50 / 70 | 30–180 | 1 · d | Calendar back leg DTE | | |
| `cal_delta_tol` | 0.05 | 0.01–0.2 | 0.01 · δ | How far from delta 0.50 the strike may sit | | "at the money" |
| `cal_take_pct` | 25 | 5–100 | 5 · % | Take profit at this % of the debit | calendars pay in small steps | |
| `diag_long_delta_lo` / `_hi` | 0.70 / 0.80 | 0.5–0.95 | 0.01 · δ | Diagonal long leg delta | | |
| `diag_long_dte_lo` / `_hi` | 180 / 365 | 90–730 | 5 · d | Diagonal long leg DTE (6–12 months) | | |
| `diag_short_delta_lo` / `_hi` | 0.20 / 0.30 | 0.05–0.5 | 0.01 · δ | Diagonal short leg delta | | |
| `diag_short_dte_lo` / `_hi` | 30 / 45 | 7–90 | 1 · d | Diagonal short leg DTE | | |

**Merge on read** — `option_prefs.clean(raw) -> dict` (pure) and `option_prefs.read(db,
user) -> dict` (the entry point every route and the nightly job call):

```python
from .strategy_rules import STRATEGY_KEYS                      # the ten keys live in strategy_rules.py; this module only imports them

def clean(raw: dict | None) -> dict:
    """The pure merge over HOUSE: every block filled from SCHEMA, bad / out-of-range -> default
    (trade_prefs._num semantics, trade_prefs.py:61-68). No I/O. Also migrates renamed keys
    (schema_version) and lists what the member changed."""
    raw = raw or {}
    out = {}
    for block, fields in SCHEMA.items():
        src = raw.get(block) if isinstance(raw.get(block), dict) else {}
        out[block] = {k: _coerce(src.get(k), f) for k, f in fields.items()}
    out["_overridden"] = {f"{b}.{k}" for b, fields in SCHEMA.items() for k, f in fields.items()
                          if (raw.get(b) or {}).get(k) is not None and out[b][k] != f.default}   # dotted field names the member changed (the drawer's override dots)
    return out

def read(db, user) -> dict:
    """clean() over the member's stored overrides, plus the two keys that are NOT schema blocks."""
    row = user.option_prefs if user is not None else None             # User.option_prefs, one-to-one (models.py)
    stored = (row.prefs if row else {}) or {}
    out = clean(stored)
    out["telegram"] = {**TELEGRAM_DEFAULT, **(stored.get("telegram") or {})}   # its OWN key: never a SCHEMA field, never hashed, written only by POST /options/telegram
    tp = trade_prefs.read(user)
    out["account"] = {"nlv": tp["nlv"], "risk_pct": tp["risk_pct"],
                      "nlv_source": "prefs" if tp["nlv"] > 0 else None}      # NOT a block: never hashed, never written here ("live" is set per request by B5.3)
    return out

TELEGRAM_DEFAULT = {"enabled": False, "chat_id": None, "verified": False, "quiet": False, "paused_until": None, "pending": None}

def write(db, user, tab, form) -> tuple[dict, list[str]]:     # tab = a drawer tab (shared | credit | debit | condor | time) and the block(s) it covers; trade_prefs.write pattern: report out-of-range, never clamp silently (trade_prefs.py:94-134); stores only values != HOUSE; recomputes prefs_hash
def reset(db, user, tab) -> dict                              # drops the tab's overrides
def for_strategy(prefs, strategy_key) -> dict                 # prefs[family_of(strategy_key)]
def family_of(strategy_key) -> str                            # bull_put -> "credit_vertical" ... (Part C's payoff.build derives the family through it)
def defined_risk(strategy_key) -> bool                        # True ONLY for bull_put, bear_call, bull_call, bear_put, iron_condor — B3.2's earnings column and D's track-idea gate read this
def distinct_hashes(db) -> list[str]                          # every DISTINCT user_option_prefs.prefs_hash, HOUSE_HASH first — the nightly job's per-symbol row list (B3.5)

LIQUIDITY_FIELDS = ("min_oi", "oi_per_contract", "max_leg_spread", "min_leg_volume")   # PICK_FIELDS' liquidity four
PICK_FIELDS = {   # the fields that change a pick — and ONLY those
  "shared":          LIQUIDITY_FIELDS + ("earnings_rule", "monthly_only", "chart_constraint"),
  "credit_vertical": ("short_delta_lo", "short_delta_hi", "width_atr_lo", "width_atr_hi", "long_offset_max", "credit_pct_min", "dte_lo", "dte_hi", "iv_gate_min"),
  "debit_vertical":  ("long_delta_lo", "long_delta_hi", "short_delta_lo", "short_delta_hi", "reward_cost_min", "dte_lo", "dte_hi"),
  "long":            ("delta_lo", "delta_hi", "theta_pct_max", "dte_lo", "dte_hi"),
  "leaps":           ("delta_lo", "delta_hi", "extrinsic_pct_max", "months_lo", "months_hi"),
  "condor":          ("short_delta_lo", "short_delta_hi", "wing_atr_lo", "wing_atr_hi", "credit_pct_min", "dte_lo", "dte_hi"),
  "time":            ("cal_front_lo", "cal_front_hi", "cal_back_lo", "cal_back_hi", "cal_delta_tol",
                      "diag_long_delta_lo", "diag_long_delta_hi", "diag_long_dte_lo", "diag_long_dte_hi",
                      "diag_short_delta_lo", "diag_short_delta_hi", "diag_short_dte_lo", "diag_short_dte_hi"),
}

def prefs_hash(merged) -> str:
    """First 12 hex of sha1 over the PICK-RELEVANT fields only (stored in the String(16)
    column): delta bands, DTE, widths, credit / reward floors, liquidity, earnings_rule,
    monthly_only, chart_constraint. NEVER nlv, risk_pct, gap_mult, the exit lines
    (premium_stop_pct, roll_dte, delta_floor, roll_delta, cal_take_pct), `account`,
    `_overridden` or `telegram` — changing the account value or an exit line must not
    invalidate a cached pick. Takes the MERGED dict (clean() / read() output)."""
    sub = {b: {k: merged[b][k] for k in keys} for b, keys in PICK_FIELDS.items()}
    return hashlib.sha1(json.dumps(sub, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]

HOUSE_HASH = prefs_hash(clean({}))     # members on house defaults share this signal row
```

#### B4.2 Candidate enumeration

`strike_picker.pick(strategy_key, chain_view, chart, gauge, prefs, *, today=None) ->
PickResult`. Pure; no NLV — sizing happens at read time (B5). Internally:
`enumerate_<family>()` → `apply_constraints()` → `liquidity()` → `score()` → `top3()`.
Every candidate is a dict — the **Pick** every part shares (contract PICK):

```python
Pick = {
  "symbol": "LRCX", "strategy": "bull_put", "family": "credit_vertical",
  "legs": [Leg, ...],                                   # the stored leg shape of B0.2: {expiry, right, strike, side, qty (positive), price (mid), bid, ask, iv (fraction), delta (signed), oi, volume}
  "expiry": "2026-11-20", "dte": 48,                    # the (front) expiry; two-expiry rows add "back_expiry", "back_dte"
  "net": -2.10,                                         # per share: NEGATIVE = credit received, POSITIVE = debit paid (the sign option_trades.net_entry keeps, B7.1) — the golden 330/320
  "width": 10.0,
  "max_profit": 210.0, "max_loss": 790.0,               # POSITIVE $ per contract, both
  "breakevens": [327.90],                               # a LIST of stock prices (one for a vertical, two for a condor / calendar)
  "pop": 0.75, "pop_kind": "keep" | "profit",           # B4.6; the number and which sentence it gets (0.75 = 1 − the short 330P's delta 0.25)
  "pop_model": 0.73 | None,                             # the C3.8 model figure, shown as "model estimate 73%"
  "greeks": {"delta": 0.062, "theta": 0.023, "vega": -0.054, "gamma": ...},   # per share, position-signed (sold leg negated)
  "liquidity": {"tier": "clean" | "limit" | "wide" | "thin" | "unknown", "widest": 0.30, "min_oi": 1840, "vol_ok": True | False | None, "worst_fill": 1.80, "notes": [...]},
  "constraint": {"ok": True, "detail": "330 sits under 336.2 (support 339.1 less 0.25 ATR) and under the trend line at expiry (362.1)"},
  "chart_stop": 336.2, "chart_stop_pl": -120.7, "rule_stop_pl": -158.0,   # per contract $, P/L sign (negative = a loss): the plan's stop (B2.6) and the two losses B5.1 computes at write time; the card prints "about -$121"
  "checks": [{"name": "open interest >= 500", "ok": True}, {"name": "earnings inside expiry", "ok": False, "blocking": False, "detail": "Oct 22 is inside Nov 20"}, ...],
  "score": 0.199, "why": [...], "words": {...},         # B4.7
  "sizing": None,                                       # filled at READ time by option_sizing.size(pick, nlv, prefs) (B5); never stored
  "status": "ok" | "nearest" | "none", "rules_line": "...", "considered": 23, "degenerate": None | {...},   # B4.8
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
| credit_vertical | sell short (P for bull_put, C for bear_call), buy long further OTM | shorts: every strike with `short_delta_lo <= abs(delta) <= hi` (else the `bull_put.ALT_NEAR_SHORTS` = 2 nearest, flagged `in_band False`, `bull_put.py:197, 288-290`); longs: the next 1..`long_offset_max` listed strikes beyond the short whose width is within `[width_atr_lo, width_atr_hi] x ATR` | every expiry in the window (the old tab took ONE expiry from the bridge; the snapshot has all). After a Live press only the live expiry is enumerated (B5.3) |
| debit_vertical | buy long (C for bull_call, P for bear_put) at `long_delta` band, sell short at `short_delta` band further OTM | longs in band; shorts: the strike AT or just beyond the chart target (`plan.target` rounded UP to the next listed strike for calls, DOWN for puts) — the chart decides the short; the delta band is checked and reported, not enforced (B4.3) | window |
| long | buy one option | strikes in `delta` band; a leg fails when `abs(theta)/price x 100 > theta_pct_max` | window |
| leaps | buy one call (LEAPS puts are not in the catalog) | strikes in `delta` band AND `extrinsic <= extrinsic_pct_max/100 x spot` where `extrinsic = price - max(0, spot - strike)` | every expiry with `months_lo x 30 <= dte <= months_hi x 30` (270–540 days) |
| condor | sell put + buy lower put, sell call + buy higher call | each side exactly as credit_vertical with the condor band; wings in `[wing_atr_lo, wing_atr_hi] x ATR`; the four legs share one expiry | window |
| calendar | sell front, buy back, same strike, same right (calls when `close >= strike` else puts — the OTM side is cheaper to run) | the strike nearest the "sit" level: the close (which must sit mid-range, `rng.pos_pct` 0.35–0.65, when a range exists — Part C2.5); keep the strike if `abs(abs(delta_front) - 0.5) <= cal_delta_tol` | front ∈ `[cal_front_lo, cal_front_hi]`, back ∈ `[cal_back_lo, cal_back_hi]`, **every front×back pair**, requirement `iv_front_atm >= iv_back_atm` per pair (term is per pair, not only the gauge's global `term_ratio`) |
| diagonal_call | buy long-dated call (LEAPS band), sell near-term call | long: `diag_long_delta` band in the long window; short: `diag_short_delta` band in the short window, AND `strike_short <= resistance.level` (under the resistance — the design's rule) AND `strike_short > spot`; hard safety rule `(K_short - K_long) + price_short >= price_long` ("if it rips through the short strike you still come out ahead": the width plus the credit covers the long's cost) | long window × short window |

#### B4.3 Chart-derived constraints (design §5.3 — hard, not a preference, switchable off only by `shared.chart_constraint`)

| strategy | constraint on strikes | source fields |
|---|---|---|
| bull_put | `short_strike <= min(sup_lo, line_at_expiry) - LEVEL_PAD_ATR x ATR` where `sup_lo` = `setup.zone[0]` for a support bounce, the EMA value for an `ema_rebound` (`setup.level`), the line's value today for a `trendline_bounce`; `line_at_expiry = tl["value_at"][expiry]` when a trend line exists and `tl["broken"]` is False | `chart.setup`, `chart.tl` |
| bear_call | `short_strike >= max(res_hi, line_at_expiry) + LEVEL_PAD_ATR x ATR` | mirror |
| bull_call / bear_put | `short_strike >= plan.target` (calls) / `<= plan.target` (puts): the short leg caps at the chart target, never before it. The short delta band becomes a **soft** check: if the forced strike's delta is outside the band the pick carries the note "short delta 0.41, above your 0.35 — the chart target sits closer than your band" and loses `SOFT_BAND_PENALTY = 0.9` in score | `chart.plan.target` |
| buy_call / buy_put | none on the strike; `plan.stop` and `plan.target` feed sizing (B5) and the ticket (B6) | `chart.plan` |
| leaps_call | none; the weekly trend is the stop (B7) | — |
| iron_condor | `short_put <= rng["zone_low"][0] - PAD` and `short_call >= rng["zone_high"][1] + PAD` | `chart.rng` (Part C2.1) |
| calendar | strike within `LEVEL_PAD_ATR x ATR` of the sit level (the close; mid-range when `rng` exists) | `chart.rng` / close |
| diagonal_call | `spot < short_strike <= resistance.level`; resistance = `rng["high"]` or `levels.resistance` (the nearest resistance_reject / flip level above) | `chart.rng`, `chart.levels.resistance` |

When the constraint removes every candidate the result is the degenerate case `constraint`
(B4.8), and the nearest candidate that fails it is kept as `nearest` so the card can say
by how much.

#### B4.4 Liquidity (shared, from `bull_put._pair`, `bull_put.py:217-238`)

Per leg: `spread = ask - bid`; tier `clean` ≤ `IDEAL_LEG_SPREAD` (0.40), `limit` ≤
`max_leg_spread`, else `wide` (→ excluded). OI: `oi >= bull_put.oi_needed(contracts,
min_open_interest=min_oi, oi_per_contract=oi_per_contract)` on every leg (`bull_put.py:159-162`),
evaluated for one contract at write time and re-checked for the sized count at read time
(a `checks[]` row, never a veto after the fact); `None` OI = "unknown" (warn, never
exclude — "TWS did not say is not evidence", `bull_put.py:229-230`); known-thin →
excluded. Volume: warn only. `liquidity_factor` = 1.0 clean / 0.9 limit / 0.85 when OI is
unknown. For multi-leg candidates the position's tier is its WORST leg. `worst_fill` = the
natural price (sold legs at the bid, bought legs at the ask) — what the collect / pay words
quote as the low end (B4.7).

#### B4.5 Scoring per family

All scores are dimensionless and only compared within one strategy; `why` strings say what
each pick is best at (the `rank_pairs` pattern, `bull_put.py:311-336`).

| family | score | notes |
|---|---|---|
| credit_vertical | `(credit / max_loss_per_share) x POP x liquidity_factor` where `credit / max_loss_per_share = credit / (width - credit)` — the return on the money at risk | `rank_pairs` sorts lexicographically (liquid, in-band, clean, ratio: `bull_put.py:308-309`); the product is the design's "credit ÷ max loss × POP" and keeps the same winners on the cases in `bull_put`'s tests (a wider pair pays more credit for more max loss and loses on ratio; a lower short wins POP and loses ratio). Hard floor `credit >= credit_pct_min/100 x (width - credit)` — the same ratio, against what you risk (the golden 330/320: 2.10 / 7.90 = 26.6% ≥ 25%) |
| debit_vertical | `(reward / cost) x POP x liquidity_factor x (SOFT_BAND_PENALTY if short delta out of band)` with `reward = width - debit`, `cost = debit` | hard floor `reward / cost >= reward_cost_min` |
| long | `(delta / price) x (1 - theta_drag / theta_max)` with `theta_drag = abs(theta) / price` per day, `theta_max = theta_pct_max / 100` | "stock-like movement per $ of premium", discounted by how close the decay sits to the member's ceiling. Legs over the ceiling are already out |
| leaps | `(delta / price) x (1 - extrinsic / (extrinsic_pct_max/100 x spot))` | leverage per $ with the least time value paid; the discount is 0 at the cap, 1 at zero extrinsic |
| condor | `(credit / max_loss_per_share) x POP_both x liquidity_factor`, `max_loss_per_share = max(width_put, width_call) - credit`, `POP_both = 1 - abs(delta_sp) - abs(delta_sc)` | hard floor `credit >= credit_pct_min/100 x max_loss_per_share` (against what you risk, as for the verticals) |
| calendar | `front_price / net_debit` (front-month premium collected per $ of back-month cost) `x POP x liquidity_factor` (POP from `payoff.pop`, B4.6) | `net_debit = back_price - front_price` is the max loss |
| diagonal_call | `(short_price x cycles / long_price) x liquidity_factor` with `cycles = long_dte / short_dte` (how many short cycles the long leg can host) | "monthly income ÷ long-leg cost", annualised to the long leg's life; the safety rule in B4.2 is a hard filter |

`extrinsic_pct_max` is "% of the stock price", not of the option: at IV 32% and 15 months
even an ISRG call 85 points in the money (delta 0.84) carries 27% of ITS price as time
value, so "≤ 10% of the option's price" would never pass and the 0.70–0.80 band could never
be met. Measured against the share price the rule does what the design means — the member
pays at most 10% of the stock for time — and it is ticker-relative.

#### B4.6 POP — one rule, one function, two sentences

Every pick carries `pop` (0..1) and `pop_kind` ∈ {`keep`, `profit`}; the sentence is
fixed by `pop_kind` (decision 9) and rendered by Part D's `option_words.pop_words(pop,
pop_kind)` — the pick carries no wording / word / text field of its own; the sentence is
always derived from `pop_kind`.

| family | `pop` | `pop_kind` | basis |
|---|---|---|---|
| bull_put / bear_call | `1 - abs(short_delta)` | keep | the chain's own delta, the "~75–80% win probability" of the playbook (`bull_put.py:21, 251`) |
| iron_condor | `1 - abs(delta_short_put) - abs(delta_short_call)` | keep | both sides, same approximation |
| **every other family** (buy_call, buy_put, bull_call, bear_put, leaps_call, calendar, diagonal_call) | `payoff.pop(family, legs, spot, sigma_h, T_h, xs, ys)["value"]` — Part C3.8's ONE function: the lognormal mass over every profitable interval of the expiry curve (`xs`, `ys` from `payoff.grid` / `expiry_curve`; for a calendar / diagonal the curve at the FRONT expiry with the back leg model-valued, which is the only POP a time spread can have — design §6e) with `sigma_h` = the ATM IV of the horizon expiry (fallback `iv30`, then HV20) and `T_h` = horizon DTE / 365 | profit | risk-neutral, as the module's docstring says (`black_scholes.py` docstring) — a pricing convention, not a forecast |

`pop_model` (the C3.8 `pop_model` figure) is computed for every family and shown as the
secondary line "model estimate {m}%" so a credit spread's delta figure and the model's
figure sit side by side. The three earlier per-family formulas (`prob_itm` at the
breakeven, the breakeven mass with `sigma = iv30`, the condor's two-sided delta) are
retired in favour of this one call for every non-credit family; the credit families keep
the delta form because it is the number the playbook and `bull_put` already quote.

The sentences (`option_words.pop_words`, verbatim — the honesty rule of the brief):

- `keep` → "About a {p}% chance of keeping the credit - an estimate from today's option
  prices (the short strike's delta), not a promise. Earnings, news and gaps are not in that
  number."
- `profit` → "About a {p}% chance of profit if held to expiry, at today's volatility; this
  trade is managed by the chart stop and target, so the real odds depend on the move, not
  this number."
- Telegram (B7.4): "about {p}% chance of keeping it (estimate)" / "about {p}% chance of
  profit (estimate)".

LEAPS additionally show `delta` as "behaves like N shares" because the LEAPS thesis is
the trend, not a one-expiry bet.

#### B4.7 Greeks in words and the `why` list

`strike_picker.words(pick, chart) -> dict` renders every number a member sees:

| key | template | example |
|---|---|---|
| `delta` (credit) | "delta {d:.2f} ≈ a 1-in-{n} chance {sym} is {below|above} {strike:g} at expiry" with `n = round(1/d)`; "below" for a sold put, "above" for a sold call | "delta 0.25 ≈ a 1-in-4 chance LRCX is below 330 at expiry" |
| `delta` (debit) | "delta {d:.2f}: moves about {d*100:.0f} cents for every $1 the stock moves (like {d*100:.0f} shares)" | "delta 0.63: moves about 63 cents per $1 (like 63 shares)" |
| `theta` | sellers: "earns about ${t:.0f} a day while nothing happens"; buyers: "loses about ${t:.0f} a day ({pct:.1f}% of what you paid)" | "earns about $2 a day" / "loses about $22 a day (0.8%)" |
| `vega` | "a 1-point rise in IV {costs|gains} you about ${v:.0f}" | "a 1-point IV rise costs you about $5" (credit spread, short vega) |
| `pop` | `option_words.pop_words(pop, pop_kind)` (B4.6) + "model estimate {m}%" | |
| `collect` / `pay` | "you collect ${worst:,.0f}–${mid:,.0f} (worst likely fill to mid)" / "you pay ${mid:,.0f}–${worst:,.0f}" per contract — the mid is what POP and max loss are quoted at; the worst likely fill is the natural price (B4.4) | "you collect $180–$210 (worst likely fill to mid)" |
| `risk` | "you risk ${m:,.0f}" (max loss) + ", but the chart stop at {stop:g} would lose about ${l:,.0f}" (from `chart_stop_pl`) | "you risk $790, but the chart stop at 336.2 would lose about $121" |
| `sizing` (read time, B5.3) | "{n} contracts: about ${loss_at_stop} if the stop fires, up to ${max_loss_total} ({pct}% of your account) if the stock gaps past it" — ALWAYS both figures | "2 contracts: about $242 if the stop fires, up to $1,580 (1.6% of your account) if the stock gaps past it" |

Gamma is not put into words for a member; it stays a column behind the full-chain
expander. `why` per pick, from the top-3 set (the `rank_pairs` vocabulary extended): "best
fit to your rules" (rank 1), "most credit per $ risked", "highest chance", "most room below
the price" / "most room above", "smallest max loss", "cheapest per delta", "least time
value", "bid/ask at the limit", "open interest unknown — check in TWS", "barely traded
today".

#### B4.8 Output shape, degenerate cases and the three basket states

```python
PickResult = {
  "strategy": "bull_put", "family": "credit_vertical", "prefs_hash": "9f1c2a7b4d0e",
  "status": "ok" | "degenerate",
  "picks": [Pick, Pick, Pick],                        # top 3 by score; picks[0].recommended = True; each with words, why, chart_stop_pl, rule_stop_pl
  "considered": 23,                                  # candidates before constraints / liquidity
  "degenerate": None | {"reason_key": ..., "text": ..., "nearest": Pick | None, "fix": "..."},
  "rules_line": "delta 0.20–0.30 · 30–60 days · width 0.5–1.5 ATR ($6–17) · credit ≥ 25% of the risk · under support 340.9 + trend line",
}
```

The signal stores `picks = {strategy_key: [Pick]}` (contract SIGNAL) — `result.picks`
when there are any, otherwise ONE Pick-shaped stub carrying the degenerate case:
`nearest` with `status = "nearest"` (greyed on the card with the one number that failed),
or `{status: "none", legs: [], ...}` when there is no nearest. `rules_line`, `considered`
and `degenerate` are repeated on every Pick of a strategy (cheap, and the list is the
only thing stored). Picks are computed for every rule in `recommended` / `also_fits`
whose `step <= CURRENT_STEP`, for every DISTINCT saved `prefs_hash`
(`option_prefs.distinct_hashes(db)`, B3.5), so the basket's idea cell has exactly **three
states**, `pick_state ∈ {has_picks, no_strike_passes, not_checked}`, all decided by
`option_store.basket_rows_for(db, user) -> dict[symbol, dict]` (ONE batched query keyed by
symbol, Part A5.3) from stored rows — never recomputed in a route: **`has_picks`** (a Pick
with `status == "ok"` under the member's hash), **`no_strike_passes`** (a row exists for
the hash and every Pick is a stub — "no strike passes your rules today", computed, true),
**`not_checked`** (no row for this hash yet — a grey dot, title "open the card to check
your rules"; opening the card computes it). The second and third are never confused: a
member is never told their rules fail when they were simply not run.

| `reason_key` | when | card text (`text`) | `fix` (what the member can change) |
|---|---|---|---|
| `no_chain` | snapshot missing / stale beyond Part A's limit | "No option data for LRCX (as of —). Press Refresh." | — |
| `no_expiry` | window empty after earnings / monthly filters | "Every 30–60 day expiry has earnings Oct 22 inside it." | "wait until after earnings, or allow defined-risk trades through earnings in My rules → Shared" |
| `no_band` | no strike in the delta band in any admitted expiry | "No strike sits in your delta 0.26–0.30 band; nearest: 330P delta 0.25 and 335P delta 0.31." (the two `ALT_NEAR_SHORTS` shown, greyed, not recommended) | "widen the band" |
| `constraint` | the chart constraint removed every in-band candidate | "No strike in your band (0.32–0.40) sits under 336.2 (support 339.1 less 0.25 ATR): the nearest under it is 330 (delta 0.25, under your band) — the market is paying you to sell closer than the chart allows." | "widen the band downward, or switch off the chart rule (not recommended — the drawer shows the consequence sentence before Save)" |
| `credit_floor` | every pair pays under `credit_pct_min` | "The best pair pays 18% of what it risks; your minimum is 25%." | "lower the minimum, or wait for IV to rise" |
| `thin` | liquidity removed everything | "The strikes under your rules are too thin (open interest under 500)." | "lower the OI floor, or pick a more liquid name" |
| `theta_cap` (long) | every in-band leg decays faster than `theta_pct_max` | "Every 45–90 day call in your band loses more than 1% a day." | "go further out in time, or raise the ceiling" |
| `extrinsic_cap` (leaps) | no strike under the time-value cap | "No 9–18 month call is deep enough: the least time value is 12% of the share price (cap 10%)." | "raise the cap, or wait for IV to fall" |
| `no_term` (calendar) | no front×back pair with front IV ≥ back IV | "The near-term month is cheaper than the later one in every pair — no calendar edge today." | — |
| `safety` (diagonal) | no long/short pair passes the width + credit ≥ long cost rule | "No short call under resistance 371.5 covers the long call's cost if the stock rips." | "a nearer resistance, or a lower long delta" |

The card always shows `rules_line`, the `considered` count and, for every degenerate reason
except `no_chain`, the `nearest` candidate greyed with the one number that failed — so "no
trade today" reads as a decision, not a blank.

### B5. Sizing — contracts from risk % of NLV at the CHART stop, capped by the gap rule

`option_sizing.size(pick, nlv, prefs) -> dict`. Pure; the ONE sizing function on the
platform, run at **READ time** (microseconds over a stored pick) by the card, the picks
partial, the ticket and the Live path — never by the nightly job, never by a template, and
Part D never recomputes a quantity. The loss at the chart stop is computed once, at write
time, into `pick.chart_stop_pl` (B5.1); `size()` only divides.

#### B5.1 The loss at the chart stop (write time → `chart_stop_pl`, `rule_stop_pl`)

Part C's `payoff.pnl(legs, S, days_ahead, today, iv_bump=0.0)` prices the position at
stock price `S` with `days_ahead` elapsed since now: each leg's remaining life is
`(leg_dte - days_ahead)/365`; a leg at or past its expiry is intrinsic; otherwise
`black_scholes.black_scholes(S, K, T_left, RISK_FREE, sigma=leg.iv x (1+iv_bump), kind).price`
(`black_scholes.py:43`, Part C3.2 `leg_value`) signed by `Leg.from_dict`'s quantity and
multiplied by `MULT`. `iv_bump` is the argument this part adds to `pnl / leg_value /
curve_at` (default 0.0, so the chart is unchanged). **`iv_bump` is a RELATIVE lift —
`sigma_used = leg.iv × (1 + iv_bump)`, defined ONCE in `payoff.leg_value` — so
`STOP_IV_BUMP = 0.10` turns the fixture's leg IV 0.46 into the `stop_iv` 0.506 (= 0.46 ×
1.10) that B5.3 reports.**

```
loss_at_stop (per contract $) = max over d in STOP_TIMES x front_dte:   -pnl(legs, S=chart_stop, days_ahead=d, iv_bump=STOP_IV_BUMP)
pick.chart_stop_pl = -max(0, loss_at_stop)          # P/L sign: negative = a loss; 0 when the stop cannot lose on the model
```

Per family the max picks the honest regime automatically:

| family | at the stop the loss is largest… | STOP_TIMES picks | LRCX 330/320P at 336.2 (IV 46%→50.6%) | ISRG 395/430C Dec at 394.27 (IV 34%→37.4%) |
|---|---|---|---|---|
| credit_vertical / condor | now — the short legs still carry all their extrinsic | d = 0 | $120.7 (d=0) vs $66 (d=24) → **$120.7** (the card says "about -$121") | — |
| long / debit_vertical / leaps | later — the long leg has decayed before the stock gets there | d = DTE/2 | — | $230 (d=0) vs $348 (d=38) → **$348** |
| calendar / diagonal | at the front expiry edge: `d = front_dte` is used instead of DTE/2 | front expiry | | |

The `STOP_IV_BUMP` lifts IV by 10% on the way down for calls AND puts (a stop on a put trade
is a rally, and IV usually falls; the bump still applies — it over-states the loss on the
mirror side by a few dollars and never under-states it, which is the direction a sizing
rule must err).

The rule stop is computed alongside into `pick.rule_stop_pl` (negative $ per contract)
for **EVERY family** — for the ticket, the chart (both stops are drawn and labelled on the
payoff chart for every family, credit and debit alike, Part C3.9) and the monitor:

| family | rule stop | source |
|---|---|---|
| credit_vertical | `loss_fraction x max_loss` = 20% of max loss (the member's `trade_prefs.read()["loss_fraction"]`, default `bull_put.LOSS_STOP_FRACTION`, `bull_put.py:616`) | `trade_prefs` |
| condor | `loss_fraction x max_loss` — the same credit-family line (20% of max loss); the condor has no loss field of its own (B4.1) | `trade_prefs` |
| long | `premium_stop_pct/100 x premium_usd` (long block, 50) | prefs |
| debit_vertical | `premium_stop_pct/100 x debit_usd` (the long block's field, shared) | prefs |
| leaps | `premium_stop_pct/100 x premium_usd` (leaps block, **40**) — a $ line now exists; the weekly trend is the thesis stop on top of it (B7.3) | prefs |
| calendar | `premium_stop_pct/100 x net_debit_usd` (the long block's field, 50) | prefs |
| diagonal | `premium_stop_pct/100 x net_debit_usd` (the leaps block's field, **40** — the diagonal's long leg inherits the leaps figure) | prefs |

`fires_first` = whichever of `|chart_stop_pl|` and `|rule_stop_pl|` is smaller — the ticket
says "the chart stop (336.2) fires first: ≈ $121 against the rule stop's $158" or the reverse.

#### B5.2 Contracts

```
risk_budget      = nlv x risk_pct / 100                                   # trade_prefs.size: "what the account is willing to lose" (trade_prefs.py:165)
loss_at_stop_usd = max(0, -pick.chart_stop_pl)
max_loss_usd     = pick.max_loss                                          # positive, per contract
notional_usd     = width x 100 for a credit family (the collateral the broker holds for a short vertical / the condor's wider wing),
                   max_loss_usd for every other family                     # every strategy in the catalog is defined-risk: for a debit the capital a contract can lose IS the capital it ties up
GAP_MULT         = prefs["shared"]["gap_mult"]                            # house 2.0 (B4.1)

by_chart_stop = floor(risk_budget / loss_at_stop_usd)                     # the sizing rule (decision in the brief); None when loss_at_stop_usd == 0
by_gap        = floor(risk_budget x GAP_MULT / max_loss_usd)              # a gap through the stop may cost GAP_MULT x the budget, never seven times
by_notional   = floor(nlv x MAX_POSITION_PCT/100 / notional_usd)          # the 10% cap (CLAUDE.md), a constant, never a member field
contracts     = min(x for x in (by_chart_stop, by_gap, by_notional) if x is not None)   # 0 is a valid answer
```

`floor`, never round up (`trade_prefs.size`: "rounding UP would spend more of the risk
budget than the member allowed", `trade_prefs.py:144-146`), and **never forced up to one**:
when one contract exceeds the budget the answer is 0 with the note "Not even one contract
fits your 1% - lower the risk or choose a narrower spread". Why the gap cap exists: the
chart-stop rule alone let a "1% risk" LRCX trade carry 8 contracts and a worst case of
$6,320 (6.3% of the account) while the card still said "risk 1%"; with `GAP_MULT` 2.0 the
same trade is 2 contracts, about $242 at the stop and at most $1,580 (1.6%) through a gap. The
`by_chart_stop` cap keeps the stop honest; the gap cap keeps the account honest. When
`loss_at_stop_usd == 0` (a degenerate `stop == entry`), `by_chart_stop` is None and the
other two size it, with the note "the chart stop loses nothing on the model; sized by the
gap and 10% caps".

**The card's sizing line ALWAYS shows both figures** (B4.7 `words.sizing`): "{n}
contracts: about ${loss_at_stop} if the stop fires, up to ${max_loss_total} ({pct}% of
your account) if the stock gaps past it". The $ figures in the line are rounded UP to the
dollar (a stated loss is never understated: 2 × 120.7 = 241.4 prints as "$242") and the
percentage to one decimal. The Telegram push never states a contract count (B7.4).

#### B5.3 Output and NLV source

```python
{"nlv": 100000.0, "nlv_source": "live" | "prefs" | None,
 "risk_pct": 1.0, "risk_budget": 1000.0, "gap_mult": 2.0,
 "loss_at_stop_usd": 120.7, "stop_price": 336.2, "stop_t_days": 0, "stop_iv": 0.506,     # stop_iv = leg iv 0.46 x (1 + STOP_IV_BUMP)
 "rule_stop_usd": 158.0, "rule_stop_kind": "20% of max loss", "fires_first": "chart",
 "by_chart_stop": 8, "by_gap": 2, "by_notional": 10, "contracts": 2,                   # floor(1000/120.7) / floor(2000/790) / floor(10000/1000)
 "capital_at_risk_usd": 241.4,          # contracts x loss_at_stop_usd
 "max_loss_total_usd": 1580.0,          # contracts x max_loss_usd
 "max_loss_pct_nlv": 1.6,
 "line": "2 contracts: about $242 if the stop fires, up to $1,580 (1.6% of your account) if the stock gaps past it",
 "note": None | "sized once you tell us the account value (My rules -> Shared)" | "Not even one contract fits your 1% - lower the risk or choose a narrower spread" }
```

This is the golden LRCX sizing (`tests/fixtures/options/lrcx.json`, B8.1) under house
prefs: NLV 100,000, risk 1%, `gap_mult` 2.0, `MAX_POSITION_PCT` 10.

NLV resolution, in order (contract SIZING): (1) the **Live figure for this request** —
`POST /options/live/{symbol}`'s payload carries `nlv` from the bridge's `/account`
(`bridge/ibkr_bridge.py:604-614` returns `net_liquidation`; the legacy `routes/options.analyze`
already accepts `payload["nlv"]` + `nlv_source`, `routes/options.py:113-124`) — used for
that request only and labelled "from TWS · [remember this]"; (2) the stored
`trade_prefs.read(user)["nlv"]` when > 0 ("your stored account value"); (3) None →
`contracts` None and the first note above. **Nothing is ever auto-written**: only an
explicit "[remember this]" click writes the figure through `trade_prefs.write(db, user,
nlv=...)`; a Live press alone stores no balance (a broker balance written without a click
is a consent problem on a shared platform).

Because sizing runs at read time, the stored picks never contain a contract count: the
first paint sizes from (2), a Live press re-sizes in-request from (1) over the SAME stored
picks (the live chain re-grades only the live expiry, B6.1 header), and Telegram sends no
count at all.

### B6. The order ticket

`order_ticket.build(pick, setup, prefs, *, dip=False, rejection=None, now=None) -> Ticket`
and `order_ticket.render(ticket, broker: "tws" | "moomoo") -> str`. The three positional
arguments are the contract's; `pick` carries its read-time `sizing` (B5), `setup` is the
signal's stored setup (B2.7), `dip` is the explicit "Enter on the dip" toggle and
`rejection` the recommender's sentence when the strategy was rejected (B6.4). The ticket is
what the card hands over (design §3: "a ready order ticket ... to paste into the broker");
the platform places nothing (DESIGN.md security posture; `routes/options.py:17-18, 151-154`).
Part D's `_options_ticket.html` (served by `GET /options/ticket/{symbol}`) prints BOTH
renderings in `<pre>` blocks plus its footer lines ("Tracking only: TradeHunter never sends
an order.", the data-age line); it holds no ticket text of its own.

#### B6.1 Structure

```python
Ticket = {
  "symbol": "LRCX", "strategy": "bull_put", "label": "Bull put spread",
  "side_line": "(you are paid; the most you can lose is fixed)",   # credit families; debit families: "(you pay; the most you can lose is what you paid)" — printed after the label on line 2 of both renderings
  "contracts": 2,                                                   # pick.sizing.contracts; 0 / None -> the quantity prints as "—" with the sizing note
  "header": "Prices are from 02 Oct 16:00 ET. Press Refresh after 21:30 Malaysia time (US open) and re-open the ticket before sending; the credit will have moved.",
  "refresh_first": False,       # True when as_of is older than the last session close AND clock._us_session_open() -> the template renders a 'Refresh first' banner with the Refresh button inline; the 60 s cooldown never blocks that first refresh
  "rejection": None | "Not recommended today: earnings Oct 22 sit inside every 30–60 day expiry.",   # B6.4: passed in as rejection=, the FIRST line of both renderings, and the tracked trade's note
  "legs": [  # OCC-style, one row per leg, qty already multiplied by contracts — the golden 330/320 (B8.1)
    {"action": "SELL", "qty": 2, "right": "P", "strike": 330.0, "expiry": "2026-11-20", "ref_mid": 9.60, "ref_bid": 9.45, "ref_ask": 9.75},
    {"action": "BUY",  "qty": 2, "right": "P", "strike": 320.0, "expiry": "2026-11-20", "ref_mid": 7.50, "ref_bid": 7.35, "ref_ask": 7.65},
  ],
  "net": {"kind": "credit", "limit": 2.10, "floor": 2.00,          # limit = mid-mid; floor = the worst net still inside credit_pct_min (25% of what you risk: c >= 0.25 x (10 - c) -> 2.00 on a $10 width) — for a debit: the worst debit still inside reward_cost_min
          "per_contract_usd": 210.0, "total_usd": 420.0,
          "work": "enter at the mid (2.10); if unfilled in a few minutes step down 0.05 at a time, never below 2.00 (never below $200 a contract)"},
  "condition": None,            # the DEFAULT for every family: the entry is a plain day limit order placed while the market is open
                                # dip=True (the explicit toggle) -> {"on": "LRCX", "field": "last", "op": "<=", "value": 341.92,
                                #    "why": "the close (349.20) has run 2.4% past the level: enter on the dip to 0.3% above support 340.9 rather than chase",
                                #    "warning": "This order will also fire if LRCX crashes through 341.92 on bad news. Only use it while you are watching."}
  "tif": "DAY",                 # entry orders are DAY; the exit orders below are GTC
  "stop":   {"chart": {"level": 336.2, "loss_usd": 241.4, "per_contract_usd": 120.7, "gap_usd": 1580.0,   # printed "about $242" / "up to $1,580" (B5.2 rounding)
                       "trigger": "LRCX last <= 336.20", "trigger_outside_rth": False,             # 'Trigger outside RTH: No' in both renderings
                       "fill": "market",                                                            # recommended; limit is the secondary option
                       "close_at": 3.31,                                                            # the model's spread mark at the stop (d=0, IV x 1.1) — the secondary limit
                       "rth_note": "This order fires on the live price during regular hours, so it can fire on an intraday dip the close would have survived. If you prefer the close-based rule, leave this order off and act on the Positions tab's verdict instead."},
             "rule":  {"kind": "20% of max loss", "loss_usd": 316.0, "per_contract_usd": 158.0, "close_at": 3.68}},   # close_at = credit + 20% of (width - credit) = 2.10 + 1.58; no order - the Positions tab watches it
  "target": {"chart": None,                                         # credit trades have no chart target
             "rule":  {"kind": "50% of the credit", "close_at": 1.05, "profit_usd": 210.0}},
  "exits_text": "Close if LRCX closes under 336.20 (the 340 support failed) — or if the spread is marked at 3.68 (20% of max loss). Take profit by buying it back at 1.05 (half the credit). Close or roll with 21 days left (Oct 30) whatever the P/L.",
  "rationale": "...the why sentence (B3.2)...", "must_happen": "...",
  "warnings": ["open interest unknown on 315P — check the OI column in TWS before ordering", "earnings Oct 22 are inside this trade — allowed by your rules; the stop is the only protection"],
  "as_of": "2026-10-02T16:00:00-04:00", "source": "cboe" | "live",
}
```

**The entry carries no condition by default.** A conditional entry placed by an absent
member fires on ANY fall through its price — including a gap straight through support on
bad news — and the broker allows one condition per order, so it cannot be bounded from
below. The default for every family is therefore a plain limit order the member places
during the session (the header says when). "Enter on the dip" is an explicit toggle on the
ticket (`dip=True`), rendered WITH its warning sentence, and only for the cases where a
dip level exists:

| strategy / setup | dip condition (only when the toggle is on) | why |
|---|---|---|
| credit spread after a `support_bounce` whose close is more than `FRESH_MAX` (0.5%, `ema_setup.py:46`) above the level | `last <= level x (1 + offset_pct/100)` | the Curated habit: enter near the level, not after the bounce has run; the credit is larger there |
| credit spread on a `fresh` bounce / rebound (close within 0.5% of the level) | None — the price IS at the level (the toggle is disabled) | |
| debit / long after a bounce | same as credit: `last <= entry` when the close has run; None when fresh | |
| `breakout_retest` | `last >= zone_hi + LEVEL_PAD_ATR x ATR` | enter when the retest holds and price turns up, not while it is still testing |
| `failed_support` (bear trades) | `last <= broke_level - LEVEL_PAD_ATR x ATR` | confirm the break |
| condor / calendar / leaps | None (the toggle is not offered) | |

**The ONE conditional order the ticket pushes hard is the chart-stop EXIT** (ORDER 2 /
CHART STOP below): it is the order a member should set up before walking away, and it is
the one whose trigger on the intraday LAST differs from the monitor's daily CLOSE — which
is why its `rth_note` is printed in full on both renderings and `Trigger outside RTH: No`
is set explicitly.

Chart target for debit trades: `target.chart = {"level": plan.target, "close_at":
-pnl(legs, S=target, days_ahead=DTE/2) / MULT + net}` — the model's estimate of what the
position is worth if the stock reaches the target around mid-life, so the member has a
limit price to place, not just a stock level.

Data age: `header` always states `as_of` in ET and the "Press Refresh after 21:30 Malaysia
time" instruction (the nightly job runs at 07:15 MYT, fourteen hours before a member can
act; the credit WILL have moved). `refresh_first` is computed by the ticket route from
`as_of` vs the last session close (`spread_monitor.et_today()` arithmetic) and
`clock._us_session_open()` (`app/services/clock.py`: weekdays 09:30–16:00 ET, excluding
the NYSE holidays the calendar service already knows, with a static list as the fallback);
when True the template shows the banner above the text and the
`POST /options/refresh/{symbol}` button inline, exempt from the 60 s cooldown for that
first press.

#### B6.2 Rendering — TWS (conditional orders on the underlying)

The mechanics the user and I settled on earlier in this project: TWS attaches a
**condition** to an order from the order ticket's Conditional tab — a *Price* condition
on another contract (here the underlying stock), *Trigger method* Last, operator ≤ / ≥, and
the order transmits only when the condition is true. **One condition set per order**, so
the ticket renders three orders: the entry, the chart-stop exit, the profit exit.

This is THE golden TWS text (the one `tests/fixtures/options/` holds; Part D prints it
verbatim and keeps no second version):

```
Prices are from 02 Oct 16:00 ET. Press Refresh after 21:30 Malaysia time (US open) and re-open the ticket before sending; the credit will have moved.
LRCX — Bull put spread (you are paid; the most you can lose is fixed) — 2 contracts — paste into TWS
ORDER 1 · ENTRY (combo, DAY, no condition — place it while the market is open)
  Strategy Builder → Vertical: SELL 2 LRCX 20 NOV 26 330 P / BUY 2 LRCX 20 NOV 26 320 P
  Limit CREDIT 2.10 (mid). Work it: if not filled in a few minutes, lower 0.05 at a time, never below 2.00.
  [Only if 'Enter on the dip' is switched on] Conditional tab → Add → Price → LRCX (STK, SMART) → Last ≤ 341.92 → submit.
    This order will also fire if LRCX crashes through 341.92 on bad news. Only use it while you are watching.
ORDER 2 · CHART STOP (combo, GTC, conditional) — the one order to set up before you walk away
  BUY 2 LRCX 20 NOV 26 330 P / SELL 2 LRCX 20 NOV 26 320 P (close the spread)
  Type: Market (recommended). Limit DEBIT 3.31 (the model's mark at the stop) is the second choice — it may not fill in a fast market.
  Conditional tab → Add → Price → LRCX (STK, SMART) → Last ≤ 336.20 → Trigger outside RTH: No → transmit when true
  This order fires on the live price during regular hours, so it can fire on an intraday dip the close would have survived. If you prefer the close-based rule, leave this order off and act on the Positions tab's verdict instead.
  (you would lose about $242 here; up to $1,580 if the stock gaps past it)
ORDER 3 · TAKE PROFIT (combo, GTC)
  BUY 2 LRCX 20 NOV 26 330 P / SELL 2 LRCX 20 NOV 26 320 P — Limit DEBIT 1.05 (half the credit kept). No condition.
Rule stop (no order - the Positions tab watches it): if the spread is marked at 3.68 or more (20% of max loss), close it.
Time stop (no order - the Positions tab watches it): close or roll with 21 days left (Oct 30), whatever the P/L.
```

For a two-expiry strategy the legs are listed per expiry; Strategy Builder's "Calendar" /
"Diagonal" presets are named. For a single long option the "combo" wording is dropped. If
a combo cannot be made conditional on the member's TWS version, the stop falls back to
the moomoo ordering below (short leg first, then the long leg) with the same naked-leg
sentence.

#### B6.3 Rendering — moomoo (price condition on another symbol)

moomoo's conditional order (Trade → Conditional Order → *Price condition*) lets the
trigger watch a **different symbol** from the one being traded — the underlying for an
option order — with a ≥ / ≤ price and then submits an order. Also **one condition per
order**, and moomoo places multi-leg strategies as separate legs or via its Options
Strategy ticket (no condition on the strategy ticket), so the ticket renders the entry as
the strategy ticket WITHOUT a condition (the default), the dip condition per leg only when
the toggle is on, and the chart stop as **two orders in a fixed order** — the short leg is
bought back FIRST, at market (or a limit of the model value × 1.15 so it fills in a fast
market), and only then is the long leg sold. A partial fill that leaves the long leg sold
and the short leg open would be a naked short option, which is the one thing a
defined-risk member must never hold:

This is THE golden moomoo text (the one `tests/fixtures/options/` holds):

```
Prices are from 02 Oct 16:00 ET. Press Refresh after 21:30 Malaysia time (US open) and re-open the ticket before sending; the credit will have moved.
LRCX — Bull put spread (you are paid; the most you can lose is fixed) — 2 contracts — paste into moomoo
ENTRY (today, no condition — place it while the market is open)
  Options → Strategy → Bull Put Spread: sell LRCX 2026/11/20 330 Put, buy LRCX 2026/11/20 320 Put, qty 2, limit net credit 2.10 (never below 2.00). DAY.
  [Only if 'Enter on the dip' is switched on] Conditional Order → Price condition → symbol LRCX → last ≤ 341.92 → then the same strategy ticket.
    This order will also fire if LRCX crashes through 341.92 on bad news. Only use it while you are watching.
CHART STOP (GTC, conditional) — two orders, in this order
  1. Conditional Order → Price condition → monitor symbol LRCX → trigger when last ≤ 336.20 → Trigger outside RTH: No
     → order: BUY TO CLOSE 330 Put, qty 2 — Market, or limit 17.14 (the model's 14.90 × 1.15)
  2. Then SELL TO CLOSE 320 Put, qty 2 — on the same trigger, or by hand once order 1 has filled
  Never sell the long leg before the short leg is closed — you would be short a naked put.
  This order fires on the live price during regular hours, so it can fire on an intraday dip the close would have survived. If you prefer the close-based rule, leave this order off and act on the Positions tab's verdict instead.
  (you would lose about $242 here; up to $1,580 if the stock gaps past it)
TAKE PROFIT (GTC)
  Strategy ticket: close the spread at net debit 1.05. No condition.
Rule stop (no order - the Positions tab watches it): close if the spread is marked at 3.68 or more (20% of max loss).
Time stop (no order - the Positions tab watches it): close or roll with 21 days left (Oct 30), whatever the P/L.
```

The naked-leg sentence reads "naked put" for a put spread and "naked call" for a call
spread; for a condor it is printed once per side. The limit on order 1 = the model's
short-leg value at the stop price (`payoff.leg_value` at S = stop, d = 0, IV bumped)
× 1.15, rounded to the tick, with the note "an estimate — at the trigger the market's own
price fills first".

#### B6.4 A rejected or unbuilt strategy

The chip row shows rejected strategies with their reason (decision 9); what a member can
DO with one is fixed:

| the shown strategy is… | strike table | button | ticket |
|---|---|---|---|
| rejected with `reason_key = earnings_inside` | **hidden entirely** — no "see what it would cost" (earnings are the one thing a stop cannot protect against, design §5.1). Exception: `shared.earnings_rule = defined_risk_only` AND the strategy is defined-risk → it is not rejected in the first place (B3.4) | none | `build()` raises `TicketRefused` and `GET /options/ticket/{symbol}` answers with the sentence, never a ticket. Test: a rejected-for-earnings strategy produces no ticket |
| rejected for any other key | shown (the member may want to see what the market offers) | "Order ticket (not recommended)" in the ghost style, never the primary style | built with `rejection=<the recommender's sentence>`: it is the FIRST line of both renderings, and `POST /options/track-idea` writes the same sentence into `option_trades.note` |
| `also_fits` with `reason_key = not_available_yet` (an unbuilt rule) | none — no picks exist | none; the chip reads "{label} · not available yet" | none |

The words "step", "phase" or a build number never appear on a ticket, a chip or a
headline.

### B7. Exits per family — the Positions monitor

#### B7.1 Storage: `option_trades` (generic legs) and `option_trade_checks` — THE positions store from step 1

`OptionSpread` (`models.py:788-849`) holds exactly two put legs, and `spread_monitor.snapshot`
is hard-wired to puts (`spread_monitor.py:109-110`) — so even a step-1 bear call cannot live
there. From step 1 **every** tracked position, for every strategy, lives in `option_trades`
and is graded by `option_exits`; `option_spreads` stays read-only for the legacy
`/portfolio` until that page is removed (decision 5) and `spread_monitor` is not changed.
The new table holds any legs and keeps the per-trade override convention (NULL = the
member's default):

```python
class OptionTrade(Base):
    __tablename__ = "option_trades"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    symbol = Column(String(20), nullable=False, index=True)
    strategy = Column(String(24), nullable=False)         # the StrategyRule.key
    family = Column(String(24), nullable=False)           # credit_vertical | debit_vertical | long | leaps | condor | time
    legs = Column(JSON, nullable=False)                   # the stored leg shape (B0.2) + entry_price, entry_delta, entry_iv per leg: [{expiry, right, strike, side, qty, price, bid, ask, iv, delta, oi, volume, entry_price, entry_delta, entry_iv}]
    front_expiry = Column(String(10), nullable=False, index=True)   # earliest leg expiry (DTE grading)
    back_expiry = Column(String(10), nullable=True)                 # latest leg expiry when they differ
    net_entry = Column(Float, nullable=False)             # per share: negative = credit received, positive = debit paid
    contracts = Column(Integer, nullable=False, default=1)
    max_loss = Column(Float, nullable=True)               # per contract $, positive; None for a long-only trade = premium
    chart_stop = Column(Float, nullable=True)             # stock level (plan.stop)
    chart_target = Column(Float, nullable=True)           # stock level (debit trades)
    roll_dte = Column(Integer, nullable=True)             # LEAPS / diagonal long leg roll date
    paper = Column(Boolean, nullable=False, default=False)    # the auto-tracked system idea (design §9, item 5)
    signal_id = Column(Integer, nullable=True)            # the option_signal row it came from (Part A), for the track record
    earnings_date_at_entry = Column(String(10), nullable=True)   # what was known when it was opened (B7.3's earnings row compares against it)
    roll_delta = Column(Float, nullable=True); loss_stop_pct = Column(Float, nullable=True)
    profit_target_pct = Column(Float, nullable=True); dte_floor = Column(Integer, nullable=True)   # the OptionSpread override set, same NULL convention (models.py:819-831)
    meta = Column(JSON, nullable=True)                    # portable JSON written at entry: whatever the exit rules need later — {"breakevens": [be_lo, be_hi]} for a calendar, {"range": {"low", "high"}} for a condor (B7.3); the migration creates it
    opened_at = Column(DateTime, default=_utcnow); status = Column(String(12), nullable=False, default="open")
    closed_at = Column(DateTime, nullable=True); close_reason = Column(String(24), nullable=True); note = Column(Text, nullable=True)   # note carries B6.4's rejection sentence when the idea was tracked against the recommender
    checks = relationship("OptionTradeCheck", back_populates="trade", cascade="all, delete-orphan", order_by="OptionTradeCheck.checked_on")

class OptionTradeCheck(Base):          # SpreadCheck (models.py:852-901) generalised: one row per (trade, ET day), upsert like spread_monitor.record_check
    __tablename__ = "option_trade_checks"
    __table_args__ = (UniqueConstraint("trade_id", "checked_on", name="uq_option_trade_check_day"),)
    id, trade_id (FK option_trades CASCADE, index), checked_on (String(10), index)
    spot, mark (Float: per-share value, cost to close), pl (Float $), loss_pct, profit_pct, dte (front), back_dte
    net_delta (shares), theta ($/day), vega ($/vol pt), legs = Column(JSON)    # per-leg {mid, delta, iv} as seen that day
    state (String(10): OK|WATCH|ROLL|CLOSE|TAKE|UNKNOWN|EXPIRED), action (Text), reasons = Column(JSON), urgent (Boolean)
    source (String(12), default "cboe"), error (Text), created_at
```

Both tables are created by the ONE migration `alembic/versions/f4a5b6c7d8e9_options_module.py`
(`down_revision = "e2f3a4b5c6d7"`, Part A's skeleton with the per-table
`if table in inspector.get_table_names(): return` guard the existing migrations use,
`e2f3a4b5c6d7_iv_scan_items.py:20-21`) alongside Part A's and Part D's seven tables; this
part contributes its **data step**, which **copies every OPEN `option_spreads` row into
`option_trades`** (`strategy="bull_put"`, `family="credit_vertical"`, two legs from
`short_strike`/`long_strike`/`short_price`/`long_price` with `entry_price` set,
`net_entry = -credit`, overrides carried over, `note="migrated from option_spreads #<id>"`).
`option_spreads` is left in place; the new Positions tab (`GET /options/positions`, Part
D5.1) renders `option_trades` rows with an `OptionTradeCheck` drawer per trade and a
`POST /options/positions/{id}/close` action (sets `status`, `closed_at`, `close_reason`);
`POST /options/track-idea` (its own path — the legacy `POST /options/track` stays
untouched for the Watchlist tab) creates the row from a Pick; `GET /options/badge` reads
`option_trade_checks` for its `urgent` / `watch` counts.

#### B7.2 Marking a trade

`option_exits.mark(trade, chain_view, today) -> snap`: per leg `option_quotes.leg(chain,
expiry, right, strike)` (`option_quotes.py:184-188`) → `norm_leg` with the chain's unit —
the RIGHT comes from the stored leg, so a bear call marks against the call chain; `mark =
Σ side × qty × price` (cost to close per share, the `spread_monitor` convention "mid is
what a broker marks at", `spread_monitor.py:17-24`); `pl = (-(net_entry) - mark) x 100 x
contracts` for credits is the existing `(credit - mark)`, and for debits `(mark - debit)`;
both are the one expression `pl = (mark_position_now - value_at_entry) x 100 x contracts`
where `value_at_entry = net_entry` and `mark_position_now = -mark` — one formula, sign
carried by `net_entry`. `mark_worst` pays every ask / hits every bid, as today. Position
greeks: `net_delta = Σ side x qty x delta x 100 x contracts`, `theta`, `vega` likewise
(`spread_monitor.py:134-143`). A missing leg → `error` naming it, with the listed-expiry
hint (`spread_monitor.py:111-126`); the geometry still renders. The per-leg `{mid, delta,
iv}` seen that day is written to `OptionTradeCheck.legs`.

#### B7.3 The rule table the monitor evaluates

`option_exits.grade(trade, snap, chart, prefs, *, earnings=None) -> verdict` with the
`bull_put.monitor` contract (`{state, action, reasons, urgent, *_breach, loss_pct,
profit_pct}`, `bull_put.py:673-675`) plus `stop_breach` / `target_breach` / `roll_breach` /
`earnings_breach`. Losing-side lines win ties (`bull_put.py:669-671`); the first matching
row in each family block decides the state; `WATCH` fires at 80% of any losing line, 50%
of the loss line and within 3 days of a time line (the near-miss rule,
`bull_put.py:739-754`).

**One row applies to every family, first in every block — earnings that appeared or
moved after entry.** The recommender checks earnings only at recommendation time (B3.4);
a date that was unknown or later when the trade was opened and now falls inside it is the
one thing the stop cannot protect against, so the monitor re-checks it every day:

| family | line | condition | state | action text (template) |
|---|---|---|---|---|
| **every family** | earnings | `earnings.date is not None and earnings.date <= front_expiry` (the SHORT leg's expiry for a diagonal, the BACK expiry for a calendar) and the trade is not allowed through it by the member's `earnings_rule` (B3.4 `allowed`) and `earnings.date != trade.earnings_date_at_entry` or it was None at entry | **WATCH (urgent)** | "Earnings {date} now fall inside this trade (the date was unknown or later when you entered). Decide before the close that day." |
| **credit_vertical** (bull_put / bear_call) | chart stop | underlying CLOSE beyond `chart_stop` (≤ for bull put, ≥ for bear call) | CLOSE | "{sym} closed at {close} — under the {level} support the trade was sold against. Close it; the thesis is gone, whatever the P/L." (mirror: "over the {level} resistance") |
| | delta | `abs(short_delta) >= roll_delta` — the house credit_vertical line is **0.35 adjust / 0.40 close** (`bull_put.DELTA_ADJUST` / `DELTA_CLOSE`, `bull_put.py:69-71`, the playbook's figures); a member's `trade_prefs.roll_delta` of 0.30 is an override example, not the default | ROLL if `dte > ADJUST_DTE` (30) else CLOSE | existing `bull_put.monitor` text (`bull_put.py:765-774`) |
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
| | **loss** | `loss_pct_of_premium >= premium_stop_pct` (leaps block, 40) | CLOSE | "Down {x}% of what you paid: the weekly trend may hold but the position has not." |
| | delta drift | `delta < delta_floor` (0.55) and `dte > roll_dte` | ROLL | "Delta has fallen to {d:.2f} (bought at {d0:.2f}): the call no longer behaves like stock. Roll down to the {new_delta} strike in the same expiry." |
| | delta up | `delta >= 0.90` | TAKE (partial) | "Delta {d:.2f}: the leverage is gone, it is stock now. Roll UP to a {delta_lo}–{delta_hi} strike and take the difference." |
| | roll date | `dte <= roll_dte` (180) | ROLL | "{dte}d left — at your roll date. Roll out to the {months} month expiry while time value is still cheap." |
| **condor** | either side's delta | `abs(delta_short_put) >= roll_delta` or `abs(delta_short_call) >= roll_delta` (0.30) | ROLL (that side) if `dte > ADJUST_DTE` else CLOSE | "The {put/call} side's short delta is {d:.2f}: roll that side {further out/closer}, or close the condor." |
| | range stop | underlying close beyond `rng["low"] - PAD` or `rng["high"] + PAD` (the edges stored with the trade at entry in `trade.meta["range"] = {low, high}`) | CLOSE | "{sym} closed outside the range ({lower}–{upper}). The sideways read is wrong — close." |
| | loss | `loss_pct >= loss_fraction` (20% of max loss — the credit-family line, B5.1; the condor has no loss field of its own) | CLOSE | |
| | profit | `profit_pct >= profit_target` (50% of credit) | TAKE | |
| | time | `dte <= dte_floor` (21) | CLOSE | |
| **calendar** | range stop | underlying close outside the model breakevens at entry (`trade.meta["breakevens"] = [be_lo, be_hi]`, written when the trade is tracked) | CLOSE | "{sym} at {close} is outside {be_lo}–{be_hi}: a calendar only pays when the stock sits still." |
| | profit | `profit_pct_of_debit >= cal_take_pct` (25%) | TAKE | "Up {x}% of the debit — calendars pay in small steps; take it." |
| | roll date | `front_dte <= 5` | ROLL | "Front leg expires in {d}d: buy it back and sell the next cycle at the same strike (or close both)." |
| | loss | `loss_pct_of_debit >= premium_stop_pct` (50%) | CLOSE | |
| **diagonal_call** | short leg ITM | `delta_short >= 0.50` | ROLL (short up and out) | "The short call is in the money (delta {d:.2f}): roll it up and out, keep the long." |
| | short roll date | `short_dte <= 7` | ROLL | "Short call expires in {d}d: let it expire or buy it back, sell next month's under {resistance}." |
| | long leg | the LEAPS rows above apply to the long leg (`roll_dte`, `delta_floor`, trend stop, and the leaps `premium_stop_pct` 40 loss line, inherited) | ROLL / CLOSE | |
| | loss | `loss_pct_of_debit >= premium_stop_pct` (the leaps block's 40 — the same line B5.1's `rule_stop_pl` is drawn at) | CLOSE | |

The nightly `option_exits.sweep(db)` — **step 4** of the nightly order (A4.2: 1 snapshot,
2 metrics / `iv_daily`, 3 engines per hash, **4 `option_exits.sweep(db)`**, 5
`option_store.prune(db, today)`, 6 `telegram_push.run`, 7 `job_runs.finish`) — is
`spread_monitor.sweep`'s shape
(`spread_monitor.py:283-344`) over `option_trades`: one chain fetch per underlying,
per-member prefs, `record_check` upsert per ET day, `actionable` = every `urgent` verdict.
For every underlying that holds a LEAPS or diagonal trade it also calls
`ema_setup.setup_for(sym, deep=True)` once (`ema_setup.py:606`; cached 15 min) so
`w_uptrend` reaches `grade()` — the weekly trend stop needs the deep bars the chain fetch
does not bring; that extra Yahoo call is counted in Part A4.1's budget. It runs inside the
`TST-Options-Nightly` task (07:15 MYT, 30-minute limit) and its per-ticker outcome goes
into the run's `option_jobs.detail`. Urgent verdicts surface in three places: the nav
badge (`GET /options/badge` reads today's `option_trade_checks` → `urgent`, `watch`), the
Positions tab's row verdict, and the existing Discord post `deploy/portfolio_daily_check.py`
already makes for actionable spread verdicts (its `--no-discord` switch and
`app.services.discord` call) — the Telegram push (B7.4) carries ideas, not exit lines.
Paper trades (`paper=True`) are swept identically; their checks build the system's track
record (design §3).

#### B7.4 Telegram push (decision 10) — Part D's design, consumed

There is ONE Telegram implementation on the platform and it is Part D's:
`app/services/telegram.py` (the sender — reuses `scripts._common.telegram_env`,
`scripts/_common.py:562-574`, for the vault token, with a `chat_id` parameter, because
`send_telegram(cfg, html)` at `scripts/_common.py:577` has none and a multi-member platform
needs one; reached through `services/resources_bridge.py`, which already puts the
TradeHunter root on `sys.path` for `resources.patterns`, `structure.py:35`) +
`app/services/telegram_push.py::run(db, as_of=run_on, dry_run=...)` + the table `option_idea_push`.
`option_exits` has no push function of its own; the nightly job's **step 6** calls
`telegram_push.run(db, as_of=run_on, dry_run=...)` after every signal row is written, after
step 4's `option_exits.sweep(db)` (so the push sees tonight's checks) and step 5's
`option_store.prune(db, today)` (`option_chain_snapshot` 90 d, `option_trade_checks` 90 d,
`option_idea_push` 45 d). What this part supplies to it is the content and the guards:

- **Per-member opt-in** (`prefs["telegram"]["enabled"]`, B4.1 — its own key, not a schema
  field) with the chat-id handshake through Part D's `POST /options/telegram` (body `{action
  ∈ {request_code, verify, quiet, pause, disable}, chat_id?, code?, pause_days?}`): the
  member sends `/start` to the bot, the bot replies with a 6-digit code (`pending{chat_id,
  code, expires}`), and the drawer accepts the chat id only together with that code
  (`verify` → `verified`); a mistyped id never receives a stranger's ideas. A `quiet`
  switch, and a "pause for 7 days" link in every message (`pause` → `paused_until`).
- **Dedupe key** `(symbol, strategy, front expiry)` recorded in `option_idea_push`; the
  same thesis is re-pushed only if the short strike moved by more than 1 ATR since the
  last push (a strike that drifts one listed step a day is not a new idea).
- **Skip** — logged, never sent — when `snapshot.partial`, `signal.status != "ok"`,
  `iv.provisional` is True, `iv.earnings_date` is None ("skipped: earnings date unknown"
  in the job log — an unverified date is exactly the gap the push would hide),
  `rule.step > CURRENT_STEP`, or `as_of` is older than the last session.
- **Content**: first line "Ideas for tonight's US session (opens 21:30 Malaysia). Prices
  are last night's close."; then per idea the headline sentence, the "What has to happen"
  line (`must_happen`), the recommended pick's line in collect / risk words with the POP
  as "about {p}% chance of keeping it (estimate)" / "about {p}% chance of profit
  (estimate)", the chart stop with its per-contract cost, the earnings flag, the deep link
  to the card (`settings.public_url + "/options?symbol=SYM"`). **Never a contract count** (sizing is a read-time, per-account number,
  B5). At most 5 ideas per message ordered by `score`, then "and N more on the page".
- Failure = a logged line, never an exception; one member's error never stops the next.

### B8. Worked examples

The two golden fixtures — `tests/fixtures/options/lrcx.json` and `isrg.json` — are
hand-set chains that reproduce every figure in B8.1 / B8.2 exactly (ONE set of numbers,
shared with Parts A, C, D and II); the supporting rows are Black-Scholes at `RISK_FREE`
0.04 on the stated IVs, so they are reproducible too; a live chain will differ by the skew
and the bid/ask. Today = 2026-10-03 (Friday). Listed expiries used: Oct 17 (14 DTE), Oct
31 (28), Nov 20 (48, monthly), Dec 19 (77, monthly), Jan 16 2027 (105). House prefs unless
stated: `gap_mult` 2.0, NLV $100,000, risk 1%, `MAX_POSITION_PCT` 10.

#### B8.1 LRCX — spot 349.20, ATR 11.54, support 340 (zone 339.1–340.9, 3 touches, pin bar on 1.8x volume), IV rank 62, IV30 46%, HV20 38%, earnings Oct 22

**Gauge** (the golden `iv`: IV30 46.0, HV20 38.0, `iv_front` 50.0 (Oct 31), `iv_back` 45.0
(Dec 19), IV rank 62). `iv_n` 252 after the bootstrap → `state ok`, `basis rank`, rank 62 →
measure 62 ≥ 50 → `SELL`; `iv_hv_premium = 46 / 38 = 1.21` (the RATIO) → "priced for 21%
more movement than the stock has actually shown"; `term_ratio`: Oct 31 ATM IV 50 / Dec 19
ATM IV 45 = 1.11 ≥ `TERM_EVENT` → the term chip reads "front month 1.11x the back - an
event is priced (earnings in 19d)" (`term_words(1.11, 19, front_dte=28)`, B1.4 — a chip,
not a `verdict_why` clause). `verdict_why` = **"IV rank 62 (>= 50) and priced for 21% more
movement than the stock has actually shown"**. Gates: buy False, sell_directional True,
sell_neutral True, mid False.

**Chart state** (EMA values are the fixture's inputs; `analyze()` run once). trend `up`
(`rng` is None, so not sideways; e20 345.1 > e50 331.8 > e200 298.4, 34 days); structure
bullish; `sup` → setup `support_bounce` quality 50 + 20 (3 touches) + 10 (vol_high) = 80,
level 340.0, zone [339.1, 340.9]; `tl` (Part C) `n_touches` 3, `value_today` 341.9,
`value_at["2026-11-20"]` = 362.1 — above the support, so the constraint `min(sup_lo,
line_at_expiry)` = 339.1: the horizontal level binds and the card says "under support".
Plan: direction up, entry 349.20; the credit stop is where the level has failed, `zone_lo −
LEVEL_PAD_ATR × ATR` = 339.1 − 2.89 = **336.2**; the debit plan's stop `min(349.20 − 11.54,
336.2)` = 336.2 as well; target 349.20 + 2 × 13.0 = 375.2, capped by nothing (no
resistance within 1.5R–2R). (The design mockup's 338 was a 0.1-ATR pad; the engine's
0.25-ATR pad — the detector's own `REACH_ATR` tolerance, a spring 0.25 ATR under the zone
being still a test, not a break — gives 336.2, and 336.2 is the figure every part now
uses; the difference is about $10 a contract at the stop.) Earnings {date: 2026-10-22,
days: 19}.

**Recommender** (prefs = house; `earnings_rule = none_inside`):

| rule | checks | result (`reason_key`) |
|---|---|---|
| bull_put | trend up ✓, setup support_bounce ✓, gate sell_directional ✓, support_level ✓, earnings: window Nov 20 (48d) and Dec 19 (77d) [Oct 31 = 28 < 30] — Oct 22 ≤ both → inside every expiry, `defined_risk` not allowed under `none_inside` → **fails** | rejected (1 fail, `earnings_inside`): "earnings Oct 22 (19d) sits inside every 30–60 day expiry" |
| bull_call | gate mid_or_buy ✗ (62), earnings ✗ | rejected (2; `expensive`) |
| buy_call | gate buy ✗, earnings ✗ | rejected (2; `expensive`) |
| iron_condor | trend sideways ✗, range ✗ | rejected (`trending_not_sideways`) |
| calendar | slow_drift? EMA20 moved 6.1 in 10 bars = 0.53 ATR ≤ 0.75 ✓; term front_ge_back ✓ (1.11); earnings inside the back expiry (Dec 19) ✗ | rejected (1, `earnings_inside`): "earnings Oct 22 sits inside the back month" |
| others | trend ✗ | rejected (`wrong_direction`) |

`recommended = None`; `shown = True` on bull_put and calendar (the two single-fail rows);
all ten rows stored; card headline (stored at write time): "Uptrend for 34 days, bounced
off support at 340 on high volume, options are expensive (IV rank 62 over the last year) —
but earnings land on Oct 22 inside every expiry in your window. Nothing to do until after
earnings; the post-earnings idea will appear on Oct 23. (Allow defined-risk trades through
earnings in My rules to see the bull put spread now.)" Because the rejection is
`earnings_inside`, the bull put's strike table is hidden and no ticket can be built
(B6.4).

**With `earnings_rule = defined_risk_only`:** bull_put fits, score 60 + (62−30)/70×20 =
9.1 + 80/5 = 16 + 5 (structure bullish) = **90.1**; `recommended = bull_put`; chips:
[✓ Bull put spread] [Bull call spread · options too expensive to buy] [Buy call · options
too expensive to buy] [other strategies ▾]; "What has to happen": "LRCX stays above 330
until Nov 20. You keep the credit if it does nothing, drifts up, or even dips a little."

**Picker** (Nov 20, 48 DTE; leg IV 0.46; band 0.20–0.30; width 0.5–1.5 ATR = $5.8–17.3, so
10 and 15 wide on the $5 ladder; short under 339.1 − 2.9 = 336.2; credit ≥ 25% of the risk).
The fixture's ladder: 335P δ 0.31, **330P δ 0.25 (mid 9.60)**, 325P δ 0.21, **320P δ 0.17
(mid 7.50)**, 315P δ 0.14, 310P δ 0.11:

| short | delta | long | width | credit (mid) | % of risk | max loss | POP (keep) | breakevens | constraint | score |
|---|---|---|---|---|---|---|---|---|---|---|
| **330P** | 0.25 | **320P** | 10 | **2.10** | 26.6% | **$790** | **75%** | **[327.90]** | 330 ≤ 336.2 ✓ | **0.199** |
| 325P | 0.21 | 315P | 10 | 2.00 | 25.0% | $800 | 79% | [323.00] | ✓ | 0.198 |
| 330P | 0.25 | 315P | 15 | 3.05 | 25.5% | $1,195 | 75% | [326.95] | ✓ | 0.191 |
| 325P | 0.21 | 310P | 15 | 2.90 | 24.0% | $1,210 | 79% | [322.10] | ✓ | under the credit floor — not a pick (counted in `considered`) |
| 335P | 0.31 | — | | | | | | | 335 ≤ 336.2 would pass the chart rule | out of band (nearest above) — not enumerated while two shorts sit in band |

Top 3 by score: **330/320** 0.199 — THE golden pick ("best fit to your rules", "most credit
per $ risked"), **325/315** 0.198 ("safer": "more room below the price", "highest chance"
79%), **330/315** 0.191 ("most credit per contract" — $305 for $1,195 at risk). Each pick
stores `chart_stop` 336.2; the golden pick stores `max_profit` 210, `max_loss` 790,
`breakevens` [327.90], `pop` 0.75 (`pop_kind keep`), `chart_stop_pl` **−120.7** (the card
prints "about −$121"), `rule_stop_pl` **−158**, `pop_model` 0.73. Words for 330/320:
"delta 0.25 ≈ a 1-in-4 chance LRCX is below 330 at expiry · you collect $180–$210 (worst
likely fill to mid) · you risk $790, but the chart stop at 336.2 would lose about $121 ·
About a 75% chance of keeping the credit - an estimate from today's option prices (the
short strike's delta), not a promise. Earnings, news and gaps are not in that number. ·
model estimate 73% · earns about $2 a day · a 1-point IV rise costs you about $5". Rules
line: "delta 0.20–0.30 · 30–60 days · width 0.5–1.5 ATR ($6–17) · credit ≥ 25% of the
risk · under support 340.9". The 325/315 row is the ONLY place its figures appear: nothing
downstream — sizing, ticket, payoff, tests — ever uses it.

**Sizing** (read time; stored NLV $100,000, risk 1% = $1,000, `gap_mult` 2.0) for the
golden 330/320: loss at stop 336.2, `stop_iv` = 0.46 × 1.10 = 0.506 (the relative bump,
B5.1): d=0 mark 3.31 → loss (3.31 − 2.10) × 100 = **$120.7**; d=24 mark 2.76 → $66; max =
$120.7. `by_chart_stop = floor(1000/120.7) = 8`; `by_gap = floor(2000/790) = 2`;
`by_notional = floor(10000/1000) = 10` (the $10 width × 100 is the collateral); **contracts
2** (the gap cap binds); capital at risk 2 × 120.7 = $241.4 → "about $242"; max loss total
$1,580 = 1.6% of the account; rule stop 20% × 790 = $158/contract → "the chart stop fires
first ($121 vs $158)". Sizing line: **"2 contracts: about $242 if the stop fires, up to
$1,580 (1.6% of your account) if the stock gaps past it"**.

**Ticket.** The B6.1 / B6.2 / B6.3 renderings ARE this trade: SELL 2 LRCX 20 NOV 26 330 P
/ BUY 2 LRCX 20 NOV 26 320 P; limit credit 2.10, floor 2.00 (25% of the risk, "never below
$200 a contract"); header "Prices are from 02 Oct 16:00 ET. Press Refresh after 21:30
Malaysia time ..."; line 2 carries "(you are paid; the most you can lose is fixed)"; entry:
no condition (the default); with the "Enter on the dip" toggle on, the close 349.20 is
2.4% above the level → beyond `FRESH_MAX` → `LRCX last ≤ 341.92` (340.9 × 1.003) plus the
crash warning; chart stop order: GTC, conditional `LRCX last ≤ 336.20`, Trigger outside
RTH: No, Market (limit debit 3.31 secondary), the RTH sentence, "about $242 here; up to
$1,580 if the stock gaps past it" (both figures, always); take profit GTC at debit 1.05;
rule stop line "(no order - the Positions tab watches it) ... marked at 3.68 or more" (2.10
+ 0.2 × 7.90); time stop Oct 30 (21 DTE). Warning: "earnings Oct 22 are inside this trade
— allowed by your rules; the stop is the only protection". moomoo: buy to close the 330 P
first (market, or limit 17.14), then sell the 320 P, with the naked-put sentence.

**Exits** on a hypothetical Oct 20 check: spot 344, 330P delta 0.22, mark 1.60 → pl =
(2.10 − 1.60) × 100 × 2 = +$100, profit_pct 24%, loss_pct 0, dte 31; earnings row: Oct 22
was known at entry (`earnings_date_at_entry` = 2026-10-22) and allowed by
`defined_risk_only` → not fired → `OK` "Inside every line (delta 0.22, 0% of max loss
used, 24% of credit captured). Hold." On Oct 23 after a gap to 335 (close under 336.2):
`CLOSE` by the chart stop — "LRCX closed at 335.0, under the 340 support the trade was
sold against" — fires before the delta line (the 330P's delta 0.55 ≥ the house 0.35 would
also fire; the chart stop is listed first). Had the earnings date been unknown at entry and appeared on Oct 10, the
Oct 10 check would have returned `WATCH (urgent)` with "Earnings 2026-10-22 now fall
inside this trade (the date was unknown or later when you entered). Decide before the
close that day."

#### B8.2 ISRG — entry 405.81 / SL 394.27 / PT 428.89 (the user's sheet said 429.14; noted, not used), ATR 11.54, earnings Oct 21, IV rank 41, IV30 34%, HV20 31%

This is the golden ISRG fixture (`tests/fixtures/options/isrg.json`): entry 405.81, ATR
11.54, stop 394.27, target 428.89, earnings Oct 21 inside the Nov / Dec expiries, IV rank
41 (`basis rank`), recommended `bull_call` Dec 395/430 under `defined_risk_only`. (Part
C5.2's single Dec 400 call at 34.30 is a SUPPLEMENTARY `buy_call` worked example on the
same entry / ATR / stop / target — a worked example, not the fixture.)

**Gauge.** rank 41 (`state ok`, `basis rank`) → `NEUTRAL` band; `iv_hv_premium` 34/31 =
1.10 ≥ `IV_HV_RICH` → the band tie-break moves the verdict to **SELL**; `verdict_why` =
"IV rank 41 (30-50) and priced for 10% more movement than the stock has actually shown -
sellers are paid"; gates: buy False, sell_directional True, sell_neutral False, **mid
True**. `term_ratio` 1.08 (earnings in 18d — the term chip says so).

**Chart state.** trend up (e20 398.2 > e50 388.0 > e200 361.5, 21 days); setup `ema_rebound`
on EMA20 (`fresh`: close 0.4% above it... the level 404.60 = EMA20; entry = 404.60 × 1.003
= 405.81 — the brief's figures are exactly the Curated offset entry); plan: stop 405.81 −
11.54 = **394.27** ✓, target 405.81 + 2 × 11.54 = **428.89** (the brief's 429.14 is the
same plan with the offset applied before the ATR — within rounding; the engine reports
428.89 and the test asserts ±0.3). `levels.resistance` 431.0 (a flip level, 2 touches) —
within 1.5R–2R of entry, so `target_level` exists and the target is capped at 428.89 (the
nearer). Earnings {2026-10-21, 18d}.

**Recommender** (house prefs, `none_inside`): every 30–90 day expiry contains Oct 21 →
bull_put (defined_risk, not allowed) rejected (1 fail, `earnings_inside`), bull_call (mid ✓,
target_level ✓, earnings ✗) rejected (1, `earnings_inside`), buy_call (gate buy ✗ at 41,
earnings ✗) rejected (2, `expensive`: "not cheap enough to buy outright (IV rank 41 >
30)"), leaps_call (weekly ✓, mid ✓, earnings any ✓ — `w_uptrend` requires 200 weekly
candles; ISRG has them → passes every check, score 40 + (20 − |41−40|) = 19 + 35/5
(ema_rebound quality 35) = 7 + 5 = 71 − 10 (unbuilt) = 61). Result: `recommended = None`
— an unbuilt rule can never be recommended; leaps_call is `also_fits` with `reason_key =
not_available_yet` and the chip "Buy LEAPS · not available yet"; `shown = True` on
bull_put and bull_call ("earnings Oct 21 inside every 30–60 day expiry"). Headline:
"Uptrend, fresh rebound on the EMA20, IV middling (rank 41 over the last year). Earnings
Oct 21 sit inside every swing expiry. The long-term chart would suit a long-dated call;
that strategy is not in TradeHunter yet. Swing ideas return after earnings." No strike
table, no ticket.

**With `defined_risk_only`:** bull_call fits: 55 + 19 + 7 + 5 = **86**; bull_put: 60 +
(41−30)/70×20 = 3.1 + 7 + 5 = 75.1 → **bull call spread recommended, bull put spread
"also fits"** — the design's two-chip case; leaps_call stays in `also_fits` behind both
(61, "· not available yet"). buy_call greyed: "options not cheap enough to buy outright
(IV rank 41 > 30)".

**Picker — bull_call** (Dec 19, 77 DTE, since Nov 20's 48 DTE is inside 30–60 as well:
both enumerated; Dec shown): long in 0.60–0.70: 390C (δ 0.66, 32.5... at 77 DTE: 395C δ
0.62 mid 32.52, 390C δ 0.66); short forced by the chart target 428.89 → next listed strike
up = **430C** (δ 0.41 at 77 DTE — outside 0.25–0.35 → soft note, ×0.9). POP from
`payoff.pop` (profit):

| long | short | debit | reward | reward/cost | breakevens | POP (profit) | loss at stop 394.27 (d=38, IV×1.1) | score |
|---|---|---|---|---|---|---|---|---|
| 395C (δ 0.62) | 430C | 15.62 | 19.38 | 1.24 | [410.62] | 46% | $348 | 1.24×0.46×0.9 = **0.513** |
| 390C (δ 0.66) | 430C | 18.47 | 21.53 | 1.17 | [408.47] | 47% | $382 | 0.495 |
| 400C (δ 0.59 — 0.01 under the band) | 430C | 12.93 | 17.07 | 1.32 | [412.93] | 45% | $309 | not enumerated (out of band); it would score 0.535 and is the `nearest` only when nothing is in band |

Top pick **395/430 Dec**. Words: "delta 0.62 − 0.41 = 0.21 net: moves about 21 cents per
$1 · you pay $1,562–$1,587 (mid to worst likely fill) · you risk $1,562, but the chart stop
at 394.27 would lose about $348 · About a 46% chance of profit if held to expiry, at
today's volatility; this trade is managed by the chart stop and target, so the real odds
depend on the move, not this number. · loses about $4 a day". Sizing: `by_chart_stop =
floor(1000/348) = 2`, `by_gap = floor(2000/1562) = 1`, `by_notional = floor(10000/1562) =
6` → **1 contract**; rule stop 50% of debit = $781/contract → chart stop fires first;
line: "1 contract: about $348 if the stop fires, up to $1,562 (1.6% of your account) if
the stock gaps past it".

**Picker — bull_put** (also fits; Nov 20 48 DTE; under the EMA20 level 404.6 − 2.9 =
401.7 — and under the stop 394.27 since the EMA is the level): band 0.20–0.30 → 385P (δ
0.30) / 380P (0.26) / 375P (0.23); pairs 385/375 credit 3.02 (30%) POP 70% score 0.304
(top), 380/370 2.66 POP 74% 0.268; loss at stop 394.27 (d=0): $100 → `by_chart_stop` 10,
`by_gap` floor(2000/698) = 2, `by_notional` floor(10000/1000) = 10 → **2 contracts**.

**Ticket (bull_call 395/430 Dec, 1 contract).** BUY 1 ISRG 19 DEC 26 395 C / SELL 1 ISRG
19 DEC 26 430 C, limit debit 15.62, ceiling 17.50 = `min(mid + 0.5 × widest_spread,
width / (1 + reward_cost_min))` = min(15.87, 35/2) → 15.87 is the working ceiling, 17.50
the absolute one; entry: no condition (the dip toggle is disabled on a fresh rebound);
chart stop GTC conditional `ISRG last ≤ 394.27`, Trigger outside RTH: No, Market
recommended (the model value at the stop today 13.32 as the secondary limit; the sizing
used the mid-life 12.14), the RTH sentence; chart target `ISRG last ≥ 428.89` → the spread
is worth ≈ 21.97 at mid-life → take-profit limit 21.95 (+$635 a contract), or hold toward
430 for the full 35.00 at expiry; warning "earnings Oct 21 inside — allowed by your rules".
moomoo stop: buy to close the 430 C first, then sell the 395 C — "Never sell the long leg
before the short leg is closed — you would be short a naked call."

**Exits.** Nov 3 check, spot 418, mark 20.4 → pl +$478 (27% of the $1,938 max profit), no
line → `OK`. Nov 24, spot 431.5 (close ≥ 428.89) → `TAKE` "Target 428.89 reached: the last
quarter comes slowest. Sell it." Alternative path: Oct 22 post-earnings gap to 388 →
`CLOSE` by the chart stop; the model marks the spread at 11.54 there (58 days left, IV ×
1.1) → loss (15.62 − 11.54) × 100 = $408, more than the $348 the chart-stop sizing
budgeted because a gap lands beyond the stop — and well inside the $2,000 the gap cap
(`gap_mult` 2.0) allowed for, which is what that cap is for; the ticket's earnings warning
said so.

#### B8.3 The other families, briefly, on the same two charts (what each engine returns)

| strategy | LRCX | ISRG |
|---|---|---|
| iron_condor | rejected: `rng` None → not sideways (`trending_not_sideways`), no range (`no_range`) | rejected: trend up |
| calendar | with the earnings rule relaxed: 350 strike (close 349.2), front Oct 31 C350 19.41 (IV 0.50) / back Dec 19 C350 29.74 (IV 0.45), debit 10.33, front/debit 1.88, `payoff.pop` at the front expiry: breakevens ≈ [319, 392], POP (profit) 54%; `slow_drift` ✓ and term ✓ → fits only when earnings allowed; the gauge's "event priced" reason is the honest flag that the front IV is earnings, not edge | rejected: earnings inside the back month |
| leaps_call | w_uptrend ✓, but IV rank 62 fails `mid_or_buy` → rejected (`expensive`): "the long leg would be bought at IV rank 62". For the picker alone (Jan 2028, 470 DTE, IV 0.42): 280C δ 0.79 extrinsic 39.3 = 11.3% of 349.2 → over the 10% cap; 270C δ 0.81 and 260C δ 0.83 out of band → degenerate `extrinsic_cap`: "the least time value in your band (280C) is 11.3% of the share price (cap 10%)" | Jan 2028 (IV 0.32) 340C δ 0.79, extrinsic 38.0 / 405.81 = 9.4% ✓ → pick (shown only once the rule is built; today the chip reads "· not available yet"); 320C δ 0.84 out of band; words "behaves like 79 shares · you pay $10,383 for exposure to $40,581 of stock (3.9x) · 9.4% of the share price is time value · About a 32% chance of profit if held to expiry ... — the thesis is the weekly trend, not this number"; sizing by the weekly-trend stop: stop = weekly EMA50 (290.1), model value there at mid-life 18.03 → loss ≈ $8,580/contract → `by_chart_stop` 0 at 1% of $100k, `by_gap` floor(2,000 / 10,383) = 0, `by_notional` floor(10,000 / 10,383) = 0 → "Not even one contract fits your 1% - lower the risk or choose a narrower spread" |
| diagonal_call | rejected: `slow_drift` ✓ but resistance_level: 371.5 exists → passes; mid_or_buy ✗ (62) → rejected (`expensive`) "the long leg would be bought at IV rank 62" | mid ✓, slow_drift (EMA20 moved 0.6 ATR in 10 bars) ✓, resistance 431 ✓ → fits at 45 + 19 + 7 + 5 − 10 = 66 → `also_fits` "· not available yet" (unbuilt). For the picker alone: long Jan 2028 340C 103.83 δ 0.79, short Nov 20 430C (δ 0.36 at 48 DTE; ≤ 431 ✓) 11.36; safety (430−340) + 11.36 = 101.36 ≥ 103.83 ✗ → fails by 2.47 → degenerate `safety`: "no short call under resistance 431 covers the long call's cost if the stock rips (short of it by $2.47)"; `nearest` shown |

### B9. Test plan

Pure modules get synthetic-chain tests (the `bull_put` discipline); the chart modules get
synthetic bar series plus the live names already used in the README; the whole path gets
one end-to-end fixture per worked example. Style: the README changelog "Tested:" line.
Infrastructure (shared with A8 / C6 / D8.2, created in step 1 before the first engine
lands): `dashboard_tst/requirements-dev.txt` (pytest) and the `dashboard_tst/tests/` tree
with `tests/fixtures/options/`.

**Fixtures** (`tests/fixtures/options/`): `chain_bs(spot, iv, expiries, strikes,
skew=0)` — a Cboe-shaped chain priced by `black_scholes` with bid/ask = mid ± spread/2,
OI/volume from a seed; `bars_synth(kind)` — daily bars for uptrend+bounce, downtrend+
breakdown, range (two edges × 3 touches), flat-no-range, slow-grind; the LRCX and ISRG
fixtures as in B8 (chain + bars + iv_series + earnings).

| module | synthetic cases | live / integration |
|---|---|---|
| `opt_legs.norm_leg` | a bridge row (iv 46.0, `oi`) with `unit="percent"` and a Cboe row (iv 0.46, `open_interest`) with `unit="fraction"` normalise to the same dict; a Cboe row with iv 3.1099 and `unit="fraction"` keeps 3.1099 (no magnitude guess); 0.0/0.0 quote → `quote_ok False`; NaN → None; negative OI → None; `stored_leg()` drops exactly the engine-only keys | one real Cboe chain (MSFT) and one bridge chain (saved payload from the old tab, through `BridgePayloadSource`) agree on every leg's `price`, `iv`, `oi` |
| `premium_gauge` | the golden LRCX inputs (IV30 46, HV20 38, front 50 / back 45, rank 62, n=252) → SELL + both sell gates, `basis rank`, `iv_hv_premium` 1.21 (a ratio), `term_ratio` 1.11, gates `{buy False, sell_directional True, sell_neutral True, mid False}` and `verdict_why` EXACTLY "IV rank 62 (>= 50) and priced for 21% more movement than the stock has actually shown"; 24 → BUY; 41 with IV/HV 1.21 → SELL, 41 with 0.95 → NEUTRAL, 41 with 0.85 → BUY; n=19 with HV → `basis provisional`, gates all False, `verdict_why` carries "12 of 60 days"; n=40 → `basis percentile`, `state pct_only`, the "(not a full year yet)" phrase; n=118 → `rank_ok`, "over 118 days"; n=0 and no HV → UNKNOWN, `basis unknown`, the exact "We cannot yet say..." sentence (the ONE string); max==min series → rank None; `term_words(1.11, 19, front_dte=28)` → "front month 1.11x the back - an event is priced (earnings in 19d)", `term_words(None, 19)` → None, `term_words(0.93, None)` → the calendar-shape sentence; `span_words` gives the three phrases; the returned dict has exactly the contract's `iv` keys (`gates` has four keys, no `term_event`) | the 7 pre-listed IV Rank tickers: server rank vs the bridge's `/iv` rank after the bootstrap agree within 1 point |
| `mirror_setups` | `mirror_bars` round-trips; `find_resistance_reject` on an inverted bounce series returns the mirrored level and a "pin" candle; `find_breakdown` fires on the breakdown fixture only on the break day and the day after; the uptrend fixture → None for both | NVDA at the 2026-09 highs prints a resistance reject; the v4.125 bounce names (CVX, WFC, TGT) print nothing on the bear side |
| `chart_state` | `ema_setup.analyze` is called exactly once per `read()` (a spy); trend up / down / sideways / unclear on the four fixtures — sideways ONLY when the fixture's `rng["sideways"]` is True (Part C6.2's clean range fed flat EMAs), never on the trending fixture; `trend_days` counts; `slow_drift` true on the grind fixture, false on the bounce fixture; `breakout_retest` from a flips-only `sup` result within 20 bars; plan stop/target reproduce ISRG 394.27 / 428.89 ± 0.3 from entry 405.81 and ATR 11.54 and LRCX 336.2 from zone low 339.1; primary setup picks the trend-agreeing one; the stored `setup` (B2.7) carries `sup / tl / tl_bounce / rng` verbatim and `tl["value_at"]` has a key for every expiry passed in | LRCX week of 2026-09-29 reads up + support_bounce 340 (the README v4.125 verification set: CVX, WFC, TGT bounces still detected through `chart_state`); DNOW / GILD / HSY (the consolidation charts in the worktree, `consol_candidate_*.jpg`) read sideways through `rng` |
| `strategy_rules` | all ten rows evaluated on each fixture, all ten returned with `fit ∈ {recommended, also_fits, rejected}`; counts as in B8 tables; every rejected row has a `reason_key` from the fixed vocabulary and `shown` is True on at most 2 single-fail rows; rank 45 → bull_call 86 > bull_put 80.3; rank 62 → bull_put only; rank 24 → buy_call > bull_call, bull_put greyed `cheap_options`; earnings inside every expiry → the exact reason string and `earnings_inside`; `defined_risk_only` flips bull_put to fit and buy_call stays rejected; an unbuilt fit is `also_fits` with `not_available_yet` and is NEVER `recommended` even when it is the only fit (ISRG, `none_inside` → `recommended None`); no member-facing string contains "step"; `basis provisional` rejects credit rows with `not_rich_enough` and passes debit rows with the warning; `why`/`must_happen` templates format with every ctx key (no KeyError on any fixture) | — |
| `option_prefs` | `clean({})` fills every block from HOUSE and `HOUSE_HASH == prefs_hash(clean({}))`; a stored `{"credit_vertical": {"short_delta_hi": "0.35"}}` reads as 0.35 through `read(db, user)` and lands in `_overridden` as `"credit_vertical.short_delta_hi"`; `"abc"` → default; out-of-range `write(db, user, tab, form)` returns the error text and stores nothing; `prefs_hash` stable under key order and 12 hex long (fits the String(16) column); changing `nlv`, `risk_pct`, `gap_mult`, any `premium_stop_pct`, `roll_dte` or anything under `prefs["telegram"]` leaves the hash UNCHANGED; changing `short_delta_hi` changes it; `HOUSE_HASH` equals the hash of a member with no overrides; `read()` returns `telegram` and `account` ({nlv, risk_pct, nlv_source}) as top-level keys and NO `telegram` field in `SCHEMA` (every `kind` ∈ {num, int, bool, choice}); every `Field` has non-empty `label`, `help`, `plain` and a `unit` in the `(default, lo, hi, kind, label, help, plain, step, unit)` order; `for_strategy("bear_call")` returns the credit_vertical block; `defined_risk()` is True for exactly bull_put, bear_call, bull_call, bear_put, iron_condor; `distinct_hashes(db)` lists HOUSE_HASH plus every stored hash once; `STRATEGY_KEYS` is imported from `strategy_rules` in the user's order | — |
| `strike_picker` | bull_put on the golden LRCX chain → the B8.1 table (scores to 3 dp, **330/320 first** at credit 2.10; `breakevens` == [327.90], a one-element list; `max_profit` 210, `max_loss` 790 positive; `pop` 0.75, `pop_kind keep`; `chart_stop_pl` −120.7, `rule_stop_pl` −158; 325/315 second, never used by anything downstream); the constraint removes 340P (δ 0.37) once the band is widened to 0.40 while 335P (under 336.2) stays; `no_band` on a chain with deltas all < 0.15 shows the 2 nearest; `credit_floor` when `credit_pct_min` = 40 (the 2.10 credit is 26.6% of its 7.90 risk); `thin` when every OI < 500; bear_call = the mirror on mirrored bars + a call chain; bull_call: the short is forced to 430 and carries the soft-band note, ×0.9, `pop_kind profit` from `payoff.pop`; buy_call: theta cap excludes 14-DTE legs; leaps: 10% of SPOT cap passes 340C on ISRG and fails on LRCX (`extrinsic_cap`); condor on the range fixture: both shorts outside `rng.zone_low[0]` / `rng.zone_high[1]`, POP_both; calendar: only front×back pairs with front IV ≥ back IV, POP from `payoff.pop` at the front expiry equals the lognormal mass between the breakevens (±1%); diagonal: the safety rule excludes the B8.3 ISRG pair by $2.47 and admits it when the long is 360C; every `words` string renders for every family, `pop` through `pop_words` with the two verbatim sentences; the degenerate stub (`status nearest/none`) is what the signal stores when nothing passes | the old tab's `bull_put.select` and the new picker agree on the top pair for the same single-expiry bridge chain when prefs = `bull_put`'s constants (delta 0.20–0.25, DTE 45–60, offsets 1–2) |
| `payoff` (iv_bump) / `option_sizing` | `pnl()` at `days_ahead=0`, `S=spot`, `iv_bump=0` equals 0 within the bid/ask (round trip); at expiry equals intrinsic; `iv_bump=0.10` on a leg at iv 0.46 prices at sigma 0.506 (relative, never 0.56); LRCX 330/320 at 336.2: $120.7 (d=0) > $66 (d=24) → $120.7; ISRG 395/430 at 394.27: $230 (d=0) < $348 (d=38) → $348; `size()` on the golden pick returns `{by_chart_stop 8, by_gap 2, by_notional 10, contracts 2, stop_t_days 0, stop_iv 0.506, max_loss_pct_nlv 1.6, nlv_source "prefs"}` and the line "2 contracts: about $242 if the stop fires, up to $1,580 (1.6% of your account) if the stock gaps past it"; ISRG 1 contract (2 / 1 / 6) as in B8; `by_notional` binds when risk_pct = 5 and `gap_mult` = 3 (41 / 18 / 10 → 10); ISRG LEAPS → 0 by all three caps with the exact note; nlv None → contracts None, `nlv_source` None + the account note "sized once you tell us the account value (My rules -> Shared)"; stop == entry → `by_chart_stop` None, sized by the other two with the note; `fires_first` flips when `loss_fraction` = 0.10; `size()` never returns 1 when `by_gap` is 0; the sizing `line` always contains both $ figures and the %; `rule_stop_pl` is non-None for EVERY family on the fixtures (credit 0.20 × max loss, leaps / diagonal 40%, long / debit / calendar 50%) | the user's live NVDA 205/195P × 6 (README v4.62: mark 1.975 vs credit 1.85 = −$75) marks to the same −$75 through `option_exits.mark` |
| `order_ticket` | `build(pick, setup, prefs)` on the golden pick renders the B6.2 TWS text and the B6.3 moomoo text byte-for-byte (the ONE golden text per broker: "2 contracts", "(you are paid; the most you can lose is fixed)" on line 2, "Rule stop (no order - the Positions tab watches it)", and the stop line with BOTH figures "about $242 here; up to $1,580"); `condition` is None by default on EVERY fixture; `dip=True` on the run-away bounce gives `≤ 341.92` AND the crash-warning sentence, on a fresh bounce gives None; both renderings contain "Trigger outside RTH: No", the RTH sentence and the "Prices are from" header; the moomoo stop lists the short leg before the long leg with the naked-put / naked-call sentence (bull_put / bear_call); the TWS stop says Market recommended and "may not fill in a fast market" on the limit; `refresh_first` True when `as_of` is the previous session and the clock is inside RTH, False otherwise; a rejected-for-`earnings_inside` strategy raises `TicketRefused` (no ticket); a strategy rejected for `expensive` builds with the rejection sentence as line 1 of both renderings; `contracts 0` prints "—" and the sizing note; three orders for a credit spread, two expiries listed per leg for a calendar; floor/ceiling arithmetic; every warning string present when OI is None | copy-pasted into TWS's Strategy Builder on paper (bridge up) — legs resolve, the chart-stop condition accepted with Trigger outside RTH off |
| `option_exits` | every row of the B7.3 table has a case that fires it and a case one tick short that returns WATCH or OK; the earnings row fires `WATCH urgent` for every family when a date appears after entry and stays quiet when the date was known and allowed at entry; the LEAPS loss row fires at 40% of premium while the weekly stack still holds; the diagonal's long leg inherits it; losing-side wins over TAKE on the same day; chart stop before delta when both fire; a `bear_call` trade marks against the CALL chain (right from the stored leg); `UNKNOWN` only when delta, mark AND the chart are all missing; `EXPIRED` for dte < 0; the migration `f4a5b6c7d8e9` copies an open `option_spreads` row and the new monitor's verdict on it equals `spread_monitor.snapshot`'s state for the same chain; `sweep` calls `setup_for(deep=True)` only for symbols holding a LEAPS / diagonal trade | the nightly `sweep` on the dev DB with the migrated NVDA spread + one paper trade per family; `GET /options/badge` counts match the checks written |
| `telegram_push` (Part D's module, this part's content) | the LRCX idea line carries the headline, the must_happen line ("LRCX stays above 330 until Nov 20..."), "about 75% chance of keeping it (estimate)", the deep link `public_url + "/options?symbol=LRCX"` and NO contract count; skipped when `earnings_date` is None (log line "skipped: earnings date unknown"), when `iv.provisional`, when `snapshot.partial`, when the rule is unbuilt; the dedupe key `(symbol, strategy, front expiry)` suppresses a one-strike drift and re-pushes after a > 1 ATR move; the first line is the "Ideas for tonight's US session (opens 21:30 Malaysia)..." sentence; 7 ideas → 5 sent + "and 2 more on the page" | a dev chat after the chat-id handshake; chunking exercised by a 12-ticker basket |
| end-to-end (`tests/test_option_engines.py`, step 1) | `option_engine.compute(chain, metrics, state, prefs)` on the LRCX and ISRG fixtures (`tests/fixtures/options/lrcx.json`, `isrg.json`) returns the B8 rows byte-stable (golden JSON): `{status, headline, setup, iv, strategies, picks, computed_ms, engine_version}` with `trend` a string, `setup` per B2.7 (flat `stop` 336.2 / `target` 375.2 beside `plan`), `iv` per B1.4, `strategies` ten rows (a rejected row has `score`, `why`, `must_happen` null; the LEAPS label is "Buy LEAPS"), `picks = {key: [Pick]}` with the golden 330/320 first, `headline` present; with and without `defined_risk_only`; prefs with a widened band change only `picks` and `prefs_hash`; two members (house + one override) produce two rows per symbol and `basket_rows_for(db, user)` returns a dict keyed by symbol whose `pick_state` is `has_picks` / `no_strike_passes` / `not_checked` correctly, the last for a third member whose hash has no row | the full basket from the IV Rank "My list" (7 tickers) in under 2 s from the stored snapshot; a "Live" press re-grades the live expiry through `BridgePayloadSource` and re-sizes from the payload's NLV (`nlv_source "live"`) without re-running the recommender and without writing a snapshot row |

Edge probes carried from `support_bounce`'s "Tested:" line (`README.md:335-339`) apply to
every chart function: 30-bar history, None volume, a still-open session at 10% and 50%, a
zero-range candle, non-numeric prices — all return None / the honest read, never raise.

