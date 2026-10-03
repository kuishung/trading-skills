## C. Chart engines: automatic trend lines, range/resistance mirror, and the risk/reward payoff chart

All paths below are relative to `dashboard_tst/`. Line numbers are as of v4.126 (commit `8a5b712`).
Three pure modules (no DB, no network, no FastAPI import) plus one Jinja partial and a set of
small hooks in the existing chart. Nothing in this part needs a table or a migration: every
output is a dict that the owner of `option_signal` stores in its JSON columns (see C7). The
Alembic head is still `e2f3a4b5c6d7_iv_scan_items` (nothing revises it) — Part C chains nothing
off it.

### C0. Files, reuse map and build phase

| File | Action | What | Needed by step (§9) |
|---|---|---|---|
| `app/services/trend_line.py` | **create** | automatic trend line + channel; trend-line bounce detector (C1) | 2 (debit family: "buy a call at the trend line") — the chart line itself can ship in 1 |
| `app/services/range_box.py` | **create** | range / resistance detector, sideways verdict (C2) | 3 (iron condor), 4 (calendar) |
| `app/services/payoff.py` | **create** | generic legs → expiry + T+0 curves, breakevens, max P/L, POP, stops, R (C3) | 1 |
| `app/templates/_payoff_chart.html` | **create** | server-rendered SVG payoff chart + hover + $/R toggle (C4) | 1 |
| `app/services/ema_setup.py` | modify | `analyze()` gains `tl`, `tl_bounce`, `rng` fields; `t1` / `r1` condition keys; chips (C1.7, C2.6) | 2, 3 |
| `app/templates/_price_chart.html` | modify | `chart_trendline` / `chart_range` overlays; `SPREAD.legs` for non-put strike labels (C4.4) | 1 (strikes), 2 (trend line), 3 (range) |
| `app/templates/base.html` | modify | four `--po-*` colour tokens, dark + light (C4.2) | 1 |
| `app/routes/options.py` (or Part D's page route) | modify | `GET /options/{symbol}/payoff` → the partial; overlays passed like `sector._bounce_overlay` (C4.5) | 1 |
| `README.md` | modify | Contents lines for the three modules + partial; changelog entry per release | each |

Primitives reused verbatim (never copied): `support_bounce.atr_series` (l.150), `_swings` (l.169),
`_members` (l.205), `_touches` (l.216), `is_pin_bar` (l.126), `is_engulfing` (l.137),
`volume_read` (l.319) and its constants (l.103-123); `black_scholes.black_scholes` /
`norm_cdf` (`app/services/black_scholes.py` l.43 / l.23); `bull_put.spread_math` (l.627),
`LOSS_STOP_FRACTION` (l.616), `RISK_FRACTION` (l.67); `trade_prefs.read` (l.71);
`option_quotes.leg` / `expiries` (l.184 / l.191); `prices.fetch_daily_ohlc` bar shape (l.337-345:
`{time:'YYYY-MM-DD', open, high, low, close, volume, session_frac?}`).

### C1. Trend-line engine — `app/services/trend_line.py`

#### C1.1 Contract

```python
def find(bars: list[dict], direction: str = "up", *, at: tuple[str, ...] = ()) -> dict | None:
    """The best still-valid trend line under the lows (``direction="up"``) or over
    the highs (``"down"``) of the last LOOKBACK sessions of ``bars`` (``/prices``
    dicts, oldest first), or None when no pair of reaction-tested pivots makes one.

    ``at`` = ISO dates (option expiries) the caller wants the line's value on;
    each is answered in ``value_at`` by weekday-count extrapolation (see value_on).

    Returns {direction, p1: {time, price}, p2: {time, price}, i1 (bar index of p1),
    slope_per_bar, slope_atr (slope / ATR), touches: [{time, price}] (oldest first,
    distinct visits, p1 and p2 included), n_touches, span_bars (p1 -> last bar),
    value_today, value_at: {date: price}, broken: bool, last_break: date|None,
    warning: bool (latest close is through the line but not by BREAK_ATR),
    residual_atr (mean |touch - line| / ATR), atr, channel: {...}|None}.

    Pure arithmetic, stdlib only; < 2 ms on 500 daily bars (C1.5)."""

def value_on(line: dict, times: list[str], date: str) -> float:
    """The line's price on ``date``. A date inside ``times`` -> its bar index; a
    later date -> last index + the number of Mon-Fri dates after the last bar up
    to and including ``date`` (holidays ignored ON PURPOSE: the chart's whitespace
    tail, _price_chart.html l.2757-2766, counts exactly the same slots, so the
    number the card quotes is the pixel the chart draws)."""

def bounce(bars: list[dict], line: dict) -> dict | None:
    """The LATEST candle as a pin bar / engulfing AT ``line`` (support_bounce's
    candle rules, tolerances and volume read, the level being the line's value on
    that bar). {time, kind: 'pin'|'engulf', low, line_value, vol_ratio, vol_high,
    vol_projected} or None. Mirrored (shooting star / bearish engulfing) when
    line['direction'] == 'down' - see C1.6."""

def overlay(line: dict | None, bounce: dict | None = None) -> dict | None:
    """The ``chart_trendline`` dict for _price_chart.html (C4.4). None -> no line."""
```

#### C1.2 Constants (all ATR-relative; the dollar never appears)

| Constant | Value | Why |
|---|---|---|
| `LOOKBACK` | `sb.LOOKBACK` = 252 | one year of pivots, the same window the support detector seeds from (l.103) |
| `PIVOT`, `REACT_BARS`, `REACT_ATR` | from `support_bounce` (3 / 10 / 1.0) | a pivot is a pivot only if it reacted ≥ 1 ATR within 10 bars on both sides — reused through `_swings` |
| `MAX_PIVOTS` | 20 | the most recent 20 qualifying pivots; caps pairs at 190 (C1.5). A year of daily bars yields 10-25 reaction-tested swing lows; older ones past 20 are history |
| `MIN_SPAN` | 15 bars | two pivots a fortnight apart do not define a direction |
| `TOUCH_ATR` | `sb.TOL_ATR` = 0.35 | a pivot within 0.35 ATR of the line is on it — the same line thickness the support detector uses (l.108) |
| `BREAK_ATR` | `sb.BREAK_ATR` = 0.5 | a close more than 0.5 ATR through the line = broken (OPTIONS_MODULE_DESIGN §6d) |
| `PIERCE_MAX_ATR` | 1.0 | cheap reject: a pivot LOW more than 1 ATR under the line means the line runs through the price action, not under it (closes decide validity; this only prunes) |
| `MIN_SLOPE_ATR` | 0.01 ATR/bar | below this the object is horizontal and belongs to `support_bounce`; 2.5 ATR per year |
| `MIN_RISE_ATR` | 2.0 ATR over the span | a line that has climbed less than two days' range since its first touch is noise, whatever its slope (added after the flat-walk synthetic case produced a 9-touch near-horizontal line without it — C6.1) |
| `MAX_SLOPE_ATR` | 0.25 ATR/bar | steeper = a blow-off, not a line to put a short strike under (62 ATR a year) |
| `RECENT` | `sb.MIN_SEP` = 5 bars | pivots must sit ≥ 5 bars before today (the current test is not a touch); a break inside these 5 bars is REPORTED (`broken=True`) rather than discarded, because "closed through the line yesterday" is the warning the recommender needs |
| `MIN_BARS` | `sb.MIN_BARS` = 60 | |

#### C1.3 Algorithm

```
find(bars, direction, at):
  n = len(bars); if n < MIN_BARS -> None
  highs, lows, closes = floats from bars (KeyError/ValueError -> None)
  atr = sb.atr_series(highs, lows, closes)         # Wilder 14, None until bar 14
  a = atr[-1]; if not a -> None
  sgn = +1 (up) | -1 (down)                           # one code path, prices signed
  end = n - 1 - RECENT ; lo_i = max(PIVOT + 14, n - 1 - LOOKBACK); if end <= lo_i -> None
  # 1. candidate pivots: support_bounce's reaction-tested swings
  sw = sb._swings(highs, lows, atr, lo_i, end)        # (idx, price, 'low'|'flip')
  piv = [(i, p) for i, p, k in sw if k == ('low' if up else 'flip')][-MAX_PIVOTS:]
  if len(piv) < 2 -> None
  # 2. the break series, precomputed once: sgn*close + 0.5*ATR must stay >= sgn*line
  cb[i] = sgn*closes[i] + BREAK_ATR*(atr[i] or a)
  best = None
  # 3. every ordered pair (a before b)
  for (ia, pa), (ib, pb) in pairs(piv):
      span = ib - ia;                      if span < MIN_SPAN: continue
      slope = (pb - pa) / span
      if not MIN_SLOPE_ATR <= sgn*slope/a <= MAX_SLOPE_ATR: continue
      if sgn*(pb - pa) < MIN_RISE_ATR*a: continue
      # 4. touches among the pivots from ia onward; cheap envelope reject
      touches = []
      for (ik, pk) in piv with ik >= ia:
          d = sgn*(pk - (pa + slope*(ik - ia))); ak = atr[ik] or a
          if d < -PIERCE_MAX_ATR*ak: reject pair
          if |d| <= TOUCH_ATR*ak: touches.append((ik, pk, |d|/ak))
      # 5. distinct visits: touches closer than sb.MIN_SEP bars are ONE touch (keep the nearer)
      if len(distinct(touches)) < 2: continue
      # 6. validity: first close through the line by > BREAK_ATR, from ia to today
      broken_at = first i in [ia, n) with cb[i] < sgn*(pa + slope*(i - ia))   # early exit
      if broken_at is not None and broken_at <= end: continue                 # history
      # 7. score; lexicographic
      rank = (n_touches, span_bars = n-1-ia, last_touch_idx, -mean_residual, -ia)
      keep the max
  if best is None -> None
  # 8. outputs
  value_today = pa + slope*(n-1-ia)
  warning = sgn*(closes[-1] - value_today) < 0 and broken_at is None
  value_at = {d: value_on(line, times, d) for d in at}
  channel = _channel(...)                                                      # C1.6
```

Scoring order, as the brief fixes it: most touches (3+ preferred), then longest span, then most
recent touch; ties then go to the line with the smallest mean residual (the one the pivots fit
best), then the older `p1` (deterministic for identical inputs).

Why pivots only for touches, closes only for validity: a trader counts the lows that *turned*
at the line (the swings), while a line is *broken* by a close, not a wick. A spring through the
line that closes back above it is still a valid line — exactly `support_bounce`'s spring rule
(docstring l.48-52).

`i1` is in the output so `value_on` works from unrounded `slope_per_bar` (6 dp) and
`p1.price` (2 dp): the worst drift over 250 bars is 250 × 5e-7 — nothing.

#### C1.4 Failure modes

| Case | Result | What the UI shows |
|---|---|---|
| < 60 bars, no ATR, < 2 qualifying pivots | `None` | no line, no chip; the headline sentence does not mention a trend line |
| every pair rejected (no slope / rise / all closed through) | `None` | same — the EMA stack still carries the trend verdict |
| best line closed through within the last 5 bars | dict with `broken=True`, `last_break` | dashed line, axis label `Trend ×3 · broken 2026-10-02`; recommender: warning ("the trend line gave way on Oct 2") — a credit idea under it is **rejected with that reason** (cross-part rule for Part B) |
| latest close under the line by ≤ 0.5 ATR | `warning=True` | solid line; chip tooltip "today closed under the line — not yet broken" |
| direction does not match the stack (caller asked "up" in a downtrend) | usually `None` or a short 2-touch line | the caller only asks for the direction the EMA20/50 order gives (C1.7) |
| non-numeric / NaN prices | `None` (caught) | — |

#### C1.5 Complexity, cap and measured cost

`_swings` is O(N·PIVOT) once (≈ 250 × 7 comparisons). Pairs ≤ C(20,2) = 190; each pair screens
≤ 20 pivots (O(P)) and only the survivors pay the O(span) close scan, which exits at the first
break. Worst case (a smooth, strong trend where every pair survives) ≈ 190 × 250 = 47 500 float
comparisons. Measured on the laptop, Python 3.12, prototype with these exact rules
(scratchpad `proto_trend_line.py`): 3-touch synthetic 300 bars 0.9-1.4 ms; broken 1.0 ms;
flat walk 0.4 ms; downtrend 0.7 ms; smooth worst case median 0.91 ms / max 1.27 ms over 20
runs; 500 bars (a 2y fetch) median 0.74 ms / max 1.25 ms. The 5 ms budget holds with 3× headroom;
`MAX_PIVOTS = 20` is the hard cap that keeps it there (a pathological series with 60 pivots would
otherwise cost 9×).

#### C1.6 Mirrors: the downtrend line and the channel

| | Uptrend line (`"up"`) | Downtrend line (`"down"`) |
|---|---|---|
| pivots | `_swings` kind `"low"` (swing lows) | kind `"flip"` (swing highs — the same reaction test, l.199-201) |
| slope | `slope/ATR` in [+0.01, +0.25] | in [−0.25, −0.01] (`sgn = −1` folds both into one check) |
| broken | a close < line − 0.5 ATR | a close > line + 0.5 ATR |
| warning | close < line | close > line |
| bounce candle | `sb.is_pin_bar(o,h,l,c)` / `sb.is_engulfing(prev, cur, a)` | the same functions on NEGATED prices: `is_pin_bar(-o, -l, -h, -c)` is a shooting star and `is_engulfing((-po,-pl,-ph,-pc), (-o,-l,-h,-c), a)` a bearish engulfing (the wick ratios and the body test are symmetric under negation — verified term by term) |
| channel | parallel line through the swing HIGHS after `p1` | parallel line through the swing LOWS |

Channel: with the chosen slope, `off = max over opposite-side pivots k ≥ i1 of sgn·(p_k − line(i_k))`
(the furthest extreme defines the parallel); its touches are the opposite-side pivots within
`TOUCH_ATR` of `line + sgn·off`; a channel exists when ≥ 2 touch. Output
`{offset, n_touches, touches, value_today, width_atr}`. `width_atr = off / ATR` is the range the
iron condor would frame in a *drifting* market (C2 covers the flat one); a channel narrower than
`range_box.MIN_WIDTH_ATR` (2.0) is reported but not usable for a condor. Cost O(P).

#### C1.7 How the setup detector uses it — `ema_setup.analyze()`

Insert after the `sup` block (`ema_setup.py` l.364-371), inside its own `try` (a detector must
never take the setup down, same comment as l.370):

```python
    # t1: the automatic trend line (services/trend_line.py) in the direction the
    # EMA20/50 order gives, and the latest candle bouncing off it.
    tl = tl_bounce = None
    try:
        tl = trend_line.find(bars, "up" if e20[-1] > e50[-1] else "down")
        if tl and not tl["broken"]:
            tl_bounce = trend_line.bounce(bars, tl)
    except Exception:  # noqa: BLE001
        tl = tl_bounce = None
```

| Change | Where | Detail |
|---|---|---|
| new fields | return dict l.419-431 and `_blank()` l.236-247 | `"tl": tl, "tl_bounce": tl_bounce` |
| new condition key | `COND_KEYS` l.123 → add `"t1"` after `"s1"`; `COND_LABELS` l.124; `COND_DEFAULT` l.140 (`True` — same 2y fetch, costs nothing extra, like s1); `COND_WEIGHT` l.142 (`"t1": 110`, equal to s1: a bounce off a 3-touch line is the same grade of setup as a bounce off a 3-touch shelf) | label: `("Trend-line bounce", "TREND-LINE BOUNCE — the debit-family setup, all required: EMA20 above EMA50 (daily); an automatic trend line under the lows with ≥ 3 touches in the last year that price has never closed more than half an ATR through; the most recent daily candle is a bullish pin bar or engulfing candle whose low tested that line; high volume for this ticker. Click the ticker: the chart draws the line, its touches and the bounce candle. A line closed through within the last week shows as broken.")` |
| `conditions()` l.433-452 | add `"t1": bool(setup.get("uptrend") and (setup.get("tl") or {}).get("n_touches", 0) >= MIN_TOUCHES_SETUP and setup.get("tl_bounce") and setup["tl_bounce"].get("vol_high"))` with `MIN_TOUCHES_SETUP = 3` (the "3+ preferred" of §6d made a gate for the *setup*; a 2-touch line is still drawn, faded) | mirrors `s1` l.450 |
| `rank()` l.465-583 | chip after the `sup` chip: `{"t": f"trend-line bounce {tl['value_today']:g} · ×{n} · {kind} · {vtxt}", "k": "tl"}`; near miss (volume not high) → `"k": "tlx"`, `S1_NEAR_MISS` score; a line with `broken=True` gets `{"t": f"trend line broken {tl['last_break']}", "k": "tlb"}` and no score | chip colours: amber (`bg-amber-500/15 text-amber-300`) for `tl`, `bg-amber-500/10` for `tlx`, rose for `tlb` — added where `supx` is handled: `_ivscan_list.html` l.88, `_curated_list.html`, `_sector_basket.html`, `_sector_symbols.html`, plus the light-theme ink in `base.html` (the v4.112 lesson) |
| the bounce test | `trend_line.bounce()` | identical gates to `sb.find` l.386-414 (shape gate first, ATR *before* the pattern: `atr[-2]` for a pin, `atr[-3]` for an engulfing), then with `lv_i = value_on(line, times, bars[i].time)` per bar: `test_low` (pin: its low; engulf: the lower of the two lows, each measured against the line on ITS bar) within `[lv − PIERCE_ATR·a, lv + REACH_ATR·a]`; close ≥ lv − TOL_ATR·a; approached: a high ≥ `REACT_ATR·a` above the line in the `REACT_BARS` before the pattern (l.456's rule); no close in that window more than `BREAK_ATR` under the line (l.458's rule); `sb.volume_read(bars, pat_start)` for the volume |
| the warning | `tl["warning"]` / `tl["broken"]` | the headline sentence (Part B) appends "— but today closed under the line" / "— the line broke on {date}"; the chart draws it dashed |
| expiries | `find(..., at=expiries)` | `analyze()` does not know the expiries; the recommender (Part B) calls `trend_line.value_on(setup["tl"], times, expiry)` per candidate expiry — `times = [b["time"] for b in bars]` — to get "the line's value at expiry = where the short strike sits under" |

`needs_deep()` is untouched: `t1` reads the same 2-year bars as `s1`.

### C2. Range / resistance detector — `app/services/range_box.py`

#### C2.1 Contract

```python
def find(bars: list[dict], *, emas: tuple[list, list, list] | None = None) -> dict | None:
    """The horizontal RANGE price is trading in now: a lower edge and an upper edge,
    each a level support_bounce would accept (reaction-tested swing seeds, every bar
    read for touches, distinct visits, lived-through invalidation), each touched
    >= MIN_TOUCHES times, both still active, with the last close inside.

    ``emas`` = the daily (EMA20, EMA50, EMA200) series ema_setup.analyze already
    computed (l.276), so the sideways verdict costs nothing extra; None -> the
    range is returned with ``sideways=None`` ("could not be judged").

    Returns {low, high, zone_low: [lo, hi], zone_high: [lo, hi], atr, width_atr,
    touches_low: [{time, price, kind}], touches_high: [...], n_low, n_high,
    since: date (the older of the two first touches), age_bars, last_touch: date,
    pos_pct (0 = on the low, 1 = on the high), inside_bars, stack_flat: bool|None,
    ema200_inside: bool|None, sideways: bool|None, reasons: [str]} or None."""

def overlay(rng: dict | None) -> dict | None:
    """The ``chart_range`` dict for _price_chart.html (C4.4)."""
```

#### C2.2 Reuse by negation — no mirrored copies of `_members` / `_touches`

`support_bounce` only knows "support": lows within the zone, closes that did not fall through,
levels price *lived under*. Resistance is the same rule with the sign flipped, so the upper edge
is read by running the **same functions on the negated series**:

```
neg = lambda xs: [-x for x in xs]
# swings: a swing HIGH of price is a swing LOW of -price; kinds come back swapped:
sw_hi = sb._swings(neg(lows), neg(highs), atr, lo_i, end)   # 'low' = swing high, 'flip' = swing low (old support)
# members / touches of an upper level H:
mem = sb._members(-H, tol, flips_hi, neg(lows), neg(highs), neg(closes), lo_i, end)
tch = sb._touches(mem, neg(lows), neg(highs), neg(closes), atr, -H, tol, end)
# prices come back negated: touch price = -p
```

Checked term by term: `_members`' "low within the zone, close not through it" becomes "high within
the zone, close not above it"; `_touches`' arrival test (l.243: a high ≥ level + 1 ATR before a
low) becomes "a low ≤ H − 1 ATR before the high" (price came UP to the resistance); its departure
(l.254) becomes "fell ≥ 1 ATR away"; the lived-under invalidation (l.265-272) becomes lived-ABOVE
(closes > H + 0.5 ATR for 3 sessions = broken out); the reclaim (l.278) becomes a close back under
it. The `flip` kind is an old support broken down through — labelled **S→R** on the chart, the
mirror of the existing `R→S`.

