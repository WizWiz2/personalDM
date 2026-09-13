from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.db.tables import Campaign, Entity, Event, Turn
from app.db.truth_engine_table import FluentAssertion, TruthEventRecord
from app.models.truth_engine import (
    EntityResolutionDecision,
    NewSemanticTypeDraft,
    SemanticTypeResolutionDecision,
)
from app.models.truth_engine_residual import (
    ResidualAtomDisposition,
    ResidualClassificationResult,
    ResidualEntityMention,
    ResidualFluentObservation,
    SemanticResidualEnvelope,
    objective_residual,
)
from app.services.truth_engine_writer import SemanticResidualWriterService
from app.services.turn_undo_service import TurnUndoService


class _Extractor:
    async def extract(self, *args, **kwargs):
        return SemanticResidualEnvelope(
            entities=[
                ResidualEntityMention(
                    ref="keeper",
                    mention_text="хранитель",
                    entity_type="character",
                    evidence="Хранитель промок под дождём.",
                )
            ],
            fluents=[
                ResidualFluentObservation(
                    atom_key="condition",
                    subject_ref="keeper",
                    semantic_description="currently established visible physical condition",
                    value="wet from rain",
                    description="The keeper is visibly wet from rain.",
                    evidence="Хранитель промок под дождём.",
                    cardinality_hint="single",
                )
            ],
        )


class _ObjectiveClassifier:
    async def classify(self, campaign_id, *, envelope, **kwargs):
        decisions = [
            ResidualAtomDisposition(atom_key=atom.atom_key, disposition="objective")
            for atom in [*envelope.fluents, *envelope.relations]
        ]
        return ResidualClassificationResult(
            decisions=decisions,
            objective=objective_residual(envelope, decisions),
        )


class _EntityResolver:
    def __init__(self, entity_id: UUID):
        self.entity_id = entity_id

    async def resolve(self, campaign_id, observations, *, local_graph=None):
        return {
            observation.observation_key: EntityResolutionDecision(
                decision="existing",
                entity_id=self.entity_id,
            )
            for observation in observations
        }


class _SemanticResolver:
    async def resolve_semantic_type(self, *args, **kwargs):
        return (
            SemanticTypeResolutionDecision(
                decision="new",
                new_type=NewSemanticTypeDraft(
                    canonical_label="Current visible condition",
                    description="The entity's currently established visible physical condition.",
                    cardinality="single",
                    value_schema={"type": "string"},
                ),
            ),
            [],
        )


@pytest.mark.asyncio
async def test_turn_undo_reverts_semantic_writer_history_and_removes_projection(db_session):
    campaign = Campaign(name="TE2 semantic writer undo")
    db_session.add(campaign)
    await db_session.flush()
    keeper = Entity(
        campaign_id=campaign.id,
        entity_type="character",
        canonical_name="Keeper",
    )
    db_session.add(keeper)
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
        content="Хранитель промок под дождём.",
        parent_turn_id=user.id,
        status="active",
        context_snapshot="{}",
    )
    db_session.add(assistant)
    await db_session.flush()
    campaign_id = UUID(campaign.id)
    campaign_key = campaign.id
    keeper_id = UUID(keeper.id)
    keeper_key = keeper.id
    user_key = user.id
    assistant_id = UUID(assistant.id)
    await db_session.commit()

    writer = SemanticResidualWriterService(
        db_session,
        extractor=_Extractor(),
        classifier=_ObjectiveClassifier(),
        entity_resolver=_EntityResolver(keeper_id),
        semantic_resolver=_SemanticResolver(),
    )
    assert await writer.write(assistant_id) is True

    before_records = list(
        (
            await db_session.execute(
                select(TruthEventRecord)
                .where(TruthEventRecord.campaign_id == campaign_key)
                .order_by(TruthEventRecord.sequence)
            )
        ).scalars().all()
    )
    assert len(before_records) == 2
    assert {record.source_turn_id for record in before_records} == {user_key}
    assert {record.status for record in before_records} == {"active"}
    current_before = (
        await db_session.execute(
            select(func.count(FluentAssertion.id)).where(
                FluentAssertion.campaign_id == campaign_key,
                FluentAssertion.subject_entity_id == keeper_key,
                FluentAssertion.is_current.is_(True),
            )
        )
    ).scalar_one()
    assert current_before == 1

    assert await TurnUndoService(db_session).undo_last_pair(campaign_id) is True

    after_records = list(
        (
            await db_session.execute(
                select(TruthEventRecord)
                .where(TruthEventRecord.campaign_id == campaign_key)
                .order_by(TruthEventRecord.sequence)
            )
        ).scalars().all()
    )
    assert [record.event_id for record in after_records] == [
        record.event_id for record in before_records
    ]
    assert {record.status for record in after_records} == {"reverted"}
    assert {record.source_turn_id for record in after_records} == {user_key}

    current_after = (
        await db_session.execute(
            select(func.count(FluentAssertion.id)).where(
                FluentAssertion.campaign_id == campaign_key,
                FluentAssertion.subject_entity_id == keeper_key,
                FluentAssertion.is_current.is_(True),
            )
        )
    ).scalar_one()
    assert current_after == 0

    # Undo changes inclusion/projection only; immutable canonical history stays queryable.
    event_count = (
        await db_session.execute(
            select(func.count(Event.id)).where(
                Event.id.in_([record.event_id for record in after_records])
            )
        )
    ).scalar_one()
    assert event_count == 2
