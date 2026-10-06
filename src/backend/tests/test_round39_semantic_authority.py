from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

import app.services.systemless_authority_guard as systemless_guard
from app.models.turn_authority import TurnAuthority
from app.services.semantic_authority_guard import install
from app.services.turn_authority_planner import CoordinatedTurnPlan, TurnAuthorityPlanner


@pytest.fixture(autouse=True)
def semantic_runtime_policy():
    install()


def _plan(*, addressed_response_requested: bool = False):
    return CoordinatedTurnPlan.model_validate(
        {
            "player_intent": "Осмотреть ящик К-7",
            "resolution": "observation",
            "addressed_response_requested": addressed_response_requested,
            "response_ownership_reason": "Семантическое решение Planner.",
        }
    )


def _authority(player_input: str = "Осматриваю ящик К-7") -> TurnAuthority:
    return TurnAuthority(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_character_name="Алекс",
        player_input=player_input,
    )


def test_requires_check_is_rejected_by_typed_plan_schema():
    with pytest.raises(ValidationError):
        CoordinatedTurnPlan.model_validate(
            {
                "player_intent": "Осмотреть ящик К-7",
                "resolution": "sequence",
                "action_sequence": {
                    "steps": [
                        {
                            "action_type": "observation",
                            "intent": "Осмотреть ящик К-7",
                            "resolution": "requires_check",
                            "safe_mundane": False,
                            "transition": {"required": False},
                        }
                    ]
                },
            }
        )


def test_response_ownership_comes_from_typed_planner_field_not_question_mark_or_verbs():
    false_plan = _plan(addressed_response_requested=False)
    true_plan = _plan(addressed_response_requested=True)

    assert not systemless_guard.addressed_response_requested(
        "Кто выдаёт постановление?",
        false_plan,
    )
    assert systemless_guard.addressed_response_requested(
        "Открываю папку на столе.",
        true_plan,
    )


def test_sticky_talk_is_only_transport_context_until_planner_decides_semantics():
    assert systemless_guard.input_uses_addressed_character("Выхожу через заднюю дверь")
    assert systemless_guard.input_uses_addressed_character("Кто выдаёт постановление?")


def test_contact_semantics_do_not_autocreate_an_npc_behind_planner_back():
    plan = _plan()

    returned = TurnAuthorityPlanner.normalize_affirmative_direct_contact(
        plan,
        "Спрашиваю охранника, кто здесь главный",
    )

    assert returned is plan
    assert returned.npc_introductions == []


class _PassReviewRouter:
    async def resolve(self, *args, **kwargs):
        return SimpleNamespace(role="evaluator")

    async def generate_json(self, *args, **kwargs):
        return {"verdict": "pass", "summary": "Ложное нарушение снято.", "violations": []}


def test_planner_prompt_explicitly_assigns_semantics_to_model():
    contract = TurnAuthorityPlanner.AUTHORITY_ADDENDUM

    assert "[INTER-AGENT SEMANTIC AUTHORITY CONTRACT]" in contract
    assert "does NOT guess meaning" in contract
    assert "addressed_response_requested" in contract
    assert "requires_check` is NOT a legal output" in contract
    assert "verb lists" in contract
