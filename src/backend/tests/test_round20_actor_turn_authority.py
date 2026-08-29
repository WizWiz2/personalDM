from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.models.proposed_change import ChangeType
from app.models.turn_authority import TurnAuthority
from app.services.actor_turn_authority_guard import (
    actor_turn_contract,
    build_actor_segment_proposals,
    extract_actor_segment_proposals,
    segment_actor_response,
)
from app.services.turn_authority_validator import TurnAuthorityValidator
from app.runtime import runtime_manifest


def _authority(
    *,
    player_name: str = "Мария",
    actor_name: str = "Грузчик",
    player_input: str = "Как зовут вашего брата?",
) -> TurnAuthority:
    return TurnAuthority(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_character_id=uuid4(),
        player_character_name=player_name,
        acting_character_id=uuid4(),
        acting_character_name=actor_name,
        player_input=player_input,
        scene_disposition="actor_turn",
        present_character_names=[player_name, actor_name],
    )


def test_actor_turn_contract_is_role_scoped_not_name_specific():
    authority = _authority()
    contract = actor_turn_contract(authority)

    assert contract is not None
    assert contract["acting_character"] == "Грузчик"
    assert "speak_as_self" in contract["authorized"]
    assert "state_personal_memories_observations_and_claims" in contract["authorized"]
    assert "mention_absent_people_places_objects_or_past_events_as_claims" in contract["authorized"]
    assert "invent_player_dialogue_or_voluntary_action" in contract["not_authorized"]
    assert "move_to_another_location_without_structured_authority" in contract["not_authorized"]
    assert "character_claim" in contract["epistemic_rule"]


def test_actor_rights_are_semantic_validator_contract_not_post_filter():
    prompt = TurnAuthorityValidator.SYSTEM_PROMPT

    assert "NPC OWNERSHIP" in prompt
    assert "PRESENT NPC DIALOGUE" in prompt
    assert "SPEAKER CONSISTENCY" in prompt
    assert "Never decide from" in prompt


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


def test_nested_actor_claim_evidence_is_deduplicated_without_semantic_guessing():
    actor_id = uuid4()
    player_id = uuid4()
    text = "Грузчик кивает. «Это мой груз», — говорит он низким голосом."
    segments = segment_actor_response(text)
    selected = [
        index
        for index, segment in enumerate(segments, start=1)
        if "Это мой груз" in segment
    ]

    proposals = build_actor_segment_proposals(
        segments,
        selected,
        acting_character_id=actor_id,
        player_character_id=player_id,
    )

    assert len(proposals) == 1
    assert proposals[0].payload["proposition"] == "Это мой груз"


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
