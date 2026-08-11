from app.models.turn_authority import PlannedNpcIntroduction
from app.services.turn_authority_planner import CoordinatedTurnPlan, TurnAuthorityPlanner
from app.services.turn_planner import (
    ActionSequencePlan,
    ActionStepPlan,
    NarrationPolicy,
    SceneTransitionPlan,
)


def _interaction_only_sequence(*consequences: str, npcs=None) -> CoordinatedTurnPlan:
    return CoordinatedTurnPlan(
        player_intent="Вернуться в таверну и поговорить с трактирщиком.",
        resolution="sequence",
        action_sequence=ActionSequencePlan(
            summary="Поговорить с трактирщиком.",
            steps=[
                ActionStepPlan(
                    action_type="interaction",
                    intent="Поговорить с трактирщиком о магах.",
                    resolution="requires_check",
                )
            ],
        ),
        npc_introductions=list(npcs or []),
        narration_policy=NarrationPolicy(),
        observable_consequences=list(consequences),
        character_beats=[],
        canon_constraints=[],
        new_fact_candidates=[],
        narration_guidance=[],
        ending_hook="",
    )


def test_round7_interaction_sequence_cannot_hide_explicit_location_change():
    plan = _interaction_only_sequence(
        "Рэт подходит к стойке таверны и обращается к трактирщику."
    )

    assert plan.scene_disposition == "sequence"
    assert plan.scene_transition.transition_type == "focus_transition"
    issues = TurnAuthorityPlanner.contract_issues(
        plan,
        'Возвращаюсь в таверну "Гнилой фонарь", чтобы поговорить с трактирщиком о магах.',
    )

    assert any("actual required location_transition" in issue for issue in issues)


def test_round7_focus_transition_does_not_satisfy_destination_movement():
    plan = CoordinatedTurnPlan(
        player_intent="Вернуться в таверну.",
        resolution="transition",
        scene_transition=SceneTransitionPlan(
            required=True,
            transition_type="focus_transition",
            scene_title="Разговор в таверне",
            reason="Модель ошибочно подменила перемещение сменой фокуса.",
        ),
        narration_policy=NarrationPolicy(),
        observable_consequences=["Рэт уже стоит у стойки таверны."],
    )

    issues = TurnAuthorityPlanner.contract_issues(
        plan,
        'Возвращаюсь в таверну "Гнилой фонарь".',
    )
    assert any("actual required location_transition" in issue for issue in issues)


def test_round7_sequence_with_real_movement_step_satisfies_movement_contract():
    plan = CoordinatedTurnPlan(
        player_intent="Вернуться в таверну и осмотреть зал.",
        resolution="sequence",
        action_sequence=ActionSequencePlan(
            summary="Вернуться в таверну и осмотреть зал.",
            steps=[
                ActionStepPlan(
                    action_type="movement",
                    intent="Вернуться в таверну Гнилой фонарь.",
                    resolution="auto_success",
                    safe_mundane=True,
                    observable_outcome="Рэт входит в таверну.",
                    transition=SceneTransitionPlan(
                        required=True,
                        transition_type="location_transition",
                        destination_location="Гнилой фонарь",
                        scene_title="Таверна «Гнилой фонарь»",
                        reason="Игрок явно возвращается в таверну.",
                    ),
                ),
                ActionStepPlan(
                    action_type="observation",
                    intent="Осмотреть зал.",
                    resolution="auto_success",
                    safe_mundane=True,
                    observable_outcome="Рэт осматривает зал.",
                ),
            ],
        ),
        narration_policy=NarrationPolicy(),
        observable_consequences=["Рэт возвращается в таверну и осматривает зал."],
    )

    assert TurnAuthorityPlanner.contract_issues(
        plan,
        'Возвращаюсь в таверну "Гнилой фонарь" и осматриваю зал.',
    ) == []


def test_round7_generic_contact_verbs_require_typed_responder_or_no_contact():
    phrases = (
        "Поговорить с трактирщиком о магах.",
        "Обращаюсь к хозяину таверны.",
        "Зову бармена к стойке.",
        "Окликаю охранника.",
    )
    unresolved = CoordinatedTurnPlan(
        player_intent="Контакт.",
        resolution="conversation",
        narration_policy=NarrationPolicy(),
        observable_consequences=["Голос отвечает на вопрос."],
    )
    for phrase in phrases:
        issues = TurnAuthorityPlanner.contract_issues(unresolved, phrase)
        assert any("direct contact is unresolved" in issue for issue in issues), phrase

    responder = CoordinatedTurnPlan(
        player_intent="Позвать бармена.",
        resolution="conversation",
        npc_introductions=[
            PlannedNpcIntroduction(
                canonical_name="Бармен Медного Котла",
                role="бармен",
                temporary_name=True,
                reason="Игрок прямо зовёт бармена.",
            )
        ],
        narration_policy=NarrationPolicy(),
        observable_consequences=["Бармен подходит к стойке."],
    )
    assert TurnAuthorityPlanner.contract_issues(responder, "Зову бармена к стойке.") == []
