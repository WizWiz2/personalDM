"""F-39: compound transition undo must move the playable character, not only scene participants."""
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.scene_transition_table import SceneTransition
from app.db.tables import Character
from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.location import LocationCreate
from app.models.scene import SceneCreate
from app.services.action_sequence_executor import ActionSequenceExecutor


@pytest.mark.asyncio
async def test_compound_transition_undo_restores_player_not_in_scene_participants(
    db_session: AsyncSession,
):
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    entities = EntityRepository(db_session)
    locations = LocationRepository(db_session)
    scenes = SceneRepository(db_session)
    await campaigns.create(campaign_id, CampaignCreate(name="F-39 regression"))
    origin = await locations.create(campaign_id, LocationCreate(canonical_name="Конюшня"))
    destination = await locations.create(campaign_id, LocationCreate(canonical_name="Зал"))
    hero = await entities.create_character(
        campaign_id, CharacterCreate(
            canonical_name="Тимон", current_location_id=origin.id,
        ),
    )
    await campaigns.update(campaign_id, CampaignUpdate(player_character_id=hero.id))
    source_scene = await scenes.create(
        campaign_id, SceneCreate(title="Конюшня", location_id=origin.id),
    )
    target_scene = await scenes.create(
        campaign_id, SceneCreate(title="Зал", location_id=destination.id),
    )
    # A playable bootstrap does not have to register the hero as a participant.
    assert hero.id not in await scenes.get_participants(source_scene.id)
    row = await db_session.get(Character, str(hero.id))
    row.current_location_id = str(destination.id)
    transition = SceneTransition(
        campaign_id=str(campaign_id),
        source_scene_id=str(source_scene.id),
        target_scene_id=str(target_scene.id),
        source_location_id=str(origin.id),
        target_location_id=str(destination.id),
        transition_type="location_transition",
        status="applied",
        target_reused=True,
    )
    db_session.add(transition)
    await db_session.flush()
    executor = ActionSequenceExecutor(db_session)
    with patch(
        "app.services.action_sequence_executor.release_transition_target",
        new_callable=AsyncMock,
    ), patch.object(executor._bridges, "mark_status", new_callable=AsyncMock):
        await executor._compensate_transition(transition, "undone")
    await db_session.refresh(row)
    assert row.current_location_id == str(origin.id)
    assert transition.status == "undone"
