from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import cli
from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.job_repo import PostTurnJobRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.repositories.turn_repo import TurnRepository
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.location import LocationCreate
from app.models.scene import SceneCreate
from app.models.turn import TurnCreate
from app.providers.llm_provider import LLMProviderError
from app.services.campaign_service import CampaignService
from app.services.character_card_service import CharacterCardService
from app.services.conversational_session_zero import (
    ConversationalSessionZeroService,
)
from app.services.entity_registrar import EntityRegistrationResult
from app.services.post_turn_processor import PostTurnProcessor
from app.services.session_zero_service import SessionZeroService


ANSWERS = {
    "world": "Приземлённое фэнтези пограничных городов, редкая магия.",
    "adventure": "Искать работу, знакомиться с людьми и постепенно влиять на город.",
    "play_style": "Диалоги, исследование и управление последствиями без спешки.",
    "tone": "Атмосферно и серьёзно, но без постоянной угрозы.",
    "wanted": "Живые NPC, бытовые сцены, отношения и долгие сюжетные линии.",
    "boundaries": "Не принимать решения и не описывать чувства за героя.",
    "hero_name": "Эйдан",
    "hero_concept": "Странствующий искатель приключений с опытом проводника.",
    "hero_capabilities": "ориентирование, переговоры, владение мечом",
    "hero_limitations": "не владеет магией, плохо разбирается в дворцовых интригах",
    "hero_goal": "найти достойную работу и закрепиться в городе",
    "hero_values": "свобода, верность слову, страх потерять самостоятельность",
    "hero_appearance": "Высокий мужчина в дорожном плаще со старым шрамом.",
    "hero_voice": "Говорит спокойно, коротко и задаёт прямые вопросы.",
    "opening_location": "Площадь у гильдии проводников",
    "opening_situation": "Эйдан только прибыл и видит доску доступной работы.",
}


@pytest.mark.asyncio
async def test_conversational_session_zero_persists_resumes_and_completes(
    db_session: AsyncSession,
):
    campaign = await CampaignService(db_session).create_campaign(
        CampaignCreate(name="Живая нулевая сессия")
    )
    await db_session.commit()
    interview = ConversationalSessionZeroService(db_session)

    for key, value in list(ANSWERS.items())[:5]:
        await interview.save_answer(campaign.id, key, value)
    resumed = ConversationalSessionZeroService(db_session)
    stored = await resumed.get_answers(campaign.id)
    assert stored["world"] == ANSWERS["world"]
    assert len(await resumed.missing_questions(campaign.id)) == len(ANSWERS) - 5

    for key, value in list(ANSWERS.items())[5:]:
        await resumed.save_answer(campaign.id, key, value)
    completed = await resumed.finalize(campaign.id)

    setup = await SessionZeroService(db_session).get(campaign.id)
    assert setup.status == "completed"
    assert setup.missing_fields == []
    assert setup.starting_location_name == ANSWERS["opening_location"]
    assert completed.scene.title == f"Начало: {ANSWERS['opening_location']}"
    assert "tavern" not in (completed.scene.location_description or "").casefold()

    card = await CharacterCardService(db_session).get_card(
        setup.player_character_id,
        campaign.id,
    )
    assert card.ready_for_play is True
    assert card.character.canonical_name == "Эйдан"
    assert card.capabilities == ["ориентирование", "переговоры", "владение мечом"]
    assert card.limitations == [
        "не владеет магией",
        "плохо разбирается в дворцовых интригах",
    ]
    assert card.goals[0].description == ANSWERS["hero_goal"]


@pytest.mark.asyncio
async def test_incomplete_cli_campaign_never_bootstraps_default_tavern(
    db_session: AsyncSession,
):
    campaign = await CampaignService(db_session).create_campaign(
        CampaignCreate(name="Без дефолтной таверны")
    )
    await db_session.commit()

    with patch.object(
        cli,
        "run_session_zero_interview",
        new_callable=AsyncMock,
        return_value=False,
    ) as interview:
        await cli.play_game_loop(
            campaign.id,
            db_session,
            CampaignService(db_session),
        )

    interview.assert_awaited_once()
    current = await CampaignRepository(db_session).get_by_id(campaign.id)
    assert current.current_scene_id is None
    assert await SceneRepository(db_session).list_by_campaign(campaign.id) == []
    assert await LocationRepository(db_session).list_by_campaign(campaign.id) == []


@pytest.mark.asyncio
async def test_scribe_429_keeps_registered_bartender_and_failed_job_audit(
    db_session: AsyncSession,
):
    campaign_id = uuid4()
    campaigns = CampaignRepository(db_session)
    locations = LocationRepository(db_session)
    entities = EntityRepository(db_session)
    scenes = SceneRepository(db_session)
    turns = TurnRepository(db_session)

    await campaigns.create(campaign_id, CampaignCreate(name="Rate limit resilience"))
    location = await locations.create(
        campaign_id,
        LocationCreate(canonical_name="Общий зал Медного Котла"),
    )
    hero = await entities.create_character(
        campaign_id,
        CharacterCreate(canonical_name="Эйдан", current_location_id=location.id),
    )
    await campaigns.update(
        campaign_id,
        CampaignUpdate(player_character_id=hero.id),
    )
    scene = await scenes.create(
        campaign_id,
        SceneCreate(title="Общий зал", location_id=location.id),
    )
    await scenes.add_participant(scene.id, hero.id)
    await campaigns.update(campaign_id, CampaignUpdate(current_scene_id=scene.id))
    user = await turns.create(
        campaign_id,
        TurnCreate(role="user", content="Я ищу работу.", scene_id=scene.id),
    )
    assistant = await turns.create(
        campaign_id,
        TurnCreate(
            role="assistant",
            content="Бармен говорит: «Какой вид работы вас интересует?»",
            scene_id=scene.id,
            parent_turn_id=user.id,
        ),
    )
    processor = PostTurnProcessor(db_session)
    await processor.enqueue(campaign_id, assistant.id)
    await db_session.commit()

    with (
        patch(
            "app.services.entity_registrar.EntityRegistrar.register_from_turn",
            new_callable=AsyncMock,
            return_value=EntityRegistrationResult(),
        ),
        patch(
            "app.services.memory_scribe.MemoryScribe.extract_proposals",
            new_callable=AsyncMock,
            side_effect=LLMProviderError(
                "LLM returned HTTP 429: rate_limit_exceeded; retry after 4.76s"
            ),
        ),
        patch(
            "app.services.thesis_curator.ThesisCurator.curate_after_turn",
            new_callable=AsyncMock,
            return_value=None,
        ),
        pytest.raises(LLMProviderError, match="429"),
    ):
        await processor.process_turn(assistant.id)

    participants = await entities.get_characters_in_scene(scene.id)
    assert {item.canonical_name for item in participants} == {"Эйдан", "Бармен"}
    bartender = next(item for item in participants if item.canonical_name == "Бармен")
    assert bartender.custom_fields["registrar"] == "deterministic_role_fallback"

    jobs = await PostTurnJobRepository(db_session).list_for_turn(assistant.id)
    memory_job = next(job for job in jobs if job.job_type == "memory_scribe")
    assert memory_job.status == "failed"
    assert "429" in (memory_job.error or "")
    assert cli._rate_limited(memory_job.error) is True
