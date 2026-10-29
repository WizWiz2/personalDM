"""Scene activation: one active scene, own theses kept, no NPC teleport, abandoned scenes refused."""
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import Campaign, Character, Scene, SceneParticipant, SceneThesis
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.location import LocationCreate
from app.models.scene import SceneCreate
from app.services.scene_lifecycle import SceneLifecycleService


async def _world(session):
    cid = uuid4()
    campaigns = CampaignRepository(session)
    await campaigns.create(cid, CampaignCreate(name="activation"))
    locations = LocationRepository(session)
    hall = await locations.create(cid, LocationCreate(canonical_name="Зал"))
    pier = await locations.create(cid, LocationCreate(canonical_name="Пристань"))
    entities = EntityRepository(session)
    hero = await entities.create_character(cid, CharacterCreate(canonical_name="Ирма", current_location_id=hall.id))
    npc = await entities.create_character(cid, CharacterCreate(canonical_name="Йонас", current_location_id=hall.id))
    await campaigns.update(cid, CampaignUpdate(player_character_id=hero.id))
    scenes = SceneRepository(session)
    a = await scenes.create(cid, SceneCreate(title="Зал", location_id=hall.id))
    b = await scenes.create(cid, SceneCreate(title="Пристань", location_id=pier.id))
    for scene in (a, b):
        session.add(SceneThesis(scene_id=str(scene.id), thesis_type="goal", text="t"))
    await session.flush()
    return cid, a.id, b.id, hero.id, npc.id, pier.id


async def _thesis_status(session, scene_id):
    rows = (await session.execute(
        select(SceneThesis.status).where(SceneThesis.scene_id == str(scene_id)).order_by(SceneThesis.text.desc())
    )).scalars().all()
    return rows[0]


@pytest.mark.asyncio
async def test_activation_closes_other_scene_theses_and_keeps_its_own(db_session):
    cid, a, b, hero, _, pier = await _world(db_session)
    life = SceneLifecycleService(db_session)
    first = await life.activate(cid, a)
    assert first.changed is True and await _thesis_status(db_session, a) == "active"
    assert await _thesis_status(db_session, b) == "resolved"  # b was active on creation
    db_session.add(SceneThesis(scene_id=str(b), thesis_type="goal", text="t2"))
    await db_session.flush()

    second = await life.activate(cid, b)
    assert second.previous_scene_id == a and second.changed is True
    assert await _thesis_status(db_session, a) == "resolved"
    assert await _thesis_status(db_session, b) == "active"
    assert (await db_session.get(Scene, str(a))).status == "completed"
    assert (await db_session.get(Campaign, str(cid))).current_scene_id == str(b)
    assert (await db_session.get(Character, str(hero))).current_location_id == str(pier)


@pytest.mark.asyncio
async def test_reactivating_the_active_scene_is_not_a_change(db_session):
    cid, a, *_ = await _world(db_session)
    life = SceneLifecycleService(db_session)
    await life.activate(cid, a)
    again = await life.activate(cid, a)
    assert again.changed is False and again.previous_scene_id == a
    assert await _thesis_status(db_session, a) == "active"


@pytest.mark.asyncio
async def test_abandoned_scene_cannot_be_activated(db_session):
    cid, a, b, *_ = await _world(db_session)
    life = SceneLifecycleService(db_session)
    await life.activate(cid, a)
    (await db_session.get(Scene, str(b))).status = "abandoned"
    with pytest.raises(ValueError):
        await life.activate(cid, b)
    assert (await db_session.get(Campaign, str(cid))).current_scene_id == str(a)


@pytest.mark.asyncio
async def test_scene_from_another_campaign_is_refused(db_session):
    cid, a, *_ = await _world(db_session)
    other, *_ = await _world(db_session)
    with pytest.raises(ValueError):
        await SceneLifecycleService(db_session).activate(other, a)


@pytest.mark.asyncio
async def test_activation_never_teleports_a_declared_npc_participant(db_session):
    cid, a, b, _, npc, _ = await _world(db_session)
    db_session.add(SceneParticipant(scene_id=str(b), entity_id=str(npc)))
    await db_session.flush()
    with pytest.raises(ValueError):
        await SceneLifecycleService(db_session).activate(cid, b)
    assert (await db_session.get(Character, str(npc))).current_location_id != str(UUID(str(b)))
