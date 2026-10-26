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
async def test_scene_state_allows_explicit_multivalued_observations_and_targeted_revision(
    db_session: AsyncSession,
):
    """Scene-state alone cannot assert single cardinality for an untyped subject.
    Distinct values may coexist; a revision replaces only the specified old one."""
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(
        campaign_id, CampaignCreate(name="Multivalued scene observations")
    )
    repo, scene_id = FactRepository(db_session), uuid4()

    async def where(value, *, operation="assert", previous=None):
        return await repo.apply_change(
            campaign_id,
            FactCreate(
                subject="кровь", predicate="находится", object_value=value,
                memory_kind="scene_state", scene_id=scene_id,
            ),
            operation=operation, cardinality="multi",
            previous_object_value=previous,
        )

    first = await where("у порога")
    second = await where("на лестнице")
    assert {fact.object_value for fact in await repo.list_active(campaign_id)} == {
        "у порога", "на лестнице",
    }

    correction = await where("на ступенях", operation="revise", previous="на лестнице")
    assert (await repo.get_by_id(first.id)).is_current is True
    assert (await repo.get_by_id(second.id)).superseded_by == correction.id
    assert {fact.object_value for fact in await repo.list_active(campaign_id)} == {
        "у порога", "на ступенях",
    }


@pytest.mark.asyncio
async def test_scene_state_preserves_distinct_predicates_and_resolves_entity_aliases(
    db_session: AsyncSession,
):
    """A typed entity can carry multiple scene properties; only the same predicate
    is replaced, even if later prose calls the entity by another name."""
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(
        campaign_id, CampaignCreate(name="Scene state properties")
    )
    attendant = await EntityRepository(db_session).create(
        campaign_id, EntityCreate(
            entity_type=EntityType.CHARACTER, canonical_name="Служащий трактира"
        )
    )
    repo, scene_id = FactRepository(db_session), uuid4()

    async def state(subject, predicate, value):
        return await repo.apply_change(campaign_id, FactCreate(
            subject=subject, predicate=predicate, object_value=value,
            memory_kind="scene_state", scene_id=scene_id,
            subject_entity_id=attendant.id,
        ))

    first = await state("Служащий трактира", "держит", "книгу")
    location = await state("служащий", "настроение", "тревожное")
    updated = await state("служащий", "держит", "фонарь")

    assert (await repo.get_by_id(first.id)).is_current is False
    assert (await repo.get_by_id(first.id)).superseded_by == updated.id
    assert (await repo.get_by_id(location.id)).is_current is True
    active = await repo.list_active(campaign_id)
    assert {(fact.predicate, fact.object_value) for fact in active} == {
        ("держит", "фонарь"), ("настроение", "тревожное"),
    }


@pytest.mark.asyncio
async def test_same_label_different_typed_objects_are_not_superseded(
    db_session: AsyncSession,
):
    """Two distinct physical boats may share a narration label and predicate."""
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(
        campaign_id, CampaignCreate(name="Two boats")
    )
    entities = EntityRepository(db_session)
    boat_a = await entities.create(
        campaign_id, EntityCreate(
            entity_type=EntityType.ITEM, canonical_name="Лодка с красным парусом",
        ),
    )
    boat_b = await entities.create(
        campaign_id, EntityCreate(
            entity_type=EntityType.ITEM, canonical_name="Лодка с белым парусом",
        ),
    )
    repo, scene_id = FactRepository(db_session), uuid4()

    async def boat(entity_id, name, value):
        return await repo.apply_change(campaign_id, FactCreate(
            subject=name, predicate="местоположение", object_value=value,
            scene_id=scene_id, memory_kind="scene_state",
            subject_entity_id=entity_id,
        ))

    old_a = await boat(boat_a.id, "лодка", "у причала")
    old_b = await boat(boat_b.id, "лодка", "у мыса")
    assert (await repo.get_by_id(old_a.id)).is_current is True
    assert (await repo.get_by_id(old_b.id)).is_current is True

    new_a = await boat(boat_a.id, "первая лодка", "у скал")
    assert (await repo.get_by_id(old_a.id)).superseded_by == new_a.id
    assert (await repo.get_by_id(old_b.id)).is_current is True
    assert {
        (fact.subject_entity_id, fact.object_value)
        for fact in await repo.list_active(campaign_id)
    } == {(boat_a.id, "у скал"), (boat_b.id, "у мыса")}


@pytest.mark.asyncio
async def test_untyped_scene_assertion_does_not_revise_a_typed_object(
    db_session: AsyncSession,
):
    """An ambiguous prose label must not silently overwrite known entity state."""
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(
        campaign_id, CampaignCreate(name="Typed and untyped")
    )
    boat = await EntityRepository(db_session).create(
        campaign_id, EntityCreate(
            entity_type=EntityType.ITEM, canonical_name="Сивая чайка",
        ),
    )
    repo, scene_id = FactRepository(db_session), uuid4()
    typed = await repo.apply_change(campaign_id, FactCreate(
        subject="лодка", predicate="состояние", object_value="опечатана",
        scene_id=scene_id, memory_kind="scene_state",
        subject_entity_id=boat.id,
    ))
    untyped = await repo.apply_change(campaign_id, FactCreate(
        subject="лодка", predicate="состояние", object_value="на плаву",
        scene_id=scene_id, memory_kind="scene_state",
    ))
    assert (await repo.get_by_id(typed.id)).is_current is True
    assert (await repo.get_by_id(untyped.id)).is_current is True


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
