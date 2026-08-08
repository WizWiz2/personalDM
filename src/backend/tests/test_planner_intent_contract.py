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

    hidden_responder = _stay("Из-за двери отвечает сонный мужской голос.")
    assert TurnAuthorityPlanner.contract_issues(
        hidden_responder,
        "Подхожу к двери и трижды стучу.",
    ), "positive responder must not hide outside npc_introductions"

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


def test_generic_contact_requires_identity_or_negative_outcome():
    hidden_informant = _stay("Информатор сообщает, что видел синее свечение.")
    assert TurnAuthorityPlanner.contract_issues(
        hidden_informant,
        "Иду в таверну расспросить информатора.",
    )

    no_contact = _stay("Подходящего информатора в зале не нашлось.")
    # Movement is intentionally absent here, so inspect only the contact half of the contract.
    contact_issues = [
        issue
        for issue in TurnAuthorityPlanner.contract_issues(
            no_contact,
            "Хочу расспросить информатора.",
        )
        if "direct contact" in issue
    ]
    assert contact_issues == []


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
