from __future__ import annotations

from typing import Any

from app.models.jobs import GenerationPhase

_INSTALLED = False
_GUARDS = (
    "actor_turn_authority",
    "actor_memory_observability",
    "systemless_authority",
    "mixed_actor_response",
    "narrator_quality_recovery",
    "narration_failure_containment",
    "session_zero_finalize",
)


def install_runtime() -> None:
    """Install only compatibility guards whose invariants do not yet have explicit owners."""
    global _INSTALLED
    if _INSTALLED:
        return

    from app.services.actor_memory_observability_guard import (
        install as install_actor_memory_observability,
    )
    from app.services.actor_turn_authority_guard import install as install_actor_turn_authority
    from app.services.mixed_actor_response_guard import install as install_mixed_actor_response
    from app.services.narration_failure_containment_guard import (
        install as install_narration_failure_containment,
    )
    from app.services.narrator_quality_recovery_guard import (
        install as install_narrator_quality_recovery,
    )
    from app.services.session_zero_finalize_guard import install as install_session_zero_finalize
    from app.services.systemless_authority_guard import install as install_systemless_authority

    install_actor_turn_authority()
    install_systemless_authority()
    install_mixed_actor_response()
    install_actor_memory_observability()
    install_narrator_quality_recovery()
    install_narration_failure_containment()
    install_session_zero_finalize()
    _INSTALLED = True


def runtime_manifest() -> dict[str, Any]:
    """Return the auditable causal order used by the production runtime."""
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
        "guards": list(_GUARDS),
        "context_pipeline": list(ContextCompiler.DEFAULT_PROVIDER_NAMES),
        "turn_pipeline": [
            "reserve_user_turn",
            "compile_planner_context",
            "plan_authority",
            "execute_structured_boundary",
            "build_turn_authority",
            "materialize_structured_outcome",
            "compile_narrator_context",
            "render_narration",
            "validate_authority",
            "publish_assistant_turn",
            "enqueue_post_turn",
        ],
        "generation_phases": [phase.value for phase in GenerationPhase],
        "failure_semantics": {
            "before_prepare": "fail_without_world_compensation",
            "after_prepare_before_publish": "compensate_then_fail",
            "after_publish": "post_turn_is_independent_and_retriable",
        },
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
