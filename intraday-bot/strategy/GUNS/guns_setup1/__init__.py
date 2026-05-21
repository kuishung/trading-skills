"""GUNS Setup 1 -- Break of Pre-Market High at 09:30 ET.

Re-exports build() from impl.py so `import guns_setup1` works for
strategy.load_known(). Implementation file is named impl.py (not
strategy.py) to avoid collision with the parent `strategy` package.
"""
from .impl import build, __version__   # noqa: F401

__all__ = ["build", "__version__"]
