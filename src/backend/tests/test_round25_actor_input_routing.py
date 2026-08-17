import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.models.turn import TurnCreate
from app.services.turn_authority_planner import CoordinatedTurnPlan
from app.services.turn_authority_service import TurnAuthorityService
from app.services.turn_runner import TurnRunner
from app.db.repositories.turn_repo import TurnRepository
from app.services.turn_planner import SceneTransitionPlan


def test_talk_target_is_routing_context_not_planner_bypass():
    anna_id = uuid4()
    routed = TurnRunner._route_addressed_input(
        TurnCreate(
            role="user",
            content="Тогда пойду один. Анна, вы со мной?",
            acting_character_id=anna_id,
        )
    )

    assert routed.acting_character_id is None
    assert routed.context_snapshot["input_routing"]["addressed_character_id"] == str(anna_id)
    assert routed.context_snapshot["input_routing"]["planner_bypass"] is False
    assert TurnRunner._addressed_character_id(routed) == anna_id


def test_assistant_persists_actor_from_final_turn_authority():
    anna_id = uuid4()
    data = TurnCreate(
        role="assistant",
        content="Анна отвечает.",
        context_snapshot={
            "turn_authority": {
                "acting_character_id": str(anna_id),
                "scene_disposition": "actor_turn",
            }
        },
    )

    assert TurnRepository._effective_acting_character_id(data) == anna_id


def _scene(scene_id, location_id, participant_names):
    return SimpleNamespace(
        scene_id=scene_id,
        location_id=location_id,
        location_path=["Город", str(location_id)],
        participant_names=list(participant_names),
        object_names=[],
    )


def _entity(entity_id, name):
    return SimpleNamespace(
        id=entity_id,
        canonical_name=name,
        aliases=[],
        entity_type="character",
    )


async def _service_fixture(*, target_has_anna: bool):
    campaign_id = uuid4()
    hero_id = uuid4()
    anna_id = uuid4()
    source_scene_id = uuid4()
    target_scene_id = uuid4()
    source_location = uuid4()
    target_location = source_location if target_has_anna else uuid4()

    user_row = SimpleNamespace(
        context_snapshot=json.dumps(
            {
                "input_routing": {
                    "addressed_character_id": str(anna_id),
                    "planner_bypass": False,
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
        id=hero_id,
        canonical_name="Алексей",
        current_location_id=target_location,
    )
    anna = SimpleNamespace(
        id=anna_id,
        canonical_name="Анна Левина",
        current_location_id=source_location,
    )
    by_id = {hero_id: hero, anna_id: anna}
    service._entities = SimpleNamespace(
        get_character=AsyncMock(side_effect=lambda value: by_id.get(value)),
        list_by_campaign=AsyncMock(
            return_value=[_entity(hero_id, "Алексей"), _entity(anna_id, "Анна Левина")]
        ),
    )
    source_state = _scene(
        source_scene_id,
        source_location,
        ["Алексей", "Анна Левина"],
    )
    target_state = _scene(
        target_scene_id,
        target_location,
        ["Алексей", "Анна Левина"] if target_has_anna else ["Алексей"],
    )
    service._scene_state = SimpleNamespace(
        get=AsyncMock(
            side_effect=lambda _campaign_id, scene_id: (
                source_state if scene_id == source_scene_id else target_state
            )
        )
    )
    return service, campaign_id, hero_id, anna_id, source_scene_id, target_scene_id


@pytest.mark.asyncio
async def test_addressed_present_npc_becomes_actor_after_stay_plan():
    service, campaign_id, _hero_id, anna_id, source_scene_id, target_scene_id = (
        await _service_fixture(target_has_anna=True)
    )
    plan = CoordinatedTurnPlan(
        player_intent="Спросить Анну о рукописи",
        resolution="success",
    )

    authority = await service.build(
        campaign_id=campaign_id,
        trigger_turn_id=uuid4(),
        player_input="Анна, когда вы видели рукопись?",
        source_scene_id=source_scene_id,
        target_scene_id=target_scene_id,
        plan=plan,
        acting_character_id=None,
    )

    assert authority.scene_disposition == "actor_turn"
    assert authority.acting_character_id == anna_id
    assert authority.acting_character_name == "Анна Левина"


@pytest.mark.asyncio
async def test_addressed_npc_loses_actor_authority_after_player_moves_away():
    service, campaign_id, _hero_id, _anna_id, source_scene_id, target_scene_id = (
        await _service_fixture(target_has_anna=False)
    )
    plan = CoordinatedTurnPlan(
        player_intent="Выйти из кабинета и пойти в библиотеку",
        resolution="success",
        scene_transition=SceneTransitionPlan(
            required=True,
            transition_type="location_transition",
            destination_location="Городская библиотека",
            reason="Игрок явно выходит из кабинета и идёт в библиотеку.",
        ),
    )

    authority = await service.build(
        campaign_id=campaign_id,
        trigger_turn_id=uuid4(),
        player_input="Тогда пойду один. Выхожу из кабинета и иду в библиотеку.",
        source_scene_id=source_scene_id,
        target_scene_id=target_scene_id,
        plan=plan,
        acting_character_id=None,
    )

    assert authority.scene_disposition == "location_transition"
    assert authority.acting_character_id is None
    assert authority.acting_character_name is None
