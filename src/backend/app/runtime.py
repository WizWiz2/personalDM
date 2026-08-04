from __future__ import annotations

from typing import Any

_INSTALLED = False
_GUARDS = (
    "memory_scribe",
    "thesis_lifecycle",
)


def install_runtime() -> None:
    """Install the remaining global runtime guards exactly once.

    Context and narration composition are explicit dependencies. Runtime bootstrap now
    installs only the two legacy guards that still mutate public extension points.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    from app.services.memory_scribe_guard import install as install_memory_scribe
    from app.services.thesis_lifecycle_guard import install as install_thesis_lifecycle

    install_memory_scribe()
    install_thesis_lifecycle()
    _INSTALLED = True


def runtime_manifest() -> dict[str, Any]:
    """Return an auditable description of the active runtime composition."""
    install_runtime()

    from app.providers.llm_provider import LLMProvider
    from app.services.context_compiler import ContextCompiler
    from app.services.memory_scribe import MemoryScribe
    from app.services.narration_pipeline import NarrationPipelineProvider
    from app.services.thesis_curator import ThesisCurator
    from app.services.turn_runner import TurnRunner

    def identity(value: object) -> str:
        module = getattr(value, "__module__", type(value).__module__)
        name = getattr(
            value,
            "__qualname__",
            getattr(value, "__name__", type(value).__name__),
        )
        return f"{module}.{name}"

    return {
        "installed": _INSTALLED,
        "guards": list(_GUARDS),
        "context_pipeline": list(ContextCompiler.DEFAULT_PROVIDER_NAMES),
        "narration_pipeline": list(NarrationPipelineProvider.STAGES),
        "turn_stream": identity(TurnRunner.run_turn_stream),
        "provider_stream": identity(LLMProvider.generate_stream),
        "narration_provider_stream": identity(
            NarrationPipelineProvider.generate_stream
        ),
        "context_compiler": identity(ContextCompiler.compile_context),
        "memory_parser": identity(MemoryScribe._parse_data),
        "thesis_reconcile": identity(ThesisCurator.reconcile),
    }
