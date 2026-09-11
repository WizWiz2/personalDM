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
    "location_profile",
    "dead_turn",
    "semantic_authority",
    "performance_telemetry",
    "quality_stabilization",
    "player_quote_provenance",
    "truth_engine_relationship_receipts",
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
    """Install compatibility guards, then hand production planning to frozen-intent pipeline."""
    global _INSTALLED
    if _INSTALLED:
        return

    _install_crash_diagnostics()

    from app.services.actor_memory_observability_guard import (
        install as install_actor_memory_observability,
    )
    from app.services.actor_turn_authority_guard import install as install_actor_turn_authority
    from app.services.dead_turn_guard import install as install_dead_turn
    from app.services.location_profile_guard import install as install_location_profile
    from app.services.mixed_actor_response_guard import install as install_mixed_actor_response
    from app.services.narration_failure_containment_guard import (
        install as install_narration_failure_containment,
    )
    from app.services.narrator_memory_audit_guard import install as install_narrator_memory_audit
    from app.services.narrator_quality_recovery_guard import (
        install as install_narrator_quality_recovery,
    )
    from app.services.performance_telemetry_guard import install as install_performance_telemetry
    from app.services.planner_compound_guard import install as install_planner_compound
    from app.services.planner_semantic_scope_guard import install as install_planner_semantic_scope
    from app.services.player_quote_provenance_guard import (
        install as install_player_quote_provenance,
    )
    from app.services.post_turn_structured_receipt_guard import (
        install as install_post_turn_structured_receipt,
    )
    from app.services.quality_stabilization_guard import install as install_quality_stabilization
    from app.services.semantic_authority_guard import install as install_semantic_authority
    from app.services.session_zero_finalize_guard import install as install_session_zero_finalize
    from app.services.session_zero_placeholder_guard import (
        install as install_session_zero_placeholder,
    )
    from app.services.systemless_authority_guard import install as install_systemless_authority
    from app.services.truth_engine_relationship_receipt_guard import (
        install as install_truth_engine_relationship_receipts,
    )
    from app.services.turn_intent_pipeline import install as install_turn_intent_pipeline

    # Performance instrumentation wraps provider/router calls only. Install it before the semantic
    # guards so every later control/narration call is visible without changing their behavior.
    install_performance_telemetry()
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
    install_location_profile()
    install_dead_turn()
    # These wrappers remain for compatibility while older planner/unit contracts are retired. The
    # production planning entry point is replaced below, so their movement/review loops no longer own
    # normal interactive turns.
    install_semantic_authority()
    install_planner_semantic_scope()
    install_post_turn_structured_receipt()
    install_quality_stabilization()
    # The outermost memory wrapper has access to the immutable user turn as well as the final
    # proposals, so narrator echoes of player speech cannot be re-attributed to an NPC claim.
    install_player_quote_provenance()
    # Writer mode keeps the generic legacy receipt reconciler disabled. Only this narrow projection
    # maps a machine-confirmed item transfer onto an already-existing item-backed debt, with accepted
    # proposal provenance so ActiveCanonReplay and /undo remain lossless.
    install_truth_engine_relationship_receipts()

    # Strangler migration boundary: ordinary production turns now use one-way semantic phases
    # (human intent -> outcome -> deterministic compiler). No rejected executable plan is fed back
    # into the legacy Planner repair/adjudication graph.
    install_turn_intent_pipeline()
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
    from app.services.turn_intent_pipeline import TurnIntentPlanningPipeline
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
        "planning_architecture": "frozen_player_intent_v1",
        "context_pipeline": list(ContextCompiler.DEFAULT_PROVIDER_NAMES),
        "turn_pipeline": [
            "reserve_user_turn",
            "compile_planner_context",
            "interpret_player_intent",
            "review_player_intent",
            "resolve_external_outcomes",
            "compile_action_plan_from_world_state",
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
            "empty_control_outcome": "fail_never_publish_generic_no_change",
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
            "player_action_ownership": "frozen_player_intent_ir",
            "intent_fidelity": "single_narrow_review_with_one_repair_max",
            "world_outcomes": "model_after_intent_freeze",
            "movement_topology": "deterministic_graph_compiler",
            "compound_action_order": "frozen_intent_order",
            "route_discovery": "explicit_intent_plus_raw_provenance_gate",
            "addressed_response": "player_intent_ir",
            "npc_introduction_semantics": "outcome_resolver",
            "location_profile": "scoped_new_destination_enrichment",
            "relationship_receipt_resolution": "machine_receipt_undo_safe_projection",
            "empty_turn_fallback": "forbidden_fail_closed",
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
        "performance_telemetry": {
            "enabled": True,
            "format": "jsonl",
            "default_path": "parent(DATA_DIR)/llm-performance.jsonl",
            "override_env": "PDM_PERFORMANCE_LOG",
        },
        "turn_stream": identity(TurnRunner.run_turn_stream),
        "turn_saga": identity(TurnSaga.run_turn_stream),
        "turn_planning_pipeline": identity(TurnIntentPlanningPipeline.plan),
        "legacy_authority_planner": identity(TurnAuthorityPlanner.plan),
        "provider_stream": identity(LLMProvider.generate_stream),
        "narration_pipeline_impl": identity(AuthorityNarrationPipeline.generate),
        "authority_validator": identity(TurnAuthorityValidator.validate),
        "context_compiler": identity(ContextCompiler.compile_context),
        "memory_parser": identity(MemoryScribe._parse_data),
        "thesis_reconcile": identity(ThesisCurator.reconcile),
        "post_turn_mode": "background",
    }
