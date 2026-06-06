# Backtesting — design & roadmap

**Role:** offline historical validation of strategies, reading the Parquet bars
store (backtest-only data, per CLAUDE.md). Lives in `review/` (Layer 5,
self-improvement). The harness is **strategy-agnostic**: strategies plug in via
an adapter Protocol; the engine knows zero strategy specifics.

> This doc is the persisted mind map. Status tags: **[BUILT]** shipped &
> tested · **[PARTIAL]** works but incomplete · **[GAP]** not built yet.

---

## Mind map

```
                              B A C K T E S T I N G  (review/)
   ┌──────────┬──────────┬──────────┬──────────┬──────────┬──────────┬──────────┐
 0.INVARIANTS 1.DATA    2.ENGINE  3.ADAPTER 4.PORTFOLIO 5.METRICS 6.OUTPUTS
                                                          │
                                              7.VALIDATION · 8.DASHBOARD/OPS · 9.SELF-IMPROVE
```

### 0 · Invariants — what makes results trustworthy
- **No-lookahead is sacred** — at sim-time T the strategy sees only bars `t ≤ T`.
- **Backtest runs the SAME decision code as live** — divergence = worthless.
- **Everything ticker-relative** (ATR / R-multiples / vol-z) — from the strategy layer.
- **Deterministic & reproducible** — no wall-clock/random in the path; stamp git-sha + `__version__`.

### 1 · Data layer — Parquet is backtest-only
- `bars_store.load_bars(sym, tf)` — the single reader. **[BUILT]**
- `bars_store.bar_session_date_et(t)` — ET **session** bucketing (pre-market 04:00 →
  after-hours 20:00 ET into one date). **[BUILT 2026-06-06]** — fixes the prior UTC-date
  bug that mis-filed after-hours bars (20:00 ET = 00:00 UTC next day) into tomorrow.
- `review/_coverage.py::check_coverage(symbols, tf, start, end)` — data-sufficiency
  pre-flight; flags missing/partial history so a sparse window can't masquerade as a
  flat result. Wired into `run()` → `summary["coverage"]`. **[BUILT 2026-06-06]**
- Trading calendar (holidays, half-days) — currently weekday-only. **[GAP]**

### 2 · Simulation engine — `_trade_sim.py`
- Bar-walk + virtual bracket: entry on `entry_signal`, tradeability veto, conservative
  same-bar SL+TP→SL, then stop / target / EOD. **[BUILT, Phase 1]**
- Entry fill = bar close; **no slippage / commission / liquidity cap / gap-through**. **[GAP]**
- **Exit logic diverges from live** (`execution/orchestrator.py` OCO brackets + policies).
  Extract a shared `exit_policy` both call. **[GAP — biggest live-vs-backtest risk]**
- Trailing-stop / time-stop / early-exit / scale-in hooks. **[GAP — Phase 4]**

### 3 · Strategy plug-in — `BacktestAdapter` Protocol + `_adapter_registry`
- Add a strategy = adapter file + 1 registry line, zero harness edits. **[BUILT]**
- Registered: `ditp_p2`. GUNS setup1/5, OS commented TODO. **[GAP — GUNS adapters next]**
- **Point-in-time normalization** — `resources/ticker_profile.py::profile_at(ticker, as_of)`
  rebuilds ATR/vol baselines from parquet ≤ as_of (no disk write). **[BUILT 2026-06-06]**
  - The DITP scanner already recomputes ATR point-in-time from daily bars ≤ as_of, so
    `ditp_p2` was already clean. `profile_at` is the reusable guard so future **intraday**
    adapters don't accidentally read the nightly `data/ticker_profile/<T>.json` snapshot
    (which is *today's* state → lookahead).

### 4 · Portfolio & risk — **[GAP — entirely missing]**
- Today each candidate is simulated in isolation → `total_R` assumes every signal taken at
  equal risk. A real account needs a running **equity curve**, **sizing vs NLV**,
  `risk_per_trade ≤ 1%`, `max_position 10%`, **`max_concurrent` enforced across the day**,
  capital exhaustion, correlation. Converts "edge per trade" → "what the account would have done."

### 5 · Metrics & analytics — `_metrics.py`
- win-rate, expectancy_R, total_R, profit-factor, max-DD_R, consec-losses, avg W/L,
  hold-mins, **by-bucket cuts** (tier/variant/confluence/exit_reason). **[BUILT]**
- Attribution by strategy **`__version__`**. **[GAP — version in journal, not yet a cut]**
- Sharpe / Sortino / Calmar / risk-of-ruin / Monte-Carlo CIs. **[GAP — Phase 5]**

### 6 · Outputs & reproducibility
- `data/review/backtest_<strat>_<ts>.{jsonl,json}` — blotter + summary. **[BUILT]**
- Run manifest: params + window + universe + **git-sha** + per-strategy `__version__` +
  coverage snapshot. **[PARTIAL — coverage in summary; git-sha/version GAP]**
- Equity curve artifact (depends on §4). **[GAP]**

### 7 · Validation methodology — **[GAP]**
- In-sample / out-of-sample split; **walk-forward** robustness across regimes; SPY benchmark;
  regime slicing (trend/chop). Rule-based ≠ fit, but must prove it isn't curve-picked to one regime.

### 8 · Dashboard & ops
- **No backtest UI yet** **[GAP]** → `backtest_runs/*.json` manifest (file-only read, the
  `pipeline_runs` pattern) + a panel in **`dashboard_tst`** (research side): run list, equity
  curve, per-strategy/version metrics, blotter.
- **Hermes** runs long param sweeps; laptop runs quick iterations. Backtest reads parquet only →
  **not IBKR-bound → any Python is fine** (no `py -3.12` constraint).

### 9 · Self-improvement loop — `propose.py`
- journal → `stats.py` → `propose.py` (threshold-based, cold-start-safe) → strategy edit +
  version bump → **backtest the change** → adopt/reject. **[BUILT v1]** · LLM reviewer
  `llm_review.py` (candidate for the Nous Hermes box). **[GAP]**

---

## Phased roadmap

| Phase | Theme | Items | Status |
|---|---|---|---|
| **0** | **Correctness foundation** | ET-session bucketing · `profile_at` point-in-time · coverage pre-flight | **✅ done 2026-06-06** |
| 1 | Engine core | bar-walk, bracket exits, R-metrics, adapter, propose v1 | ✅ pre-existing |
| 2 | Coverage | GUNS setup1/5 + OS adapters | GAP |
| 3 | Realism | slippage/commission/liquidity · **shared live exit policy** | GAP |
| 4 | Account-level | portfolio sim: equity curve, sizing, max_concurrent | GAP |
| 5 | Rigor | walk-forward / OOS · advanced metrics · trading calendar · reproducibility manifest | GAP |
| 6 | Visibility | `dashboard_tst` backtest panel · Hermes sweep wiring | GAP |
| 7 | Intelligence | LLM reviewer on the Nous Hermes box | GAP |

## How to backtest a strategy
```
py review/backtest.py --strategy ditp_p2 --start 2026-05-12 --end 2026-05-22
py review/backtest.py --strategy ditp_p2 --symbols NVDA,MSFT --no-write   # smoke
py review/backtest.py --list-strategies
```
Adding one: implement `BacktestAdapter` at `strategy/<FAMILY>/<setup>/backtest_adapter.py`,
add a line to `review/_adapter_registry._ADAPTERS`. Use `profile_at(t, as_of)` for any
ATR/vol baseline — never `get_profile()` (that's today's snapshot = lookahead).
