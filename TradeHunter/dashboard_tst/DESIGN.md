# dashboard_tst — Design blueprint (trend & swing product + collaboration platform)

**Status:** DRAFT — agreed vision, pre-implementation. This is the
blueprint we design against; nothing here is built yet. Decisions marked
**[OPEN]** still need the user's call before the affected work starts.

**Last updated:** 2026-05-30

---

## 1. One-liner

`dashboard_tst` is TradeHunter's **trend & swing** product: a
members-only, internet-facing web platform where a study group screens
tickers, computes MATP/MBP target levels, studies patterns, and
**collaborates on the actual trade decision** (entry / stop-loss /
profit-target) — backed by an option win-rate calculator and a
parquet-driven backtester, and driving its own trend-swing trading bot
that journals, reviews, and self-improves exactly like the intraday bot.

---

## 2. The roof model (decided)

**TradeHunter is the roof.** Under it sit two *products* that are
different trading methodologies but share one analysis library:

```
TradeHunter/                  roof — umbrella + shared resources
├── resources/                SHARED: patterns.py, bars_store.py (parquet),
│                               trend_state.py, finviz_screener.py, MATP/, ...
├── journal/                  SHARED: decision/event logging (Layer 4)
├── review/                   SHARED: self-improvement loop (Layer 5)
├── execution/                SHARED: order submission layer (Layer 3)
├── strategy/                 per-family strategy modules (Layer 2)
├── dashboard_intraday/       PRODUCT 1: intraday methodology (DITP/GUNS).
│                               Operational, LAN/local only.
└── dashboard_tst/            PRODUCT 2: trend & swing methodology +
                                public collaboration platform. THIS doc.
```

The two products diverge in **methodology, UI, and exposure** — not in
the underlying analysis library. Both import the same `resources/`.
This honours the day-one rule (everything inside `TradeHunter/`) and the
cross-PC sync invariant.

---

## 3. User-facing workflow (decided)

The platform walks a study group through a funnel:

1. **Screen** — a Finviz filter URL produces the candidate universe.
2. **Level** — compute **MATP** (Median Analyst Target Price) + **MBP**
   (Max Buy Price = MATP / 1.15) per ticker. Refreshed **quarterly**
   (post-earnings analyst-target cadence).
3. **Classify / study** — tag each ticker's trend state and run pattern
   recognition to surface candidate trades.
4. **Collaborate** — members discuss and converge on **entry, SL, PT**
   for a setup. This is the social core of the product.
5. **Option win-rate** — a **Black-Scholes** module estimates the
   probability of an option outcome for the agreed levels.
6. **Backtest** — simulate the agreed entry/SL/PT against the parquet
   price history to report a **success rate**.

---

## 4. The trend-swing bot (decided in principle)

`dashboard_tst` drives its **own** bot, separate from the intraday
orchestrator. It is a first-class strategy family that flows through the
existing shared layers. Per the "adding a strategy" checklist + gating
rules in `CLAUDE.md`:

- **Own strategy family** — `strategy/<SWING_FAMILY>/` with setup
  module(s) (`impl.py` + `__version__` + `changelog.md`) and its own
  per-family scanner writing its own watchlist file. **[OPEN: family
  name + which swing setups]**
- **Own gating flags** — independent `state/enabled_<swing>.flag` +
  `state/armed_<swing>.flag`. Never a shared global ARM (hard rule).
- **Own IBKR clientId** — a new number; 71/80/83/84/85/98/99 are taken.
  **[OPEN: assign next free clientId, append to the CLAUDE.md table]**
- **Own state namespace + watchlists** — disjoint from the intraday
  bot's flags so the two never compete. (The "one orchestrator per
  machine" rule was about *the same* flags; two bots with disjoint
  state + clientIds can coexist — but we'll be explicit about where
  each runs.)

### Journaling / review / improve — REUSED, not rebuilt

The journal (Layer 4) and review (Layer 5) are already strategy-aware
(events carry the strategy name + `__version__`), so the swing bot plugs
in directly:

