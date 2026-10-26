"""sim300 N2: theses lived on in completed scenes and came back on every revisit."""
from uuid import uuid4

import pytest

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import Turn
from app.db.thesis_lifecycle_table import ThesisLifecycleProfile
from app.models.campaign import CampaignCreate
from app.models.scene import SceneCreate
from app.models.scene_thesis import SceneThesisCreate, ThesisType
from app.services.memory_operations import MemoryOperationsService
from app.services.scene_lifecycle import SceneLifecycleService


@pytest.mark.asyncio
async def test_leaving_a_scene_closes_its_theses(db_session):
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(campaign_id, CampaignCreate(name="Маяк"))
    scenes = SceneRepository(db_session)
    station = await scenes.create(campaign_id, SceneCreate(title="Станция"))
    lighthouse = await scenes.create(campaign_id, SceneCreate(title="Маяк"))
    lifecycle = SceneLifecycleService(db_session)
    await lifecycle.activate(campaign_id, station.id)
    for pinned in (False, True):
        await scenes.create_thesis(
            station.id, SceneThesisCreate(thesis_type=ThesisType.TENSION, text="все трое в зале", pinned=pinned)
        )

    await lifecycle.activate(campaign_id, lighthouse.id)
    assert await scenes.list_theses_by_scene(station.id, active_only=True) == []

    await lifecycle.activate(campaign_id, station.id)  # return: no stale working memory
    assert await scenes.list_theses_by_scene(station.id, active_only=True) == []


@pytest.mark.asyncio
async def test_unreinforced_thesis_expires_by_turn_count(db_session):
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(campaign_id, CampaignCreate(name="TTL"))
    scenes = SceneRepository(db_session)
    scene = await scenes.create(campaign_id, SceneCreate(title="Причал"))
    turns = []
    for index in range(4):
        turn = Turn(id=str(uuid4()), campaign_id=str(campaign_id), scene_id=str(scene.id),
                    role="assistant", content=str(index), status="active")
        db_session.add(turn)
        await db_session.flush()
        turns.append(turn)
    thesis = await scenes.create_thesis(
        scene.id, SceneThesisCreate(thesis_type=ThesisType.TENSION, text="лодка"), source_turn_id=turns[0].id
    )
    db_session.add(ThesisLifecycleProfile(thesis_id=str(thesis.id), semantic_key="лодка", ttl_turns=3))
    await db_session.flush()

    closed = await MemoryOperationsService(db_session).expire_scene_theses(campaign_id, scene.id)

    assert closed == 1
    assert await scenes.list_theses_by_scene(scene.id, active_only=True) == []
