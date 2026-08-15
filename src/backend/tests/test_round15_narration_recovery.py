from __future__ import annotations

from uuid import uuid4

from app.models.narration_validation import NarrationValidationResult
from app.models.turn_authority import TurnAuthority
from app.services.narration_publication_guard import NarrationPublicationGuard
from app.services.turn_authority_validator import TurnAuthorityValidator


def _authority(**overrides) -> TurnAuthority:
    payload = {
        "campaign_id": uuid4(),
        "trigger_turn_id": uuid4(),
        "player_character_name": "Элдон",
        "player_input": "Осматриваю дверь и жду результата.",
        "observable_consequences": ["Замок покрыт ржавчиной."],
        "scene_disposition": "stay",
    }
    payload.update(overrides)
    return TurnAuthority(**payload)


def _validation(*violations: dict) -> NarrationValidationResult:
    return NarrationValidationResult.model_validate(
        {
            "verdict": "repair_required",
            "summary": "Нужно убрать нарушения.",
            "violations": list(violations),
        }
    )


def test_publication_guard_removes_exact_bad_segment_and_keeps_safe_prose():
    authority = _authority()
    candidate = "Замок покрыт ржавчиной. Элдон решает немедленно войти внутрь."
    validation = _validation(
        {
            "violation_type": "player_agency",
            "severity": "error",
            "evidence": "Элдон решает немедленно войти внутрь.",
            "correction": "Удалить придуманное решение героя.",
        }
    )

    published, diagnostics = NarrationPublicationGuard.publish(
        authority,
        candidate,
        validation,
    )

    assert published == "Замок покрыт ржавчиной."
    assert diagnostics["mode"] == "sanitized_candidate"
    assert diagnostics["matched_error_count"] == 1


def test_publication_guard_scrubs_named_player_action_when_validator_evidence_is_generic():
    authority = _authority()
    candidate = "Замок остаётся закрытым. Элдон колеблется, решая, стоит ли ломать дверь."
    validation = _validation(
        {
            "violation_type": "player_agency",
            "severity": "error",
            "evidence": "Нарратор приписывает герою новое решение.",
            "correction": "Не принимать решение за игрока.",
        }
    )

    published, diagnostics = NarrationPublicationGuard.publish(
        authority,
        candidate,
        validation,
    )

    assert published == "Замок остаётся закрытым."
    assert diagnostics["mode"] == "sanitized_candidate"
    assert diagnostics["ordinary_agency_scrubbed"] is True


def test_legacy_no_result_stub_is_never_published_from_authority_projection():
    authority = _authority(
        observable_consequences=[
            "Попытка пока не приводит к подтверждённому результату."
        ],
        ending_hook="",
    )

    published = NarrationPublicationGuard.render_authority(authority)

    assert "подтверждённому результату" not in published.casefold()
    assert published == "Пока ничего заметно не меняется."


def test_repair_prompt_regenerates_from_authority_without_rejected_candidate_anchor():
    authority = _authority()
    rejected = "Элдон решает войти внутрь и улыбается."
    validation = _validation(
        {
            "violation_type": "player_agency",
            "severity": "error",
            "evidence": "Элдон решает войти внутрь и улыбается.",
            "correction": "Не продолжать действие героя.",
        }
    )

    prompt = TurnAuthorityValidator.repair_prompt(authority, rejected, validation)

    assert "С НУЛЯ" in prompt
    assert "REJECTED CANDIDATE OMITTED" in prompt
    assert rejected not in prompt
    assert "observable_consequences" in prompt
    assert "Не пересказывай действие игрока" in prompt


def test_narrator_payload_exposes_explicit_player_agency_contract():
    payload = _authority().narrator_payload()
    contract = payload["player_agency_contract"]

    assert contract["do_not_restate_player_voluntary_action"] is True
    assert contract["do_not_extend_player_voluntary_action"] is True
    assert contract["do_not_assign_player_thoughts_emotions_or_decisions"] is True
    assert contract["response_focus"] == "world_or_npc_response_to_current_input"
