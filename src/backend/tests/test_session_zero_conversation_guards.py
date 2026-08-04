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
        "Корпорации, уличные раннеры, Матрица, магия и метачеловечество."
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
async def test_english_reply_is_replaced_without_losing_declared_topic(
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
    assert decision.question_topics == [
        "world.starting_location_name",
        "world.starting_situation",
    ]
    assert "начать кампанию" in decision.assistant_message
    state = await interview.get_state(campaign.id)
    assert state.response_language == "ru"
    assert state.draft.world.setting_name == "Shadowrun"
    assert state.draft.world.rules_system == "Shadowrun"


@pytest.mark.asyncio
async def test_write_in_russian_command_keeps_current_topic(
    db_session: AsyncSession,
):
    campaign = await _campaign(db_session, "Russian command")
    interview = SessionZeroInterviewService(db_session)
    start_topics = [
        "world.starting_location_name",
        "world.starting_situation",
    ]
    first_decision = _decision(
        "С какой конкретной ситуации начать кампанию?",
        question_topics=start_topics,
        world=SHADOWRUN_WORLD,
    )

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new_callable=AsyncMock,
        return_value=first_decision,
    ):
        first = await interview.answer(campaign.id, "Хочу Shadowrun")

    blocked_model = AsyncMock(side_effect=AssertionError("LLM must not be called"))
    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new=blocked_model,
    ):
        second = await interview.answer(
            campaign.id,
            "Пиши по русски всегда",
        )

    blocked_model.assert_not_awaited()
    assert second.assistant_message.startswith(
        "Да. Дальше говорю только по-русски."
    )
    assert second.question_topics == first.question_topics == start_topics
    assert "начать кампанию" in second.assistant_message


@pytest.mark.asyncio
async def test_duplicate_personality_question_moves_to_narrow_missing_field(
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
    assert second.question_topics == ["character.values"]
    assert "принципы" in second.assistant_message
    assert "характер" not in second.assistant_message.casefold()
    state = await interview.get_state(campaign.id)
    assert "не потерять человечность" in state.draft.character.personality
    assert state.draft.world.setting_name == "Shadowrun"


@pytest.mark.asyncio
async def test_explicit_start_delegation_is_materialized_by_model(
    db_session: AsyncSession,
):
    campaign = await _campaign(db_session, "Delegated Shadowrun start")
    interview = SessionZeroInterviewService(db_session)
    start_topics = [
        "world.starting_location_name",
        "world.starting_situation",
    ]
    first_decision = _decision(
        "Где вы видите Кабуто в начале игры? Есть ли конкретное место или ситуация?",
        question_topics=start_topics,
        world=SHADOWRUN_WORLD,
        character={
            "name": "Кабуто",
            "description": "Уличный эльф-раннер.",
            "appearance": "Всегда носит закрытый шлем-маску.",
        },
    )
    delegated_decision = _decision(
        "Начнём с первого дела Кабуто. Какие у него принципы?",
        question_topics=["character.values"],
        world={
            "starting_location_name": "Ночной рынок Редмонда",
            "starting_situation": (
                "Кабуто получает дешёвый контракт на взлом терминала местной банды."
            ),
        },
    )

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new_callable=AsyncMock,
        return_value=first_decision,
    ):
        await interview.answer(campaign.id, "Мой герой — Кабуто")

    model = AsyncMock(return_value=delegated_decision)
    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new=model,
    ):
        await interview.answer(campaign.id, "Нет особых предпочтений")

    model.assert_awaited_once()
    state = await interview.get_state(campaign.id)
    assert set(state.delegated_fields) >= set(start_topics)
    assert state.draft.world.starting_location_name == "Ночной рынок Редмонда"
    assert "контракт" in state.draft.world.starting_situation
    assert not state.draft.world.starting_location_name.startswith("Стартовая точка")
    assert "на усмотрение мастера" not in state.draft.world.starting_situation


@pytest.mark.asyncio
async def test_bare_no_is_not_automatic_delegation(
    db_session: AsyncSession,
):
    campaign = await _campaign(db_session, "Bare no")
    interview = SessionZeroInterviewService(db_session)
    first_decision = _decision(
        "Хочется мрачного и безнадёжного тона?",
        question_topics=["world.tone"],
        world=SHADOWRUN_WORLD,
    )
    second_decision = _decision(
        "Понял. Что должно быть в центре игры?",
        question_topics=["world.play_style"],
        world={"tone": "Приключенческий тон без постоянной безнадёжности."},
    )

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new_callable=AsyncMock,
        return_value=first_decision,
    ):
        await interview.answer(campaign.id, "Хочу Shadowrun")

    model = AsyncMock(return_value=second_decision)
    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new=model,
    ):
        await interview.answer(campaign.id, "Нет")

    model.assert_awaited_once()
    state = await interview.get_state(campaign.id)
    assert state.delegated_fields == []
    assert state.draft.world.tone.startswith("Приключенческий")


