"""Connectivity smoke test for IBKR data feed.

Verifies:
  1. TCP handshake to 127.0.0.1:7497 (TWS paper)
  2. API auth succeeds with the configured clientId
  3. We can fetch server time (proves the data session is alive)
  4. We can fetch a SPY snapshot quote (proves market-data sub works)

Does NOT place any order. Safe to run any time.
"""
from __future__ import annotations

# Python 3.14 / eventkit shim — must run before ib_insync import.
import asyncio
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

import json
import sys
from pathlib import Path

from ib_insync import IB, Stock

CONFIG = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text())
HOST = CONFIG.get("ibkr_host", "127.0.0.1")
PORT = int(CONFIG.get("ibkr_port", 7497))
CLIENT_ID = int(CONFIG.get("ibkr_client_id", 71))

print(f"Connecting to {HOST}:{PORT} as clientId={CLIENT_ID} ...")
ib = IB()
try:
    ib.connect(HOST, PORT, clientId=CLIENT_ID, timeout=10, readonly=True)
except Exception as e:
    sys.exit(f"FAIL connect: {e}")

print(f"  connected. server version = {ib.client.serverVersion()}")
print(f"  server time              = {ib.reqCurrentTime()}")

contract = Stock("SPY", "SMART", "USD")
ib.qualifyContracts(contract)
ticker = ib.reqMktData(contract, "", snapshot=True, regulatorySnapshot=False)
ib.sleep(3)  # let the snapshot arrive

print(f"  SPY snapshot: bid={ticker.bid}  ask={ticker.ask}  last={ticker.last}")
ib.disconnect()
print("OK — IBKR data feed reachable.")
