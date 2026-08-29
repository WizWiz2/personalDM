from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.repositories.turn_repo import TurnRepository
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.location import LocationCreate
from app.models.narration_validation import NarrationValidationResult
from app.models.scene import SceneCreate
from app.models.turn import ChatMessage, TurnCreate
from app.models.turn_authority import TurnAuthority
from app.services.authority_narration_pipeline import AuthorityNarrationPipeline
from app.services.narration_repetition_guard import NarrationRepetitionGuard
from app.services.scene_lifecycle import SceneLifecycleService
from app.services.turn_authority_validator import TurnAuthorityValidator


PREVIOUS_REPLY = (
    "Бармен медленно протирает стакан и пожимает плечами. «Я просто подаю напитки, мисс. "
    "Про эту Морскую Звезду я уже сказал всё, что знаю»."
)
DISTINCT_REPLY = (
    "Бармен ставит стакан на стойку. «Нет, мисс. Больше добавить нечего. Если тот человек "
    "вернётся, я дам вам знать»."
)


class FakeRouter:
    async def resolve(self, *args, **kwargs):
        return SimpleNamespace(
            config=SimpleNamespace(model_name="qwen2.5:7b"),
            source="control_default",
        )


async def _setup_actor_turn(db_session: AsyncSession):
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    entities = EntityRepository(db_session)
    locations = LocationRepository(db_session)
    scenes = SceneRepository(db_session)
    turns = TurnRepository(db_session)

    await campaigns.create(campaign_id, CampaignCreate(name="Repetition guard"))
    tavern = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Портовой трактир"),
    )
    hero = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Мария"),
    )
    bartender = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Бармен"),
    )
    await campaigns.update(
        campaign_id,
        CampaignUpdate(player_character_id=hero.id),
    )
    scene = await scenes.create(
        campaign_id,
        SceneCreate(title="У стойки", location_id=tavern.id),
    )
    await scenes.add_participant(scene.id, hero.id)
    await scenes.add_participant(scene.id, bartender.id)
    await SceneLifecycleService(db_session).activate(campaign_id, scene.id)

    old_user = await turns.create(
        campaign_id,
        TurnCreate(
            role="user",
            content="Что вы знаете о Морской Звезде?",
            scene_id=scene.id,
            acting_character_id=bartender.id,
        ),
    )
    await turns.create(
        campaign_id,
        TurnCreate(
            role="assistant",
            content=PREVIOUS_REPLY,
            scene_id=scene.id,
            acting_character_id=bartender.id,
            parent_turn_id=old_user.id,
            model_name="gemma4:e4b",
        ),
    )
    current = await turns.create(
        campaign_id,
        TurnCreate(
            role="user",
            content="А больше ничего не припомните?",
            scene_id=scene.id,
            acting_character_id=bartender.id,
        ),
    )
    await db_session.commit()

    authority = TurnAuthority(
        campaign_id=campaign_id,
        trigger_turn_id=current.id,
        player_character_name="Мария",
        player_input=current.content,
        acting_character_id=bartender.id,
        acting_character_name="Бармен",
        scene_disposition="actor_turn",
        ending_hook="Бармен сохраняет прежнюю позицию.",
    )
    return campaign_id, scene.id, authority


def test_guard_detects_near_verbatim_but_not_new_response():
    guard = NarrationRepetitionGuard(SimpleNamespace())
    repeated = PREVIOUS_REPLY.replace("медленно ", "")
    match = guard.detect(repeated, [PREVIOUS_REPLY], actor_turn=True)
    assert match is not None
    assert match.similarity >= guard.ACTOR_THRESHOLD
    assert guard.detect(DISTINCT_REPLY, [PREVIOUS_REPLY], actor_turn=True) is None


@pytest.mark.asyncio
async def test_actor_repeat_gets_one_retry_before_validation(
    db_session: AsyncSession,
    monkeypatch,
):
    campaign_id, scene_id, authority = await _setup_actor_turn(db_session)
    pipeline = AuthorityNarrationPipeline(db_session, FakeRouter())
    generated = iter(
        [
            (PREVIOUS_REPLY, {"model": "gemma4:e4b", "attempt": 1}),
            (DISTINCT_REPLY, {"model": "gemma4:e4b", "attempt": 2}),
        ]
    )

    async def fake_generate(messages, selection, *, temperature):
        return next(generated)

    async def pass_validation(self, selection, current_authority, candidate):
        return NarrationValidationResult(
            verdict="pass",
            summary="within authority",
            violations=[],
        )

    monkeypatch.setattr(pipeline, "_generate_text", fake_generate)
    monkeypatch.setattr(TurnAuthorityValidator, "validate", pass_validation)

    result = await pipeline.generate(
        campaign_id=campaign_id,
        trigger_turn_id=authority.trigger_turn_id,
        scene_id=scene_id,
        narrator_messages=[
            ChatMessage(role="system", content="Roleplay the selected NPC."),
            ChatMessage(role="user", content=authority.player_input),
        ],
        narrator_selection=SimpleNamespace(
            config=SimpleNamespace(model_name="gemma4:e4b")
        ),
        authority=authority,
    )

    assert result.text == DISTINCT_REPLY
    assert result.validation_status == "passed"
    repetition = result.telemetry["repetition_guard"]
    assert repetition["detected"] is True
    assert repetition["retried"] is True
    assert repetition["exhausted"] is False


@pytest.mark.asyncio
async def test_persistent_repeat_publishes_authority_instead_of_looping(
    db_session: AsyncSession,
    monkeypatch,
):
    campaign_id, scene_id, authority = await _setup_actor_turn(db_session)
    pipeline = AuthorityNarrationPipeline(db_session, FakeRouter())
    calls = 0

    async def always_repeat(messages, selection, *, temperature):
        nonlocal calls
        calls += 1
        return PREVIOUS_REPLY, {"model": "gemma4:e4b", "attempt": calls}

    async def validator_must_not_run(self, selection, current_authority, candidate):
        raise AssertionError(
            "deterministic TurnAuthority projection is already the authority source"
        )

    monkeypatch.setattr(pipeline, "_generate_text", always_repeat)
    monkeypatch.setattr(TurnAuthorityValidator, "validate", validator_must_not_run)

    result = await pipeline.generate(
        campaign_id=campaign_id,
        trigger_turn_id=authority.trigger_turn_id,
        scene_id=scene_id,
        narrator_messages=[
            ChatMessage(role="system", content="Roleplay the selected NPC."),
            ChatMessage(role="user", content=authority.player_input),
        ],
        narrator_selection=SimpleNamespace(
            config=SimpleNamespace(model_name="gemma4:e4b")
        ),
        authority=authority,
    )

    assert calls == 2
    assert result.validation_status == "safe_fallback"
    assert result.text == "Бармен умолкает."
    assert "сохраняет прежнюю позицию" not in result.text
    assert result.text != PREVIOUS_REPLY
    assert result.telemetry["repetition_guard"]["exhausted"] is True
    assert (
        result.telemetry["narration_validation"]["semantic_failure_recovered"]
        is True
    )
