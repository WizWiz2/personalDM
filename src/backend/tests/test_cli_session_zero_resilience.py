from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import cli
from app.application import GameApplication
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
from app.services.entity_registrar import EntityRegistrationResult
from app.services.post_turn_processor import PostTurnProcessor
from app.services.session_zero_interview import SessionZeroInterviewService
from app.services.session_zero_service import SessionZeroService


INTERVIEW_DECISION = {
    "assistant_message": (
        "Кажется, основа уже ясна. Я собрал договорённости; проверь их перед стартом."
    ),
    "ready_to_finalize": True,
    "missing_topics": [],
    "summary": "Городское фэнтези Pathfinder 2e про самостоятельного искателя.",
    "draft": {
        "world": {
            "setting_name": "Лаэрн",
            "genre": "приземлённое городское фэнтези",
            "premise": "Искатель приключений ищет работу и постепенно влияет на город.",
            "tone": "атмосферно, спокойно, без обязательной угрозы",
            "themes": ["живые NPC", "бытовые сцены", "долгие линии"],
            "boundaries": ["не решать и не чувствовать за героя"],
            "boundaries_confirmed": True,
            "rules_system": "Pathfinder 2e",
            "world_summary": "Пограничный торговый город с редкой магией.",
            "play_style": "диалоги, исследование и последствия без спешки",
            "narrative_style": "романная атмосферная проза",
            "content_rating": "16+",
            "starting_location_name": "Площадь у гильдии проводников",
            "starting_situation": (
                "Эйдан только прибыл и видит доску доступной работы; "
                "его первый выбор остаётся за игроком."
            ),
            "starting_scene_title": "Первый день в Лаэрне",
        },
        "character": {
            "name": "Эйдан",
            "description": "Странствующий искатель приключений и проводник.",
            "appearance": "Высокий мужчина в дорожном плаще со старым шрамом.",
            "personality": "Спокойный, самостоятельный и прямой.",
            "values": ["свобода", "верность слову"],
            "fears": ["потерять самостоятельность"],
            "desires": ["закрепиться в городе"],
            "voice": "спокойный низкий голос",
            "speech_patterns": "говорит коротко и задаёт прямые вопросы",
            "biography": "Работал проводником на пограничных трактах.",
            "capabilities": ["ориентирование", "переговоры", "владение мечом"],
            "limitations": ["не владеет магией", "не знает местную знать"],
            "first_goal": "найти достойную работу",
        },
    },
}


@pytest.mark.asyncio
async def test_real_cli_interview_materializes_once_through_session_zero(
    db_session: AsyncSession,
):
    campaign = await CampaignService(db_session).create_campaign(
        CampaignCreate(name="Живая модельная нулевая сессия")
    )
    await db_session.commit()

    with (
        patch(
            "app.services.session_zero_interview.RoleModelRouter.generate_json",
            new_callable=AsyncMock,
            return_value=INTERVIEW_DECISION,
        ),
        patch("builtins.input", side_effect=["Хочу городское фэнтези.", "да"]),
    ):
        completed = await cli.run_session_zero_interview(campaign.id, db_session)

    assert completed is True
    setup = await SessionZeroService(db_session).get(campaign.id)
    assert setup.status == "completed"
    assert setup.rules_system == "Pathfinder 2e"
    assert setup.content_rating == "16+"
    assert setup.starting_location_name == "Площадь у гильдии проводников"
    assert "tavern" not in (setup.starting_location_name or "").casefold()

    card = await CharacterCardService(db_session).get_card(
        setup.player_character_id,
        campaign.id,
    )
    assert card.ready_for_play is True
    assert card.character.values == ["свобода", "верность слову"]
    assert card.character.fears == ["потерять самостоятельность"]
    assert card.character.values != card.character.fears
    assert card.capabilities == [
        "ориентирование",
        "переговоры",
        "владение мечом",
    ]
    assert card.limitations == ["не владеет магией", "не знает местную знать"]

    scenes = await SceneRepository(db_session).list_by_campaign(campaign.id)
    locations = await LocationRepository(db_session).list_by_campaign(campaign.id)
    assert len(scenes) == 1
    assert len(locations) == 1
    assert scenes[0].title == "Первый день в Лаэрне"


@pytest.mark.asyncio
async def test_rate_limited_interview_saves_pending_answer_without_world_objects(
    db_session: AsyncSession,
):
    campaign = await CampaignService(db_session).create_campaign(
        CampaignCreate(name="Отложенная беседа")
    )
    await db_session.commit()
    interview = SessionZeroInterviewService(db_session)

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new_callable=AsyncMock,
        side_effect=LLMProviderError("HTTP 429 rate_limit_exceeded"),
    ), pytest.raises(LLMProviderError, match="429"):
        await interview.answer(campaign.id, "Хочу медленную политическую игру.")

    state = await interview.get_state(campaign.id)
    assert state.pending_user_message == "Хочу медленную политическую игру."
    assert state.messages[-1] == {
        "role": "user",
        "content": "Хочу медленную политическую игру.",
    }
    assert await SceneRepository(db_session).list_by_campaign(campaign.id) == []
    assert await LocationRepository(db_session).list_by_campaign(campaign.id) == []
    assert await EntityRepository(db_session).list_by_campaign(campaign.id) == []

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new_callable=AsyncMock,
        return_value=INTERVIEW_DECISION,
    ):
        decision = await interview.retry_pending(campaign.id)
    assert decision is not None
    assert decision.ready_to_finalize is True
    assert (await interview.get_state(campaign.id)).pending_user_message is None


@pytest.mark.asyncio
async def test_cli_rate_limit_never_bootstraps_default_tavern(
    db_session: AsyncSession,
):
    campaign = await CampaignService(db_session).create_campaign(
        CampaignCreate(name="Без таверны при 429")
    )
    await db_session.commit()

    with (
        patch(
            "app.services.session_zero_interview.RoleModelRouter.generate_json",
            new_callable=AsyncMock,
            side_effect=LLMProviderError("HTTP 429 rate_limit_exceeded"),
        ),
        patch("builtins.input", side_effect=["Хочу научную фантастику."]),
    ):
        completed = await cli.run_session_zero_interview(campaign.id, db_session)

    assert completed is False
    current = await CampaignRepository(db_session).get_by_id(campaign.id)
    assert current.current_scene_id is None
    assert await SceneRepository(db_session).list_by_campaign(campaign.id) == []
    assert await LocationRepository(db_session).list_by_campaign(campaign.id) == []


@pytest.mark.asyncio
async def test_scribe_429_is_nonfatal_and_does_not_create_regex_bartender(
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
        LocationCreate(canonical_name="Общий зал"),
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
    await campaigns.update(
        campaign_id,
        CampaignUpdate(current_scene_id=scene.id),
    )
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
            side_effect=LLMProviderError("HTTP 429 rate_limit_exceeded"),
        ),
        patch(
            "app.services.thesis_curator.ThesisCurator.curate_after_turn",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        await processor.process_turn(assistant.id)

    participants = await entities.get_characters_in_scene(scene.id)
    assert {item.canonical_name for item in participants} == {"Эйдан"}
    assert not any(
        item.canonical_name == "Бармен"
        for item in await entities.list_by_campaign(campaign_id)
    )

    jobs = await PostTurnJobRepository(db_session).list_for_turn(assistant.id)
    memory_job = next(job for job in jobs if job.job_type == "memory_scribe")
    assert memory_job.status == "failed"
    assert "429" in (memory_job.error or "")
    assert GameApplication.is_rate_limited_error(memory_job.error) is True
