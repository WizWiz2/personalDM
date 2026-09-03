import asyncio
from uuid import uuid4

from app.models.turn import ChatMessage
from app.services.planner_structural_repair_guard import generate_with_structural_repair
from app.services.turn_authority_planner import CoordinatedTurnPlan
from app.services.turn_planner import ActionSequencePlan, ActionStepPlan


def _context(item_id):
    return [
        ChatMessage(
            role="system",
            content=(
                "[AUTHORITATIVE SCENE STATE]\n"
                "Objects physically here: none recorded\n"
                "[STRUCTURED ACTION REFERENCES]\n"
                f"Player-owned items: латунный ключ [id={item_id}]\n"
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
        generate_with_structural_repair(
            None,
            None,
            _context(item_id),
            fake_generate,
        )
    )

    assert result.action_sequence.steps[0].inventory_operation == "place"
    assert len(calls) == 2
    assert "[STRUCTURED ACTION TYPE REPAIR]" in calls[1][-1].content
    assert "Local body motion does not use action_type=movement" in calls[1][-1].content


def test_owned_take_is_repaired_before_semantic_review():
    item_id = uuid4()
    calls = []

    async def fake_generate(_planner, _selection, messages):
        calls.append(messages)
        if len(calls) == 1:
            return _inventory_plan(item_id, "take")
        return _inventory_plan(item_id, "place")

    result = asyncio.run(
        generate_with_structural_repair(
            None,
            None,
            _context(item_id),
            fake_generate,
        )
    )

    assert result.action_sequence.steps[0].inventory_operation == "place"
    assert len(calls) == 2
    assert "[DETERMINISTIC INVENTORY CONTRACT REJECTION]" in calls[1][-1].content
    assert "already player-owned" in calls[1][-1].content
