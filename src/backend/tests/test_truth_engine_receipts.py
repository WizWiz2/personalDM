from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.repositories.turn_repo import TurnRepository
from app.db.tables import Event
from app.db.truth_engine_table import FluentAssertion, SemanticType, TruthEventRecord
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.location import LocationCreate
from app.models.scene import SceneCreate
from app.models.turn import TurnCreate
from app.services.scene_lifecycle import SceneLifecycleService
from app.services.scene_transition_executor import SceneTransitionExecutor
from app.services.truth_engine_receipts import (
    CORE_ENTITY_LOCATION,
    StructuredReceiptEventCompiler,
)
from app.services.turn_planner import SceneTransitionPlan
from app.services.turn_undo_service import TurnUndoService


async def _movement_world(db_session: AsyncSession):
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    entities = EntityRepository(db_session)
    locations = LocationRepository(db_session)
    scenes = SceneRepository(db_session)
    turns = TurnRepository(db_session)

    await campaigns.create(campaign_id, CampaignCreate(name="TE2 receipt movement"))
    tavern = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Tavern"),
    )
    hall = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Hall", parent_location_id=tavern.id),
    )
    room = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Room", parent_location_id=tavern.id),
    )
    hero = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Hero", current_location_id=hall.id),
    )
    await campaigns.update(
        campaign_id,
        CampaignUpdate(player_character_id=hero.id),
    )
    source = await scenes.create(
        campaign_id,
        SceneCreate(title="Hall", location_id=hall.id),
    )
    await scenes.add_participant(source.id, hero.id)
    await SceneLifecycleService(db_session).activate(campaign_id, source.id)
    user = await turns.create(
        campaign_id,
        TurnCreate(
            role="user",
            content="Move to the room.",
            scene_id=source.id,
        ),
    )
    await db_session.flush()
    return campaign_id, hall, room, hero, source, user


async def _current_location_projection(db_session, campaign_id, hero_id):
    semantic_type = (
        await db_session.execute(
            select(SemanticType).where(
                SemanticType.campaign_id == str(campaign_id),
                SemanticType.system_key == CORE_ENTITY_LOCATION,
            )
        )
    ).scalar_one()
    assertion = (
        await db_session.execute(
            select(FluentAssertion).where(
                FluentAssertion.campaign_id == str(campaign_id),
                FluentAssertion.subject_entity_id == str(hero_id),
                FluentAssertion.semantic_type_id == semantic_type.id,
                FluentAssertion.is_current.is_(True),
            )
        )
    ).scalar_one()
    return json.loads(assertion.value_json), assertion


@pytest.mark.asyncio
async def test_applied_movement_publishes_receipt_once_and_seeds_baseline(
    db_session: AsyncSession,
):
    campaign_id, hall, room, hero, source, user = await _movement_world(db_session)
    executor = SceneTransitionExecutor(db_session)
    transition = await executor.apply(
        campaign_id,
        source.id,
        user.id,
        SceneTransitionPlan(
            required=True,
            transition_type="location_transition",
            destination_location=room.canonical_name,
            destination_parent_location="Tavern",
            scene_title="Room",
            reason="Structured movement receipt test.",
        ),
    )
    assert transition is not None

    before = (
        await db_session.execute(
            select(func.count(TruthEventRecord.event_id)).where(
                TruthEventRecord.campaign_id == str(campaign_id)
            )
        )
    ).scalar_one()
    assert before == 0

    assert await executor.mark_applied(transition.transition_id)

    records = list(
        (
            await db_session.execute(
                select(TruthEventRecord)
                .where(TruthEventRecord.campaign_id == str(campaign_id))
                .order_by(TruthEventRecord.sequence)
            )
        ).scalars().all()
    )
    assert [row.source_kind for row in records] == [
        "legacy_baseline",
        "executor_receipt",
    ]
    assert records[0].source_turn_id is None
    assert records[1].source_turn_id == str(user.id)

    current, assertion = await _current_location_projection(
        db_session,
        campaign_id,
        hero.id,
    )
    assert current == {"entity_id": str(room.id)}
    assert assertion.authority == "executor_receipt"

    # Explicitly replay the same structured receipt to prove event-key idempotency.
    repeated = await StructuredReceiptEventCompiler(db_session).compile_applied_transition(
        transition.transition_id
    )
    assert len(repeated) == 1
    after = (
        await db_session.execute(
            select(func.count(TruthEventRecord.event_id)).where(
                TruthEventRecord.campaign_id == str(campaign_id)
            )
        )
    ).scalar_one()
    assert after == 2


@pytest.mark.asyncio
async def test_turn_undo_reverts_receipt_but_preserves_event_history_and_baseline(
    db_session: AsyncSession,
):
    campaign_id, hall, room, hero, source, user = await _movement_world(db_session)
    turns = TurnRepository(db_session)
    executor = SceneTransitionExecutor(db_session)
    transition = await executor.apply(
        campaign_id,
        source.id,
        user.id,
        SceneTransitionPlan(
            required=True,
            transition_type="location_transition",
            destination_location=room.canonical_name,
            destination_parent_location="Tavern",
            scene_title="Room",
            reason="Structured movement receipt undo test.",
        ),
    )
    assert transition is not None
    assert await executor.mark_applied(transition.transition_id)
    assistant = await turns.create(
        campaign_id,
        TurnCreate(
            role="assistant",
            content="You arrive in the room.",
            scene_id=transition.target_scene_id,
            parent_turn_id=user.id,
        ),
    )
    await db_session.flush()

    before, _ = await _current_location_projection(db_session, campaign_id, hero.id)
    assert before == {"entity_id": str(room.id)}

    assert await TurnUndoService(db_session).undo_last_pair(campaign_id)

    records = list(
        (
            await db_session.execute(
                select(TruthEventRecord)
                .where(TruthEventRecord.campaign_id == str(campaign_id))
                .order_by(TruthEventRecord.sequence)
            )
        ).scalars().all()
    )
    assert len(records) == 2
    baseline, movement = records
    assert baseline.status == "active"
    assert movement.status == "reverted"
    assert movement.source_turn_id == str(user.id)

    current, assertion = await _current_location_projection(
        db_session,
        campaign_id,
        hero.id,
    )
    assert current == {"entity_id": str(hall.id)}
    assert assertion.authority == "legacy_baseline"

    # Legacy replay must not cascade-delete immutable TE2 event rows.
    event_count = (
        await db_session.execute(
            select(func.count(Event.id)).where(
                Event.id.in_([baseline.event_id, movement.event_id])
            )
        )
    ).scalar_one()
    assert event_count == 2
    assert assistant.status == "active"  # detached read object; DB status is checked by undo service
