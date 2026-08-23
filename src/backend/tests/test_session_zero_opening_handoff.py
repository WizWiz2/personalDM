from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.turn_repo import TurnRepository
from app.models.campaign import CampaignCreate
from app.runtime import install_runtime
from app.services.campaign_service import CampaignService
from app.services.session_zero_interview import SessionZeroInterviewService


install_runtime()


START_PATCH = {
    "world": {
        "setting_name": "Эшфорд",
        "genre": "городское мистическое расследование",
        "world_summary": "Небольшой город, где за спокойным фасадом происходят странные вещи.",
        "tone": "ночная тревога и приземлённая мистика",
        "starting_location_name": "окраина города Эшфорд",
        "starting_situation": "Посреди ночи в шатре директора появились странные гости.",
        "starting_scene_title": "Начало: окраина города Эшфорд",
        "starter_presence_confirmed": True,
        "starter_npcs": [],
    },
    "character": {
        "name": "Александр",
        "description": "Частный сыщик, привыкший разбираться в странных историях.",
        "first_goal": "Понять, что происходит в шатре директора.",
    },
}


def _decision(message: str):
    return {
        "assistant_message": message,
        "tool_calls": [
            {"name": "update_session_zero", "patch": START_PATCH},
            {"name": "finalize_session_zero"},
        ],
        "question_topics": [],
    }


async def _opening_stream(*args, **kwargs):
    yield (
        "Ночь легла на окраину Эшфорда плотным тёмным слоем. За последними домами "
        "город быстро растворяется в сырой земле, редких огнях и силуэтах временных строений. "
        "Здесь тише, чем в центре, и каждый отдельный звук кажется ближе.\n\n"
        "Шатёр директора стоит среди этой темноты как единственная точка, вокруг которой "
        "собралось всё напряжение ночи. Ткань стен едва заметно шевелится, а изнутри пробивается "
        "тусклый свет, слишком слабый, чтобы разобрать происходящее снаружи.\n\n"
        "Именно здесь начинается история Александра. Он уже находится на окраине города; "
        "ему не нужно никуда прибывать или выполнять чужую команду, чтобы приключение началось. "
        "Есть только место, странные гости внутри и причина выяснить, что происходит.\n\n"
        "Эшфорд пока не раскрывает своих секретов. Но этой ночью один из них находится совсем рядом — "
        "за тонкой стеной шатра. Дальше мир будет отвечать на решения Александра."
    )


@pytest.mark.asyncio
async def test_ready_session_zero_message_is_terminal_even_if_model_asks_question(
    db_session: AsyncSession,
):
    campaign = await CampaignService(db_session).create_campaign(
        CampaignCreate(name="Terminal handoff")
    )
    await db_session.commit()
    interview = SessionZeroInterviewService(db_session)

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new_callable=AsyncMock,
        return_value=_decision("Всё готово. Ну что, начинаем?"),
    ):
        result = await interview.answer(campaign.id, "Да, запускай")

    assert result.ready_to_finalize is True
    assert result.assistant_message == (
        "Всё готово. Нулевая сессия завершена — начинаем приключение."
    )
    assert "?" not in result.assistant_message
    assert result.question_topics == []


@pytest.mark.asyncio
async def test_finalize_persists_one_system_owned_opening_assistant_turn(
    db_session: AsyncSession,
):
    campaign = await CampaignService(db_session).create_campaign(
        CampaignCreate(name="Opening handoff")
    )
    await db_session.commit()
    interview = SessionZeroInterviewService(db_session)

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new_callable=AsyncMock,
        return_value=_decision("Готово. Переходим к игре?"),
    ):
        decision = await interview.answer(campaign.id, "Начинаем")
    assert decision.ready_to_finalize is True

    with patch(
        "app.providers.llm_provider.LLMProvider.generate_stream",
        side_effect=_opening_stream,
    ):
        completion = await interview.finalize(campaign.id)
        # A browser/API retry after the successful finalize must not duplicate the opening.
        retry = await interview.finalize(campaign.id)

    assert retry.scene.id == completion.scene.id
    history = await TurnRepository(db_session).get_history(
        campaign.id,
        limit=20,
        channel="narrative",
    )
    openings = [
        turn
        for turn in history
        if turn.role == "assistant"
        and "session_zero_opening" in str(turn.context_snapshot)
    ]
    assert len(openings) == 1
    opening = openings[0]
    assert opening.parent_turn_id is None
    assert opening.scene_id == completion.scene.id
    assert opening.model_name is not None
    assert "Ночь легла на окраину Эшфорда" in opening.content
    assert len(opening.content) > 700
    assert "Что вы делаете дальше?" not in opening.content


@pytest.mark.asyncio
async def test_opening_falls_back_to_persisted_scene_when_narrator_is_unavailable(
    db_session: AsyncSession,
):
    campaign = await CampaignService(db_session).create_campaign(
        CampaignCreate(name="Opening fallback")
    )
    await db_session.commit()
    interview = SessionZeroInterviewService(db_session)

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new_callable=AsyncMock,
        return_value=_decision("Начинаем?"),
    ):
        await interview.answer(campaign.id, "Погнали")

    async def broken_stream(*args, **kwargs):
        from app.providers.llm_provider import LLMProviderError
        raise LLMProviderError("offline")
        yield ""  # pragma: no cover

    with patch(
        "app.providers.llm_provider.LLMProvider.generate_stream",
        side_effect=broken_stream,
    ):
        completion = await interview.finalize(campaign.id)

    history = await TurnRepository(db_session).get_history(
        campaign.id,
        limit=20,
        channel="narrative",
    )
    opening = next(
        turn
        for turn in history
        if turn.role == "assistant"
        and "session_zero_opening" in str(turn.context_snapshot)
    )
    assert opening.scene_id == completion.scene.id
    assert "окраина города Эшфорд" in opening.content
    assert "Посреди ночи в шатре директора появились странные гости" in opening.content
    assert "Начинаем" not in opening.content
