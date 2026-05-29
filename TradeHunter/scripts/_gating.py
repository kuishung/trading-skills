"""Per-strategy runtime gating: ON/OFF + ARMED.

Each wired strategy has two independent LIVE filesystem flags:

  state/enabled_<strategy>.flag    presence => strategy is ON
                                   (full pipeline runs: scanner, resources,
                                   analysis, journaling)
                                   absence => strategy is OFF
                                   (no scanner, no resources, no analysis,
                                   no journal entries except a one-line
                                   strategy_off_skipped marker per
                                   scheduled fire)

  state/armed_<strategy>.flag      presence => strategy is ARMED
                                   (plans get submitted to Alpaca)
                                   absence => strategy is DISARMED
                                   (plans get journaled as entry_disarmed,
                                   no submission)

Three meaningful states per strategy:

  OFF                — nothing runs; saves compute. ARM state is
                       remembered but irrelevant.
  ON + DISARMED      — full pipeline runs, journal accumulates
                       everything (universe, rejections, plans), but
                       Execution layer is never called. This is the
                       "live paper-eval" mode for vetting a strategy.
  ON + ARMED         — full pipeline runs AND plans flow to Execution.

Why filesystem flags:
  - Atomic toggles with zero locking.
  - Survives dashboard restart.
  - Bot (trade_day.py) and dashboard (dashboard.py) can both touch the
    state without any JSON merge race.
  - Bot reads flags LIVE at the relevant checkpoint, so dashboard
    toggles take effect on the next scheduled fire / next submit
    attempt -- no bot restart needed.

Read timing:
  is_enabled() — checked at the TOP of trade_day._fire_strategy_entries,
                 before pick_universe / fetch_bars / evaluate. Toggling
                 OFF mid-session takes effect at the NEXT scheduled fire
                 (an already-firing strategy completes its current
                 entry phase).
  is_armed()   — checked at the SUBMIT site, right before
                 TradingClient.submit_order. Toggling disarm
                 mid-session takes effect on the very next entry.

Global --dry-run override:
  trade_day.py --dry-run still wins. It forces every submit path into
  log-only mode regardless of per-strategy ARM state. Used for replays
  / smoke tests.

Migration:
  - migrate_global_arm_flag()  — old single state/armed.flag => per-
                                 strategy armed_<name>.flag. One-shot.
  - bootstrap_from_config()    — first time the dashboard sees a
                                 strategy with no enabled_<name>.flag,
                                 seed it from cfg.strategies.<name>.enabled.
                                 After seeding, the flag wins; config is
                                 only the first-run default.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPTS_DIR.parent
STATE_DIR = SKILL_DIR / "state"

GLOBAL_ARM_FLAG = STATE_DIR / "armed.flag"   # legacy, migrated on first boot


def _arm_flag(strategy_name: str) -> Path:
    return STATE_DIR / f"armed_{strategy_name}.flag"


def _enable_flag(strategy_name: str) -> Path:
    return STATE_DIR / f"enabled_{strategy_name}.flag"


def _write_flag(path: Path, label: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"{label} at {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n",
        encoding="utf-8",
    )


def _remove_flag(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# ---------- ARM gate ----------

def is_armed(strategy_name: str) -> bool:
    return _arm_flag(strategy_name).exists()


def set_armed(strategy_name: str, value: bool) -> None:
    p = _arm_flag(strategy_name)
    if value:
        _write_flag(p, "armed")
    else:
        _remove_flag(p)


def all_armed_state(known: tuple[str, ...] | list[str]) -> dict[str, bool]:
    return {name: is_armed(name) for name in known}


def set_all_armed(known: tuple[str, ...] | list[str], value: bool) -> None:
    for name in known:
        set_armed(name, value)


# ---------- ENABLE (ON/OFF) gate ----------

def is_enabled(strategy_name: str) -> bool:
    return _enable_flag(strategy_name).exists()


def set_enabled(strategy_name: str, value: bool) -> None:
    p = _enable_flag(strategy_name)
    if value:
        _write_flag(p, "enabled")
    else:
        _remove_flag(p)


def all_enabled_state(known: tuple[str, ...] | list[str]) -> dict[str, bool]:
    return {name: is_enabled(name) for name in known}


def set_all_enabled(known: tuple[str, ...] | list[str], value: bool) -> None:
    for name in known:
        set_enabled(name, value)


# ---------- Migration ----------

def migrate_global_arm_flag(known: tuple[str, ...] | list[str]) -> dict[str, bool] | None:
    """If the legacy state/armed.flag exists, treat it as 'every strategy
    was armed' and write per-strategy arm flags for every name in `known`,
    then delete the global flag. Idempotent. Safe to call on every
    dashboard startup."""
    if not GLOBAL_ARM_FLAG.exists():
        return None
    set_all_armed(known, True)
    _remove_flag(GLOBAL_ARM_FLAG)
    sys.stderr.write(
        f"[_gating] migrated legacy armed.flag -> per-strategy armed flags for "
        f"{len(known)} known strategies.\n"
    )
    return all_armed_state(known)


# Backwards-compat alias for code paths still calling the old name.
migrate_global_flag = migrate_global_arm_flag


def bootstrap_from_config(cfg: dict, known: tuple[str, ...] | list[str]) -> dict[str, bool]:
    """First-run seeding for the ENABLE flag.

    For each strategy in `known`:
      - If state/enabled_<name>.flag already exists -> leave it alone
        (the user has been managing the flag from the dashboard; we
        must not stomp their state on restart).
      - Otherwise -> seed from cfg.strategies.<name>.enabled (default
        False if the key is absent). Write the flag if and only if
        the config default is True.

    Returns the resulting full enable map. Idempotent. Safe to call
    every dashboard boot.
    """
    strat_cfg = (cfg.get("strategies") or {}) if isinstance(cfg, dict) else {}
    for name in known:
        if _enable_flag(name).exists():
            continue   # user has managed this, do not stomp
        block = strat_cfg.get(name) or {}
        default = bool(block.get("enabled", False))
        if default:
            set_enabled(name, True)
        # If default is False, leave the flag absent => strategy is OFF.
    return all_enabled_state(known)


# ---------- Combined view ----------

def all_states(known: tuple[str, ...] | list[str]) -> dict[str, dict[str, bool]]:
    """Convenience: returns {name: {"enabled": bool, "armed": bool}}
    so a single filesystem walk feeds both the UI summary and any
    log/telemetry consumer."""
    out: dict[str, dict[str, bool]] = {}
    for name in known:
        out[name] = {
            "enabled": is_enabled(name),
            "armed":   is_armed(name),
        }
    return out


# ---------- Dev / debug CLI ----------

def _cli(argv: list[str]) -> int:
    """py scripts/_gating.py                                # show all states
       py scripts/_gating.py enable guns_setup1             # turn ON
       py scripts/_gating.py disable guns_setup5            # turn OFF
       py scripts/_gating.py arm guns_setup1                # ARM
       py scripts/_gating.py disarm guns_setup5             # DISARM
       py scripts/_gating.py enable-all / disable-all
       py scripts/_gating.py arm-all / disarm-all
    """
    # Late import: avoid the dashboard / bot circular surface.
    from strategy import KNOWN_STRATEGIES

    if not argv:
        for name, st in all_states(KNOWN_STRATEGIES).items():
            on = "ON " if st["enabled"] else "off"
            armed = "ARMED   " if st["armed"] else "disarmed"
            print(f"  {on}   {armed}   {name}")
        return 0
    op = argv[0]
    if op == "enable-all":
        set_all_enabled(KNOWN_STRATEGIES, True);  return _cli([])
    if op == "disable-all":
        set_all_enabled(KNOWN_STRATEGIES, False); return _cli([])
    if op == "arm-all":
        set_all_armed(KNOWN_STRATEGIES, True);    return _cli([])
    if op == "disarm-all":
        set_all_armed(KNOWN_STRATEGIES, False);   return _cli([])
    if op in ("enable", "disable") and len(argv) >= 2:
        set_enabled(argv[1], op == "enable");  return _cli([])
    if op in ("arm", "disarm") and len(argv) >= 2:
        set_armed(argv[1], op == "arm");       return _cli([])
    print(_cli.__doc__);  return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
