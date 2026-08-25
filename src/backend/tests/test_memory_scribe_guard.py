"""Compatibility test entrypoint while CI references the historical guard filename.

The regression itself lives under the domain-oriented normalization name.
"""

from test_memory_scribe_normalization import (  # noqa: F401
    test_supported_outcome_with_unknown_entity_becomes_canon_gap,
)

__all__ = ["test_supported_outcome_with_unknown_entity_becomes_canon_gap"]
