from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

from app.models.player_intent import (
    ActionOutcomeDecision,
    PlayerActionIntent,
    PlayerIntentContract,
    TurnOutcomeDecision,
)
from app.services.action_plan_compiler import ActionPlanCompiler

ROOM = UUID("00000000-0000-4000-8000-000000000101")
CORRIDOR = UUID("00000000-0000-4000-8000-000000000102")
OFFICE = UUID("00000000-0000-4000-8000-000000000103")
WAREHOUSE = UUID("00000000-0000-4000-8000-000000000104")
SCENE = UUID("00000000-0000-4000-8000-000000000201")
CAMPAIGN = UUID("00000000-0000-4000-8000-000000000301")


def _location(location_id: UUID, name: str):
    return SimpleNamespace(id=location_id, canonical_name=name, aliases=[], parent_location_id=None)


def _exit(source: UUID, target: UUID, target_name: str):
    return SimpleNamespace(
        from_location_id=source,
        to_location_id=target,
        to_location_name=target_name,
        label=target_name,
        active=True,
        discovered=True,
        access_rule=None,
    )


class _FakeState:
    def __init__(self, exits):
        self._exits = exits

    async def list_exits(self, campaign_id, from_location_id, *, include_hidden=False):
        del campaign_id, include_hidden
        return list(self._exits.get(from_location_id, []))


class _Compiler(ActionPlanCompiler):
    def __init__(self, locations, exits):
        self._session = None
        self._locations = None
        self._state = _FakeState(exits)
        self._fixture_locations = locations

    async def _world(self, campaign_id):
        del campaign_id
        return SCENE, SimpleNamespace(location_id=ROOM), list(self._fixture_locations)


@pytest.mark.asyncio
async def test_conditional_route_and_explicit_contact_keep_external_resolution():
    route = _exit(ROOM, CORRIDOR, "Коридор")
    route.access_rule = "Only with permission"
    compiler = _Compiler(
        [_location(ROOM, "Комната"), _location(CORRIDOR, "Коридор")], {ROOM: [route]}
    )
    contract = PlayerIntentContract(summary="Иду в коридор.", actions=[_move("Коридор", CORRIDOR)])
    assert await compiler.resolve_known_travel(CAMPAIGN, contract) is None
    route.access_rule = None
    contract.actions[0].movement_method = "special"
    assert await compiler.resolve_known_travel(CAMPAIGN, contract) is None
    contract.actions[0].movement_method = "ordinary"
    contract.addressed_response_requested = True
    assert await compiler.resolve_known_travel(CAMPAIGN, contract) is None


@pytest.mark.asyncio
async def test_unknown_destination_still_requires_world_resolution():
    compiler = _Compiler([_location(ROOM, "Комната")], {})
    contract = PlayerIntentContract(summary="Иду в сад.", actions=[_move("Сад")])
    assert await compiler.resolve_known_travel(CAMPAIGN, contract) is None


def _move(destination: str, location_id: UUID | None = None) -> PlayerActionIntent:
    return PlayerActionIntent(
        action_type="movement",
        intent=f"Переместиться в {destination}.",
        destination_location=destination,
        destination_location_id=location_id,
    )


def _success(index: int) -> ActionOutcomeDecision:
    return ActionOutcomeDecision(
        action_index=index,
        resolution="auto_success",
        safe_mundane=True,
        observable_outcome="Переход завершён.",
    )


@pytest.mark.asyncio
async def test_same_location_request_does_not_block_following_actions():
    compiler = _Compiler([_location(ROOM, "Мастерская")], {})
    contract = PlayerIntentContract(summary="Вхожу в мастерскую и смотрю стол.", actions=[
        _move("Мастерская", ROOM), PlayerActionIntent(action_type="observation", intent="Осмотреть стол."),
    ])
    plan = await compiler.compile(CAMPAIGN, contract, TurnOutcomeDecision(action_outcomes=[
        _success(0), ActionOutcomeDecision(
            action_index=1, resolution="auto_success", observable_outcome="На столе пустая катушка.",
        ),
    ]))
    assert all(step.resolution == "auto_success" for step in plan.action_sequence.steps)
    assert not plan.action_sequence.steps[0].transition.required
    assert "уже находишься" in plan.action_sequence.steps[0].observable_outcome


@pytest.mark.asyncio
async def test_approved_companion_survives_movement_compilation():
    compiler = _Compiler(
        [_location(ROOM, "Комната"), _location(CORRIDOR, "Коридор")],
        {ROOM: [_exit(ROOM, CORRIDOR, "Коридор")]},
    )
    action = _move("Коридор", CORRIDOR)
    action.requested_companions = ["Валерьян"]
    contract = PlayerIntentContract(summary="Следую за Валерьяном.", actions=[action])
    assert await compiler.resolve_known_travel(CAMPAIGN, contract) is None
    outcome = _success(0).model_copy(update={"carry_participants": ["Валерьян"]})
    plan = await compiler.compile(
        CAMPAIGN, contract, TurnOutcomeDecision(action_outcomes=[outcome])
    )
    assert plan.action_sequence.steps[0].transition.carry_participants == ["Валерьян"]


