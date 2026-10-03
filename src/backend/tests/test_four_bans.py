"""The narration contract is four bans judged by one validator call; everything else is allowed."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.narration_validation import NarrationValidationResult
from app.models.turn import ChatMessage
from app.models.turn_authority import PlannedNpcIntroduction, TurnAuthority
from app.services.authority_narration_pipeline import AuthorityNarrationPipeline
from app.services.narration_publication_guard import (
    NarrationPublicationError,
    NarrationPublicationGuard,
)
from app.services.turn_authority_validator import TurnAuthorityValidator, four_bans_only

pytestmark = pytest.mark.interagent_contract_enforced

DIALOGUE = "— Не знаю я никакого Шептуна, — бурчит дежурный.\n— Ступай, — добавляет он и прикрывает дверь."


def _authority(**updates) -> TurnAuthority:
    base = dict(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_character_name="Рэт",
        player_input="Стучу в дверь и спрашиваю про Шептуна.",
        scene_disposition="stay",
        present_character_names=["Рэт"],
        known_absent_character_names=["Шептун"],
        allowed_new_npcs=[
            PlannedNpcIntroduction(
                canonical_name="Дежурный",
                role="дежурный",
                reason="Открыл на стук.",
                temporary_name=True,
            )
        ],
        established_state=["дверь фабрики — заперта: нет."],
        observable_consequences=["Дежурный приоткрывает дверь."],
    )
    base.update(updates)
    return TurnAuthority(**base)


def _verdict(*violation_types: str) -> dict:
    if not violation_types:
        return {"verdict": "pass", "summary": "ok", "violations": []}
    return {
        "verdict": "repair_required",
        "summary": "нарушение",
        "violations": [
            {
                "violation_type": kind,
                "severity": "error",
                "evidence": "Рэт решает уйти",
                "correction": "Убрать решение героя.",
            }
            for kind in violation_types
        ],
    }


class _Router:
    def __init__(self, *verdicts: dict):
        self.verdicts = list(verdicts)
        self.calls: list[list[ChatMessage]] = []

    async def resolve(self, *args, **kwargs):
        return SimpleNamespace(config=SimpleNamespace(model_name="control"), source="test")

    async def generate_json(self, _provider, _selection, messages, **kwargs):
        assert issubclass(kwargs["response_model"], NarrationValidationResult)
        self.calls.append(messages)
        return self.verdicts.pop(0)


def test_validator_prompt_is_the_four_bans_and_allows_mentions_and_optional_speech():
    prompt = TurnAuthorityValidator.SYSTEM_PROMPT
    for ban in ("absent_character", "canon_conflict", "player_agency", "invalid_movement"):
        assert ban in prompt
    assert "MENTIONING someone who is not here is allowed" in prompt
    assert "None of that is required" in prompt


def test_validator_payload_holds_only_typed_four_bans_facts():
    payload = _authority().validator_payload()
    assert payload["present_characters"] == ["Рэт"]
    assert payload["known_absent_characters"] == ["Шептун"]
    assert payload["established_state"] == ["дверь фабрики — заперта: нет."]
    for removed in ("addressed_response", "question_responses", "scene_development", "covers_questions"):
        assert removed not in payload


def test_non_ban_errors_never_fail_a_narration():
    result = four_bans_only(NarrationValidationResult.model_validate(_verdict("meta_language", "other")))
    assert result.verdict == "pass"
    assert {item.severity for item in result.violations} == {"warning"}
    kept = four_bans_only(NarrationValidationResult.model_validate(_verdict("player_agency")))
    assert kept.verdict == "repair_required"


@pytest.mark.asyncio
async def test_one_validator_call_per_narration():
    router = _Router(_verdict())
    result = await TurnAuthorityValidator(router).validate(
        SimpleNamespace(config=SimpleNamespace(model_name="control")), _authority(), DIALOGUE,
    )
    assert result.verdict == "pass"
    assert len(router.calls) == 1
    assert DIALOGUE in router.calls[0][-1].content


@pytest.mark.asyncio
async def test_beat_grant_is_honored_only_by_an_exact_owner_fragment():
    owner = uuid4()
    authority = _authority(beat_owner_id=owner, beat_owner_name="Дежурный")
    assert authority.narrator_payload()["beat_owner"] == {"id": str(owner), "name": "Дежурный"}
    honored = {**_verdict(), "beat_owner_turn": {"form": "dialogue", "evidence": "Ступай"}}
    invented = {**_verdict(), "beat_owner_turn": {"form": "action", "evidence": "уходит прочь"}}
    silent = {**_verdict(), "beat_owner_turn": {"form": "none", "evidence": ""}}
    router = _Router(honored, invented, silent)
    validator = TurnAuthorityValidator(router)
    selection = SimpleNamespace(config=SimpleNamespace(model_name="control"))
    results = [await validator.validate(selection, authority, DIALOGUE) for _ in range(3)]
    assert [validator.beat_unhonored(authority, item, DIALOGUE) for item in results] == [
        False, True, True,
    ]
    assert "[BEAT OWNER] Дежурный" in router.calls[0][-1].content
    assert "Этот бит принадлежит Дежурный" in validator.repair_prompt(authority, DIALOGUE, results[2])
    assert not validator.beat_unhonored(_authority(), results[2], DIALOGUE)


def test_validated_dialogue_with_dashes_is_published_verbatim():
    published, audit = NarrationPublicationGuard.publish(
        _authority(), DIALOGUE, NarrationValidationResult.model_validate(_verdict()),
    )
    assert published == DIALOGUE
    assert audit["mode"] == "validated_candidate"


def test_fallback_stops_at_first_blocked_step():
    authority = _authority(
        scene_disposition="sequence",
        action_sequence={
            "steps": [
                {"status": "completed", "action_type": "service", "observable_outcome": "Вы платите за комнату."},
                {
                    "status": "blocked",
                    "action_type": "movement",
                    "public_blocking_reason": "Дверь на лестницу заперта.",
                },
                {"status": "skipped", "action_type": "rest"},
            ]
        },
    )
    assert NarrationPublicationGuard.render_authority(authority) == (
        "Вы платите за комнату. Дверь на лестницу заперта."
    )


def test_fallback_raises_only_without_any_typed_outcome():
    with pytest.raises(NarrationPublicationError):
        NarrationPublicationGuard.render_authority(_authority(observable_consequences=[]))


@pytest.mark.asyncio
async def test_rejected_repair_publishes_typed_outcome_after_exactly_two_checks(db_session, monkeypatch):
    from app.db.repositories.turn_repo import TurnRepository
    from app.models.campaign import CampaignCreate
    from app.models.turn import TurnCreate
    from app.services.campaign_service import CampaignService

    campaign = await CampaignService(db_session).create_campaign(CampaignCreate(name="Four bans"))
    user_turn = await TurnRepository(db_session).create(
        campaign.id, TurnCreate(role="user", content="Стучу в дверь."),
    )
    await db_session.commit()
    router = _Router(_verdict("player_agency"), _verdict("player_agency"))
    pipeline = AuthorityNarrationPipeline(db_session, router)
    drafts: list[str] = []

    async def narrate(messages, selection, *, temperature):
        drafts.append(messages[-1].content)
        return "Рэт решает уйти.", {"model": "narrator"}

    monkeypatch.setattr(pipeline, "_generate_text", narrate)
    authority = _authority(campaign_id=campaign.id, trigger_turn_id=user_turn.id)
    result = await pipeline.generate(
        campaign_id=campaign.id,
        trigger_turn_id=user_turn.id,
        scene_id=None,
        narrator_messages=[ChatMessage(role="system", content="Narrate.")],
        narrator_selection=SimpleNamespace(config=SimpleNamespace(model_name="narrator")),
        authority=authority,
    )
    assert result.text == "Дежурный приоткрывает дверь."
    assert result.validation_status == "safe_fallback"
    assert len(drafts) == 2 and "[REPAIR REJECTED NARRATION]" in drafts[1]
    assert len(router.calls) == 2


@pytest.mark.asyncio
async def test_validator_outage_keeps_the_unchecked_draft_off_the_surface(db_session, monkeypatch):
    from app.db.repositories.turn_repo import TurnRepository
    from app.models.campaign import CampaignCreate
    from app.models.turn import TurnCreate
    from app.services.campaign_service import CampaignService

    campaign = await CampaignService(db_session).create_campaign(CampaignCreate(name="Outage"))
    user_turn = await TurnRepository(db_session).create(
        campaign.id, TurnCreate(role="user", content="Стучу в дверь."),
    )
    await db_session.commit()

    class _Down(_Router):
        async def generate_json(self, *args, **kwargs):
            raise ValueError("control model unavailable")

    pipeline = AuthorityNarrationPipeline(db_session, _Down())

    async def narrate(messages, selection, *, temperature):
        return "Непроверенный черновик.", {"model": "narrator"}

    monkeypatch.setattr(pipeline, "_generate_text", narrate)
    result = await pipeline.generate(
        campaign_id=campaign.id,
        trigger_turn_id=user_turn.id,
        scene_id=None,
        narrator_messages=[ChatMessage(role="system", content="Narrate.")],
        narrator_selection=SimpleNamespace(config=SimpleNamespace(model_name="narrator")),
        authority=_authority(campaign_id=campaign.id, trigger_turn_id=user_turn.id),
    )
    assert result.text == "Дежурный приоткрывает дверь."
    assert result.validation_status == "failed_open"
