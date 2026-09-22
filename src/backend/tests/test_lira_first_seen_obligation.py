"""Live residual after #189: placed kitchen + unplaced Лира first_seen → stamp + fail-close."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.models.narration_validation import NarrationValidationResult
from app.models.turn_authority import TurnAuthority
from app.services.narration_publication_guard import NarrationPublicationGuard
from app.services.narrator_authority_contracts import (
    addressed_response_beat_present,
    addressed_response_erasure_spans,
)
from app.services.turn_authority_planner import CoordinatedTurnPlan
from app.services.turn_authority_service import (
    TurnAuthorityService,
    _address_repair_colocated,
)
from app.services.turn_authority_validator import TurnAuthorityValidator


def _authority(**updates) -> TurnAuthority:
    base = dict(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_character_name="Эйдан",
        player_input="Лира, где здесь хлеб?",
        scene_disposition="stay",
        present_character_names=["Эйдан", "Лира"],
        resolution="conversation",
        observable_consequences=[],
    )
    base.update(updates)
    return TurnAuthority(**base)


def _pass() -> NarrationValidationResult:
    return NarrationValidationResult(verdict="pass", summary="ok", violations=[])


def _scene(scene_id, location_id, participant_names):
    return SimpleNamespace(
        scene_id=scene_id,
        location_id=location_id,
        location_path=["Город", str(location_id or "null")],
        participant_names=list(participant_names),
        object_names=[],
    )


def _entity(entity_id, name, custom_fields=None):
    return SimpleNamespace(
        id=entity_id,
        canonical_name=name,
        aliases=[],
        entity_type="character",
        custom_fields=custom_fields,
    )


def test_colocated_same_scene_unplaced_allows_placed_kitchen():
    kitchen = uuid4()
    assert not _address_repair_colocated(
        scene_location_id=kitchen,
        character_location_id=None,
        player_location_id=kitchen,
    )
    assert _address_repair_colocated(
        scene_location_id=kitchen,
        character_location_id=None,
        player_location_id=kitchen,
        same_scene_unplaced=True,
    )
    assert _address_repair_colocated(
        scene_location_id=None,
        character_location_id=None,
        player_location_id=kitchen,
    )


@pytest.mark.asyncio
async def test_placed_scene_unplaced_lira_first_seen_stamps_obligation(monkeypatch):
    async def _no_lines(*_args, **_kwargs):
        return []

    async def _no_subjects(*_args, **_kwargs):
        return []

    monkeypatch.setattr(
        "app.services.turn_authority_service.established_state_lines",
        _no_lines,
    )
    monkeypatch.setattr(
        "app.services.turn_authority_service.established_subjects",
        _no_subjects,
    )
    campaign_id = uuid4()
    hero_id = uuid4()
    lira_id = uuid4()
    steward_id = uuid4()
    scene_id = uuid4()
    location_id = uuid4()

    user_row = SimpleNamespace(
        context_snapshot=json.dumps(
            {
                "input_routing": {
                    "addressed_character_id": str(steward_id),
                    "planner_bypass": False,
                    "user_actor": "player_character",
                }
            }
        )
    )
    session = SimpleNamespace(get=AsyncMock(return_value=user_row))
    service = TurnAuthorityService.__new__(TurnAuthorityService)
    service._session = session
    service._campaigns = SimpleNamespace(
        get_by_id=AsyncMock(return_value=SimpleNamespace(player_character_id=hero_id))
    )
    hero = SimpleNamespace(
        id=hero_id, canonical_name="Эйдан", current_location_id=location_id
    )
    lira = SimpleNamespace(
        id=lira_id,
        canonical_name="Лира",
        current_location_id=None,
        custom_fields={"first_seen_scene_id": str(scene_id)},
    )
    steward = SimpleNamespace(
        id=steward_id,
        canonical_name="Управляющая домом",
        current_location_id=location_id,
        custom_fields={},
    )
    by_id = {hero_id: hero, lira_id: lira, steward_id: steward}
    service._entities = SimpleNamespace(
        get_character=AsyncMock(side_effect=lambda value: by_id.get(value)),
        list_by_campaign=AsyncMock(
            return_value=[
                _entity(hero_id, "Эйдан"),
                _entity(
                    lira_id,
                    "Лира",
                    custom_fields={"first_seen_scene_id": str(scene_id)},
                ),
                _entity(steward_id, "Управляющая домом"),
            ]
        ),
    )
    state = _scene(scene_id, location_id, ["Эйдан", "Управляющая домом"])
    service._scene_state = SimpleNamespace(get=AsyncMock(return_value=state))

    plan = CoordinatedTurnPlan(
        player_intent="Спросить Лиру",
        resolution="conversation",
        addressed_response_requested=True,
    )
    player_input = "Лира, где здесь хлеб? Ответь коротко именно ты, Лира."
    authority = await service.build(
        campaign_id=campaign_id,
        trigger_turn_id=uuid4(),
        player_input=player_input,
        source_scene_id=scene_id,
        target_scene_id=scene_id,
        plan=plan,
        acting_character_id=None,
    )
    assert "Лира" in authority.present_character_names
    assert "Лира" not in authority.known_absent_character_names
    assert authority.addressed_response_obligation == "Лира"
    assert authority.acting_character_id == lira_id
    assert authority.acting_character_name == "Лира"
    payload = authority.narrator_payload()
    assert "Лира" in payload["present_characters"]
    assert "Лира" not in payload["known_absent_characters"]


@pytest.mark.asyncio
async def test_null_scene_sticky_upravlyayushchaya_addresses_lira_stamps(monkeypatch):
    async def _no_lines(*_args, **_kwargs):
        return []

    async def _no_subjects(*_args, **_kwargs):
        return []

    monkeypatch.setattr(
        "app.services.turn_authority_service.established_state_lines",
        _no_lines,
    )
    monkeypatch.setattr(
        "app.services.turn_authority_service.established_subjects",
        _no_subjects,
    )
    campaign_id = uuid4()
    hero_id = uuid4()
    lira_id = uuid4()
    steward_id = uuid4()
    scene_id = uuid4()
    location_id = uuid4()

    user_row = SimpleNamespace(
        context_snapshot=json.dumps(
            {
                "input_routing": {
                    "addressed_character_id": str(steward_id),
                    "planner_bypass": False,
                    "user_actor": "player_character",
                }
            }
        )
    )
    session = SimpleNamespace(get=AsyncMock(return_value=user_row))
    service = TurnAuthorityService.__new__(TurnAuthorityService)
    service._session = session
    service._campaigns = SimpleNamespace(
        get_by_id=AsyncMock(return_value=SimpleNamespace(player_character_id=hero_id))
    )
    hero = SimpleNamespace(
        id=hero_id, canonical_name="Эйдан", current_location_id=location_id
    )
    lira = SimpleNamespace(
        id=lira_id,
        canonical_name="Лира",
        current_location_id=None,
        custom_fields={"first_seen_scene_id": str(scene_id)},
    )
    steward = SimpleNamespace(
        id=steward_id,
        canonical_name="Управляющая домом",
        current_location_id=location_id,
        custom_fields={},
    )
    by_id = {hero_id: hero, lira_id: lira, steward_id: steward}
    service._entities = SimpleNamespace(
        get_character=AsyncMock(side_effect=lambda value: by_id.get(value)),
        list_by_campaign=AsyncMock(
            return_value=[
                _entity(hero_id, "Эйдан"),
                _entity(
                    lira_id,
                    "Лира",
                    custom_fields={"first_seen_scene_id": str(scene_id)},
                ),
                _entity(steward_id, "Управляющая домом"),
            ]
        ),
    )
    state = _scene(scene_id, None, ["Эйдан", "Управляющая домом"])
    service._scene_state = SimpleNamespace(get=AsyncMock(return_value=state))

    plan = CoordinatedTurnPlan(
        player_intent="Спросить Лиру",
        resolution="conversation",
        addressed_response_requested=True,
    )
    authority = await service.build(
        campaign_id=campaign_id,
        trigger_turn_id=uuid4(),
        player_input="Лира, где здесь хлеб? Ответь коротко именно ты, Лира.",
        source_scene_id=scene_id,
        target_scene_id=scene_id,
        plan=plan,
        acting_character_id=None,
    )
    assert authority.addressed_response_obligation == "Лира"
    assert authority.acting_character_name == "Лира"
    assert "Лира" in authority.present_character_names


def test_obligation_rejects_absence_claim_with_rival_hiring_speech():
    authority = _authority(
        player_character_name="Эйдан",
        player_input="Лира, где здесь хлеб? Ответь коротко именно ты, Лира.",
        present_character_names=["Эйдан", "Лира", "Управляющая домом"],
        addressed_response_obligation="Лира",
    )
    candidate = (
        "Лира сейчас не находится на кухне. Управляющая домом подходит ближе и спокойно отвечает: "
        "«Наймом распоряжаюсь я. Хлеб лежит на полке у очага.»"
    )
    assert (
        addressed_response_beat_present(
            candidate, "Лира", rival_names=["Управляющая домом"]
        )
        is False
    )
    spans = addressed_response_erasure_spans(candidate, authority)
    assert any("no_response_beat" in span for span in spans)
    result = TurnAuthorityValidator.apply_deterministic_speaker_authority(
        _pass(), authority, candidate
    )
    assert result.verdict == "repair_required"
    surgical, audit = NarrationPublicationGuard.surgical_repair_candidate(
        candidate, result
    )
    assert surgical is None
    assert audit["reason"] == "addressed_response_obligation_not_surgically_repairable"
