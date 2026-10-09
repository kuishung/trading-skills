# dashboard_tst/bridge/ — the member IBKR connector (2.0)

The small program each member runs **on their own PC**, next to their own TWS / IB
Gateway, that feeds the Options page with option data from the member's own IBKR login
(OPTIONS_V2_DESIGN.md §5). Members download it from the Options page as
`TradeHunter-IBKR-Connector-<version>.zip` (built from this folder by
`app/services/opt_connector_pkg.py`).

## Why it exists

The Options page takes every option figure from IBKR and nothing else, and **TWS runs on
each member's machine under their own login**. The web app never connects to IBKR itself:
only the Hermes collector (clientId 89) and members' connectors do. So the browser asks
the connector on `127.0.0.1` for a chain and relays it to the server, which validates it
and stores it in the **shared pool** (`opt_quote`): every contract carries its time, its
source (`member` + who) and its market data type, so one member's live read helps every
other member looking at that ticker.

```
browser (app.tradehunter.net) ──fetch──> 127.0.0.1:9224 ──ib_insync──> your TWS (read-only)
                              ──POST───> server: validate + store in the shared pool
```

Browsers permit an HTTPS page to fetch `http://127.0.0.1` (loopback counts as a
trustworthy origin), which is what makes this work with nothing exposed.

## Contents

- `ibkr_bridge.py` — the connector (2.0). Read-only (`readonly=True`, no order path).
  Settings page `GET /` + `POST /settings` (same-origin only), `/health` (cached state),
  `/chain2` (a chain for a fetch-window spec), `/underlying` (spot + a year of daily bars
  and IV), `/account`; the 1.x `/chain`, `/iv`, `/scan` unchanged for the legacy hidden pages.
- `th_ibkr.py` — the IBKR fetch library shared with the Hermes collector (Part B, not
  this folder's connector code; the connector imports it as a sibling module).
- `install_bridge.ps1` — the one-time installer (PS 5.1, ASCII): Python 3.12 check
  (offers `winget install -e --id Python.Python.3.12`), `pip install --user ib_insync`,
  Startup shortcut, `tradehunter://start-bridge` handler, starts the connector and opens
  its settings page. `-Uninstall` removes the shortcut and the handler.
- `start_ibkr_bridge.bat` — launcher (uses `py -3.12`; extra arguments pass through).
- `requirements.txt` — just `ib_insync`.

## Install (member, once per PC)

1. Download the zip from the Options page and unzip it anywhere (keep the folder there).
2. Right-click `install_bridge.ps1` > **Run with PowerShell**. Or:
   `powershell -ExecutionPolicy Bypass -File install_bridge.ps1`
3. In the settings page that opens, http://127.0.0.1:9224/, pick the TWS port
   (TWS live 7496 / TWS paper 7497 / Gateway live 4001 / Gateway paper 4002) and press
   **Save & reconnect**.

In TWS: **File > Global Configuration > API > Settings > Enable ActiveX and Socket
Clients**, and leave **Read-Only API** ticked — a hard guarantee at the TWS end that no
API client can place an order.

**Python 3.12 is required.** `ib_insync` imports `eventkit`, which calls
`asyncio.get_event_loop()` at import time — removed in 3.14.

The installer's **`tradehunter://start-bridge`** handler is what makes the page's
"Start" button work: a web page cannot launch a local program, so a custom URL protocol
(the mechanism Zoom and Teams links use) runs the launcher; Chrome asks permission the
first time. **Security:** the registered command is fixed and the URL argument (`%1`) is
deliberately **not** passed to the shell, so no website can land text on a command line.

## Settings

`%APPDATA%\TradeHunter\connector.json` (`~/.config/tradehunter/connector.json` off
Windows), written by the settings page (and with the defaults on first start):

```json
{"tws_host": "127.0.0.1", "tws_port": 7496, "client_id": 86, "max_lines": 40, "allowed_origins": []}
```

Bounds: port 1-65535, client ID 0-999999, market-data lines 5-200, at most 20 extra
origins (`scheme://host[:port]`). A bad field in a hand-edited file falls back to its
default and is shown on the settings page. Command-line flags override the file for one
run: `--port`, `--tws-host`, `--client-id`, `--max-lines`, `--origin` (repeatable),
`--config`, `--bridge-port`, `--strike-window` (legacy `/chain`). A Save on the settings
page replaces the command-line values for the rest of that run.

## Endpoints

| Endpoint | Returns |
|---|---|
| `GET /` | the settings page (inline CSS + JS, no CDN; live status line polled every 2 s) |
| `POST /settings` | JSON or form; **same-origin only**: writes `connector.json`, reconnects; `{ok, config, path, reconnecting}` or 400 `{ok: false, error, errors}` |
| `GET /health` | `{ok, version, tws_connected, connected, tws, client_id, mdt, account_type, error}` from cached state (no IB call) |
| `GET /chain2?symbol=&spec=<json>` | `th_ibkr.fetch` (chain_defs + fresh spot -> `plan(spec)` -> `quote`) + `{ok: true, connector_version, partial}`; 20 s cache per (symbol, spec); 150 s timeout, the read's deadline 15 s before it (a long read returns what it read, `partial: true`) |
| `GET /underlying?symbol=` | `{ok, symbol, spot, mdt, bars: [last 260], iv_series: [last 260]}`; the history is pulled once per symbol per day (an empty answer from IBKR is an error, not cached) |
| `GET /account` | `{ok, net_liquidation, currency}` |
| `GET /chain`, `/iv`, `/scan` | the 1.x replies, unchanged |

Errors: bad input 400 `{ok: false, error}`; an IBKR failure 200 `{ok: false, error}` (the
1.x convention the pages read); a `/chain2` read that waited for another one and has too
little time left 200 `{ok: false, busy: true, error}` (the page reports it as a failed read).

## Security

The connector can read your account, so it answers only **allow-listed origins**: any
HTTPS host in `tradehunter.net`, `http://127.0.0.1` / `http://localhost` on the dev
ports 8000-8099, plus extras from the settings page or `--origin`; with Private Network
Access preflight headers for the real site. A browser request from another site with no
`Origin` at all (`Sec-Fetch-Site: cross-site` - an `<img>`, a no-cors fetch) is refused
on every endpoint but the settings page. It binds `127.0.0.1` only. Added in 2.0: requests whose
`Host` header is not a loopback name are refused (DNS rebinding), and `POST /settings`
accepts only the connector's own page (`Origin` / `Sec-Fetch-Site` / `Referer` checked, no
CORS header, a cross-origin preflight is refused) so no website can repoint it.

