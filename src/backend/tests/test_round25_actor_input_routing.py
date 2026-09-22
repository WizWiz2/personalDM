import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.models.turn import ChatMessage, TurnCreate
from app.services.turn_authority_planner import CoordinatedTurnPlan
from app.services.turn_authority_service import (
    TurnAuthorityService,
    _address_repair_colocated,
)
from app.services.turn_runner import TurnRunner
from app.db.repositories.turn_repo import TurnRepository
from app.services.turn_planner import SceneTransitionPlan


def test_talk_target_is_routing_context_not_planner_bypass():
    anna_id = uuid4()
    routed = TurnRunner._route_addressed_input(
        TurnCreate(
            role="user",
            content="Тогда пойду один. Анна, вы со мной?",
            acting_character_id=anna_id,
        )
    )

    assert routed.acting_character_id is None
    assert routed.context_snapshot["input_routing"]["addressed_character_id"] == str(anna_id)
    assert routed.context_snapshot["input_routing"]["planner_bypass"] is False
    assert routed.context_snapshot["input_routing"]["user_actor"] == "player_character"
    assert TurnRunner._addressed_character_id(routed) == anna_id


@pytest.mark.asyncio
async def test_round26_planner_context_never_turns_addressee_into_speaker(monkeypatch):
    anna_id = uuid4()
    seen: dict = {}

    class FakeCompiler:
        def __init__(self, _session):
            pass

        async def compile_context(self, **kwargs):
            seen.update(kwargs)
            return [
                ChatMessage(role="system", content="BASE PLAYER CONTEXT"),
                ChatMessage(role="user", content=kwargs["current_user_content"]),
            ], {}

    class FakeEntityRepository:
        def __init__(self, _session):
            pass

        async def get_character(self, entity_id):
            assert entity_id == anna_id
            return SimpleNamespace(id=anna_id, canonical_name="Ирина")

    monkeypatch.setattr("app.services.context_compiler.ContextCompiler", FakeCompiler)
    monkeypatch.setattr("app.services.turn_runner.EntityRepository", FakeEntityRepository)

    runner = TurnRunner.__new__(TurnRunner)
    runner._session = SimpleNamespace()
    routed = TurnRunner._route_addressed_input(
        TurnCreate(
            role="user",
            content="Ирина, я слушаю. Расскажите по порядку.",
            acting_character_id=anna_id,
        )
    )

    compiled, _compiler, _budget = await runner._compile(
        uuid4(),
        routed,
        uuid4(),
        SimpleNamespace(context_window=6144),
    )
    messages, metadata = compiled

    assert seen["acting_character_id"] is None
    assert "Addressed character: Ирина" in messages[0].content
    assert "listener/target" in messages[0].content
    assert "NOT the speaker" in messages[0].content
    assert "[ACTOR OUTPUT CONTRACT: Ирина]" not in messages[0].content
    assert metadata["input_routing"]["addressed_character_id"] == str(anna_id)
    assert metadata["input_routing"]["user_actor"] == "player_character"


def test_assistant_persists_actor_from_final_turn_authority():
    anna_id = uuid4()
    data = TurnCreate(
        role="assistant",
        content="Анна отвечает.",
        context_snapshot={
            "turn_authority": {
                "acting_character_id": str(anna_id),
                "scene_disposition": "actor_turn",
            }
        },
    )

    assert TurnRepository._effective_acting_character_id(data) == anna_id


def _scene(scene_id, location_id, participant_names):
    return SimpleNamespace(
        scene_id=scene_id,
        location_id=location_id,
        location_path=["Город", str(location_id)],
        participant_names=list(participant_names),
        object_names=[],
    )


def _entity(entity_id, name):
    return SimpleNamespace(
        id=entity_id,
        canonical_name=name,
        aliases=[],
        entity_type="character",
    )


