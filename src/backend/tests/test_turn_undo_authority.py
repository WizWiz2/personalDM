from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.belief_repo import BeliefRepository
from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.fact_repo import FactRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.proposed_change_repo import ProposedChangeRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.repositories.turn_repo import TurnRepository
from app.db.tables import Entity, PostTurnJob
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.location import LocationCreate
from app.models.proposed_change import ChangeType, ProposalAction, ProposedChangeCreate
from app.models.scene import SceneCreate
from app.models.turn import TurnCreate
from app.models.turn_authority import PlannedNpcIntroduction, TurnAuthority
from app.services.canon_applier import CanonApplier
from app.services.post_turn_processor import PostTurnProcessor
from app.services.turn_outcome_materializer import TurnOutcomeMaterializer
from app.services.turn_undo_service import TurnUndoService


async def _base_turn(db_session: AsyncSession):
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    entities = EntityRepository(db_session)
    locations = LocationRepository(db_session)
    scenes = SceneRepository(db_session)
    turns = TurnRepository(db_session)

    await campaigns.create(campaign_id, CampaignCreate(name="Undo authority"))
    location = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Переулок"),
    )
    player = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Рэт", current_location_id=location.id),
    )
    await campaigns.update(
        campaign_id,
        CampaignUpdate(player_character_id=player.id),
    )
    scene = await scenes.create(
        campaign_id,
        SceneCreate(title="Переулок", location_id=location.id),
    )
    await scenes.add_participant(scene.id, player.id, allow_movement=True)
    user = await turns.create(
        campaign_id,
        TurnCreate(
            role="user",
            content="Стучу в дверь.",
            scene_id=scene.id,
        ),
    )
    assistant = await turns.create(
        campaign_id,
        TurnCreate(
            role="assistant",
            content="Дежурный открывает дверь и сообщает о синем свечении.",
            parent_turn_id=user.id,
            scene_id=scene.id,
        ),
    )
    await db_session.commit()
    return campaign_id, player, scene, user, assistant


@pytest.mark.asyncio
async def test_undo_replays_only_active_canon_and_removes_turn_introduced_npc(
    db_session: AsyncSession,
):
    campaign_id, player, scene, user, assistant = await _base_turn(db_session)
    proposals = ProposedChangeRepository(db_session)
    applier = CanonApplier(db_session)

    changes = [
        ProposedChangeCreate(
            change_type=ChangeType.FACT,
            payload={
                "subject": "фабрика",
                "predicate": "светится",
                "object_value": "синим",
                "confidence": 1.0,
                "visibility": "public",
                "scope": "campaign",
            },
        ),
        ProposedChangeCreate(
            change_type=ChangeType.KNOWLEDGE,
            payload={
                "recipient_id": str(player.id),
                "proposition": "Внутри фабрики видели синее свечение.",
                "confidence": 0.9,
            },
        ),
    ]
    created = await proposals.create_batch(assistant.id, changes)
    for row, change in zip(created, changes, strict=True):
        await applier.apply(
            campaign_id,
            change.change_type,
            change.payload,
            assistant.id,
        )
        await proposals.resolve(row.id, ProposalAction(status="accepted"))

    authority = TurnAuthority(
        campaign_id=campaign_id,
        trigger_turn_id=user.id,
        player_character_id=player.id,
        player_character_name="Рэт",
        player_input="Стучу в дверь.",
        source_scene_id=scene.id,
        target_scene_id=scene.id,
        present_character_names=["Рэт"],
        allowed_new_npcs=[
            PlannedNpcIntroduction(
                canonical_name="Дежурный фабрики",
                role="дежурный",
                reason="Ответил на прямой стук игрока.",
                temporary_name=True,
            )
        ],
        observable_consequences=["Дежурный фабрики открывает дверь."],
    )
    outcome = await TurnOutcomeMaterializer(db_session).materialize(
        authority,
        source_turn_id=assistant.id,
    )
    assert len(outcome.introduced_character_ids) == 1
    await db_session.commit()

    assert len(await FactRepository(db_session).list_active(campaign_id)) == 1
    assert len(await BeliefRepository(db_session).get_for_character(player.id)) == 1
    assert await db_session.get(Entity, str(outcome.introduced_character_ids[0])) is not None

    assert await TurnUndoService(db_session).undo_last_pair(campaign_id) is True
    await db_session.commit()

    assert await FactRepository(db_session).list_active(campaign_id) == []
    assert await BeliefRepository(db_session).get_for_character(player.id) == []
    assert await db_session.get(Entity, str(outcome.introduced_character_ids[0])) is None

    history = await TurnRepository(db_session).get_history(
        campaign_id,
        active_only=False,
    )
    pair = {str(turn.id): turn.status for turn in history}
    assert pair[str(user.id)] == "undone"
    assert pair[str(assistant.id)] == "undone"


@pytest.mark.asyncio
async def test_background_memory_job_becomes_noop_if_turn_was_undone_first(
    db_session: AsyncSession,
):
    campaign_id, _player, _scene, _user, assistant = await _base_turn(db_session)
    processor = PostTurnProcessor(db_session)
    await processor.enqueue(campaign_id, assistant.id)
    await db_session.commit()

    assert await TurnRepository(db_session).undo_last_pair(campaign_id) is True
    await db_session.commit()

    await processor.process_turn(assistant.id)

    proposal_rows = await ProposedChangeRepository(db_session).get_for_turn(assistant.id)
    assert proposal_rows == []
    jobs = (
        await db_session.execute(
            select(PostTurnJob).where(PostTurnJob.assistant_turn_id == str(assistant.id))
        )
    ).scalars().all()
    assert jobs
    assert all(job.status == "completed" for job in jobs)
    assert all("skipped" in (job.error or "") for job in jobs)
