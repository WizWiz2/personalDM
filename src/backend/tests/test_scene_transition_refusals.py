"""SceneTransitionExecutor refuses unauthorized or unroutable moves and leaves the world unchanged."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.repositories.turn_repo import TurnRepository
from app.db.scene_state_table import LocationExit
from app.db.tables import Character, Location, Scene
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.location import LocationCreate
from app.models.scene import SceneCreate
from app.models.scene_state import LocationExitCreate
from app.models.turn import TurnCreate
from app.services.player_destination_authorization import DestinationAuthorization
from app.services.scene_lifecycle import SceneLifecycleService
from app.services.scene_state_service import SceneStateService
from app.services.scene_transition_executor import SceneTransitionExecutor
from app.services.turn_planner import SceneTransitionPlan


async def _world(session, *, exit_to_pier: dict | None = None):
    cid = uuid4()
    campaigns = CampaignRepository(session)
    await campaigns.create(cid, CampaignCreate(name="refusals"))
    locations = LocationRepository(session)
    hall = await locations.create(cid, LocationCreate(canonical_name="Зал"))
    pier = await locations.create(cid, LocationCreate(canonical_name="Пристань"))
    hero = await EntityRepository(session).create_character(
        cid, CharacterCreate(canonical_name="Ирма", current_location_id=hall.id))
    await campaigns.update(cid, CampaignUpdate(player_character_id=hero.id))
    scene = await SceneRepository(session).create(cid, SceneCreate(title="Зал", location_id=hall.id))
    await SceneLifecycleService(session).activate(cid, scene.id)
    if exit_to_pier is not None:
        await SceneStateService(session).create_exit(
            cid, hall.id, LocationExitCreate(to_location_id=pier.id, label="Пристань", **exit_to_pier))
    turn = await TurnRepository(session).create(cid, TurnCreate(role="user", content="Иду на пристань"))
    await session.flush()
    return SimpleNamespace(cid=cid, hall=hall, pier=pier, hero=hero, scene=scene, turn=turn)


def _executor(session, *, applicable: bool, authorized: bool, destination: str = "Пристань"):
    executor = SceneTransitionExecutor(session)
    executor._destination_authorizer = SimpleNamespace(authorize=AsyncMock(return_value=DestinationAuthorization(
        applicable=applicable, authorized=authorized, reason="test", destination=destination)))
    return executor


def _plan(destination="Пристань"):
    return SceneTransitionPlan(required=True, transition_type="location_transition", destination_location=destination)


async def _snapshot(session, w):
    hero = await session.get(Character, str(w.hero.id))
    scenes = (await session.execute(select(func.count()).select_from(Scene).where(Scene.campaign_id == str(w.cid)))).scalar()
    places = (await session.execute(select(func.count()).select_from(Location))).scalar()
    return hero.current_location_id, scenes, places


async def _refused(session, w, executor, plan=None, **kwargs):
    before = await _snapshot(session, w)
    with pytest.raises(ValueError):
        await executor.apply(w.cid, w.scene.id, w.turn.id, plan or _plan(), **kwargs)
    assert await _snapshot(session, w) == before


@pytest.mark.asyncio
async def test_unauthorized_player_destination_is_refused(db_session):
    w = await _world(db_session, exit_to_pier={})
    await _refused(db_session, w, _executor(db_session, applicable=True, authorized=False))


@pytest.mark.asyncio
async def test_authorized_destination_name_replaces_the_planner_wording(db_session):
    w = await _world(db_session, exit_to_pier={})
    executor = _executor(db_session, applicable=True, authorized=True, destination="Пристань")
    applied = await executor.apply(w.cid, w.scene.id, w.turn.id, _plan("причал у реки"))
    assert applied.target_location_id == w.pier.id
    assert (await db_session.get(Character, str(w.hero.id))).current_location_id == str(w.pier.id)


@pytest.mark.asyncio
async def test_existing_place_without_any_route_is_refused(db_session):
    w = await _world(db_session)
    await _refused(db_session, w, _executor(db_session, applicable=False, authorized=False))


@pytest.mark.asyncio
async def test_unknown_place_cannot_be_invented_without_a_profile(db_session):
    w = await _world(db_session, exit_to_pier={})
    await _refused(db_session, w, _executor(db_session, applicable=False, authorized=False), _plan("Ладога"))


@pytest.mark.asyncio
async def test_inactive_route_is_refused(db_session):
    w = await _world(db_session, exit_to_pier={"active": False, "access_rule": "sealed"})
    await _refused(db_session, w, _executor(db_session, applicable=True, authorized=True))


@pytest.mark.asyncio
async def test_undiscovered_route_is_refused_without_discovery(db_session):
    w = await _world(db_session, exit_to_pier={"discovered": False})
    await _refused(db_session, w, _executor(db_session, applicable=False, authorized=False))


@pytest.mark.asyncio
async def test_authorized_move_discovers_a_hidden_route(db_session):
    w = await _world(db_session, exit_to_pier={"discovered": False})
    applied = await _executor(db_session, applicable=True, authorized=True).apply(w.cid, w.scene.id, w.turn.id, _plan())
    assert applied.target_location_id == w.pier.id
    route = (await db_session.execute(select(LocationExit).where(LocationExit.to_location_id == str(w.pier.id)))).scalar_one()
    assert route.discovered is True


@pytest.mark.asyncio
async def test_open_route_to_existing_place_reuses_it(db_session):
    w = await _world(db_session, exit_to_pier={})
    _, _, places = await _snapshot(db_session, w)
    applied = await _executor(db_session, applicable=False, authorized=False).apply(w.cid, w.scene.id, w.turn.id, _plan())
    assert applied.target_location_id == w.pier.id
    assert (await _snapshot(db_session, w))[2] == places
