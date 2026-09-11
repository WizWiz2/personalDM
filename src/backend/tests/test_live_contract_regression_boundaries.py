from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.turn import ChatMessage
from app.models.turn_authority import PlannedNpcIntroduction
from app.services.live_contract_stabilization_guard import (
    _NPC_RECOVERY_BOUNDARY_CONTRACT,
    _SEMANTIC_BOUNDARY_CONTRACT,
)
from app.services.planner_compound_guard import _COMPOUND_AUTHORITY, _COMPOUND_REVIEW
from app.services.planner_semantic_scope_guard import (
    _normalize_unproven_npc_introductions,
)
from app.services.turn_authority_planner import CoordinatedTurnPlan, TurnAuthorityPlanner
from app.services.turn_authority_resolvers import NpcIntroductionResolver
from app.services.turn_planner import ActionSequencePlan, ActionStepPlan, SceneTransitionPlan
from live_model_contracts.state_oracles import is_lighting_fact, light_is_on


def _unproven_intro() -> PlannedNpcIntroduction:
    return PlannedNpcIntroduction(
        canonical_name="Алексей",
        role="дежурный у стойки",
        description="Дежурный находится за стойкой и отвечает на вопросы посетителей.",
        appearance="Тёмная рабочая форма и бейдж без читаемого отсюда имени.",
        temporary_name=False,
        personal_name_evidence=None,
        reason="Игрок напрямую обратился к дежурному.",
    )


def _plan_with_intro(introduction: PlannedNpcIntroduction) -> CoordinatedTurnPlan:
    plan = CoordinatedTurnPlan.conservative_fallback("Я обращаюсь к дежурному.")
    plan.npc_introductions = [introduction]
    return plan


def test_unproven_personal_name_is_downgraded_to_temporary_role_identity() -> None:
    plan = _plan_with_intro(_unproven_intro())

    result = _normalize_unproven_npc_introductions(plan)

    assert result is plan
    assert len(plan.npc_introductions) == 1
    introduction = plan.npc_introductions[0]
    assert introduction.canonical_name == "Дежурный у стойки"
    assert introduction.temporary_name is True
    assert introduction.personal_name_evidence is None


def test_authority_resolver_rechecks_unproven_personal_name() -> None:
    introductions = NpcIntroductionResolver.sanitize_introductions([_unproven_intro()])

    assert len(introductions) == 1
    introduction = introductions[0]
    assert introduction.canonical_name == "Дежурный у стойки"
    assert introduction.temporary_name is True
    assert introduction.personal_name_evidence is None


def test_temporary_flag_does_not_preserve_unproven_personal_name() -> None:
    introduction = _unproven_intro()
    introduction.temporary_name = True
    plan = _plan_with_intro(introduction)

    _normalize_unproven_npc_introductions(plan)
    resolved = NpcIntroductionResolver.sanitize_introductions(plan.npc_introductions)

    assert plan.npc_introductions[0].canonical_name == "Дежурный у стойки"
    assert plan.npc_introductions[0].temporary_name is True
    assert resolved[0].canonical_name == "Дежурный у стойки"
    assert resolved[0].temporary_name is True


def test_evidence_backed_personal_name_is_preserved() -> None:
    introduction = PlannedNpcIntroduction(
        canonical_name="Алексей",
        role="дежурный у стойки",
        description="Дежурный находится за стойкой и отвечает на вопросы посетителей.",
        appearance="Тёмная рабочая форма и бейдж.",
        temporary_name=False,
        personal_name_evidence="«Меня зовут Алексей», — представляется дежурный.",
        reason="Персонаж явно представился в текущем обмене.",
    )
    plan = _plan_with_intro(introduction)

    _normalize_unproven_npc_introductions(plan)
    resolved = NpcIntroductionResolver.sanitize_introductions(plan.npc_introductions)

    assert plan.npc_introductions[0].canonical_name == "Алексей"
    assert plan.npc_introductions[0].temporary_name is False
    assert plan.npc_introductions[0].personal_name_evidence
    assert resolved[0].canonical_name == "Алексей"
    assert resolved[0].temporary_name is False


def test_lighting_oracle_accepts_predicate_boolean_shape_from_live_run() -> None:
    row = {
        "subject": "комната Кая",
        "predicate": "освещена",
        "object": "да",
        "truth": "true",
        "current": True,
    }

    assert is_lighting_fact(row)
    assert light_is_on(row)


def test_lighting_oracle_negative_value_wins_over_positive_predicate() -> None:
    row = {
        "subject": "комната Кая",
        "predicate": "освещена",
        "object": "нет",
        "truth": "true",
        "current": True,
    }

    assert is_lighting_fact(row)
    assert not light_is_on(row)


def test_live_semantic_contract_distinguishes_person_transfer_from_drop() -> None:
    assert "inventory_operation=give" in _SEMANTIC_BOUNDARY_CONTRACT
    assert "drop means deliberately relinquishing" in _SEMANTIC_BOUNDARY_CONTRACT
    assert "stationary/negative constraint" in _SEMANTIC_BOUNDARY_CONTRACT
    assert "temporary_name=true" in _NPC_RECOVERY_BOUNDARY_CONTRACT