## Changelog

### 2026-10-09 — connector 2.0 review fixes: chunked reads that fit, no leaked lines, no cross-site reads
From the six-lens review of the v4.133 build (Options v2), before it shipped:
- **A big chain no longer times out with nothing** (review #16/#19). A large-cap's default
  window (~2,000 listed contracts, ~45-50 waves) took longer than the 110 s cap, the read
  was cancelled with every row thrown away, and the member's loop asked for the same
  ticker forever. Now `/chain2` passes the spec's `max_expiries` / `max_side` (the server
  hands members chunks of up to 6 expiries, 25 strikes a side) to `th_ibkr.plan`, and the
  read gets a deadline 15 s under the connector's own limit (now 150 s; the page waits
  160 s): `th_ibkr.quote` then starts no new wave, cuts the open one short and returns
  the rows read so far with `"partial": true` (always in the answer). After waiting > 1 s
  for another read with < 20 s left, it answers `{ok: false, busy: true, error}` instead
  of an empty read (the `busy` flag added at integration, `ConnectorBusy`, so the answer
  matches the server/page contract). The timeout text no longer tells the member to LOWER
  the lines (that made reads slower).
- **Market-data lines can no longer leak** (review #20, `th_ibkr.py`): the release of a
  wave's lines (and the spot's) is a `finally` that never awaits - unpaced cancels,
  counted afterwards against the message rate (`_Bucket.charge`). The old paced release
  was a cancellation point: a timeout landing there left lines streaming until the
  connector closed, eating the login's 100-line allowance the member's TWS shares.
