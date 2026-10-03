# Critic findings on the four design parts (2026-10-03)

Two independent critics reviewed `part_A_data.md`, `part_B_engines.md`, `part_C_chart_engines.md`, `part_D_ui.md`.
NOT yet reconciled into the parts. Reconcile these FIRST when resuming (blockers, then majors), then merge the four parts into `../../OPTIONS_MODULE_DESIGN.md` Part II.


## Critic: None

**Verdict:** Not buildable as one document yet. Each part is strong on its own, but they disagree on the things a member would be hurt by: how many contracts to trade (B sizes to the chart stop and reaches 10 contracts with a worst case of 6.8% of the account; D sizes to max loss and forces at least one contract even when it exceeds the budget), where a tracked position lives (three stores), how the payoff chart is drawn (two implementations), what the rules fields are called and mean, and how the push is sent. On the member's side the design must still close five holes before step 1: the 'enter on the dip' conditional order that fires on a crash, bear calls tracked into a put-priced monitor, rejected (incl. earnings-inside) strategies that remain ticketable, live rows mixed into stale snapshots, and the moomoo leg-by-leg stop that can leave a naked short. The honesty layer needs 'about ... an estimate, not a promise' on every chance figure, the day-count behind every IV rank, 'what has to happen' on the card, and last-night's-prices wording on the ticket and the push. With the reconciliation list applied and the blockers fixed, the four parts merge into a design that is safe to build in the §9 order.

### [blocker] (cross-part) Contracts are sized two different ways (B: by the loss at the chart stop; D: by max loss), and the B way lets a '1% risk' trade carry a worst-case loss of 6-7% of the account while the card still says 'risk 1%'.

- Evidence: B5.2: "by_chart_stop = floor(risk_budget / loss_at_stop_usd)" -> LRCX 325/315: "contracts 10; capital at risk $990; max loss total $6,830" on NLV $100,000 (6.8%). D2.5: "qty = max(1, floor(risk_budget / max_loss))" -> floor(1000/683) = 1 contract for the same trade. B8.2 itself admits the gap case: "loss (15.62 − 11.54) × 200 = $816, more than the $696 the sizing budgeted". OPTIONS_MODULE_DESIGN §5.1: "Risk per trade | 1% of account | the user's sizing rule".
- Fix: One rule, in B5 and D2.5: contracts = min(by_chart_stop, by_max_loss) where by_max_loss = floor(risk_budget x GAP_MULT / max_loss_usd) with GAP_MULT a named shared pref (house default 2.0: a gap through the stop may cost twice the budget, never seven times). The card's sizing line must show both numbers every time: '10 contracts: about $990 if the stop fires, up to $6,830 (6.8% of your account) if the stock gaps past it'. Drop D's max(1, ...): when one contract exceeds the budget show 0 and 'not even one contract fits your 1% - lower the risk or choose a narrower spread' (B5.3 already has this note). Telegram never states a contract count.

### [blocker] (B) The 'enter on the dip' conditional entry (last <= 341.92) is a trap for an absent member: it fires on any fall through that price, including a gap straight through support, and it has no lower bound because the broker allows one condition per order.

- Evidence: B6.1: "condition: {"on": "LRCX", "field": "last", "op": "<=", "value": 341.92, "why": "...enter on the dip to 0.3% above support 340.9 rather than chase"}" and B6.2 "Conditional tab -> Add -> Price -> LRCX -> Last <= 341.92 -> submit". D2.8 prints the opposite and equally useless condition: "send only while LRCX is at or above 340 (support)" - true at 349, so it transmits at once. Brief/B6.2: "One condition set per order".
- Fix: Default = NO entry condition on the entry order for every family; the member places the limit order during the session (the ticket already says 'day order, while the market is open'). Offer 'enter on the dip' only as an explicit toggle on the ticket with the sentence 'This order will also fire if LRCX crashes through 341.92 on bad news. Only use it while you are watching.' Delete D2.8's '>= 340' line. The ONE conditional order the ticket should push hard is the chart-stop exit (ORDER 2).

### [blocker] (cross-part) Positions are stored in three different places by the three parts, so a tracked trade can appear twice on the Positions tab or be graded by the wrong engine.

- Evidence: A6.3: "option_spreads — untouched ... nothing in this part changes the table". B7.1: new table option_trades + a migration data step that "copies every open option_spreads row into option_trades" and "the new Positions panel reads only option_trades". D1.11: "bull_put / bear_call -> option_spreads ... every other strategy -> option_positions + option_position_legs" and D1.14 renders _portfolio_list.html from option_spreads plus a 'generic' block.
- Fix: Pick one for step 1 (D's: credit verticals in option_spreads, everything else in option_positions/legs, swept by the existing spread_monitor; B's option_trades/option_trade_checks become the step-2 generic valuer and the copy of option_spreads happens only when the generic valuer lands). State it once in the merged document and delete the other two descriptions.

### [blocker] (D) A bear call spread tracked into option_spreads is priced with PUTS by the existing monitor, so the Positions row and the nav badge show a wrong P/L for a real trade.