| Capability | Mechanism | Build effort |
|---|---|---|
| Decision + event journaling | `journal/writer.py`, `journal/events.py` → `data/journal/journal_<date>.jsonl`, tagged with the swing family | **Reuse** |
| Per-strategy stats | `review/stats.py` | **Reuse** |
| Self-improvement proposals (version-bumped edits) | `review/propose.py` | **Reuse** |
| Backtest / success-rate | `review/backtest.py` + `resources/bars_store.py` | Reuse engine; **build one swing backtest adapter** (cf. `strategy/DITP/ditp_p2/backtest_adapter.py`) |
| Dashboard surfacing | Family tab auto-appears from journal events; gating drawer auto-lists ON/ARM | **Reuse (automatic)** |

Net: journaling → review → improve comes essentially for free. The real
new authoring is the swing setups + one backtest adapter.

---

## 5. The review loop is shared with collaborators (decided)

The self-improvement review loop (Layer 5) is **shared with
collaborators** — it's the feedback core of the collaboration. Members
co-develop the swing setups, so they see how those setups actually
performed and what the review proposes:

- **Shared with collaborators:** per-strategy review output — win-rate,
  average R, total R, and the proposed version-bumped strategy edits —
  alongside the human-collaboration data (setups, patterns, MATP levels,
  backtest hit-rates).
- **Never shared (always trusted-side):** broker credentials and the
  live order-execution session. Non-negotiable regardless of product
  policy.