- **Cross-site requests without an Origin do no work** (review #0): `<img>` / no-cors
  GETs from any page used to run `/chain2`, `/underlying`, `/scan`, `/account` in full
  (only the answer was unreadable) - holding the quote slot and up to 40 lines per
  request. Refused now when `Sec-Fetch-Site` says cross-site / same-site and no `Origin`
  is sent; the settings page stays reachable (the Options page links to it).
- **Loopback origins: dev ports 8000-8099 only** (review, low). 2.0 trusted every local
  port - any program on the member's PC could read `/account`. Others need an explicit
  extra origin (settings page / `--origin`).
- **`/underlying`: an empty history is an error, not cached for the day** (review #18,
  connector side): ib_insync answers a failed historical request with an empty list.
- `th_ibkr.py`: "contract does not exist" remembered 3 days (was 6 h - every evening's
  EOD pass re-asked IBKR about ~2,000 union strikes per large name); a cancelled wait for
  a historical slot gives the slot back.

Tested: `tests/test_connector.py`, `tests/test_th_ibkr.py`, `tests/test_opt_collector.py`
(the cross-site refusal and what stays open, the chunk + deadline + partial passthrough,
"busy", the dev-port allow-list, the empty-history error; plan's `max_expiries`, the
deadline returning the waves read so far and cutting an open window, a cancellation at
the release leaving no line open - which fails on the old release code).

### 2026-10-09 — connector 2.0: settings page, `/chain2`, `/underlying`, downloadable zip
User: *"for option data, I want every data to be from IBKR"* and *"the user will use his
own IBKR API ... when the option data gets updated from any user using it IBKR live data
other users will also benefit"*; Q&A: *"I need a link for user to download the IBKR
connector and it can set the setting there. When it runs it shows a green pill; if it is
not running it shows red."* OPTIONS_V2_DESIGN.md §5 (Part D). The bridge becomes the
member connector that feeds the shared option pool:

- **Settings in the connector.** `connector.json` (`%APPDATA%\TradeHunter\`) with
  defaults and bounds; `GET /` is a self-contained settings page (TWS host, port with the
  four presets TWS live 7496 / TWS paper 7497 / Gateway live 4001 / Gateway paper 4002,
  client ID, market-data lines, extra origins, a live status line, **Save & reconnect**).
  `POST /settings` is same-origin only. CLI flags still override for one run. The file
  is created with the defaults on first start so a member can find it.
- **Connects by itself.** A keeper on the IB loop connects at start-up and reconnects
  after a drop (5 s backoff doubling to 30 s) and at once after a Save, so the Options
  page's pill turns green without a request. After connecting it reads one SPY quote so
  the status shows live / delayed before the first chain; the account type (paper = an
  account id starting with D) comes from `managedAccounts()`.
- **`/health` answers from cached state** (measured 2-25 ms against the real process):
  `{ok, version: "2.0", tws_connected, tws, client_id, mdt, account_type, error}`; the
  1.x `connected` key stays for the legacy pages. Not polled into the console log.
- **`/chain2?symbol=&spec=`**: the spec (design §3.2) is validated, then `th_ibkr.fetch`
  runs (a th_ibkr without `fetch` gets the same chain_defs + spot -> plan -> quote
  steps), one chain at a time (the line limit is shared by every client of the login),
  20 s cache per (symbol, spec), 110 s timeout so the page's 120 s fetch gets a JSON error.
  NaN in any reply goes out as `null`.
- **`/underlying?symbol=`**: fresh spot (20 s cache) + one year of daily bars and IBKR's
  daily 30-day IV (last 260 each), pulled once per symbol per day — the member's
  contribution to `opt_underlying_daily` when Hermes has not read the history yet.
- **`/account`** returns `{ok, net_liquidation, currency}` (th_ibkr.account).
- **`th_ibkr.py` (new here, shared with the Hermes collector) subscribes option contracts
  with generic tick `"101"` only** (`OPT_TICKS`: open interest, ticks 27/28), not the
  design's `"100,101,106"`: 100 and 106 are UNDERLYING ticks (the stock's option volume /
  IV) and on an option contract they risk error 321, which empties the whole chain. Day
  volume and the model greeks / IV come with the default tick set — the same choice 1.4
  proved (below). Contracts IBKR sent nothing for (no bid, ask, last or delta) are left out
  of a `/chain2` reply, so a dead feed never overwrites good shared data with blanks.
- **Kept unchanged:** `/chain`, `/iv`, `/scan` (1.6 code, for the legacy hidden pages),
  the origin allow-list (now also any `http://127.0.0.1:*` / `http://localhost:*` port, and
  extras from the settings file), the PNA preflight, read-only connect, the clientId walk,
  one connector per port + retiring an earlier copy. The legacy `/chain` now also waits its
  turn behind a `/chain2` read instead of doubling the lines in use.
- **New hardening:** a request whose `Host` is not loopback is refused (DNS rebinding);
  the settings page sends `X-Frame-Options: DENY` and a CSP.
- **Installer** (`install_bridge.ps1`, ASCII for PS 5.1): checks `py -3.12`, offers
  `winget install -e --id Python.Python.3.12` (asks y/n), `pip install --user -r
  requirements.txt`, unblocks the unzipped files, then the Startup shortcut (now
  minimised) and the `tradehunter://` handler as before, starts the connector and opens
  http://127.0.0.1:9224/. Keeps the window open at the end ("Run with PowerShell" closes
  it otherwise). `-Uninstall` unchanged; new `-SkipPython`, `-NoStart`, `-NoPause`.
