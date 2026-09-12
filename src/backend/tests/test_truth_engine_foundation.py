from __future__ import annotations

import json
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.db.tables import Campaign, Entity, Turn
from app.db.truth_engine_table import (
    AssertionSupport,
    EntityMention,
    FluentAssertion,
    TruthEventEffect,
    TruthEventRecord,
    WorldRelationAssertion,
)
from app.models.truth_engine import (
    CanonicalEventCreate,
    SemanticTypeCreate,
    TruthEventEffectCreate,
    TruthEffectType,
)
from app.services.truth_engine import CanonicalEventStore, SemanticTypeRegistry, WorldReducer


async def _world(db_session):
    campaign = Campaign(name="TE2 test")
    db_session.add(campaign)
    await db_session.flush()
    entities = [
        Entity(
            campaign_id=campaign.id,
            entity_type="test",
            canonical_name=f"entity-{index}",
        )
        for index in range(3)
    ]
    db_session.add_all(entities)
    await db_session.flush()
    return UUID(campaign.id), [UUID(row.id) for row in entities]


async def _semantic_type(db_session, campaign_id, *, kind, cardinality="single"):
    return await SemanticTypeRegistry(db_session).create(
        campaign_id,
        SemanticTypeCreate(
            kind=kind,
            canonical_label=f"test-{kind}-{cardinality}",
            description="Arbitrary semantic property used to prove ID-based projection behavior.",
            cardinality=cardinality,
        ),
    )


def _set_fluent(event_key, subject_id, type_id, value, *, source_turn_id=None):
    return CanonicalEventCreate(
        event_key=event_key,
        event_type="test_state_change",
        description="A deterministic state changed.",
        source_kind="test_receipt",
        source_turn_id=source_turn_id,
        effects=[
            TruthEventEffectCreate(
                effect_type=TruthEffectType.SET_FLUENT,
                payload={
                    "subject_entity_id": str(subject_id),
                    "semantic_type_id": str(type_id),
                    "value": value,
                },
            )
        ],
    )


@pytest.mark.asyncio
async def test_single_cardinality_fluent_versions_by_stable_ids(db_session):
    campaign_id, entities = await _world(db_session)
    semantic_type_id = await _semantic_type(
        db_session, campaign_id, kind="fluent", cardinality="single"
    )
    reducer = WorldReducer(db_session)

    first = await reducer.append_and_reduce(
        campaign_id,
        _set_fluent("event-1", entities[0], semantic_type_id, {"code": "v1"}),
    )
    second = await reducer.append_and_reduce(
        campaign_id,
        _set_fluent("event-2", entities[0], semantic_type_id, {"code": "v2"}),
    )

    rows = list(
        (
            await db_session.execute(
                select(FluentAssertion)
                .where(FluentAssertion.campaign_id == str(campaign_id))
                .order_by(FluentAssertion.created_at, FluentAssertion.id)
            )
        ).scalars().all()
    )
    assert first.applied_effects == 1
    assert second.applied_effects == 1
    assert len(rows) == 2
    old = next(row for row in rows if json.loads(row.value_json)["code"] == "v1")
    current = next(row for row in rows if json.loads(row.value_json)["code"] == "v2")
    assert old.is_current is False
    assert old.valid_until_event_id == str(second.event_id)
    assert current.is_current is True
    assert current.valid_until_event_id is None


@pytest.mark.asyncio
async def test_repeating_same_fluent_value_adds_support_without_duplicate_state(db_session):
    campaign_id, entities = await _world(db_session)
    semantic_type_id = await _semantic_type(db_session, campaign_id, kind="fluent")
    reducer = WorldReducer(db_session)

    await reducer.append_and_reduce(
        campaign_id,
        _set_fluent("event-a", entities[0], semantic_type_id, {"value": 7}),
    )
    await reducer.append_and_reduce(
        campaign_id,
        _set_fluent("event-b", entities[0], semantic_type_id, {"value": 7}),
    )

    assertion_count = (
        await db_session.execute(
            select(func.count(FluentAssertion.id)).where(
                FluentAssertion.campaign_id == str(campaign_id)
            )
        )
    ).scalar_one()
    support_count = (
        await db_session.execute(
            select(func.count(AssertionSupport.id)).where(
                AssertionSupport.campaign_id == str(campaign_id),
                AssertionSupport.assertion_kind == "fluent",
            )
        )
    ).scalar_one()
    assert assertion_count == 1
    assert support_count == 2