@pytest.mark.asyncio
async def test_model_cannot_rewrite_unrelated_confirmed_field(
    db_session: AsyncSession,
):
    campaign = await _campaign(db_session, "Stable draft merge")
    interview = SessionZeroInterviewService(db_session)
    first_decision = _decision(
        "Какой у Кабуто характер?",
        question_topics=["character.personality"],
        world=SHADOWRUN_WORLD,
        character={
            "name": "Кабуто",
            "description": "Уличный эльф-раннер.",
            "appearance": "Обезображенное лицо скрыто шлемом-маской.",
        },
    )
    second_decision = _decision(
        "Какие у Кабуто принципы?",
        question_topics=["character.values"],
        world={"setting_name": "Cyberpunk 2077"},
        character={
            "personality": "Старается выжить и не потерять себя.",
        },
    )

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new_callable=AsyncMock,
        side_effect=[first_decision, second_decision],
    ):
        await interview.answer(campaign.id, "Хочу Shadowrun, герой Кабуто")
        await interview.answer(
            campaign.id,
            "Он старается выжить на улицах и не потерять себя.",
        )

    state = await interview.get_state(campaign.id)
    assert state.draft.world.setting_name == "Shadowrun"
    assert state.draft.character.personality.startswith("Старается выжить")


@pytest.mark.asyncio
async def test_full_kabuto_regression_keeps_language_topic_and_concrete_start(
    db_session: AsyncSession,
):
    campaign = await _campaign(db_session, "Kabuto live transcript")
    interview = SessionZeroInterviewService(db_session)
    start_topics = [
        "world.starting_location_name",
        "world.starting_situation",
    ]
    model_answers = [
        _decision(
            "Great choice! Where do you envision your character at the beginning?",
            question_topics=start_topics,
            world=SHADOWRUN_WORLD,
        ),
        _decision(
            "Есть ли всё-таки пожелания к первой сцене?",
            question_topics=start_topics,
        ),
        _decision(
            "Я выберу старт. Теперь расскажи о герое.",
            question_topics=[
                "character.name",
                "character.description",
                "character.appearance",
            ],
            world={
                "starting_location_name": "Подпольная клиника в Редмонде",
                "starting_situation": (
                    "Кабуто приходит в себя после неудачного дела и получает шанс "
                    "расплатиться за лечение новым взломом."
                ),
            },
        ),
        _decision(
            "Какой у Кабуто характер?",
            question_topics=["character.personality"],
            character={
                "name": "Кабуто",
                "description": "Уличный эльф, пытающийся выбраться с низов.",
                "appearance": "Обезображенное лицо всегда скрыто шлемом-маской.",
            },
        ),
        _decision(
            "Какой у Кабуто характер?",
            question_topics=["character.personality"],
            character={
                "personality": "Старается выжить на улицах и не потерять себя."
            },
        ),
    ]

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new_callable=AsyncMock,
        side_effect=model_answers,
    ) as model:
        first = await interview.answer(campaign.id, "Я хочу сыграть в SHADOWRUN")
        language = await interview.answer(campaign.id, "Пиши по русски всегда")
        hesitant = await interview.answer(campaign.id, "Не очень")
        delegated = await interview.answer(campaign.id, "Нет особых предпочтений")
        hero = await interview.answer(
            campaign.id,
            "Кабуто. Он эльф, но его лицо обезображено, поэтому он всегда в шлеме-маске.",
        )
        personality = await interview.answer(
            campaign.id,
            "Он просто старается выжить на улицах и не потерять себя.",
        )

    assert model.await_count == 5
    assert first.question_topics == language.question_topics == start_topics
    assert hesitant.question_topics == start_topics
    assert delegated.question_topics == ["character.name"]
    assert hero.question_topics == ["character.personality"]
    assert personality.question_topics == ["character.values"]
    assert "характер" not in personality.assistant_message.casefold()

    state = await interview.get_state(campaign.id)
    assert state.response_language == "ru"
    assert state.draft.world.setting_name == "Shadowrun"
    assert state.draft.world.starting_location_name == "Подпольная клиника в Редмонде"
    assert "Стартовая точка" not in state.draft.world.starting_location_name
    assert state.draft.character.name == "Кабуто"
    assert "обезображенное" in state.draft.character.appearance.casefold()
    assert "не потерять себя" in state.draft.character.personality
