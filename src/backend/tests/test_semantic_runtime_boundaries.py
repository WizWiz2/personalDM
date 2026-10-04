from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services.actor_memory_observability_guard import extract_actor_segment_proposals_with_audit
from app.services.entity_identity import resolve_character_candidates
from app.models.session_zero_interview import SessionZeroStarterNPC


@pytest.mark.asyncio
async def test_silence_word_cannot_hide_a_factual_claim():
    actor_id, player_id = uuid4(), uuid4()
    router = SimpleNamespace(
        resolve=AsyncMock(return_value=SimpleNamespace()),
        generate_json=AsyncMock(return_value={"segment_ids": [1]}),
    )
    scribe = SimpleNamespace(
        _entity_repo=SimpleNamespace(list_by_campaign=AsyncMock(return_value=[]), get_character=AsyncMock(side_effect=[
            SimpleNamespace(canonical_name="Марта"), SimpleNamespace(canonical_name="Игрок"),
        ])),
        _model_router=router, _llm_provider=SimpleNamespace(), last_audit={},
    )
    proposals = await extract_actor_segment_proposals_with_audit(
        scribe, campaign_id=uuid4(), assistant_content="— Молчу, потому что дверь закрыта.",
        acting_character_id=actor_id, player_character_id=player_id,
    )
    router.generate_json.assert_awaited_once()
    assert len(proposals) == 1
    assert proposals[0].payload["source_character_id"] == str(actor_id)


def test_role_synonyms_do_not_merge_distinct_people():
    location_id, entity_id = uuid4(), uuid4()
    existing = SimpleNamespace(
        id=entity_id, canonical_name="Хозяин", aliases=[],
        description="Хозяин и трактирщик", custom_fields={},
    )
    assert resolve_character_candidates(
        [existing], proposed_name="Трактирщик", proposed_role="трактирщик",
        temporary_name=True, target_location_id=location_id,
        character_locations={entity_id: location_id},
    ) == []


@pytest.mark.parametrize("description", [
    "Ирина — фотограф.", "Его зовут Харун.", "A pilot named Xanthe.", "担当者は葵です。",
])
def test_description_does_not_override_typed_identity(description):
    npc = SessionZeroStarterNPC(role="собеседник", description=description)
    assert npc.name is None
    explicit = SessionZeroStarterNPC(role="собеседник", name="Xanthe", name_kind="personal", description=description)
    assert explicit.name == "Xanthe"


@pytest.mark.asyncio
@pytest.mark.parametrize("situation", [
    "Хозяин трактира предлагает работу, выход открыт.",
    "Бармен за стойкой, герой заперт.",
    "The innkeeper offers a contract in an open tavern.",
])
async def test_untyped_opening_prose_cannot_create_people_or_routes(db_session, situation):
    from app.db.repositories.entity_repo import EntityRepository
    from app.db.repositories.location_repo import LocationRepository
    from app.db.repositories.scene_repo import SceneRepository
    from app.models.campaign import CampaignCreate
    from app.models.character import CharacterCreate
    from app.models.location import LocationCreate
    from app.models.scene import SceneCreate
    from app.services.campaign_service import CampaignService
    from app.services.playable_bootstrap import PlayableBootstrapService
    from app.services.scene_lifecycle import SceneLifecycleService

    campaign = await CampaignService(db_session).create_campaign(CampaignCreate(name="Untyped start"))
    location = await LocationRepository(db_session).create(
        campaign.id, LocationCreate(canonical_name="Таверна"),
    )
    player = await EntityRepository(db_session).create_character(
        campaign.id, CharacterCreate(canonical_name="Игрок", current_location_id=location.id),
    )
    scenes = SceneRepository(db_session)
    scene = await scenes.create(campaign.id, SceneCreate(title="Старт", location_id=location.id))
    await scenes.add_participant(scene.id, player.id)
    await SceneLifecycleService(db_session).activate(campaign.id, scene.id)
    result = await PlayableBootstrapService(db_session).ensure(
        campaign.id, scene.id, player_character_id=player.id,
        starting_location_id=location.id, starting_situation=situation,
    )
    assert result.state.participant_ids == [player.id]
    assert result.state.available_exits == []
    assert result.created_npc_id is None
    assert result.created_exit_location_id is None
    assert result.state.object_ids
