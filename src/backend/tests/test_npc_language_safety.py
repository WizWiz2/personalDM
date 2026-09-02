import pytest

from app.models.turn_authority import PlannedNpcIntroduction
from app.services.turn_authority_planner import CoordinatedTurnPlan, TurnAuthorityPlanner
from app.services.turn_authority_resolvers import (
    AuthorityResolutionError,
    NpcIntroductionResolver,
)
from app.services.turn_planner import TurnPlanningError


def _planner_plan(intro: PlannedNpcIntroduction) -> CoordinatedTurnPlan:
    return CoordinatedTurnPlan(
        player_intent="Поговорить с диспетчером.",
        resolution="conversation",
        npc_introductions=[intro],
        addressed_response_requested=True,
        response_ownership_reason="Игрок обращается к новому собеседнику.",
        observable_consequences=["Диспетчер отвечает на вопрос игрока."],
    )


def test_cjk_generated_npc_name_becomes_readable_role_identity_before_authority():
    intro = PlannedNpcIntroduction(
        canonical_name="未知调度员",
        role="диспетчер",
        description="Рабочий у трапа.",
        reason="Игрок обратился к нему и получил ответ.",
    )

    normalized = NpcIntroductionResolver.sanitize_introductions([intro])

    assert len(normalized) == 1
    assert normalized[0].canonical_name == "Диспетчер"
    assert normalized[0].temporary_name is True
    assert normalized[0].canonical_name != "Безымянный собеседник"


def test_synthetic_placeholder_uses_role_instead_of_becoming_canon():
    intro = PlannedNpcIntroduction(
        canonical_name="Безымянный собеседник",
        role="портовый диспетчер",
        reason="Игрок получил прямой ответ.",
        temporary_name=True,
    )

    normalized = NpcIntroductionResolver.sanitize_introductions([intro])

    assert normalized[0].canonical_name == "Портовый диспетчер"
    assert normalized[0].temporary_name is True


def test_unreadable_name_without_readable_role_fails_closed():
    intro = PlannedNpcIntroduction(
        canonical_name="未知调度员",
        role="未知角色",
        reason="Игрок получил прямой ответ.",
    )

    with pytest.raises(AuthorityResolutionError):
        NpcIntroductionResolver.sanitize_introductions([intro])


def test_planner_itself_never_synthesizes_unnamed_interlocutor():
    plan = _planner_plan(
        PlannedNpcIntroduction(
            canonical_name="未知调度员",
            role="портовый диспетчер",
            reason="Игрок получил прямой ответ.",
        )
    )

    TurnAuthorityPlanner._sanitize_npc_names(plan, "Я спрашиваю диспетчера о грузе.")

    assert plan.npc_introductions[0].canonical_name == "Портовый диспетчер"
    assert plan.npc_introductions[0].temporary_name is True
    assert plan.npc_introductions[0].canonical_name != "Безымянный собеседник"


def test_planner_rejects_unreadable_identity_instead_of_inventing_placeholder():
    plan = _planner_plan(
        PlannedNpcIntroduction(
            canonical_name="未知调度员",
            role="未知角色",
            reason="Игрок получил прямой ответ.",
        )
    )

    with pytest.raises(TurnPlanningError, match="without a usable role"):
        TurnAuthorityPlanner._sanitize_npc_names(
            plan,
            "Я спрашиваю диспетчера о грузе.",
        )
