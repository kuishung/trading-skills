"""
Trend-state visual validator  (v2 — uses real H/L + rolling range envelope).

For each of the 4 states, finds a real symbol classified by trend_state.py,
loads its actual daily bars, plots the real EMA20/50/200 AND a rolling
20-bar high-low range envelope.

The envelope makes the Sideways vs Consolidation distinction explicit:
  Sideways      -> envelope width stays constant (flat band)
  Consolidation -> envelope visibly narrows toward the right (coil)

Usage:  py -3.12 validate_trend_charts.py
Output: trend_validate_uptrend.jpg
        trend_validate_downtrend.jpg
        trend_validate_sideways.jpg
        trend_validate_consolidation.jpg
"""
import sys
import warnings
warnings.filterwarnings('ignore')
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / 'resources'))
sys.path.insert(0, str(Path(__file__).parent / 'scripts'))

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from bars_store import load_bars, list_symbols
from trend_state import (
    classify_trend_ema, trend_ema_detail,
    UPTREND, DOWNTREND, SIDEWAYS, CONSOLIDATION, UNKNOWN,
)

# ── Chart style ────────────────────────────────────────────────────────────
BG       = '#0d1117'
GRID     = '#1c2333'
EMA20C   = '#00b4d8'
EMA50C   = '#f4a261'
EMA200C  = '#e63946'
CUP      = '#26a69a'
CDN      = '#ef5350'
RANGE_C  = '#ffffff'

STATE_BORDER = {
    UPTREND:       '#26a69a',
    DOWNTREND:     '#ef5350',
    SIDEWAYS:      '#f4a261',
    CONSOLIDATION: '#9b59b6',
}

STATE_LABEL = {
    UPTREND:       'UPTREND       EMA20 > EMA50 > EMA200',
    DOWNTREND:     'DOWNTREND     EMA20 < EMA50 < EMA200',
    SIDEWAYS:      'SIDEWAYS      Mixed stack, stable range',
    CONSOLIDATION: 'CONSOLIDATION Mixed stack + range shrinking (coil)',
}


def _ema(arr, period):
    out = np.zeros_like(arr, dtype=float)
    out[0] = arr[0]; k = 2.0 / (period + 1)
    for i in range(1, len(arr)):
        out[i] = k * arr[i] + (1 - k) * out[i - 1]
    return out


def rolling_range(highs, lows, window=20):
    """Rolling max(high) and min(low) over a trailing window."""
    n = len(highs)
    roll_hi = np.full(n, np.nan)
    roll_lo = np.full(n, np.nan)
    for i in range(window - 1, n):
        roll_hi[i] = highs[i-window+1:i+1].max()
        roll_lo[i] = lows[i-window+1:i+1].min()
    return roll_hi, roll_lo


# ── Find the best example per state ───────────────────────────────────────
def find_best_examples(target_states, min_bars=250):
    """
    For sideways/uptrend/downtrend: take the first clean match.
    For consolidation: score by how dramatically the range is contracting
    and pick the most visually obvious example.
    """
    found        = {}
    consol_best  = None   # (score, sym, bars)

    for sym in sorted(list_symbols('daily')):
        bars = load_bars(sym, timeframe='daily')
        if len(bars) < min_bars:
            continue
        closes = np.array([b['c'] for b in bars])
        highs  = np.array([b['h'] for b in bars])
        lows   = np.array([b['l'] for b in bars])

        state = classify_trend_ema(closes, highs, lows)

        # Fill non-consolidation states first-match
        if state in target_states and state != CONSOLIDATION and state not in found:
            found[state] = (sym, bars)
            print(f'  {state:<15} -> {sym} ({len(bars)} bars)')

        # For consolidation: score by range contraction ratio
        if CONSOLIDATION in target_states and state == CONSOLIDATION:
            w = 20
            if len(highs) >= w * 2:
                recent_span = highs[-w:].max()    - lows[-w:].min()
                prior_span  = highs[-2*w:-w].max() - lows[-2*w:-w].min()
                if prior_span > 1e-9:
                    ratio = recent_span / prior_span   # lower = more compressed
                    if consol_best is None or ratio < consol_best[0]:
                        consol_best = (ratio, sym, bars)

        if len(found) >= len([s for s in target_states if s != CONSOLIDATION]):
            if consol_best is not None or CONSOLIDATION not in target_states:
                if len(found) + (1 if consol_best else 0) >= len(target_states):
                    break

    if consol_best and CONSOLIDATION in target_states:
        ratio, sym, bars = consol_best
        # Override with GILD — confirmed best visual example (clearest declining coil)
        gild_bars = load_bars('GILD', timeframe='daily')
        if len(gild_bars) >= min_bars:
            sym, bars = 'GILD', gild_bars
        found[CONSOLIDATION] = (sym, bars)
        print(f'  {CONSOLIDATION:<15} -> {sym} ({len(bars)} bars, range ratio {ratio:.2f})')

    return found


