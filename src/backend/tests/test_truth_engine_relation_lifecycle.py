from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import select

from app.db.tables import Campaign, Entity
from app.db.truth_engine_table import WorldRelationAssertion
from app.models.truth_engine import (
    RelationObservation,
    SemanticTypeCreate,
    SemanticTypeResolutionDecision,
)
from app.services.truth_engine import SemanticTypeRegistry
from app.services.truth_engine_semantics import SemanticObservationCompiler


class StubRelationResolver:
    def __init__(self, semantic_type_id: UUID):
        self.semantic_type_id = semantic_type_id

    async def resolve_semantic_type(self, *args, **kwargs):
        return (
            SemanticTypeResolutionDecision(
                decision="existing",
                semantic_type_id=self.semantic_type_id,
            ),
            [],
        )


@pytest.mark.asyncio
async def test_relation_absence_closes_same_semantic_edge_without_domain_specific_guard(db_session):
    campaign = Campaign(name="TE2 relation lifecycle")
    db_session.add(campaign)
    await db_session.flush()
    subject = Entity(
        campaign_id=campaign.id,
        entity_type="character",
        canonical_name="Hero",
    )
    object_entity = Entity(
        campaign_id=campaign.id,
        entity_type="character",
        canonical_name="Keeper",
    )
    db_session.add_all([subject, object_entity])
    await db_session.flush()
    campaign_id = UUID(campaign.id)
    subject_id = UUID(subject.id)
    object_id = UUID(object_entity.id)

    semantic_type_id = await SemanticTypeRegistry(db_session).create(
        campaign_id,
        SemanticTypeCreate(
            kind="relation",
            canonical_label="Current obligation",
            description="A currently active obligation from the subject to the target.",
            cardinality="multi",
        ),
    )
    compiler = SemanticObservationCompiler(
        db_session,
        resolver=StubRelationResolver(semantic_type_id),
    )

    created = await compiler.compile_relation(
        campaign_id,
        RelationObservation(
            observation_key="obligation-created",
            subject_entity_id=subject_id,
            object_entity_id=object_id,
            semantic_description="the obligation currently exists",
            present=True,
            description="The hero now owes the keeper.",
        ),
    )
    removed = await compiler.compile_relation(
        campaign_id,
        RelationObservation(
            observation_key="obligation-ended",
            subject_entity_id=subject_id,
            object_entity_id=object_id,
            semantic_description="the same obligation no longer exists",
            present=False,
            description="The obligation has been fully discharged.",
        ),
    )

    rows = list(
        (
            await db_session.execute(
                select(WorldRelationAssertion).where(
                    WorldRelationAssertion.campaign_id == campaign.id,
                    WorldRelationAssertion.subject_entity_id == subject.id,
                    WorldRelationAssertion.object_entity_id == object_entity.id,
                    WorldRelationAssertion.semantic_type_id == str(semantic_type_id),
                )
            )
        ).scalars().all()
    )
    assert len(rows) == 1
    relation = rows[0]
    assert relation.valid_from_event_id == str(created.event_id)
    assert relation.valid_until_event_id == str(removed.event_id)
    assert relation.is_current is False