async def _service_fixture(*, target_has_anna: bool, monkeypatch=None):
    if monkeypatch is not None:
        async def _no_lines(*_args, **_kwargs):
            return []

        async def _no_subjects(*_args, **_kwargs):
            return []

        monkeypatch.setattr(
            "app.services.turn_authority_service.established_state_lines",
            _no_lines,
        )
        monkeypatch.setattr(
            "app.services.turn_authority_service.established_subjects",
            _no_subjects,
        )
    campaign_id = uuid4()
    hero_id = uuid4()
    anna_id = uuid4()
    source_scene_id = uuid4()
    target_scene_id = uuid4()
    source_location = uuid4()
    target_location = source_location if target_has_anna else uuid4()

    user_row = SimpleNamespace(
        context_snapshot=json.dumps(
            {
                "input_routing": {
                    "addressed_character_id": str(anna_id),
                    "planner_bypass": False,
                    "user_actor": "player_character",
                }
            }
        )
    )
    session = SimpleNamespace(get=AsyncMock(return_value=user_row))
    service = TurnAuthorityService.__new__(TurnAuthorityService)
    service._session = session
    service._campaigns = SimpleNamespace(
        get_by_id=AsyncMock(return_value=SimpleNamespace(player_character_id=hero_id))
    )

    hero = SimpleNamespace(
        id=hero_id,
        canonical_name="Алексей",
        current_location_id=target_location,
    )
    anna = SimpleNamespace(
        id=anna_id,
        canonical_name="Анна Левина",
        current_location_id=source_location,
    )
    by_id = {hero_id: hero, anna_id: anna}
    service._entities = SimpleNamespace(
        get_character=AsyncMock(side_effect=lambda value: by_id.get(value)),
        list_by_campaign=AsyncMock(
            return_value=[_entity(hero_id, "Алексей"), _entity(anna_id, "Анна Левина")]
        ),
    )
    source_state = _scene(
        source_scene_id,
        source_location,
        ["Алексей", "Анна Левина"],
    )
    target_state = _scene(
        target_scene_id,
        target_location,
        ["Алексей", "Анна Левина"] if target_has_anna else ["Алексей"],
    )
    service._scene_state = SimpleNamespace(
        get=AsyncMock(
            side_effect=lambda _campaign_id, scene_id: (
                source_state if scene_id == source_scene_id else target_state
            )
        )
    )
    return service, campaign_id, hero_id, anna_id, source_scene_id, target_scene_id


@pytest.mark.asyncio
async def test_addressed_present_npc_becomes_actor_after_stay_plan(monkeypatch):
    service, campaign_id, _hero_id, anna_id, source_scene_id, target_scene_id = (
        await _service_fixture(target_has_anna=True, monkeypatch=monkeypatch)
    )
    plan = CoordinatedTurnPlan(
        player_intent="Спросить Анну о рукописи",
        resolution="conversation",
        addressed_response_requested=True,
        response_ownership_reason="Игрок задаёт прямой вопрос выбранной Анне.",
    )

    authority = await service.build(
        campaign_id=campaign_id,
        trigger_turn_id=uuid4(),
        player_input="Анна, когда вы видели рукопись?",
        source_scene_id=source_scene_id,
        target_scene_id=target_scene_id,
        plan=plan,
        acting_character_id=None,
    )

    assert authority.scene_disposition == "actor_turn"
    assert authority.acting_character_id == anna_id
    assert authority.acting_character_name == "Анна Левина"


@pytest.mark.asyncio
async def test_addressed_npc_loses_actor_authority_after_player_moves_away(monkeypatch):
    service, campaign_id, _hero_id, _anna_id, source_scene_id, target_scene_id = (
        await _service_fixture(target_has_anna=False, monkeypatch=monkeypatch)
    )
    plan = CoordinatedTurnPlan(
        player_intent="Выйти из кабинета и пойти в библиотеку",
        resolution="transition",
        addressed_response_requested=False,
        scene_transition=SceneTransitionPlan(
            required=True,
            transition_type="location_transition",
            destination_location="Городская библиотека",
            reason="Игрок явно выходит из кабинета и идёт в библиотеку.",
        ),
    )

    authority = await service.build(
        campaign_id=campaign_id,
        trigger_turn_id=uuid4(),
        player_input="Тогда пойду один. Выхожу из кабинета и иду в библиотеку.",
        source_scene_id=source_scene_id,
        target_scene_id=target_scene_id,
        plan=plan,
        acting_character_id=None,
    )

    assert authority.scene_disposition == "location_transition"
    assert authority.acting_character_id is None
    assert authority.acting_character_name is None


