from types import SimpleNamespace

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
        "player_intent": "Выйти из комнаты в холл и затем пойти в контору.",
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


class _CompoundRepairRouter:
    """Return typed fixtures by semantic role, not by incidental guard call order."""

    def __init__(self):
        self.plan_calls = 0
        self.review_calls = 0
        self.adjudication_calls = 0
        self.calls: list[tuple[str, list[ChatMessage]]] = []

    async def generate_json(
        self,
        provider,
        selection,
        messages,
        *,
        response_model,
        **kwargs,
    ):
        del provider, selection, kwargs
        name = response_model.__name__
        self.calls.append((name, list(messages)))

        if name == "CoordinatedTurnPlan":
            self.plan_calls += 1
            return _plan("Холл") if self.plan_calls == 1 else _plan("Холл", "Контора")

        if name == "SemanticPlanReview":
            self.review_calls += 1
            if self.review_calls == 1:
                return {
                    "verdict": "repair_required",
                    "summary": "Потерян второй переход.",
                    "issues": [
                        "Игрок после холла явно идёт в контору, но второй шаг перемещения отсутствует."
                    ],
                }
            return {"verdict": "pass", "summary": "План покрывает оба перехода.", "issues": []}

        if name == "PlanReviewAdjudication":
            self.adjudication_calls += 1
            return {"plan_valid": False, "assessments": []}

        # New deterministic/profile guards are allowed to add bounded descriptive metadata before
        # semantic review. They are not part of the behavior this regression is asserting.
        if name == "LocationProfilePatchSet":
            return {
                "patches": [
                    {
                        "transition_index": index,
                        "profile": (
                            f"{destination} — известная точка маршрута внутри текущего городского "
                            "района. Здесь достаточно пространства для обычного прохода; окружение "
                            "не добавляет угроз, препятствий или новых сюжетных событий само по себе."
                        ),
                    }
                    for index, destination in enumerate(("Холл", "Контора"))
                ]
            }
        if name in {"CompoundActionPatchSet", "NpcProfilePatchSet"}:
            return {"patches": []}
        if name == "NpcContactDecision":
            return {"outcome": "no_contact", "npc_introductions": []}

        raise AssertionError(f"Unexpected control response model: {name}")


@pytest.mark.asyncio
@pytest.mark.interagent_contract_enforced
async def test_semantic_reviewer_repairs_dropped_compound_movement_step():
    install_compound_guard()
    router = _CompoundRepairRouter()
    planner = TurnAuthorityPlanner(router)
    user_input = "Выхожу из комнаты в холл, потом иду в контору."
    context = [
        ChatMessage(
            role="system",
            content=(
                "Текущая сцена: комната над трактиром.\n"
                "Location path: Комната над трактиром\n"
                "Available exits: лестница -> Холл, проход -> Контора"
            ),
        ),
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
    assert router.plan_calls == 2
    assert router.review_calls == 2
    assert router.adjudication_calls == 1

    plan_messages = [messages for name, messages in router.calls if name == "CoordinatedTurnPlan"]
    review_messages = [messages for name, messages in router.calls if name == "SemanticPlanReview"]
    assert "COMPOUND ACTION PRESERVATION" in plan_messages[0][0].content
    assert "COMPOUND COVERAGE REVIEW" in review_messages[0][0].content
    assert "второй шаг перемещения отсутствует" in plan_messages[1][-1].content