So there is one shared analytics surface, with a single hard carve-out
for credentials + execution. (This supersedes the earlier "two tiers,
admin-only review" draft.)

**[OPEN] one presentation choice:** if the journal carries *absolute*
dollar figures (account balance, position notional), do we surface those
to collaborators too, or present review data in **normalised** form
(R-multiples, win-rate %) — which the strategy code already favours per
the Option-A ticker-relative rule in `CLAUDE.md`? Default: share
normalised metrics; decide absolute-dollar visibility later.

---

## 6. Security & deployment architecture (guardrails — non-negotiable)

This product is public-facing, so the bot's credentialed parts and the
collaboration surface have opposite security needs. We split them:

### Control plane vs execution plane

- **Control plane (public app):** admin-only endpoints to start / stop /
  ARM the swing bot and show its status. Collaborators see status
  read-only at most; only the owner can toggle. Enforced by auth + role
  checks — not "please don't click".
- **Execution plane (trusted LAN side):** the swing orchestrator + IBKR
  session + broker credentials. Isolated from the public surface. The
  control plane sends *authenticated* signals to it; the broker session
  is never reachable from the internet.

### Hard constraints (will not be compromised)

1. **Execution engine + broker creds live on the trusted LAN side**,
   never on the public host with collaborators.
2. **Broker credentials + the live order-execution session are never
   exposed** to collaborators or the public host. (Strategy-performance
   review data IS shared — see §5; the carve-out is credentials + the raw
   broker session, not the performance analytics.)
3. **Auth required**, per-user identity, role-based authorisation.
4. **Reverse proxy + TLS** (e.g. Caddy → Let's Encrypt); only 443 open;
   IBKR ports + LAN dashboards firewalled off from the public host.
5. **Isolated from the trading Vault** — the public app gets its own
   minimal secrets (its DB, any data-API keys), never the trading creds.
6. Standard web hygiene: rate limiting, input validation, security
   headers, DB backups.
7. **Do not reuse the operational dashboard's bot/account endpoints**
   on the public surface. The inherited intraday endpoints from the fork
   are stripped; the swing bot control is purpose-built and admin-gated.

---

## 7. Reuse map (capability → existing module)

| Capability | Existing code | Status |
|---|---|---|
| Finviz universe | `resources/finviz_screener.py`, `finviz` MCP, MATP Stage 1 | reuse |
| MATP / MBP levels | `resources/MATP/` (writes CSV/Sheets today → write to platform DB) | reuse + retarget output |
| Trend classification | `resources/trend_state.py` | reuse |
| Pattern recognition | `resources/patterns.py` | reuse |
| Parquet bars / backtest | `resources/bars_store.py`, `review/backtest.py` | reuse + swing adapter |
| Journaling | `journal/writer.py`, `journal/events.py` | reuse |
| Self-improvement | `review/stats.py`, `review/propose.py` | reuse |
| **Black-Scholes option win-rate** | none | **build new** |
| **Collaboration (accounts, setups, comments, entry/SL/PT)** | none | **build new** |
| **Web platform shell (auth, DB, TLS deploy)** | none (current fork is operational, LAN-only) | **build new** |

---

## 8. Phased roadmap (proposed)

1. **Secure shell** — FastAPI app, user accounts + auth, isolated deploy
   with Caddy/TLS. Prove the secure, public shell before any trading
   content. Strip inherited operational/bot endpoints from the fork.
2. **Universe + MATP board** — Finviz filter → universe → MATP/MBP in the
   DB, quarterly refresh job, sortable board UI.
3. **Setup studies + collaboration** — post a setup (symbol, trend-state,
   pattern), propose/discuss entry/SL/PT, comments, roles.
4. **Black-Scholes module** — option win-rate calculator for agreed levels.
5. **Backtest module** — simulate entry/SL/PT against parquet, report
   hit-rate; surface results + the shared review analytics to
   collaborators (normalised metrics; credentials/execution stay
   trusted-side).
6. **Swing bot wiring** — author the swing strategy family + scanner +
   gating + clientId; admin-only control plane; execution on trusted
   side; journal + review flow automatically.

---

## 9. Open decisions (need the user's call)

These gate the affected phases. Recorded here so they're not lost.

- **[DECIDED] Networking = public URL via Cloudflare Tunnel (on Hermes).**
  Collaborators install nothing — they open `https://study.<your-domain>`.
  `cloudflared` runs on Hermes alongside uvicorn and dials **out** to
  Cloudflare (no inbound ports opened, no router changes), giving a free,
  auto-HTTPS public URL that proxies to `localhost:8000`. Supersedes the
  earlier Hamachi-VPN plan (dropped once we learned collaborators won't
  install a VPN client). Free; only a domain (~$10/yr) is needed for a
  stable URL. Access control is the login (password mode); set
  `TST_HTTPS_ONLY=1`. The deploy loop is push → Hermes `git pull` + restart
  service (autopull task). See DEPLOY.md.
- **[OPEN] Host isolation** on the R720: dedicated new Hyper-V DMZ VM
  (recommended) vs the existing Hermes VM. Lower stakes under Path B (no
  public surface), but a separate VM is still cleaner. The app holds no
  broker creds regardless.
- **[PROVISIONAL — scaffolded]** Web stack: **FastAPI + SQLAlchemy +
  Jinja2/HTMX**, DB via `TST_DATABASE_URL` (SQLite for dev, Postgres for
  prod — no code change). Chosen as the scaffold default; change before
  Phase 2 if you'd rather go React SPA / pin Postgres now.
- **[DECIDED] Auth = mode-switchable (`TST_AUTH_MODE`).** Active mode is
  **`password` (Path B)**: the admin creates member accounts (email +
  password, PBKDF2-hashed, approved on creation); access is gated by the
  Hamachi VPN + login. The **`google` (Path A)** mode is fully built and
  parked: Google OAuth (OIDC) + admin-approval queue, `TST_ADMIN_EMAIL`
  auto-promoted on first sign-in, optional email-domain allowlist. Flip to
  Path A with `TST_AUTH_MODE=google` once a domain + TLS exist (Google
  rejects bare/private IPs as redirect URIs, so it can't run over a raw
  Hamachi IP). Google sign-in for `openid`/`email`/`profile` is free.
- **[OPEN] Swing strategy family** name + which swing setups to code
  first (none exist yet — DITP/GUNS are intraday).
- **[OPEN] Swing bot clientId** — assign the next free number, append to
  the CLAUDE.md allocation table.
- **[OPEN] Black-Scholes "maximum win rate"** — exact definition
  (probability ITM = N(d2)? probability of touching PT?) and the options
  data source (IV, risk-free rate, expiries — from where?).
- **[OPEN] Backtest "success"** definition — what counts as a win when
  simulating entry/SL/PT, and which parquet timeframe(s) (daily is full;
  intraday is lookback-limited per the yfinance/IBKR seed caps).

---

## 10. What this product is NOT

- Not a fork that keeps the intraday bot-control + account endpoints on
  the public internet — those are stripped.
- Not a place that exposes broker credentials or the raw order-execution
  session — those stay trusted-side. (Strategy-performance review data IS
  shared with collaborators; see §5.)
- Not a second copy of the analysis library — it imports the shared
  `resources/`, same as `dashboard_intraday`.