@pytest.mark.asyncio
async def test_obligation_to_named_npc_remaps_sticky_prior_listener_actor(monkeypatch):
    """Sticky /talk listener must not keep actor ownership when this turn obligates another NPC.

    Prior listener dialogue history would otherwise bleed into the new address.
    """
    async def _no_lines(*_args, **_kwargs):
        return []

    async def _no_subjects(*_args, **_kwargs):
        return []

    monkeypatch.setattr(
        "app.services.turn_authority_service.established_state_lines",
        _no_lines,
    )
    monkeypatch.setattr(
        "app.services.turn_authority_service.established_subjects",
        _no_subjects,
    )
    campaign_id = uuid4()
    hero_id = uuid4()
    lira_id = uuid4()
    steward_id = uuid4()
    scene_id = uuid4()
    location_id = uuid4()

    # Sticky prior listener is steward (Управляющая), but player names Лира this turn.
    user_row = SimpleNamespace(
        context_snapshot=json.dumps(
            {
                "input_routing": {
                    "addressed_character_id": str(steward_id),
                    "planner_bypass": False,
                    "user_actor": "player_character",
                }
            }
        )
    )
    session = SimpleNamespace(get=AsyncMock(return_value=user_row))
    service = TurnAuthorityService.__new__(TurnAuthorityService)
    service._session = session
    service._campaigns = SimpleNamespace(
        get_by_id=AsyncMock(return_value=SimpleNamespace(player_character_id=hero_id))
    )

    hero = SimpleNamespace(id=hero_id, canonical_name="Эйдан", current_location_id=location_id)
    lira = SimpleNamespace(id=lira_id, canonical_name="Лира", current_location_id=location_id)
    steward = SimpleNamespace(
        id=steward_id, canonical_name="Управляющая домом", current_location_id=location_id
    )
    by_id = {hero_id: hero, lira_id: lira, steward_id: steward}
    service._entities = SimpleNamespace(
        get_character=AsyncMock(side_effect=lambda value: by_id.get(value)),
        list_by_campaign=AsyncMock(
            return_value=[
                _entity(hero_id, "Эйдан"),
                _entity(lira_id, "Лира"),
                _entity(steward_id, "Управляющая домом"),
            ]
        ),
    )
    state = _scene(scene_id, location_id, ["Эйдан", "Лира", "Управляющая домом", "Служанка"])
    service._scene_state = SimpleNamespace(get=AsyncMock(return_value=state))

    plan = CoordinatedTurnPlan(
        player_intent="Спросить Лиру где хлеб",
        resolution="conversation",
        addressed_response_requested=True,
        response_ownership_reason="Игрок прямо называет Лиру.",
    )

    authority = await service.build(
        campaign_id=campaign_id,
        trigger_turn_id=uuid4(),
        player_input="Лира, где здесь хлеб?",
        source_scene_id=scene_id,
        target_scene_id=scene_id,
        plan=plan,
        acting_character_id=None,
    )

    assert authority.addressed_response_obligation == "Лира"
    assert authority.acting_character_id == lira_id
    assert authority.acting_character_name == "Лира"
    assert authority.scene_disposition == "actor_turn"


