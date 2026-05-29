# Trend Identification Strategy — Stocks / Equities
## Timeframe: 1 Year · 1 Day Candles
> **Indicators Used:** EMA 20 · EMA 50 · EMA 200

Source: user-provided document (2026-05-29)
Methodology type: mechanical classifier (condition → state)

---

## Overview

Classifies a symbol's daily-chart condition into one of four states
using three exponential moving averages. Primarily used as a bias
filter for intraday setups: the state determines which directional
setups are valid on the day.

| Condition     | EMA Stack                | Price Location  | ADX Proxy          |
|---------------|--------------------------|-----------------|--------------------|
| Uptrend       | EMA20 > EMA50 > EMA200   | Above EMA20     | Expanding spread   |
| Downtrend     | EMA20 < EMA50 < EMA200   | Below EMA20     | Expanding spread   |
| Sideways      | EMA20 ≈ EMA50, mixed     | Between EMAs    | Flat, converging   |
| Consolidation | All EMAs converging      | Tight range near all EMAs | Contracting spread |

---

## 1. Uptrend

### Definition
Price is in a sustained bullish phase with higher highs and higher lows
supported by a bullish EMA alignment.

### EMA Conditions
- EMA20 **above** EMA50
- EMA50 **above** EMA200
- All three EMAs sloping **upward**

### Price Conditions
- Price trading **above EMA20** on most candles
- Pullbacks find support at **EMA20 or EMA50**
- EMA20 and EMA50 spread is **widening**

### Signal Confirmation
- Bullish candle closes **above EMA20** after a pullback → continuation entry
- Price reclaims EMA50 after a dip with volume → re-entry signal

### Invalidation
- Daily close **below EMA50** with EMA20 beginning to flatten or turn down

### Visual Cue
```
Price
  ↑   ●●●●
     ── EMA20 (fast, rising)
    ──── EMA50 (mid, rising)
  ──────── EMA200 (slow, rising)
```

---

## 2. Downtrend

### Definition
Price is in a sustained bearish phase with lower highs and lower lows,
confirmed by a bearish EMA alignment.

### EMA Conditions
- EMA20 **below** EMA50
- EMA50 **below** EMA200
- All three EMAs sloping **downward**

### Price Conditions
- Price trading **below EMA20** on most candles
- Bounces are rejected at **EMA20 or EMA50**
- EMA20 and EMA50 spread is **widening downward**

### Signal Confirmation
- Bearish candle closes **below EMA20** after a bounce → short/exit signal
- Price fails to reclaim EMA50 on a rally → continuation of downtrend

### Invalidation
- Daily close **above EMA50** with EMA20 beginning to flatten or curl up

### Visual Cue
```
  ──────── EMA200 (slow, declining)
    ──── EMA50 (mid, declining)
     ── EMA20 (fast, declining)
  ↓   ●●●●
Price
```

---

## 3. Sideways (Range)

### Definition
Price oscillates within a horizontal range with no clear directional
bias. EMAs are flat and intertwined.

### EMA Conditions
- EMA20 and EMA50 are **close together** (spread < 1–2% of price)
- Both are **flat or undulating** with no clear slope
- EMA200 may still be sloping from a prior trend

### Price Conditions
- Price crosses **above and below EMA20 frequently**
- No sustained move beyond EMA50 in either direction
- Identifiable **support and resistance levels** bracket the range

### Signal Confirmation
- Buy near range **support** when price bounces off EMA50 upward
- Sell / short near range **resistance** when price is rejected downward
- Avoid breakout trades until a close **clearly beyond the range** is confirmed

### Invalidation
- A daily close with strong momentum **outside the range** with EMA20
  crossing EMA50 decisively

### Visual Cue
```
── Resistance ──────────────────────
     ── EMA20 ≈ EMA50 (flat, tangled)
         ●●●● price oscillating
── Support ─────────────────────────
```

---

## 4. Consolidation (Compression)

### Definition
Price is compressing into a tight range after a directional move. All
three EMAs are converging, signalling a high-probability breakout setup
is forming.

### EMA Conditions
- **All three EMAs converging** toward each other
- EMA20–EMA50 spread is **contracting**
- EMA50 approaching EMA200 (or crossing is imminent)

### Price Conditions
- Candle bodies are **shrinking** (decreasing daily range)
- Price is coiling inside a narrowing structure (triangle, flag, wedge)
- Volume is **declining** into the compression

### Signal Confirmation
- Wait for a **high-volume breakout candle** above/below the structure
- EMA20 must **separate from EMA50** in the breakout direction
- Entry on the **retest** of the breakout level or the EMA20 after
  expansion begins

### Invalidation
- Price returns inside the consolidation range after a breakout →
  treat as a failed breakout / fakeout

### Visual Cue
```
         ●●
        ●  ●
       ●    ●   ← price range shrinking
  EMA20 ──┐
  EMA50 ──┤ ← EMAs converging
  EMA200 ─┘
```

---

## EMA Quick Reference

| EMA    | Period   | Role                                               |
|--------|----------|----------------------------------------------------|
| EMA 20 | 20 days  | Short-term momentum, dynamic support/resistance    |
| EMA 50 | 50 days  | Medium-term trend confirmation                     |
| EMA200 | 200 days | Long-term trend bias, institutional reference      |

---

## Condition Decision Tree

```
Is EMA20 > EMA50 > EMA200 (all sloping up)?
  ├── YES → UPTREND
  └── NO
       Is EMA20 < EMA50 < EMA200 (all sloping down)?
         ├── YES → DOWNTREND
         └── NO
              Are all EMAs converging (spread contracting)?
                ├── YES → CONSOLIDATION
                └── NO → SIDEWAYS
```

---

## Implementation Notes

- On a **1Y1D chart**, EMA200 ≈ the full year of data — primary bias filter.
- **Never trade against the EMA200 direction** unless confirmed
  consolidation/reversal signals are present.
- Consolidation vs Sideways differ in **intent**: sideways is stable
  ranging; consolidation is compression before expansion.
- Re-evaluate condition **weekly** or after any daily close that crosses
  a key EMA.

## Implementation Status

| Component                        | Status      | Notes                                      |
|----------------------------------|-------------|--------------------------------------------|
| `classify_trend_ema()`           | Done ✅      | `resources/trend_state.py` v1.0.0          |
| `trend_ema_detail()`             | Done ✅      | Returns EMA values + spread metrics        |
| `classify_symbol_trend(symbol)`  | Done ✅      | Loads daily parquet via bars_store         |
| `classify_universe_trends(list)` | Done ✅      | Batch wrapper; 679-symbol smoke pass       |
| Per-symbol state cache           | Planned     | Pre-market refresh, JSON in ticker_profile |
| Intraday setup bias gate         | Planned     | Filter DITP/GUNS entries by state          |
| Dashboard trend-state column     | Planned     | Scanner table + chart pane pill            |
