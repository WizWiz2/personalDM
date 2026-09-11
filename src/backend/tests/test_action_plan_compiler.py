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
    return SimpleNamespace(
        id=location_id,
        canonical_name=name,
        aliases=[],
    )


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


def _move(destination: str) -> PlayerActionIntent:
    return PlayerActionIntent(
        action_type="movement",
        intent=f"Переместиться в {destination}.",
        destination_location=destination,
    )


def _success(index: int) -> ActionOutcomeDecision:
    return ActionOutcomeDecision(
        action_index=index,
        resolution="auto_success",
        safe_mundane=True,
        observable_outcome="Переход завершён.",
    )


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
        actions=[_move("Коридор"), _move("Контора")],
    )
    decision = TurnOutcomeDecision(
        action_outcomes=[_success(0), _success(1)],
    )

    plan = await compiler.compile(CAMPAIGN, contract, decision)

    assert [
        step.transition.destination_location for step in plan.action_sequence.steps
    ] == ["Коридор", "Контора"]
    assert all(step.resolution == "auto_success" for step in plan.action_sequence.steps)
    assert plan.scene_transition.sequence_payload["_authority_source"] == "player_intent_compiler"


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
        actions=[_move("Коридор"), _move("Склад")],
    )
    decision = TurnOutcomeDecision(
        action_outcomes=[_success(0), _success(1)],
    )

    plan = await compiler.compile(CAMPAIGN, contract, decision)
    first, second = plan.action_sequence.steps

    assert first.resolution == "auto_success"
    assert first.transition.destination_location == "Коридор"
    assert second.resolution == "blocked"
    assert second.transition.required is False
    assert "not an available exit" in (second.blocking_reason or "")


@pytest.mark.asyncio
async def test_new_explicit_destination_is_the_only_route_discovery_step() -> None:
    compiler = _Compiler(
        [_location(ROOM, "Комната Кая")],
        {ROOM: []},
    )
    contract = PlayerIntentContract(
        summary="Кай идёт в соседнюю круглосуточную прачечную.",
        actions=[
            PlayerActionIntent(
                action_type="movement",
                intent="Идти в круглосуточную прачечную соседнего дома.",
                destination_location="Круглосуточная прачечная соседнего дома",
                allow_route_discovery=True,
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
            )
        ],
    )

    plan = await compiler.compile(CAMPAIGN, contract, decision)
    payload = plan.scene_transition.sequence_payload

    assert payload["_route_discovery_steps"] == [0]
    assert plan.action_sequence.steps[0].transition.destination_location == (
        "Круглосуточная прачечная соседнего дома"
    )
    assert "DESTINATION PROFILE:" in (
        plan.action_sequence.steps[0].transition.bridge_summary or ""
    )
