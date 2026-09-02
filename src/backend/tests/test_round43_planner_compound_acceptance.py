from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.turn import ChatMessage
from app.services.planner_compound_guard import install as install_compound_guard
from app.services.turn_authority_planner import TurnAuthorityPlanner


def _plan(*destinations: str) -> dict:
    steps = [
        {
            "action_type": "movement",
            "intent": f"Перейти в {destination}",
            "resolution": "auto_success",
            "safe_mundane": True,
            "observable_outcome": f"Герой оказывается в {destination}.",
            "transition": {
                "required": True,
                "transition_type": "location_transition",
                "destination_location": destination,
                "scene_title": destination,
            },
        }
        for destination in destinations
    ]
    return {
        "player_intent": "Выйти из комнаты, спуститься в холл и затем пойти в контору.",
        "resolution": "sequence",
        "action_sequence": {
            "summary": "Последовательное перемещение по трём точкам.",
            "steps": steps,
        },
        "observable_consequences": ["Маршрут выполняется по порядку."],
        "canon_constraints": [],
        "new_fact_candidates": [],
        "narration_guidance": [],
        "character_beats": [],
        "npc_introductions": [],
        "addressed_response_requested": False,
    }


@pytest.mark.asyncio
@pytest.mark.interagent_contract_enforced
async def test_semantic_reviewer_repairs_dropped_compound_movement_step():
    install_compound_guard()
    router = SimpleNamespace()
    router.generate_json = AsyncMock(
        side_effect=[
            _plan("Холл"),
            {
                "verdict": "repair_required",
                "summary": "Потерян второй переход.",
                "issues": [
                    "Игрок после холла явно идёт в контору, но второй movement step отсутствует."
                ],
            },
            _plan("Холл", "Контора"),
            {"verdict": "pass", "summary": "", "issues": []},
        ]
    )
    planner = TurnAuthorityPlanner(router)
    user_input = "Выхожу из комнаты, спускаюсь в холл, потом иду в контору."
    context = [
        ChatMessage(role="system", content="Текущая сцена: комната над трактиром."),
        ChatMessage(role="user", content=user_input),
    ]

    result = await planner.plan(
        SimpleNamespace(),
        context,
        latest_user_input=user_input,
    )

    assert [
        step.transition.destination_location for step in result.action_sequence.steps
    ] == ["Холл", "Контора"]
    assert router.generate_json.await_count == 4

    first_prompt = router.generate_json.await_args_list[0].args[2][0].content
    review_prompt = router.generate_json.await_args_list[1].args[2][0].content
    repair_prompt = router.generate_json.await_args_list[2].args[2][-1].content
    assert "COMPOUND ACTION PRESERVATION" in first_prompt
    assert "COMPOUND COVERAGE REVIEW" in review_prompt
    assert "второй movement step отсутствует" in repair_prompt
