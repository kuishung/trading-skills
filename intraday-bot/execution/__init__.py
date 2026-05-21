"""Execution layer -- Alpaca bridge + active position management.

Receives plans from Strategy and:
  - Places stop-limit entries
  - Attaches OCO bracket on fill
  - Actively manages positions:
      * Moves stop to breakeven at the strategy's declared R-multiple
      * (Future) trailing stop when the position runs in our favor
      * (Future) reversal-close when the thesis breaks
  - EOD safety sweep at 15:58 ET (close_all_positions, cancel_orders)

The Execution layer is NOT fully strategy-agnostic in its exit policy.
Each strategy is expected to declare how it wants positions managed;
Execution honors that contract.

Today this layer is a single module (orchestrator.py); future splits
will probably extract position_manager.py and alpaca_client.py.
"""
