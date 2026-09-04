from __future__ import annotations

from uuid import UUID

import pytest

from app.db.tables import Campaign, Entity
from app.db.truth_engine_table import SemanticType
from app.models.truth_engine import SemanticTypeCreate, SemanticTypeResolutionDecision
from app.services.truth_engine import SemanticTypeRegistry
from app.services.truth_engine_protected_semantics import ProtectedAwareSemanticResolver
from app.services.truth_engine_semantics import TruthCandidateRetriever


@pytest.mark.asyncio
async def test_system_semantic_types_are_hidden_normally_but_preserved_as_collision_candidates(
    db_session,
):
    campaign = Campaign(name="TE2 protected semantic retrieval")
    db_session.add(campaign)
    await db_session.flush()
    subject = Entity(
        campaign_id=campaign.id,
        entity_type="character",
        canonical_name="Hero",
    )
    db_session.add(subject)
    await db_session.flush()
    campaign_id = UUID(campaign.id)
    subject_id = UUID(subject.id)

    dynamic_id = await SemanticTypeRegistry(db_session).create(
        campaign_id,
        SemanticTypeCreate(
            kind="fluent",
            canonical_label="Visible condition",
            description="An open-ended visible condition.",
            cardinality="single",
        ),
    )
    core = SemanticType(
        campaign_id=campaign.id,
        system_key="core.entity.location",
        kind="fluent",
        canonical_label="Entity location",
        description="Engine-owned current entity location.",
        cardinality="single",
    )
    db_session.add(core)
    await db_session.flush()
    core_id = UUID(core.id)

    retriever = TruthCandidateRetriever(db_session)
    open_candidates = await retriever.semantic_type_candidates(
        campaign_id,
        kind="fluent",
        subject_entity_id=subject_id,
    )
    collision_candidates = await retriever.semantic_type_candidates(
        campaign_id,
        kind="fluent",
        subject_entity_id=subject_id,
        include_system_types=True,
    )

    assert dynamic_id in {candidate.semantic_type_id for candidate in open_candidates}
    assert core_id not in {candidate.semantic_type_id for candidate in open_candidates}

    by_id = {candidate.semantic_type_id: candidate for candidate in collision_candidates}
    assert by_id[core_id].system_key == "core.entity.location"
    assert by_id[dynamic_id].system_key is None

    collision = ProtectedAwareSemanticResolver.protected_collision(
        SemanticTypeResolutionDecision(
            decision="existing",
            semantic_type_id=core_id,
        ),
        collision_candidates,
    )
    assert collision is not None
    assert collision.semantic_type_id == core_id
    assert collision.system_key == "core.entity.location"