@pytest.mark.asyncio
async def test_multi_relation_keeps_independent_graph_edges(db_session):
    campaign_id, entities = await _world(db_session)
    semantic_type_id = await _semantic_type(
        db_session, campaign_id, kind="relation", cardinality="multi"
    )
    reducer = WorldReducer(db_session)

    event = CanonicalEventCreate(
        event_key="relation-event",
        event_type="test_relation_change",
        description="Two graph edges become true.",
        source_kind="test_receipt",
        effects=[
            TruthEventEffectCreate(
                effect_type=TruthEffectType.ADD_RELATION,
                payload={
                    "subject_entity_id": str(entities[0]),
                    "semantic_type_id": str(semantic_type_id),
                    "object_entity_id": str(target),
                },
            )
            for target in entities[1:]
        ],
    )
    await reducer.append_and_reduce(campaign_id, event)

    rows = list(
        (
            await db_session.execute(
                select(WorldRelationAssertion).where(
                    WorldRelationAssertion.campaign_id == str(campaign_id),
                    WorldRelationAssertion.is_current.is_(True),
                )
            )
        ).scalars().all()
    )
    assert {row.object_entity_id for row in rows} == {str(entities[1]), str(entities[2])}


@pytest.mark.asyncio
async def test_event_key_is_idempotent_and_does_not_duplicate_effects(db_session):
    campaign_id, entities = await _world(db_session)
    semantic_type_id = await _semantic_type(db_session, campaign_id, kind="fluent")
    store = CanonicalEventStore(db_session)
    payload = _set_fluent("same-key", entities[0], semantic_type_id, "value")

    first = await store.append(campaign_id, payload)
    second = await store.append(campaign_id, payload)

    assert first.event_id == second.event_id
    event_count = (
        await db_session.execute(
            select(func.count(TruthEventRecord.event_id)).where(
                TruthEventRecord.campaign_id == str(campaign_id)
            )
        )
    ).scalar_one()
    effect_count = (
        await db_session.execute(
            select(func.count(TruthEventEffect.id)).where(
                TruthEventEffect.event_id == str(first.event_id)
            )
        )
    ).scalar_one()
    assert event_count == 1
    assert effect_count == 1


@pytest.mark.asyncio
async def test_multiple_mentions_can_point_to_one_entity_without_identity_duplication(db_session):
    campaign_id, entities = await _world(db_session)
    reducer = WorldReducer(db_session)
    event = CanonicalEventCreate(
        event_key="identity-observation",
        event_type="identity_observation",
        description="One entity was referenced in two ways.",
        source_kind="semantic_compiler",
        effects=[
            TruthEventEffectCreate(
                effect_type=TruthEffectType.RECORD_MENTION,
                payload={"entity_id": str(entities[0]), "mention_text": "role label"},
            ),
            TruthEventEffectCreate(
                effect_type=TruthEffectType.RECORD_MENTION,
                payload={"entity_id": str(entities[0]), "mention_text": "personal label"},
            ),
        ],
    )
    await reducer.append_and_reduce(campaign_id, event)

    mentions = list(
        (
            await db_session.execute(
                select(EntityMention).where(EntityMention.campaign_id == str(campaign_id))
            )
        ).scalars().all()
    )
    assert len(mentions) == 2
    assert {row.entity_id for row in mentions} == {str(entities[0])}
    assert {row.mention_text for row in mentions} == {"role label", "personal label"}


@pytest.mark.asyncio
async def test_rebuild_excludes_reverted_turn_events_and_restores_previous_state(db_session):
    campaign_id, entities = await _world(db_session)
    semantic_type_id = await _semantic_type(db_session, campaign_id, kind="fluent")
    first_turn = Turn(campaign_id=str(campaign_id), role="assistant", content="first")
    second_turn = Turn(campaign_id=str(campaign_id), role="assistant", content="second")
    db_session.add_all([first_turn, second_turn])
    await db_session.flush()

    reducer = WorldReducer(db_session)
    await reducer.append_and_reduce(
        campaign_id,
        _set_fluent(
            "turn-1-effect",
            entities[0],
            semantic_type_id,
            "before",
            source_turn_id=UUID(first_turn.id),
        ),
    )
    await reducer.append_and_reduce(
        campaign_id,
        _set_fluent(
            "turn-2-effect",
            entities[0],
            semantic_type_id,
            "after",
            source_turn_id=UUID(second_turn.id),
        ),
    )

    changed = await CanonicalEventStore(db_session).set_turn_status(
        campaign_id, UUID(second_turn.id), active=False
    )
    rebuilt = await reducer.rebuild(campaign_id)

    current = list(
        (
            await db_session.execute(
                select(FluentAssertion).where(
                    FluentAssertion.campaign_id == str(campaign_id),
                    FluentAssertion.is_current.is_(True),
                )
            )
        ).scalars().all()
    )
    assert changed == 1
    assert rebuilt.replayed_events == 1
    assert len(current) == 1
    assert json.loads(current[0].value_json) == "before"