@pytest.mark.asyncio
async def test_compound_route_is_compiled_from_each_virtual_intermediate_location() -> None:
    compiler = _Compiler(
        [
            _location(ROOM, "Комната Кая"),
            _location(CORRIDOR, "Коридор"),
            _location(OFFICE, "Контора"),
        ],
        {
            ROOM: [_exit(ROOM, CORRIDOR, "Коридор")],
            CORRIDOR: [_exit(CORRIDOR, ROOM, "Комната Кая"), _exit(CORRIDOR, OFFICE, "Контора")],
        },
    )
    contract = PlayerIntentContract(
        summary="Кай идёт из комнаты через коридор в контору.",
        actions=[_move("Коридор", CORRIDOR), _move("Контора", OFFICE)],
    )
    decision = await compiler.resolve_known_travel(CAMPAIGN, contract)
    assert decision is not None
    assert decision.npc_introductions == []

    plan = await compiler.compile(CAMPAIGN, contract, decision)

    assert [step.transition.destination_location for step in plan.action_sequence.steps] == [
        "Коридор",
        "Контора",
    ]
    assert all(step.resolution == "auto_success" for step in plan.action_sequence.steps)
    assert plan.scene_transition.sequence_payload["_authority_source"] == "player_intent_compiler"
    assert plan.scene_transition.sequence_payload["_route_discovery_steps"] == []


@pytest.mark.asyncio
async def test_missing_second_edge_blocks_that_hop_without_erasing_completed_prefix() -> None:
    compiler = _Compiler(
        [
            _location(ROOM, "Комната Кая"),
            _location(CORRIDOR, "Коридор"),
            _location(WAREHOUSE, "Склад"),
        ],
        {
            ROOM: [_exit(ROOM, CORRIDOR, "Коридор")],
            CORRIDOR: [_exit(CORRIDOR, ROOM, "Комната Кая")],
        },
    )
    contract = PlayerIntentContract(
        summary="Кай выходит в коридор и затем пытается пройти на склад.",
        actions=[_move("Коридор", CORRIDOR), _move("Склад", WAREHOUSE)],
    )
    decision = await compiler.resolve_known_travel(CAMPAIGN, contract)
    assert decision is not None

    plan = await compiler.compile(CAMPAIGN, contract, decision)
    first, second = plan.action_sequence.steps

    assert first.resolution == "auto_success"
    assert first.transition.destination_location == "Коридор"
    assert second.resolution == "blocked"
    assert second.transition.required is False
    assert "not reachable over known open routes" in (second.blocking_reason or "")


@pytest.mark.asyncio
async def test_known_place_over_open_routes_is_one_trip() -> None:
    compiler = _Compiler(
        [_location(ROOM, "Комната Кая"), _location(CORRIDOR, "Коридор"), _location(OFFICE, "Контора")],
        {
            ROOM: [_exit(ROOM, CORRIDOR, "Коридор")],
            CORRIDOR: [_exit(CORRIDOR, ROOM, "Комната Кая"), _exit(CORRIDOR, OFFICE, "Контора")],
        },
    )
    contract = PlayerIntentContract(summary="Иду в контору.", actions=[_move("Контора", OFFICE)])
    decision = await compiler.resolve_known_travel(CAMPAIGN, contract)
    plan = await compiler.compile(CAMPAIGN, contract, decision)
    (step,) = plan.action_sequence.steps
    assert step.resolution == "auto_success"
    assert step.transition.destination_location_id == OFFICE


@pytest.mark.asyncio
async def test_unknown_explicit_destination_becomes_one_route_discovery_step() -> None:
    compiler = _Compiler([_location(ROOM, "Комната Кая")], {ROOM: []})
    contract = PlayerIntentContract(
        summary="Кай идёт в соседнюю круглосуточную прачечную.",
        actions=[
            PlayerActionIntent(
                action_type="movement",
                intent="Идти в круглосуточную прачечную соседнего дома.",
                destination_location="Круглосуточная прачечная соседнего дома",
            )
        ],
    )
    profile = (
        "Небольшая круглосуточная прачечная занимает первый этаж соседнего дома. "
        "Вдоль стен стоят ряды обычных стиральных и сушильных машин, у входа есть стойка оплаты."
    )
    decision = TurnOutcomeDecision(
        action_outcomes=[
            ActionOutcomeDecision(
                action_index=0,
                resolution="auto_success",
                safe_mundane=True,
                observable_outcome="Кай добирается до прачечной.",
                destination_profile=profile,
                destination_name="Круглосуточная прачечная",
            ),
            _success(1),
        ],
    )
    contract.actions.append(_move("Комната Кая", ROOM))

    plan = await compiler.compile(CAMPAIGN, contract, decision)
    payload = plan.scene_transition.sequence_payload

    assert payload["_route_discovery_steps"] == [0]
    first, back = plan.action_sequence.steps
    # The name comes from the generated profile; containment from its typed flag.
    assert first.transition.destination_location == "Круглосуточная прачечная"
    assert first.transition.destination_parent_location is None
    assert "DESTINATION PROFILE:" in (first.transition.bridge_summary or "")
    # A hop after a place created this turn compiles from it instead of losing the origin.
    assert back.resolution == "auto_success"
    assert back.transition.destination_location_id == ROOM


@pytest.mark.asyncio
async def test_typed_time_advance_moves_the_scene_clock_with_the_first_act() -> None:
    compiler = _Compiler(
        [_location(ROOM, "Комната"), _location(CORRIDOR, "Коридор")],
        {ROOM: [_exit(ROOM, CORRIDOR, "Коридор")]},
    )
    trip = PlayerIntentContract(
        summary="Утром выхожу в коридор.", actions=[_move("Коридор", CORRIDOR)], time_advance="утром",
    )
    plan = await compiler.compile(CAMPAIGN, trip, TurnOutcomeDecision(action_outcomes=[_success(0)]))
    assert plan.action_sequence.steps[0].transition.time_after == "утром"
    look = PlayerIntentContract(
        summary="Утром осматриваю стол.", time_advance="утром",
        actions=[PlayerActionIntent(action_type="observation", intent="Осмотреть стол.")],
    )
    plan = await compiler.compile(CAMPAIGN, look, TurnOutcomeDecision(action_outcomes=[_success(0)]))
    transition = plan.action_sequence.steps[0].transition
    assert (transition.transition_type, transition.time_after) == ("time_transition", "утром")
