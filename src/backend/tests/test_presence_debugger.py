from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import Entity, Scene, SceneParticipant
from app.models.campaign import CampaignCreate
from app.models.character import CharacterCreate, CharacterUpdate
from app.models.location import LocationCreate
from app.models.scene import SceneCreate
from app.services.presence_debugger import PresenceDebugger


@pytest.mark.asyncio
async def test_presence_debugger_lists_registered_npc_and_location_mismatch(
    db_session: AsyncSession,
):
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(
        campaign_id,
        CampaignCreate(name="Presence debugger"),
    )
    locations = LocationRepository(db_session)
    tavern = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Таверна"),
    )
    guild = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Гильдия"),
    )
    entities = EntityRepository(db_session)
    npc = await entities.create_character(
        campaign_id,
        CharacterCreate(
            canonical_name="Бармен Роэн",
            current_location_id=tavern.id,
            custom_fields={
                "source_turn_id": str(uuid4()),
                "first_seen_scene_id": "pending",
                "role": "бармен",
                "importance": "supporting",
            },
        ),
    )
    raw = await db_session.get(Entity, str(npc.id))
    raw.provenance = "narrator_extracted"

    scenes = SceneRepository(db_session)
    scene = await scenes.create(
        campaign_id,
        SceneCreate(title="Общий зал", location_id=tavern.id),
    )
    raw.custom_fields = raw.custom_fields.replace("pending", str(scene.id))
    await scenes.add_participant(scene.id, npc.id)

    clean = await PresenceDebugger(db_session).snapshot(campaign_id)
    assert clean["presence_state_issues"] == []
    assert clean["health"]["auto_registered_npcs"] == 1
    assert clean["auto_registered_npcs"][0]["name"] == "Бармен Роэн"
    assert clean["auto_registered_npcs"][0]["scene_ids"] == [str(scene.id)]

    # Corrupt state directly to prove the debugger catches the invariant even when a
    # legacy import or manual database edit bypasses SceneRepository.
    await entities.update_character(
        npc.id,
        CharacterUpdate(current_location_id=guild.id),
    )
    mismatch = await PresenceDebugger(db_session).snapshot(campaign_id)
    assert mismatch["health"]["presence_state_errors"] == 1
    assert "current_location_id" in mismatch["presence_state_issues"][0]


@pytest.mark.asyncio
async def test_presence_debugger_flags_duplicate_active_membership(
    db_session: AsyncSession,
):
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(
        campaign_id,
        CampaignCreate(name="Duplicate presence"),
    )
    location = await LocationRepository(db_session).create(
        campaign_id,
        LocationCreate(canonical_name="Площадь"),
    )
    npc = await EntityRepository(db_session).create_character(
        campaign_id,
        CharacterCreate(
            canonical_name="Стражник",
            current_location_id=location.id,
        ),
    )
    scenes = SceneRepository(db_session)
    first = await scenes.create(
        campaign_id,
        SceneCreate(title="Север площади", location_id=location.id),
    )
    second = await scenes.create(
        campaign_id,
        SceneCreate(title="Юг площади", location_id=location.id),
    )
    # Insert directly to represent corrupted legacy state with two simultaneously
    # active scenes; repository insertion itself correctly enforces only location.
    db_session.add(
        SceneParticipant(scene_id=str(first.id), entity_id=str(npc.id))
    )
    db_session.add(
        SceneParticipant(scene_id=str(second.id), entity_id=str(npc.id))
    )
    await db_session.flush()

    snapshot = await PresenceDebugger(db_session).snapshot(campaign_id)
    assert any("2 active scenes" in issue for issue in snapshot["presence_state_issues"])


@pytest.mark.asyncio
async def test_presence_debugger_ignores_completed_scene_membership(
    db_session: AsyncSession,
):
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(
        campaign_id,
        CampaignCreate(name="Completed leftover"),
    )
    locations = LocationRepository(db_session)
    street = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Улица"),
    )
    tavern = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Таверна"),
    )
    hero = await EntityRepository(db_session).create_character(
        campaign_id,
        CharacterCreate(
            canonical_name="Вера",
            current_location_id=tavern.id,
        ),
    )
    scenes = SceneRepository(db_session)
    old = await scenes.create(
        campaign_id,
        SceneCreate(title="Улица у трактира", location_id=street.id),
    )
    current = await scenes.create(
        campaign_id,
        SceneCreate(title="Таверна", location_id=tavern.id),
    )
    old_row = await db_session.get(Scene, str(old.id))
    old_row.status = "completed"
    current_row = await db_session.get(Scene, str(current.id))
    current_row.status = "active"
    await scenes.add_participant(old.id, hero.id, allow_movement=True)
    await scenes.add_participant(current.id, hero.id, allow_movement=True)

    snapshot = await PresenceDebugger(db_session).snapshot(campaign_id)

    assert snapshot["presence_state_issues"] == []
    assert snapshot["health"]["presence_state_errors"] == 0
