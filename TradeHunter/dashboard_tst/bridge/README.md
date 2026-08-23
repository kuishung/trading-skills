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
  Endpoints: `/health`, `/chain`, `/iv`, `/account`.
- `start_ibkr_bridge.bat` — launcher (uses `py -3.12`).
- `requirements.txt` — just `ib_insync`.

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
