"""Wait for a running ingest process to exit, then launch a new
ibkr_history bulk_update. One-shot operational glue — designed to be
used when you want to extend or replace an ingest without interleaving
two TWS sessions (clientId collision).

Git Bash's `ps -p PID` does NOT see native Windows process IDs, so a
bash watcher loop has to use `tasklist` for the wait. This script
wraps that pattern + the full-universe symbol resolution + the
bulk_update call into one launch-and-forget process.

Typical use:
    py scripts/wait_and_ingest.py --wait-pid 21732 \\
        --timeframes 3min --seed-days 180 --force-seed --universe daily

`--universe daily` means "all symbols with a daily parquet on disk"
(1500+ for our setup) — the broadest universe a backtest cares about.
The narrower `journal` source (default of ibkr_history.py update --universe)
is too tight for a 180-day backtest seed.

Logs everything (watcher status + bulk_update stdout/stderr) to
data/_ingest_<tf>_<lookback>d_<ts>.log so the user can tail it later.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# --- bootstrap (matches scanner.py / backtest.py shape) ---
_root = Path(__file__).resolve().parent.parent
for _p in [str(_root)] + [str(_root / s) for s in
        ("scripts", "resources", "strategy", "execution",
         "journal", "review", "dashboard")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)
SKILL_DIR = _root


def is_windows_pid_alive(pid: int) -> bool:
    """Check via `tasklist` (Windows-native) since Git Bash `ps -p` can't
    see processes that weren't spawned by the same bash session."""
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            text=True, stderr=subprocess.DEVNULL,
        )
        # tasklist returns "INFO: No tasks..." when PID is gone
        return "python" in out.lower() or "py.exe" in out.lower()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def resolve_universe(source: str) -> list[str]:
    """Resolve the symbol universe per the --universe option."""
    import bars_store  # type: ignore
    if source == "daily":
        return bars_store.list_symbols("daily")
    if source == "1min":
        return bars_store.list_symbols("1min")
    if source == "3min":
        return bars_store.list_symbols("3min")
    if source == "journal":
        from ibkr_history import build_universe  # type: ignore
        from scripts._common import load_config  # type: ignore
        return build_universe(load_config(), journal_days=30)
    raise ValueError(f"unknown --universe source: {source!r}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wait-pid", type=int,
                    help="Windows PID to wait for (poll every 60s via tasklist). "
                         "Skip this flag to launch immediately.")
    ap.add_argument("--timeframes", default="3min",
                    help="comma-separated timeframes to ingest (default: 3min)")
    ap.add_argument("--seed-days", type=int, default=180,
                    help="lookback in days per (symbol, timeframe). Default 180 "
                         "= the DITP P2 backtest target depth.")
    ap.add_argument("--force-seed", action="store_true",
                    help="re-seed every symbol regardless of existing data. "
                         "Use to EXTEND existing depth backward.")
    ap.add_argument("--universe", default="daily",
                    choices=["daily", "1min", "3min", "journal"],
                    help="how to resolve the symbol list. 'daily' = every symbol "
                         "with a daily parquet (full backtest universe, ~1500). "
                         "'journal' = symbols from recent journal events (~50, "
                         "the default of ibkr_history.py update --universe).")
    ap.add_argument("--pacing", type=float, default=7.0,
                    help="seconds between IBKR requests (under 60-per-600s cap)")
    args = ap.parse_args()

    timeframes = [tf.strip() for tf in args.timeframes.split(",") if tf.strip()]
    # Log file lives alongside the data it documents — honours cfg["data_root"]
    from _common import get_data_root
    log_dir = get_data_root()
    tf_label = "-".join(timeframes)
    log_path = log_dir / f"_ingest_{tf_label}_{args.seed_days}d_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    def log(msg: str) -> None:
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(line, flush=True)

    log(f"wait_and_ingest: timeframes={timeframes} seed_days={args.seed_days} "
        f"force_seed={args.force_seed} universe={args.universe} pacing={args.pacing}")

    # --- Wait phase ---
    if args.wait_pid:
        log(f"polling Windows PID {args.wait_pid} every 60s (tasklist) ...")
        n_polls = 0
        while is_windows_pid_alive(args.wait_pid):
            time.sleep(60)
            n_polls += 1
            if n_polls % 10 == 0:
                log(f"... still waiting (PID {args.wait_pid} alive after {n_polls} min)")
        log(f"PID {args.wait_pid} has exited after {n_polls} polls (~{n_polls} min)")

    # --- Launch phase ---
    log("resolving universe ...")
    symbols = resolve_universe(args.universe)
    log(f"universe ({args.universe}): {len(symbols)} symbols")

    log("starting bulk_update ...")
    from ibkr_history import bulk_update  # type: ignore
    try:
        results = bulk_update(
            symbols, timeframes,
            lookback_days_for_new=args.seed_days,
            pacing_s=args.pacing,
            force_seed=args.force_seed,
        )
        total = sum(results.values())
        log(f"DONE: {total} bars written across {len(results)} (symbol,timeframe) pairs")
        return 0
    except Exception as exc:
        log(f"FAILED: {exc!r}")
        raise


if __name__ == "__main__":
    sys.exit(main())
