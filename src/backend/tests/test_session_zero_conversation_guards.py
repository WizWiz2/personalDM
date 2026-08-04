from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.campaign import CampaignCreate
from app.services.campaign_service import CampaignService
from app.services.session_zero_interview import SessionZeroInterviewService


SHADOWRUN_WORLD = {
    "setting_name": "Shadowrun",
    "genre": "киберпанк с магией",
    "rules_system": "Shadowrun",
    "world_summary": (
        "Корпорации, уличные раннеры, матрица, магия и метачеловечество."
    ),
}


async def _campaign(db_session: AsyncSession, name: str):
    campaign = await CampaignService(db_session).create_campaign(
        CampaignCreate(name=name)
    )
    await db_session.commit()
    return campaign


def _decision(
    assistant_message: str,
    *,
    question_topics: list[str],
    world: dict | None = None,
    character: dict | None = None,
):
    return {
        "assistant_message": assistant_message,
        "ready_to_finalize": False,
        "question_topics": question_topics,
        "missing_topics": [],
        "draft": {
            "world": world or {},
            "character": character or {},
        },
    }


@pytest.mark.asyncio
async def test_english_session_zero_reply_is_replaced_with_russian_question(
    db_session: AsyncSession,
):
    campaign = await _campaign(db_session, "Shadowrun language guard")
    interview = SessionZeroInterviewService(db_session)
    model_decision = _decision(
        "Great choice! Where do you envision your character at the beginning?",
        question_topics=[
            "world.starting_location_name",
            "world.starting_situation",
        ],
        world=SHADOWRUN_WORLD,
    )

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new_callable=AsyncMock,
        return_value=model_decision,
    ):
        decision = await interview.answer(
            campaign.id,
            "Я хочу сыграть в SHADOWRUN",
        )

    assert "Great choice" not in decision.assistant_message
    assert SessionZeroInterviewService._is_russian_text(
        decision.assistant_message
    )
    state = await interview.get_state(campaign.id)
    assert state.response_language == "ru"
    assert state.draft.world.setting_name == "Shadowrun"
    assert state.draft.world.rules_system == "Shadowrun"


@pytest.mark.asyncio
async def test_write_in_russian_command_bypasses_model_and_changes_topic(
    db_session: AsyncSession,
):
    campaign = await _campaign(db_session, "Russian command")
    interview = SessionZeroInterviewService(db_session)
    first_question = "Расскажи о герое: как его зовут, кто он и как выглядит?"
    first_decision = _decision(
        first_question,
        question_topics=[
            "character.name",
            "character.description",
            "character.appearance",
        ],
        world=SHADOWRUN_WORLD,
    )

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new_callable=AsyncMock,
        return_value=first_decision,
    ):
        await interview.answer(campaign.id, "Хочу Shadowrun")

    blocked_model = AsyncMock(side_effect=AssertionError("LLM must not be called"))
    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new=blocked_model,
    ):
        decision = await interview.answer(
            campaign.id,
            "Пиши по русски всегда",
        )

    blocked_model.assert_not_awaited()
    assert decision.assistant_message.startswith(
        "Да. Дальше говорю только по-русски."
    )
    assert decision.assistant_message != first_question
    assert decision.question_topics != [
        "character.name",
        "character.description",
        "character.appearance",
    ]


@pytest.mark.asyncio
async def test_exact_duplicate_question_is_replaced_after_answer_is_extracted(
    db_session: AsyncSession,
):
    campaign = await _campaign(db_session, "Duplicate question guard")
    interview = SessionZeroInterviewService(db_session)
    repeated_question = (
        "Как вы представляете личность и характер Кабуто? Есть ли особенности "
        "его поведения или манеры говорить?"
    )
    first_decision = _decision(
        repeated_question,
        question_topics=["character.personality"],
        world=SHADOWRUN_WORLD,
        character={
            "name": "Кабуто",
            "description": "Уличный эльф, который пытается выжить.",
            "appearance": "Обезображенное лицо скрыто шлемом-маской.",
        },
    )
    second_decision = _decision(
        repeated_question,
        question_topics=["character.personality"],
        character={
            "personality": (
                "Упорный выживальщик, который старается не потерять человечность."
            )
        },
    )

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new_callable=AsyncMock,
        side_effect=[first_decision, second_decision],
    ):
        first = await interview.answer(
            campaign.id,
            "Кабуто — эльф с обезображенным лицом в шлеме-маске.",
        )
        second = await interview.answer(
            campaign.id,
            "Он просто старается выжить на улицах и не потерять себя.",
        )

    assert first.assistant_message == repeated_question
    assert second.assistant_message != repeated_question
    assert second.question_topics != ["character.personality"]
    state = await interview.get_state(campaign.id)
    assert "не потерять человечность" in state.draft.character.personality
    assert state.draft.world.setting_name == "Shadowrun"


@pytest.mark.asyncio
async def test_no_start_preference_delegates_choice_without_second_llm_call(
    db_session: AsyncSession,
):
    campaign = await _campaign(db_session, "Delegated Shadowrun start")
    interview = SessionZeroInterviewService(db_session)
    start_question = (
        "Где вы видите Кабуто в начале игры? Есть ли конкретное место или ситуация?"
    )
    first_decision = _decision(
        start_question,
        question_topics=[
            "world.starting_location_name",
            "world.starting_situation",
        ],
        world=SHADOWRUN_WORLD,
        character={
            "name": "Кабуто",
            "description": "Уличный эльф-раннер.",
            "appearance": "Всегда носит закрытый шлем-маску.",
        },
    )

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new_callable=AsyncMock,
        return_value=first_decision,
    ):
        first = await interview.answer(campaign.id, "Мой герой — Кабуто")

    assert first.assistant_message == start_question
    blocked_model = AsyncMock(side_effect=AssertionError("LLM must not be called"))
    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new=blocked_model,
    ):
        second = await interview.answer(
            campaign.id,
            "Нет особых предпочтений",
        )

    blocked_model.assert_not_awaited()
    assert "оставим на усмотрение мастера" in second.assistant_message
    state = await interview.get_state(campaign.id)
    assert set(state.delegated_fields) >= {
        "world.starting_location_name",
        "world.starting_situation",
    }
    assert state.draft.world.starting_location_name == (
        "Стартовая точка в Shadowrun"
    )
    assert "первые слова" in state.draft.world.starting_situation