@pytest.mark.asyncio
async def test_engine_travel_authority_overrides_typed_missing_travel_without_committed_input() -> None:
    player_input = "Я остаюсь на месте и спрашиваю Мартина, всё ли с возвратом ключа закончено."
    router = SimpleNamespace(
        generate_json=AsyncMock(
            return_value={
                "verdict": "repair_required",
                "issues": ["План упустил движение к адресату."],
                "summary": "Missing travel.",
                "defect_kinds": ["missing_travel"],
            }
        )
    )
    planner = TurnAuthorityPlanner(router)
    plan = CoordinatedTurnPlan.conservative_fallback(player_input)
    plan.observable_consequences = ["Мартин подтверждает, что долг закрыт."]

    review = await planner._semantic_review(
        None,
        [],
        player_input,
        plan,
        ["Кай", "Мартин Вэнс"],
    )

    assert review.verdict == "pass"
    assert review.issues == []
    assert review.defect_kinds == []
    assert review._engine_authored is True


def _room_with_corridor_exit() -> list[ChatMessage]:
    return [
        ChatMessage(
            role="system",
            content="Location path: Комната Кая\nAvailable exits: Комната Кая -> Коридор\n",
        )
    ]


def _blocker_input() -> str:
    return (
        "Я выхожу из комнаты в коридор, а затем пытаюсь пройти прямо из коридора на склад. "
        "Прямого прохода из коридора на склад нет."
    )


def _corridor_step() -> ActionStepPlan:
    return ActionStepPlan(
        action_type="movement",
        intent="Выйти в коридор",
        resolution="auto_success",
        safe_mundane=True,
        observable_outcome="Кай выходит в коридор.",
        transition=SceneTransitionPlan(
            required=True,
            transition_type="location_transition",
            destination_location="Коридор",
        ),
    )


def test_unavailable_hop_is_covered_by_blocked_step() -> None:
    plan = CoordinatedTurnPlan.conservative_fallback(_blocker_input())
    plan.action_sequence = ActionSequencePlan(
        steps=[
            _corridor_step(),
            ActionStepPlan(
                action_type="movement",
                intent="Пройти на склад",
                resolution="blocked",
                blocking_reason="Из коридора нет прохода на склад.",
            ),
        ]
    )
    travel = TurnAuthorityPlanner._canonical_travel_authority(
        _blocker_input(),
        plan,
        _room_with_corridor_exit(),
    )
    assert travel.committed is True
    assert travel.has_location_transition is True
    assert travel.unavailable_committed_travel is True
    assert travel.blocked_attempt_typed is True


def test_unavailable_hop_without_blocked_step_is_uncovered() -> None:
    plan = CoordinatedTurnPlan.conservative_fallback(_blocker_input())
    plan.action_sequence = ActionSequencePlan(steps=[_corridor_step()])
    travel = TurnAuthorityPlanner._canonical_travel_authority(
        _blocker_input(),
        plan,
        _room_with_corridor_exit(),
    )
    assert travel.unavailable_committed_travel is True
    assert travel.blocked_attempt_typed is False


@pytest.mark.asyncio
async def test_engine_requires_blocked_step_for_unavailable_committed_hop() -> None:
    player_input = _blocker_input()
    router = SimpleNamespace(
        generate_json=AsyncMock(return_value={"verdict": "pass", "issues": [], "summary": ""})
    )
    planner = TurnAuthorityPlanner(router)
    plan = CoordinatedTurnPlan.conservative_fallback(player_input)
    plan.action_sequence = ActionSequencePlan(steps=[_corridor_step()])

    review = await planner._semantic_review(
        None,
        _room_with_corridor_exit(),
        player_input,
        plan,
        ["Кай"],
    )

    assert review.verdict == "repair_required"
    assert "missing_committed_action" in review.defect_kinds
    assert any("blocked action_sequence step" in issue for issue in review.issues)


def test_mentioned_only_introductions_are_dropped_from_typed_participation() -> None:
    plan = CoordinatedTurnPlan.conservative_fallback("Я иду в коридор, прямого прохода на склад нет.")
    plan.npc_introductions = [
        PlannedNpcIntroduction(
            canonical_name="Незнакомец",
            role="прохожий",
            reason="Упомянут в описании закрытой двери.",
        )
    ]
    assessments = [
        {
            "introduction_index": 0,
            "participation": "mentioned_only",
            "designation": "uncertain",
        }
    ]

    dropped = TurnAuthorityPlanner._drop_non_encountered_introductions(plan, assessments)

    assert dropped is True
    assert plan.npc_introductions == []


def test_compound_contract_preserves_explicit_intermediate_destination_and_tail() -> None:
    example = "выхожу из комнаты в коридор и иду в контору"

    assert example in _COMPOUND_AUTHORITY
    assert example in _COMPOUND_REVIEW
    assert "silently stop at" in _COMPOUND_AUTHORITY
    assert "lost the tail" in _COMPOUND_REVIEW
    assert "Incidental path" in _COMPOUND_REVIEW
