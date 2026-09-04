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
from app.db.tables import Entity, Event, Item
from app.db.truth_engine_table import FluentAssertion, SemanticType, TruthEventRecord
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.location import LocationCreate
from app.models.scene import SceneCreate
from app.models.scene_state import LocationExitCreate
from app.models.turn import TurnCreate
from app.services.scene_lifecycle import SceneLifecycleService
from app.services.scene_state_service import SceneStateService
from app.services.scene_transition_executor import SceneTransitionExecutor
from app.services.truth_engine_receipts import (
    CORE_ENTITY_LOCATION,
    CORE_ITEM_POSITION,
    StructuredReceiptEventCompiler,
)
from app.services.turn_planner import (
    ActionSequencePlan,
    ActionStepPlan,
    SceneTransitionPlan,
    TurnPlan,
)
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
    await SceneStateService(db_session).create_exit(
        campaign_id,
        hall.id,
        LocationExitCreate(
            to_location_id=room.id,
            label="Room",
            bidirectional=True,
            reverse_label="Hall",
        ),
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


async def _current_projection(db_session, campaign_id, subject_id, system_key):
    semantic_type = (
        await db_session.execute(
            select(SemanticType).where(
                SemanticType.campaign_id == str(campaign_id),
                SemanticType.system_key == system_key,
            )
        )
    ).scalar_one()
    assertion = (
        await db_session.execute(
            select(FluentAssertion).where(
                FluentAssertion.campaign_id == str(campaign_id),
                FluentAssertion.subject_entity_id == str(subject_id),
                FluentAssertion.semantic_type_id == semantic_type.id,
                FluentAssertion.is_current.is_(True),
            )
        )
    ).scalar_one()
    return json.loads(assertion.value_json), assertion


async def _current_location_projection(db_session, campaign_id, hero_id):
    return await _current_projection(
        db_session,
        campaign_id,
        hero_id,
        CORE_ENTITY_LOCATION,
    )


async def _current_item_projection(db_session, campaign_id, item_id):
    return await _current_projection(
        db_session,
        campaign_id,
        item_id,
        CORE_ITEM_POSITION,
    )


def _inventory_turn_plan(
    *,
    item_id,
    operation: str,
    target_id=None,
    outcome: str,
) -> TurnPlan:
    return TurnPlan(
        player_intent=outcome,
        resolution="sequence",
        action_sequence=ActionSequencePlan(
            summary=outcome,
            steps=[
                ActionStepPlan(
                    action_type="inventory",
                    intent=outcome,
                    resolution="auto_success",
                    safe_mundane=True,
                    observable_outcome=outcome,
                    item_id=item_id,
                    inventory_operation=operation,
                    inventory_target_id=target_id,
                )
            ],
        ),
    )


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
            context_snapshot={
                "scene_transition": {
                    "status": "applied",
                    "source_scene_id": str(source.id),
                    "target_scene_id": str(transition.target_scene_id),
                }
            },
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


@pytest.mark.asyncio
async def test_applied_take_projects_item_position_and_undo_restores_baseline(
    db_session: AsyncSession,
):
    campaign_id, hall, _room, hero, source, user = await _movement_world(db_session)
    item_id = uuid4()
    db_session.add(
        Entity(
            id=str(item_id),
            campaign_id=str(campaign_id),
            entity_type="item",
            canonical_name="brass key",
        )
    )
    db_session.add(
        Item(
            entity_id=str(item_id),
            current_owner_id=None,
            current_location_id=str(hall.id),
        )
    )
    await db_session.flush()

    plan = _inventory_turn_plan(
        item_id=item_id,
        operation="take",
        outcome="Take the brass key.",
    )
    executor = SceneTransitionExecutor(db_session)
    applied = await executor.apply(
        campaign_id,
        source.id,
        user.id,
        plan.scene_transition,
    )
    assert applied is not None
    item = await db_session.get(Item, str(item_id))
    assert item.current_owner_id == str(hero.id)
    assert item.current_location_id is None

    # Prepared execution is not canonical truth until publication succeeds.
    count_before_publish = (
        await db_session.execute(
            select(func.count(TruthEventRecord.event_id)).where(
                TruthEventRecord.campaign_id == str(campaign_id)
            )
        )
    ).scalar_one()
    assert count_before_publish == 0

    assert await executor.mark_applied(applied.transition_id)
    current, assertion = await _current_item_projection(db_session, campaign_id, item_id)
    assert current == {"mode": "owned", "entity_id": str(hero.id)}
    assert assertion.authority == "executor_receipt"

    records = list(
        (
            await db_session.execute(
                select(TruthEventRecord)
                .where(TruthEventRecord.campaign_id == str(campaign_id))
                .order_by(TruthEventRecord.sequence)
            )
        ).scalars().all()
    )
    assert [row.source_kind for row in records] == ["legacy_baseline", "executor_receipt"]
    assert records[0].source_turn_id is None
    assert records[1].source_turn_id == str(user.id)

    await TurnRepository(db_session).create(
        campaign_id,
        TurnCreate(
            role="assistant",
            content="You take the brass key.",
            scene_id=source.id,
            parent_turn_id=user.id,
            context_snapshot={"turn_authority": {"action_sequence": {"status": "applied"}}},
        ),
    )
    await db_session.flush()

    assert await TurnUndoService(db_session).undo_last_pair(campaign_id)

    restored, restored_assertion = await _current_item_projection(
        db_session,
        campaign_id,
        item_id,
    )
    assert restored == {"mode": "located", "entity_id": str(hall.id)}
    assert restored_assertion.authority == "legacy_baseline"
    item = await db_session.get(Item, str(item_id))
    assert item.current_owner_id is None
    assert item.current_location_id == str(hall.id)

    records = list(
        (
            await db_session.execute(
                select(TruthEventRecord)
                .where(TruthEventRecord.campaign_id == str(campaign_id))
                .order_by(TruthEventRecord.sequence)
            )
        ).scalars().all()
    )
    assert records[0].status == "active"
    assert records[1].status == "reverted"


@pytest.mark.asyncio
async def test_applied_give_projects_machine_resolved_owner_without_semantic_rewrite(
    db_session: AsyncSession,
):
    campaign_id, hall, _room, hero, source, user = await _movement_world(db_session)
    entities = EntityRepository(db_session)
    target = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Recipient", current_location_id=hall.id),
    )
    await SceneRepository(db_session).add_participant(source.id, target.id)

    item_id = uuid4()
    db_session.add(
        Entity(
            id=str(item_id),
            campaign_id=str(campaign_id),
            entity_type="item",
            canonical_name="sealed letter",
        )
    )
    db_session.add(
        Item(
            entity_id=str(item_id),
            current_owner_id=str(hero.id),
            current_location_id=None,
        )
    )
    await db_session.flush()

    plan = _inventory_turn_plan(
        item_id=item_id,
        operation="give",
        target_id=target.id,
        outcome="Give the sealed letter to the recipient.",
    )
    executor = SceneTransitionExecutor(db_session)
    applied = await executor.apply(
        campaign_id,
        source.id,
        user.id,
        plan.scene_transition,
    )
    assert applied is not None
    assert await executor.mark_applied(applied.transition_id)

    item = await db_session.get(Item, str(item_id))
    assert item.current_owner_id == str(target.id)
    assert item.current_location_id is None

    current, assertion = await _current_item_projection(db_session, campaign_id, item_id)
    assert current == {"mode": "owned", "entity_id": str(target.id)}
    assert assertion.authority == "executor_receipt"

    # Publication is idempotent; there is exactly one imported baseline and one receipt event.
    await StructuredReceiptEventCompiler(db_session).compile_applied_transition(
        applied.transition_id
    )
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
    assert records[0].source_kind == "legacy_baseline"
    assert records[1].source_kind == "executor_receipt"
