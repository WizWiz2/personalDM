from __future__ import annotations

from typing import Any

_INSTALLED = False
_GUARDS = (
    "scene_context",
    "memory_context",
    "narration_validation",
    "memory_scribe",
    "thesis_lifecycle",
)


def install_runtime() -> None:
    """Install the complete Personal DM runtime exactly once.

    Every production entrypoint must call this function.  Keeping composition here
    prevents CLI, FastAPI and simulation tools from silently running different sets
    of guards because of import order.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    from app.services.memory_context_guard import install as install_memory_context
    from app.services.memory_scribe_guard import install as install_memory_scribe
    from app.services.narration_validation_guard import (
        install as install_narration_validation,
    )
    from app.services.scene_context_guard import install as install_scene_context
    from app.services.thesis_lifecycle_guard import install as install_thesis_lifecycle

    # The order is intentional. Scene state wraps the base compiler first; transient
    # memory then wraps that enriched compiler. The remaining guards patch distinct
    # public extension points.
    install_scene_context()
    install_memory_context()
    install_narration_validation()
    install_memory_scribe()
    install_thesis_lifecycle()
    _INSTALLED = True


def runtime_manifest() -> dict[str, Any]:
    """Return an auditable description of the active runtime composition."""
    install_runtime()

    from app.providers.llm_provider import LLMProvider
    from app.services.context_compiler import ContextCompiler
    from app.services.memory_scribe import MemoryScribe
    from app.services.thesis_curator import ThesisCurator
    from app.services.turn_runner import TurnRunner

    def identity(value: object) -> str:
        module = getattr(value, "__module__", type(value).__module__)
        name = getattr(value, "__qualname__", getattr(value, "__name__", type(value).__name__))
        return f"{module}.{name}"

    return {
        "installed": _INSTALLED,
        "guards": list(_GUARDS),
        "turn_stream": identity(TurnRunner.run_turn_stream),
        "provider_stream": identity(LLMProvider.generate_stream),
        "context_compiler": identity(ContextCompiler.compile_context),
        "memory_parser": identity(MemoryScribe._parse_data),
        "thesis_reconcile": identity(ThesisCurator.reconcile),
    }
