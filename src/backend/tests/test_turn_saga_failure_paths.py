"""TurnSaga failure, cancellation and compensation, observed from a second DB session.

A second session only sees committed rows, so a dropped commit or a skipped rollback in the saga
is visible here (the shared-session tests could not see it)."""
import asyncio
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import select

from app.db.generation_lifecycle_table import GenerationLifecycle
from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.generation_lifecycle_repo import GenerationLifecycleRepository
from app.db.repositories.job_repo import GenerationRunRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.repositories.turn_repo import TurnRepository
from app.db.scene_transition_table import SceneTransition
from app.db.tables import Character, Fact, GenerationRun, Turn
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.jobs import GenerationPhase
from app.models.location import LocationCreate
from app.models.scene import SceneCreate
from app.models.scene_state import LocationExitCreate
from app.models.turn import TurnCreate
from app.services.campaign_service import CampaignService
from app.services.master_service import PENDING_DIRECTOR_FIELD, MasterService
from app.services.scene_lifecycle import SceneLifecycleService
from app.services.scene_state_service import SceneStateService
from app.services.scene_transition_executor import SceneTransitionExecutor
from app.services.turn_planner import SceneTransitionPlan
from app.services.turn_saga import TurnSaga


async def _stash_pending_rhythm(session, campaign_id):
    master = MasterService(session)
    row = await master._ensure_setup(campaign_id)
    custom = dict(master._setups.decode_dict(row.custom_fields))
    custom[PENDING_DIRECTOR_FIELD] = {"selection": None}
    await master._setups.update(row, {"custom_fields": custom})


async def _pending_rhythm(session, campaign_id):
    master = MasterService(session)
    row = await master._ensure_setup(campaign_id)
    return PENDING_DIRECTOR_FIELD in master._setups.decode_dict(row.custom_fields)


async def _run_failing_turn(session, monkeypatch, error):
    campaign = await CampaignService(session).create_campaign(CampaignCreate(name="saga failure"))

    async def plan(self, **kwargs):
        await _stash_pending_rhythm(self._session, campaign.id)
        await self._session.commit()  # PLANNED may already have committed a director selection
        raise error

    monkeypatch.setattr(TurnSaga, "_plan", plan)
    chunks = []
    try:
        async for chunk in TurnSaga(session).run_turn_stream(campaign.id, TurnCreate(role="user", content="Жду.")):
            chunks.append(chunk)
    except asyncio.CancelledError:
        chunks.append(None)
    return campaign.id, chunks


async def _committed(file_db, campaign_id):
    async with file_db() as other:
        user = (await other.execute(select(Turn).where(Turn.campaign_id == str(campaign_id),
                                                        Turn.role == "user"))).scalar_one()
        run = (await other.execute(select(GenerationRun).where(GenerationRun.user_turn_id == user.id))).scalar_one()
        return user.status, run.status, await _pending_rhythm(other, campaign_id)


@pytest.mark.asyncio
async def test_failed_plan_fails_the_turn_and_rewinds_pending_rhythm_durably(file_db, monkeypatch):
    async with file_db() as session:
        cid, chunks = await _run_failing_turn(session, monkeypatch, RuntimeError("planner exploded"))
    assert any("planner exploded" in (c or "") for c in chunks)
    assert await _committed(file_db, cid) == ("failed", "failed", False)


@pytest.mark.asyncio
async def test_cancelled_turn_is_cancelled_failed_and_re_raised(file_db, monkeypatch):
    async with file_db() as session:
        cid, chunks = await _run_failing_turn(session, monkeypatch, asyncio.CancelledError())
    assert chunks == [None]
    assert await _committed(file_db, cid) == ("failed", "cancelled", False)


