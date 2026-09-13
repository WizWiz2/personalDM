from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.db.tables import Campaign, Entity, Scene, SceneParticipant
from app.db.truth_engine_table import (
    FluentAssertion,
    SemanticType,
    TruthEventRecord,
    WorldRelationAssertion,
)
from app.models.truth_engine import (
    CanonicalEventCreate,
    EntityResolutionDecision,
    FluentObservation,
    NewSemanticTypeDraft,
    RelationObservation,
    SemanticTypeCreate,
    SemanticTypeResolutionDecision,
    TruthEffectType,
    TruthEventEffectCreate,
)
from app.services.truth_engine import SemanticTypeRegistry, WorldReducer
from app.services.truth_engine_semantics import (
    ConstrainedSemanticResolver,
    SemanticObservationCompiler,
    SemanticResolutionError,
    TruthCandidateRetriever,
)


async def _world(db_session):
    campaign = Campaign(name="TE2 semantics")
    db_session.add(campaign)
    await db_session.flush()
    entities = [
        Entity(
            campaign_id=campaign.id,
            entity_type="character",
            canonical_name=name,
            description=description,
        )
        for name, description in [
            ("Hero", "Player character"),
            ("Keeper", "Current room keeper"),
            ("Merchant", "A distant merchant"),
        ]
    ]
    item = Entity(
        campaign_id=campaign.id,
        entity_type="item",
        canonical_name="Key",
        description="A physical key",
    )
    db_session.add_all([*entities, item])
    await db_session.flush()
    scene = Scene(campaign_id=campaign.id, title="Current room", status="active")
    db_session.add(scene)
    await db_session.flush()
    db_session.add_all(
        [
            SceneParticipant(scene_id=scene.id, entity_id=entities[0].id),
            SceneParticipant(scene_id=scene.id, entity_id=entities[1].id),
        ]
    )
    await db_session.flush()
    return (
        UUID(campaign.id),
        [UUID(entity.id) for entity in entities],
        UUID(item.id),
        UUID(scene.id),
    )


class StubSemanticResolver:
    def __init__(self, *decisions):
        self.decisions = list(decisions)

    async def resolve_semantic_type(self, *args, **kwargs):
        return self.decisions.pop(0), []


@pytest.mark.asyncio
async def test_entity_candidate_retrieval_is_structural_and_type_bounded(db_session):
    campaign_id, characters, item_id, scene_id = await _world(db_session)
    retriever = TruthCandidateRetriever(db_session)

    candidates = await retriever.entity_candidates(
        campaign_id,
        expected_types=["character"],
        scene_id=scene_id,
        context_entity_ids=[characters[2]],
    )

    assert [candidate.entity_id for candidate in candidates] == [
        characters[2],
        characters[0],
        characters[1],
    ]
    assert all(candidate.entity_type == "character" for candidate in candidates)
    assert candidates[0].context_linked is True
    assert candidates[1].scene_local is True
    assert item_id not in {candidate.entity_id for candidate in candidates}


@pytest.mark.asyncio
async def test_semantic_candidates_prioritize_active_subject_slots_and_hide_core_protocol(db_session):
    campaign_id, characters, _, _ = await _world(db_session)
    registry = SemanticTypeRegistry(db_session)
    active_type = await registry.create(
        campaign_id,
        SemanticTypeCreate(
            kind="fluent",
            canonical_label="Dynamic state",
            description="A dynamic arbitrary state for the subject.",
            cardinality="single",
        ),
    )
    other_type = await registry.create(
        campaign_id,
        SemanticTypeCreate(
            kind="fluent",
            canonical_label="Other state",
            description="Another unrelated arbitrary state.",
            cardinality="single",
        ),
    )
    core = SemanticType(
        campaign_id=str(campaign_id),
        system_key="core.test.protocol",
        kind="fluent",
        canonical_label="Core protocol state",
        description="Engine-owned protocol state.",
        cardinality="single",
    )
    db_session.add(core)
    await db_session.flush()
    await WorldReducer(db_session).append_and_reduce(
        campaign_id,
        CanonicalEventCreate(
            event_key="seed-dynamic-state",
            event_type="test",
            description="Seed one current dynamic state.",
            source_kind="test",
            effects=[
                TruthEventEffectCreate(
                    effect_type=TruthEffectType.SET_FLUENT,
                    payload={
                        "subject_entity_id": str(characters[0]),
                        "semantic_type_id": str(active_type),
                        "value": "current",
                    },
                )
            ],
        ),
    )

    candidates = await TruthCandidateRetriever(db_session).semantic_type_candidates(
        campaign_id,
        kind="fluent",
        subject_entity_id=characters[0],
    )

    assert candidates[0].semantic_type_id == active_type
    assert candidates[0].active_for_subject is True
    assert other_type in {candidate.semantic_type_id for candidate in candidates}
    assert UUID(core.id) not in {candidate.semantic_type_id for candidate in candidates}


def test_resolver_rejects_ids_outside_bounded_candidate_set():
    allowed_id = uuid4()
    candidate = {
        "entity_id": allowed_id,
        "entity_type": "character",
        "canonical_name": "Known",
    }
    from app.models.truth_engine import EntityResolutionCandidate

    candidates = [EntityResolutionCandidate(**candidate)]
    with pytest.raises(SemanticResolutionError, match="outside its candidates"):
        ConstrainedSemanticResolver.validate_entity_decision(
            EntityResolutionDecision(decision="existing", entity_id=uuid4()),
            candidates,
        )


