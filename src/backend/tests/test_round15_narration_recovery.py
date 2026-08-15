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


def test_ordinary_rejected_narration_projects_authority_even_if_one_bad_segment_is_exact():
    authority = _authority()
    candidate = "Дежурный внезапно отступает в тень. Элдон решает немедленно войти внутрь."
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
    assert "отступает" not in published
    assert diagnostics["mode"] == "authority_projection"


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
    rejected = "Элдон решает войти внутрь и улыбается. Потом он достаёт нож."
    evidence = "Элдон решает войти внутрь и улыбается."
    validation = _validation(
        {
            "violation_type": "player_agency",
            "severity": "error",
            "evidence": evidence,
            "correction": "Не продолжать действие героя.",
        }
    )

    prompt = TurnAuthorityValidator.repair_prompt(authority, rejected, validation)

    assert "[REPAIR REJECTED NARRATION]" in prompt
    assert "[REPAIR AGAINST TURN AUTHORITY]" in prompt
    assert "С НУЛЯ" in prompt
    assert "REJECTED CANDIDATE OMITTED" in prompt
    assert evidence in prompt
    assert rejected not in prompt
    assert "Потом он достаёт нож" not in prompt
    assert "observable_consequences" in prompt
    assert "Не пересказывай действие игрока" in prompt


def test_narrator_payload_exposes_explicit_player_agency_contract():
    payload = _authority().narrator_payload()
    contract = payload["player_agency_contract"]

    assert contract["do_not_restate_player_voluntary_action"] is True
    assert contract["do_not_extend_player_voluntary_action"] is True
    assert contract["do_not_assign_player_thoughts_emotions_or_decisions"] is True
    assert contract["response_focus"] == "world_or_npc_response_to_current_input"
