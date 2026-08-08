from uuid import uuid4

from app.models.narration_validation import NarrationValidationResult
from app.models.turn_authority import TurnAuthority
from app.services.turn_authority_validator import TurnAuthorityValidator


def _authority() -> TurnAuthority:
    return TurnAuthority(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_character_name="Рэт Уайтмоур",
        acting_character_name="Старуха Грета",
        player_input="В какой дом сворачивала тень?",
        scene_disposition="actor_turn",
    )


def _pass() -> NarrationValidationResult:
    return NarrationValidationResult(
        verdict="pass",
        summary="Ответ принадлежит только NPC.",
        violations=[],
    )


def test_actor_agency_is_detected_even_when_model_validator_returns_pass():
    authority = _authority()
    candidate = (
        "Грета шепчет: «Тень свернула к старому складу». "
        "Рэт Уайтмоур кивнул и записал ответ."
    )

    result = TurnAuthorityValidator.apply_deterministic_actor_agency(
        _pass(),
        authority,
        candidate,
    )

    assert result.verdict == "repair_required"
    assert any(
        item.violation_type == "player_agency" and item.severity == "error"
        for item in result.violations
    )


def test_clean_actor_reply_stays_passed():
    result = TurnAuthorityValidator.apply_deterministic_actor_agency(
        _pass(),
        _authority(),
        "Грета шепчет: «Тень свернула к старому складу у фабрики».",
    )

    assert result.verdict == "pass"
    assert result.violations == []


def test_npc_may_address_player_by_name_without_becoming_player_agency():
    result = TurnAuthorityValidator.apply_deterministic_actor_agency(
        _pass(),
        _authority(),
        "«Рэт Уайтмоур, решай сам», — сухо говорит Грета.",
    )

    assert result.verdict == "pass"
    assert result.violations == []
