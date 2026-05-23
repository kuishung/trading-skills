"""OS — Opening Surge family.

Internal strategy (not from a course/PDF). Simplified mechanical
pre-market breakout play modeled on GUNS Setup 1 but stripped of the
catalyst + float qualitative gates so it can run fully automated with
no human in the loop.

Reference doc: strategies-reference/OS.md (in the worktree, not
intraday-bot/). Setup folders live under this directory as
strategy/OS/<setup_name>/.

Setups wired:
  os_breakout — Break of Pre-Market High at 09:30 ET (analogous to
                GUNS Setup 1, simpler universe filter)
"""
