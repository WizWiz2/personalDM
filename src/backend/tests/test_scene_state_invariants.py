from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.location import LocationCreate
from app.models.scene import SceneCreate
from app.models.scene_state import LocationExitCreate, SceneStateUpdate
from app.services.context_compiler import ContextCompiler
from app.services.scene_lifecycle import SceneLifecycleService
from app.services.scene_state_service import SceneStateService
from app.services.scene_transition_executor import SceneTransitionExecutor
from app.services.turn_planner import SceneTransitionPlan


def _campaign(client: TestClient) -> str:
    response = client.post("/api/campaigns", json={"name": "Scene State"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _location(
    client: TestClient,
    campaign_id: str,
    name: str,
    parent_id: str | None = None,
) -> dict:
    payload = {"canonical_name": name}
    if parent_id:
        payload["parent_location_id"] = parent_id
    response = client.post(f"/api/campaigns/{campaign_id}/locations", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _character(
    client: TestClient,
    campaign_id: str,
    name: str,
    location_id: str | None = None,
) -> dict:
    payload = {"canonical_name": name}
    if location_id:
        payload["current_location_id"] = location_id
    response = client.post(f"/api/campaigns/{campaign_id}/characters", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_scene_state_api_exposes_time_presence_objects_and_exits(client: TestClient):
    campaign_id = _campaign(client)
    city = _location(client, campaign_id, "Лантерн")
    tavern = _location(client, campaign_id, "Медный Котёл", city["id"])
    hall = _location(client, campaign_id, "Общий зал", tavern["id"])
    room = _location(client, campaign_id, "Комната №3", tavern["id"])
    hero = _character(client, campaign_id, "Эйдан", hall["id"])
    bartender = _character(client, campaign_id, "Бармен", hall["id"])
    client.put(
        f"/api/campaigns/{campaign_id}",
        json={"player_character_id": hero["id"]},
    )
    scene = client.post(
        f"/api/campaigns/{campaign_id}/scenes",
        json={"title": "Вечер в таверне", "location_id": hall["id"]},
    ).json()
    added = client.post(
        f"/api/scenes/{scene['id']}/participants",
        params={"entity_id": bartender["id"]},
    )
    assert added.status_code == 200, added.text

    exit_response = client.post(
        f"/api/campaigns/{campaign_id}/locations/{hall['id']}/exits",
        json={
            "to_location_id": room["id"],
            "label": "Лестница к комнатам",
            "travel_time": "2 минуты",
            "bidirectional": True,
            "reverse_label": "Лестница в общий зал",
        },
    )
    assert exit_response.status_code == 201, exit_response.text
    state_update = client.put(
        f"/api/campaigns/{campaign_id}/scenes/{scene['id']}/state",
        json={
            "world_time_label": "поздний вечер",
            "world_time_order": 12,
            "scene_goal": "получить работу",
            "active_conflict": "нет",
        },
    )
    assert state_update.status_code == 200, state_update.text

    state = state_update.json()
    assert state["location_path"] == ["Лантерн", "Медный Котёл", "Общий зал"]
    assert state["world_time_label"] == "поздний вечер"
    assert state["world_time_order"] == 12
    assert set(state["participant_names"]) == {"Эйдан", "Бармен"}
    assert state["available_exits"][0]["to_location_id"] == room["id"]
    assert state["invariant_errors"] == []


@pytest.mark.asyncio
async def test_context_contract_excludes_character_in_another_location(
    db_session: AsyncSession,
):
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    locations = LocationRepository(db_session)
    entities = EntityRepository(db_session)
    scenes = SceneRepository(db_session)

    await campaigns.create(campaign_id, CampaignCreate(name="Prompt contract"))
    hall = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Общий зал"),
    )
    room = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Комната"),
    )
    hero = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Эйдан", current_location_id=hall.id),
    )
    bartender = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Бармен", current_location_id=hall.id),
    )
    stalker = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Человек в плаще", current_location_id=room.id),
    )
    await campaigns.update(
        campaign_id,
        CampaignUpdate(player_character_id=hero.id),
    )
    scene = await scenes.create(
        campaign_id,
        SceneCreate(title="Общий зал", location_id=hall.id),
    )
    await scenes.add_participant(scene.id, bartender.id)
    await SceneLifecycleService(db_session).activate(campaign_id, scene.id)
    await SceneStateService(db_session).create_exit(
        campaign_id,
        hall.id,
        LocationExitCreate(
            to_location_id=room.id,
            label="Дверь в комнату",
        ),
    )
    await db_session.commit()

    messages, metadata = await ContextCompiler(db_session).compile_context(
        campaign_id=campaign_id,
        scene_id=scene.id,
        current_user_content="Я осматриваюсь.",
    )
    system = messages[0].content
    assert "[AUTHORITATIVE SCENE STATE]" in system
    assert "Physically present characters: Бармен, Эйдан" in system
    assert "Дверь в комнату -> Комната" in system
    assert "Человек в плаще" not in system
    assert metadata["scene_state"]["location_id"] == str(hall.id)
    assert str(stalker.id) not in metadata["scene_state"]["participant_ids"]


