"""dashboard_tst platform — members-only trend & swing collaboration app.

A separate FastAPI application from the legacy operational fork
(``dashboard_tst/server.py``). See ``dashboard_tst/DESIGN.md`` for the
blueprint. Run from the ``dashboard_tst/`` directory:

    uvicorn app.main:app --reload

Security posture is baked in from the start: session auth, invite-only
accounts, an admin-only control plane, and NO order execution in this
process (the execution plane lives trusted-side).
"""

__version__ = "0.0.1"
