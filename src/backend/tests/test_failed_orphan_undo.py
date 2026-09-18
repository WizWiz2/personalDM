from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.job_repo import GenerationRunRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.repositories.turn_repo import TurnRepository
from app.db.tables import GenerationRun
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.location import LocationCreate
from app.models.scene import SceneCreate
from app.models.turn import TurnCreate
from app.services.turn_undo_service import TurnUndoService


@pytest.mark.asyncio
async def test_undo_discards_failed_orphan_without_touching_previous_pair(
    db_session: AsyncSession,
):
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    entities = EntityRepository(db_session)
    locations = LocationRepository(db_session)
    scenes = SceneRepository(db_session)
    turns = TurnRepository(db_session)
    runs = GenerationRunRepository(db_session)

    await campaigns.create(campaign_id, CampaignCreate(name="Failed orphan undo"))
    location = await locations.create(
        campaign_id, LocationCreate(canonical_name="Коридор")
    )
    player = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Кай", current_location_id=location.id),
    )
    await campaigns.update(campaign_id, CampaignUpdate(player_character_id=player.id))
    scene = await scenes.create(
        campaign_id, SceneCreate(title="Коридор", location_id=location.id)
    )
    await scenes.add_participant(scene.id, player.id, allow_movement=True)

    completed_user = await turns.create(
        campaign_id,
        TurnCreate(role="user", content="Стою у двери.", scene_id=scene.id),
    )
    completed_assistant = await turns.create(
        campaign_id,
        TurnCreate(
            role="assistant",
            content="Дверь закрыта, коридор тих.",
            parent_turn_id=completed_user.id,
            scene_id=scene.id,
        ),
    )
    completed_run = await runs.create(campaign_id, completed_user.id)
    await runs.set_status(
        completed_run.id, "completed", assistant_turn_id=completed_assistant.id
    )

    orphan = await turns.create(
        campaign_id,
        TurnCreate(role="user", content="Не заметил никого.", scene_id=scene.id),
    )
    await turns.mark_failed(orphan.id)
    failed_run = await runs.create(campaign_id, orphan.id)
    await runs.set_status(
        failed_run.id,
        "failed",
        error="frozen intent pipeline failed: ExactTurnOutcomeDecisionDraft npc_introductions.0",
    )
    await db_session.commit()

    success = await TurnUndoService(db_session).undo_last_pair(campaign_id)
    assert success is True
    await db_session.commit()

    refreshed_orphan = await turns.get_by_id(orphan.id)
    assert refreshed_orphan is not None
    assert refreshed_orphan.status == "undone"

    still_active_user = await turns.get_by_id(completed_user.id)
    still_active_assistant = await turns.get_by_id(completed_assistant.id)
    assert still_active_user is not None and still_active_user.status == "active"
    assert still_active_assistant is not None and still_active_assistant.status == "active"

    remaining = (
        await db_session.execute(
            select(GenerationRun).where(GenerationRun.id == str(failed_run.id))
        )
    ).scalar_one_or_none()
    assert remaining is None

    latest = await runs.list_for_campaign(campaign_id, limit=1)
    assert latest and latest[0].id == completed_run.id
    assert latest[0].status == "completed"

    # A second undo still targets the completed pair, not a missing orphan.
    pair = await turns.get_latest_undoable_pair(campaign_id)
    assert pair is not None
    assert pair[0].id == completed_user.id
    assert pair[1].id == completed_assistant.id
