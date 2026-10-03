from uuid import uuid4

from app.config import settings
from app.models.turn_authority import TurnAuthority
from app.services.narrator_quality_recovery_guard import (
    narrator_context_budget,
)


def authority(**updates):
    base = dict(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_character_id=uuid4(),
        player_character_name="Александр",
        player_input="Я оглядываюсь.\n- Кто здесь?",
        scene_disposition="stay",
        transition_type="none",
        source_location_path=["окраина города Эшфорд", "шатер директора"],
        target_location_path=["окраина города Эшфорд", "шатер директора"],
        present_character_names=["Александр"],
        resolution="observation",
        observable_consequences=[],
        allow_new_complication=False,
    )
    base.update(updates)
    return TurnAuthority(**base)


def test_narrator_budget_no_longer_reserves_previous_planner_call():
    context_window = 4096
    expected = (
        context_window
        - settings.RESPONSE_RESERVE_TOKENS
        - int(context_window * settings.SAFETY_MARGIN_PERCENT)
    )

    assert narrator_context_budget(context_window) == expected
    assert narrator_context_budget(context_window) == expected
    assert narrator_context_budget(context_window) > 1650