#### C2.3 Constants

| Constant | Value | Why |
|---|---|---|
| `LOOKBACK`, `PIVOT`, `MIN_SEP`, `REACT_*`, `TOL_ATR`, `SEP_ATR`, `BREAK_ATR`, `BREAK_BARS` | from `support_bounce` | one definition of a level in the whole app |
| `MIN_TOUCHES` | 2 per edge | OPTIONS_MODULE_DESIGN §5.2 iron condor: "a range with both edges touched ≥ 2 times" |
| `MIN_WIDTH_ATR` | 2.0 | narrower and the condor's short strikes (outside the range) sit inside one day's move of spot — no premium worth selling; also the floor for a usable channel (C1.6) |
| `MAX_WIDTH_ATR` | 10.0 | wider is not "sideways" at the option's horizon; it is two trends |
| `ACTIVE_BARS` | 60 | each edge's latest touch within the last 60 sessions — a range being traded NOW, not last winter's |
| `INSIDE_BARS` | 15 | the last 15 closes inside `[low − 0.5 ATR, high + 0.5 ATR]` (three weeks of being contained) |
| `STACK_TOL_ATR` | 1.0 | EMA20 and EMA50 within one ATR of each other = braided |
| `SLOPE_BARS` / `SLOPE_TOL_ATR` | 10 / 0.5 | EMA20 moved less than half an ATR in two weeks = going nowhere |
| `MIN_BARS` | 60 | |

