"""Prepare-phase NPC/item/place are journaled durably and compensated through one path,
whether the saga fails in-process or the server died after PREPARED."""
import pytest
from sqlalchemy import select

from app.db.generation_lifecycle_table import GenerationLifecycle, ProvisionalTurnEffect
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.generation_lifecycle_repo import GenerationLifecycleRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import Entity, GenerationRun, Item, SceneParticipant, Turn
from app.models.character import CharacterCreate
from app.models.entity import EntityCreate, EntityType
from app.models.jobs import GenerationPhase
from app.models.location import LocationCreate
from app.services.generation_startup_recovery import recover_interrupted_generations
from app.services.turn_outcome_materializer import MaterializedTurnOutcome, TurnOutcomeMaterializer
from app.services.turn_saga import TurnSaga

from tests.test_turn_saga_failure_paths import _prepared_world


async def _prepare_with_new_world(file_db):
    """Commit the PREPARED boundary with a new NPC (in scene), item (placed) and place."""
    async with file_db() as session:
        w = await _prepared_world(session)
        entities = EntityRepository(session)
        npc = await entities.create_character(w.cid, CharacterCreate(canonical_name="Сторож"))
        await SceneRepository(session).add_participant(w.scene.id, npc.id, allow_movement=True)
        item = await entities.create(w.cid, EntityCreate(entity_type=EntityType.ITEM, canonical_name="Фонарь"))
        session.add(Item(entity_id=str(item.id), current_location_id=str(w.hall.id), is_unique=True))
        place = await LocationRepository(session).create(w.cid, LocationCreate(canonical_name="Погреб"))
        await TurnOutcomeMaterializer(session)._journal(w.turn.id, MaterializedTurnOutcome(
            introduced_character_ids=(npc.id,), introduced_item_ids=(item.id,), introduced_place_ids=(place.id,)))
        await GenerationLifecycleRepository(session).set_phase(w.run.id, GenerationPhase.PREPARED)
        await session.commit()
    return w, npc.id, item.id, place.id


async def _assert_rolled_back(file_db, w, npc_id, item_id, place_id):
    async with file_db() as other:
        assert await other.get(Entity, str(npc_id)) is None
        assert await other.get(Entity, str(item_id)) is None
        assert await other.get(Item, str(item_id)) is None
        assert (await other.get(Entity, str(place_id))).status == "inactive"
        dangling = (await other.execute(select(SceneParticipant.entity_id).where(
            SceneParticipant.entity_id.in_((str(npc_id), str(item_id)))))).scalars().all()
        assert dangling == []
        statuses = (await other.execute(select(ProvisionalTurnEffect.status).where(
            ProvisionalTurnEffect.trigger_turn_id == str(w.turn.id)))).scalars().all()
        assert statuses == ["rolled_back"] * 3
        assert (await other.get(GenerationLifecycle, str(w.run.id))).phase == GenerationPhase.COMPENSATED.value


@pytest.mark.asyncio
async def test_crash_after_prepared_is_rolled_back_on_startup_and_recovery_is_idempotent(file_db):
    w, *ids = await _prepare_with_new_world(file_db)  # process dies here: run stays running
    async with file_db() as session:
        assert await recover_interrupted_generations(session) == 1
    await _assert_rolled_back(file_db, w, *ids)
    async with file_db() as session:
        assert await recover_interrupted_generations(session) == 0
    await _assert_rolled_back(file_db, w, *ids)
    async with file_db() as other:
        assert (await other.get(GenerationRun, str(w.run.id))).status == "failed"
        assert (await other.get(Turn, str(w.turn.id))).status == "failed"


@pytest.mark.asyncio
async def test_in_process_failure_uses_the_same_compensation(file_db):
    w, *ids = await _prepare_with_new_world(file_db)
    async with file_db() as session:
        await TurnSaga(session)._compensate(w.run.id, w.turn.id, True, w.cid)
    await _assert_rolled_back(file_db, w, *ids)


@pytest.mark.asyncio
async def test_published_turn_confirms_its_effects_and_compensation_keeps_them(file_db):
    w, npc_id, item_id, place_id = await _prepare_with_new_world(file_db)
    async with file_db() as session:
        await TurnOutcomeMaterializer(session).bind_to_assistant(w.turn.id, w.turn.id)
        await session.commit()
    async with file_db() as session:
        await TurnOutcomeMaterializer(session).rollback(w.turn.id)
        await session.commit()
    async with file_db() as other:
        assert await other.get(Entity, str(npc_id)) is not None
        assert await other.get(Item, str(item_id)) is not None
        assert (await other.get(Entity, str(place_id))).status != "inactive"
        statuses = (await other.execute(select(ProvisionalTurnEffect.status))).scalars().all()
        assert statuses == ["confirmed"] * 3
