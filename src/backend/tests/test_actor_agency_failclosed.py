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
    return NarrationValidationResult(verdict="pass", summary="ok", violations=[])


def test_actor_agency_is_not_reinterpreted_by_deterministic_word_matching():
    authority = _authority()
    candidate = (
        "Грета шепчет: «Тень свернула к старому складу». "
        "Рэт Уайтмоур кивнул и записал ответ."
    )

    result = TurnAuthorityValidator.apply_deterministic_actor_agency(
        _pass(), authority, candidate
    )

    assert result.verdict == "pass"
    assert result.violations == []


def test_semantic_validator_contract_protects_player_inside_actor_turn():
    prompt = TurnAuthorityValidator.SYSTEM_PROMPT

    assert "PLAYER AGENCY" in prompt
    assert "NPC OWNERSHIP" in prompt
    assert "PRESENT NPC DIALOGUE" in prompt
    assert "Never cite an NPC-owned fragment as player agency" in prompt
    assert "Never decide from" in prompt


def test_actor_turn_contract_explicitly_distinguishes_speaker_consistency():
    prompt = TurnAuthorityValidator.SYSTEM_PROMPT

    assert "SPEAKER CONSISTENCY" in prompt
    assert "current actor" in prompt
