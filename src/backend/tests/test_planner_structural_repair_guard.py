import asyncio
from uuid import uuid4

from app.models.turn import ChatMessage
from app.services.planner_structural_repair_guard import (
    generate_with_structural_repair,
    normalize_structured_destinations,
    structured_time_contract_issues,
)
from app.services.turn_authority_planner import CoordinatedTurnPlan
from app.services.turn_planner import (
    ActionSequencePlan,
    ActionStepPlan,
    SceneTransitionPlan,
)


def _context(item_id=None, *, exits="none recorded", location="Комната Кая"):
    owned = f"латунный ключ [id={item_id}]" if item_id else "none recorded"
    return [
        ChatMessage(
            role="system",
            content=(
                "[AUTHORITATIVE SCENE STATE]\n"
                f"Location path: {location}\n"
                "Objects physically here: none recorded\n"
                f"Available exits: {exits}\n"
                "[STRUCTURED ACTION REFERENCES]\n"
                f"Player-owned items: {owned}\n"
                "Physically present characters: none recorded\n"
            ),
        ),
        ChatMessage(
            role="user",
            content="Я аккуратно кладу латунный ключ на рабочий стол и убираю руку.",
        ),
    ]


def _inventory_plan(item_id, operation):
    return CoordinatedTurnPlan(
        player_intent="Кладу латунный ключ на рабочий стол.",
        resolution="sequence",
        action_sequence=ActionSequencePlan(
            steps=[
                ActionStepPlan(
                    action_type="inventory",
                    intent="Кладу латунный ключ на рабочий стол.",
                    resolution="auto_success",
                    safe_mundane=True,
                    observable_outcome="Латунный ключ лежит на рабочем столе.",
                    item_id=item_id,
                    inventory_operation=operation,
                )
            ]
        ),
    )


def _interaction_plan():
    return CoordinatedTurnPlan(
        player_intent="Взаимодействую с предметом.",
        resolution="sequence",
        action_sequence=ActionSequencePlan(
            steps=[
                ActionStepPlan(
                    action_type="interaction",
                    intent="Взаимодействую с предметом.",
                    resolution="auto_success",
                    observable_outcome="Действие завершено.",
                )
            ]
        ),
    )


def _rest_plan(*, structured_time):
    transition = (
        SceneTransitionPlan(
            required=True,
            transition_type="time_transition",
            elapsed_time="8 часов",
            time_after="Утро",
        )
        if structured_time
        else SceneTransitionPlan()
    )
    return CoordinatedTurnPlan(
        player_intent="Сплю восемь часов и просыпаюсь утром.",
        resolution="sequence",
        action_sequence=ActionSequencePlan(
            steps=[
                ActionStepPlan(
                    action_type="rest",
                    intent="Сплю восемь часов.",
                    resolution="auto_success",
                    observable_outcome="После сна наступает утро.",
                    transition=transition,
                )
            ]
        ),
    )


def _movement_plan(destination):
    return CoordinatedTurnPlan(
        player_intent="Перехожу в другую комнату.",
        resolution="sequence",
        action_sequence=ActionSequencePlan(
            steps=[
                ActionStepPlan(
                    action_type="movement",
                    intent="Перехожу в другую комнату.",
                    resolution="auto_success",
                    transition=SceneTransitionPlan(
                        required=True,
                        transition_type="location_transition",
                        destination_location=destination,
                    ),
                )
            ]
        ),
    )


def test_invalid_movement_schema_gets_one_generation_retry():
    item_id = uuid4()
    calls = []

    async def fake_generate(_planner, _selection, messages):
        calls.append(messages)
        if len(calls) == 1:
            raise ValueError(
                "auto-success movement steps require an explicit location_transition"
            )
        return _inventory_plan(item_id, "place")

    result = asyncio.run(
        generate_with_structural_repair(None, None, _context(item_id), fake_generate)
    )

    assert result.action_sequence.steps[0].inventory_operation == "place"
    assert len(calls) == 2
    assert "[STRUCTURED ACTION TYPE REPAIR]" in calls[1][-1].content
    assert "Local body motion does not use action_type=movement" in calls[1][-1].content


def test_incomplete_inventory_metadata_gets_schema_retry():
    calls = []

    async def fake_generate(_planner, _selection, messages):
        calls.append(messages)
        if len(calls) == 1:
            raise ValueError(
                "completed inventory steps require item_id and inventory_operation"
            )
        return _interaction_plan()

    result = asyncio.run(
        generate_with_structural_repair(None, None, _context(), fake_generate)
    )

    assert result.action_sequence.steps[0].action_type == "interaction"
    assert len(calls) == 2
    assert "[STRUCTURED INVENTORY FIELD REPAIR]" in calls[1][-1].content
    assert "non-inventory interaction" in calls[1][-1].content


def test_owned_take_is_repaired_before_semantic_review():
    item_id = uuid4()
    calls = []

    async def fake_generate(_planner, _selection, messages):
        calls.append(messages)
        if len(calls) == 1:
            return _inventory_plan(item_id, "take")
        return _inventory_plan(item_id, "place")

    result = asyncio.run(
        generate_with_structural_repair(None, None, _context(item_id), fake_generate)
    )

    assert result.action_sequence.steps[0].inventory_operation == "place"
    assert len(calls) == 2
    assert "[DETERMINISTIC INVENTORY CONTRACT REJECTION]" in calls[1][-1].content
    assert "already player-owned" in calls[1][-1].content


def test_completed_rest_requires_structured_time_transition():
    issues = structured_time_contract_issues(_rest_plan(structured_time=False))

    assert len(issues) == 1
    assert "auto_success rest" in issues[0]
    assert "time_transition" in issues[0]
    assert structured_time_contract_issues(_rest_plan(structured_time=True)) == []


def test_rest_without_time_transition_is_repaired_before_semantic_review():
    calls = []

    async def fake_generate(_planner, _selection, messages):
        calls.append(messages)
        return _rest_plan(structured_time=len(calls) > 1)

    result = asyncio.run(
        generate_with_structural_repair(None, None, _context(), fake_generate)
    )

    transition = result.action_sequence.steps[0].transition
    assert transition.transition_type == "time_transition"
    assert transition.time_after == "Утро"
    assert len(calls) == 2
    assert "[DETERMINISTIC TIME CONTRACT REJECTION]" in calls[1][-1].content


def test_known_exit_prefix_strips_planner_description_from_destination():
    plan = _movement_plan(
        "Коридор — Небольшой коридор с несколькими дверьми, освещенный слабым светом."
    )

    normalize_structured_destinations(
        plan,
        _context(exits="дверь -> Коридор"),
    )

    assert plan.action_sequence.steps[0].transition.destination_location == "Коридор"


def test_current_location_suffix_is_removed_from_compound_destination():
    plan = _movement_plan("Контора — Комната Кая")

    normalize_structured_destinations(plan, _context(location="Комната Кая"))

    assert plan.action_sequence.steps[0].transition.destination_location == "Контора"


def test_unanchored_dashed_location_name_is_left_untouched():
    plan = _movement_plan("Дом — Подвал")

    normalize_structured_destinations(plan, _context(location="Площадь"))

    assert plan.action_sequence.steps[0].transition.destination_location == "Дом — Подвал"
