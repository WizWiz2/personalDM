"""QA-302 regressions: routes."""
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.turn_repo import TurnRepository
from app.db.scene_state_table import LocationExit
from app.models.narration_validation import GrantedBeat, RouteDisclosure
from app.models.player_intent import (
    ActionOutcomeDecision,
    PlayerActionIntent,
    PlayerIntentContract,
    TurnOutcomeDecision,
)
from app.models.scene_state import LocationExitCreate
from app.models.turn import TurnCreate
from app.models.turn_authority import TurnAuthority
from app.services.action_plan_compiler import ActionPlanCompiler
from app.services.action_sequence_executor import ActionSequenceExecutor
from app.services.published_routes import publish_routes, undo_published_routes
from app.services.scene_state_service import SceneStateService
from app.services.turn_outcome_materializer import TurnOutcomeMaterializer
from app.services.turn_undo_service import TurnUndoService
from tests.test_qa219_regressions import _world
from tests.test_qa223_regressions import _execute


def test_one_way_directions_require_an_explicit_public_obstacle():
    with pytest.raises(ValidationError):
        RouteDisclosure(destination=f"known:{uuid4()}", name="Контора",
                        evidence="Отсюда идите по улице до конторы.", bidirectional=False)



def test_one_way_restriction_must_be_part_of_the_disclosed_directions():
    route = RouteDisclosure(destination=f"known:{uuid4()}", name="Нижний двор",
        evidence="Во двор ведёт жёлоб, обратно по нему не подняться.", bidirectional=False,
        one_way_evidence="обратно по нему не подняться")
    assert not route.bidirectional
    with pytest.raises(ValidationError):
        route.model_copy().model_validate({**route.model_dump(), "one_way_evidence": "Мост разрушен."})



@pytest.mark.asyncio
async def test_discovered_route_does_not_reopen_an_established_return_gate(db_session):
    cid, room, elsewhere, _, _, _, _ = await _world(db_session)
    closed = LocationExit(campaign_id=str(cid), from_location_id=str(elsewhere),
        to_location_id=str(room), label="Обратно", active=False, discovered=True,
        access_rule="Обратный ход закрыт решёткой.")
    db_session.add(closed)
    await db_session.flush()
    await SceneStateService(db_session).ensure_destination(cid, room, elsewhere, allow_discovery=True)
    assert not closed.active
    assert closed.access_rule == "Обратный ход закрыт решёткой."



@pytest.mark.asyncio
async def test_missing_edge_is_not_a_blocker_of_an_approved_physical_trip(db_session):
    cid, room, elsewhere, _, _, _, scene = await _world(db_session)
    contract = PlayerIntentContract(summary="Иду в лавку.", actions=[PlayerActionIntent(
        action_type="movement", intent="Дойти до лавки.", destination_location="Лавка",
        destination_location_id=elsewhere)])
    # No graph authority exists yet: the external resolver must decide this trip.
    assert await ActionPlanCompiler(db_session).resolve_known_travel(cid, contract) is None
    report = await _execute(db_session, cid, scene, contract, TurnOutcomeDecision(action_outcomes=[
        ActionOutcomeDecision(action_index=0, resolution="auto_success", safe_mundane=True,
                              observable_outcome="Приходишь к лавке по обычной улице.")]))
    assert report.steps[0].status == "completed"
    assert report.steps[0].target_location_id == elsewhere
    state = SceneStateService(db_session)
    assert room in {edge.to_location_id for edge in await state.list_exits(cid, elsewhere)}
    await db_session.commit()
    async with AsyncSession(db_session.bind) as reader:
        assert room in {edge.to_location_id for edge in await SceneStateService(reader).list_exits(cid, elsewhere)}



