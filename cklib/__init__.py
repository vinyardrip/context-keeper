"""Context Keeper — modular Python package."""

from .config import VERSION
from .core import ContextKeeper, FocusResult, ProjectContext

__all__ = ["VERSION", "ContextKeeper", "FocusResult", "ProjectContext"]
__version__ = VERSION