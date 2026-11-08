"""Paper Trail: a multi-agent receipt scanner and price tracker that runs on your own hardware.

``__version__`` is looked up in the installed package's metadata on first access, not at
import: the lookup costs about 0.3 s, which every command would otherwise pay.
"""

import importlib.metadata
from functools import cache


@cache
def _version() -> str:
    return importlib.metadata.version("paper-trail")


def __getattr__(name: str) -> str:
    if name == "__version__":
        return _version()
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
