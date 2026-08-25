from __future__ import annotations

from typing import Any

_INSTALLED = False
_COMPATIBILITY_GUARDS = (
    "actor_turn_authority",
    "actor_memory_observability",
    "systemless_authority",
    "round33_identity",
    "round34_live",
    "mixed_actor_response",
    "memory_scribe",
    "narrator_quality_recovery",
    "narration_failure_containment",
    "session_zero_finalize",
)


def install_runtime() -> None:
    """Install compatibility guards that have not yet been absorbed by their owners.

    New runtime invariants must be implemented by their owning service or by explicit
    composition. This bootstrap exists only while the remaining historical guards are
    migrated out of global class mutation.
    """
    global _INSTALLED
    if _INSTALLED:
        return

    from app.services.actor_memory_observability_guard import (
        install as install_actor_memory_observability,
    )
    from app.services.actor_turn_authority_guard import install as install_actor_turn_authority
    from app.services.memory_scribe_guard import install as install_memory_scribe
    from app.services.mixed_actor_response_guard import install as install_mixed_actor_response
    from app.services.narration_failure_containment_guard import (
        install as install_narration_failure_containment,
    )
    from app.services.narrator_quality_recovery_guard import (
        install as install_narrator_quality_recovery,
    )
    from app.services.round33_identity_guard import install as install_round33_identity
    from app.services.round34_live_guard import install as install_round34_live
    from app.services.session_zero_finalize_guard import install as install_session_zero_finalize
    from app.services.systemless_authority_guard import install as install_systemless_authority

    install_memory_scribe()
    install_actor_turn_authority()
    install_systemless_authority()
    install_round33_identity()
    install_round34_live()
    install_mixed_actor_response()
    install_actor_memory_observability()
    install_narrator_quality_recovery()
    install_narration_failure_containment()
    install_session_zero_finalize()
    _INSTALLED = True


def runtime_manifest() -> dict[str, Any]:
    """Return an auditable description of the active runtime composition."""
    install_runtime()

    from app.providers.llm_provider import LLMProvider
    from app.services.authority_narration_pipeline import AuthorityNarrationPipeline
    from app.services.context_compiler import ContextCompiler
    from app.services.memory_scribe import MemoryScribe
    from app.services.thesis_curator import ThesisCurator
    from app.services.turn_authority_planner import TurnAuthorityPlanner
    from app.services.turn_authority_validator import TurnAuthorityValidator
    from app.services.turn_runner import TurnRunner
    from app.services.turn_saga import TurnSaga

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
        "compatibility_guards": list(_COMPATIBILITY_GUARDS),
        # Keep the legacy key while external debugger consumers migrate. It now means
        # exactly the same thing and should monotonically shrink to an empty list.
        "guards": list(_COMPATIBILITY_GUARDS),
        "context_pipeline": list(ContextCompiler.DEFAULT_PROVIDER_NAMES),
        "turn_pipeline": [
            "compile_context",
            "plan_authority",
            "execute_structured_boundary",
            "build_turn_authority",
            "render_narration",
            "validate_authority",
            "materialize_structured_outcome",
            "commit",
            "enqueue_post_turn",
        ],
        "narration_pipeline": [
            "generate_draft",
            "guard_repetition",
            "validate_authority",
            "repair_once",
            "guard_repetition",
            "contain_presentation_failure",
            "publish_accepted",
        ],
        "turn_stream": identity(TurnRunner.run_turn_stream),
        "turn_saga": identity(TurnSaga.run_turn_stream),
        "provider_stream": identity(LLMProvider.generate_stream),
        "narration_pipeline_impl": identity(AuthorityNarrationPipeline.generate),
        "authority_planner": identity(TurnAuthorityPlanner.plan),
        "authority_validator": identity(TurnAuthorityValidator.validate),
        "context_compiler": identity(ContextCompiler.compile_context),
        "memory_parser": identity(MemoryScribe._parse_data),
        "thesis_reconcile": identity(ThesisCurator.reconcile),
        "post_turn_mode": "background",
    }
