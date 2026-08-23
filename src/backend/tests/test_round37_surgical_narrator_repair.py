from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.db.repositories.turn_repo import TurnRepository
from app.models.campaign import CampaignCreate
from app.models.narration_validation import NarrationValidationResult, NarrationViolation
from app.models.turn import ChatMessage, TurnCreate
from app.models.turn_authority import TurnAuthority
from app.services.authority_narration_pipeline import AuthorityNarrationPipeline
from app.services.campaign_service import CampaignService
from app.services.narration_publication_guard import NarrationPublicationGuard
from app.services.narrator_quality_recovery_guard import narrator_ownership_violations
from app.services.role_model_router import RoleModelRouter
from app.services.turn_authority_validator import TurnAuthorityValidator


def _authority(**updates) -> TurnAuthority:
    base = dict(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_character_id=uuid4(),
        player_character_name="Александр",
        player_input="Я оглядываюсь.\n- Кто здесь?",
        scene_disposition="stay",
        transition_type="none",
        source_location_path=["перевал", "лагерь"],
        target_location_path=["перевал", "лагерь"],
        present_character_names=["Александр", "Тамар"],
        resolution="observation",
        observable_consequences=[],
        allow_new_complication=False,
    )
    base.update(updates)
    return TurnAuthority(**base)


def _passed() -> NarrationValidationResult:
    return NarrationValidationResult(verdict="pass", summary="ok", violations=[])


def _rejected(evidence: str, correction: str = "Удалить нарушение.") -> NarrationValidationResult:
    return NarrationValidationResult(
        verdict="repair_required",
        summary="Есть одно локальное нарушение.",
        violations=[
            NarrationViolation(
                violation_type="player_agency",
                severity="error",
                evidence=evidence,
                correction=correction,
            )
        ],
    )


def test_physical_paraphrase_of_authorized_action_is_not_lexically_rejected():
    authority = _authority(
        player_input="Вхожу в шатёр.",
        scene_disposition="location_transition",
        transition_type="location_transition",
        source_location_path=["перевал", "лагерь"],
        target_location_path=["перевал", "лагерь", "шатёр"],
    )
    candidate = "Вы делаете шаг внутрь шатра. Внутри тесно и пахнет сухой тканью."

    assert narrator_ownership_violations(authority, candidate) == []


def test_high_confidence_internal_state_is_still_rejected():
    authority = _authority(player_input="Осматриваюсь внутри.")
    candidate = "Вы понимаете, что нужно остаться, и чувствуете тревогу."

    violations = narrator_ownership_violations(authority, candidate)

    assert violations
    assert violations[0].violation_type == "player_agency"
    assert "понимаете" in violations[0].evidence.casefold()


def test_general_question_does_not_let_narrator_choose_tamar_as_addressee():
    authority = _authority()
    candidate = "Вы обращаетесь к Тамар. Она смотрит в сторону тропы."

    violations = narrator_ownership_violations(authority, candidate)

    assert violations
    assert any("обращаетесь к Тамар" in item.evidence for item in violations)


def test_present_tamar_is_protected_from_false_absent_character_verdict():
    authority = _authority(player_input="Что это за лагерь?")
    result = NarrationValidationResult(
        verdict="repair_required",
        summary="Тамар якобы отсутствует.",
        violations=[
            NarrationViolation(
                violation_type="absent_character",
                severity="error",
                evidence="Тамар отвечает негромко.",
                correction="Убрать отсутствующую Тамар.",
            )
        ],
    )

    filtered = TurnAuthorityValidator.apply_deterministic_authority(result, authority)

    assert filtered.verdict == "pass"
    assert filtered.violations == []


def test_surgical_candidate_preserves_good_npc_dialogue_and_removes_only_bad_span():
    bad = "Вы решаете подойти к костру."
    candidate = (
        "Тамар отвечает негромко: «Это место стоянки каравана, не настоящий лагерь». "
        "У края площадки потрескивают угли. "
        f"{bad} "
        "Туман закрывает дальний склон, но сама стоянка остаётся видна."
    )
    result = _rejected(bad)

    repaired, metadata = NarrationPublicationGuard.surgical_repair_candidate(candidate, result)

    assert repaired is not None
    assert bad not in repaired
    assert "Тамар отвечает негромко" in repaired
    assert "потрескивают угли" in repaired
    assert "Туман закрывает дальний склон" in repaired
    assert metadata["strategy"] == "deterministic_span_removal"
    assert metadata["retained_ratio"] > 0.6


def test_surgical_candidate_refuses_fuzzy_or_unmatched_validator_evidence():
    candidate = "Тамар отвечает на вопрос. Ветер шевелит край шатра."
    result = _rejected("Нарратор сделал что-то не то.")

    repaired, metadata = NarrationPublicationGuard.surgical_repair_candidate(candidate, result)

    assert repaired is None
    assert metadata["reason"] == "not_all_error_evidence_matched"


def test_model_repair_prompt_edits_original_in_place_instead_of_starting_over():
    authority = _authority(player_input="Что это за лагерь?")
    candidate = (
        "Тамар отвечает: «Это стоянка каравана». "
        "Вы решаете немедленно уйти к тропе."
    )
    result = _rejected("Вы решаете немедленно уйти к тропе.")

    prompt = TurnAuthorityValidator.repair_prompt(authority, candidate, result)

    assert candidate in prompt
    assert "EDIT IN PLACE" in prompt
    assert "С НУЛЯ" not in prompt
    assert "не заменяй легальную реплику NPC на молчание" in prompt


@pytest.mark.asyncio
async def test_authority_pipeline_revalidates_surgical_candidate_before_publication(
    db_session,
    monkeypatch,
):
    campaign = await CampaignService(db_session).create_campaign(
        CampaignCreate(name="Round 37 surgical repair")
    )
    user_turn = await TurnRepository(db_session).create(
        campaign.id,
        TurnCreate(role="user", content="Что это за лагерь?"),
    )
    await db_session.commit()

    good_dialogue = "Тамар отвечает: «Это стоянка каравана, не настоящий лагерь»."
    bad = "Вы решаете подойти к ней вплотную."
    draft = f"{good_dialogue} Угли у палаток ещё тлеют. {bad}"

    class FakeRouter:
        async def resolve(self, *args, **kwargs):
            return SimpleNamespace(
                config=SimpleNamespace(model_name="qwen2.5:7b"),
                source="control_default",
            )

    pipeline = AuthorityNarrationPipeline(db_session, FakeRouter())

    async def one_draft(messages, selection, *, temperature):
        return draft, {"model": "gemma4:e4b"}

    calls = 0

    async def reject_then_pass(self, selection, authority, candidate_text):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _rejected(bad)
        assert bad not in candidate_text
        assert good_dialogue in candidate_text
        return _passed()

    monkeypatch.setattr(pipeline, "_generate_text", one_draft)
    monkeypatch.setattr(TurnAuthorityValidator, "validate", reject_then_pass)

    authority = _authority(
        campaign_id=campaign.id,
        trigger_turn_id=user_turn.id,
        player_input="Что это за лагерь?",
        observable_consequences=["Тамар может ответить на вопрос о стоянке."],
    )
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

    assert result.validation_status == "repaired"
    assert good_dialogue in result.text
    assert bad not in result.text
    assert result.telemetry["narration_validation"]["repair_strategy"] == (
        "deterministic_span_removal"
    )
    assert calls == 2