#### C2.4 Algorithm

```
find(bars, emas):
  highs/lows/closes/atr as in sb.find; a = atr[-1]; n >= MIN_BARS
  end = n - 1; lo_i = max(PIVOT + 14, n - 1 - LOOKBACK)
  # lower edges: exactly sb.find's level loop (l.434-466) WITHOUT the bounce-candle
  # test and WITHOUT the "established/approached" gates; keep EVERY level with
  # >= MIN_TOUCHES distinct touches instead of only the best:
  lows_lv = []
  for seed in swings(kind 'low'):
      lvl = seed; twice: lvl = mean of swing prices within TOL_ATR*a of lvl     # l.436-438
      mem = sb._members(lvl, tol, flips, highs, lows, closes, lo_i, end); dedupe by member set
      t = sb._touches(mem, highs, lows, closes, atr, lvl, tol, end)
      if len(t) >= MIN_TOUCHES: lows_lv.append((mean(t prices), t, zone))
  highs_lv = the same on the negated series (C2.2), prices negated back
  # pair the edges
  cands = []
  for L in lows_lv, H in highs_lv:
      w = (H.level - L.level) / a
      if not MIN_WIDTH_ATR <= w <= MAX_WIDTH_ATR: continue
      if n-1 - L.last_touch_idx > ACTIVE_BARS or n-1 - H.last_touch_idx > ACTIVE_BARS: continue
      if not (L.level - BREAK_ATR*a <= closes[-1] <= H.level + BREAK_ATR*a): continue
      cands.append(((min(nL, nH), nL + nH, max(last touch idx)), L, H))
  if not cands -> None
  L, H = max(cands)
  inside = count of trailing closes inside [L - 0.5a, H + 0.5a] (stop at the first outside)
  stack_flat = None if emas is None else (|e20[-1]-e50[-1]| <= STACK_TOL_ATR*a and |e20[-1]-e20[-1-SLOPE_BARS]| <= SLOPE_TOL_ATR*a)
  ema200_inside = None if emas is None else (L <= e200[-1] <= H)
  sideways = None if stack_flat is None else (stack_flat and inside >= INSIDE_BARS)
  reasons: ["EMA20/50 braided (0.4 ATR apart)", "EMA20 flat (+0.1 ATR in 10 bars)", "17 closes inside", "EMA200 inside the range"] or the failing one(s)
  pos_pct = (closes[-1] - L) / (H - L)
```

Cost: twice `support_bounce`'s level loop (≤ 30 seeds × (`_members` + `_touches`) each
O(N)) ≈ 5-10 ms per ticker. It runs inside `setup_for` (15-min cache, l.606-628) and the
nightly job — not on a request path, so it is not under the trend line's 5 ms cap.

#### C2.5 Who consumes it (cross-part contract)

| Consumer | Reads | Rule it applies |
|---|---|---|
| iron condor (Part B recommender / picker) | `sideways`, `low`, `high`, `width_atr`, `n_low`, `n_high` | fits only when `sideways` is True; short put strike < `low`, short call strike > `high` (§5.3); reject reason otherwise: "trending, not sideways" / "range only 1.4 ATR wide" |
| calendar | `sideways` or (`stack_flat` and `pos_pct` in [0.35, 0.65]) | "price expected to sit near a strike": the ATM strike nearest the close, which must sit mid-range |
| bear call spread | `high`, `touches_high` | "resistance holding — short strike ABOVE resistance" (the upper edge alone is a resistance even when the lower edge is missing: `find()` also exposes `resistance_only(bars)` returning the best upper level with ≥ 2 touches when no range pairs — same loop, no pairing) |
| bull call spread / diagonal | `high` | the target to cap at / the strike to sell the short call under |
| chart | `overlay(rng)` | two price lines + touch markers (C4.4) |
| `ema_setup.analyze()` | — | stores it as `"rng"`; condition key `"r1"` ("Range", default True, weight 0; informational chip `k: "rng"`: `range 318–346 · ×3/×2 · 2.4 ATR · sideways`) |

#### C2.6 Failure modes

| Case | Result |
|---|---|
| an edge with ≥ 2 touches but the other missing | `None` from `find()`; `resistance_only()` / the support detector still serve the directional spreads |
| price has broken out (last close outside by > 0.5 ATR) | `None` — the range is over; the breakout-retest logic (Part B) takes it from here |
| EMAs not supplied | range returned, `sideways=None`, reasons `["trend not judged"]`; the condor chip reads "range found, trend not judged" |
| width < 2 ATR | `None` (rejected at pairing); reason surfaces only through the condor's rejection text when a narrower pair existed: the module keeps `narrowest_rejected_atr` for that sentence |

### C3. Payoff engine — `app/services/payoff.py`

Pure stdlib. One code path for all ten strategies; the family name only selects the labels, the
analytic max P/L shortcut and the POP flavour.

#### C3.1 Legs model

```python
@dataclass
class Leg:
    right: str        # "C" | "P"
    strike: float
    expiry: str       # "YYYY-MM-DD"
    qty: int          # contracts, +long / -short (a bull put 330/320 x1 = [P330 qty -1, P320 qty +1])
    price: float      # per-share price the leg was (or would be) dealt at: the mid for an idea, the fill for a position
    iv: float | None = None      # decimal; None -> solved from price (C3.3)
    delta: float | None = None   # signed per share, from the chain; used only for the delta-POP
MULT = 100
RISK_FREE = 0.04      # = bull_put.bs_put's default r (l.79) so the two models agree to the cent
```

Family keys (used by the picker, the recommender and this module): `buy_call`, `buy_put`,
`bull_call`, `bear_put`, `leaps`, `bull_put`, `bear_call`, `iron_condor`, `calendar`,
`diagonal`, plus `custom` (the full-chain expander: any legs, numeric everything).
`CREDIT_FAMILIES = {bull_put, bear_call, iron_condor}` → wording "chance of keeping it"; the rest
"chance of profit" (decision 9).

#### C3.2 Functions

