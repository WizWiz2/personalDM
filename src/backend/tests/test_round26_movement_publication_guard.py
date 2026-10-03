from uuid import uuid4

from app.models.narration_validation import NarrationValidationResult
from app.models.turn_authority import TurnAuthority


def _passed() -> NarrationValidationResult:
    return NarrationValidationResult(verdict="pass", summary="model says pass", violations=[])


def _authority(*, moved: bool = False) -> TurnAuthority:
    source = ["Город", "Окрестности старого офиса"]
    target = ["Город", "Городской морг"] if moved else list(source)
    return TurnAuthority(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_character_id=uuid4(),
        player_character_name="Виктор Соколов",
        player_input="Виктор направляется в городской морг.",
        source_location_path=source,
        target_location_path=target,
        scene_disposition="location_transition" if moved else "stay",
        transition_type="location_transition" if moved else "none",
    )


def test_real_structured_transition_remains_machine_visible():
    authority = _authority(moved=True)

    assert authority.scene_disposition == "location_transition"
    assert authority.transition_type == "location_transition"
    assert authority.source_location_path != authority.target_location_path