async def _prepared_world(session):
    cid = (await CampaignService(session).create_campaign(CampaignCreate(name="prepared"))).id
    locations = LocationRepository(session)
    hall = await locations.create(cid, LocationCreate(canonical_name="Зал"))
    pier = await locations.create(cid, LocationCreate(canonical_name="Пристань"))
    hero = await EntityRepository(session).create_character(
        cid, CharacterCreate(canonical_name="Ирма", current_location_id=hall.id))
    await CampaignRepository(session).update(cid, CampaignUpdate(player_character_id=hero.id))
    scene = await SceneRepository(session).create(cid, SceneCreate(title="Зал", location_id=hall.id))
    await SceneLifecycleService(session).activate(cid, scene.id)
    await SceneStateService(session).create_exit(cid, hall.id, LocationExitCreate(to_location_id=pier.id, label="Пристань"))
    turn = await TurnRepository(session).create(cid, TurnCreate(role="user", content="Иду на пристань", scene_id=scene.id))
    run = await GenerationRunRepository(session).start_or_resume(cid, turn.id)
    await GenerationLifecycleRepository(session).start_attempt(run.id)
    await session.commit()
    return SimpleNamespace(cid=cid, hall=hall, pier=pier, hero=hero, scene=scene, turn=turn, run=run)


class _FactMaterializer:
    """Stands in for TurnOutcomeMaterializer: its rollback undoes one committed world fact."""

    def __init__(self, session, fact_id):
        self._session, self._fact_id = session, fact_id

    async def rollback(self, outcome):
        await self._session.delete(await self._session.get(Fact, self._fact_id))


@pytest.mark.asyncio
async def test_prepared_turn_compensation_restores_the_world_durably(file_db):
    async with file_db() as session:
        w = await _prepared_world(session)
        executor = SceneTransitionExecutor(session)
        applied = await executor.apply(w.cid, w.scene.id, w.turn.id, SceneTransitionPlan(
            required=True, transition_type="location_transition", destination_location="Пристань"))
        assert applied.target_location_id == w.pier.id
        fact = Fact(campaign_id=str(w.cid), subject="Ирма", predicate="activity", object_value="идёт")
        session.add(fact)
        await _stash_pending_rhythm(session, w.cid)
        await GenerationLifecycleRepository(session).set_phase(w.run.id, GenerationPhase.PREPARED)
        await session.commit()  # the prepare transaction is durable
        session.add(Fact(campaign_id=str(w.cid), subject="Ирма", predicate="condition", object_value="устала"))

        await TurnSaga(session)._compensate(
            w.run.id, executor, applied, _FactMaterializer(session, fact.id),
            SimpleNamespace(has_changes=True), True, campaign_id=w.cid,
        )

    async with file_db() as other:
        hero = await other.get(Character, str(w.hero.id))
        transition = await other.get(SceneTransition, str(applied.transition_id))
        lifecycle = await other.get(GenerationLifecycle, str(w.run.id))
        facts = (await other.execute(select(Fact.predicate).where(Fact.campaign_id == str(w.cid)))).scalars().all()
        assert hero.current_location_id == str(w.hall.id)
        assert transition.status == "rolled_back"
        assert lifecycle.phase == GenerationPhase.COMPENSATED.value
        assert facts == []
        assert await _pending_rhythm(other, w.cid) is False


@pytest.mark.asyncio
async def test_unprepared_compensation_discards_uncommitted_structure(file_db):
    async with file_db() as session:
        w = await _prepared_world(session)
        await SceneTransitionExecutor(session).apply(w.cid, w.scene.id, w.turn.id, SceneTransitionPlan(
            required=True, transition_type="location_transition", destination_location="Пристань"))
        await TurnSaga(session)._compensate(w.run.id, None, None, None, None, False, campaign_id=w.cid)
        await session.commit()
    async with file_db() as other:
        assert (await other.get(Character, str(w.hero.id))).current_location_id == str(w.hall.id)
        assert (await other.get(GenerationLifecycle, str(w.run.id))).phase != GenerationPhase.COMPENSATED.value
        assert UUID((await other.get(Turn, str(w.turn.id))).id) == w.turn.id
