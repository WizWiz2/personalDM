"""Route announcements must survive publication and retract with the source pair."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.db.repositories.turn_repo import TurnRepository
from app.db.scene_state_table import LocationExit
from app.db.tables import Character, Entity, Turn
from app.models.item_state import ItemStateSnapshot
from app.models.narration_validation import GrantedBeat, RouteDisclosure
from app.models.player_intent import PlayerIntentContract
from app.models.turn import TurnCreate
from app.models.turn_authority import TurnAuthority
from app.models.turn_authority import PlannedPlaceIntroduction
from app.services.action_plan_compiler import ActionPlanCompiler
from app.services.authority_narration_pipeline import AuthorityNarrationPipeline
from app.services.item_prompt_scope import prompt_item_states
from app.services.published_routes import publish_routes, route_catalog, undo_published_routes
from app.services.turn_undo_service import TurnUndoService
from app.services.turn_outcome_materializer import TurnOutcomeMaterializer
from app.services.turn_outcome_resolver import _outcome_wire_model, normalize_outcome_draft
from tests.test_qa219_regressions import _world


def _authority(cid, npc, scene):
    return TurnAuthority(campaign_id=cid, trigger_turn_id=uuid4(), player_input="Как пройти?",
        target_scene_id=scene, beat_owner_id=npc, beat_owner_name="Алиса",
        present_character_names=["Алиса", "Вера"])


def _directions(npc, destination, name="Лавка", *, bidirectional=True, profile=None):
    evidence = f"Отсюда идите прямо по дороге до места «{name}»."
    return "— " + evidence, GrantedBeat(cast_id=str(npc), kind="speech", evidence=evidence,
        route_disclosures=[RouteDisclosure(destination=destination, name=name, profile=profile,
                                          evidence=evidence, bidirectional=bidirectional)])


@pytest.mark.asyncio
async def test_published_directions_make_originally_unreachable_destination_traversable_and_undo(db_session):
    cid, room, elsewhere, _, npc, _, scene = await _world(db_session)
    compiler = ActionPlanCompiler(db_session)
    assert await compiler._route_blocker(cid, room, elsewhere)
    turns = TurnRepository(db_session)
    user = await turns.create(cid, TurnCreate(role="user", content="Как пройти?", scene_id=scene))
    prose, beat = _directions(npc, f"known:{elsewhere}")
    assistant = await turns.create(cid, TurnCreate(role="assistant", content=prose,
        scene_id=scene, parent_turn_id=user.id))
    receipt = await publish_routes(db_session, _authority(cid, npc, scene), beat, prose, assistant.id)
    (await db_session.get(Turn, str(assistant.id))).context_snapshot = json.dumps(
        {"published_routes": receipt})
    assert len(receipt["edges"]) == 2
    assert await compiler._route_blocker(cid, room, elsewhere) is None
    assert await compiler._route_blocker(cid, elsewhere, room) is None
    await db_session.commit()
    assert await TurnUndoService(db_session).undo_last_pair(cid)
    assert await compiler._route_blocker(cid, room, elsewhere)


@pytest.mark.asyncio
async def test_disclosure_does_not_open_blocked_edge_or_erase_access_rule(db_session):
    cid, room, elsewhere, _, npc, _, scene = await _world(db_session)
    edge = LocationExit(campaign_id=str(cid), from_location_id=str(room),
        to_location_id=str(elsewhere), label="Закрытая дорога", discovered=False,
        active=False, access_rule="Проход закрыт до ремонта моста.")
    db_session.add(edge)
    await db_session.flush()
    prose, beat = _directions(npc, f"known:{elsewhere}")
    source = uuid4()
    receipt = await publish_routes(db_session, _authority(cid, npc, scene), beat, prose, source)
    assert edge.discovered and not edge.active and edge.access_rule == "Проход закрыт до ремонта моста."
    assert len(receipt["edges"]) == 1  # Do not invent an open return path around the same closure.
    assert await ActionPlanCompiler(db_session)._route_blocker(cid, room, elsewhere)
    await undo_published_routes(db_session, cid, {"published_routes": receipt}, source)
    assert not edge.discovered and not edge.active


@pytest.mark.asyncio
async def test_new_destination_has_parent_route_and_publication_provenance_and_is_retracted(db_session):
    cid, room, _, _, npc, _, scene = await _world(db_session)
    prose, beat = _directions(npc, f"new_in:{room}", "Районная контора",
        profile="Отдельное каменное здание на площади, с приёмной и вывеской районной конторы.")
    source = uuid4()
    receipt = await publish_routes(db_session, _authority(cid, npc, scene), beat, prose, source)
    destination = receipt["places"][0]
    assert await ActionPlanCompiler(db_session)._route_blocker(cid, room, UUID(destination)) is None
    assert destination in [place["id"] for place in await route_catalog(db_session, cid)]
    # The same published directions reuse the identity and edges, rather than spawning twins.
    second = await publish_routes(db_session, _authority(cid, npc, scene), beat, prose, source)
    assert second == {"places": [], "edges": []}
    await undo_published_routes(db_session, cid, {"published_routes": receipt}, source)
    assert (await db_session.get(Entity, destination)).status == "inactive"
    assert destination not in [place["id"] for place in await route_catalog(db_session, cid)]


@pytest.mark.asyncio
async def test_unpublished_route_writes_rollback_and_cannot_use_another_campaign_destination(db_session):
    cid, room, elsewhere, _, npc, _, scene = await _world(db_session)
    await db_session.commit()
    prose, beat = _directions(npc, f"known:{elsewhere}")
    await publish_routes(db_session, _authority(cid, npc, scene), beat, prose, uuid4())
    await db_session.rollback()
    assert not (await db_session.execute(select(LocationExit).where(LocationExit.campaign_id == str(cid)))).scalars().all()
    prose, beat = _directions(npc, f"known:{uuid4()}")
    with pytest.raises(ValueError, match="active campaign"):
        await publish_routes(db_session, _authority(cid, npc, scene), beat, prose, uuid4())


@pytest.mark.asyncio
async def test_stale_cast_list_cannot_publish_directions_from_an_offscreen_speaker(db_session):
    cid, _, elsewhere, _, npc, _, scene = await _world(db_session)
    (await db_session.get(Character, str(npc))).current_location_id = str(elsewhere)
    prose, beat = _directions(npc, f"known:{elsewhere}")
    with pytest.raises(ValueError, match="physically present"):
        await publish_routes(db_session, _authority(cid, npc, scene), beat, prose, uuid4())


@pytest.mark.asyncio
async def test_undo_retracts_a_planned_place_introduction(db_session):
    cid, room, _, _, npc, _, scene = await _world(db_session)
    turns = TurnRepository(db_session)
    user = await turns.create(cid, TurnCreate(role="user", content="Где контора?", scene_id=scene))
    authority = _authority(cid, npc, scene)
    authority.allowed_new_places = [PlannedPlaceIntroduction(canonical_name="Районная контора",
        parent_location_id=room, profile="Отдельное каменное здание на площади, с приёмной районной конторы.")]
    materializer = TurnOutcomeMaterializer(db_session)
    outcome = await materializer.materialize(authority, source_turn_id=user.id)
    assistant = await turns.create(cid, TurnCreate(role="assistant", content="Контора на площади.",
        scene_id=scene, parent_turn_id=user.id))
    await materializer.bind_to_assistant(user.id, assistant.id)
    destination = str(outcome.introduced_place_ids[0])
    assert (await db_session.get(Entity, destination)).status == "active"
    await db_session.commit()
    assert await TurnUndoService(db_session).undo_last_pair(cid)
    assert (await db_session.get(Entity, destination)).status == "inactive"


@pytest.mark.asyncio
async def test_native_narration_schema_requires_disclosures_bound_to_registered_places(db_session):
    cid, _, elsewhere, _, npc, _, scene = await _world(db_session)
    prose, beat = _directions(npc, f"known:{elsewhere}")
    router = SimpleNamespace(generate_json=AsyncMock(return_value={"prose": prose, "beat": beat.model_dump()}))
    pipeline = AuthorityNarrationPipeline(db_session, router, SimpleNamespace(last_telemetry={}))
    text, certified, _ = await pipeline._narrate([], SimpleNamespace(), _authority(cid, npc, scene), temperature=0)
    schema = router.generate_json.call_args.kwargs["response_model"].model_json_schema()
    beat_schema = next(value for value in schema["$defs"].values() if "cast_id" in value.get("properties", {}))
    assert "route_disclosures" in beat_schema["required"]
    assert certified.route_disclosures[0].destination == f"known:{elsewhere}" and text == prose
    certified.route_disclosures[0].evidence = "Он якобы слышал о дороге."
    assert "route evidence" in certified.failure(npc, "Алиса", text)


def test_remote_document_details_are_scoped_without_hiding_identity_or_mutating_audit_state():
    hero, here, remote, item_id = uuid4(), uuid4(), uuid4(), uuid4()
    text = "Полный текст распоряжения о перевозке. " * 50
    states = [ItemStateSnapshot(item_id=item_id, name="Проколотый бланк", location_id=remote,
        revealed_content=text, properties={"folded": "yes"})]
    compact = prompt_item_states(states, location_id=here, query="Осматриваю помещение.")
    assert compact[0]["item_id"] == str(item_id) and compact[0]["location_id"] == str(remote)
    assert compact[0]["archived_content_available"] and "revealed_content" not in compact[0]
    assert len(json.dumps(compact)) < len(text) / 3
    # Morphology retrieves an offscene document when the input uses an inflected form.
    referenced = prompt_item_states(states, location_id=here, query="Что было написано на проколотом бланке?")
    assert referenced[0]["revealed_content"] == text
    assert states[0].revealed_content == text and states[0].properties == {"folded": "yes"}
    authority = TurnAuthority(campaign_id=uuid4(), trigger_turn_id=uuid4(), player_input="Жду.",
        player_character_id=hero, place_frame={"location_id": str(here)}, item_states=states)
    assert "revealed_content" not in authority.narrator_payload()["item_states"][0]
    assert authority.validator_payload()["item_states"][0]["revealed_content"] == text


def test_misspelled_offscreen_mention_can_bind_existing_cast_without_forcing_a_twin():
    person = str(uuid4())
    wire = _outcome_wire_model(0, required_absent=("Линния",), known_cast={person: "Линия"})
    npc = {"canonical_name": "Линния", "mentioned_name": "Линния", "cast_id": person,
        "role": "служащая", "description": "Знакомая служащая станции в аккуратном тёмном костюме.",
        "appearance": "Женщина средних лет в очках и тёмном костюме без украшений.",
        "reason": "Игрок спрашивает, жива ли уже известная служащая.", "temporary_name": False,
        "personal_name_evidence": "Линния жива?"}
    data = {"action_outcomes": [], "npc_introductions": [], "absent_people": [npc],
            "observable_consequences": ["Алиса отвечает на вопрос об известной служащей."]}
    draft = wire.model_validate(data)
    decision = normalize_outcome_draft(draft, PlayerIntentContract(summary="Линния жива?"))
    assert decision.npc_introductions[0].canonical_name == "Линия"
    assert decision.npc_introductions[0].existing_entity_id == UUID(person)
    assert decision.npc_introductions[0].sent_for
    # A source mention cannot legitimize an unrelated newly invented identity.
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="frozen"):
        wire.model_validate({**data, "absent_people": [
            {**npc, "cast_id": "newcomer", "canonical_name": "Новая служащая"}]})