@pytest.mark.asyncio
@pytest.mark.parametrize("published", [False, True])
async def test_discovered_trip_routes_retract_after_compensation_or_published_undo(db_session, published):
    cid, room, elsewhere, _, _, _, scene = await _world(db_session)
    contract = PlayerIntentContract(summary="Иду в лавку.", actions=[PlayerActionIntent(
        action_type="movement", intent="Дойти до лавки.", destination_location="Лавка",
        destination_location_id=elsewhere)])
    report = await _execute(db_session, cid, scene, contract, TurnOutcomeDecision(action_outcomes=[
        ActionOutcomeDecision(action_index=0, resolution="auto_success", safe_mundane=True,
                              observable_outcome="Приходишь к лавке по обычной улице.")]))
    assert report.steps[0].status == "completed"
    if published:
        await ActionSequenceExecutor(db_session).mark_applied(report.sequence_id)
        assistant = await TurnRepository(db_session).create(cid, TurnCreate(role="assistant",
            content="Приходишь к лавке.", scene_id=scene, parent_turn_id=report.trigger_turn_id))
        await TurnOutcomeMaterializer(db_session).bind_to_assistant(report.trigger_turn_id, assistant.id)
    await db_session.commit()
    if published:
        assert await TurnUndoService(db_session).undo_last_pair(cid)
    else:
        await TurnOutcomeMaterializer(db_session).rollback(report.trigger_turn_id)
    await db_session.commit()
    async with AsyncSession(db_session.bind) as reader:
        state = SceneStateService(reader)
        assert not await state.list_exits(cid, room, include_hidden=True)
        assert not await state.list_exits(cid, elsewhere, include_hidden=True)



@pytest.mark.asyncio
async def test_explicit_one_way_exit_is_a_real_return_restriction(db_session):
    cid, room, elsewhere, _, _, _, _ = await _world(db_session)
    await SceneStateService(db_session).create_exit(cid, room, LocationExitCreate(
        to_location_id=elsewhere, label="Спуск", bidirectional=False))
    compiler = ActionPlanCompiler(db_session)
    assert await compiler._route_blocker(cid, room, elsewhere, allow_discovery=True) is None
    assert await compiler._route_blocker(cid, elsewhere, room, allow_discovery=True) is not None



@pytest.mark.asyncio
async def test_grounded_one_way_disclosure_persists_and_undo_retracts_its_return_obstacle(db_session):
    cid, room, elsewhere, _, npc, _, scene = await _world(db_session)
    evidence = "К лавке спуститесь по жёлобу; обратно по нему не подняться."
    route = RouteDisclosure(destination=f"known:{elsewhere}", name="Лавка", evidence=evidence,
                            bidirectional=False, one_way_evidence="обратно по нему не подняться")
    authority = TurnAuthority(campaign_id=cid, trigger_turn_id=uuid4(), player_input="Как пройти?",
                             target_scene_id=scene, beat_owner_id=npc, beat_owner_name="Алиса")
    source = uuid4()
    receipt = await publish_routes(db_session, authority, GrantedBeat(cast_id=str(npc), kind="speech",
        evidence=evidence, route_disclosures=[route]), "— " + evidence, source)
    compiler = ActionPlanCompiler(db_session)
    assert await compiler._route_blocker(cid, room, elsewhere, allow_discovery=True) is None
    assert await compiler._route_blocker(cid, elsewhere, room, allow_discovery=True)
    await undo_published_routes(db_session, cid, {"published_routes": receipt}, source)
    assert not await SceneStateService(db_session).list_exits(cid, room, include_hidden=True)
    assert not await SceneStateService(db_session).list_exits(cid, elsewhere, include_hidden=True)



@pytest.mark.asyncio
async def test_reversible_directions_and_undo_preserve_an_existing_one_way_gate(db_session):
    cid, room, elsewhere, _, npc, _, scene = await _world(db_session)
    reverse = LocationExit(campaign_id=str(cid), from_location_id=str(elsewhere),
        to_location_id=str(room), label="Обратно", active=False, discovered=False,
        access_rule="Обратная калитка заперта.")
    db_session.add(reverse)
    await db_session.flush()
    prose = "— Отсюда идите по улице прямо до лавки."
    beat = GrantedBeat(cast_id=str(npc), kind="speech", evidence=prose[2:],
        route_disclosures=[RouteDisclosure(destination=f"known:{elsewhere}", name="Лавка",
                                           evidence=prose[2:])])
    authority = TurnAuthority(campaign_id=cid, trigger_turn_id=uuid4(), player_input="Как пройти?",
        target_scene_id=scene, beat_owner_id=npc, beat_owner_name="Алиса")
    receipt = await publish_routes(db_session, authority, beat, prose, uuid4())
    assert receipt["edges"]  # The forward discovery is still useful.
    assert not reverse.active and reverse.access_rule == "Обратная калитка заперта."

