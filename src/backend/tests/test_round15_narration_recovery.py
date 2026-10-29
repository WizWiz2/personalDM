from __future__ import annotations

from uuid import uuid4


from app.models.narration_validation import NarrationValidationResult
from app.models.turn_authority import TurnAuthority
from app.services.narration_publication_guard import (
    NarrationPublicationGuard,
)


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
