from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.db.narration_validation_table import NarrationValidationRun
from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.db.repositories.turn_repo import TurnRepository
from app.models.campaign import CampaignCreate
from app.models.narration_validation import NarrationValidationResult
from app.models.turn import ChatMessage, TurnCreate
from app.models.turn_authority import TurnAuthority
from app.providers.llm_provider import LLMProviderError, LLMProviderTruncatedError
from app.services.authority_narration_pipeline import AuthorityNarrationPipeline
from app.services.campaign_service import CampaignService
from app.services.narration_failure_containment_guard import install as install_containment
from app.services.role_model_router import RoleModelRouter
from app.services.turn_authority_validator import TurnAuthorityValidator


def _authority(*, campaign_id=None, trigger_turn_id=None) -> TurnAuthority:
    return TurnAuthority(
        campaign_id=campaign_id or uuid4(),
        trigger_turn_id=trigger_turn_id or uuid4(),
        player_character_name="Мария",
        player_input="Осматриваю дверь.",
        observable_consequences=["Дверь оказывается открыта."],
        scene_disposition="stay",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        LLMProviderTruncatedError(
            "LLM produced completion budget exhausted (finish_reason='length')",
            partial_text="Оборванный технически непригодный текст",
        ),
        LLMProviderError("LLM returned HTTP 500: internal provider failure"),
    ],
)
async def test_provider_failure_after_authority_publishes_safe_projection(
    db_session,
    monkeypatch,
    error,
):
    install_containment()
    pipeline = AuthorityNarrationPipeline(
        db_session,
        RoleModelRouter(ProviderConfigRepository(db_session)),
    )

    async def fail_generation(messages, selection, *, temperature):
        raise error

    monkeypatch.setattr(pipeline, "_generate_text", fail_generation)
    authority = _authority()

    result = await pipeline.generate(
        campaign_id=authority.campaign_id,
        trigger_turn_id=authority.trigger_turn_id,
        scene_id=None,
        narrator_messages=[ChatMessage(role="system", content="Narrate.")],
        narrator_selection=SimpleNamespace(
            config=SimpleNamespace(model_name="gemma4:e4b")
        ),
        authority=authority,
    )

    assert result.text == "Дверь оказывается открыта."
    assert "Generation failed" not in result.text
    assert "completion budget" not in result.text
    assert "HTTP 500" not in result.text
    assert result.validation_status == "safe_fallback"
    assert result.telemetry["narration_degraded"] is True
    assert result.telemetry["structured_outcome_preserved"] is True
    assert (
        result.telemetry["narration_validation"]["presentation_failure_recovered"]
        is True
    )


@pytest.mark.asyncio
async def test_repair_generation_failure_finalizes_validation_audit_and_preserves_authority(
    db_session,
    monkeypatch,
):
    install_containment()
    campaign = await CampaignService(db_session).create_campaign(
        CampaignCreate(name="Presentation containment")
    )
    user_turn = await TurnRepository(db_session).create(
        campaign.id,
        TurnCreate(role="user", content="Осматриваю дверь."),
    )
    await db_session.commit()

    class FakeRouter:
        async def resolve(self, *args, **kwargs):
            return SimpleNamespace(
                config=SimpleNamespace(model_name="qwen2.5:7b"),
                source="control_default",
            )

    pipeline = AuthorityNarrationPipeline(db_session, FakeRouter())
    calls = 0

    async def generate_then_truncate(messages, selection, *, temperature):
        nonlocal calls
        calls += 1
        if calls == 1:
            return "Мария решает войти внутрь.", {"model": "gemma4:e4b"}
        raise LLMProviderTruncatedError(
            "LLM produced completion budget exhausted (finish_reason='length')",
            partial_text="Мария",
        )

    async def reject_player_agency(self, selection, authority, candidate_text):
        return NarrationValidationResult.model_validate(
            {
                "verdict": "repair_required",
                "summary": "Нельзя принимать решение за игрока.",
                "violations": [
                    {
                        "violation_type": "player_agency",
                        "severity": "error",
                        "evidence": "Мария решает войти внутрь.",
                        "correction": "Оставить решение игроку.",
                    }
                ],
            }
        )

    monkeypatch.setattr(pipeline, "_generate_text", generate_then_truncate)
    monkeypatch.setattr(TurnAuthorityValidator, "validate", reject_player_agency)

    authority = _authority(campaign_id=campaign.id, trigger_turn_id=user_turn.id)
    result = await pipeline.generate(
        campaign_id=campaign.id,
        trigger_turn_id=user_turn.id,
        scene_id=None,
        narrator_messages=[ChatMessage(role="system", content="Narrate.")],
        narrator_selection=SimpleNamespace(
            config=SimpleNamespace(model_name="gemma4:e4b")
        ),
        authority=authority,
    )

    run = (
        await db_session.execute(
            select(NarrationValidationRun).where(
                NarrationValidationRun.trigger_turn_id == str(user_turn.id)
            )
        )
    ).scalar_one()

    assert result.text == "Дверь оказывается открыта."
    assert result.validation_status == "safe_fallback"
    assert run.status == "repaired"
    assert run.final_text == result.text
    assert run.failure_reason is not None
    assert "LLMProviderTruncatedError" in run.failure_reason
    assert "completion budget" not in result.text
