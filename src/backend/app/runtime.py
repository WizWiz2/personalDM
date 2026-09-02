from __future__ import annotations

import faulthandler
import os
import sys
import traceback
from pathlib import Path
from typing import Any, TextIO

from app.models.jobs import GenerationPhase

_INSTALLED = False
_CRASH_LOG_HANDLE: TextIO | None = None
_GUARDS = (
    "actor_turn_authority",
    "actor_memory_observability",
    "narrator_memory_audit",
    "systemless_authority",
    "mixed_actor_response",
    "narrator_quality_recovery",
    "narration_failure_containment",
    "session_zero_finalize",
    "session_zero_placeholder",
    "planner_compound",
    "semantic_authority",
)


def _install_crash_diagnostics() -> None:
    """Persist fatal/unhandled Python diagnostics without changing failure semantics."""
    global _CRASH_LOG_HANDLE
    if _CRASH_LOG_HANDLE is not None:
        return
    path = Path(os.getenv("PDM_CRASH_LOG", "data/personal-dm-crash.log"))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a", encoding="utf-8", buffering=1)
    except OSError:
        # Diagnostics must never prevent the game from starting.
        try:
            faulthandler.enable(all_threads=True)
        except (RuntimeError, OSError):
            pass
        return

    _CRASH_LOG_HANDLE = handle
    try:
        faulthandler.enable(file=handle, all_threads=True)
    except (RuntimeError, OSError):
        pass

    previous_hook = sys.excepthook

    def logged_excepthook(exc_type, exc_value, exc_traceback):
        try:
            handle.write("\n=== UNHANDLED PERSONALDM EXCEPTION ===\n")
            traceback.print_exception(
                exc_type,
                exc_value,
                exc_traceback,
                file=handle,
            )
            handle.flush()
        finally:
            previous_hook(exc_type, exc_value, exc_traceback)

    sys.excepthook = logged_excepthook


def install_runtime() -> None:
    """Install compatibility guards, then replace lexical semantics with agent-owned policy."""
    global _INSTALLED
    if _INSTALLED:
        return

    _install_crash_diagnostics()

    from app.services.actor_memory_observability_guard import (
        install as install_actor_memory_observability,
    )
    from app.services.actor_turn_authority_guard import install as install_actor_turn_authority
    from app.services.mixed_actor_response_guard import install as install_mixed_actor_response
    from app.services.narration_failure_containment_guard import (
        install as install_narration_failure_containment,
    )
    from app.services.narrator_memory_audit_guard import install as install_narrator_memory_audit
    from app.services.narrator_quality_recovery_guard import (
        install as install_narrator_quality_recovery,
    )
    from app.services.planner_compound_guard import install as install_planner_compound
    from app.services.semantic_authority_guard import install as install_semantic_authority
    from app.services.session_zero_finalize_guard import install as install_session_zero_finalize
    from app.services.session_zero_placeholder_guard import (
        install as install_session_zero_placeholder,
    )
    from app.services.systemless_authority_guard import install as install_systemless_authority

    install_actor_turn_authority()
    install_systemless_authority()
    install_mixed_actor_response()
    install_actor_memory_observability()
    install_narrator_memory_audit()
    install_narrator_quality_recovery()
    install_narration_failure_containment()
    install_session_zero_finalize()
    install_session_zero_placeholder()
    install_planner_compound()
    # This policy intentionally installs last: legacy guards may still expose compatibility helpers,
    # but no lexical/regex semantic decision is allowed to remain authoritative in production.
    install_semantic_authority()
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
            "semantic_re_adjudication_on_failure",
            "repair_once",
            "guard_repetition",
            "contain_presentation_failure",
            "publish_accepted",
        ],
        "semantic_policy": {
            "ownership": "model",
            "sensory_vs_internal_state": "model",
            "addressed_response": "typed_planner_field",
            "npc_introduction_semantics": "model",
            "movement_intent_semantics": "model",
            "compound_action_coverage": "model_with_semantic_review",
            "narrator_memory_attribution": "independent_segment_audit",
            "plot_fact_recovery": "evidence_grounded_second_pass",
            "requires_check": "structurally_forbidden",
        },
        "crash_diagnostics": {
            "faulthandler": True,
            "unhandled_exception_log": os.getenv(
                "PDM_CRASH_LOG",
                "data/personal-dm-crash.log",
            ),
        },
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