@pytest.mark.asyncio
async def test_named_colocated_npc_missing_from_participants_stamps_obligation(monkeypatch):
    """Campaign entity at kitchen location but absent from participants still gets obligation.

    Live residual after #186: player names Лира; she is a known co-located character; stale
    participant list must not prevent addressed_response_obligation from stamping.
    """
    async def _no_lines(*_args, **_kwargs):
        return []

    async def _no_subjects(*_args, **_kwargs):
        return []

    monkeypatch.setattr(
        "app.services.turn_authority_service.established_state_lines",
        _no_lines,
    )
    monkeypatch.setattr(
        "app.services.turn_authority_service.established_subjects",
        _no_subjects,
    )
    campaign_id = uuid4()
    hero_id = uuid4()
    lira_id = uuid4()
    steward_id = uuid4()
    scene_id = uuid4()
    location_id = uuid4()

    user_row = SimpleNamespace(
        context_snapshot=json.dumps(
            {
                "input_routing": {
                    "addressed_character_id": str(steward_id),
                    "planner_bypass": False,
                    "user_actor": "player_character",
                }
            }
        )
    )
    session = SimpleNamespace(get=AsyncMock(return_value=user_row))
    service = TurnAuthorityService.__new__(TurnAuthorityService)
    service._session = session
    service._campaigns = SimpleNamespace(
        get_by_id=AsyncMock(return_value=SimpleNamespace(player_character_id=hero_id))
    )

    hero = SimpleNamespace(id=hero_id, canonical_name="Эйдан", current_location_id=location_id)
    lira = SimpleNamespace(id=lira_id, canonical_name="Лира", current_location_id=location_id)
    steward = SimpleNamespace(
        id=steward_id, canonical_name="Управляющая домом", current_location_id=location_id
    )
    by_id = {hero_id: hero, lira_id: lira, steward_id: steward}
    service._entities = SimpleNamespace(
        get_character=AsyncMock(side_effect=lambda value: by_id.get(value)),
        list_by_campaign=AsyncMock(
            return_value=[
                _entity(hero_id, "Эйдан"),
                _entity(lira_id, "Лира"),
                _entity(steward_id, "Управляющая домом"),
            ]
        ),
    )
    # Stale participants: kitchen has steward/hero but Лира missing despite co-location.
    state = _scene(scene_id, location_id, ["Эйдан", "Управляющая домом"])
    service._scene_state = SimpleNamespace(get=AsyncMock(return_value=state))

    plan = CoordinatedTurnPlan(
        player_intent="Спросить Лиру где хлеб",
        resolution="conversation",
        addressed_response_requested=True,
        response_ownership_reason="Игрок прямо называет Лиру.",
    )

    authority = await service.build(
        campaign_id=campaign_id,
        trigger_turn_id=uuid4(),
        player_input="Лира, где здесь хлеб?",
        source_scene_id=scene_id,
        target_scene_id=scene_id,
        plan=plan,
        acting_character_id=None,
    )

    assert "Лира" in authority.present_character_names
    assert authority.addressed_response_obligation == "Лира"
    assert authority.acting_character_id == lira_id
    assert any(item.canonical_name == "Лира" for item in authority.allowed_existing_npc_arrivals)


@pytest.mark.asyncio
async def test_named_npc_elsewhere_is_not_teleported_into_present(monkeypatch):
    """Naming a known entity in another location must not invent presence."""
    async def _no_lines(*_args, **_kwargs):
        return []

    async def _no_subjects(*_args, **_kwargs):
        return []

    monkeypatch.setattr(
        "app.services.turn_authority_service.established_state_lines",
        _no_lines,
    )
    monkeypatch.setattr(
        "app.services.turn_authority_service.established_subjects",
        _no_subjects,
    )
    campaign_id = uuid4()
    hero_id = uuid4()
    lira_id = uuid4()
    scene_id = uuid4()
    kitchen_id = uuid4()
    hall_id = uuid4()

    session = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(context_snapshot=None)))
    service = TurnAuthorityService.__new__(TurnAuthorityService)
    service._session = session
    service._campaigns = SimpleNamespace(
        get_by_id=AsyncMock(return_value=SimpleNamespace(player_character_id=hero_id))
    )
    hero = SimpleNamespace(id=hero_id, canonical_name="Эйдан", current_location_id=kitchen_id)
    lira = SimpleNamespace(id=lira_id, canonical_name="Лира", current_location_id=hall_id)
    by_id = {hero_id: hero, lira_id: lira}
    service._entities = SimpleNamespace(
        get_character=AsyncMock(side_effect=lambda value: by_id.get(value)),
        list_by_campaign=AsyncMock(
            return_value=[_entity(hero_id, "Эйдан"), _entity(lira_id, "Лира")]
        ),
    )
    state = _scene(scene_id, kitchen_id, ["Эйдан"])
    service._scene_state = SimpleNamespace(get=AsyncMock(return_value=state))

    plan = CoordinatedTurnPlan(
        player_intent="Спросить Лиру",
        resolution="conversation",
        addressed_response_requested=True,
    )
    authority = await service.build(
        campaign_id=campaign_id,
        trigger_turn_id=uuid4(),
        player_input="Лира, где здесь хлеб?",
        source_scene_id=scene_id,
        target_scene_id=scene_id,
        plan=plan,
        acting_character_id=None,
    )
    assert "Лира" not in authority.present_character_names
    assert authority.addressed_response_obligation is None

