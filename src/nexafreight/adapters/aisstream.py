"""Compatibility re-export — T-026.

The canonical AISStream adapter implementation lives at:
    nexafreight.adapters.feed.aisstream

This placeholder existed before T-026 and may be referenced by early
router stubs.  It is replaced here with a thin re-export so that any
code importing from the old path continues to work without duplicating
business logic.

Once all importers have been updated to use the canonical path, this
file can be removed.  Do not add business logic here.
"""

from nexafreight.adapters.feed.aisstream import (  # noqa: F401
    AISStreamAdapter,
)

__all__ = ["AISStreamAdapter"]
