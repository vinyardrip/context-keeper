"""Context Keeper — modular Python package.

The single source of truth for the version is
:data:`cklib.config.VERSION`; ``__version__`` mirrors it.
"""

from .config import VERSION
from .core import ContextKeeper, FocusResult, ProjectContext

__all__ = ["VERSION", "ContextKeeper", "FocusResult", "ProjectContext"]
__version__ = VERSION
