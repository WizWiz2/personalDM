from uuid import uuid4

from app.models.narration_validation import NarrationValidationResult
from app.models.turn_authority import TurnAuthority
from app.services.turn_authority_validator import TurnAuthorityValidator


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


def test_round26_t5_rejects_morg_arrival_when_authority_stays():
    result = TurnAuthorityValidator.apply_deterministic_movement_surface(
        _passed(),
        _authority(moved=False),
        "Виктор Соколов движется к массивному зданию городского морга.",
    )

    assert result.verdict == "repair_required"
    violation = next(item for item in result.violations if item.violation_type == "invalid_movement")
    assert "городского морга" in violation.evidence


def test_round26_t6_rejects_return_to_office_when_authority_stays():
    result = TurnAuthorityValidator.apply_deterministic_movement_surface(
        _passed(),
        _authority(moved=False),
        "Виктор Соколов возвращается к старому офису и толкает дверь.",
    )

    assert result.verdict == "repair_required"
    assert any(item.violation_type == "invalid_movement" for item in result.violations)


def test_local_movement_inside_current_scene_is_not_location_transition():
    result = TurnAuthorityValidator.apply_deterministic_movement_surface(
        _passed(),
        _authority(moved=False),
        "Виктор Соколов подходит к окну и смотрит на улицу.",
    )

    assert result.verdict == "pass"
    assert result.violations == []


def test_real_structured_transition_allows_arrival_narration():
    result = TurnAuthorityValidator.apply_deterministic_movement_surface(
        _passed(),
        _authority(moved=True),
        "Виктор Соколов прибывает к городскому моргу.",
    )

    assert result.verdict == "pass"
    assert result.violations == []


def test_npc_movement_does_not_fake_player_location_divergence():
    result = TurnAuthorityValidator.apply_deterministic_movement_surface(
        _passed(),
        _authority(moved=False),
        "Ирина возвращается в офис и закрывает за собой дверь.",
    )

    assert result.verdict == "pass"