| Function | Signature | Does |
|---|---|---|
| `normalise_iv` | `(v) -> float\|None` | Cboe gives decimals (`_portfolio_list.html` l.335 prints `h.iv * 100`), the IBKR bridge gives percent (`bull_put.pl_profile` l.181 divides by 100). Rule: `v > 3.0 → v/100`; `≤ 0`/None → None |
| `implied_vol` | `(price, S, K, T, kind) -> float\|None` | bisection on σ ∈ [0.01, 5.0], 60 iterations (precision 1e-18 — far past the cent); None when `price` ≤ intrinsic (stale quote) |
| `calibrate` | `(legs, spot, today) -> list[Leg]` | per-leg σ in this order: solved from `price` → `normalise_iv(leg.iv)` → the sibling leg's σ → the chain's `iv30` → HV20 (the caller passes `fallback_iv`). Calibrating from the *dealt price* makes the T+0 curve pass through P&L = 0 at spot for a fresh idea, and through the current P&L for an open position (whose legs carry entry prices and whose σ is solved from *today's* mids — see C3.11) |
| `horizon` | `(legs, today) -> (expiry, dte)` | the FRONT expiry = `min(leg.expiry)`; everything "at expiry" is at this date |
| `leg_value` | `(leg, S, days_ahead, today) -> float` | `T = max(dte(leg) − days_ahead, 0)/365`; `T == 0` → intrinsic; else `black_scholes(S, K, T, RISK_FREE, σ, kind).price` |
| `pnl` | `(legs, S, days_ahead, today) -> float` | `Σ qty · (leg_value − price) · MULT` |
| `grid` | `(legs, spot, atr, marker_xs) -> list[float]` | C3.5 |
| `expiry_curve` | `(legs, xs, today) -> list[float]` | `pnl(..., days_ahead = horizon dte)`: single-expiry legs are intrinsic (piecewise linear, exact); back-month legs are BS-valued at the front expiry with their own σ (§6e: "the back leg valued by the model") |
| `curve_at` | `(legs, xs, days_ahead, today)` | T+0 (`days_ahead = 0`) and any T+n |
| `breakevens` | `(xs, ys, legs, today) -> list[float]` | C3.6 |
| `extremes` | `(family, legs, xs, ys) -> dict` | C3.7 |
| `pop` | `(family, legs, spot, sigma_h, T_h, xs, ys) -> dict` | C3.8 |
| `price_at_pnl` | `(legs, target, lo, hi, days_ahead, today) -> float\|None` | bisection on the T+n curve: the stock price at which the position shows `target` dollars (used for the rule stop's price) |
| `build` | `(legs, *, family, spot, atr, today, contracts=1, chart_stop=None, target=None, rule_frac=None, levels=(), sigma_fallback=None, pl_now=None) -> dict` | the JSON of C3.10 |

#### C3.3 Expiry payoff as a piecewise-linear function

For legs that all expire at the horizon, `pnl(S)` is linear between consecutive strikes and on the
two rays; breakpoints = the sorted distinct strikes. The grid therefore **contains every strike
exactly**, so the polyline through the grid IS the function (no sampling error at the kinks).
Two-expiry structures (calendar, diagonal, LEAPS with a short front leg) are smooth, not
piecewise linear: the back leg is `black_scholes(S, K_back, T_back − T_front, …)` at every `S`,
and all downstream numbers are numeric on the dense grid.

#### C3.4 T+0 and T+n curves

`curve_at(legs, xs, 0)` with the calibrated σ per leg, `r = RISK_FREE`, `q = 0`. The caption
(decision 8): *"Today: what the trade is worth if the stock moved there now, at today's implied
volatility."* `pl_profile` (`bull_put.py` l.165-193) already draws a 15-DTE curve for puts only;
`payoff` generalises it and the Options card drops the table in `_options_analysis.html`
l.233-256 in favour of the chart. A T+n curve is available (`days_ahead = n`) but not drawn by
default — the brief keeps two lines.

#### C3.5 The price grid

```
lo = min(min strike, spot, every marker x) − PAD_ATR·atr        PAD_ATR = 2.0
hi = max(max strike, spot, every marker x) + PAD_ATR·atr
xs = 201 evenly spaced points in [lo, hi]  ∪  {all strikes}  ∪  {marker xs}  ∪  {breakevens found}
```
sorted, de-duplicated at 4 dp. ATR pads the view instead of a percent so a $30 stock and a $900
stock get the same two days of room on either side (CLAUDE.md: ticker-relative). For a long
single leg the far ray is unbounded; the grid stops at `hi` and the chart draws an arrowhead
(C4.2). `atr` is the daily ATR(14) from the same bars the setup was read from
(`setup["sup"]["atr"]` / `trend_line` `atr`); the route passes it.

#### C3.6 Breakevens

For every grid interval with a sign change (or an exact zero), linear interpolation between the
two grid points — exact for the piecewise-linear expiry curve because the kinks are grid points.
For a smooth (two-expiry) curve the interpolated root is then refined by 40 bisection steps of
`pnl` on that interval. Output sorted ascending; 0, 1 or 2 values for the ten families
(a custom structure may have more).

#### C3.7 Max profit / max loss

| Family | max profit | max loss | How |
|---|---|---|---|
| bull_put / bear_call | `credit · 100` | `(width − credit) · 100` | analytic — identical to `bull_put.spread_math` l.627-642 (the Positions monitor keeps using that function; this one must agree to the cent — asserted in C6.2) |
| bull_call / bear_put | `(width − debit) · 100` | `debit · 100` | analytic |
| buy_call / buy_put / leaps | **unlimited** (call) / `(K − debit)·100` (put) | `debit · 100` | analytic; `unlimited: true` → the card prints "unlimited" and the chart's right ray ends in an arrowhead |
| iron_condor | `credit · 100` | `(wider wing width − credit) · 100` | analytic (per wing, worst wing) |
| calendar / diagonal | numeric: `max(ys)` on the grid, at `x = argmax` | `net debit · 100` | analytic for the loss (a long back-month leg cannot be worth less than 0 when the front expires), numeric for the profit — labelled "≈ modelled" |
| custom | numeric both; a non-zero ray slope → `unlimited`/`undefined` flags | | |

Every analytic value is cross-checked against the numeric one in tests (C6.2); if they differ by
more than $0.01 the numeric wins at runtime and a `warnings: ["max loss recomputed numerically"]`
entry is added (never silently).

#### C3.8 POP per family

Two numbers are always computed; the family picks which one the big label shows, the other sits
in the tooltip.

| Number | Formula | Shown as the label for |
|---|---|---|
| `pop_delta` | credit vertical: `1 − |Δ_short|` (exists: `bull_put._pair` l.251 `pop_est`); iron condor: `1 − |Δ_short put| − |Δ_short call|`; debit vertical: `|Δ_long| − |Δ_short|` is NOT a probability — not computed (None) | bull_put, bear_call, iron_condor → "chance of keeping it" |
| `pop_model` | risk-neutral `P(S_T ∈ profit region at the horizon)`: with `F(x) = N((ln(x/S) − (r − σ²/2)T) / (σ√T))` (`black_scholes.norm_cdf`), sum `F(x_i) − F(x_{i−1})` over every grid interval whose two endpoints are both > 0, plus the tails (`F(x_0)` if the left ray profits, `1 − F(x_last)` if the right one does); `σ` = the ATM IV of the horizon expiry (the chain leg nearest spot; fallback `iv30`, then HV20), `T` = horizon DTE / 365 | buy_call, buy_put, bull_call, bear_put, leaps, calendar, diagonal, custom → "chance of profit" |

Both carry the existing caveat (`black_scholes.py` docstring l.12-15, `_bs_calc.html` l.65-67):
risk-neutral is a pricing convention, not a forecast. For a debit idea managed by the chart
stop/target the card adds "(held to expiry)" after the number — the exit comes earlier.

#### C3.9 Stops, target and R

| Marker | x (stock price) | y read | Families |
|---|---|---|---|
| chart stop | `chart_stop` from the setup (support bounce: under the bounce low; trend-line bounce: under the line; Curated convention: entry − 1 ATR) | `pnl_today = pnl(legs, stop, 0)` AND `pnl_expiry` (tooltip); the label quotes **today** (the stop is hit within days — the broker screen shows the T+0 value, not the expiry one) | all |
| rule stop | the price where the **today** curve reaches `−rule_frac · max_loss` (`price_at_pnl`), `rule_frac` = `trade_prefs.read(user)["loss_fraction"]` (l.85-86, default `bull_put.LOSS_STOP_FRACTION` 0.20) | a HORIZONTAL dashed line at that P&L, plus a vertical marker at the price | credit families only (CREDIT_FAMILIES); for debit families the member's rule is the chart stop |
| target | `target` from the setup (resistance / 2R) | `pnl_today(target)`; expiry value in the tooltip | debit families; for credit families the target is the P&L line `+profit_target · credit` (`trade_prefs` l.87-88, default 50%) — drawn horizontal |
| now | `spot` | 0 for an idea, `pl_now` for a position (the dot) | all |
| levels | support / resistance / trend line value at the horizon expiry (`trend_line.value_on`) / range edges | — (vertical, labelled) | from the setup |

R (one number per structure, `units.r_dollars`, per contract):

| Families | R = | Why |
|---|---|---|
| bull_put, bear_call, iron_condor | `rule_frac · max_loss` (`bull_put.spread_math` l.642 `risk_20pct`) | the sizing rule already divides the risk budget by this (`_pair` l.228) |
| buy_call, buy_put, bull_call, bear_put, leaps, calendar, diagonal | `|pnl_today(chart_stop)|`, or `max_loss` when no chart stop | the Curated convention: SL = 1 ATR, PT = 2R; R is what the stop costs **today** |

The $/R toggle divides every y (curves, hlines, marker reads, max P/L) by `r_dollars`; the server
renders either (`units=$|R`), the client never recomputes.

#### C3.10 The JSON the chart receives

```json
{
  "family": "bull_put", "label": "Nov 20 330/320 put", "symbol": "LRCX",
  "spot": 349.20, "atr": 11.5, "contracts": 1, "today": "2026-10-03",
  "horizon": {"expiry": "2026-11-20", "dte": 48},
  "legs": [{"right": "P", "strike": 330, "expiry": "2026-11-20", "qty": -1, "price": 5.70, "iv": 0.2797, "iv_source": "solved"},
           {"right": "P", "strike": 320, "expiry": "2026-11-20", "qty":  1, "price": 3.60, "iv": 0.2886, "iv_source": "solved"}],
  "xs": [297.0, 297.375, "...", 372.0],
  "expiry": [ -790.0, "..." ],
  "today":  [ -612.4, "..." ],                 // null when no σ could be found (C3.12)
  "breakevens": [327.90],
  "max_profit": 210.0, "max_loss": -790.0, "unlimited_profit": false, "unlimited_loss": false,
  "pop": {"label": "chance of keeping it", "value": 0.745, "basis": "1 − short delta 0.255",
          "model": 0.729, "model_basis": "lognormal, σ 28.4%, 48 d"},
  "markers": [
    {"x": 349.20, "label": "now 349.20", "kind": "now"},
    {"x": 327.90, "label": "breakeven 327.90", "kind": "breakeven"},
    {"x": 338.00, "label": "stop 338 · ≈ −$102 today", "kind": "stop", "y_today": -102.1, "y_expiry": 210.0},
    {"x": 332.72, "label": "rule stop ≈ 332.7", "kind": "rule_stop", "y_today": -158.0},
    {"x": 340.00, "label": "support 340", "kind": "level"},
    {"x": 336.10, "label": "trend line at expiry 336.1", "kind": "level"},
    {"x": 330.00, "label": "short 330", "kind": "strike"}, {"x": 320.00, "label": "long 320", "kind": "strike"}
  ],
  "hlines": [
    {"y": -158.0, "label": "rule stop −$158 (20% of max loss)", "kind": "rule_stop"},
    {"y": 105.0,  "label": "take profit +$105 (50% of credit)", "kind": "target"},
    {"y": 210.0,  "label": "max profit", "kind": "max_profit"}, {"y": -790.0, "label": "max loss", "kind": "max_loss"}
  ],
  "dot": null,                                   // {"x": spot, "y": pl_now} on the Positions page
  "units": {"mode": "$", "per": "contract", "r_dollars": 158.0, "r_basis": "20% of max loss"},
  "caption": "Today: what the trade is worth if the stock moved there now, at today's implied volatility.",
  "warnings": []
}
```
`xs`, `expiry`, `today` are parallel arrays. `kind` ∈ `now | breakeven | stop | rule_stop |
target | level | strike`. Marker `y_*` fields exist only where a read makes sense.

#### C3.11 Where it is called

| Caller | Legs come from | `pl_now` / σ |
|---|---|---|
| the Options card (Part D): `GET /options/{symbol}/payoff?sig=<option_signal.id>&strategy=bull_put&rank=0&units=$` | the pick stored in `option_signal.picks[strategy][rank]` (Part B's shape: it must carry `legs` in the C3.1 shape with the mid as `price` and the chain `iv`/`delta`) | idea: `pl_now=None`, σ solved from the mids |
| the full-chain expander ("what if this strike"): `POST /options/{symbol}/payoff` body `{family, legs:[...], units}` | the request (re-validated: strikes on the chain via `option_quotes.leg`, qty ∈ [−10, 10], prices > 0) | same |
| the Positions page: `GET /options/payoff?spread=<option_spreads.id>` | `OptionSpread` row (short/long strikes, expiry, credit, contracts → legs with the ENTRY prices) + `spread_monitor.snapshot` (l.42-157): σ solved from the current mids (`short_leg`/`long_leg` l.128), `pl_now = snap["pl"]` (l.146) | `dot = {x: snap.spot, y: snap.pl}`; `chart_stop` from the row's `lines` when set |
| the Telegram push (step 5) | the same `build()` dict → the numbers in the sentence; the SVG is not sent in v1 | |

Routes are thin: load legs → `payoff.build(...)` → `templates.TemplateResponse("_payoff_chart.html", {"po": result, ...})`.

#### C3.12 Failure modes

| Case | Result | UI |
|---|---|---|
| no σ for a leg (no chain, price ≤ intrinsic, no fallback) | `today: null`, `warnings: ["today line needs a quote"]`, `pop.model` None | expiry line only; caption replaced by the warning in amber; the stop marker reads the expiry value and says "at expiry" |
| horizon expiry in the past | `error: "expired"` | the partial renders the message, no chart (Positions page: the row already says EXPIRED, `spread_monitor` l.87-95) |
| `credit ≤ 0` or `width ≤ 0` for a credit family | `error: "no edge: the spread pays nothing"` (the picker never emits one — `bull_put._pair` l.210-215 — this guards the expander) | message |
| strike not on the chain (expander) | 400 with the nearest listed strikes, the way `spread_monitor` names expiries (l.118-123) | inline error under the form |
| unbounded max profit | `unlimited_profit: true`, `max_profit: null` | "unlimited" text, arrowhead |
| `contracts == 0` (NLV not told) | per-contract numbers, `units.per = "contract"` | legend "per contract — set your account size in My rules to see position totals" |
| ATR missing | `PAD` falls back to `0.05 · spot` with a warning | — |
| `r_dollars == 0` (a stop above the entry on a debit) | R toggle disabled, warning "R undefined: the stop does not lose money" | toggle greyed |

### C4. Rendering

#### C4.1 Decision: a server-rendered inline SVG, not a second lightweight-charts instance

| Consideration | lightweight-charts 4.2.0 (loaded at `_price_chart.html` l.323) | inline SVG from Jinja |
|---|---|---|
| x-axis | **time only**. A price axis means fake timestamps per grid point, `timeScale.tickMarkFormatter` and `localization.timeFormatter` hacks, and the crosshair label still formats as a date | native: x = price |
| HTMX swap when another strike is clicked | a new `createChart` per swap, teardown of the old instance, the init race the price chart already fights (`REFRESH`, l.364-372) | the partial is just replaced |
| light / dark | colours are baked at `createChart` (`_light` at l.2391-2394) → re-create on toggle | CSS variables; the theme toggle (`base.html` l.518-533) recolours it live |
| filled profit / loss zones | `addBaselineSeries` could do it, but not clipped at breakevens with two fills AND two lines | two `<path>`s |
| vertical markers with labels | HTML badges positioned by `timeToCoordinate`, re-placed on every pan/zoom (the EXP badge machinery, l.2779-2805) | `<line>` + `<text>` |
| Telegram / print later | screenshot only | the same SVG → PNG |
| cost | ~45 KB script already loaded, but a second instance per card | ~6 KB of markup |

The price chart stays lightweight-charts (it IS time-based); the payoff pane is SVG. No pan/zoom
is needed on a payoff chart — the grid already frames the strikes, spot and the stops (C3.5).

#### C4.2 Visual spec

Tokens added to `base.html` `:root` (dark, after `--tv-down` l.108) and the `.light` remap (l.263 on):

| Token | Dark | Light | Use |
|---|---|---|---|
| `--po-exp` | `#1D9E75` | `#1D9E75` | expiry line, 2 px solid; profit-zone edge |
| `--po-today` | `#7F77DD` | `#6A62C9` | today line, 1.5 px, `stroke-dasharray 5 3` |
| `--po-profit-fill` | `rgba(29,158,117,.18)` | `rgba(29,158,117,.12)` | profit zone (teal-50) |
| `--po-loss-fill` | `rgba(224,99,67,.18)` | `rgba(224,99,67,.12)` | loss zone (coral-50) |
| `--po-loss` | `#E0633D` | `#C9502C` | rule-stop / max-loss lines, stop marker |
| text, axes, grid | `--tv-text` (l.100), `--tv-muted` (l.102), `--tv-border` / `--tv-border-2` (l.98-99) | | zero line = `--tv-border-2` 1 px; grid = `--tv-border` at .5 |

Geometry: `viewBox="0 0 640 300"`, margins 8 / 12 / 36 / 48 (t/r/b/l), `width:100%; height:auto`.
Scales: `x = 48 + (S − lo)/(hi − lo)·580`; `y = 8 + (ymax − v)/(ymax − ymin)·256` with `[ymin, ymax]` =
the min/max over both curves and all hlines, padded 6 %. Zero always inside.

| Element | Spec |
|---|---|
| profit zone | for each run of consecutive `expiry[i] > 0`: polygon `[(x_i, y_i)…, (x_end, y0), (x_start, y0)]`, `fill: var(--po-profit-fill)`; runs split exactly at the breakevens (they are grid points) |
| loss zone | the mirror with `--po-loss-fill` |
| expiry line | `<path>` through all points, `stroke: var(--po-exp)`, 2 px, round joins; an arrowhead marker at the right end when `unlimited_profit` |
| today line | `<path>`, `--po-today`, 1.5 px dashed; omitted when `today` is null |
| zero line | horizontal, `--tv-border-2` |
| hlines | 1 px dashed: `rule_stop` and `max_loss` in `--po-loss`, `target` and `max_profit` in `--po-exp`; label right-aligned above the line in `--tv-muted` 10 px |
| vertical markers | 1 px line full height; colour by kind: `now` `--tv-text`, `breakeven` `--tv-muted`, `stop`/`rule_stop` `--po-loss`, `target` `--po-exp`, `level` `#22d3ee` (support cyan, as the price chart) / `#f59e0b` (trend line amber), `strike` `--tv-muted` dotted; label 10 px at the top, alternating top (y 18) / second row (y 30) when two markers are within 56 px, so "now 349.20", "support 340", "stop 338" never overprint |
| the dot | Positions page: `<circle r=4>` at `(spot, pl_now)` in `--po-today` with a white ring |
| axes | x ticks every `nice(atr)` dollars ($1/2/5/10/25) in `--tv-muted` 10 px; y ticks: 5 "nice" steps in $ or R |
| legend (HTML under the SVG, not inside it) | swatch + "At expiry", dashed swatch + "Today", then: `breakeven 327.90 · max +$210 / −$790 · chance of keeping it 74%` and the caption line; on the right the `[$ | R]` toggle |
| hover | a transparent full-plot `<rect>`; `mousemove` → nearest `xs` index → a 1 px cursor line and a tooltip `<div>` (absolute, follows x, clamped inside the pane): `at 338.00 · today −$102 · at expiry +$210` (R mode: `−0.65 R · +1.33 R`); `mouseleave` hides it; touch: `touchmove` does the same |
| the $/R toggle | two buttons; a click re-requests the partial with `units=R` (`hx-get` on the pane's own URL, `hx-target` the pane); the choice is remembered in `localStorage['th.payoff.units']` (a per-viewer convenience, the `TF_KEY` pattern l.374) and sent as the default on the next render |
| no chart cases | the partial renders the `error` / warning line in the same pane height so the card does not jump |

The partial carries its data once, as `<script type="application/json" id="po-<uuid>">` with
`xs`, `expiry`, `today`, `units`; the ~60-line hover script reads it by id (several payoff panes
on one page — the Positions list — must not collide).

#### C4.3 `_payoff_chart.html` (sketch)

```jinja
{# Context: po (payoff.build dict), pane_url (this pane's own GET url, for the units toggle) #}
<div class="po-pane relative" id="po-{{ po.uid }}" data-units="{{ po.units.mode }}">
  <div class="flex items-baseline gap-2 text-[11px] mb-1">
    <span class="text-slate-300">{{ po.label }}</span>
    <span class="text-slate-500">expiry {{ po.horizon.expiry }} · {{ po.horizon.dte }}d</span>
    <span class="ml-auto"> {# $ | R #}
      <button hx-get="{{ pane_url }}&units=$" hx-target="#po-{{ po.uid }}" hx-swap="outerHTML" ...>$</button>
      <button hx-get="{{ pane_url }}&units=R" ... {% if not po.units.r_dollars %}disabled{% endif %}>R</button>
    </span>
  </div>
  {% if po.error %}<div class="h-[300px] flex items-center justify-center text-xs text-amber-300">{{ po.error }}</div>
  {% else %}
  <svg viewBox="0 0 640 300" class="w-full h-auto select-none" role="img" aria-label="risk and reward">
    {% for z in po.svg.profit_zones %}<path d="{{ z }}" fill="var(--po-profit-fill)"/>{% endfor %}
    {% for z in po.svg.loss_zones %}<path d="{{ z }}" fill="var(--po-loss-fill)"/>{% endfor %}
    <line ... zero line .../>  {# hlines, markers, axes: loops #}
    <path d="{{ po.svg.expiry_path }}" fill="none" stroke="var(--po-exp)" stroke-width="2"/>
    {% if po.svg.today_path %}<path d="{{ po.svg.today_path }}" fill="none" stroke="var(--po-today)" stroke-width="1.5" stroke-dasharray="5 3"/>{% endif %}
    <rect class="po-hit" x="48" y="8" width="580" height="256" fill="transparent"/>
  </svg>
  <div class="po-tip hidden absolute ..."></div>
  <div class="legend ...">…</div>
  <script type="application/json" id="po-data-{{ po.uid }}">{{ po.series|tojson }}</script>
  <script>(function(){ /* hover + tooltip; reads #po-data-… */ })();</script>
  {% endif %}
</div>
```
The path strings (`po.svg.*`) are built server-side by `payoff.svg_paths(result)` (pure string
work, 201 points) so the template has no arithmetic. The scrollbar rule (CLAUDE.md) is inherited —
the pane never scrolls.

#### C4.4 Hooks into `_price_chart.html` — the trend line, the channel and the range

| # | Where (v4.126 line) | Change |
|---|---|---|
| 1 | after `var BOUNCE = …` (l.415) | `var TL = {{ (chart_trendline if (chart_trendline is defined and chart_trendline) else None)\|tojson }};` and `var RANGE = {{ (chart_range if (chart_range is defined and chart_range) else None)\|tojson }};` with a comment block like BOUNCE's (l.408-414): read-only, never into the member's drawings |
| 2 | the candle series' `autoscaleInfoProvider` (l.2419-2431) | add `TL && TL.value_today, TL && TL.channel && TL.channel.value_today, RANGE && RANGE.low, RANGE && RANGE.high` to the list at l.2423-2424 — a line under the lows must not scroll off |
| 3 | `snap(t)` (l.2573-2579, inside the BOUNCE IIFE) | hoist to the chart scope right after `var candleMarks = [];` (l.2524) as `function snapTime(t)`; BOUNCE, TL and RANGE call it (one weekly-snapping rule for every overlay) |
| 4 | after the BOUNCE IIFE (l.2594) and before `try { if (candleMarks.length)` (l.2595) | the TL block and the RANGE block below |
| 5 | `SPREAD` block (l.2667-2694) | `SPREAD.legs` (optional): `[{strike, label: 'Short 330P' \| 'Long 400C', kind: 'short'\|'long'}]`; when present it is drawn instead of `short`/`long` (l.2668-2681 hard-code the `'P'` suffix), same colours (short rose `#fb7185` 2 px, long slate `#94a3b8` dashed), and every strike joins `spreadLevels` (l.2686-2691). Old callers (`_spreads_chart.html` l.17, `_portfolio_chart.html` l.29) keep working |
| 6 | the future tail (l.2738-2771) | after `s.setData(bars.concat(ws))` (l.2767): `if (window.__paintTL) __paintTL(far, ws.length);` — extends the trend line to the expiry badge |

The TL block:

```js
      // ---- automatic trend line (services/trend_line.py) ----------------------
      // A line series from p1 to the last candle (and on to the expiry badge once
      // the whitespace tail exists), a marker under every counted touch, the
      // bounce candle, and the parallel channel line. Read-only: a scanner's line
      // must never land in the member's saved drawings (same reason as BOUNCE).
      var tlSeries = null, tlChan = null;
      (function () {
        if (!TL || TL.value_today == null) return;
        try {
          var lastT = bars[bars.length - 1].time, up = TL.direction !== 'down';
          var col = '#f59e0b';
          var opts = { color: col, lineWidth: 2, lineStyle: TL.broken ? 2 : 0, priceLineVisible: false,
                       lastValueVisible: true, title: 'Trend ×' + TL.n_touches + (TL.broken ? ' · broken' : ''),
                       crosshairMarkerVisible: false,
                       autoscaleInfoProvider: function () { return null; } };   // the candle provider frames it
          tlSeries = chart.addLineSeries(opts);
          var p1t = snapTime(TL.p1.time);
          window.__paintTL = function (far, slots) {
            var pts = [{ time: p1t, value: TL.p1.price }, { time: lastT, value: TL.value_today }];
            if (far && far > lastT) {
              var v = (TL.value_at && TL.value_at[far] != null) ? TL.value_at[far]
                    : TL.value_today + TL.slope_per_bar * slots * (WEEKLY ? 5 : 1);
              pts.push({ time: far, value: v });
            }
            if (p1t === lastT) pts.shift();
            tlSeries.setData(pts);
            if (tlChan && TL.channel) tlChan.setData(pts.map(function (p) { return { time: p.time, value: p.value + TL.channel.offset }; }));
          };
          if (TL.channel && TL.channel.n_touches >= 2) {
            tlChan = chart.addLineSeries({ color: col, lineWidth: 1, lineStyle: 2, priceLineVisible: false,
              lastValueVisible: true, title: 'Channel ×' + TL.channel.n_touches, crosshairMarkerVisible: false,
              autoscaleInfoProvider: function () { return null; } });
          }
          __paintTL(null, 0);
          var n = (TL.touches || []).length;
          (TL.touches || []).forEach(function (p, i) {
            var t = snapTime(p.time); if (!t) return;
            candleMarks.push({ time: t, position: up ? 'belowBar' : 'aboveBar', shape: 'circle', color: col,
                               text: 'TL ' + (i + 1) + '/' + n });
          });
          ((TL.channel && TL.channel.touches) || []).forEach(function (p) {
            var t = snapTime(p.time); if (!t) return;
            candleMarks.push({ time: t, position: up ? 'aboveBar' : 'belowBar', shape: 'circle', color: col, text: 'ch' });
          });
          var b = TL.bounce, bt = b && snapTime(b.time);
          if (bt) {
            // the support-bounce arrow may already sit on this candle: one label, not two
            var same = candleMarks.filter(function (m) { return m.time === bt && m.shape === 'arrowUp'; })[0];
            var txt = (b.kind === 'engulf' ? 'engulfing' : 'pin bar') + ' at trend line' + (b.vol_ratio ? ' ×' + b.vol_ratio + ' vol' : '');
            if (same) same.text += ' + trend line';
            else candleMarks.push({ time: bt, position: up ? 'belowBar' : 'aboveBar', shape: up ? 'arrowUp' : 'arrowDown',
                                    color: '#a3e635', text: txt });
          }
        } catch (e) { /* decoration only */ }
      })();
```

Lightweight-charts draws a straight segment between consecutive points in *logical* (bar) space,
so on D the segment from `p1` to the last candle is exact (slope per session) and on W it is exact
at both ends and within a day's holiday-drift between (`snapTime` puts `p1` on the weekly candle
containing it — the same tolerance the BOUNCE markers accept, l.2565-2568). The extension to the
expiry badge uses the server's `value_at[expiry]` when the tail ends on that date, else the
slope × tail slots; both count weekday slots, as `value_on` does.

The RANGE block (same place):

```js
      (function () {
        if (!RANGE || RANGE.low == null || RANGE.high == null) return;
        try {
          s.createPriceLine({ price: RANGE.low,  color: '#22d3ee', lineWidth: 1, lineStyle: 0, axisLabelVisible: true, title: 'Range low ×' + RANGE.n_low });
          s.createPriceLine({ price: RANGE.high, color: '#e879f9', lineWidth: 1, lineStyle: 0, axisLabelVisible: true, title: 'Range high ×' + RANGE.n_high });
          (RANGE.touches_low || []).forEach(function (p, i) { var t = snapTime(p.time); if (t) candleMarks.push({ time: t, position: 'belowBar', shape: 'circle', color: '#22d3ee', text: (p.kind === 'flip' ? 'R→S ' : 'low ') + (i + 1) }); });
          (RANGE.touches_high || []).forEach(function (p, i) { var t = snapTime(p.time); if (t) candleMarks.push({ time: t, position: 'aboveBar', shape: 'circle', color: '#e879f9', text: (p.kind === 'flip' ? 'S→R ' : 'high ') + (i + 1) }); });
        } catch (e) {}
      })();
```
Colours follow the chart's existing grammar: cyan = support (BOUNCE l.2570), fuchsia = resistance
(the curator `R` line l.2645), amber = the new dynamic line, lime = the bounce candle (l.2589).

#### C4.5 Passing the overlays (routes and partials)

Mirror of `sector._bounce_overlay` (`routes/sector.py` l.352-365): the setup is read from the
same `ema_setup.setup_for(sym, deep=…)` cache the list used, so the line on the chart is the line
the chip talks about.

```python
def _chart_overlays(user: User, sym: str, expiry: str | None) -> dict:
    """chart_bounce / chart_trendline / chart_range for the Options card's chart."""
    from ..services import ema_setup as es, trend_line, range_box
    enabled = es.clean_enabled((getattr(user, "prefs", None) or {}).get(SYM_CONDS_PREF))
    st = es.setup_for(sym, deep=es.needs_deep(enabled)) or {}
    tl = st.get("tl")
    if tl and expiry:
        tl = dict(tl, value_at={expiry: trend_line.value_on(tl, st.get("times") or [], expiry)})
    return {"bounce": _bounce_overlay(user, sym),            # existing, reused as is
            "trendline": trend_line.overlay(tl, st.get("tl_bounce")),
            "range": range_box.overlay(st.get("rng"))}
```
(`analyze()` must keep `"times": [b["time"] for b in bars]` on the setup dict for this — a list of
~500 strings, cached with the rest; or the route re-reads `fetch_daily_ohlc(sym)` from the same
15-min cache, which costs one dict lookup.)

The card's chart partial (Part D, e.g. `_options_card_chart.html`) sets, like `_sector_chart.html`
l.52-54 and `_spreads_chart.html` l.17 / `_portfolio_chart.html` l.29:
```jinja
{% set chart_bounce = overlays.bounce %}
{% set chart_trendline = overlays.trendline %}
{% set chart_range = overlays.range %}
{% set chart_spread = {'legs': pick.chart_legs, 'expiry': pick.expiry, 'label': pick.label, 'breakeven': pick.breakeven} %}
{% include "_price_chart.html" %}
```
`trend_line.overlay()` returns `{direction, p1, p2, slope_per_bar, touches, n_touches, value_today,
value_at, broken, last_break, channel, bounce, label}`; `range_box.overlay()` returns `{low, high,
n_low, n_high, touches_low, touches_high, sideways, label}`.

#### C4.6 Dashboard-visibility checklist (CLAUDE.md hard rule)

| Runtime state | Surface |
|---|---|
| a trend line found / its touch count / broken | price chart line + axis label; `tl`/`tlx`/`tlb` chip in the IV Rank list, Sector symbols/basket, Curated; the card's headline |
| a range found / sideways verdict | two price lines + markers; `rng` chip; the condor chip's fit/reject reason |
| payoff numbers | the SVG pane on the card; the Positions row expander with the dot |
| engine failures | chip absent + the headline sentence; the payoff pane's amber line; nothing is CLI-only |

### C5. Worked examples (numbers from the prototype `proto_payoff.py`, `RISK_FREE = 0.04`, today 2026-10-03)

#### C5.1 LRCX bull put 330/320, Nov 20 2026 (48 DTE), credit 2.10 — spot 349.20, ATR 11.5

Quotes assumed: 330P mid 5.70, 320P mid 3.60 (credit 2.10). Calibration solves σ = 27.97 % / 28.86 %;
model deltas −0.255 / −0.174 (the mockup's "74 %" is this 0.255).

| Quantity | Value | Formula / source |
|---|---|---|
| max profit | **+$210** | 2.10 × 100 |
| max loss | **−$790** | (10 − 2.10) × 100 — equals `bull_put.spread_math` |
| breakeven | **327.90** | 330 − 2.10 |
| chance of keeping it | **74 %** (74.5) | 1 − 0.255 (`pop_delta`); model 72.9 % (lognormal σ 28.4 %, 48 d) — the two agree within 2 points |
| P&L at spot today | $0.00 | calibration check |
| chart stop 338 | **≈ −$102 today** (+$210 at expiry) | T+0 curve at 338 — the mockup's "≈ $220" was a placeholder; at these IVs the real read is −$102 (it scales with IV: the today curve steepens as σ rises) |
| rule stop | −$158 = 20 % × 790, reached at **332.72** today | `price_at_pnl(legs, −158, …, days 0)` — 5 points under the chart stop: the chart stop fires first, as the design intends |
| take profit | +$105 at 50 % of credit | `trade_prefs.profit_target_pct` |
| R | $158 | max profit = +1.33 R; loss at 338 = −0.65 R |
| levels marked | support 340; trend line at expiry (from C1 `value_on`, e.g. 336.1) | both above the short strike 330 — the member sees "short strike under support AND under the line" at a glance |

Order of markers on the x-axis, left to right: long 320 · breakeven 327.90 · short 330 · rule stop
332.7 · trend line 336.1 · stop 338 · support 340 · now 349.20. Labels alternate rows (C4.2).

#### C5.2 ISRG Dec 18 2026 400 call at 34.30 (76 DTE) — entry 405.89, SL 394.27, PT 429.14

(The Curated convention gives the levels: ATR 11.62 → SL = entry − 1 ATR; PT = entry + 2R.)
Solved σ = 40.4 %, delta 0.586.

| Quantity | Value | Note |
|---|---|---|
| breakeven at expiry | **434.30** | 400 + 34.30 |
| max loss | **−$3,430** | the premium |
| max profit | unlimited | arrowhead on the right ray |
| loss at SL 394.27 | **≈ −$645 today** (−$3,430 at expiry) | T+0: the call keeps most of its time value when the stock is down 1 ATR tomorrow |
| gain at PT 429.14 | **≈ +$1,496 today** (−$516 at expiry!) | the PT is a *today* number by construction: at expiry 429.14 is under the 434.30 breakeven. The caption on debit cards must say so: "stop and target are read on the today line — this trade is managed by the chart, not held to expiry" |
| theta | −$0.21 / share / day = 0.63 % of premium per day | within the member's "theta/day ≤ 1 % of premium" rule (§5.2) |
| chance of profit (held to expiry) | **34 %** | P(S_T > 434.30), σ 40.4 %, 76 d |
| R | $645 (loss at the chart stop today) | PT = **+2.32 R** — the 2R target in stock terms is 2.3R in option terms because delta rises on the way up |

Markers: SL 394.27 · strike 400 · now 405.89 · PT 429.14 · breakeven 434.30. hlines: max loss only
(no rule stop: debit family).

#### C5.3 A calendar — XYZ at 100, sell Oct 30 2026 100C (27 DTE, IV 34 %) / buy Dec 4 2026 100C (62 DTE, IV 30 %)

Front ≥ back IV, as the catalog requires. BS prices 3.83 / 5.26 → **debit 1.43**.

| Quantity | Value | Note |
|---|---|---|
| horizon | the front expiry, 27 d | the back leg is BS-valued with 35 d left at every S |
| max profit | **≈ +$247 at 100.00** (modelled) | `argmax` on the grid |
| max loss | **−$143** | the debit (analytic); the grid shows −$140 at ±20 % because the back leg still carries cents there — which is why the loss is analytic, not numeric |
| breakevens | **93.90 / 107.88** | numeric + bisection |
| chance of profit | **54 %** | lognormal σ 34 %, 27 d, over [93.90, 107.88] |
| today at spot | $0.00 | calibration |
| if pinned at 100 in 10 days | +$35 | a T+10 read the card can quote: "decay collected so far" |
| expected move at the front expiry | 9.25 | 100 × 0.34 × √(27/365); the breakevens sit at −0.66 / +0.85 of it |

The expiry line here is the dome shape; the today line is nearly flat — exactly the picture that
tells a member "this trade earns by time, not by direction".

### C6. Test plan

#### C6.1 Trend-line engine — synthetic bar series (`tests/test_trend_line.py`, the `mk()` generator of the prototype: weekday dates, noise ±0.6, wicks 0.2-1.2)

| Case | Series | Expected |
|---|---|---|
| 3-touch line | `100 + 0.2·i + 8·sin(π·(i mod 60)/60)`, 300 bars (dips every 60 bars) | a line with **4** touches (Dec 18, Mar 19, Jun 10, Sep 2), slope 0.19/bar, `broken=False`, channel with 4 touches, width ≈ 5.9 ATR |
| broken line | the same, then −1.5/bar from bar 280 | `None` (the break is older than RECENT) — measured 1.0 ms |
| break inside RECENT | the same, a 1.5-ATR close through on bar 297 | the line returned with `broken=True`, `last_break` = that date |
| older break, shallower survivor | the same with a −25 step at bar 240 | a 2-touch line that was never closed through (legitimate: a shallower line held); the setup gate (≥ 3) ignores it |
| tie lows | each dip printed twice on consecutive bars (equal lows) | 4 touches, each tie merged into ONE visit (`MIN_SEP`) |
| no trend | `100 + U(−3, 3)` per bar | `None` — the 9-touch near-horizontal line the first prototype found is rejected by `MIN_RISE_ATR` (rise 0.87 ATR < 2) |
| downtrend mirror | `200 − 0.2·i − 8·sin(…)`, `direction="down"` | 4 touches, slope −0.199, `value_today` under the lows of the highs |
| bounce at the line | the 3-touch series with a hammer printed on the line as the last candle at 1.5× volume | `bounce()` → `{kind:'pin', vol_high:True}`; `t1` met in `conditions()` |
| hammer 1 ATR above the line | | `bounce()` → `None` (not a test of the line) |
| timing | 20 runs each of the smooth worst case (`100 + 0.3·i + 3·sin(i/4)`) and a 500-bar 2y series | median < 1 ms, max < 2 ms (measured 0.91/1.27 and 0.74/1.25) — assert `< 5 ms` |
| edge probes | 30 bars; None volume; NaN close; a zero-range candle | `None` or an honest read, never an exception (the s1 edge list, README v4.124) |
| `value_on` | a line with `p1` 100 bars back and slope 0.2; dates: last bar, +1 weekday, a Saturday, +10 calendar days | last bar = `value_today`; Monday after a Friday = +0.2; Saturday = the same as Friday; +10 calendar days = +8 weekday slots × 0.2 |

Live tickers (week of 2026-10-05, D chart): LRCX, MA (the v4.125 bounce names), NVDA, KO, ISRG —
record `n_touches`, `p1`/`p2`, `broken` and eyeball the line against the candles; a line through
candle bodies = a bug in the close test.

#### C6.2 Range detector

| Case | Series | Expected |
|---|---|---|
| clean range | 160 bars of `100 + 6·sin(π·i/20)` with noise (lows at 94, highs at 106 every 40 bars) | `low ≈ 94`, `high ≈ 106`, ≥ 3 touches each, `width_atr ≈ 4-5`, `sideways=True` when fed flat EMAs, `pos_pct` consistent with the last close |
| S→R flip | a level that was support for 60 bars, broken down, then tested from below | the upper edge's touches include `kind: 'flip'` dated after the breakdown |
| breakout | the clean range, then 10 closes 1 ATR above the high | `None` |
| trending | the 3-touch trend series | `None` (no lower level with 2 touches at one price) or a range rejected by `stack_flat=False` — never `sideways=True` |
| narrow | amplitude 1 ATR | `None` (width floor) |
| negation identity | run `support_bounce._touches` on `(highs, lows, closes, level)` and on `(neg(lows), neg(highs), neg(closes), −level)` for a series mirrored around 0 | identical touch indices and kinds |

#### C6.3 Payoff identities (`tests/test_payoff.py`)

| Identity | Assertion |
|---|---|
| put-call parity | `black_scholes(..., 'call').price − black_scholes(..., 'put').price == S − K·e^{−rT}` to 1e-9 (measured: 9.2077 = 9.2077 on the ISRG inputs) |
| vertical max loss | bull put 330/320 @ 2.10: `max_loss == −(10 − 2.10)·100 == −790`, `max_profit == 210`, breakeven `327.90` — and equal to `bull_put.spread_math` to the cent |
| mirrored vertical | bear call 360/370 @ 2.10 → the same numbers with the breakeven at 362.10; the expiry curve is the bull put's mirrored around spot |
| fresh idea | `pnl_today(spot) == 0` within $0.01 for every family after calibration (measured −0.00) |
| open position | legs at entry prices, σ from today's mids → `pnl_today(spot) == spread_monitor.snapshot()['pl']` within $0.01 |
| long call | expiry curve `== max(S − K, 0)·100 − premium·100` at every grid point; `unlimited_profit` True |
| condor | expiry curve == bull put curve + bear call curve point-wise; `max_loss` = the wider wing − credit |
| calendar | `max_loss == −debit·100` (analytic) and `min(ys) ≥ max_loss − 5` (numeric never below it); two breakevens around the strike |
| POP | credit vertical: `pop_delta` and `pop_model` within 5 points on the LRCX inputs (74.5 vs 72.9); long call: `pop_model == 1 − N(d2)` at `K = breakeven` (equals `studies.black_scholes_grid`'s `prob_profit`, l.165-167) |
| breakevens | every returned x has `|pnl_expiry(x)| < $0.01` |
| grid | every strike and every marker x is present in `xs`; `lo`/`hi` pad by exactly 2 ATR beyond the extreme |
| rule stop | `price_at_pnl(legs, −0.2·790, …)` returns 332.72 and `pnl_today(332.72) == −158` within $0.5 |
| R | LRCX: `r_dollars == 158`; ISRG: `r_dollars == |pnl_today(394.27)| == 645` |
| units | the `R` render's arrays equal the `$` arrays / `r_dollars` |
| failure modes | price below intrinsic → `today` None + warning; expired → `error`; zero credit → `error`; chain strike missing (expander) → 400 with the nearest strikes |

#### C6.4 Browser checklist (dev DB, then Hermes after deploy)

1. Options card, LRCX: the price chart shows `Support 340` (cyan), `Trend ×3` (amber, ends at the
   `EXP` badge), touch circles `TL 1/3…3/3` under the right candles, the pin-bar arrow with
   `+ trend line` when both coincide; strikes `Short 330P` / `Long 320P`; no console errors.
2. Switch to W: the line snaps to weekly candles, markers survive, the extension still reaches `EXP`.
3. Payoff pane: teal expiry line, purple dashed today line, teal/coral zones meeting exactly at
   327.90, the eight markers of C5.1 without overprinting labels, both stop lines labelled, zero
   line, legend sentence; hover at 338 reads `today −$102 · at expiry +$210`.
4. Click the second strike candidate: the pane re-renders in place (HTMX), the price chart's
   strike lines move, the trend line stays.
5. `$ | R` toggle: axis and tooltip in R; reload — still R (localStorage); a debit idea with an
   undefined R shows the toggle disabled with the reason.
6. Theme toggle (sun/moon): the SVG recolours live; the price chart keeps its own behaviour.
7. ISRG card: markers SL / strike / now / PT / BE; the "read on the today line" caption present;
   "unlimited" + arrowhead.
8. Calendar card: dome-shaped expiry line, flat today line, two breakevens, max loss = debit.
9. Positions page: an open spread's row expander shows the pane with the dot at `(spot, P&L)`;
   with Cboe down, the expiry line still renders and the amber "today line needs a quote" shows.
10. Sector symbols / IV Rank list: `tl` chips appear (amber), `tlb` (rose) on a ticker whose line
    broke this week; clicking opens the chart with the dashed broken line.
11. A ticker with no trend line / no range: no line, no chip, no error; the chart is the v4.126 one.
12. Scrollbars: the pane adds none; nothing overrides `base.html`'s invisible-until-hover rule.

README changelog paragraph, to be written in the house style when each release lands, e.g.:
*"Tested: the synthetic series above (3-touch, broken, tie lows, flat walk → None, downtrend
mirror), timings median 0.9 ms / max 1.3 ms; payoff identities (parity 9.2077 = 9.2077, 790 =
width − credit, T+0 at spot = 0.00, condor = two verticals); in the browser (dev DB): LRCX card
with Support 340 + Trend ×3 + strikes, the payoff pane's eight markers and the hover read at 338,
W switch, $/R toggle persisted, theme toggle, ISRG + calendar cards, Positions dot; no console
errors."*

### C7. What this part assumes and needs from the others

Assumptions: the setup dict keeps its single-source role (everything the chart draws comes from
`ema_setup.setup_for`'s cache, including the new `tl` / `rng`); Cboe IV is a decimal and bridge IV
a percent (`normalise_iv` guards both); the chart stop for a credit idea is the stock level under
the bounce low / under the trend line, supplied by the setup detector; `RISK_FREE = 0.04` and `q =
0` for every T+0 read; the Options card always passes an expiry, so the chart's whitespace tail
exists and the trend line reaches the `EXP` badge.

Needs: Part B (recommender / picker) stores each pick's `legs` in the C3.1 shape (`right, strike,
expiry, qty, price = mid, iv, delta`) plus `chart_stop`, `target`, `levels` and the family key, and
calls `trend_line.value_on` for "under the line at expiry" and `range_box` for the condor /
calendar gates with the names in C2.5; Part D (page) owns `GET /options/{symbol}/payoff` and the
card chart partial, passing `chart_trendline` / `chart_range` / `chart_spread.legs` as in C4.5; the
owner of `option_signal` stores `setup = {sup, tl, tl_bounce, rng}` and `payoff` (the C3.10 dict
minus the arrays) as JSON; the Positions page (Part D, step 2) includes the pane with
`?spread=<id>`; Telegram (step 5) reads `pop.label` / `pop.value` and the stop reads from the same
`build()` dict.
