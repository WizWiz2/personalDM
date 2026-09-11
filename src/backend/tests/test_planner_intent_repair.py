from datetime import datetime
from uuid import uuid4

import pytest

from app.models.provider_config import ProviderConfigRead
from app.models.turn import ChatMessage
from app.services.role_model_router import ModelRole, RoleModelSelection
from app.services.npc_identity_binding import IdentityBindingDecision
from app.services.turn_authority_planner import (
    CoordinatedTurnPlan,
    SemanticPlanReview,
    TurnAuthorityPlanner,
)

pytestmark = pytest.mark.interagent_contract_enforced


class _RepairingPlannerRouter:
    def __init__(self):
        self.plan_calls = 0
        self.review_calls = 0

    async def generate_json(self, provider, selection, messages, *, response_model, **kwargs):
        if response_model is IdentityBindingDecision:
            return {
                'designation_kind': 'description', 'binding_source': 'planned_outcome',
                'introduction_index': 0, 'participation': 'encountered',
                'designation': 'role_reference', 'encounter_source': 'planned_outcome',
                'encounter_evidence': 'На стук дверь открывает Дежурный фабрики.',
                'designation_source': 'planned_outcome',
                'designation_evidence': 'Дежурный фабрики',
                'reason': 'Роль относится к открывшему дверь человеку.',
            }
        if response_model.__name__ == 'PlanReviewAdjudication':
            return {'plan_valid': False, 'assessments': []}
        if response_model is SemanticPlanReview:
            self.review_calls += 1
            if self.review_calls == 1:
                return {
                    "verdict": "repair_required",
                    "summary": "Контакт разрешён положительно, но responder не типизирован.",
                    "issues": ["Положительный responder должен быть в npc_introductions."],
                }
            return {"verdict": "pass", "summary": "Исправлено.", "issues": []}

        assert response_model is CoordinatedTurnPlan
        self.plan_calls += 1
        if self.plan_calls == 1:
            return {
                "player_intent": "Постучать и дождаться ответа.",
                "resolution": "observation",
                "npc_introductions": [],
                "observable_consequences": ["Стук разносится за дверью."],
            }

        assert "[PLAN SEMANTIC REPAIR]" in messages[-1].content
        return {
            "player_intent": "Постучать и дождаться ответа.",
            "resolution": "conversation",
            "npc_introductions": [
                {
                    "canonical_name": "Дежурный фабрики",
                    "role": "дежурный",
                    "description": (
                        "Усталый ночной дежурный фабрики, который держится настороженно и "
                        "не спешит впускать незнакомцев внутрь."
                    ),
                    "appearance": (
                        "Коренастый мужчина около пятидесяти лет с небритым лицом, короткими "
                        "седыми волосами и тёмной рабочей курткой поверх выцветшей рубашки."
                    ),
                    "voice": "Низкий хрипловатый голос; говорит коротко и сонно.",
                    "temporary_name": True,
                    "reason": "Ответил на прямой стук игрока.",
                }
            ],
            "observable_consequences": ["На стук дверь открывает Дежурный фабрики."],
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
async def test_ambiguous_contact_plan_gets_targeted_semantic_repair():
    router = _RepairingPlannerRouter()
    planner = TurnAuthorityPlanner(router)

    plan = await planner.plan(
        _selection(),
        [
            ChatMessage(role="system", content="[Current Scene: фабрика]"),
            ChatMessage(role="user", content="Подхожу к двери и трижды стучу."),
        ],
    )

    assert router.plan_calls == 2
    assert router.review_calls == 2
    assert [npc.canonical_name for npc in plan.npc_introductions] == ["Дежурный"]
    assert plan.npc_introductions[0].role == "дежурный"
    assert plan.npc_introductions[0].temporary_name is True
    assert "открывает" in plan.observable_consequences[0]