@pytest.mark.asyncio
async def test_transition_rejects_existing_location_not_in_exit_map(
    db_session: AsyncSession,
):
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    locations = LocationRepository(db_session)
    entities = EntityRepository(db_session)
    scenes = SceneRepository(db_session)
    state = SceneStateService(db_session)

    await campaigns.create(campaign_id, CampaignCreate(name="Exit enforcement"))
    hall = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Общий зал"),
    )
    room = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Комната"),
    )
    cellar = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Подвал"),
    )
    hero = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Эйдан", current_location_id=hall.id),
    )
    await campaigns.update(
        campaign_id,
        CampaignUpdate(player_character_id=hero.id),
    )
    source = await scenes.create(
        campaign_id,
        SceneCreate(title="Общий зал", location_id=hall.id),
    )
    await SceneLifecycleService(db_session).activate(campaign_id, source.id)
    await state.create_exit(
        campaign_id,
        hall.id,
        LocationExitCreate(to_location_id=room.id, label="Лестница наверх"),
    )

    with pytest.raises(ValueError, match="not an available exit"):
        await SceneTransitionExecutor(db_session).apply(
            campaign_id,
            source.id,
            None,
            SceneTransitionPlan(
                required=True,
                transition_type="location_transition",
                destination_location=cellar.canonical_name,
                reason="Игрок пытается пройти в подвал без известного пути.",
            ),
        )


@pytest.mark.asyncio
async def test_time_transition_inherits_and_advances_world_time(
    db_session: AsyncSession,
):
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    locations = LocationRepository(db_session)
    entities = EntityRepository(db_session)
    scenes = SceneRepository(db_session)
    state = SceneStateService(db_session)

    await campaigns.create(campaign_id, CampaignCreate(name="Time state"))
    room = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Комната"),
    )
    hero = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Эйдан", current_location_id=room.id),
    )
    await campaigns.update(
        campaign_id,
        CampaignUpdate(player_character_id=hero.id),
    )
    source = await scenes.create(
        campaign_id,
        SceneCreate(title="Ночь", location_id=room.id),
    )
    await SceneLifecycleService(db_session).activate(campaign_id, source.id)
    await state.update(
        campaign_id,
        source.id,
        SceneStateUpdate(world_time_label="ночь", world_time_order=7),
    )

    transition = await SceneTransitionExecutor(db_session).apply(
        campaign_id,
        source.id,
        None,
        SceneTransitionPlan(
            required=True,
            transition_type="time_transition",
            elapsed_time="8 часов",
            time_after="утро",
            scene_title="Утро в комнате",
            reason="Игрок спит до утра.",
        ),
    )
    assert transition is not None
    target = await state.get(campaign_id, transition.target_scene_id)
    assert target.world_time_label == "утро"
    assert target.world_time_order == 8
    assert target.location_id == room.id
    assert target.participant_ids == [hero.id]
