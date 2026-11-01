"""Context Keeper — modular Python package.

Version: 0.9.3 (kept in sync with :data:`cklib.config.VERSION`).
"""

from .config import VERSION
from .core import ContextKeeper, FocusResult, ProjectContext

__all__ = ["VERSION", "ContextKeeper", "FocusResult", "ProjectContext"]
__version__ = VERSION
