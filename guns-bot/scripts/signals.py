"""Signal computations for guns-bot.

Pure functions: given raw bar data, return decisions. No I/O. Kept small and
unit-test friendly. Wrapped by trade_day.py with the Alpaca fetch layer.

Vocabulary:
- "PM" = pre-market session, defined as 04:00–09:30 ET.
- "RTH" = regular trading hours, 09:30–16:00 ET.
- Bars are dicts with keys: t (datetime), o, h, l, c, v.
"""
from __future__ import annotations

from datetime import datetime, time as dtime
from statistics import median


# ---------- EMAs ----------

def ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1 - alpha) * out[-1])
    return out


# ---------- Pre-market analysis ----------

def split_pm_rth(bars: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split 1-min bars into (pre-market 04:00-09:29:59, RTH 09:30+)."""
    pm: list[dict] = []
    rth: list[dict] = []
    for b in bars:
        t = b["t"]
        local = t.time() if isinstance(t, datetime) else t
        if dtime(4, 0) <= local < dtime(9, 30):
            pm.append(b)
        elif local >= dtime(9, 30):
            rth.append(b)
    return pm, rth


def pm_summary(pm_bars: list[dict]) -> dict | None:
    """Return PM high/low/volume/open/last_close, or None if no PM bars."""
    if not pm_bars:
        return None
    return {
        "pm_high": max(b["h"] for b in pm_bars),
        "pm_low": min(b["l"] for b in pm_bars),
        "pm_volume": sum(b["v"] for b in pm_bars),
        "pm_open": pm_bars[0]["o"],
        "pm_last_close": pm_bars[-1]["c"],
        "n_bars": len(pm_bars),
    }


def is_consolidating_near_pm_high(
    pm_bars: list[dict],
    pm_high: float,
    window_min: int,
    band_pct: float,
) -> bool:
    """True if every bar in the last `window_min` minutes of PM closed within
    `band_pct` of pm_high (i.e. range-bound just under the high). Skips empty
    bar gaps — only the bars that exist must satisfy the band."""
    if not pm_bars or pm_high <= 0:
        return False
    last_t = pm_bars[-1]["t"]
    if not isinstance(last_t, datetime):
        return False
    cutoff = last_t.timestamp() - window_min * 60
    window_bars = [b for b in pm_bars if b["t"].timestamp() >= cutoff]
    if len(window_bars) < max(3, window_min // 5):
        return False
    band = pm_high * (band_pct / 100.0)
    return all(pm_high - b["c"] <= band and b["c"] > 0 for b in window_bars)


# ---------- Setup 1: break of PM high ----------

def stop_loss_distance_dollars(entry_price: float) -> float:
    """Adam Khoo's stop-by-price-band rule (uses the conservative end of each band)."""
    if entry_price < 20:
        return 0.15
    if entry_price < 30:
        return 0.20
    if entry_price < 50:
        return 0.30
    if entry_price < 100:
        return 0.50
    # Above $100 isn't in the deck — extrapolate at 0.6% of price, capped at $1.
    return min(max(entry_price * 0.006, 0.50), 1.00)


def build_setup1_plan(
    symbol: str,
    pm_high: float,
    pm_summary_obj: dict,
    take_profit_R: float,
) -> dict:
    """Compute entry/stop/target levels for Setup 1 (break of PM high).

    Entry rule from deck:
      - Buy-stop 1¢ above PM high
      - Limit 3-5¢ above stop (we use 3¢)
      - Stop-loss by price band
      - TP at 2R-2.5R (caller picks via take_profit_R)
    """
    stop_trigger = round(pm_high + 0.01, 2)
    limit_price = round(stop_trigger + 0.03, 2)
    # Effective entry for sizing/RR — assume worst case (limit_price).
    entry_for_math = limit_price
    sl_dist = stop_loss_distance_dollars(entry_for_math)
    stop_loss = round(entry_for_math - sl_dist, 2)
    take_profit = round(entry_for_math + take_profit_R * sl_dist, 2)
    return {
        "setup": 1,
        "symbol": symbol,
        "entry_stop_trigger": stop_trigger,
        "entry_limit": limit_price,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "risk_per_share": round(sl_dist, 4),
        "pm_high": round(pm_high, 4),
        "pm_volume": pm_summary_obj["pm_volume"],
    }


# ---------- Setup 5: break of first 1-min candle ----------

def evaluate_setup5_first_minute(
    first_min: dict,
    prior_pm_bars: list[dict],
    take_profit_R: float,
    symbol: str,
) -> dict | None:
    """Return a plan dict if the first 1-min RTH candle qualifies for Setup 5,
    else None.

    Deck requirements:
      - first 1-min candle is bullish (close > open)
      - first 1-min candle closes above 9 EMA AND 20 EMA
      - first 1-min candle is "not too big" relative to normal — proxy: range
        ≤ 1.5× the median range of the last 20 PM bars (a reasonable normal-size
        baseline; the deck is qualitative on this point)
    """
    o, h, l, c = first_min["o"], first_min["h"], first_min["l"], first_min["c"]
    if c <= o:
        return None  # not bullish

    # Build EMAs from the trailing PM closes + this first-minute close so the
    # EMA is up-to-date at the bar's close.
    closes = [b["c"] for b in prior_pm_bars[-100:]] + [c]
    if len(closes) < 21:
        return None  # not enough history to compute EMA20
    e9 = ema_series(closes, 9)[-1]
    e20 = ema_series(closes, 20)[-1]
    if not (c > e9 and c > e20):
        return None

    # Size check — last 20 PM bars' median range
    recent_ranges = [b["h"] - b["l"] for b in prior_pm_bars[-20:] if (b["h"] - b["l"]) > 0]
    candle_range = h - l
    if recent_ranges:
        norm = median(recent_ranges)
        if norm > 0 and candle_range > 1.5 * norm:
            return None

    entry_stop_trigger = round(h + 0.01, 2)
    entry_limit = round(entry_stop_trigger + 0.03, 2)
    sl_dist = round(entry_limit - (l - 0.01), 4)
    if sl_dist <= 0:
        return None
    stop_loss = round(l - 0.01, 2)
    take_profit = round(entry_limit + take_profit_R * sl_dist, 2)
    return {
        "setup": 5,
        "symbol": symbol,
        "entry_stop_trigger": entry_stop_trigger,
        "entry_limit": entry_limit,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "risk_per_share": sl_dist,
        "first_min_high": h,
        "first_min_low": l,
        "first_min_close": c,
        "ema9_at_close": round(e9, 4),
        "ema20_at_close": round(e20, 4),
    }


# ---------- Setup 4: first intraday bull flag (thrust + pullback + continuation) ----------

def _atr_from_bars(bars: list[dict], period: int) -> float:
    """Wilder-style ATR over the trailing `period` bars. Returns 0 if not
    enough bars or zero ranges."""
    if len(bars) < period + 1:
        return 0.0
    trs: list[float] = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    tail = trs[-period:]
    return sum(tail) / period if tail else 0.0


def evaluate_setup4_bull_flag(
    intraday_bars: list[dict],
    symbol: str,
    take_profit_R: float,
    *,
    lookback_bars: int = 30,
    thrust_min_atr_mult: float = 2.0,
    pullback_min_retrace: float = 0.30,
    pullback_max_retrace: float = 0.62,
    vol_confirm_mult: float = 1.2,
) -> dict | None:
    """Setup 4 — first intraday bull flag.

    Adam Khoo's deck describes this as a *qualitative* pattern: after a
    push up, price pulls back to the 9 or 20 EMA on the 1-min timeframe
    and then breaks the pullback's high. We encode it as deterministic
    rules so the bot can fire it without human eyes.

    Inputs:
      intraday_bars   - 1-min bars from market open through "now".
                        Must include at least `lookback_bars + 5` bars
                        for the ATR + EMA calculations to be stable.
      symbol          - ticker symbol.
      take_profit_R   - R multiple for the take-profit leg (2.0 typical).

    Rules (all must hold):
      1. THRUST: there's a peak high within the first 2/3 of the window
         such that (peak_high − close at thrust_start) ≥ thrust_min_atr_mult
         × ATR(5) on 1-min bars.
      2. PULLBACK: after the peak, price retraces to between
         pullback_min_retrace and pullback_max_retrace of the thrust.
      3. CONSOLIDATION: the last 3 bars close in the upper half of the
         pullback range (i.e. holding above the pullback midpoint).
      4. EMA HOLD: latest close > EMA(9) AND > EMA(20) on the bar series.
      5. VOLUME CONFIRM: latest bar volume ≥ vol_confirm_mult × the
         20-bar trailing average volume.

    Output (or None if any rule fails):
      A plan dict in the same shape as build_setup1_plan / Setup-5,
      with entry_stop_trigger = max(last 3 highs) + $0.01, stop = pullback
      low − $0.01, target at 2R (or take_profit_R if customised).
    """
    if len(intraday_bars) < lookback_bars + 5:
        return None
    window = intraday_bars[-lookback_bars:]
    closes = [b["c"] for b in window]
    highs = [b["h"] for b in window]
    lows = [b["l"] for b in window]
    vols = [b["v"] for b in window]

    atr5 = _atr_from_bars(window, 5)
    if atr5 <= 0:
        return None

    # Look for the peak in the first 2/3 of the window so we still have
    # bars left for the pullback + consolidation phase.
    search_end = max(3, int(lookback_bars * 2 / 3))
    peak_idx = max(range(search_end), key=lambda i: highs[i])
    peak_high = highs[peak_idx]
    if peak_idx >= lookback_bars - 3:
        return None  # need at least 3 bars after the peak

    # Define the "thrust" leg as ~8 bars before peak (or from window start).
    thrust_start_idx = max(0, peak_idx - 8)
    thrust = peak_high - closes[thrust_start_idx]
    if thrust < thrust_min_atr_mult * atr5:
        return None

    # Pullback: lowest low between peak and now.
    after_peak = window[peak_idx + 1:]
    pullback_low = min(b["l"] for b in after_peak)
    pullback = peak_high - pullback_low
    retrace = pullback / thrust if thrust > 0 else 0.0
    if not (pullback_min_retrace <= retrace <= pullback_max_retrace):
        return None

    # Consolidation: last 3 closes above the pullback midpoint.
    mid = pullback_low + pullback / 2.0
    if not all(c >= mid for c in closes[-3:]):
        return None

    # EMA hold (computed on the window's closes).
    if len(closes) < 21:
        return None
    e9 = ema_series(closes, 9)[-1]
    e20 = ema_series(closes, 20)[-1]
    if not (closes[-1] > e9 and closes[-1] > e20):
        return None

    # Volume confirm.
    if len(vols) >= 20:
        avg_v = sum(vols[-20:]) / 20.0
        if avg_v > 0 and vols[-1] < vol_confirm_mult * avg_v:
            return None
    else:
        avg_v = 0.0

    entry_stop = round(max(highs[-3:]) + 0.01, 2)
    entry_limit = round(entry_stop + 0.03, 2)
    stop_loss = round(pullback_low - 0.01, 2)
    risk_per_share = round(entry_limit - stop_loss, 4)
    if risk_per_share <= 0:
        return None
    take_profit = round(entry_limit + take_profit_R * risk_per_share, 2)

    return {
        "setup": 4,
        "symbol": symbol,
        "entry_stop_trigger": entry_stop,
        "entry_limit": entry_limit,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "risk_per_share": risk_per_share,
        "thrust_dollars": round(thrust, 4),
        "atr5_1min": round(atr5, 4),
        "peak_high": round(peak_high, 4),
        "pullback_low": round(pullback_low, 4),
        "retrace_pct": round(retrace, 3),
        "ema9_at_signal": round(e9, 4),
        "ema20_at_signal": round(e20, 4),
        "vol_ratio": round(vols[-1] / avg_v, 2) if avg_v > 0 else None,
    }


# ---------- Setup 6: closing-bell momentum (not in the original deck) ----------

def evaluate_setup6_closing_breakout(
    intraday_bars: list[dict],
    open_price: float,
    symbol: str,
    take_profit_R: float,
    *,
    min_day_gain_pct: float = 3.0,
    closing_quartile: float = 0.75,
    last_min_vol_mult: float = 2.0,
    consolidation_bars: int = 5,
) -> dict | None:
    """Setup 6 — closing-bell breakout. NOT in Adam Khoo's deck — invented
    for the bot to exploit closing-window volatility (15:50-15:58 ET).

    Forced MOC-style exit means the take-profit is tight: 2R unless the
    bar to close cap intervenes. Caller is responsible for sending an
    exit order before 16:00 ET regardless of TP/SL state.

    Rules (all must hold):
      1. Up ≥ min_day_gain_pct from today's open (the move is real).
      2. Latest close is in the upper closing_quartile fraction of the
         intraday range (high water — not fading into the close).
      3. Last bar volume ≥ last_min_vol_mult × the average of the prior
         30 bars (capital flowing in, not just drift).
      4. Latest close > max(highs of the prior consolidation_bars) − the
         last bar — i.e. just broke a clean consolidation top.
      5. Above EMA(9) AND EMA(20) on the 1-min series.

    Output (or None): plan dict. stop = bar of breakout's low − $0.01.
    target = 2R (or take_profit_R if customised).
    """
    if len(intraday_bars) < 35 or open_price <= 0:
        return None
    last = intraday_bars[-1]
    intraday_high = max(b["h"] for b in intraday_bars)
    intraday_low = min(b["l"] for b in intraday_bars)

    # 1. Up enough on the day
    day_gain_pct = (last["c"] - open_price) / open_price * 100.0
    if day_gain_pct < min_day_gain_pct:
        return None

    # 2. Closing in upper quartile of day range
    rng = intraday_high - intraday_low
    if rng <= 0:
        return None
    pos = (last["c"] - intraday_low) / rng
    if pos < closing_quartile:
        return None

    # 3. Volume burst on last bar
    prior_30 = intraday_bars[-31:-1]
    avg_v = sum(b["v"] for b in prior_30) / len(prior_30) if prior_30 else 0
    if avg_v <= 0 or last["v"] < last_min_vol_mult * avg_v:
        return None

    # 4. Broke prior consolidation high (excluding the current bar)
    prior_consol = intraday_bars[-(consolidation_bars + 1):-1]
    consol_high = max(b["h"] for b in prior_consol) if prior_consol else 0
    if last["c"] <= consol_high:
        return None

    # 5. EMA hold
    closes = [b["c"] for b in intraday_bars]
    if len(closes) < 21:
        return None
    e9 = ema_series(closes, 9)[-1]
    e20 = ema_series(closes, 20)[-1]
    if not (last["c"] > e9 and last["c"] > e20):
        return None

    entry_stop = round(last["h"] + 0.01, 2)
    entry_limit = round(entry_stop + 0.03, 2)
    stop_loss = round(last["l"] - 0.01, 2)
    risk_per_share = round(entry_limit - stop_loss, 4)
    if risk_per_share <= 0:
        return None
    take_profit = round(entry_limit + take_profit_R * risk_per_share, 2)

    return {
        "setup": 6,
        "symbol": symbol,
        "entry_stop_trigger": entry_stop,
        "entry_limit": entry_limit,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "risk_per_share": risk_per_share,
        "day_gain_pct": round(day_gain_pct, 2),
        "range_position": round(pos, 3),
        "vol_ratio_vs_30bar_avg": round(last["v"] / avg_v, 2),
        "consol_high_broken": round(consol_high, 4),
        "ema9_at_signal": round(e9, 4),
        "ema20_at_signal": round(e20, 4),
        "force_exit_at_et": "15:58",
    }


# ---------- Sizing ----------

def position_size(equity: float, risk_pct: float, risk_per_share: float) -> int:
    """Number of whole shares to risk `risk_pct` of equity per trade.
    Returns 0 if the math doesn't yield at least 1 share."""
    if equity <= 0 or risk_per_share <= 0:
        return 0
    dollars = equity * risk_pct
    qty = int(dollars // risk_per_share)
    return max(qty, 0)


# ---------- Spread check ----------

def spread_ok(bid: float, ask: float, max_cents: float) -> bool:
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return False
    return (ask - bid) * 100 <= max_cents