- Evidence: D1.11: "bull_put / bear_call -> option_spreads ... spread_monitor.snapshot must learn right='C' for bear_call (cross-part need, part C)". Code: app/services/spread_monitor.py:109-110 hard-code option_quotes.leg(ch, expiry, "P", short_strike) / leg(ch, expiry, "P", long_strike); part C does not list that change in its C0 file table.
- Fix: Make the right='C' change to spread_monitor.snapshot (and spread_math's breakeven for calls) a step-1 task owned by whoever edits spread_monitor, with a test 'a bear_call row marks against the call chain'. Until it is merged, POST /options/track must refuse bear_call with the toast 'Bear call tracking arrives with the next release' rather than write a put-priced row.

### [blocker] (D) A strategy the recommender rejected - including for 'earnings inside' - can still be ticketed and tracked: the greyed chip shows strikes and the Order ticket / Track this buttons stay live.

- Evidence: D2.3: "A greyed chip, when clicked, shows the picks with a banner (amber): 'Not recommended today: ... Showing the strikes anyway so you can see what it would cost.'" and D2.5 #optActions renders unconditionally. D2.5 only hides picks when "the rule is 'not allowed'" for earnings - but D2.3's reason list also carries 'expensive', 'wrong direction', 'no setup' etc. with strikes shown. §5.1: earnings inside = "the one thing a stop cannot protect".
- Fix: When the shown strategy is rejected: (a) for reason 'earnings inside' hide the strike table entirely (no 'see what it would cost') unless the member's rule is 'defined-risk only' or 'allowed'; (b) for every other rejection keep the strikes but replace the primary button with 'Order ticket (not recommended)' in the ghost style and put the rejection sentence as the first line of the ticket and of the tracked position's note. Add a test: a rejected-for-earnings strategy produces no ticket.

### [blocker] (cross-part) Three Alembic migrations all chain off e2f3a4b5c6d7, three different id's, three overlapping table sets - the merged document would create multiple heads and a duplicate user_option_prefs.

- Evidence: A2.2: "revision = "f4a5b6c7d8e9"; down_revision = "e2f3a4b5c6d7"" (six tables incl. user_option_prefs, option_jobs). B0.1: "alembic/versions/f0a1b2c3d4e5_option_engines.py ... chained off e2f3a4b5c6d7" (user_option_prefs, option_trades, option_trade_checks). D1.12: "f9a0b1c2d3e4_options_module.py ... down_revision = "e2f3a4b5c6d7"" (option_basket, user_option_prefs, option_positions, option_position_legs, option_idea_push, option_job_runs). Verified head today: only e2f3a4b5c6d7_iv_scan_items.py, down_revision d1e2f3a4b5c6.
- Fix: One file, f4a5b6c7d8e9_options_module.py, chained off e2f3a4b5c6d7, creating exactly: option_basket (A's shape with owner_key), option_chain_snapshot, iv_daily, option_signal, user_option_prefs (A's shape: prefs + prefs_hash + schema_version), option_jobs (A's name; D's status strip reads it), option_positions + option_position_legs (D), option_idea_push (D). B's option_trades/option_trade_checks go in a later revision with the generic valuer.

### [major] (D) 'Chance of keeping it' is printed as a bare number with a formula in brackets, not as a model estimate with its basis and its limits - the brief's explicit honesty requirement.

- Evidence: D2.7 pop_words: "74% chance of keeping it (1 minus the short strike's delta)." / "46% chance of profit (the odds the stock is past the breakeven at expiry, at today's volatility)." D4.4 Telegram: "74% chance of keeping it" with no qualifier. C3.8 has the honest material ("risk-neutral is a pricing convention, not a forecast", basis + model number) but D does not use it.
- Fix: pop_words(credit): 'About a 74% chance of keeping the credit - an estimate from today's option prices (the short strike's delta), not a promise. Earnings, news and gaps are not in that number.' pop_words(debit): 'About a 46% chance of profit if held to expiry, at today's volatility; this trade is managed by the chart stop and target, so the real odds depend on the move, not this number.' Show the second (model) figure from C3.8 in the title: 'model estimate 73%'. Telegram: 'about 74% chance of keeping it (estimate)'.

### [major] (D) The ticket and the push present last night's prices as if they were tradeable now; by the time a Malaysian member can act (21:30 MYT, 14 hours after the 07:15 push) the limit price and the greeks are stale, and the ticket's '~15 min delayed' line understates it.

- Evidence: D2.8: "Data as of 02 Oct 16:00 ET (Cboe, ~15 min delayed) — check the mark in your broker before sending." A4.1: job at "07:15 MYT = 19:15 ET"; CLAUDE.md: US open = 21:30 MYT. D4.4 pushes 'Nov 20 330/320 put · collect ≈ $210' with no 'when'.
- Fix: Ticket header line: 'Prices are from yesterday's close (02 Oct 16:00 ET). Press Refresh after 21:30 Malaysia time (US open) and re-open the ticket before sending; the credit will have moved.' Make the ticket button itself re-check age: if as_of is older than the last session close and the US session is open, the ticket renders a 'Refresh first' banner with the Refresh button inline (and the 60 s cooldown must not block that first refresh). Telegram first line: 'Ideas for tonight's US session (opens 21:30 Malaysia). Prices are last night's close.'

### [major] (B) The moomoo chart-stop rendering sends the two closing legs as separate conditional orders with model limit prices; a partial fill leaves the member naked short, and a limit below the market means the stop never executes.

- Evidence: B6.3: "two legs = two conditional orders, both on the same LRCX <= 336.20 trigger" with "buy to close 325 Put qty 10 limit 18.20 / sell to close 315 Put qty 10 limit 14.04" and "these are estimates — at the trigger, use the market's mid" (a member who is away cannot do that).
- Fix: Render the moomoo stop as: (1) 'Buy to close the SHORT leg first (325 Put) - market order, or limit = model x 1.15'; (2) 'Then sell the LONG leg (315 Put)'; with the sentence 'Never sell the long leg before the short leg is closed - you would be short a naked put.' Same ordering rule for TWS if the combo cannot be conditional. For the TWS combo stop recommend Market (the design already lists it) and make Limit the secondary option with 'may not fill in a fast market'.

### [major] (cross-part) The chart stop is defined on the daily CLOSE by the engines but the ticket's conditional order fires on the intraday LAST, and the ticket does not say the order may fire on a wick or outside regular hours.

- Evidence: B7.3 credit_vertical chart stop: "underlying CLOSE beyond chart_stop". B6.2: "Conditional tab -> Add -> Price -> LRCX (STK, SMART) -> Last <= 336.20 -> transmit when true". D2.8: "stop: close if ... LRCX closes under 338".
- Fix: State the difference on the ticket in one line: 'This order fires on the live price during regular hours, so it can fire on an intraday dip the close would have survived. If you prefer the close-based rule, leave this order off and act on the Positions tab's verdict instead.' Set 'Trigger outside RTH: No' explicitly in both broker renderings.

### [major] (cross-part) Live (TWS) rows are written into the stored snapshot by part D but part A says they are never persisted; mixing one live expiry with delayed/yesterday rows makes the picks table compare prices from different moments without saying so per row.

- Evidence: A1.5: "Live chains are not written to option_chain_snapshot (decision 1: the bridge is a read only)". D1.10 step 1: "snapshot_store.write(db, converted, source='ibkr', as_of=now, partial=True) - ONLY the (expiry, right, strike) keys the bridge sent are overwritten; every other expiry stays Cboe". D6 badge: "live (TWS) for Nov 20 · other expiries delayed".
- Fix: Follow A: grade the live chain in-request (BridgePayloadSource), never store it. On the card after a Live press, the picks table is restricted to the live expiry and each row carries 'live 21:42 ET' in the legs column; other expiries are hidden behind 'show delayed expiries'. The only persisted artefact of Live is the IV series (A4.6).

### [major] (D) A ticker whose picks have simply not been computed under the member's rules is labelled 'no strike passes your rules today' in the basket - a false statement that will send members loosening rules for nothing.

- Evidence: D1.4: "has_picks = bool(rec and strike_picker.cached(s, rec["key"], rules["hash"]))" and D2.2: "faded (opacity-60) when not has_picks with title 'recommended, but no strike passes your rules today'". A5.1: the nightly job writes only the house hash; a member with overrides gets a row "lazily the first time they open the card".
- Fix: Three states in the basket, not two: has picks / no strike passes (computed) / not checked yet (grey dot, title 'open the card to check your rules'). Better: the nightly job also computes picks for every distinct saved prefs_hash (A5.1's own 'members on house defaults share one row' already covers most), so the basket is honest on first paint.

### [major] (B) A strategy whose picker is not built can occupy the 'recommended' chip, and the member-facing text leaks build jargon ('coming in step 4').

- Evidence: B3.3 score: "- (10 if rule.step > current_step else 0) ... it is still SHOWN with 'coming in step N'". B8.2: "recommended = leaps_call labelled 'coming in step 4 — shown for the read'"; headline "the long-term read supports a LEAPS call (step 4)".
- Fix: An unbuilt rule can never be `recommended`: move it to also_fits with the chip text '· not available yet' and the headline 'The long-term chart would suit a long-dated call; that strategy is not in TradeHunter yet.' Never show the words 'step N' to a member.

### [major] (cross-part) When IV history is too short, the headline and gauge strings assume a rank exists; the member sees 'Options are expensive (IV rank )' or a provisional verdict dressed as a firm one.

- Evidence: D2.7: "gauge SELL | 'Options are expensive (IV rank {{ r }})'"; D2.2 IV colour from iv_rank only. B1.5: "< 20 obs, HV present | provisional | all False". A3.3: "forming -> 'IV rank: forming (34 of 60 days) — press Live to load a year from TWS'".
- Fix: Add gauge wording for basis in (percentile, provisional, unknown): 'Options look expensive against the last 34 days (not a full year yet)'; 'We cannot yet say whether options are expensive - 12 of 60 days of history. If you have TWS on this PC, press Live to load a year.' The basket IV cell shows '~62' with a dotted underline when basis != rank and '–' when unknown; never colour amber on a provisional read. iv_rank_words must include the day count: 'over 118 days' / 'over the last year'.

### [major] (D) The Telegram push can be noisy or misleading: a one-strike shift re-pushes the same thesis, stale/partial/provisional ideas are not excluded, unknown-earnings ideas go out, and a mistyped chat id sends ideas to a stranger.

- Evidence: D4.3 idea_key: "'LRCX|bull_put|2026-11-20|330|320' - the legs define the idea ... a different short strike is a new idea" (spot drifting one listed strike a day = a push a day). A4.4 instead dedupes "not pushed in the last 5 days for the same (symbol, strategy, expiry, short_strike, long_strike)". No part excludes partial chains (A1.3), stale snapshots, provisional gauge (B1.4) or earnings_date None (A3.7 'warning, not a veto') from the push. D4.2 stores a typed chat id with no verification.
- Fix: Guards, in telegram_push.run: (1) key = (symbol, strategy, front expiry) + re-push only if the short strike moved > 1 ATR; (2) skip when snapshot.partial, status != ok, gauge.provisional, earnings_date is None (say 'skipped: earnings date unknown' in the job log), rule.step > current_step, or as_of older than the last session; (3) cap at 5 ideas per message ordered by score, 'and N more on the page'; (4) chat-id handshake: the member sends /start to the bot, the bot replies with a 6-digit code, the drawer accepts the id only with that code; (5) a per-member 'quiet' switch and a 'pause for 7 days' link in every message.

### [major] (cross-part) The account value from TWS is auto-stored by D but B says it is never stored silently; auto-storing a broker balance without a click is a consent and privacy problem on a shared platform.

- Evidence: D1.10 step 4: "payload.nlv: tp.write(db, user, nlv=nlv) only if the member's stored nlv is 0". B5.3: "A 'remember this' button next to the TWS figure writes it through trade_prefs.write ... it is never stored silently."
- Fix: Use B's rule: the Live NLV sizes the current request and shows 'from TWS · [remember this]'; nothing is written without the click.

### [major] (D) The full-chain expander will ship thousands of rows to a phone.

- Evidence: D1.15 chain(): "The full stored chain for the expander ... ?expiry="; A2.3: "MSFT 3,710; SPY ~15k" contracts per snapshot; D2.5 loads it with "hx-trigger='toggle once'" into "overflow-auto max-h-[50vh]".
- Fix: Default the expander to the pick's expiry and +/- 12 strikes around spot, with an expiry picker and 'show all strikes' per expiry; never render more than ~60 rows without a click.

### [major] (B) LEAPS and diagonal long legs have no premium stop, so a member can hold a $10,000 option through a 50% drawdown while the weekly EMA stack still holds.

- Evidence: B5.2 rule stop table: "leaps | none in $ — the weekly trend (B7)". B7.3 leaps rows: trend stop, delta drift, delta up, roll date - no loss line. B4.1 'long' block has premium_stop_pct 50 but leaps block has none.
- Fix: Add premium_stop_pct to the leaps block (house default 40) and a B7.3 row 'loss | loss_pct_of_premium >= premium_stop_pct | CLOSE | "Down {x}% of what you paid: the weekly trend may hold but the position has not."'. The diagonal inherits it through its long leg.

### [major] (B) An open position is never re-checked for an earnings date that appears or moves after entry, although the design calls earnings the one thing a stop cannot protect against.

- Evidence: B3.4 checks earnings only at recommendation time; B7.3's rule table has no earnings row for any family; bull_put.py:381-397 gates earnings only in select().
- Fix: Add a monitor row for every family: 'earnings | earnings_date now <= front_expiry and not allowed by the member's rule | WATCH (urgent) | "Earnings {date} now fall inside this trade (the date was unknown or later when you entered). Decide before the close that day."' and include it in the Telegram/Discord actionable set.

### [major] (cross-part) The My rules schema differs between B and D in field names, units and meaning, so the drawer would not round-trip through the engine.

- Evidence: B4.1 credit block: width_atr_lo/hi (ATR), long_offset_max, iv_gate_min, earnings_rule none_inside|defined_risk_only; leaps extrinsic_pct_max = "% of the STOCK price". D3.1: width_strikes (1-5 strikes), sell_iv_rank_min / buy_iv_rank_max in shared, earnings no|defined_risk|yes, leaps_extrinsic_pct_max "time value at most % of the option price", plus stop_atr / target_r as member-editable (B keeps STOP_ATR / TARGET_RR as constants). B blocks: credit_vertical, debit_vertical, long, leaps, condor, time; D families: credit, debit, condor, time.
- Fix: Adopt B's blocks and field names as the schema (they are what the picker reads) and D's labels/help/translations as the presentation layer; one FIELDS table in option_prefs.py that both import. Decisions to write down: width in ATR (B) with the $ figure shown beside it (D's intent); extrinsic cap = % of the STOCK price (B's argument stands); earnings choices = none_inside / defined_risk_only only (no 'yes' - an undefined-risk trade through earnings is not something the house offers); stop_atr/target_r stay constants in v1.

### [major] (cross-part) Two incompatible payoff charts are specified (server-rendered SVG vs client canvas) with different grids, different T+0 definitions, different R definitions and different captions.

- Evidence: C4.1: "a server-rendered inline SVG, not a second lightweight-charts instance"; C3.5 grid ATR-padded with strikes as grid points; C3.4 T+0 at days_ahead=0; C3.9 R = 20% of max loss for credit families. D2.6: "<canvas id='optPayoffCanvas'>" painted by window.thPayoffLoad from JSON; D1.7 grid "121 points over [spot − 3·EM, spot + 3·EM]"; today at "T=dte/365 − 1/365" ('tomorrow'); "r_value = |pl_today(stop_chart)|" for all families.
- Fix: Build C's payoff.py + _payoff_chart.html (one code path, theme tokens, HTMX swap) and drop D2.6's canvas and D1.7's JSON formulas; D's /options/payoff/{symbol} route returns C's partial. Keep C's R definitions. One caption: 'Dashed line: what the trade would be worth if the stock moved there today, at today's implied volatility - an estimate. Solid line: at expiry (48 days).'

### [major] (D) The card's signal lookup assumes one row per symbol, but the storage part keeps one row per (symbol, day, kind, prefs hash); the query raises on the second member.

- Evidence: D1.4: "sig = db.query(OptionSignal).filter(OptionSignal.symbol == sym).one_or_none()" and D1.4 basket: "sigs = {s.symbol: s for s in db.query(OptionSignal).filter(OptionSignal.symbol.in_(...)).all()}". A2.1: "UniqueConstraint('symbol', 'snap_on', 'kind', 'prefs_hash')" with house + per-member rows.
- Fix: D reads through A5.3's option_store.card_for(db, symbol, user) / a latest-per-symbol helper (newest snap_on, member hash else house hash); the basket builder calls one batched 'latest signal per symbol for this hash' query.

### [major] (D) On a phone the Live (TWS) button and its failure text make no sense (the bridge lives on the member's PC), and the 17-field debit rules form is a wall.

- Evidence: D2.1 mobile: "the layout is a single column ... then the My rules <details> as a bottom sheet"; D2.9: "Could not reach your IBKR bridge on this PC (127.0.0.1:9224). Start TWS and bridge\start_ibkr_bridge.bat"; D3.1 debit FIELDS has 17 entries.
- Fix: Hide Live on touch/narrow viewports with the hint 'Live quotes need TWS on your PC'; split the debit tab into 'Buy call / put', 'Spreads' and 'LEAPS' sub-sections collapsed by default on < lg; show the one-line translation first and the fields under 'change these'.

### [major] (cross-part) The bridge version for the IV series is wrong in part D (1.4 already exists) and the series unit differs between A and D.

- Evidence: bridge/ibkr_bridge.py:631: "server_version = 'TradeHunterIBKRBridge/1.5' # ... 1.4 open interest per leg; 1.5 no fixed waits". A4.6: "Bridge 1.6: /iv?symbol=X&series=1 returns ... 'iv': 0.3123 (fraction)". D1.10: "Bridge change required (bridge 1.4): ... 'iv': round(b.close*100, 1)" (percent) and D2.9 'bridge older than 1.4'.
- Fix: Bridge 1.6; series in FRACTION as the bridge already holds it; the server multiplies by 100 once in option_store.bootstrap_iv (A4.6). Every member-facing string says 'older than 1.6'.

### [minor] (D) Several member strings use trader jargon, promotional phrasing or all-caps headings.

- Evidence: D2.7: "finishes in the money"; iv_rank_words: "buyers get a deal"; gamma_words shown to members; reason_short "front IV under back", "no long-dated", "not rich enough"; D2.8: "(you sell; defined risk)", "natural 2.00"; D2.5 heading "STRIKES under your rules", D2.6 "RISK & REWARD". B4.7: "delta 0.26 ≈ 1-in-4 chance of finishing in the money"; B1.4: "options priced 1.21x the stock's realised move"; B3.2 leaps why: "deep-in-the-money call replaces the stock with 3.9x the exposure per dollar".
- Fix: Rewrites: 'finishes in the money' -> 'is below this strike at expiry' (puts) / 'above' (calls); 'buyers get a deal' -> 'cheap by this stock's own standards'; hide gamma behind the full-chain expander; chip reasons -> 'near-term not dearer', 'no 9-18 month options stored', 'premium not rich enough'; '(you sell; defined risk)' -> '(you are paid; the most you can lose is fixed)'; 'natural 2.00' -> 'worst likely fill 2.00'; headings in sentence case: 'Strikes under your rules', 'Risk and reward'; '1.21x the stock's realised move' -> 'priced for 21% more movement than the stock has actually shown'; LEAPS why -> 'A long-dated call bought deep in the money moves almost like 79 shares, for about a quarter of the money, and only 9% of the share price pays for time.'

### [minor] (B) The credit shown ('you collect $317') is the mid of a market up to $0.50 wide per leg; the member's realistic fill is lower and the max loss correspondingly higher.

- Evidence: B4.1 max_leg_spread default 0.50; B6.1 "limit = mid-mid; floor = the worst net still inside credit_pct_min"; B4.7 collect words use the mid only.
- Fix: Show a range on the pick row: 'collect $300-$317 (worst likely fill to mid)' with the POP/max loss quoted at the mid and the ticket's floor shown as 'never below $250'.

### [minor] (A) The forming-IV message tells every member to 'press Live' although Live needs TWS on a PC, which most members on the Hermes site will not have.

- Evidence: A3.3: "forming -> 'IV rank: forming (34 of 60 days) — press Live to load a year from TWS'".
- Fix: 'IV rank: not enough history yet (34 of 60 days). It fills in by itself; if you run TWS on this PC, Live loads a year at once.'

### [minor] (B) The must_happen sentence - the brief's 'what has to happen for this to work' - is produced by B but has no place on D's card.

- Evidence: B3.2 templates: "must_happen: '{symbol} stays above {short_strike:g} until {expiry_label}...'"; D2.3 card layout has headline, gauge, chips, chart, picks, payoff, actions - no must_happen line; §6a requires "a one-sentence WHY and 'what has to happen for this to work'".
- Fix: Add a 'What has to happen' line under the chip row in D2.3 (text from B's must_happen for the chosen strategy), and the same line in the Telegram block.

### [minor] (D) Chart-constraint and other safety switches can be turned off by a non-technical member with one click and no consequence shown.

- Evidence: D3.1: ("under_level", "Short strike must be under support (over resistance)", ..., True) as a plain checkbox; B4.8 fix text: "switch off the chart rule (not recommended)".
- Fix: Unticking a safety switch (under_level, outside_range, earnings -> defined_risk_only) shows an inline amber sentence before Save ('Without this, the short strike can sit inside the zone the chart says must hold') and marks the override dot rose instead of amber.


### Reconciliation (field names / shapes to unify)

- Migration: ONE file f4a5b6c7d8e9_options_module.py off e2f3a4b5c6d7 (drop B's f0a1b2c3d4e5 and D's f9a0b1c2d3e4 ids).
- Job table: option_jobs (A's name/shape; D's option_job_runs merges into it; D's job_runs.start/finish/latest/missed wrap it).
- Positions store, step 1: credit verticals -> option_spreads; all other strategies -> option_positions + option_position_legs (D). B's option_trades / option_trade_checks = the step-2 generic valuer's revision; no copy of option_spreads in step 1.
- Prefs: table user_option_prefs with A's columns (prefs JSON sparse overrides, prefs_hash, schema_version); blocks and field names from B4.1 (shared, credit_vertical, debit_vertical, long, leaps, condor, time); D's FAMILIES/FAMILY_LABELS become the five drawer tabs mapping onto those blocks (Shared, Credit spreads = credit_vertical, Buy call/put = debit_vertical + long + leaps, Iron condor = condor, Time spreads = time). Width in ATR (B) with $ shown; extrinsic cap = % of STOCK price; earnings_rule in {none_inside, defined_risk_only}; stop_atr / target_r are constants, not fields.
- Sizing: one function option_sizing.size (B5) with contracts = min(by_chart_stop, by_max_loss(GAP_MULT), by_notional); D2.5's qty formula and max(1, ...) are removed.
- Strategy keys: bull_put, bear_call, buy_call, buy_put, bull_call, bear_put, leaps_call, iron_condor, calendar, diagonal_call everywhere (C's family keys 'leaps' and 'diagonal' map to these; C's CREDIT_FAMILIES = {bull_put, bear_call, iron_condor}).
- Fit values in option_signal.strategies: recommended | also_fits | rejected (A/B); D's 'also' -> 'also_fits'. Rejected rows carry reason_short (D2.7 fixed list) AND the long reason (B).
- Pick leg shape: {expiry, right, strike, side: 'sell'|'buy', qty (positive), price (mid), bid, ask, delta (signed), iv (FRACTION), oi, volume}; C's payoff.Leg is built from it with qty = +qty for buy / -qty for sell. Key name is oi (never open_interest) once past opt_legs.norm_leg.
- Units: per-contract iv = fraction everywhere after normalisation; iv_daily per-day stats (iv30, hv20, hv60) = percent; the bridge /iv series = fraction, multiplied by 100 once in option_store.bootstrap_iv. Bridge version 1.6.
- POP naming: pop (number) + pop_kind in {'keep','profit'} on the pick (B); option_words.pop_words maps to 'chance of keeping it' / 'chance of profit'; C's pop.label uses the same two strings; C's pop.model is shown as the secondary figure.
- Payoff: C's payoff.py + _payoff_chart.html (server SVG) is the one implementation; D's canvas/JSON (D1.7, D2.6) is dropped; the route is GET /options/payoff/{symbol}?strategy=&pick=&units=$|R returning the partial. R = 20% of max loss for credit families, |pnl_today(chart_stop)| for debit (C3.9).
- Chart stop pad: LEVEL_PAD_ATR = 0.25 (B) is the single constant; the mockup's 338 and C5's examples are replaced by the engine value (336.2 for the LRCX fixture).
- Signal read: option_store.card_for(db, symbol, user) (A5.3) is the only read path; D's one_or_none / in_ queries are replaced by it and by a latest-per-symbol batch helper.
- Live (TWS): never persisted (A1.5); graded in-request via BridgePayloadSource; D1.10's snapshot_store.write(partial=True) is removed; endpoint names: POST /options/{sym}/live (payload: chain, iv, nlv, diag) replaces D's /options/live/{sym} and A's /options/{sym}/iv/bootstrap (the bootstrap happens inside it). Other endpoints follow A's /options/{sym}/refresh style.
- Telegram: one implementation, app/services/telegram_push.py (D4) with per-member opt-in, chat-id handshake and the option_idea_push table; A's option_push.push_new_ideas / option_signal.pushed_at and B's option_exits.notify are folded into it. Sender app/services/telegram.py reuses scripts._common.telegram_env (verified at scripts/_common.py:562) with a chat_id parameter. Dedupe key = (symbol, strategy, front expiry) with the > 1 ATR short-strike move rule.
- Basket: table option_basket with A's owner_key + source enum (typed|paste|watchlist|ivscan|scanner|screener|sector|system), D's pos column added; MAX_BASKET = 60 per member (D's budget argument) while A's 100 import cap is lowered to match.
- Status: GET /options/status returns A4.3's JSON; the HTMX strip is GET /options/status/strip rendering _options_status.html from that JSON.
- NLV: B5.3's order (Live figure for the request -> stored trade_prefs nlv -> None + note); never auto-written.
- Chip reason strings: D2.7's fixed reason_short list is the vocabulary; B3.3's TREND_REASON / IV_REASON / NEED_REASON produce the long form.

## Critic: None

**Verdict:** NOT BUILDABLE AS CONCATENATED — but close, and the fix is editorial rather than design work. Each part is individually well grounded: 249 citations spot-checked, 227 land within six lines of the cited line and the rest are small offsets or a wrong file attribution; every reused function is real (option_quotes.fetch_chain/leg/expiries/clear_cache, bull_put._mid/_count/oi_needed/spread_math/monitor, spread_monitor.et_today/snapshot/sweep, support_bounce.atr_series/_swings/_members/_touches/find, ema_setup.analyze/clean_enabled/setup_for, trade_prefs.read/write, scripts._common._env_lookup/telegram_env/send_telegram, black_scholes.black_scholes); the Cboe file carries exactly the five extra fields A adds and the 23 expiries out to 2029; the bridge is 1.5 with /chain,/iv,/scan,/account and the dated IV bars already fetched; the Alembic head is e2f3a4b5c6d7; uvicorn runs single-worker. CLAUDE.md compliance is good: no raw SQL or SQLite-only constructs, no parquet on a live view (Yahoo via prices.fetch_daily_ohlc everywhere), thresholds ATR/ratio-relative except D's width-in-strikes and the permitted liquidity rules, the scrollbar rule honoured, every runtime state given a dashboard surface. All ten strategies are covered end to end in B (rule row → enumeration → chart constraint → score → POP → sizing → ticket → exit row) with C's payoff for every family; the only completeness soft spots are the bear-side setup detectors (not prototyped) and the LEAPS weekly-trend exit needing deep bars in the sweep. What blocks the build is that the four authors each invented the seams: three migration ids, three positions stores, two baskets, two job tables, three prefs schemas, two option_signal row semantics, two payoff renderers with two JSON contracts and two routes, two trend-line drawings, four leg shapes, two sideways detectors, two term-structure definitions, three POP methods, three Telegram modules, plus two rule contradictions (D's qty = budget/max_loss rounded up vs B's chart-stop sizing; D persisting the live chain vs decision 1). Resolve by ownership — A: storage, nightly, signal keys, data endpoints' semantics; B: engines, prefs schema, sizing, ticket, exits and the option_trades store; C: trend line, range box, payoff engine and SVG renderer; D: routes, templates, HTMX flows, rules drawer, Telegram, status/badge — then apply the reconciliation list in one editing pass (rename, delete the losing duplicates, fix the toggle trigger and the ungated /track). After that pass the document is buildable in the §9 phasing.

### [blocker] (cross-part) Three incompatible positions stores are specified for the same step-1 feature, and the one D keeps cannot grade a bear call.

- Evidence: A6.3 (part_A l.908-914): "option_spreads — untouched ... a legs JSON column in a later revision". B7.1 (part_B l.975-1021): new `option_trades` + `option_trade_checks`, migration "copies every open option_spreads row into option_trades", "the new Positions panel reads only option_trades". D1.11/D1.12 (part_D l.452-461, 493-535): new `option_positions` + `option_position_legs`, "bull_put / bear_call -> option_spreads ... so the existing sweep ... continue unchanged". But spread_monitor.py:109-110 is hard-wired to puts: `s = option_quotes.leg(ch, expiry, "P", short_strike)` — a step-1 bear_call row in option_spreads is priced with the wrong right.
- Fix: Adopt B's `option_trades` / `option_trade_checks` + `option_exits.mark/grade/sweep` as THE positions store and monitor from step 1 (generic legs cover bear_call now and every later family). Delete D's `option_positions` / `option_position_legs` and the 'generic valuer in step 2' paragraph; D's Positions tab renders `option_trades` rows with an `OptionTradeCheck` drawer and `POST /options/positions/{id}/close`. Keep `option_spreads` read-only for the legacy `/portfolio` until removal (B7.1's copy step); `/options/badge` reads `option_trade_checks`. Remove the 'spread_monitor must learn right=C' cross-part need.

### [blocker] (cross-part) Three migration files with three revision ids all chain off e2f3a4b5c6d7 and all create `user_option_prefs` with different columns; two of them would make `alembic upgrade head` fail with multiple heads.

- Evidence: A2.2: `alembic/versions/f4a5b6c7d8e9_options_module.py`, `down_revision = "e2f3a4b5c6d7"` (six tables incl. user_option_prefs with prefs_hash, schema_version). B0.1/B7.1: `f0a1b2c3d4e5_option_engines.py` chained off e2f3a4b5c6d7 (user_option_prefs, option_trades, option_trade_checks). D1.12: `f9a0b1c2d3e4_options_module.py`, `down_revision = "e2f3a4b5c6d7"` (six tables incl. user_option_prefs, option_job_runs). Verified head: only `e2f3a4b5c6d7_iv_scan_items.py:13-14` revises `d1e2f3a4b5c6`; `app/db.py:94` runs `command.upgrade(cfg, "head")` at startup, which raises on a branched head.
- Fix: One file `alembic/versions/f4a5b6c7d8e9_options_module.py` (A's skeleton with the per-table `get_table_names()` guard) creating nine tables: option_basket, option_chain_snapshot, iv_daily, option_signal, user_option_prefs, option_jobs, option_trades, option_trade_checks, option_idea_push, plus B's data step copying open option_spreads. Delete the B and D migration names from their parts; every part cites `f4a5b6c7d8e9`.

### [blocker] (cross-part) `option_signal` has two incompatible row semantics: A keys one row per (symbol, snap_on, kind, prefs_hash) with picks per row; D assumes one row per symbol holding `picks[strategy][hash]`, and queries it with `.one_or_none()`, which raises under A's model.

- Evidence: A2.1 (part_A l.397-427): `UniqueConstraint("symbol", "snap_on", "kind", "prefs_hash")`, `user_id` nullable, `trend = Column(String(12))`, `headline`, `setup/iv/strategies/picks/payoff/ticket` JSON. D1.4 (part_D l.214): `sig = db.query(OptionSignal).filter(OptionSignal.symbol == sym).one_or_none()`; D0 (l.36): "columns symbol, as_of, trend (JSON), ... picks (JSON: {strategy: {prefs_hash: pick_result}}), earnings (JSON {date, inside_expiry}), stale (bool)"; D1.4 l.191 reads `signal.trend.get("order")`, l.233 `sig.trend.close`, `sig.trend.atr14`.
- Fix: A's model and A5.3 `option_store.card_for(db, symbol, user)` are the only read path; D's `_card_context` / `_basket_context` call `card_for` (and a `basket_rows_for(db, user)` batch variant) and never query OptionSignal directly. `trend` stays String; add `close`, `trend_days`, `atr` under `setup`; earnings stay `iv.earnings_date` / `iv.earnings_days`; `stale` is computed by `card_for` from `snap_on` vs `et_today()`. Drop D's `trend JSON {kind, order, close, atr14, n_days}` and `earnings JSON`.

### [blocker] (cross-part) The member-rules schema exists in three shapes (module name, block names, field names, units) — the engine and the drawer would not read the same key.

- Evidence: A (l.17, A5.1): `option_prefs.HOUSE`, blocks "shared, credit_spread, debit, condor, time". B4.1 (l.537-635): `option_prefs.SCHEMA` with SEVEN blocks `shared, credit_vertical, debit_vertical, long, leaps, condor, time`, fields `width_atr_lo/hi`, `long_offset_max`, `credit_pct_min`, `iv_gate_min`, `theta_pct_max`, `premium_stop_pct`, `extrinsic_pct_max` ("% of the STOCK price"), `months_lo/hi`, `earnings_rule` ∈ {none_inside, defined_risk_only}. D3.1 (l.995-1071): `option_rules.FIELDS` with FIVE blocks `shared, credit, debit, condor, time`, fields `max_ba_width`, `earnings` ∈ {no, defined_risk, yes}, `sell_iv_rank_min`, `buy_iv_rank_max`, `width_strikes` (1-5 listed strikes), `spread_short_delta_lo/hi`, `stop_atr`, `target_r`, `loss_stop_pct`, `leaps_extrinsic_pct_max` ("% of the option price"), `leaps_dte_lo/hi`, `cal_delta_lo/hi`.
- Fix: One module `app/services/option_prefs.py` = B's SCHEMA (its blocks, field names, ATR widths, extrinsic as % of spot, two-value `earnings_rule`) extended with D's per-field `label/help/plain/step/unit` columns and D's `write(db,user,block,form)` / `reset` / `for_strategy` wrappers. D's five tabs map onto B's seven blocks (tab 'Buy call/put' renders `debit_vertical` + `long` + `leaps`; the four credit exit lines still write through `trade_prefs`). Delete `option_rules.py`, D's FIELDS table and A's block names.

### [blocker] (cross-part) The payoff chart is specified twice with different renderers, routes and JSON contracts.

- Evidence: C4.1-C4.3 (part_C l.536-620): server-rendered inline SVG partial `_payoff_chart.html` from `payoff.build()`; route `GET /options/{symbol}/payoff?sig=&strategy=&rank=&units=$|R`; JSON with `markers: [{x,label,kind}]`, `hlines`, `pop: {label,value,basis,model}`, `units: {mode, r_dollars}`, `max_loss: -790`, grid = 201 points ±2 ATR ∪ strikes. D1.7/D2.6 (part_D l.324-365, 819-846): client-side `<canvas id="optPayoffCanvas">` painted by `window.thPayoffLoad` from `GET /options/payoff/{symbol}?strategy=&pick=&units=usd|r`; `markers` is a dict keyed `spot/stop_chart/stop_rule/...`, `pop`/`pop_word`/`pop_text`, `r_value`, `palette`, `max_loss: 790`, grid 121 points over ±3·EM clamped to [0.5, 1.5]·spot, today curve at `T = dte/365 − 1/365`.
- Fix: C's renderer and C's `build()` dict (theme tokens recolour live, no second chart instance, strikes are grid points so the kinks are exact); D's path convention and query names: `GET /options/payoff/{symbol}?strategy=&pick=&units=$|R` rendering `_payoff_chart.html`, swapped into `#optPayoff` by HTMX (no `thPayoffLoad`, no canvas). `max_loss` is reported as a positive magnitude everywhere (C changes sign); T+0 at t=0 (C/B), caption keeps the word 'tomorrow'.

### [blocker] (cross-part) The automatic trend line is drawn two different ways on the same chart, with two different data shapes.

- Evidence: C4.4 (part_C l.622-698): `var TL = {{ chart_trendline|tojson }}`, `chart.addLineSeries` + `window.__paintTL` extending to the EXP badge, `candleMarks` 'TL i/n', channel series; shape from `trend_line.overlay()`: `{direction, p1{time,price}, p2, slope_per_bar, touches, n_touches, value_today, value_at, broken, last_break, channel, bounce, label}`. D2.4 (part_D l.765-767): `var TLINE = ...` painted "by the drawing overlay (setupDrawing, :678) as a read-only shape list RO_SHAPES = [{type:'tline', a:{t,p}, b:{t,p}, ro:true}]" in cyan dotted; shape `{a:{t,p}, b:{t,p}, touches, slope_per_day, value_today, value_at, broken}`. The drawing layer's tline branch (_price_chart.html:928-929) has no `ro` concept today.
- Fix: C's implementation and C's overlay dict are canonical; `chart_trendline` IS `trend_line.overlay(tl, tl_bounce)`. Delete D's RO_SHAPES paragraph and the `{a,b}` shape; keep D's hook list pointing at C4.4 items 1-6.

### [major] (D) D persists the member's live bridge chain into `option_chain_snapshot`, which contradicts decision 1 and Part A's rule that Live is read-only.

- Evidence: D1.10 step 1 (part_D l.412-415): "snapshot_store.write(db, converted, source='ibkr', as_of=now, partial=True) - ONLY the (expiry, right, strike) keys the bridge sent are overwritten". A1.5 (part_A l.281-283): "Live chains are **not written to option_chain_snapshot** (decision 1: the bridge is a read only) ... the only thing a Live press persists is the IV series". Brief decision 1: "the IBKR bridge is the member-side 'Live' read only".
- Fix: `POST /options/live/{symbol}` builds `BridgePayloadSource(payload)` (A1.5), runs B's pure `strike_picker.pick(...)` and `option_sizing.size(...)` in-request, renders the card with the 'live · TWS' badge, and persists only the IV series (A4.6 `bootstrap_iv`) and the optional NLV via `trade_prefs.write`. Delete `snapshot_store.write(... partial=True)` and the 'live (TWS) for <expiry> · other expiries delayed' badge logic that depends on stored partial rows.

### [major] (D) D's bridge bump to '1.4' is already taken, and the two parts disagree on the unit of the bootstrapped IV series.

- Evidence: bridge/ibkr_bridge.py:631: `server_version = "TradeHunterIBKRBridge/1.5"   # ... 1.4 open interest per leg; 1.5 no fixed waits`. D1.10 (l.399): "Bridge change required (bridge 1.4): /iv?symbol=X&series=1 adds series: [{on, iv: round(b.close*100, 1)}]" (percent) and D2.9 "Your bridge is older than 1.4". A4.6 (l.749-751): "Bridge 1.6: ... series: [{on, iv: 0.3123}] (fraction ...)" and `iv30 = iv × 100` on the server.
- Fix: Version 1.6 everywhere. The bridge sends PERCENT (`round(b.close*100, 1)`, the unit its own `iv_current/iv_low/iv_high` already use, ibkr_bridge.py:559-563); `option_store.bootstrap_iv` stores it as-is, bounded 0.1..1000 (A's `_b` style). D's 'older than 1.4' strings become 'older than 1.6'.

### [major] (D) D sizes contracts by max loss and rounds UP to 1, contradicting B's chart-stop sizing (the decision in the brief) and the platform's never-round-up rule.

- Evidence: D2.5 (part_D l.817): "`qty` = `max(1, floor(risk_budget / max_loss))`". B5.2 (part_B l.819-831): `by_chart_stop = floor(risk_budget / loss_at_stop_usd)`, `by_notional = floor(nlv x max_position_pct/100 / max_loss_usd)`, `contracts = max(0, min(...))`, "floor, never round up (trade_prefs.size ... trade_prefs.py:144-146)" — verified at trade_prefs.py:144-146.
- Fix: The card's contracts box is pre-filled from `pick.sizing.contracts` (B5.3) and the note is shown when it is 0 or None; D never recomputes a quantity. Same for the Telegram line and the ticket.

### [major] (cross-part) The `strategies` JSON and its `fit` vocabulary differ between the writer (B), the store (A) and the reader (D).

- Evidence: A5.2 (l.822-828): flat list of all ten with `fit ∈ recommended | also_fits | rejected`, `rank`, `reason`. B3.3 (l.468-471): `recommend()` returns `{recommended, also_fits, rejected_shown, others, chips}` with rejected rows carrying `reasons: [text...]`, `n_fail`. D0 (l.32) and D2.3: `fit: "recommended"|"also"|"rejected"` and a `reason_short` from the fixed list in D2.7; D1.4 l.182 `x["fit"] == "recommended"`.
- Fix: The signal writer flattens B's result into A's list: every row `{key, label, fit, score, step, why, must_happen, reasons: [text], reason_key, shown: bool}` with `fit ∈ recommended | also_fits | rejected` and `shown=True` on the ≤2 near-miss rejects. B's `fails` tuples carry a `reason_key` drawn from D2.7's fixed list (expensive, cheap options, not rich enough, trending not sideways, no range, wrong direction, no setup, earnings inside, front IV under back, no long-dated, no weekly trend, coming in step N). D's `chip_row` consumes that list.

### [major] (cross-part) A leg is described in four different shapes across the parts.

- Evidence: A5.2 (l.831-832): `{expiry, right, strike, side, qty, price, delta, iv, oi, bid_ask}`. B4.2 (l.645-647): `Leg = norm_leg(...) + {"side": "sell"|"buy", "qty": 1}` with `net` negative = credit. C3.1 (l.345-353): `Leg(right, strike, expiry, qty signed +long/−short, price, iv, delta)`. D0 (l.33): `{expiry, right, strike, side, qty, price, bid, ask, delta, iv, oi, volume}`; D's `OptionPositionLeg.side` is an Integer ±1.
- Fix: Stored/API leg = `{expiry, right, strike, side: "sell"|"buy", qty: positive int, price (the mid), bid, ask, iv (fraction), delta (signed), oi, volume}`; `payoff.Leg.from_dict()` derives the signed quantity. `OptionTrade.legs` JSON uses the same keys plus `entry_price, entry_delta, entry_iv`.

### [major] (cross-part) The range / sideways detector is designed twice with different rules, constants and output names.

- Evidence: B2.4 (l.282-339): `range_detector.py`, `mirror_bars`, `RANGE_LOOKBACK = 120`, width 2.0–8.0 ATR, sideways = EMA20/50/200 spread ≤ 1 ATR AND EMA50 20-bar change ≤ 0.5 ATR AND range; output `{lower, upper, lower_zone, upper_zone, lower_touches, upper_touches, width_atr, mid}`. C2 (l.216-336): `range_box.py`, negated series through `sb._swings/_members/_touches`, LOOKBACK 252, width 2.0–10.0 ATR, `ACTIVE_BARS 60`, `INSIDE_BARS 15`, sideways = EMA20/50 within 1 ATR AND EMA20 10-bar move < 0.5 ATR AND 15 closes inside; output `{low, high, zone_low, zone_high, n_low, n_high, pos_pct, sideways, reasons, ...}`.
- Fix: C's `range_box.py` is the module (it was prototyped and is what `ema_setup.analyze()` stores as `rng`); B's `chart_state` reads `rng` and sets `trend = "sideways"` iff `rng["sideways"]`; B's iron-condor constraint uses `rng.zone_low[0]` / `rng.zone_high[1]`. B's `range_detector.py` keeps only what C does not provide — `find_resistance_reject` and `find_breakdown` (the mirrored bear-side setups) — and is renamed `mirror_setups.py`. Delete B's `SIDEWAYS_*`, `RANGE_*` constants.

### [major] (cross-part) Term structure has two definitions with opposite scales.

- Evidence: A3.4 (l.617-623): `term_slope = (iv_front - iv_back) / iv_back`, "> +0.05 backwardation". B1.2/B1.3 (l.122, 139-140): `term_slope = iv_front / iv_back`, `TERM_EVENT = 1.05`, `TERM_CONTANGO = 0.95`; B3.3 l.455 tests `gauge["term_slope"] >= 1.0`.
- Fix: Store `iv_front`, `iv_back` (percent) and `term_ratio = iv_front / iv_back` in `iv_daily` and `signal.iv`; B's gauge and recommender read `term_ratio` against 1.05 / 0.95; the name `term_slope` is removed from all four parts.

### [major] (B) The 'iv > 3.0 means percent' heuristic corrupts real Cboe rows.

- Evidence: B0.2 (l.66-67): "`iv` > 3.0 → divide by 100 (no equity option has 300% IV on a delayed feed; a bridge percent always does)"; C3.2 `normalise_iv`: "v > 3.0 → v/100". Live probe of the Cboe MSFT file (2026-10-02 session, critic_integration_cboe_probe.py): a deep-ITM contract prints `"iv": 3.1099` as a FRACTION and the file's iv range is 0.1375..8.3157; A1.1's sanity filter deliberately keeps iv up to 5.0.
- Fix: Normalise by SOURCE, never by magnitude: A's `ContractRow.iv` is already a fraction; only `BridgePayloadSource` divides by 100. `opt_legs.norm_leg(row, unit="fraction"|"percent")` and `payoff.normalise_iv(v, unit=...)` take the unit from the chain's `source` and never guess.

### [major] (cross-part) The chart stop is defined three ways; the card, the sizing and the rules drawer would quote different numbers.

- Evidence: B2.6 (l.362-371): credit stop = `setup.zone[0] - LEVEL_PAD_ATR x ATR` (336.2 for LRCX), debit stop = `min(entry - STOP_ATR x ATR, zone_lo - pad)` (Curated convention from entry). C7 (l.911-912): "the chart stop for a credit idea is the stock level under the bounce low / under the trend line, supplied by the setup detector" and C5.1 uses the mockup's 338. D3.1 (l.1040): field `stop_atr` 'Stop, ATR below the setup — 1 ATR under the bounce low / trend line'; D1.7 (l.363): "stop_chart = signal.setup.stop (1 ATR under the bounce low / the trend line for a long)".
- Fix: B's `plan` is the single source: `setup.stop = plan.stop`, `setup.target = plan.target`. D's `stop_atr` field is B's `STOP_ATR` (measured from the ENTRY for debit trades) and `LEVEL_PAD_ATR` (0.25) is the credit pad; D's help text and D1.7 wording corrected to that. C's worked example notes 338 is a 0.1-ATR pad and the engine's default gives 336.2.

### [major] (cross-part) 'Chance of profit' for debit and time strategies is computed three different ways.

- Evidence: B4.6 (l.732-735): bull_call = `black_scholes(..., K=breakeven, sigma=leg.iv).prob_itm`; calendar = lognormal mass between model breakevens with `sigma = iv30`. C3.8 (l.445): lognormal over every profitable grid interval with σ = the ATM IV of the horizon expiry (fallback iv30, HV20). D1.7 (l.361): "debit families P(S_T beyond breakeven) from the lognormal with sigma = iv30".
- Fix: One function, C's `payoff.pop(family, legs, spot, sigma_h, T_h, xs, ys)`, called by B's picker for every non-credit family (it also covers calendars); credit families keep `1 − |Δ_short|` (and the condor's two-sided form). Picks carry `pop` (0..1) + `pop_kind` ∈ {keep, profit}; the payoff dict carries C's `pop{label, value, basis, model, model_basis}`.

### [major] (cross-part) The Telegram push is designed three times with different dedupe keys and transports.

- Evidence: A4.4 (l.724-735): `option_push.push_new_ideas(db, run_on)` via `scripts._common.send_telegram(cfg, html)` to the vault chat, dedupe on `option_signal.pushed_at` + 5 days per legs. B7.4 (l.1087-1099): `option_exits.notify(cards)` via `send_telegram`. D4 (l.1163-1248): `services/telegram.py` (own POST because `send_telegram` has no chat_id — verified _common.py:577-580) + `telegram_push.run(db, as_of, dry_run)`, per-member `chat_id`, `option_idea_push` table, 45-day prune.
- Fix: D's design (per-member chat id is required on a multi-member platform). A's nightly step 5 becomes `telegram_push.run(db, as_of=run_on, dry_run=args.telegram_dry_run)`; `option_signal.pushed_at` and `option_push.py` are dropped; B7.4's `notify` is deleted and the exit-line push stays on the existing Discord path.

### [major] (cross-part) Two job-record tables and two `/options/status` return types.

- Evidence: A2.1 (l.445-460) `option_jobs(job, run_on, source, started_at, finished_at, symbols, ok, errors, rows, detail JSON, note)`; A4.3 `GET /options/status` returns JSON `{run_on, finished_at, symbols, ok, errors, rows, source, stale, running, next_due}`. D1.12 (l.550-563) `option_job_runs(job, as_of, started_at, finished_at, tickers, done, failed, pushed, note)` + `services/job_runs.py`; D1.3 `GET /options/status` returns the HTML strip `_options_status.html`.
- Fix: A's `OptionJob` model (its `detail` JSON is what the per-ticker pill needs) + D's `services/job_runs.py` (`start/finish/latest/missed`) as the service over it, plus a `pushed` column. `GET /options/status` = D's HTML strip; `GET /options/badge` JSON carries A's fields (`run_on, finished_at, ok, errors, stale, running, job_missed, ideas_new, urgent, watch`).

### [major] (cross-part) Two `option_basket` schemas and two caps.

- Evidence: A2.1 (l.309-327): `owner_key` ("u<id>" or "system"), `user_id` nullable, `active`, `added_on` String(10), cap 100 (A6.2). D1.12 (l.471-482): `user_id` NOT NULL, `pos`, `added_on` DateTime, no `active`, `MAX_BASKET = 60` justified by the job budget (D1.2).
- Fix: A's columns (owner_key keeps the system basket without a NULL in a UNIQUE) plus D's `pos`; `added_on` stays an ET date string; `MAX_BASKET = 60` (D's budget: 60 tickers × ~2.5 s inside the 30-minute task limit A4.1 sets).

### [major] (cross-part) Route paths and basket-import source names differ between A, C and D.

- Evidence: A (l.26, A4.7, A6.2): `POST /options/{sym}/refresh`, `POST /options/{sym}/iv/bootstrap`, `POST /options/basket/import?from=ivscan|universe|watchlist|positions`; C3.11: `GET /options/{symbol}/payoff`. D1.3: `POST /options/refresh/{symbol}`, `POST /options/live/{symbol}`, `GET /options/payoff/{symbol}`, `POST /options/basket/import` body `source=paste|watchlist|ivscan_list|scanner|screener`. In the repo, `prefs.ivscan_universe` is the typed 'My list' (ivscan.py:85-105) and `IVScanItem` rows are the TWS scanner output (models.py:904-915) — A's `ivscan` and D's `ivscan_list` name different things.
- Fix: D's `verb/{symbol}` convention for every endpoint (`/options/refresh/{symbol}`, `/options/live/{symbol}`, `/options/payoff/{symbol}`, `/options/chain/{symbol}`, `/options/ticket/{symbol}`); the IV bootstrap is a step inside `/options/live/{symbol}`. Import sources: `paste | watchlist | ivscan_list (prefs.ivscan_universe) | ivscan_scan (IVScanItem rows) | scanner (live bridge /scan) | screener (spread_candidates) | positions`.

### [major] (D) The two `toggle`-triggered lazy loads never fire: the DOM toggle event is dispatched on the <details> element and does not bubble to its children.

- Evidence: D2.1 (l.673-674): `<div id="optRulesBody" ... hx-get="/options/rules?family=shared" hx-trigger="toggle[this.parentElement.open] once">` inside `<details id="optRules">`; D2.5 (l.811): `<div hx-get="/options/chain/..." hx-trigger="toggle once">` inside `<details>`. No template in the repo uses a toggle trigger (grep `hx-trigger="...toggle` → none), so there is no precedent proving it works.
- Fix: `hx-trigger="toggle from:closest details once"` on both divs (or move the `hx-get` onto the `<details>` with `hx-target` the body div). Add the case to D8.1 step 2/12.

### [major] (D) Gating the whole new router with `require_menu("options")` breaks the legacy Watchlist Options tab's Track form, which D routes into the new handler.

- Evidence: D1.1 (l.59): "insert above app.include_router(options_routes.router): app.include_router(options_page_routes.router, dependencies=[Depends(menus.require_menu("options"))])"; D1.11: the new `POST /options/track` answers the legacy form. The legacy router is included with NO menu gate (main.py:229-231) because `_chart_pane.html:69-73` loads `/options/{sym}` for every Watchlist user; `_options_analysis.html:129` posts to `/options/track`. `require_menu` 303-redirects a user lacking the key (menus.py:141-151), so a member without the new `options` grant loses tracking from the Watchlist tab.
- Fix: Split the new module into two routers: `router` (page + fragments, gated by `require_menu("options")`) and `track_router` with only `POST /track` (require_user, registered before the legacy router). Or give the new endpoint its own path (`/options/track-idea`) and leave the legacy `/options/track` untouched.

### [major] (cross-part) The chart-state reader (B) and the setup-dict extension (C) compute the same things twice and name them differently.

- Evidence: C1.7/C2.5 (l.187-214, 326): `ema_setup.analyze()` gains `tl`, `tl_bounce`, `rng` (and needs `times`); condition keys `t1`/`r1`. B2.1 (l.217-243): `chart_state.read()` calls `support_bounce.find`, `range_detector.find_range`, and reads a `trendline` dict `{direction, slope_per_day, value_today, touches: 3, first_touch, last_touch, broken, value_at}`; A5.2 stores `setup.trend_line{value_today, value_at, touches: 3, slope_per_day}`; C's `tl` has `slope_per_bar`, `touches` as a LIST, `n_touches`, `p1/p2`.
- Fix: `chart_state.read()` calls `ema_setup.analyze(bars, long_bars)` once and reads `sup / tl / tl_bounce / rng` from it (no second detector run). The stored `setup` carries C's dicts verbatim under `setup.sup`, `setup.tl`, `setup.tl_bounce`, `setup.rng`, plus B's `kind, direction, level, zone, touches(int), quality, plan, levels, evidence`. Names: `slope_per_bar`, `n_touches`, `p1/p2`; `value_at` is filled by the nightly job calling `trend_line.find(..., at=[every expiry in the snapshot])`. Note in the release that `t1`/`r1` join COND_KEYS (every `sym_conds` reader sees two more switches) and budget range_box's 5-10 ms inside `setups_for_many` on the Sector / IV Rank list requests (C1.7's 'not on a request path' is only true after the 15-min cache warms).

### [major] (cross-part) The prefs hash includes sizing inputs, so changing the account value invalidates every cached pick.

- Evidence: B4.1 (l.628-629): `out["shared"]["risk_pct"], out["shared"]["nlv"] = tp["risk_pct"], tp["nlv"]` inside the merged dict; B4.2 Candidate carries `contracts, sizing`; A5.1: `prefs_hash = sha1(canonical_json(merged_prefs))`; D3.1 `read()`: "hash ... excluding exit lines that do not change a pick".
- Fix: `option_prefs.prefs_hash()` hashes pick-relevant fields only (delta bands, DTE, widths, credit/reward floors, liquidity, `earnings_rule`, `monthly_only`, `chart_constraint`); `nlv`, `risk_pct`, exit lines and telegram settings are excluded. `option_sizing.size()` runs at read time over the cached picks (microseconds) and the Telegram line sizes from the stored NLV at push time.

### [major] (D) The order-ticket text contradicts B's ticket in structure and in the direction of the entry condition.

- Evidence: D2.8 (l.921-934): one `<pre>` block, "Trigger (optional): send only while LRCX is at or above 340 (support) — TWS: Condition → Price ≥ 340". B6.1/B6.2 (l.891-892, 926-940): entry condition `last <= level x (1 + offset_pct/100)` = 341.92 ("enter on the dip ... rather than chase"), rendered as THREE orders (entry DAY, chart-stop GTC conditional, take-profit GTC) per broker via `order_ticket.render(ticket, broker)`.
- Fix: `_options_ticket.html` prints `order_ticket.render(ticket, broker)` for TWS and moomoo (B6.2/B6.3) inside the `<pre>`; D's own text block and the '≥ 340' trigger are deleted. Decision 10's see-and-approve wording (data age, 'Tracking only') stays as D's footer lines.

### [minor] (cross-part) Strategy keys and the word 'family' are used inconsistently.

- Evidence: C3.1 (l.358-360) family keys `leaps`, `diagonal`, `custom`; A5.2 l.826 `"key": "long_call"`; B3.2 / D3.1 use `buy_call, buy_put, bull_call, bear_put, leaps_call, bull_put, bear_call, iron_condor, calendar, diagonal_call`. B's `family` = `credit_vertical|debit_vertical|long|leaps|condor|time`; D's `family` = `credit|debit|condor|time`; C's `family` parameter is the strategy key.
- Fix: `strategy` = B's ten keys (+ `custom` for the expander only); `family` = B's six engine families; `tab` = D's five drawer tabs. C's `payoff.build(..., strategy=...)` takes the strategy key and derives the family via `option_prefs.family_of()`.

### [minor] (D) D's `iv_daily.latest` keys do not match A's columns.

- Evidence: D0 (l.30): `{"on","iv30","hv20","hv60","iv_rank","iv_pct","skew","term_slope","n"}`. A2.1 IVDaily: `skew25`, `skew_norm`, `iv_n`, `state`, `iv_front`, `iv_back`, `iv_hv`, `em30`, `earnings_date`, `earnings_days`.
- Fix: D reads A's column names (`iv_n`, `skew25`, `state`, `term_ratio` after the term fix); `option_words.iv_rank_words` / `gauge` take the `signal.iv` dict (A5.2), which already contains the verdict.

### [minor] (D) Width in listed strikes is not ticker-relative and differs from B's ATR band.

- Evidence: D3.1 (l.1022): `("width_strikes", "Width, in strikes", ..., 2, 1, 5)` — two strikes is $2 on a $1-spaced chain and $10 on a $5-spaced one regardless of ATR. B4.1 (l.561): `width_atr_lo / width_atr_hi` 0.5 / 1.5 ATR with "the $ figure is shown next to it". CLAUDE.md 'Normalized strategy parameters': every threshold ATR-relative.
- Fix: B's `width_atr_lo/hi` (and `wing_atr_lo/hi` for the condor) in the drawer, with the $ translation `rule_words` renders from the ticker's ATR.

### [minor] (D) D's earnings rule offers a third value ('yes' = allowed for every strategy) that the design forbids.

- Evidence: D3.1 (l.1011): `("earnings", ..., "no", ["no", "defined_risk", "yes"])`; D3.2 choice labels `yes → "allowed"`. Design §5.1: "Earnings inside the expiry — not allowed (or defined-risk only)"; B4.1 `earnings_rule ∈ {none_inside, defined_risk_only}`.
- Fix: Two values, B's names (`none_inside`, `defined_risk_only`), labels 'not allowed' / 'defined-risk trades only'.

### [minor] (cross-part) The rule stop is drawn for different families in C and D.

- Evidence: C3.9 (l.456): rule stop "credit families only (CREDIT_FAMILIES); for debit families the member's rule is the chart stop". D1.7 (l.362): "For the debit family: where today[i] ≤ −rules.debit.loss_stop_pct/100 · debit_paid (default 50)". B5.2 table (l.835-842) defines a $ rule stop for every family except LEAPS (`premium_stop_pct` for long/debit/time).
- Fix: Draw the rule-stop marker and hline for every family that has a $ rule in B5.2 (credit 20 % of max loss; long/debit/time `premium_stop_pct` of the debit); LEAPS has none. C3.9's 'credit families only' sentence is replaced by B5.2's table.

### [minor] (cross-part) The headline sentence is composed at write time in A and at render time in D.

- Evidence: A5.2 (l.846): "every number the page shows comes from this row (no recomputation on read)"; `option_signal.headline` Text column. D1.4 (l.230): `"headline": option_words.headline(sig, chosen) if sig else None`; D2.7 `headline(sig, chosen)` templates.
- Fix: The engine calls `option_words.headline(...)` (D2.7's templates, moved to services) at write time and stores the sentence in `option_signal.headline`; `_options_card.html` prints `sig.headline`. The chip click never changes the sentence (D1.4's own rule).

### [minor] (D) Two citations point at the wrong file or an existing number is wrong.

- Evidence: D1.2 (l.127-128): "Cboe at the nightly scan's 1.5 s pause (`deploy/spread_scan.py:323`, `PAUSE = 1.5`)" — `PAUSE = 1.5` is `app/services/spread_scan.py:321`; `deploy/spread_scan.py:39` is the `--pause` argparse default. D6 strip example `job ✓ 06:31 MYT` and D2.1 — A's task runs at 07:15 MYT (after the 06:30 spread scan, verified setup_spread_scan_task.ps1:4-6 and setup_portfolio_check_task.ps1:26 `$At = "06:00"`).
- Fix: Cite `app/services/spread_scan.py:321-323`; use 07:17 MYT in the examples; `job_runs.missed` threshold 08:00 MYT stays.

### [minor] (B) B's claim that the bull put path can feed `bull_put` 'untouched' needs one more adapter detail, and B7.3's LEAPS trend stop needs data the sweep does not fetch.

- Evidence: B7.3 (l.1063): LEAPS 'trend stop — chart.w_uptrend False on two consecutive weekly closes' requires `ema_setup.analyze(..., long_bars)` with ~10 years of bars (ema_setup.py:354-361 WEEKLY_200_MIN) but B7.3's `option_exits.sweep` is "spread_monitor.sweep over option_trades: one chain fetch per underlying" (l.1081-1083) — no bars fetch.
- Fix: `option_exits.sweep` fetches `ema_setup.setup_for(sym, deep=True)` once per underlying that holds a LEAPS/diagonal trade (cached 15 min) and passes `w_uptrend` into `grade()`; document the extra Yahoo call in A4.1's budget.

### [minor] (cross-part) Ownership of `routes/options_page.py` and of the nightly script is stated differently.

- Evidence: A0 (l.26): "the page router is Part C's; these four are the data endpoints this part owns"; C7 (l.919): "Part D (page) owns GET /options/{symbol}/payoff"; D0 (l.39): "Nightly job entry point deploy/options_nightly.py (C)" — C never mentions a nightly script; A4.1 designs it.
- Fix: D owns `app/routes/options_page.py` (A's four data endpoints move into it under D's paths); A owns `deploy/options_nightly.py` and `setup_options_nightly_task.ps1`; C owns `trend_line.py`, `range_box.py`, `payoff.py`, `_payoff_chart.html`.

### [minor] (cross-part) The test infrastructure the three parts assume does not exist yet.

- Evidence: A8, B9, C6, D8.2 all place files under `dashboard_tst/tests/` and run pytest; `ls dashboard_tst/tests` → no such directory; `app/requirements.txt` has no pytest (A8 notes a `requirements-dev.txt`).
- Fix: One `requirements-dev.txt` (pytest) and one `dashboard_tst/tests/` tree with the fixtures directory A8/B9 describe (`tests/fixtures/options/`), created in step 1 before the first engine lands; README Contents line for it.


### Reconciliation (field names / shapes to unify)

- Migration: ONE file `alembic/versions/f4a5b6c7d8e9_options_module.py` (down_revision e2f3a4b5c6d7) creating option_basket, option_chain_snapshot, iv_daily, option_signal, user_option_prefs, option_jobs, option_trades, option_trade_checks, option_idea_push (+ B's option_spreads copy step). Drop `f0a1b2c3d4e5` and `f9a0b1c2d3e4`.
- Positions store: `option_trades` + `option_trade_checks` (B7.1) for every strategy from step 1; drop `option_positions` / `option_position_legs` (D); `option_spreads` read-only legacy.
- Job record: table `option_jobs` / model `OptionJob` (A2.1, + `pushed` int); service `services/job_runs.py` with `start/finish/latest/missed` (D6). Drop `option_job_runs` / `OptionJobRun`.
- Basket: `option_basket(owner_key, user_id nullable, symbol, source, note, active, added_on String(10), pos)`; `MAX_BASKET = 60`.
- Prefs: module `app/services/option_prefs.py` (drop `option_rules.py`); blocks `shared, credit_vertical, debit_vertical, long, leaps, condor, time` (B4.1 field names, ATR widths, `extrinsic_pct_max` = % of spot, `earnings_rule ∈ {none_inside, defined_risk_only}`, `max_leg_spread`, `min_oi`); drawer tabs `shared|credit|debit|condor|time` where `debit` renders `debit_vertical + long + leaps`. `prefs_hash` = first 12 hex of sha1 over pick-relevant fields only (no nlv/risk_pct/exit lines/telegram).
- option_signal: A's row-per-(symbol, snap_on, kind, prefs_hash) model; read only through `option_store.card_for(db, symbol, user)`; `trend` String ∈ up|down|sideways|unclear; `setup = {kind, direction, level, zone, touches:int, quality, close, trend_days, atr, ema{e20,e50,e200}, plan{entry,stop,target,r}, levels{support,resistance}, sup, tl, tl_bounce, rng, evidence[]}` where `sup/tl/tl_bounce/rng` are C's dicts verbatim; `iv` = A5.2 dict with `term_ratio` instead of `term_slope`, `iv_n`, `state`, `skew25`, `skew_norm`, `earnings_date`, `earnings_days`, `verdict`, `verdict_why`, `gates`, `provisional`; `headline` stored at write time.
- strategies: flat list of all ten `{key, label, fit ∈ recommended|also_fits|rejected, score, step, why, must_happen, reasons[], reason_key, shown}`; `fit` value `also_fits` (not `also`); `reason_key` from D2.7's fixed list.
- Strategy keys: `buy_call, buy_put, bull_call, bear_put, leaps_call, bull_put, bear_call, iron_condor, calendar, diagonal_call` (+ `custom` in the payoff expander only). `family` = B's `credit_vertical|debit_vertical|long|leaps|condor|time`; `tab` = D's five.
- Leg: `{expiry, right, strike, side:"sell"|"buy", qty:+int, price, bid, ask, iv (fraction), delta (signed), oi, volume}`; `payoff.Leg.from_dict()` derives the signed qty; `OptionTrade.legs` adds `entry_price, entry_delta, entry_iv`; `OptionTradeCheck.legs` per-day `{mid, delta, iv}`.
- Pick: B4.2 Candidate with `pop` (0..1) + `pop_kind ∈ keep|profit` (drop `pop_wording`, `pop_word`, `pop_text`); `max_loss`, `max_profit` positive $ per contract; `breakevens: []` (list, not `breakeven`); `chart_stop`, `chart_stop_pl`, `rule_stop_pl`, `sizing` (B5.3), `words`, `why`, `liquidity.tier ∈ clean|limit|wide|thin|unknown`, `checks[]`.
- Payoff: C's `payoff.build()` dict (markers list with `kind`, `hlines`, `pop{label,value,basis,model,model_basis}`, `units{mode:'$'|'R', r_dollars, r_basis}`) with `max_loss` reported positive; `pnl/leg_value/curve_at` gain `iv_bump: float = 0.0` for B5; route `GET /options/payoff/{symbol}?strategy=&pick=&units=$|R` → `_payoff_chart.html` (SVG). No canvas, no `thPayoffLoad`, no `r_value`/`palette` JSON.
- Trend line: C's `tl` dict (`direction, p1{time,price}, p2, i1, slope_per_bar, slope_atr, touches[{time,price}], n_touches, span_bars, value_today, value_at{date:price}, broken, last_break, warning, residual_atr, atr, channel`); `chart_trendline = trend_line.overlay(tl, tl_bounce)`; drawn as C4.4's line series. Drop `trend_line{touches:int, slope_per_day}` (A), `trendline{a,b}` (D) and `slope_per_day` everywhere.
- Range: C's `range_box.find()` → `rng` dict (`low, high, zone_low, zone_high, n_low, n_high, touches_low, touches_high, width_atr, pos_pct, stack_flat, sideways, reasons`); B's `chart_state.trend == "sideways"` iff `rng.sideways`; B's `range_detector.py` → `mirror_setups.py` (resistance_reject, failed_support only).
- Term structure: `iv_front`, `iv_back` (pct), `term_ratio = iv_front/iv_back`; thresholds `TERM_EVENT 1.05`, `TERM_CONTANGO 0.95`; remove `term_slope`.
- IV units: `ContractRow.iv` fraction (A normalises at the source; only `BridgePayloadSource` divides by 100); `iv_daily.*` and `signal.iv.*` percent; `norm_leg(row, unit=)` / `normalise_iv(v, unit=)` take the unit explicitly — no `> 3.0` heuristic. IV bootstrap series from bridge 1.6 in PERCENT, stored as-is.
- Stops: `setup.stop = plan.stop` (B2.6): credit `zone_lo − LEVEL_PAD_ATR×ATR`, debit `min(entry − STOP_ATR×ATR, zone_lo − pad)`; rule stop per B5.2 table for every family except LEAPS; both drawn on the payoff chart (decision 7).
- Sizing: `option_sizing.size()` (B5) at read time; `contracts = min(floor(budget/loss_at_chart_stop), floor(nlv×10%/max_loss))`, 0 allowed with the note; D never computes `qty`.
- Ticket: B's `order_ticket.build()` + `render(ticket, broker)`; `_options_ticket.html` prints the rendered text for TWS and moomoo plus D's footer lines. Entry condition per B6.1 (dip to `level × (1+offset_pct)`), not '≥ support'.
- Telegram: D's `services/telegram.py` (per-member `chat_id`, vault token via `scripts._common.telegram_env`) + `services/telegram_push.py::run(db, as_of, dry_run)` + `option_idea_push`; called by A's nightly step 5; drop `option_push.py`, `option_signal.pushed_at`, `option_exits.notify`.
- Routes (all in D's `app/routes/options_page.py`): `GET /options`, `/options/basket`, `POST /options/basket/add|remove|import` (sources `paste|watchlist|ivscan_list|ivscan_scan|scanner|screener|positions`), `GET /options/card/{symbol}`, `/options/picks/{symbol}`, `/options/chart/{symbol}`, `/options/payoff/{symbol}`, `/options/chain/{symbol}`, `/options/ticket/{symbol}`, `/options/rules` (+POST, `/reset`), `POST /options/refresh/{symbol}`, `POST /options/live/{symbol}` (grades in-request, persists only the IV series + optional NLV), `POST /options/track` on an UNGATED sub-router, `GET /options/positions`, `GET /options/status` (HTML strip), `GET /options/badge` (JSON incl. job fields). Drop A's `/options/{sym}/refresh`, `/options/{sym}/iv/bootstrap`, C's `/options/{symbol}/payoff`.
- Bridge: `TradeHunterIBKRBridge/1.6`; `/iv?symbol=X&series=1` → `series: [{on, iv (percent)}]` ≤ 400 points oldest first; 'older than 1.6' messages.
- Nightly: `deploy/options_nightly.py` + `TST-Options-Nightly` 07:15 MYT (A4); writes `option_jobs`; `job ✓ 07:17 MYT` in the UI examples; `missed` = no finished run for the last ET trading day by 08:00 MYT.
- Chart spread overlay: `chart_spread = {legs: [{strike, right, side, label}], breakevens: [], expiry, label}` (old `{short, long, breakeven}` still accepted); `window.thChartSetStrikes(spec)` is the exposed handle (D2.4) and `SPREAD.legs` the template field (C4.4 item 5).
- Tests: `dashboard_tst/requirements-dev.txt` (pytest) + `dashboard_tst/tests/` with `tests/fixtures/options/` shared by A8/B9/C6/D8.2.
