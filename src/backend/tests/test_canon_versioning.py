from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.belief_repo import BeliefRepository
from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.fact_repo import FactRepository
from app.db.repositories.relationship_repo import RelationshipRepository
from app.models.belief import BeliefCreate
from app.models.campaign import CampaignCreate
from app.models.entity import EntityCreate, EntityType
from app.models.fact import FactCreate
from app.models.relationship import RelationshipCreate


@pytest.mark.asyncio
async def test_single_fact_revises_old_value_and_exact_assert_is_noop(
    db_session: AsyncSession,
):
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(
        campaign_id,
        CampaignCreate(name="Fact Versions"),
    )
    repo = FactRepository(db_session)

    first = await repo.apply_change(
        campaign_id,
        FactCreate(subject="ворота", predicate="состояние", object_value="закрыты"),
        operation="assert",
        cardinality="single",
    )
    duplicate = await repo.apply_change(
        campaign_id,
        FactCreate(subject=" Ворота ", predicate="Состояние", object_value="закрыты"),
        operation="assert",
        cardinality="single",
    )
    assert duplicate.id == first.id
    assert len(await repo.list_active(campaign_id)) == 1

    second = await repo.apply_change(
        campaign_id,
        FactCreate(subject="ворота", predicate="состояние", object_value="открыты"),
        operation="revise",
        cardinality="single",
    )
    active = await repo.list_active(campaign_id)
    old = await repo.get_by_id(first.id)
    assert [item.object_value for item in active] == ["открыты"]
    assert old.is_current is False
    assert old.superseded_by == second.id


@pytest.mark.asyncio
async def test_multi_fact_keeps_multiple_values_and_retracts_one(
    db_session: AsyncSession,
):
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(
        campaign_id,
        CampaignCreate(name="Multi Facts"),
    )
    repo = FactRepository(db_session)
    await repo.apply_change(
        campaign_id,
        FactCreate(subject="Элдон", predicate="имеет_улику", object_value="карта"),
        cardinality="multi",
    )
    await repo.apply_change(
        campaign_id,
        FactCreate(subject="Элдон", predicate="имеет_улику", object_value="печать"),
        cardinality="multi",
    )
    assert {item.object_value for item in await repo.list_active(campaign_id)} == {
        "карта",
        "печать",
    }

    await repo.apply_change(
        campaign_id,
        FactCreate(subject="Элдон", predicate="имеет_улику", object_value="карта"),
        operation="retract",
        cardinality="multi",
    )
    assert [item.object_value for item in await repo.list_active(campaign_id)] == ["печать"]


@pytest.mark.asyncio
async def test_belief_correction_supersedes_previous_proposition(
    db_session: AsyncSession,
):
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(
        campaign_id,
        CampaignCreate(name="Belief Versions"),
    )
    entity = await EntityRepository(db_session).create(
        campaign_id,
        EntityCreate(entity_type=EntityType.CHARACTER, canonical_name="Элдон"),
    )
    repo = BeliefRepository(db_session)
    first = await repo.apply_change(
        BeliefCreate(
            character_id=entity.id,
            proposition="Западный проход безопасен.",
            status="believed",
        )
    )
    second = await repo.apply_change(
        BeliefCreate(
            character_id=entity.id,
            proposition="Западный проход заминирован.",
            status="known",
        ),
        operation="contradict",
        previous_proposition="Западный проход безопасен.",
    )
    old = await repo.get_by_id(first.id)
    current = await repo.get_for_character(entity.id)
    assert [item.id for item in current] == [second.id]
    assert old.is_current is False
    assert old.superseded_by == second.id


@pytest.mark.asyncio
async def test_relationship_revision_keeps_one_current_assertion(
    db_session: AsyncSession,
):
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(
        campaign_id,
        CampaignCreate(name="Relationship Versions"),
    )
    entities = EntityRepository(db_session)
    eldon = await entities.create(
        campaign_id,
        EntityCreate(entity_type=EntityType.CHARACTER, canonical_name="Элдон"),
    )
    garrick = await entities.create(
        campaign_id,
        EntityCreate(entity_type=EntityType.CHARACTER, canonical_name="Гаррик"),
    )
    repo = RelationshipRepository(db_session)
    first = await repo.apply_change(
        campaign_id,
        RelationshipCreate(
            subject_id=eldon.id,
            object_id=garrick.id,
            relation_type="доверие",
            description="Элдон осторожно доверяет Гаррику.",
            intensity=0.2,
        ),
    )
    second = await repo.apply_change(
        campaign_id,
        RelationshipCreate(
            subject_id=eldon.id,
            object_id=garrick.id,
            relation_type="доверие",
            description="Элдон доверяет Гаррику после спасения.",
            intensity=0.8,
        ),
        operation="revise",
    )
    old = await repo.get_by_id(first.id)
    current = await repo.get_for_character(eldon.id, object_ids=[garrick.id])
    assert [item.id for item in current] == [second.id]
    assert old.is_current is False
    assert old.superseded_by == second.id
