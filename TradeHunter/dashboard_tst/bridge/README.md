# dashboard_tst/bridge/ — the per-member IBKR bridge

A small program each member runs **on their own PC**, next to their own TWS.

## Why it exists

TradeHunter is a shared, server-hosted platform, but **TWS runs on each member's
machine under their own login**. A server-side IBKR client cannot work:

- it would use one account's data and one account's balance for everybody,
- position sizing is `20% of max loss < 2% of NLV` — the wrong NLV silently
  produces the wrong contract count,
- and it would mean exposing somebody's TWS to the network.

So the browser talks to a bridge on `127.0.0.1` and the server only grades rules.
Same shape as the TradingView bridge already used by "Plot on TV".

```
browser (tradehunter.net) ──fetch──> 127.0.0.1:9224 ──ib_insync──> your TWS
                          ──POST───> server: rule evaluation + rendering only
```

Browsers permit an HTTPS page to fetch `http://127.0.0.1` (loopback counts as a
trustworthy origin), which is what makes this work with nothing exposed.

## Contents

- `ibkr_bridge.py` — the bridge. Read-only (`readonly=True`, no order path).
  Endpoints: `/health`, `/chain` (`side=put` since 1.3), `/iv`, `/account`, `/scan` (1.1).
- `start_ibkr_bridge.bat` — launcher (uses `py -3.12`).
- `requirements.txt` — just `ib_insync`.

## One-time setup (recommended)

Run on **your PC**, in PowerShell:

```
powershell -ExecutionPolicy Bypass -File install_bridge.ps1
```

It does two independent things, neither needing admin rights:

1. **Auto-start** - a shortcut in your Startup folder, so the bridge runs whenever
   Windows does. This is the real fix for "is the bridge up?", which caused every
   false "bridge is down" report during development.
2. **A working "Start the bridge" button** in the web app. A web page *cannot* launch a
   local program - browsers forbid it, and no amount of code changes that. So this
   registers a custom URL protocol, `tradehunter://start-bridge` (the mechanism Zoom and
   Teams links use); the button opens that URL and Windows runs the launcher. Chrome asks
   permission the first time, which is the point - your machine decides, not the page.

**Security:** the registered command is fixed and the URL argument (`%1`) is deliberately
**not** passed to the shell. If it were, any website could put arbitrary text after
`tradehunter://` and land it on a command line. The handler can only ever start this one
script, with no arguments.

Undo with `.\install_bridge.ps1 -Uninstall`.

## Install (once, per PC)

```
py -3.12 -m pip install -r requirements.txt
```

**Python 3.12 is required.** `ib_insync` imports `eventkit`, which calls
`asyncio.get_event_loop()` at import time — removed in 3.14.

## Run (whenever you want the Options tab)

```
start_ibkr_bridge.bat
```

Defaults to TWS on `127.0.0.1:7496` with clientId 86. Flags: `--port` (7497 TWS
paper, 4001/4002 IB Gateway live/paper), `--tws-host`, `--client-id`,
`--strike-window`, `--origin` (extra allowed browser origin).

In TWS: **API > Settings > Enable ActiveX and Socket Clients**, and leave
**Read-Only API** ticked — that is a hard guarantee at the TWS end that no API
client can place an order.

## Security

The bridge can read your account, so it answers only **allow-listed origins**
(`tradehunter.net` plus localhost dev ports). Without that, any site you visited
could read your balances off localhost. It binds `127.0.0.1` only — never
reachable from the network.

## Changelog

### 2026-09-19 — bridge 1.5: the chain no longer waits on fixed timers (20-21 s -> 8-10 s)
User: *"when i get data from IBKR, it is quite slow, what is the problem?"* Measured against a live
TWS (Saturday, market closed): `/account` 0.0 s, `/iv` 1.1-2.3 s, **`/chain` 20-22 s every time**.
A stage-by-stage probe of the chain path showed IBKR was not the slow part - the bridge was:

| Stage | Time | |
|---|---|---|
| connect / qualify stock / chain definition / qualify strikes | 0.2 + 0.2 + 0.6 + 0.8 s | fine |
| **spot price via `reqTickersAsync`** | **11.1 s** | a SNAPSHOT: IBKR holds it open until it "ends", up to 11 s whenever no fresh trade arrives (evenings, weekends, thin names). The close was in the ticker after ~3 s. |
| **option quotes: `asyncio.sleep(8.0)`** | **8.0 s** | fixed. Arrival curve: open interest ~1.0 s, greeks ~1.5-2.0 s, prices ~3.1-3.6 s, then NOTHING changes. |

