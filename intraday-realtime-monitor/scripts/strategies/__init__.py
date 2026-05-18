"""Strategy modules.

Each strategy file exports an ``evaluate_setup(bar, profile, state)`` function
that returns either an order spec dict or None. The dispatcher in
monitor.py imports the strategy and calls evaluate_setup on every bar.

The same function is called identically from live, dry-run, and replay
modes — that's the testability invariant. The function never knows which
mode it's running under.
"""
