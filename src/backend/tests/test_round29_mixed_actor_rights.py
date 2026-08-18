from types import SimpleNamespace
from uuid import uuid4

from app.models.narration_validation import NarrationValidationResult, NarrationViolation
from app.services.mixed_actor_response_guard import (
    actor_response_contract,
    protect_actor_response_validation,
)


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


def test_mixed_sequence_allows_selected_npc_owned_claim() -> None:
    authority = _mixed_authority()
    evidence = "Марина Орлова отвечает: «Фотографии сделала я сама вчера вечером.»"
    result = NarrationValidationResult(
        verdict="repair_required",
        summary="Новое неподтвержденное обстоятельство",
        violations=[
            NarrationViolation(
                violation_type="ungrounded_complication",
                severity="error",
                evidence=evidence,
                correction="Не добавлять неавторизованную информацию.",
            )
        ],
    )

    protected = protect_actor_response_validation(authority, result, evidence)

    assert protected.verdict == "pass"
    assert protected.violations == []
    assert authority.scene_disposition == "sequence"


def test_mixed_sequence_still_rejects_world_mutation_outside_npc_speech() -> None:
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

    assert protected.verdict == "repair_required"
    assert len(protected.violations) == 1
    assert protected.violations[0].violation_type == "ungrounded_complication"