- **`_spot()`** replaces the snapshot with a streaming subscription read as soon as it has a number:
  a live price the moment one exists, else the close after `SPOT_SETTLE` (0.8 s); ceiling
  `SPOT_WAIT` 6 s (3 s was tried first and failed HD / XOM - out of hours the close lands at
  3.1-3.6 s). Falls back once to delayed-frozen if the current type yields nothing. It now runs
  CONCURRENTLY with `reqSecDefOptParams` - neither needs the other.
- **`_quote()`** closes its window when the picture is complete and has stopped changing: every
  contract priced AND greeks on at least half (deep OTM strikes never get a model) AND no newly
  populated field for `QUOTE_QUIET` 1 s; or nothing new at all for `QUOTE_STALL` 3 s (a feed with no
  entitlement used to cost the full 8 s). Both halves are required: greeks arrive before prices out
  of hours and after them in hours. `quote_wait` (8 s) remains the ceiling.
- **`_forget()`** - found while testing. ib_insync keeps one Ticker per contract for the life of the
  connection and `cancelMktData` does not clear it, so "has the data arrived?" read off a reused
  ticker said yes instantly; and a spot could be hours old on a bridge left running all day. Tickers
  are blanked before each subscription - except the second half of the entitlement probe
  (`fresh=False`), because TWS does not resend model greeks to a re-subscription seconds later and
  wiping them returned chains with prices and no deltas.
- **A "delayed" verdict now expires** (`MKT_RECHECK` 15 min). It was sticky for the life of the
  process, so a bridge started at the weekend - when even a fully entitled account gets few model
  greeks - kept serving 15-minute-old quotes through Monday's session.
- **No deltas -> one retry with the other market data type** (prices kept, verdict unchanged). A
  chain without deltas cannot be graded, and out of hours TWS sometimes sends no greeks under one
  type and does under the other (COST, 29 puts: 0 then 11). Response field `greeks_from_delayed`.

Verified by running 1.5 on port 9225 (clientId 98) BESIDE the running 1.3 and asking both for the
same chains: ABBV 9.3 vs 20.6 s, PEP 9.8 vs 20.1, ORCL 9.8 vs 20.9, MRK 11.3 vs 21.1, LIN 12.0 vs
21.0, V 10.1 s (23/23 deltas), COST 14.0 s with the retry. Deltas equal or better than 1.3 on every
symbol, plus open interest and volume on every leg (1.4, now confirmed live: tick 101 arrives on
this account). All numbers are OUT OF HOURS; in market hours a live price arrives in under a
second, so the chain should be faster still - not yet measured. `/iv` and `/account` were never
the problem and are unchanged.

### 2026-09-19 — bridge 1.4: open interest on every option leg
User: *"when select option trade, we need the open interest and volume so that it is liquid
enough"*. `/chain` rows have always had an `oi` field, but it was hard-coded `None`: open
interest is not in IBKR's default tick set, and `_quote` subscribed with an empty generic-tick
list. It now asks for **generic tick 101** on option contracts, and `_row` reads the answer off
the tick for the contract's own side (27 = call, 28 = put - `ib_insync`'s
`callOpenInterest` / `putOpenInterest`). Still a streaming subscription, never a snapshot:
IBKR refuses generic ticks on snapshots. Day `volume` needed nothing (default tick 8) but is
now reported as a real `0` when TWS says zero, and `None` only when no tick arrived - the
server treats "unknown" and "zero" differently.

The response also carries `oi_ok` (did ANY leg report open interest - false on a feed that
carries none) and `bridge` (this version), so the server can tell "thin" from "not reported".

**A running bridge must be restarted to pick this up** (close its window, run
`start_ibkr_bridge.bat`). Until then the web app shows the open-interest check as "not
reported" - a warning, not a block. Not yet verified against a live TWS (written with TWS
off); the row building was checked against `ib_insync` 0.9.86's Ticker on Python 3.12.

### 2026-09-18 — bridge 1.3: `/chain?side=put`, the bull put spread view
The symmetric window (10 strikes each side of the price) is right for reading a chain and
wrong for finding a short put at delta 0.20-0.25: on META ($689, $5 strikes) it stops at 640,
and the 0.20-0.25 deltas sit at 610-620. `side=put` quotes PUTS only, 26 strikes below the
price and 2 above (never further than 28% under it). Dropping the calls pays for the reach:
29 contracts instead of 42, so fewer market-data lines than before. The response carries
`put_side: true` and an empty `calls`; ATM IV is then read off the puts.

Verified live: META 2026-10-30 returned 29 puts from 560 to 700 with deltas 0.09-0.53. Before
the open TWS returns no bid/ask (only `last`); the server-side rules flag that.

