from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.services.actor_memory_observability_guard import (
    _augment_trace,
    extract_actor_segment_proposals_with_audit,
)
from app.services.systemless_authority_guard import (
    detect_contained_repetition,
    ensure_distinct_physical_location,
    normalize_addressed_conversation,
)
from app.services.turn_authority_planner import CoordinatedTurnPlan, TurnAuthorityPlanner
from app.services.turn_planner import ActionSequencePlan, ActionStepPlan


def _base_plan(**updates) -> CoordinatedTurnPlan:
    payload = {
        "player_intent": "Поговорить с собеседником",
        "resolution": "conversation",
        "observable_consequences": [],
        "character_beats": [],
        "canon_constraints": [],
        "new_fact_candidates": [],
        "narration_guidance": [],
        "ending_hook": "",
    }
    payload.update(updates)
    return CoordinatedTurnPlan.model_validate(payload)


def test_systemless_schema_rejects_requires_check_before_execution() -> None:
    with pytest.raises(ValidationError):
        ActionStepPlan(
            action_type="interaction",
            intent="Спросить Виктора, кто ещё мог это видеть",
            resolution="requires_check",
        )


def test_plain_addressed_question_is_typed_as_response_ownership_not_action_step() -> None:
    plan = _base_plan(
        addressed_response_requested=True,
        response_ownership_reason="Игрок задаёт вопрос выбранному присутствующему NPC.",
    )

    normalized = normalize_addressed_conversation(
        plan,
        "Было ли что-нибудь необычное?",
    )

    assert normalized is plan
    assert normalized.resolution == "conversation"
    assert normalized.action_sequence.steps == []
    assert normalized.addressed_response_requested is True


def test_mixed_world_action_keeps_only_world_step_and_typed_response_request() -> None:
    plan = _base_plan(
        resolution="sequence",
        addressed_response_requested=True,
        response_ownership_reason="После осмотра игрок задаёт вопрос Виктору.",
        action_sequence=ActionSequencePlan(
            steps=[
                ActionStepPlan(
                    action_type="observation",
                    intent="Обыскать шкаф",
                    resolution="auto_success",
                    safe_mundane=True,
                    observable_outcome="Шкаф осмотрен.",
                )
            ]
        ),
    )

    assert len(plan.action_sequence.steps) == 1
    assert plan.action_sequence.steps[0].intent == "Обыскать шкаф"
    assert plan.addressed_response_requested is True


def test_person_vs_object_and_unsolicited_contact_meaning_belongs_to_semantic_reviewer() -> None:
    prompt = TurnAuthorityPlanner.SEMANTIC_REVIEW_PROMPT

    assert "CONTACT/IDENTITY" in prompt
    assert "previously" in prompt
    assert "unknown physical responder" in prompt
    assert "npc_introductions" in prompt
    assert "Do not use keyword lists" in prompt


def test_location_transition_cannot_resolve_to_current_physical_location() -> None:
    location_id = uuid4()
    resolved = SimpleNamespace(id=location_id)

    with pytest.raises(ValueError, match="current physical location"):
        ensure_distinct_physical_location(location_id, resolved)


def test_repetition_guard_detects_old_reply_embedded_in_larger_answer() -> None:
    previous = (
        "Я был у старого особняка около полуночи и видел у боковой двери серый автомобиль, "
        "который стоял там примерно десять минут."
    )
    candidate = (
        previous
        + " Потом я ушёл к остановке. Ещё я вспомнил, что возле ворот лежала мокрая газета."
    )

    match = detect_contained_repetition(candidate, [previous])

    assert match is not None
    assert match.previous_text == previous
    assert match.similarity == 1.0
    assert match.exact is False


@pytest.mark.asyncio
async def test_actor_selector_retries_empty_selection_without_rewriting_evidence() -> None:
    actor_id = uuid4()
    player_id = uuid4()
    scribe = SimpleNamespace(
        _entity_repo=SimpleNamespace(
            get_character=AsyncMock(
                side_effect=[
                    SimpleNamespace(canonical_name="Виктор"),
                    SimpleNamespace(canonical_name="Алекс"),
                ]
            )
        ),
        _model_router=SimpleNamespace(
            resolve=AsyncMock(return_value=SimpleNamespace()),
            generate_json=AsyncMock(
                side_effect=[
                    {"segment_ids": []},
                    {"segment_ids": [1]},
                ]
            ),
        ),
        _llm_provider=object(),
        last_audit={},
    )
    published = "Я видел красную машину возле старого дома около полуночи."

    proposals = await extract_actor_segment_proposals_with_audit(
        scribe,
        campaign_id=uuid4(),
        assistant_content=published,
        acting_character_id=actor_id,
        player_character_id=player_id,
    )

    assert len(proposals) == 1
    assert scribe._model_router.generate_json.await_count == 2
    assert proposals[0].payload["_canon"]["evidence"] == published
    assert proposals[0].payload["_canon"]["segment_id"] == 1
    assert scribe.last_audit["selector_attempts"] == 2
    assert scribe.last_audit["selector_status"] == "selected"
    assert scribe.last_audit["selected_segment_ids"] == [1]


def test_flight_recorder_surfaces_persisted_actor_selector_audit() -> None:
    assistant_id = str(uuid4())
    audit = {
        "selector_status": "empty_selection",
        "selector_attempts": 2,
        "candidate_segments": [{"segment_id": 1, "text": "Я ничего не видел."}],
        "selected_segment_ids": [],
        "selector_error": None,
    }
    snapshot = {
        "turns": [
            {
                "id": assistant_id,
                "actor_id": str(uuid4()),
                "content": "Я ничего не видел.",
                "context_snapshot": {"actor_memory_debug": audit},
            }
        ],
        "proposals": [],
    }
    trace = {"memory": {}, "diagnostics": []}

    augmented = _augment_trace(snapshot, assistant_id, trace)

    assert augmented["memory"]["actor_selector"] == audit
