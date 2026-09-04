from __future__ import annotations

import json
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.db.tables import Campaign, Entity, Turn
from app.db.truth_engine_table import FluentAssertion, SemanticType, TruthEventRecord
from app.models.truth_engine import (
    EntityResolutionDecision,
    NewSemanticTypeDraft,
    SemanticTypeResolutionDecision,
)
from app.models.truth_engine_residual import (
    ResidualEntityMention,
    ResidualFluentObservation,
    SemanticResidualEnvelope,
)
from app.services.truth_engine_turn_context import SemanticTurnContextReader
from app.services.truth_engine_writer import SemanticResidualWriterService


class StubExtractor:
    def __init__(self, envelope: SemanticResidualEnvelope):
        self.envelope = envelope
        self.calls = 0

    async def extract(self, *args, **kwargs):
        self.calls += 1
        return self.envelope


class StubJointEntityResolver:
    def __init__(self, entity_id: UUID):
        self.entity_id = entity_id
        self.calls = 0

    async def resolve(self, campaign_id, observations, *, local_graph=None):
        self.calls += 1
        return {
            observation.observation_key: EntityResolutionDecision(
                decision="existing",
                entity_id=self.entity_id,
            )
            for observation in observations
        }


class StubSemanticResolver:
    def __init__(self):
        self.calls = 0

    async def resolve_semantic_type(self, *args, **kwargs):
        self.calls += 1
        return (
            SemanticTypeResolutionDecision(
                decision="new",
                new_type=NewSemanticTypeDraft(
                    canonical_label="Current visible condition",
                    description="The currently established visible condition of the entity.",
                    cardinality="single",
                    value_schema={"type": "string"},
                ),
            ),
            [],
        )


class InactiveBarrierReader(SemanticTurnContextReader):
    async def pair_is_active(self, assistant_turn_id, expected_user_turn_id):
        return False


async def _turn_pair(db_session, *, actor_scoped: bool = False):
    campaign = Campaign(name="TE2 semantic writer")
    db_session.add(campaign)
    await db_session.flush()
    entity = Entity(
        campaign_id=campaign.id,
        entity_type="character",
        canonical_name="Keeper",
    )
    db_session.add(entity)
    await db_session.flush()
    user = Turn(
        campaign_id=campaign.id,
        role="user",
        content="Я смотрю на хранителя.",
        status="active",
    )
    db_session.add(user)
    await db_session.flush()
    assistant = Turn(
        campaign_id=campaign.id,
        role="assistant",
        content="Хранитель явно промок под дождём.",
        parent_turn_id=user.id,
        acting_character_id=(entity.id if actor_scoped else None),
        status="active",
        context_snapshot="{}",
    )
    db_session.add(assistant)
    await db_session.flush()
    await db_session.commit()
    return campaign, entity, user, assistant


def _envelope() -> SemanticResidualEnvelope:
    return SemanticResidualEnvelope(
        entities=[
            ResidualEntityMention(
                ref="keeper",
                mention_text="хранитель",
                entity_type="character",
                evidence="Хранитель явно промок под дождём.",
            )
        ],
        fluents=[
            ResidualFluentObservation(
                atom_key="visible-condition",
                subject_ref="keeper",
                semantic_description="currently established visible physical condition",
                value="wet from rain",
                description="The keeper is visibly wet from rain.",
                evidence="Хранитель явно промок под дождём.",
                cardinality_hint="single",
            )
        ],
    )


@pytest.mark.asyncio
async def test_writer_publishes_user_sourced_events_and_is_retry_idempotent(db_session):
    campaign, entity, user, assistant = await _turn_pair(db_session)
    extractor = StubExtractor(_envelope())
    entity_resolver = StubJointEntityResolver(UUID(entity.id))
    semantic_resolver = StubSemanticResolver()
    writer = SemanticResidualWriterService(
        db_session,
        extractor=extractor,
        entity_resolver=entity_resolver,
        semantic_resolver=semantic_resolver,
    )

    assert await writer.write(UUID(assistant.id)) is True
    assert await writer.write(UUID(assistant.id)) is True

    records = list(
        (
            await db_session.execute(
                select(TruthEventRecord)
                .where(TruthEventRecord.campaign_id == campaign.id)
                .order_by(TruthEventRecord.sequence)
            )
        ).scalars().all()
    )
    assert len(records) == 2
    assert {record.source_kind for record in records} == {"semantic_compiler"}
    assert {record.source_turn_id for record in records} == {user.id}
    assert entity_resolver.calls == 1
    assert semantic_resolver.calls == 1

    semantic_types = list(
        (
            await db_session.execute(
                select(SemanticType).where(
                    SemanticType.campaign_id == campaign.id,
                    SemanticType.system_key.is_(None),
                )
            )
        ).scalars().all()
    )
    assert len(semantic_types) == 1
    fluent = (
        await db_session.execute(
            select(FluentAssertion).where(
                FluentAssertion.campaign_id == campaign.id,
                FluentAssertion.is_current.is_(True),
            )
        )
    ).scalar_one()
    assert fluent.subject_entity_id == entity.id
    assert json.loads(fluent.value_json) == "wet from rain"

    assistant_row = await db_session.get(Turn, assistant.id)
    snapshot = json.loads(assistant_row.context_snapshot)
    audit = snapshot[SemanticResidualWriterService.SNAPSHOT_KEY]
    assert audit["mode"] == "writer"
    assert audit["source_user_turn_id"] == user.id
    assert len(audit["event_ids"]["fluents"]) == 1
    assert audit["event_ids"]["relations"] == []


@pytest.mark.asyncio
async def test_writer_refuses_objective_semantics_for_actor_scoped_dialogue(db_session):
    campaign, entity, _user, assistant = await _turn_pair(db_session, actor_scoped=True)
    extractor = StubExtractor(_envelope())
    writer = SemanticResidualWriterService(
        db_session,
        extractor=extractor,
        entity_resolver=StubJointEntityResolver(UUID(entity.id)),
        semantic_resolver=StubSemanticResolver(),
    )

    assert await writer.write(UUID(assistant.id)) is False
    assert extractor.calls == 0
    count = (
        await db_session.execute(
            select(func.count(TruthEventRecord.event_id)).where(
                TruthEventRecord.campaign_id == campaign.id
            )
        )
    ).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_writer_activity_barrier_prevents_any_publish_after_undo_wins(db_session):
    campaign, entity, _user, assistant = await _turn_pair(db_session)
    extractor = StubExtractor(_envelope())
    entity_resolver = StubJointEntityResolver(UUID(entity.id))
    semantic_resolver = StubSemanticResolver()
    writer = SemanticResidualWriterService(
        db_session,
        extractor=extractor,
        context_reader=InactiveBarrierReader(db_session),
        entity_resolver=entity_resolver,
        semantic_resolver=semantic_resolver,
    )

    assert await writer.write(UUID(assistant.id)) is False
    assert extractor.calls == 1
    assert entity_resolver.calls == 1
    # The first guarded boundary is entity materialization, so semantic-slot judgement never starts.
    assert semantic_resolver.calls == 0
    event_count = (
        await db_session.execute(
            select(func.count(TruthEventRecord.event_id)).where(
                TruthEventRecord.campaign_id == campaign.id
            )
        )
    ).scalar_one()
    semantic_type_count = (
        await db_session.execute(
            select(func.count(SemanticType.id)).where(
                SemanticType.campaign_id == campaign.id
            )
        )
    ).scalar_one()
    assert event_count == 0
    assert semantic_type_count == 0
