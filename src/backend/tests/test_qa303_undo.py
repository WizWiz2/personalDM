"""QA-302 regressions: undo."""
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.event_repo import EventRepository
from app.db.repositories.turn_repo import TurnRepository
from app.models.event import EventCreate
from app.models.truth_engine import CanonicalEventCreate
from app.models.turn import TurnCreate
from app.services.truth_engine import CanonicalEventStore
from app.services.turn_undo_service import TurnUndoService
from tests.test_qa219_regressions import _world


@pytest.mark.asyncio
async def test_undo_hides_legacy_source_events_from_active_history_but_keeps_audit(db_session):
    cid, _, _, _, _, _, scene = await _world(db_session)
    turns = TurnRepository(db_session)
    user = await turns.create(cid, TurnCreate(role="user", content="Кладу акт на перила.", scene_id=scene))
    assistant = await turns.create(cid, TurnCreate(role="assistant", content="Акт лежит на перилах.",
        scene_id=scene, parent_turn_id=user.id))
    store = CanonicalEventStore(db_session)
    event = await store.append(cid, CanonicalEventCreate(event_key="test:transfer", event_type="item_transfer",
        description="Акт перенесён из рук на перила.", source_turn_id=assistant.id, source_kind="engine"))
    independent = await store.append(cid, CanonicalEventCreate(event_key="test:weather", event_type="weather",
        description="Идёт дождь.", source_kind="gm"))
    await db_session.commit()
    assert await TurnUndoService(db_session).undo_last_pair(cid)
    await db_session.commit()
    async with AsyncSession(db_session.bind) as reader:
        active = await EventRepository(reader).list_by_campaign(cid)
        assert event.event_id not in {e.id for e in active}
        assert independent.event_id in {e.id for e in active}
        historical = await EventRepository(reader).get_by_id(event.event_id)
        assert historical is not None and historical.status != "active"
        assert (await CanonicalEventStore(reader).get(event.event_id)).status != "active"
        from app.services.debugger_service import DebuggerService
        snapshot = await DebuggerService(reader).snapshot(cid)
        assert next(e for e in snapshot["events"] if e["id"] == str(event.event_id))["status"] != "active"



@pytest.mark.asyncio
async def test_source_status_also_filters_events_without_a_truth_record(db_session):
    cid, _, _, _, _, _, scene = await _world(db_session)
    turns, events = TurnRepository(db_session), EventRepository(db_session)
    source = await turns.create(cid, TurnCreate(role="assistant", content="Старое событие.", scene_id=scene))
    event = await events.create(cid, EventCreate(event_type="item_transfer", description="Старое перемещение.",
                                               source_turns=[source.id]))
    await turns.mark_undone(source.id)
    assert not await events.list_by_campaign(cid)
    assert (await events.get_by_id(event.id)).status == "undone"



@pytest.mark.asyncio
async def test_location_catalog_uses_latest_active_opening_not_undone_prose(db_session):
    from app.services.turn_intent_pipeline import TurnIntentPlanningPipeline

    cid, room, _, _, _, _, scene = await _world(db_session)
    turns = TurnRepository(db_session)
    await turns.create(cid, TurnCreate(role="assistant", content="Первое старое описание.", scene_id=scene))
    await turns.create(cid, TurnCreate(role="assistant", content="Акт находится в руках.", scene_id=scene))
    cancelled = await turns.create(cid, TurnCreate(role="assistant", content="Акт на перилах.", scene_id=scene))
    await turns.mark_undone(cancelled.id)
    _, catalog = await TurnIntentPlanningPipeline(db_session, None)._location_catalog(cid)
    assert catalog[str(room)]["scene_opening"] == "Акт находится в руках."

