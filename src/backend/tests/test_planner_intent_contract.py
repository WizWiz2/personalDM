from app.models.turn_authority import PlannedNpcIntroduction
from app.services.turn_authority_planner import CoordinatedTurnPlan, TurnAuthorityPlanner
from app.services.turn_planner import NarrationPolicy, SceneTransitionPlan


def _stay(*consequences: str, npcs=None) -> CoordinatedTurnPlan:
    return CoordinatedTurnPlan(
        player_intent="Проверка",
        resolution="observation",
        scene_disposition="stay",
        npc_introductions=list(npcs or []),
        narration_policy=NarrationPolicy(),
        observable_consequences=list(consequences),
        character_beats=[],
        canon_constraints=[],
        new_fact_candidates=[],
        narration_guidance=[],
        ending_hook="",
    )


def test_knock_requires_structured_responder_or_explicit_no_answer():
    ambiguous = _stay("Стук разносится по коридору.")
    assert TurnAuthorityPlanner.contract_issues(
        ambiguous,
        "Подхожу к двери и трижды стучу.",
    )

    responder = _stay(
        "Дверь открывает дежурный фабрики.",
        npcs=[
            PlannedNpcIntroduction(
                canonical_name="Дежурный фабрики",
                role="дежурный",
                reason="Ответил на стук.",
                temporary_name=True,
            )
        ],
    )
    assert TurnAuthorityPlanner.contract_issues(
        responder,
        "Подхожу к двери и трижды стучу.",
    ) == []

    nobody = _stay("На стук никто не отвечает.")
    assert TurnAuthorityPlanner.contract_issues(
        nobody,
        "Подхожу к двери и трижды стучу.",
    ) == []


def test_explicit_destination_movement_cannot_silently_stay():
    ambiguous = _stay("Рэт выходит на улицу.")
    issues = TurnAuthorityPlanner.contract_issues(
        ambiguous,
        "Иду в таверну «Гнилой фонарь».",
    )
    assert any("location_transition" in issue for issue in issues)

    blocked = _stay("Путь закрыт запертой решёткой, пройти не удаётся.")
    assert TurnAuthorityPlanner.contract_issues(
        blocked,
        "Иду в таверну «Гнилой фонарь».",
    ) == []

    transition = CoordinatedTurnPlan(
        player_intent="Идти в таверну.",
        resolution="transition",
        scene_disposition="location_transition",
        narration_policy=NarrationPolicy(),
        scene_transition=SceneTransitionPlan(
            required=True,
            transition_type="location_transition",
            destination_location="Гнилой фонарь",
            scene_title="Таверна «Гнилой фонарь»",
            reason="Игрок явно идёт в таверну.",
        ),
        observable_consequences=["Рэт достигает таверны «Гнилой фонарь»."],
        character_beats=[],
        canon_constraints=[],
        new_fact_candidates=[],
        narration_guidance=[],
        ending_hook="",
    )
    assert TurnAuthorityPlanner.contract_issues(
        transition,
        "Иду в таверну «Гнилой фонарь».",
    ) == []
