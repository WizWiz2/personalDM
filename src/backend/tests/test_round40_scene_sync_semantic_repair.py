from datetime import datetime
from uuid import uuid4

import pytest

from app.models.provider_config import ProviderConfigRead
from app.models.turn import ChatMessage
from app.services.role_model_router import ModelRole, RoleModelSelection
from app.services.turn_authority_planner import TurnAuthorityPlanner


class _SceneSyncRouter:
    def __init__(self):
        self.plan_calls = 0
        self.review_calls = 0

    async def generate_json(self, provider, selection, messages, **kwargs):
        system = messages[0].content
        if "[TURN PLAN SEMANTIC REVIEWER]" in system:
            self.review_calls += 1
            if self.review_calls == 1:
                return {
                    "verdict": "repair_required",
                    "summary": "Перемещение потеряно в структуре.",
                    "issues": [
                        "Игрок явно идёт на Старую Марину, но plan оставляет stay без location_transition."
                    ],
                }
            return {
                "verdict": "pass",
                "summary": "Физическая граница сцены теперь типизирована.",
                "issues": [],
            }

        self.plan_calls += 1
        if self.plan_calls == 1:
            return {
                "player_intent": "Дойти до Старой Марины.",
                "resolution": "observation",
                "observable_consequences": ["Вера выходит из промышленного комплекса."],
                "npc_introductions": [],
                "addressed_response_requested": False,
            }

        assert "[PLAN SEMANTIC REPAIR]" in messages[-1].content
        return {
            "player_intent": "Дойти до Старой Марины.",
            "resolution": "transition",
            "scene_transition": {
                "required": True,
                "transition_type": "location_transition",
                "destination_location": "Старая Марина",
                "scene_title": "Старая Марина",
                "reason": "Игрок явно покидает текущую физическую локацию и идёт на Старую Марину.",
            },
            "observable_consequences": ["Вера добирается до Старой Марины."],
            "npc_introductions": [],
            "addressed_response_requested": False,
        }


def _selection():
    campaign_id = uuid4()
    config = ProviderConfigRead(
        id=uuid4(),
        campaign_id=campaign_id,
        base_url="http://localhost:11434/v1",
        model_name="fake-qwen",
        has_api_key=False,
        context_window=6144,
        created_at=datetime.utcnow(),
    )
    return RoleModelSelection(
        role=ModelRole.PLANNER,
        config=config,
        api_key=None,
        fallback_config=config,
        fallback_api_key=None,
        source="test",
    )


@pytest.mark.asyncio
async def test_committed_physical_travel_hidden_as_stay_is_repaired_before_narrator():
    router = _SceneSyncRouter()
    planner = TurnAuthorityPlanner(router)
    selection = _selection()
    context = [
        ChatMessage(
            role="system",
            content=(
                "[AUTHORITATIVE SCENE STATE]\n"
                "Scene: Промышленный комплекс (active)\n"
                "Location: Промышленный комплекс\n"
            ),
        ),
        ChatMessage(role="user", content="Иду в город, на Старую Марину."),
    ]
    base_messages = planner.planning_messages(context)
    player_input = planner._latest_user_text(context)

    rejected = await planner._generate_plan(selection, base_messages)
    first_review = await planner._semantic_review(selection, context, player_input, rejected)
    assert first_review.verdict == "repair_required"

    repaired = await planner._generate_plan(
        selection,
        planner._repair_messages(
            base_messages,
            player_input,
            first_review.issues,
            rejected,
        ),
    )
    final_review = await planner._semantic_review(selection, context, player_input, repaired)

    assert router.plan_calls == 2
    assert router.review_calls == 2
    assert final_review.verdict == "pass"
    assert repaired.scene_disposition == "location_transition"
    assert repaired.scene_transition.required is True
    assert repaired.scene_transition.transition_type == "location_transition"
    assert repaired.scene_transition.destination_location == "Старая Марина"
