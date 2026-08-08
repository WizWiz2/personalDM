from datetime import datetime
from uuid import uuid4

import pytest

from app.models.provider_config import ProviderConfigRead
from app.models.turn import ChatMessage
from app.services.role_model_router import ModelRole, RoleModelSelection
from app.services.turn_authority_planner import TurnAuthorityPlanner


class _RepairingPlannerRouter:
    def __init__(self):
        self.calls = 0

    async def generate_json(self, provider, selection, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return {
                "player_intent": "Постучать и дождаться ответа.",
                "resolution": "observation",
                "scene_disposition": "stay",
                "npc_introductions": [],
                "observable_consequences": ["Стук разносится за дверью."],
                "character_beats": [],
                "canon_constraints": [],
                "new_fact_candidates": [],
                "narration_guidance": [],
                "ending_hook": "",
            }
        assert "[PLAN CONTRACT REPAIR]" in messages[-1].content
        return {
            "player_intent": "Постучать и дождаться ответа.",
            "resolution": "conversation",
            "scene_disposition": "stay",
            "npc_introductions": [
                {
                    "canonical_name": "Дежурный фабрики",
                    "role": "дежурный",
                    "description": "Сонный ночной дежурный.",
                    "appearance": "",
                    "voice": "",
                    "temporary_name": True,
                    "reason": "Ответил на прямой стук игрока.",
                }
            ],
            "observable_consequences": ["На стук дверь открывает Дежурный фабрики."],
            "character_beats": [],
            "canon_constraints": [],
            "new_fact_candidates": [],
            "narration_guidance": [],
            "ending_hook": "Дежурный ждёт вопроса.",
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
async def test_ambiguous_contact_plan_gets_targeted_contract_repair():
    router = _RepairingPlannerRouter()
    planner = TurnAuthorityPlanner(router)

    plan = await planner.plan(
        _selection(),
        [
            ChatMessage(role="system", content="[Current Scene: фабрика]"),
            ChatMessage(role="user", content="Подхожу к двери и трижды стучу."),
        ],
    )

    assert router.calls == 2
    assert [npc.canonical_name for npc in plan.npc_introductions] == [
        "Дежурный фабрики"
    ]
    assert "открывает" in plan.observable_consequences[0]
    assert TurnAuthorityPlanner.contract_issues(
        plan,
        "Подхожу к двери и трижды стучу.",
    ) == []
