"""Deprecated compatibility hook for older simulation helpers.

Memory Scribe invariants now live directly in :mod:`app.services.memory_scribe`.
Production runtime must not install or monkeypatch anything from this module. The no-op
``install`` remains temporarily so existing simulation harnesses can migrate independently
without changing runtime semantics.
"""

from __future__ import annotations


def install() -> None:
    """Compatibility no-op; MemoryScribe already owns these invariants."""
    return None


__all__ = ["install"]
