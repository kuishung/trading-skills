"""GUNS Setup 5 -- Break of First 1-Minute RTH Candle at 09:31 ET.

Re-exports build() from impl.py so `import guns_setup5` works for
strategy.load_known(). Implementation file is named impl.py (not
strategy.py) to avoid collision with the parent `strategy` package.
"""
from .impl import build, __version__   # noqa: F401

__all__ = ["build", "__version__"]
