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
                    help="comma-separated timeframes to ingest. Each entry may "
                         "carry its own depth as TF:DAYS — e.g., "
                         "'3min:180,5min:180,daily:730' gives daily 2-year "
                         "history while intraday stays at 180d. Entries "
                         "without :DAYS use --seed-days.")
    ap.add_argument("--seed-days", type=int, default=180,
                    help="lookback in days for any timeframe that didn't "
                         "specify a per-tf depth in --timeframes. Default 180 "
                         "= the DITP P2 backtest target depth.")
    ap.add_argument("--force-seed", action="store_true",
                    help="re-seed every symbol regardless of existing data. "
                         "Use to EXTEND existing depth backward.")
    ap.add_argument("--universe", default="daily",
                    choices=["daily", "1min", "3min", "journal"],
                    help="how to resolve the symbol list. 'daily' = every symbol "
                         "with a daily parquet (full backtest universe, ~1500). "
                         "'journal' = symbols from recent journal events (~50, "
                         "the default of ibkr_history.py update --universe). "
                         "IGNORED when --symbols-file is set.")
    ap.add_argument("--symbols-file",
                    help="Read the universe from a plain-text file (one symbol "
                         "per line, '#' comments and blank lines skipped). "
                         "Use this for pure-IBKR Hermes runs where neither "
                         "yfinance nor Wikipedia is available — pre-generate "
                         "the file with resources/universe_full.txt or any "
                         "custom list. Overrides --universe when set.")
    ap.add_argument("--pacing", type=float, default=7.0,
                    help="seconds between IBKR requests (under 60-per-600s cap)")
    args = ap.parse_args()

    # Parse --timeframes — each entry can be either "3min" or "3min:180".
    # Returns (timeframes_list, per_tf_days_dict). Any tf without :DAYS uses
    # args.seed_days as the fallback.
    timeframes: list[str] = []
    lookback_days_by_tf: dict[str, int] = {}
    for spec in args.timeframes.split(","):
        spec = spec.strip()
        if not spec:
            continue
        if ":" in spec:
            tf, days_str = spec.split(":", 1)
            tf = tf.strip()
            try:
                days = int(days_str.strip())
            except ValueError:
                ap.error(f"--timeframes entry {spec!r}: days must be int")
            timeframes.append(tf)
            lookback_days_by_tf[tf] = days
        else:
            timeframes.append(spec)
            lookback_days_by_tf[spec] = args.seed_days

    # Log file lives alongside the data it documents — honours cfg["data_root"]
    from _common import get_data_root
    log_dir = get_data_root()
    tf_label = "-".join(timeframes)
    # Pick the LARGEST seed-days seen for the filename — captures the run's
    # outermost lookback ("180d" for 3m/5m runs, "730d" if daily is in the mix).
    max_days = max(lookback_days_by_tf.values()) if lookback_days_by_tf else args.seed_days
    log_path = log_dir / f"_ingest_{tf_label}_{max_days}d_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    def log(msg: str) -> None:
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(line, flush=True)

    log(f"wait_and_ingest: timeframes={timeframes} "
        f"seed_days_by_tf={lookback_days_by_tf} "
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
    if args.symbols_file:
        # Pure-IBKR mode — read the symbol list from a static file (no yfinance,
        # no Wikipedia, no dependency on existing parquets). The file is
        # typically resources/universe_full.txt, generated on a host that has
        # network access to Wikipedia. Reserved Windows names should already
        # be filtered when the file was generated; we re-filter defensively.
        from pathlib import Path as _P
        symbols_path = _P(args.symbols_file)
        if not symbols_path.is_absolute():
            symbols_path = SKILL_DIR / symbols_path
        if not symbols_path.exists():
            log(f"FATAL: --symbols-file {symbols_path} not found")
            return 2
        RESERVED = {'CON','PRN','AUX','NUL',
                    'COM1','COM2','COM3','COM4','COM5','COM6','COM7','COM8','COM9',
                    'LPT1','LPT2','LPT3','LPT4','LPT5','LPT6','LPT7','LPT8','LPT9'}
        raw = symbols_path.read_text(encoding="utf-8").splitlines()
        symbols = sorted({
            s.strip().upper() for s in raw
            if s.strip() and not s.lstrip().startswith("#")
        } - RESERVED)
        log(f"universe (file={symbols_path.name}): {len(symbols)} symbols")
    else:
        symbols = resolve_universe(args.universe)
        log(f"universe ({args.universe}): {len(symbols)} symbols")

    log("starting bulk_update ...")
    from ibkr_history import bulk_update  # type: ignore
    try:
        results = bulk_update(
            symbols, timeframes,
            lookback_days_for_new=args.seed_days,
            lookback_days_by_tf=lookback_days_by_tf,
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
