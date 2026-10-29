"""A world NPC move inside a full TurnSaga turn: ledger receipt, physical move and undo provenance
are committed together with the published answer (observed from a second session)."""
import json

import pytest
from sqlalchemy import select

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import Character, Event, GenerationRun, Turn
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.location import LocationCreate
from app.models.scene import SceneCreate
from app.models.scene_state import LocationExitCreate
from app.models.turn import TurnCreate
from app.models.turn_authority import PlannedWorldMove, TurnAuthority
from app.services.authority_narration_pipeline import AuthorityNarrationPipeline, AuthorityNarrationResult
from app.services.campaign_service import CampaignService
from app.services.scene_lifecycle import SceneLifecycleService
from app.services.scene_state_service import SceneStateService
from app.services.turn_authority_service import TurnAuthorityService
from app.services.turn_saga import TurnSaga


async def _world(session, *, npc_location=True):
    cid = (await CampaignService(session).create_campaign(CampaignCreate(name="world move"))).id
    locations = LocationRepository(session)
    hall = await locations.create(cid, LocationCreate(canonical_name="Зал"))
    pier = await locations.create(cid, LocationCreate(canonical_name="Пристань"))
    entities = EntityRepository(session)
    hero = await entities.create_character(cid, CharacterCreate(canonical_name="Ирма", current_location_id=hall.id))
    npc = await entities.create_character(cid, CharacterCreate(
        canonical_name="Йонас", current_location_id=hall.id if npc_location else None))
    await CampaignRepository(session).update(cid, CampaignUpdate(player_character_id=hero.id))
    scene = await SceneRepository(session).create(cid, SceneCreate(title="Зал", location_id=hall.id))
    await SceneLifecycleService(session).activate(cid, scene.id)
    await SceneStateService(session).create_exit(cid, hall.id, LocationExitCreate(to_location_id=pier.id, label="Пристань"))
    await session.commit()
    return cid, scene.id, npc.id, hall.id, pier.id


def _seams(monkeypatch, npc, pier):
    async def plan(self, **kwargs):
        return None, {"status": "skipped"}

    async def build(self, **kw):
        return TurnAuthority(
            campaign_id=kw["campaign_id"], trigger_turn_id=kw["trigger_turn_id"], player_input=kw["player_input"],
            source_scene_id=kw["source_scene_id"], target_scene_id=kw["target_scene_id"],
            world_move=PlannedWorldMove(kind="npc_move", source="Йонас обещал встретить лодку",
                                        outcome="Йонас уходит встречать лодку", actor_id=npc,
                                        destination_location_id=pier),
        )

    async def generate(self, **kw):
        return AuthorityNarrationResult(text="Йонас кивает и уходит к пристани.", telemetry={})

    monkeypatch.setattr(TurnSaga, "_plan", plan)
    monkeypatch.setattr(TurnAuthorityService, "build", build)
    monkeypatch.setattr(AuthorityNarrationPipeline, "generate", generate)


async def _play(file_db, cid, scene):
    async with file_db() as session:
        return "".join([c async for c in TurnSaga(session).run_turn_stream(
            cid, TurnCreate(role="user", content="Жду.", scene_id=scene))])


@pytest.mark.asyncio
async def test_world_npc_move_is_recorded_and_applied_with_the_published_turn(file_db, monkeypatch):
    async with file_db() as session:
        cid, scene, npc, hall, pier = await _world(session)
    _seams(monkeypatch, npc, pier)
    assert "Йонас" in await _play(file_db, cid, scene)

    async with file_db() as other:
        assistant = (await other.execute(select(Turn).where(Turn.campaign_id == str(cid),
                                                             Turn.role == "assistant"))).scalar_one()
        events = (await other.execute(select(Event).where(Event.event_type == "world_initiative:npc_move"))).scalars().all()
        assert [json.loads(e.source_turns) for e in events] == [[assistant.id]]
        assert (await other.get(Character, str(npc))).current_location_id == str(pier)
        snapshot = json.loads(assistant.context_snapshot)
        assert snapshot["world_move_before"]["origin_location_id"] == str(hall)
        assert snapshot["approved_world_move"]["kind"] == "npc_move"
        run = (await other.execute(select(GenerationRun).where(GenerationRun.campaign_id == str(cid)))).scalar_one()
        assert run.status == "completed" and run.assistant_turn_id == assistant.id


@pytest.mark.asyncio
async def test_world_move_of_npc_without_position_fails_the_turn_and_persists_nothing(file_db, monkeypatch):
    async with file_db() as session:
        cid, scene, npc, _, pier = await _world(session, npc_location=False)
    _seams(monkeypatch, npc, pier)
    assert "Generation failed" in await _play(file_db, cid, scene)

    async with file_db() as other:
        roles = (await other.execute(select(Turn.role, Turn.status).where(Turn.campaign_id == str(cid)))).all()
        assert [tuple(r) for r in roles] == [("user", "failed")]
        assert (await other.execute(select(Event).where(Event.event_type.like("world_initiative:%")))).first() is None
        assert (await other.get(Character, str(npc))).current_location_id is None
