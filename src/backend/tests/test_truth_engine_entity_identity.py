from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.db.tables import Campaign, Entity, Turn
from app.db.truth_engine_table import EntityMention, TruthEventRecord
from app.models.truth_engine import EntityMentionObservation, EntityResolutionDecision
from app.services.truth_engine import CanonicalEventStore, WorldReducer
from app.services.truth_engine_semantics import SemanticObservationCompiler, TruthCandidateRetriever


class StubEntityResolver:
    def __init__(self, *decisions: EntityResolutionDecision):
        self.decisions = list(decisions)

    async def resolve_entity(self, *args, **kwargs) -> EntityResolutionDecision:
        return self.decisions.pop(0)


async def _campaign(db_session) -> UUID:
    campaign = Campaign(name="TE2 entity identity")
    db_session.add(campaign)
    await db_session.flush()
    return UUID(campaign.id)


@pytest.mark.asyncio
async def test_distinct_entities_may_share_same_human_label_without_identity_suffix(db_session):
    campaign_id = await _campaign(db_session)
    compiler = SemanticObservationCompiler(
        db_session,
        resolver=StubEntityResolver(
            EntityResolutionDecision(decision="new"),
            EntityResolutionDecision(decision="new"),
        ),
    )

    first_id = await compiler.compile_entity_reference(
        campaign_id,
        EntityMentionObservation(
            observation_key="guard-at-west-door",
            mention_text="Guard",
            entity_type="character",
            description="The guard currently standing at the west door.",
            evidence="A guard is standing at the west door.",
        ),
    )
    second_id = await compiler.compile_entity_reference(
        campaign_id,
        EntityMentionObservation(
            observation_key="guard-at-east-door",
            mention_text="Guard",
            entity_type="character",
            description="A different guard currently standing at the east door.",
            evidence="Another guard is standing at the east door.",
        ),
    )

    assert first_id != second_id
    entities = list(
        (
            await db_session.execute(
                select(Entity)
                .where(
                    Entity.campaign_id == str(campaign_id),
                    Entity.entity_type == "character",
                    Entity.canonical_name == "Guard",
                )
                .order_by(Entity.id)
            )
        ).scalars().all()
    )
    assert len(entities) == 2
    assert {UUID(row.id) for row in entities} == {first_id, second_id}
    assert {row.provenance for row in entities} == {"truth_engine"}

    mentions = list(
        (
            await db_session.execute(
                select(EntityMention).where(EntityMention.campaign_id == str(campaign_id))
            )
        ).scalars().all()
    )
    assert len(mentions) == 2
    assert {UUID(row.entity_id) for row in mentions} == {first_id, second_id}
    assert {row.mention_text for row in mentions} == {"Guard"}


@pytest.mark.asyncio
async def test_entity_observation_retry_returns_original_uuid_and_does_not_duplicate_registry(db_session):
    campaign_id = await _campaign(db_session)
    compiler = SemanticObservationCompiler(
        db_session,
        resolver=StubEntityResolver(EntityResolutionDecision(decision="new")),
    )
    observation = EntityMentionObservation(
        observation_key="stable-retry-key",
        mention_text="Unknown courier",
        entity_type="character",
        evidence="An unknown courier enters the room.",
    )

    first_id = await compiler.compile_entity_reference(campaign_id, observation)
    second_id = await compiler.compile_entity_reference(campaign_id, observation)

    assert second_id == first_id
    entity_count = (
        await db_session.execute(
            select(func.count(Entity.id)).where(
                Entity.campaign_id == str(campaign_id),
                Entity.provenance == "truth_engine",
            )
        )
    ).scalar_one()
    mention_count = (
        await db_session.execute(
            select(func.count(EntityMention.id)).where(
                EntityMention.campaign_id == str(campaign_id),
                EntityMention.entity_id == str(first_id),
            )
        )
    ).scalar_one()
    event_count = (
        await db_session.execute(
            select(func.count(TruthEventRecord.event_id)).where(
                TruthEventRecord.campaign_id == str(campaign_id),
                TruthEventRecord.event_key == "semantic_entity:stable-retry-key",
            )
        )
    ).scalar_one()
    assert entity_count == 1
    assert mention_count == 1
    assert event_count == 1


@pytest.mark.asyncio
async def test_reverted_origin_mention_hides_te2_registry_shell_from_future_resolution(db_session):
    campaign_id = await _campaign(db_session)
    user_turn = Turn(
        campaign_id=str(campaign_id),
        role="user",
        content="I look at the stranger by the gate.",
    )
    db_session.add(user_turn)
    await db_session.flush()

    compiler = SemanticObservationCompiler(
        db_session,
        resolver=StubEntityResolver(EntityResolutionDecision(decision="new")),
    )
    entity_id = await compiler.compile_entity_reference(
        campaign_id,
        EntityMentionObservation(
            observation_key="undoable-stranger",
            mention_text="Stranger",
            entity_type="character",
            source_turn_id=UUID(user_turn.id),
            evidence="A stranger is visible by the gate.",
        ),
    )

    before = await TruthCandidateRetriever(db_session).entity_candidates(
        campaign_id,
        expected_types=["character"],
    )
    assert entity_id in {candidate.entity_id for candidate in before}

    changed = await CanonicalEventStore(db_session).set_turn_status(
        campaign_id,
        UUID(user_turn.id),
        active=False,
    )
    assert changed == 1
    await WorldReducer(db_session).rebuild(campaign_id)

    # The stable registry UUID remains for referential/history safety, but its only active-world
    # support was reverted, so the semantic resolver cannot accidentally resurrect it.
    registry_row = await db_session.get(Entity, str(entity_id))
    assert registry_row is not None
    assert registry_row.provenance == "truth_engine"
    remaining_mentions = (
        await db_session.execute(
            select(func.count(EntityMention.id)).where(
                EntityMention.campaign_id == str(campaign_id),
                EntityMention.entity_id == str(entity_id),
            )
        )
    ).scalar_one()
    assert remaining_mentions == 0

    after = await TruthCandidateRetriever(db_session).entity_candidates(
        campaign_id,
        expected_types=["character"],
    )
    assert entity_id not in {candidate.entity_id for candidate in after}
