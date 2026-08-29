from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import pytest

from app.config import ModelRole
from app.models.narration_validation import (
    NarrationValidationResult,
    NarrationViolation,
)
from app.models.role_model import RoleModelConfig, RoleModelSelection
from app.models.turn_authority import TurnAuthority
from app.services.narration_publication_guard import NarrationPublicationGuard


def _validation(*violations: dict) -> NarrationValidationResult:
    return NarrationValidationResult(
        valid=not violations,
        violations=[NarrationViolation.model_validate(item) for item in violations],
        summary="test",
    )


def _selection() -> RoleModelSelection:
    config = RoleModelConfig(
        id=uuid4(),
        role=ModelRole.NARRATOR,
        provider="ollama",
        model_name="test-narrator",
        base_url="http://localhost:11434",
        temperature=0.2,
        max_tokens=256,
        timeout_seconds=30,
        enabled=True,
        priority=1,
        has_api_key=False,
        context_window=6144,
        created_at=datetime.utcnow(),
    )
    return RoleModelSelection(
        role=ModelRole.NARRATOR,
        config=config,
        api_key=None,
        fallback_config=config,
        fallback_api_key=None,
        source="test",
    )


def test_actor_publication_guard_discards_rejected_actor_candidate():
    authority = TurnAuthority(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_character_name="Рэт Уайтмоур",
        acting_character_name="Старуха Грета",
        player_input="В какой дом сворачивала тень?",
        scene_disposition="actor_turn",
    )
    candidate = (
        "Грета хмурится. «Тень свернула к старому складу у фабрики», — шепчет она. "
        "Рэт Уайтмоур кивнул, записывая детали. «Спасибо, Грета», — сказал Рэт Уайтмоур."
    )
    validation = _validation(
        {
            "violation_type": "player_agency",
            "severity": "error",
            "evidence": "Рэт Уайтмоур кивнул, записывая детали.",
            "correction": "Убрать придуманное действие героя.",
        },
        {
            "violation_type": "player_agency",
            "severity": "error",
            "evidence": "«Спасибо, Грета», — сказал Рэт Уайтмоур.",
            "correction": "Убрать придуманную реплику героя.",
        },
    )

    published, diagnostics = NarrationPublicationGuard.publish(
        authority,
        candidate,
        validation,
    )

    # A rejected actor draft is completely untrusted at the publication boundary. Repair happens
    # upstream and must be revalidated; exhausted publication falls back to a deterministic
    # authority projection rather than retaining any apparently useful sentence from the draft.
    assert "Тень свернула" not in published
    assert "кивнул" not in published
    assert "Спасибо, Грета" not in published
    assert published == "Старуха Грета пока не отвечает."
    assert diagnostics["mode"] == "authority_projection"
    assert diagnostics["candidate_discarded"] is True


def test_unresolved_semantic_violation_projects_authority_instead_of_bad_prose():
    authority = TurnAuthority(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_character_name="Рэт",
        player_input="Подхожу к двери и стучу.",
        scene_disposition="stay",
        observable_consequences=["За дверью пока тихо"],
    )
    candidate = "Дверь распахивается, и хозяин приглашает Рэта внутрь."
    validation = _validation(
        {
            "violation_type": "canon_conflict",
            "severity": "error",
            "evidence": candidate,
            "correction": "Не придумывать открытие двери.",
        }
    )

    published, diagnostics = NarrationPublicationGuard.publish(
        authority,
        candidate,
        validation,
    )

    assert published == "За дверью пока тихо."
    assert diagnostics["mode"] == "authority_projection"
    assert diagnostics["candidate_discarded"] is True


@pytest.mark.asyncio
async def test_second_semantic_reject_publishes_authority_instead_of_failing_turn(
    monkeypatch,
):
    from app.services.authority_narration_pipeline import AuthorityNarrationPipeline

    authority = TurnAuthority(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_character_name="Рэт",
        player_input="Стучу в дверь.",
        scene_disposition="stay",
        observable_consequences=["За дверью слышен осторожный шорох"],
    )

    class FakeProvider:
        async def generate_stream(self, **kwargs):
            yield "Дверь открывается сама."

    class FakeValidator:
        def __init__(self, *args, **kwargs):
            self.calls = 0

        async def validate(self, **kwargs):
            self.calls += 1
            return _validation(
                {
                    "violation_type": "canon_conflict",
                    "severity": "error",
                    "evidence": kwargs.get("draft", ""),
                    "correction": "Не открывать дверь.",
                }
            )

    monkeypatch.setattr(
        "app.services.authority_narration_pipeline.LLMProvider",
        lambda *args, **kwargs: FakeProvider(),
    )
    monkeypatch.setattr(
        "app.services.authority_narration_pipeline.TurnAuthorityValidator",
        FakeValidator,
    )

    pipeline = AuthorityNarrationPipeline.__new__(AuthorityNarrationPipeline)
    pipeline._session = None
    pipeline._provider = FakeProvider()
    pipeline._validator = FakeValidator()
    pipeline._selection = _selection()
    pipeline._validation_repo = None

    result = await pipeline._generate_with_validation(
        authority=authority,
        messages=[],
        selection=_selection(),
        scene_id=None,
    )

    assert "Дверь открывается" not in result.text
    assert "За дверью слышен осторожный шорох" in result.text


@pytest.mark.asyncio
async def test_real_truncation_gets_one_continuation_attempt(monkeypatch):
    from app.services.authority_narration_pipeline import AuthorityNarrationPipeline

    authority = TurnAuthority(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_character_name="Рэт",
        player_input="Осматриваюсь.",
        scene_disposition="stay",
    )

    class FakeProvider:
        def __init__(self):
            self.calls = 0

        async def generate_stream(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                yield "Коридор тянется к лестнице, и где-то наверху слышен"
            else:
                yield " тихий звон стекла."

    class PassValidator:
        async def validate(self, **kwargs):
            return _validation()

    provider = FakeProvider()
    monkeypatch.setattr(
        "app.services.authority_narration_pipeline.LLMProvider",
        lambda *args, **kwargs: provider,
    )
    monkeypatch.setattr(
        "app.services.authority_narration_pipeline.TurnAuthorityValidator",
        lambda *args, **kwargs: PassValidator(),
    )

    pipeline = AuthorityNarrationPipeline.__new__(AuthorityNarrationPipeline)
    pipeline._session = None
    pipeline._provider = provider
    pipeline._validator = PassValidator()
    pipeline._selection = _selection()
    pipeline._validation_repo = None

    result = await pipeline._generate_with_validation(
        authority=authority,
        messages=[],
        selection=_selection(),
        scene_id=None,
    )

    assert provider.calls == 2
    assert "тихий звон стекла" in result.text