### 2026-09-18 — bridge 1.2: one bridge per port; a new start replaces the old one
User restarted the bridge for 1.1 and the IV Rank page still said "older version without the
scanner". Three bridges were running (05:47, 20:56, 21:01). `HTTPServer` sets SO_REUSEADDR,
and on **Windows** that lets a second process bind a port that is already listened on, with
no error. All three "listened"; the OLDEST (1.0, from the morning) received every request,
so no restart could ever take effect. Every press of the web app's Start button or a second
run of the launcher added another invisible copy.

Two changes:
- `_ExclusiveServer` turns the flag off on Windows, so a second bind fails loudly.
- `_retire_other_copies()` runs first: any process LISTENING on the bridge port whose command
  line names `ibkr_bridge` is stopped, with its whole launcher tree (`cmd.exe` -> `py.exe` ->
  `python.exe`), so starting the bridge always means "run this one". Other programs on the
  port are left alone and reported. The startup banner now prints the version.

Verified on the affected PC: one launch through `start_ibkr_bridge.bat` stopped all three
old trees, `/health` reports 1.2 with a single listener, `/scan` and `/iv` answer for
`https://app.tradehunter.net`. A force-stopped copy may keep clientId 86 held in TWS; the
existing clientId walk (86 -> 87...) covers that.

### 2026-09-18 — bridge 1.1: `/scan`, the TWS "High IV Rank" scanner
New endpoint `/scan?iv_rank=30&price=100&volume=200000` for the platform's Options > IV Rank
page. It runs `reqScannerData` with scan code `SCAN_ivRank52w_DESC` (the API name of TWS's
"52 Week IV Rank" sort), location `STK.US.MAJOR`, and the scanner's own filter codes
`ivRank52wAbove` (in percent, like the TWS field), `priceAbove`, `volumeAbove`. Returns up to
50 symbols in rank order; cached 120 s per criteria. The scanner does not return the rank
figure itself, so the page reads that per ticker from the existing `/iv`.

Still read-only: a scanner subscription is market data, there is no order path. Same origin
allow-list as every other endpoint (verified: a foreign origin gets 403).

**A running bridge must be restarted to pick this up** (close its window, run
`start_ibkr_bridge.bat`). A 1.0 bridge answers `/scan` with "unknown endpoint", and the page
tells the member to restart it. Verified against live TWS: 8 symbols pre-market (the volume
floor is today's volume, so the list is short before the open and grows through the session).

### 2026-08-23 — step past a held clientId instead of demanding a TWS restart
TWS keeps a client slot registered when a process dies without disconnecting — a
force-kill, a crash, a closed lid — and the next connection on that id dies in the
handshake with an EMPTY error. The bridge now walks to the next free id (up to 6) and
remembers it, rather than telling the member to restart TWS. Verified by running a second
bridge demanding an id the first held: TWS answered "clientId 86 already in use" and it
came up on 87.

A genuinely unreachable TWS still fails immediately with its real message (connection
refused), so this never masks the case that actually needs attention.

### 2026-08-23 — allow the whole platform domain, not one hostname
The real cause of three rounds of "No IBKR bridge": the site is served from
**`https://app.tradehunter.net`**, and the allow-list held only the apex
`https://tradehunter.net`. A non-allow-listed origin gets a 403 with no CORS headers, and
the browser surfaces that as `TypeError: Failed to fetch` — identical to the bridge being
down, which is exactly what the panel then claimed.

Now any **HTTPS** host in `tradehunter.net` (apex or subdomain) is accepted. Still refused:
`https://tradehunter.net.evil.com`, `https://eviltradehunter.net`, and plain-HTTP
`http://app.tradehunter.net`.

### 2026-08-23 — Private Network Access preflight
The tab reported "no bridge" from **tradehunter.net** while the bridge was demonstrably
running and answering curl. Cause: **Chrome's Private Network Access** (104+). A page on a
PUBLIC origin reaching a PRIVATE address (127.0.0.1) is preflighted even for a simple GET,
and the browser drops the request unless the response carries
`Access-Control-Allow-Private-Network: true`. The bridge now echoes it when asked.

This could not show up in local testing: `localhost:8011 -> 127.0.0.1:9224` is
private-to-private, which never triggers PNA. Only the real HTTPS site does.

### 2026-08-23 — created
Extracted from the server (`app/services/ibkr_options.py`, deleted) when the
requirement landed that each member uses their own TWS login. Carries over the
hard-won fixes from the server-side version: qualify strikes before quoting (the
`reqSecDefOptParams` strike list is the union across all expirations, so slicing
it directly starves the chain and changes which strike looks closest to the
target delta), one subscribe-wait-read window instead of `reqTickersAsync`
(which waits for every contract and always burns its timeout on a delayed feed),
a sticky majority-based live/delayed probe, cache expiry stamped at store time,
and disconnect on exit so TWS releases the clientId.