def test_address_repair_colocated_null_scene_uses_player_or_unstructured_bag():
    """#187/#188 bypass: kitchen scene_location_links null must not block co-locate."""
    kitchen = uuid4()
    hall = uuid4()
    # Unstructured scene: fall back to PC location.
    assert _address_repair_colocated(
        scene_location_id=None,
        character_location_id=kitchen,
        player_location_id=kitchen,
    )
    assert not _address_repair_colocated(
        scene_location_id=None,
        character_location_id=hall,
        player_location_id=kitchen,
    )
    # Live kitchen bag: scene + PC + addressee all lack structured location.
    assert _address_repair_colocated(
        scene_location_id=None,
        character_location_id=None,
        player_location_id=None,
    )
    # Live residual after #188: PC already placed, named addressee row still unplaced,
    # scene links null — must still share the unstructured bag.
    assert _address_repair_colocated(
        scene_location_id=None,
        character_location_id=None,
        player_location_id=kitchen,
    )
    # Do not invent an unplaced NPC into a placed scene.
    assert not _address_repair_colocated(
        scene_location_id=kitchen,
        character_location_id=None,
        player_location_id=kitchen,
    )
    # str/UUID normalization
    assert _address_repair_colocated(
        scene_location_id=str(kitchen),
        character_location_id=kitchen,
        player_location_id=None,
    )


@pytest.mark.asyncio
async def test_null_scene_location_colocated_lira_stamps_obligation(monkeypatch):
    """Live bypass after #187: kitchen scene has null location_id; Лира shares PC place.

    Participant list is stale (Лира missing). Without player-location fallback, obligation
    never stamps and rival Управляющая hiring prose can publish.
    """
    async def _no_lines(*_args, **_kwargs):
        return []

    async def _no_subjects(*_args, **_kwargs):
        return []

    monkeypatch.setattr(
        "app.services.turn_authority_service.established_state_lines",
        _no_lines,
    )
    monkeypatch.setattr(
        "app.services.turn_authority_service.established_subjects",
        _no_subjects,
    )
    campaign_id = uuid4()
    hero_id = uuid4()
    lira_id = uuid4()
    steward_id = uuid4()
    scene_id = uuid4()
    location_id = uuid4()

    session = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(context_snapshot=None)))
    service = TurnAuthorityService.__new__(TurnAuthorityService)
    service._session = session
    service._campaigns = SimpleNamespace(
        get_by_id=AsyncMock(return_value=SimpleNamespace(player_character_id=hero_id))
    )
    hero = SimpleNamespace(id=hero_id, canonical_name="Эйдан", current_location_id=location_id)
    lira = SimpleNamespace(id=lira_id, canonical_name="Лира", current_location_id=location_id)
    steward = SimpleNamespace(
        id=steward_id, canonical_name="Управляющая домом", current_location_id=location_id
    )
    by_id = {hero_id: hero, lira_id: lira, steward_id: steward}
    service._entities = SimpleNamespace(
        get_character=AsyncMock(side_effect=lambda value: by_id.get(value)),
        list_by_campaign=AsyncMock(
            return_value=[
                _entity(hero_id, "Эйдан"),
                _entity(lira_id, "Лира"),
                _entity(steward_id, "Управляющая домом"),
            ]
        ),
    )
    # Kitchen scene: no structured location link (null location_id).
    state = _scene(scene_id, None, ["Эйдан", "Управляющая домом"])
    service._scene_state = SimpleNamespace(get=AsyncMock(return_value=state))

    plan = CoordinatedTurnPlan(
        player_intent="Спросить Лиру где хлеб",
        resolution="conversation",
        addressed_response_requested=True,
        response_ownership_reason="Игрок прямо называет Лиру.",
    )
    authority = await service.build(
        campaign_id=campaign_id,
        trigger_turn_id=uuid4(),
        player_input="Лира, где здесь хлеб? Ответь коротко именно ты, Лира.",
        source_scene_id=scene_id,
        target_scene_id=scene_id,
        plan=plan,
        acting_character_id=None,
    )
    assert "Лира" in authority.present_character_names
    assert authority.addressed_response_obligation == "Лира"
    assert authority.acting_character_id == lira_id


