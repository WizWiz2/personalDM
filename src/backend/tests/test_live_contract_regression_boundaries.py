from app.models.turn_authority import PlannedNpcIntroduction
from app.services.live_contract_stabilization_guard import (
    _NPC_RECOVERY_BOUNDARY_CONTRACT,
    _SEMANTIC_BOUNDARY_CONTRACT,
)
from app.services.planner_semantic_scope_guard import (
    _normalize_unproven_npc_introductions,
)
from app.services.turn_authority_planner import CoordinatedTurnPlan
from app.services.turn_authority_resolvers import NpcIntroductionResolver
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
