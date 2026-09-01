from app.models.turn_authority import PlannedNpcIntroduction
from app.services.turn_authority_planner import CoordinatedTurnPlan, TurnAuthorityPlanner


def test_russian_surface_replaces_cjk_generated_npc_name_with_temporary_identity():
    plan = CoordinatedTurnPlan(
        player_intent="Задать вопрос диспетчеру.",
        resolution="conversation",
        observable_consequences=["Диспетчер отвечает."],
        character_beats=[],
        canon_constraints=[],
        new_fact_candidates=[],
        narration_guidance=[],
        ending_hook="Ответ получен.",
        npc_introductions=[
            PlannedNpcIntroduction(
                canonical_name="未知调度员",
                role="диспетчер",
                description="Рабочий у трапа.",
                reason="Игрок обратился к нему и получил ответ.",
            )
        ],
    )

    TurnAuthorityPlanner._sanitize_npc_names(plan, "Я подхожу к диспетчеру и спрашиваю, кто отправил баржу.")

    intro = plan.npc_introductions[0]
    assert intro.canonical_name == "Безымянный собеседник"
    assert intro.temporary_name is True