@pytest.mark.asyncio
async def test_unstructured_null_location_kitchen_bag_stamps_obligation(monkeypatch):
    """When scene/PC/Лира all lack location ids, unique address still stamps obligation."""
    async def _no_lines(*_args, **_kwargs):
        return []

    async def _no_subjects(*_args, **_kwargs):
        return []

    monkeypatch.setattr(
        "app.services.turn_authority_service.established_state_lines",
        _no_lines,
    )
    monkeypatch.setattr(
        "app.services.turn_authority_service.established_subjects",
        _no_subjects,
    )
    campaign_id = uuid4()
    hero_id = uuid4()
    lira_id = uuid4()
    scene_id = uuid4()

    session = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(context_snapshot=None)))
    service = TurnAuthorityService.__new__(TurnAuthorityService)
    service._session = session
    service._campaigns = SimpleNamespace(
        get_by_id=AsyncMock(return_value=SimpleNamespace(player_character_id=hero_id))
    )
    hero = SimpleNamespace(id=hero_id, canonical_name="Эйдан", current_location_id=None)
    lira = SimpleNamespace(id=lira_id, canonical_name="Лира", current_location_id=None)
    by_id = {hero_id: hero, lira_id: lira}
    service._entities = SimpleNamespace(
        get_character=AsyncMock(side_effect=lambda value: by_id.get(value)),
        list_by_campaign=AsyncMock(
            return_value=[_entity(hero_id, "Эйдан"), _entity(lira_id, "Лира")]
        ),
    )
    state = _scene(scene_id, None, ["Эйдан"])
    service._scene_state = SimpleNamespace(get=AsyncMock(return_value=state))

    plan = CoordinatedTurnPlan(
        player_intent="Спросить Лиру",
        resolution="conversation",
        addressed_response_requested=True,
    )
    authority = await service.build(
        campaign_id=campaign_id,
        trigger_turn_id=uuid4(),
        player_input="Лира, где здесь хлеб?",
        source_scene_id=scene_id,
        target_scene_id=scene_id,
        plan=plan,
        acting_character_id=None,
    )
    assert "Лира" in authority.present_character_names
    assert authority.addressed_response_obligation == "Лира"

@pytest.mark.asyncio
async def test_null_scene_pc_placed_unplaced_lira_stamps_obligation(monkeypatch):
    """Live bypass after #188: scene null, PC has location, Лира row unplaced.

    Obligation must stamp and sticky EN Housekeeper must not keep acting ownership.
    """
    async def _no_lines(*_args, **_kwargs):
        return []

    async def _no_subjects(*_args, **_kwargs):
        return []

    monkeypatch.setattr(
        "app.services.turn_authority_service.established_state_lines",
        _no_lines,
    )
    monkeypatch.setattr(
        "app.services.turn_authority_service.established_subjects",
        _no_subjects,
    )
    campaign_id = uuid4()
    hero_id = uuid4()
    lira_id = uuid4()
    housekeeper_id = uuid4()
    scene_id = uuid4()
    location_id = uuid4()

    user_row = SimpleNamespace(
        context_snapshot=json.dumps(
            {
                "input_routing": {
                    "addressed_character_id": str(housekeeper_id),
                    "planner_bypass": False,
                    "user_actor": "player_character",
                }
            }
        )
    )
    session = SimpleNamespace(get=AsyncMock(return_value=user_row))
    service = TurnAuthorityService.__new__(TurnAuthorityService)
    service._session = session
    service._campaigns = SimpleNamespace(
        get_by_id=AsyncMock(return_value=SimpleNamespace(player_character_id=hero_id))
    )
    hero = SimpleNamespace(id=hero_id, canonical_name="Эйдан", current_location_id=location_id)
    lira = SimpleNamespace(id=lira_id, canonical_name="Лира", current_location_id=None)
    housekeeper = SimpleNamespace(
        id=housekeeper_id, canonical_name="Housekeeper", current_location_id=location_id
    )
    by_id = {hero_id: hero, lira_id: lira, housekeeper_id: housekeeper}
    service._entities = SimpleNamespace(
        get_character=AsyncMock(side_effect=lambda value: by_id.get(value)),
        list_by_campaign=AsyncMock(
            return_value=[
                _entity(hero_id, "Эйдан"),
                _entity(lira_id, "Лира"),
                _entity(housekeeper_id, "Housekeeper"),
            ]
        ),
    )
    # Stale participants include invented EN twin; Лира missing.
    state = _scene(scene_id, None, ["Эйдан", "Housekeeper", "Управляющая домом"])
    service._scene_state = SimpleNamespace(get=AsyncMock(return_value=state))

    plan = CoordinatedTurnPlan(
        player_intent="Спросить Лиру",
        resolution="conversation",
        addressed_response_requested=True,
    )
    authority = await service.build(
        campaign_id=campaign_id,
        trigger_turn_id=uuid4(),
        player_input="Лира, где здесь хлеб? Ответь коротко именно ты, Лира.",
        source_scene_id=scene_id,
        target_scene_id=scene_id,
        plan=plan,
        acting_character_id=None,
    )
    assert "Лира" in authority.present_character_names
    assert authority.addressed_response_obligation == "Лира"
    assert authority.acting_character_id == lira_id
    assert authority.acting_character_name == "Лира"


