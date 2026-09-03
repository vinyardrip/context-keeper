"""Context Keeper — modular Python package."""

from .config import VERSION
from .core import ContextKeeper, FocusResult

__all__ = ["VERSION", "ContextKeeper", "FocusResult"]
__version__ = VERSION