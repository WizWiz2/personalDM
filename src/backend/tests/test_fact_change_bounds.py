"""apply_change cardinality, scene bounds and operation semantics, observed via current facts."""
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.fact_repo import FactRepository
from app.db.tables import Fact
from app.models.campaign import CampaignCreate
from app.models.fact import FactCreate

ENTITY = uuid4()


async def _repo(session):
    cid = uuid4()
    await CampaignRepository(session).create(cid, CampaignCreate(name="bounds"))
    return cid, FactRepository(session)


def _fact(value, *, predicate="known_feature", scene=None, subject="маяк", entity=ENTITY):
    return FactCreate(subject=subject, predicate=predicate, object_value=value, subject_entity_id=entity,
                      scope="scene" if scene else "campaign", scene_id=scene)


async def _current(repo, cid):
    return sorted((f.object_value, f.scene_id) for f in await repo.list_active(cid))


@pytest.mark.asyncio
async def test_multi_value_revision_stays_inside_its_scene(db_session):
    cid, repo = await _repo(db_session)
    a, b = uuid4(), uuid4()
    await repo.apply_change(cid, _fact("трещина", scene=a))
    await repo.apply_change(cid, _fact("трещина", scene=b))
    await repo.apply_change(cid, _fact("заделана", scene=b), operation="revise", previous_object_value="трещина")
    assert await _current(repo, cid) == sorted([("трещина", a), ("заделана", b)])


@pytest.mark.asyncio
async def test_single_value_supersedes_across_scenes(db_session):
    cid, repo = await _repo(db_session)
    await repo.apply_change(cid, _fact("Зал", predicate="location", scene=uuid4()))
    await repo.apply_change(cid, _fact("Пристань", predicate="location"))
    assert [v for v, _ in await _current(repo, cid)] == ["Пристань"]


@pytest.mark.asyncio
async def test_multi_values_coexist_and_revise_replaces_only_the_named_one(db_session):
    cid, repo = await _repo(db_session)
    await repo.apply_change(cid, _fact("высокий"))
    await repo.apply_change(cid, _fact("старый"))
    revised = await repo.apply_change(cid, _fact("отремонтированный"), operation="revise",
                                      previous_object_value="старый")
    assert [v for v, _ in await _current(repo, cid)] == ["высокий", "отремонтированный"]
    old = (await db_session.execute(select(Fact).where(Fact.object_value == "старый"))).scalar_one()
    assert old.is_current is False and old.superseded_by == str(revised.id)


@pytest.mark.asyncio
async def test_revise_without_previous_value_keeps_existing_multi_values(db_session):
    cid, repo = await _repo(db_session)
    await repo.apply_change(cid, _fact("высокий"))
    await repo.apply_change(cid, _fact("старый"), operation="revise")
    assert [v for v, _ in await _current(repo, cid)] == ["высокий", "старый"]


@pytest.mark.asyncio
async def test_retract_multi_removes_only_the_named_value(db_session):
    cid, repo = await _repo(db_session)
    await repo.apply_change(cid, _fact("высокий"))
    await repo.apply_change(cid, _fact("старый"))
    assert await repo.apply_change(cid, _fact("старый"), operation="retract") is None
    assert [v for v, _ in await _current(repo, cid)] == ["высокий"]


@pytest.mark.asyncio
async def test_retract_single_removes_the_current_value_whatever_it_is(db_session):
    cid, repo = await _repo(db_session)
    await repo.apply_change(cid, _fact("открыта", predicate="access_state"))
    await repo.apply_change(cid, _fact("заперта", predicate="access_state"), operation="retract")
    assert await _current(repo, cid) == []


@pytest.mark.asyncio
async def test_unknown_operation_is_treated_as_assert(db_session):
    """An unknown operation never retracts or duplicates; it degrades to an idempotent assert."""
    cid, repo = await _repo(db_session)
    first = await repo.apply_change(cid, _fact("высокий"))
    again = await repo.apply_change(cid, _fact("высокий"), operation="erase")
    assert again is not None and again.id == first.id
    assert [v for v, _ in await _current(repo, cid)] == ["высокий"]


@pytest.mark.asyncio
async def test_other_entity_with_same_text_subject_is_untouched(db_session):
    cid, repo = await _repo(db_session)
    other = uuid4()
    await repo.apply_change(cid, _fact("Зал", predicate="location", entity=other))
    await repo.apply_change(cid, _fact("Пристань", predicate="location"))
    assert [v for v, _ in await _current(repo, cid)] == ["Зал", "Пристань"]