@pytest.mark.asyncio
async def test_new_fluent_semantic_type_is_created_by_canonical_observation_event(db_session):
    campaign_id, characters, _, scene_id = await _world(db_session)
    decision = SemanticTypeResolutionDecision(
        decision="new",
        new_type=NewSemanticTypeDraft(
            canonical_label="Current illumination state",
            description="The currently established illumination state of this entity.",
            cardinality="single",
            value_schema={"type": "string"},
        ),
    )
    compiler = SemanticObservationCompiler(
        db_session,
        resolver=StubSemanticResolver(decision),
    )
    result = await compiler.compile_fluent(
        campaign_id,
        FluentObservation(
            observation_key="turn-1:observation-0",
            subject_entity_id=characters[0],
            semantic_description="current illumination state",
            value="lit",
            description="The room is now lit.",
            scene_id=scene_id,
            authority="dm_confirmed",
            evidence="The room is now lit.",
        ),
    )

    record = await db_session.get(TruthEventRecord, str(result.event_id))
    semantic_type = (
        await db_session.execute(
            select(SemanticType).where(
                SemanticType.campaign_id == str(campaign_id),
                SemanticType.system_key.is_(None),
                SemanticType.canonical_label == "Current illumination state",
            )
        )
    ).scalar_one()
    assertion = (
        await db_session.execute(
            select(FluentAssertion).where(
                FluentAssertion.campaign_id == str(campaign_id),
                FluentAssertion.subject_entity_id == str(characters[0]),
                FluentAssertion.semantic_type_id == semantic_type.id,
                FluentAssertion.is_current.is_(True),
            )
        )
    ).scalar_one()

    assert record is not None
    assert record.source_kind == "semantic_compiler"
    assert semantic_type.created_by_event_id == str(result.event_id)
    assert json.loads(assertion.value_json) == "lit"
    assert assertion.scene_id == str(scene_id)
    assert assertion.authority == "dm_confirmed"


@pytest.mark.asyncio
async def test_existing_single_fluent_slot_versions_by_semantic_id_not_wording(db_session):
    campaign_id, characters, _, _ = await _world(db_session)
    semantic_type_id = await SemanticTypeRegistry(db_session).create(
        campaign_id,
        SemanticTypeCreate(
            kind="fluent",
            canonical_label="Established state",
            description="One established changing state of the subject.",
            cardinality="single",
        ),
    )
    existing = SemanticTypeResolutionDecision(
        decision="existing",
        semantic_type_id=semantic_type_id,
    )
    compiler = SemanticObservationCompiler(
        db_session,
        resolver=StubSemanticResolver(existing, existing),
    )

    await compiler.compile_fluent(
        campaign_id,
        FluentObservation(
            observation_key="first",
            subject_entity_id=characters[0],
            semantic_description="first natural-language phrasing",
            value={"state": "before"},
            description="First observation.",
        ),
    )
    second = await compiler.compile_fluent(
        campaign_id,
        FluentObservation(
            observation_key="second",
            subject_entity_id=characters[0],
            semantic_description="completely different phrasing selected to the same ID",
            value={"state": "after"},
            description="Second observation.",
        ),
    )

    assertions = list(
        (
            await db_session.execute(
                select(FluentAssertion).where(
                    FluentAssertion.campaign_id == str(campaign_id),
                    FluentAssertion.semantic_type_id == str(semantic_type_id),
                )
            )
        ).scalars().all()
    )
    assert len(assertions) == 2
    current = next(row for row in assertions if row.is_current)
    old = next(row for row in assertions if not row.is_current)
    assert json.loads(current.value_json) == {"state": "after"}
    assert old.valid_until_event_id == str(second.event_id)


@pytest.mark.asyncio
async def test_relation_observation_uses_same_dynamic_semantic_pipeline(db_session):
    campaign_id, characters, _, _ = await _world(db_session)
    decision = SemanticTypeResolutionDecision(
        decision="new",
        new_type=NewSemanticTypeDraft(
            canonical_label="Current obligation target",
            description="The entity to whom the subject currently owes this obligation.",
            cardinality="single",
        ),
    )
    compiler = SemanticObservationCompiler(
        db_session,
        resolver=StubSemanticResolver(decision),
    )
    result = await compiler.compile_relation(
        campaign_id,
        RelationObservation(
            observation_key="relation-1",
            subject_entity_id=characters[0],
            object_entity_id=characters[1],
            semantic_description="current obligation target",
            description="The hero now owes the keeper.",
            authority="dm_confirmed",
            evidence="The debt is now owed to the keeper.",
        ),
    )

    relation = (
        await db_session.execute(
            select(WorldRelationAssertion).where(
                WorldRelationAssertion.campaign_id == str(campaign_id),
                WorldRelationAssertion.is_current.is_(True),
            )
        )
    ).scalar_one()
    semantic_type = await db_session.get(SemanticType, relation.semantic_type_id)
    assert relation.subject_entity_id == str(characters[0])
    assert relation.object_entity_id == str(characters[1])
    assert relation.authority == "dm_confirmed"
    assert semantic_type is not None
    assert semantic_type.created_by_event_id == str(result.event_id)