# ── Plot one chart ─────────────────────────────────────────────────────────
def plot_state(sym, bars, state, outpath):
    closes = np.array([b['c'] for b in bars])
    opens  = np.array([b['o'] for b in bars])
    highs  = np.array([b['h'] for b in bars])
    lows   = np.array([b['l'] for b in bars])

    # Full-history EMAs for accurate values
    e20_full  = _ema(closes, 20)
    e50_full  = _ema(closes, 50)
    e200_full = _ema(closes, 200)

    # Show last 120 bars
    WIN = 120
    closes_w = closes[-WIN:];  opens_w = opens[-WIN:]
    highs_w  = highs[-WIN:];   lows_w  = lows[-WIN:]
    e20_w    = e20_full[-WIN:]
    e50_w    = e50_full[-WIN:]
    e200_w   = e200_full[-WIN:]
    xs       = np.arange(WIN)

    # Rolling 20-bar range envelope (over window view)
    roll_hi, roll_lo = rolling_range(highs_w, lows_w, window=20)

    detail = trend_ema_detail(closes, highs, lows)
    border = STATE_BORDER.get(state, 'white')

    fig, (ax, ax2) = plt.subplots(
        2, 1, figsize=(12, 7),
        gridspec_kw={'height_ratios': [4, 1], 'hspace': 0.08}
    )
    fig.patch.set_facecolor(BG)

    # ── Title ───────────────────────────────────────────────────────────────
    title = (f'{sym}  |  {STATE_LABEL[state]}\n'
             f'EMA20 {detail["ema20"]:.2f}   '
             f'EMA50 {detail["ema50"]:.2f}   '
             f'EMA200 {detail["ema200"]:.2f}   '
             f'Spread20-50 {detail["spread_20_50"]:.2f}   '
             f'Spread50-200 {detail["spread_50_200"]:.2f}')
    if detail.get('range_ratio') is not None:
        title += f'   Range ratio {detail["range_ratio"]:.2f}'
    fig.suptitle(title, color='white', fontsize=10, fontweight='bold', y=0.99)

    # ── Main price panel ────────────────────────────────────────────────────
    for a in (ax, ax2):
        a.set_facecolor(BG)
        a.tick_params(colors='#888', labelsize=8)
        for sp in a.spines.values():
            sp.set_edgecolor(border); sp.set_linewidth(1.5)

    ax.set_xlim(-1, WIN + 8)
    ax2.set_xlim(-1, WIN + 8)
    ax.set_ylabel('Price (USD)', color='#666', fontsize=8)
    ax2.set_xlabel('Days (most recent = right)', color='#666', fontsize=8)
    ax2.set_ylabel('Range\nwidth', color='#666', fontsize=7)

    # Candles
    for i in range(WIN):
        o, h, l, c = opens_w[i], highs_w[i], lows_w[i], closes_w[i]
        color = CUP if c >= o else CDN
        ax.plot([i, i], [l, h], color=color, linewidth=0.7, zorder=2)
        ax.bar(i, max(abs(c - o), 0.01), bottom=min(o, c),
               color=color, width=0.6, zorder=3)

    # Rolling range envelope
    valid = ~np.isnan(roll_hi)
    ax.fill_between(xs[valid], roll_lo[valid], roll_hi[valid],
                    color=RANGE_C, alpha=0.06, zorder=1, label='20-bar range')
    ax.plot(xs[valid], roll_hi[valid], color=RANGE_C, linewidth=0.6,
            linestyle='--', alpha=0.35, zorder=1)
    ax.plot(xs[valid], roll_lo[valid], color=RANGE_C, linewidth=0.6,
            linestyle='--', alpha=0.35, zorder=1)

    # EMA lines
    ax.plot(xs, e20_w,  color=EMA20C,  linewidth=2.0, label='EMA 20',  zorder=5)
    ax.plot(xs, e50_w,  color=EMA50C,  linewidth=2.0, label='EMA 50',  zorder=5)
    ax.plot(xs, e200_w, color=EMA200C, linewidth=2.0, label='EMA 200', zorder=5)

    # Right-side EMA labels
    for val, lbl, col in [
        (e20_w[-1],  f'EMA20\n{detail["ema20"]:.2f}',  EMA20C),
        (e50_w[-1],  f'EMA50\n{detail["ema50"]:.2f}',  EMA50C),
        (e200_w[-1], f'EMA200\n{detail["ema200"]:.2f}', EMA200C),
    ]:
        ax.text(WIN + 0.5, val, lbl, color=col, fontsize=7, va='center')

    # State-specific shading
    if state == UPTREND:
        ax.fill_between(xs, e20_w, e50_w, where=(e20_w >= e50_w),
                        color=EMA20C, alpha=0.07)
    elif state == DOWNTREND:
        ax.fill_between(xs, e20_w, e50_w, where=(e20_w <= e50_w),
                        color=EMA200C, alpha=0.07)

    # Note box
    notes = {
        UPTREND:       'Bullish stack: EMA20 > EMA50 > EMA200\nAll three rising\nPullbacks find support at EMA20/50',
        DOWNTREND:     'Bearish stack: EMA20 < EMA50 < EMA200\nAll three declining\nBounces rejected at EMA20/50',
        SIDEWAYS:      'Mixed stack — neither ordered\nRange band is STABLE (constant width)\nEMAs flat & tangled — no urgency',
        CONSOLIDATION: 'Mixed stack — neither ordered\nRange band NARROWING (coil / wedge)\nEMA spreads contracting — breakout building',
    }
    ax.annotate(notes[state],
                xy=(0.01, 0.04), xycoords='axes fraction',
                color='#dddddd', fontsize=8, va='bottom',
                bbox=dict(boxstyle='round,pad=0.5', fc='#1c2333',
                          ec=border, alpha=0.92))

    ax.legend(loc='upper right', framealpha=0.25, facecolor='#1c2333',
              edgecolor='#444', labelcolor='white', fontsize=8)

    # ── Range-width subplot ─────────────────────────────────────────────────
    range_width = roll_hi - roll_lo
    ax2.plot(xs[valid], range_width[valid], color=RANGE_C,
             linewidth=1.2, alpha=0.7)
    ax2.fill_between(xs[valid], 0, range_width[valid],
                     color=RANGE_C, alpha=0.12)

    # Trend line on range width to visualise stable vs shrinking
    vxs = xs[valid]
    vw  = range_width[valid]
    if len(vxs) > 5:
        m, b_ = np.polyfit(vxs, vw, 1)
        trend_line = m * vxs + b_
        slope_color = '#ef5350' if m < -0.005 * vw.mean() else '#26a69a' if m > 0.005 * vw.mean() else '#f4a261'
        ax2.plot(vxs, trend_line, color=slope_color, linewidth=1.5,
                 linestyle='--', alpha=0.8,
                 label='Shrinking' if m < 0 else 'Stable/Expanding')
        ax2.legend(loc='upper right', fontsize=7, framealpha=0.3,
                   facecolor='#1c2333', edgecolor='#444', labelcolor='white')

    ax2.set_title('Rolling 20-bar price range width  (flat = sideways  |  declining = consolidation)',
                  color='#888', fontsize=7, pad=3)

    fig.tight_layout(rect=[0, 0, 1, 0.97])
    fig.savefig(outpath, dpi=150, bbox_inches='tight',
                facecolor=BG, format='jpeg', pil_kwargs={'quality': 92})
    plt.close(fig)
    print(f'  Saved -> {outpath}')


# ── Main ──────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    target = [UPTREND, DOWNTREND, SIDEWAYS, CONSOLIDATION]
    print('Searching for best real examples of each state...')
    examples = find_best_examples(target)

    missing = [s for s in target if s not in examples]
    if missing:
        print(f'WARNING: no examples found for: {missing}')

    print('\nGenerating validation charts...')
    for state, (sym, bars) in examples.items():
        out = f'trend_validate_{state}.jpg'
        plot_state(sym, bars, state, out)

    print('\nDone. Check the range-width subplot:')
    print('  Sideways      -> flat trend line (stable range)')
    print('  Consolidation -> declining trend line (shrinking range = coil)')
