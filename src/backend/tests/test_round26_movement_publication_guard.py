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


def test_movement_meaning_is_owned_by_semantic_validator_not_word_matching():
    result = TurnAuthorityValidator.apply_deterministic_movement_surface(
        _passed(),
        _authority(moved=False),
        "Виктор Соколов движется к массивному зданию городского морга.",
    )

    assert result.verdict == "pass"
    prompt = TurnAuthorityValidator.SYSTEM_PROMPT
    assert "MOVEMENT/TIME" in prompt
    assert "true scene transition" in prompt
    assert "meaning, not vocabulary" in prompt


def test_local_movement_inside_current_scene_is_not_deterministically_reclassified():
    result = TurnAuthorityValidator.apply_deterministic_movement_surface(
        _passed(),
        _authority(moved=False),
        "Виктор Соколов подходит к окну и смотрит на улицу.",
    )

    assert result.verdict == "pass"
    assert result.violations == []


def test_real_structured_transition_remains_machine_visible():
    authority = _authority(moved=True)

    assert authority.scene_disposition == "location_transition"
    assert authority.transition_type == "location_transition"
    assert authority.source_location_path != authority.target_location_path


def test_npc_movement_is_not_misclassified_by_deterministic_player_surface_code():
    result = TurnAuthorityValidator.apply_deterministic_movement_surface(
        _passed(),
        _authority(moved=False),
        "Ирина возвращается в офис и закрывает за собой дверь.",
    )

    assert result.verdict == "pass"
