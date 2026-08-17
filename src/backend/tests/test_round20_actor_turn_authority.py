from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.models.narration_validation import NarrationValidationResult
from app.models.proposed_change import ChangeType
from app.models.turn_authority import TurnAuthority
from app.services.actor_turn_authority_guard import (
    actor_turn_contract,
    build_actor_segment_proposals,
    extract_actor_segment_proposals,
    protect_actor_turn_validation,
    segment_actor_response,
)
from app.runtime import runtime_manifest


def _authority() -> TurnAuthority:
    return TurnAuthority(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_character_id=uuid4(),
        player_character_name="Мария",
        acting_character_id=uuid4(),
        acting_character_name="Грузчик",
        player_input="Как зовут вашего брата?",
        scene_disposition="actor_turn",
        present_character_names=["Мария", "Грузчик"],
    )


def test_actor_turn_contract_is_role_scoped_not_name_specific():
    authority = _authority()
    contract = actor_turn_contract(authority)

    assert contract is not None
    assert contract["acting_character"] == "Грузчик"
    assert "speak_as_self" in contract["authorized"]
    assert "invent_player_dialogue_or_voluntary_action" in contract["not_authorized"]
    assert "move_to_another_location_without_structured_authority" in contract["not_authorized"]


def test_actor_owned_dialogue_is_not_player_agency():
    authority = _authority()
    result = NarrationValidationResult.model_validate(
        {
            "verdict": "repair_required",
            "summary": "NPC якобы действует без разрешения.",
            "violations": [
                {
                    "violation_type": "player_agency",
                    "severity": "error",
                    "evidence": "Грузчик теребит рукав и отвечает: «Его зовут Иван Сергеевич».",
                    "correction": "Удалить реплику и действие Грузчика.",
                }
            ],
        }
    )

    filtered = protect_actor_turn_validation(authority, result)

    assert filtered.verdict == "pass"
    assert filtered.violations == []


def test_actor_turn_still_protects_player_agency():
    authority = _authority()
    result = NarrationValidationResult.model_validate(
        {
            "verdict": "repair_required",
            "summary": "Мастер решил за героя.",
            "violations": [
                {
                    "violation_type": "player_agency",
                    "severity": "error",
                    "evidence": "Мария кивает и обещает найти его.",
                    "correction": "Удалить действие и обещание Марии.",
                }
            ],
        }
    )

    filtered = protect_actor_turn_validation(authority, result)

    assert filtered.verdict == "repair_required"
    assert len(filtered.violations) == 1


def test_actor_turn_does_not_excuse_unstructured_movement():
    authority = _authority()
    result = NarrationValidationResult.model_validate(
        {
            "verdict": "repair_required",
            "summary": "NPC самовольно сменил локацию.",
            "violations": [
                {
                    "violation_type": "invalid_movement",
                    "severity": "error",
                    "evidence": "Грузчик выходит из конторы и уходит на причал.",
                    "correction": "Оставить Грузчика в текущей локации.",
                }
            ],
        }
    )

    filtered = protect_actor_turn_validation(authority, result)

    assert filtered.verdict == "repair_required"
    assert filtered.violations[0].violation_type == "invalid_movement"


def test_actor_claim_provenance_is_fixed_by_typed_actor():
    actor_id = uuid4()
    player_id = uuid4()
    text = "Грузчик отвечает: «Моего брата зовут Иван Сергеевич. Он пропал вчера вечером»."
    segments = segment_actor_response(text)
    selected = [
        index
        for index, segment in enumerate(segments, start=1)
        if "брата зовут" in segment or "пропал вчера" in segment
    ]
    proposals = build_actor_segment_proposals(
        segments,
        selected,
        acting_character_id=actor_id,
        player_character_id=player_id,
    )

    assert len(proposals) >= 2
    assert all(item.change_type == ChangeType.KNOWLEDGE for item in proposals)
    assert all(item.payload["source_character_id"] == str(actor_id) for item in proposals)
    assert all(item.payload["recipient_id"] == str(player_id) for item in proposals)
    assert all(item.payload["proposition"] in text for item in proposals)


def test_actor_claim_cannot_reference_nonexistent_segment():
    proposals = build_actor_segment_proposals(
        ["Грузчик молча теребит рукав."],
        [42],
        acting_character_id=uuid4(),
        player_character_id=uuid4(),
    )

    assert proposals == []


@pytest.mark.asyncio
async def test_empty_general_scribe_can_recover_actor_claims():
    actor_id = uuid4()
    player_id = uuid4()
    text = "Грузчик отвечает: «Моего брата зовут Иван Сергеевич»."
    segments = segment_actor_response(text)
    target_id = next(
        index
        for index, segment in enumerate(segments, start=1)
        if "брата зовут" in segment
    )
    scribe = SimpleNamespace(
        _entity_repo=SimpleNamespace(
            get_character=AsyncMock(
                side_effect=[
                    SimpleNamespace(canonical_name="Грузчик"),
                    SimpleNamespace(canonical_name="Мария"),
                ]
            )
        ),
        _model_router=SimpleNamespace(
            resolve=AsyncMock(return_value=SimpleNamespace()),
            generate_json=AsyncMock(return_value={"segment_ids": [target_id]}),
        ),
        _llm_provider=SimpleNamespace(),
    )

    proposals = await extract_actor_segment_proposals(
        scribe,
        campaign_id=uuid4(),
        assistant_content=text,
        acting_character_id=actor_id,
        player_character_id=player_id,
    )

    assert len(proposals) == 1
    assert proposals[0].change_type == ChangeType.KNOWLEDGE
    assert proposals[0].payload["source_character_id"] == str(actor_id)
    assert proposals[0].payload["recipient_id"] == str(player_id)
    assert "брата зовут Иван Сергеевич" in proposals[0].payload["proposition"]


def test_runtime_manifest_reports_actor_turn_guard():
    assert "actor_turn_authority" in runtime_manifest()["guards"]
