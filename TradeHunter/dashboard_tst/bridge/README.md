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
