from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import Character, SceneParticipant
from app.models.campaign import CampaignCreate
from app.models.character import CharacterCreate
from app.models.location import LocationCreate
from app.models.scene import SceneCreate
from app.services.presence_service import PresenceService


@pytest.mark.asyncio
async def test_presence_service_rejects_teleport_without_structured_movement(
    db_session: AsyncSession,
):
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(
        campaign_id,
        CampaignCreate(name="Presence authority"),
    )
    locations = LocationRepository(db_session)
    scenes = SceneRepository(db_session)
    entities = EntityRepository(db_session)
    first_location = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Первая комната"),
    )
    second_location = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Вторая комната"),
    )
    character = await entities.create_character(
        campaign_id,
        CharacterCreate(
            canonical_name="Свидетель",
            current_location_id=first_location.id,
        ),
    )
    first_scene = await scenes.create(
        campaign_id,
        SceneCreate(title="Первая", location_id=first_location.id),
    )
    second_scene = await scenes.create(
        campaign_id,
        SceneCreate(title="Вторая", location_id=second_location.id),
    )
    presence = PresenceService(db_session)
    await presence.add_participant(first_scene.id, character.id)

    with pytest.raises(ValueError, match="explicit structured movement"):
        await presence.add_participant(second_scene.id, character.id)


@pytest.mark.asyncio
async def test_structured_presence_move_updates_current_location_and_preserves_history(
    db_session: AsyncSession,
):
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(
        campaign_id,
        CampaignCreate(name="Presence movement"),
    )
    locations = LocationRepository(db_session)
    scenes = SceneRepository(db_session)
    entities = EntityRepository(db_session)
    first_location = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Склад"),
    )
    second_location = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Двор"),
    )
    character = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Охранник", current_location_id=first_location.id),
    )
    first_scene = await scenes.create(
        campaign_id,
        SceneCreate(title="Склад", location_id=first_location.id),
    )
    second_scene = await scenes.create(
        campaign_id,
        SceneCreate(title="Двор", location_id=second_location.id),
    )
    presence = PresenceService(db_session)
    await presence.add_participant(first_scene.id, character.id)
    await presence.move_to_scene(second_scene.id, character.id)

    memberships = set(
        (
            await db_session.execute(
                select(SceneParticipant.scene_id).where(
                    SceneParticipant.entity_id == str(character.id)
                )
            )
        ).scalars().all()
    )
    state = await db_session.get(Character, str(character.id))

    assert memberships == {str(first_scene.id), str(second_scene.id)}
    assert state.current_location_id == str(second_location.id)