@pytest.mark.asyncio
async def test_named_campaign_addressee_clears_sticky_housekeeper_when_unpromotable(
    monkeypatch,
):
    """If Лира cannot be promoted (other location), sticky Housekeeper still must clear."""
    async def _no_lines(*_args, **_kwargs):
        return []

    async def _no_subjects(*_args, **_kwargs):
        return []

    monkeypatch.setattr(
        "app.services.turn_authority_service.established_state_lines",
        _no_lines,
    )
    monkeypatch.setattr(
        "app.services.turn_authority_service.established_subjects",
        _no_subjects,
    )
    campaign_id = uuid4()
    hero_id = uuid4()
    lira_id = uuid4()
    housekeeper_id = uuid4()
    scene_id = uuid4()
    kitchen_id = uuid4()
    hall_id = uuid4()

    user_row = SimpleNamespace(
        context_snapshot=json.dumps(
            {
                "input_routing": {
                    "addressed_character_id": str(housekeeper_id),
                    "planner_bypass": False,
                    "user_actor": "player_character",
                }
            }
        )
    )
    session = SimpleNamespace(get=AsyncMock(return_value=user_row))
    service = TurnAuthorityService.__new__(TurnAuthorityService)
    service._session = session
    service._campaigns = SimpleNamespace(
        get_by_id=AsyncMock(return_value=SimpleNamespace(player_character_id=hero_id))
    )
    hero = SimpleNamespace(id=hero_id, canonical_name="Эйдан", current_location_id=kitchen_id)
    lira = SimpleNamespace(id=lira_id, canonical_name="Лира", current_location_id=hall_id)
    housekeeper = SimpleNamespace(
        id=housekeeper_id, canonical_name="Housekeeper", current_location_id=kitchen_id
    )
    by_id = {hero_id: hero, lira_id: lira, housekeeper_id: housekeeper}
    service._entities = SimpleNamespace(
        get_character=AsyncMock(side_effect=lambda value: by_id.get(value)),
        list_by_campaign=AsyncMock(
            return_value=[
                _entity(hero_id, "Эйдан"),
                _entity(lira_id, "Лира"),
                _entity(housekeeper_id, "Housekeeper"),
            ]
        ),
    )
    state = _scene(scene_id, kitchen_id, ["Эйдан", "Housekeeper"])
    service._scene_state = SimpleNamespace(get=AsyncMock(return_value=state))

    plan = CoordinatedTurnPlan(
        player_intent="Спросить Лиру",
        resolution="conversation",
        addressed_response_requested=True,
    )
    authority = await service.build(
        campaign_id=campaign_id,
        trigger_turn_id=uuid4(),
        player_input="Лира, где здесь хлеб?",
        source_scene_id=scene_id,
        target_scene_id=scene_id,
        plan=plan,
        acting_character_id=None,
    )
    assert "Лира" not in authority.present_character_names
    assert authority.addressed_response_obligation is None
    assert authority.acting_character_id is None
    assert authority.acting_character_name is None

