"""Resolve the running build's git commit SHA (read at import).

Shown on the login page + /status so you can tell at a glance whether a
machine (esp. Hermes) is actually running the latest code after a deploy.
Reads .git directly (no `git` on PATH required, no subprocess). The value
is fixed at process start, so it reflects the commit the app was launched
on -- which is exactly what we want (it only changes on restart).
"""
from __future__ import annotations

from pathlib import Path


def _read_build() -> str:
    here = Path(__file__).resolve()
    for base in here.parents:
        git = base / ".git"
        if not git.is_dir():
            continue
        try:
            head = (git / "HEAD").read_text(encoding="utf-8").strip()
            if not head.startswith("ref:"):
                return head[:7]  # detached HEAD -> raw sha
            ref = head[4:].strip()
            loose = git / ref
            if loose.exists():
                return loose.read_text(encoding="utf-8").strip()[:7]
            packed = git / "packed-refs"
            if packed.exists():
                for line in packed.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith(("#", "^")) and line.endswith(ref):
                        return line.split(" ", 1)[0][:7]
        except Exception:
            return "unknown"
        return "unknown"
    return "unknown"


BUILD = _read_build()
