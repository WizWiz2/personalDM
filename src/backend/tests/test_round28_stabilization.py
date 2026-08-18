from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.models.turn_authority import PlannedNpcIntroduction
from app.services.actor_memory_observability_guard import (
    _augment_trace,
    extract_actor_segment_proposals_with_audit,
)
from app.services.systemless_authority_guard import (
    detect_contained_repetition,
    ensure_distinct_physical_location,
    normalize_addressed_conversation,
    systemless_contract_issues,
)
from app.services.turn_authority_planner import CoordinatedTurnPlan
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


def test_systemless_contract_rejects_requires_check() -> None:
    plan = _base_plan(
        action_sequence=ActionSequencePlan(
            steps=[
                ActionStepPlan(
                    action_type="interaction",
                    intent="Спросить Виктора, кто ещё мог это видеть",
                    resolution="requires_check",
                )
            ]
        )
    )

    issues = systemless_contract_issues(plan, "Кто ещё мог это видеть?")

    assert any("no check resolver" in issue for issue in issues)


def test_plain_addressed_question_normalizes_to_actor_conversation() -> None:
    plan = _base_plan(
        action_sequence=ActionSequencePlan(
            steps=[
                ActionStepPlan(
                    action_type="interaction",
                    intent="Уточнить у Виктора, было ли что-нибудь необычное",
                    resolution="requires_check",
                )
            ]
        ),
        observable_consequences=["Ответ зависит от проверки."],
    )

    normalized = normalize_addressed_conversation(
        plan,
        "Было ли что-нибудь необычное?",
    )

    assert normalized.resolution == "conversation"
    assert normalized.action_sequence.steps == []
    assert normalized.scene_transition.required is False
    assert normalized.observable_consequences == []


def test_mixed_world_action_keeps_world_step_and_extracts_dialogue_response() -> None:
    plan = _base_plan(
        action_sequence=ActionSequencePlan(
            steps=[
                ActionStepPlan(
                    action_type="observation",
                    intent="Обыскать шкаф",
                    resolution="requires_check",
                ),
                ActionStepPlan(
                    action_type="interaction",
                    intent="Спросить Виктора о шкафе",
                    resolution="requires_choice",
                ),
            ]
        )
    )

    normalized = normalize_addressed_conversation(
        plan,
        "Осматриваю шкаф. Виктор, что вы о нём знаете?",
    )

    assert len(normalized.action_sequence.steps) == 1
    assert normalized.action_sequence.steps[0].intent == "Обыскать шкаф"
    assert normalized.resolution == "sequence"


def test_player_premise_cannot_create_unsolicited_npc() -> None:
    plan = _base_plan(
        npc_introductions=[
            PlannedNpcIntroduction(
                canonical_name="Символ",
                role="странный символ на двери",
                reason="Игрок упомянул символ в своём описании.",
            )
        ]
    )

    issues = systemless_contract_issues(
        plan,
        "Осматриваю дверь, на которой, по словам свидетеля, был странный символ.",
    )

    assert any(
        "new physical NPC introductions are not authorized" in issue
        for issue in issues
    )


def test_directly_sought_unknown_contact_may_still_be_introduced() -> None:
    plan = _base_plan(
        npc_introductions=[
            PlannedNpcIntroduction(
                canonical_name="Прохожий",
                role="прохожий",
                temporary_name=True,
                reason="Игрок прямо расспрашивает прохожего.",
            )
        ]
    )

    issues = systemless_contract_issues(
        plan,
        "Расспрашиваю прохожего о старом особняке.",
    )

    assert not any(
        "new physical NPC introductions are not authorized" in issue
        for issue in issues
    )


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