- **Download:** `app/services/opt_connector_pkg.build_zip()` zips this folder in memory
  (`ibkr_bridge.py`, `th_ibkr.py`, the .bat, the installer, requirements, a generated
  README.txt) as `TradeHunter-IBKR-Connector-2.0.zip`, CRLF for the Windows text files.

Tested: `tests/test_connector.py` (80 cases, no ib_insync, no network): config defaults /
round trip / corrupt file / bounds, the origin and Host rules, `POST /settings`
same-origin only (JSON, form, cross-origin Origin / Sec-Fetch-Site / Referer, foreign
Host, invalid values), `/health` shape and < 200 ms on a live HTTP server, `/chain2`
routing + cache + timeout + 400s on a stub th_ibkr, `/underlying`, `/account`, the keeper
(read-only connect, paper account, reconnect on Save, unreachable TWS), the zip's contents
and name, the installer's text. Also run for real under Python 3.12 with ib_insync on a
spare port aimed at a closed TWS port (health, page, save, refusal, error path) and the
settings page clicked through in a browser; the installer parsed by PowerShell 5.1 and
dry-run with every side effect skipped. **Not yet run against a live TWS** — that is the
laptop's first step after deploy.

### 2026-10-04 — bridge 1.6: `/iv?symbol=X&series=1`, the dated daily IV series (percent)
The Options page's **Live** button bootstraps a year of IV history into the server's
`iv_daily` table from the member's own TWS (OPTIONS_MODULE_DESIGN.md II.2.13 / Part A
§A4.6), so a basket name gets an IV rank with a real window the first evening instead of
"forming" for sixty days. The bridge already fetched the dated daily
`OPTION_IMPLIED_VOLATILITY` bars for `/iv`; it only summarised them. Additive:

- `GET /iv?symbol=LRCX&series=1` adds `"series": [{"on": "YYYY-MM-DD", "iv": 31.2}, ...]`
  to the reply - **PERCENT** (`round(close * 100, 1)`, the unit `iv_current` / `iv_low` /
  `iv_high` in the same reply already use), **oldest first**, at most **400** points
  (`IV_SERIES_MAX`). The series is also returned on the "not enough history" reply (fewer
  than 30 points), since the server can store whatever exists.
- Without `series=1` the reply is byte-for-byte what 1.5 sent, so the IV Rank page and the
  Watchlist tab keep working on either bridge version. The `/iv` cache key now carries the
  flag, so the two shapes never serve each other.
- `server_version` -> `TradeHunterIBKRBridge/1.6`. The server's `BRIDGE_MIN_VERSION` is
  `"1.6"`; when the Options card's Live press finds no `series` in the reply, its IV line
  says "Your bridge is older than 1.6 - restart `bridge\start_ibkr_bridge.bat`" (the chain
  still grades live). Nothing else changed: `/chain`, `/account`, `/scan`, `/health` and
  the origin rules are 1.5's.
- `_bar_day()` reads the bar date as a `datetime.date` (what `formatDate=1` gives for day
  bars on current ib_insync) or a raw `YYYYMMDD` string, so an older build does not drop
  the dates.

Server side the series is stored AS-IS by `option_store.bootstrap_iv` (bounded `0.1 <= iv <=
1000`, dates within the last 400 days, never overwriting a day the server read from Cboe /
Alpaca itself). Tested: the handler on a stubbed `reqHistoricalDataAsync` (dated bars ->
`series` oldest first, percent, capped at 400; `series` absent without the flag; the
`< 30` reply still carries it); a live TWS check is the laptop's step-1 walkthrough item.

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
