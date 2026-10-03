from uuid import uuid4

from app.models.narration_validation import NarrationValidationResult
from app.models.turn_authority import TurnAuthority
from app.services.turn_authority_planner import TurnAuthorityPlanner


def _pass() -> NarrationValidationResult:
    return NarrationValidationResult(verdict="pass", summary="Кандидат принят.", violations=[])


def _authority(player_input: str) -> TurnAuthority:
    return TurnAuthority(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_character_name="Рэт Уайтмоур",
        player_input=player_input,
        observable_consequences=["У двери остаётся тусклый свет."],
    )


def test_semantic_plan_reviewer_owns_unresolved_choice_stale_turn_and_movement_meaning():
    prompt = TurnAuthorityPlanner.SEMANTIC_REVIEW_PROMPT

    assert "CURRENT INPUT" in prompt
    assert "PLAYER AGENCY" in prompt
    assert "MOVEMENT/TIME" in prompt
    assert "CONTACT/IDENTITY" in prompt
    assert "LANGUAGE" in prompt
    assert "Do not use keyword lists" in prompt


