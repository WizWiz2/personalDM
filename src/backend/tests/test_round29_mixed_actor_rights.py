from types import SimpleNamespace
from uuid import uuid4

from app.models.narration_validation import NarrationValidationResult, NarrationViolation
from app.services.mixed_actor_response_guard import (
    actor_response_contract,
    protect_actor_response_validation,
)
from app.services.turn_authority_validator import TurnAuthorityValidator


class _Authority(SimpleNamespace):
    def model_copy(self, *, update):
        return _Authority(**{**self.__dict__, **update})


def _mixed_authority():
    return _Authority(
        scene_disposition="sequence",
        acting_character_id=uuid4(),
        acting_character_name="Марина Орлова",
        player_character_name="Алекс",
    )


def test_mixed_sequence_exposes_actor_response_contract_without_changing_world_disposition() -> None:
    authority = _mixed_authority()

    contract = actor_response_contract(authority)

    assert contract is not None
    assert contract["acting_character"] == "Марина Орлова"
    assert contract["mixed_response"] is True
    assert contract["world_disposition"] == "sequence"
    assert "speak_as_self" in contract["authorized"]
    assert authority.scene_disposition == "sequence"


def test_mixed_sequence_actor_claim_is_authorized_by_typed_semantic_contract() -> None:
    contract = actor_response_contract(_mixed_authority())
    prompt = TurnAuthorityValidator.SYSTEM_PROMPT

    assert contract is not None
    assert "state_personal_memories_observations_and_claims" in contract["authorized"]
    assert "ACTOR TURN RIGHTS" in prompt
    assert "character_claim" in prompt
    assert "not objective world canon" in prompt


def test_legacy_post_filter_is_noop_and_world_mutation_remains_for_semantic_validator() -> None:
    authority = _mixed_authority()
    candidate = (
        "Марина Орлова отвечает: «Фотографии сделала я сама вчера вечером.» "
        "В этот момент дверь распахивается и в комнату входит незнакомец."
    )
    result = NarrationValidationResult(
        verdict="repair_required",
        summary="Неавторизованная физическая сцена",
        violations=[
            NarrationViolation(
                violation_type="ungrounded_complication",
                severity="error",
                evidence="В этот момент дверь распахивается и в комнату входит незнакомец.",
                correction="Не вводить физическое событие без TurnAuthority.",
            )
        ],
    )

    protected = protect_actor_response_validation(authority, result, candidate)

    assert protected is result
    assert protected.verdict == "repair_required"
    assert len(protected.violations) == 1
