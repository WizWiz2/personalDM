from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.narration_validation_table import NarrationValidationRun
from app.models.narration_validation import (
    NarrationValidationResult,
    NarrationViolation,
)
from app.services.narration_validator import (
    NarrationValidationError,
    NarrationValidator,
)


INVALID_DRAFT = (
    "Криповый бармен уже стоит у кровати. "
    "Ты решаешь довериться ему и обещаешь пойти следом."
)
REPAIRED_TEXT = (
    "Комната остаётся тихой. За закрытой дверью слышен только далёкий шум "
    "общего зала. Перед тобой остаётся выбор, что делать дальше."
)


def repair_required() -> NarrationValidationResult:
    return NarrationValidationResult(
        verdict="repair_required",
        summary="Absent NPC and protagonist agency violation.",
        violations=[
            NarrationViolation(
                violation_type="absent_character",
                severity="error",
                evidence="Криповый бармен уже стоит у кровати",
                correction="Remove the absent bartender from the room.",
            ),
            NarrationViolation(
                violation_type="player_agency",
                severity="error",
                evidence="Ты решаешь довериться ему",
                correction="Leave trust and the next action to the player.",
            ),
        ],
    )


def passed() -> NarrationValidationResult:
    return NarrationValidationResult(
        verdict="pass",
        summary="Candidate respects scene state and player agency.",
        violations=[],
    )


async def raw_narrator(_provider, messages, *args, **kwargs):
    if "[REPAIR REJECTED NARRATION]" in messages[-1].content:
        yield REPAIRED_TEXT
    else:
        yield INVALID_DRAFT


async def latest_validation(db_session: AsyncSession) -> NarrationValidationRun:
    row = (
        await db_session.execute(
            select(NarrationValidationRun)
            .order_by(NarrationValidationRun.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    assert row is not None
    return row


def test_validation_result_rejects_pass_with_errors():
    with pytest.raises(ValidationError):
        NarrationValidationResult(
            verdict="pass",
            violations=[
                NarrationViolation(
                    violation_type="player_agency",
                    severity="error",
                    evidence="The hero agrees.",
                    correction="Leave the decision open.",
                )
            ],
        )


def test_repair_prompt_contains_only_actionable_contract():
    messages = NarrationValidator.repair_messages(
        [],
        INVALID_DRAFT,
        repair_required(),
    )
    prompt = messages[-1].content
    assert "[REPAIR REJECTED NARRATION]" in prompt
    assert "absent_character" in prompt
    assert "player_agency" in prompt
    assert INVALID_DRAFT in prompt
    assert "Return only the repaired narrative prose" in prompt


@pytest.mark.asyncio
async def test_invalid_draft_is_repaired_before_delivery(
    client: TestClient,
    db_session: AsyncSession,
):
    campaign_id = client.post(
        "/api/campaigns",
        json={"name": "Validation gate"},
    ).json()["id"]

    with patch(
        "app.services.narration_validation_guard._ORIGINAL_GENERATE_STREAM",
        side_effect=raw_narrator,
    ), patch.object(
        NarrationValidator,
        "validate",
        new_callable=AsyncMock,
        side_effect=[repair_required(), passed()],
    ):
        response = client.post(
            f"/api/campaigns/{campaign_id}/turns",
            json={"role": "user", "content": "Закрываю дверь и осматриваюсь."},
        )

    assert response.status_code == 200, response.text
    assert response.text == REPAIRED_TEXT
    assert INVALID_DRAFT not in response.text

    history = client.get(f"/api/campaigns/{campaign_id}/turns").json()
    assert history[-1]["content"] == REPAIRED_TEXT
    validation = await latest_validation(db_session)
    assert validation.status == "repaired"
    assert validation.draft_text == INVALID_DRAFT
    assert validation.final_text == REPAIRED_TEXT
    assert validation.repair_attempts == 1
    assert validation.violation_count == 2


@pytest.mark.asyncio
async def test_validator_failure_is_explicitly_failed_open(
    client: TestClient,
    db_session: AsyncSession,
):
    campaign_id = client.post(
        "/api/campaigns",
        json={"name": "Validator outage"},
    ).json()["id"]

    with patch(
        "app.services.narration_validation_guard._ORIGINAL_GENERATE_STREAM",
        side_effect=raw_narrator,
    ), patch.object(
        NarrationValidator,
        "validate",
        new_callable=AsyncMock,
        side_effect=NarrationValidationError("control model unavailable"),
    ):
        response = client.post(
            f"/api/campaigns/{campaign_id}/turns",
            json={"role": "user", "content": "Осматриваюсь."},
        )

    assert response.status_code == 200, response.text
    assert response.text == INVALID_DRAFT
    history = client.get(f"/api/campaigns/{campaign_id}/turns").json()
    assert history[-1]["content"] == INVALID_DRAFT
    validation = await latest_validation(db_session)
    assert validation.status == "failed_open"
    assert validation.final_text == INVALID_DRAFT
    assert "control model unavailable" in (validation.failure_reason or "")


def test_exhausted_repairs_never_publish_rejected_text(client: TestClient):
    campaign_id = client.post(
        "/api/campaigns",
        json={"name": "Rejected narration"},
    ).json()["id"]

    with patch(
        "app.services.narration_validation_guard._ORIGINAL_GENERATE_STREAM",
        side_effect=raw_narrator,
    ), patch.object(
        NarrationValidator,
        "validate",
        new_callable=AsyncMock,
        return_value=repair_required(),
    ):
        response = client.post(
            f"/api/campaigns/{campaign_id}/turns",
            json={"role": "user", "content": "Жду в тишине."},
        )

    assert response.status_code == 200, response.text
    assert INVALID_DRAFT not in response.text
    assert REPAIRED_TEXT not in response.text
    assert "Generation failed after retry" in response.text
    assert client.get(f"/api/campaigns/{campaign_id}/turns").json() == []
