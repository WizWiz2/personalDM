"""QA-210: a triggered structured deadline cannot disappear from a published turn."""

from uuid import uuid4

from app.models.turn_authority import PlannedWorldMove, TurnAuthority
from app.services.narration_publication_guard import NarrationPublicationGuard


def test_committed_deadline_outcome_survives_transition_only_narration():
    outcome = "Лицензия переправы аннулирована на рассвете."
    authority = TurnAuthority(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_input="Жду до рассвета.",
        world_move=PlannedWorldMove(
            kind="deadline",
            source="Гильдия аннулирует лицензию переправы на рассвете",
            outcome=outcome,
            due_phase="dawn",
        ),
    )
    candidate = "Переход к причалу завершён."
    result = NarrationPublicationGuard.include_critical_world_outcome(authority, candidate)
    assert candidate in result
    assert outcome in result
    assert NarrationPublicationGuard.include_critical_world_outcome(authority, result) == result


def test_nondeadline_story_moves_do_not_get_forced_additions():
    authority = TurnAuthority(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_input="Осматриваюсь.",
        world_move=PlannedWorldMove(
            kind="pressure",
            source="Напряжение между дозорными возрастает",
            outcome="Один из дозорных угрожает устроить забастовку",
        ),
    )
    assert NarrationPublicationGuard.include_critical_world_outcome(authority, "Тишина.") == "Тишина."
