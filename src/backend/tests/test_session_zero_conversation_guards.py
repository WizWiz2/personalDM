from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.campaign import CampaignCreate
from app.models.session_zero_interview import (
    SessionZeroInterviewDraft,
    SessionZeroInterviewPatch,
)
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


def _model_decision(
    assistant_message: str,
    *,
    question_topics: list[str],
    world: dict | None = None,
    character: dict | None = None,
    ready_to_finalize: bool = False,
):
    return {
        "assistant_message": assistant_message,
        "ready_to_finalize": ready_to_finalize,
        "question_topics": question_topics,
        "patch": {
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
    model_decision = _model_decision(
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
    assert decision.draft.world.setting_name == "Shadowrun"
    state = await interview.get_state(campaign.id)
    assert state.response_language == "ru"
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
    first_decision = _model_decision(
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
        second = await interview.answer(campaign.id, "Пиши по русски всегда")

    blocked_model.assert_not_awaited()
    assert second.assistant_message.startswith(
        "Да. Дальше говорю только по-русски."
    )
    assert second.question_topics == first.question_topics == start_topics
    assert second.draft.world.setting_name == "Shadowrun"


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
    first_decision = _model_decision(
        repeated_question,
        question_topics=["character.personality"],
        world=SHADOWRUN_WORLD,
        character={
            "name": "Кабуто",
            "description": "Уличный эльф, который пытается выжить.",
            "appearance": "Обезображенное лицо скрыто шлемом-маской.",
        },
    )
    second_decision = _model_decision(
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
        await interview.answer(
            campaign.id,
            "Кабуто — эльф с обезображенным лицом в шлеме-маске.",
        )
        second = await interview.answer(
            campaign.id,
            "Он просто старается выжить на улицах и не потерять себя.",
        )

    assert second.question_topics == ["character.values"]
    assert "принципы" in second.assistant_message
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
    first_decision = _model_decision(
        "Где вы видите Кабуто в начале игры? Есть ли конкретное место или ситуация?",
        question_topics=start_topics,
        world=SHADOWRUN_WORLD,
        character={
            "name": "Кабуто",
            "description": "Уличный эльф-раннер.",
            "appearance": "Всегда носит закрытый шлем-маску.",
        },
    )
    delegated_decision = _model_decision(
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
        decision = await interview.answer(campaign.id, "Нет особых предпочтений")

    model.assert_awaited_once()
    state = await interview.get_state(campaign.id)
    assert set(state.delegated_fields) >= set(start_topics)
    assert decision.draft.world.setting_name == "Shadowrun"
    assert state.draft.world.starting_location_name == "Ночной рынок Редмонда"
    assert "контракт" in state.draft.world.starting_situation


@pytest.mark.asyncio
async def test_bare_no_is_not_automatic_delegation(
    db_session: AsyncSession,
):
    campaign = await _campaign(db_session, "Bare no")
    interview = SessionZeroInterviewService(db_session)
    first_decision = _model_decision(
        "Хочется мрачного и безнадёжного тона?",
        question_topics=["world.tone"],
        world=SHADOWRUN_WORLD,
    )
    second_decision = _model_decision(
        "Понял. Что должно быть в центре игры?",
        question_topics=["world.play_style"],
        world={"tone": "Приключенческий тон без постоянной безнадёжности."},
    )

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new_callable=AsyncMock,
        side_effect=[first_decision, second_decision],
    ):
        await interview.answer(campaign.id, "Хочу Shadowrun")
        await interview.answer(campaign.id, "Нет")

    state = await interview.get_state(campaign.id)
    assert state.delegated_fields == []
    assert state.draft.world.tone.startswith("Приключенческий")


@pytest.mark.asyncio
async def test_patch_cannot_rewrite_unrelated_confirmed_field(
    db_session: AsyncSession,
):
    campaign = await _campaign(db_session, "Stable patch merge")
    interview = SessionZeroInterviewService(db_session)
    first_decision = _model_decision(
        "Какой у Кабуто характер?",
        question_topics=["character.personality"],
        world=SHADOWRUN_WORLD,
        character={
            "name": "Кабуто",
            "description": "Уличный эльф-раннер.",
            "appearance": "Обезображенное лицо скрыто шлемом-маской.",
        },
    )
    second_decision = _model_decision(
        "Какие у Кабуто принципы?",
        question_topics=["character.values"],
        world={"setting_name": "Cyberpunk 2077"},
        character={"personality": "Старается выжить и не потерять себя."},
    )

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new_callable=AsyncMock,
        side_effect=[first_decision, second_decision],
    ):
        await interview.answer(campaign.id, "Хочу Shadowrun, герой Кабуто")
        second = await interview.answer(
            campaign.id,
            "Он старается выжить на улицах и не потерять себя.",
        )

    assert second.draft.world.setting_name == "Shadowrun"
    assert second.draft.character.name == "Кабуто"
    assert second.draft.character.personality.startswith("Старается выжить")


@pytest.mark.asyncio
async def test_model_request_uses_compact_patch_budget_and_short_history(
    db_session: AsyncSession,
):
    campaign = await _campaign(db_session, "Compact patch request")
    interview = SessionZeroInterviewService(db_session)
    response = _model_decision(
        "Как зовут героя?",
        question_topics=["character.name"],
        world=SHADOWRUN_WORLD,
    )
    model = AsyncMock(return_value=response)

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new=model,
    ):
        await interview.answer(campaign.id, "Хочу Shadowrun")

    kwargs = model.await_args.kwargs
    assert kwargs["max_tokens"] == 1000
    assert kwargs["response_model"].__name__ == "SessionZeroInterviewModelDecision"
    messages = model.await_args.args[2]
    assert len(messages) <= SessionZeroInterviewService.MAX_HISTORY_MESSAGES + 1
    assert "CURRENT DRAFT — ТОЛЬКО ДЛЯ ЧТЕНИЯ" in messages[0].content
    assert "Не копируй CURRENT DRAFT" in messages[0].content


def test_patch_accumulates_complete_card_without_losing_earlier_fields():
    draft = SessionZeroInterviewDraft()
    world_patch = SessionZeroInterviewPatch.model_validate(
        {"world": SHADOWRUN_WORLD}
    )
    draft = SessionZeroInterviewService._apply_patch(draft, world_patch)
    hero_patch = SessionZeroInterviewPatch.model_validate(
        {
            "character": {
                "name": "Кабуто",
                "description": "Уличный эльф-раннер и хакер.",
                "appearance": (
                    "Обожжённое лицо скрыто шлемом-маской; одежда не сковывает движения."
                ),
            }
        }
    )
    draft = SessionZeroInterviewService._apply_patch(draft, hero_patch)

    assert draft.world.setting_name == "Shadowrun"
    assert draft.world.world_summary.startswith("Корпорации")
    assert draft.character.name == "Кабуто"
    assert "Обожжённое лицо" in draft.character.appearance


def test_full_patch_remains_finalize_ready():
    patch = SessionZeroInterviewPatch.model_validate(
        {
            "world": {
                **SHADOWRUN_WORLD,
                "premise": "Кабуто берётся за опасные теневые контракты.",
                "tone": "Мрачное приключение с редкими передышками.",
                "play_style": "Задания, расследования и последствия решений.",
                "starting_location_name": "Подпольная клиника Редмонда",
                "starting_situation": "Кабуто приходит в себя после провального дела.",
                "boundaries_confirmed": True,
            },
            "character": {
                "name": "Кабуто",
                "description": "Уличный эльф-раннер и хакер.",
                "appearance": "Обожжённое лицо скрыто шлемом-маской.",
                "personality": "Практичный и осторожный выживальщик.",
                "values": ["Не предавать тех, кто ему доверился"],
                "fears": ["Снова оказаться беспомощным"],
                "desires": ["Выбраться из нищеты"],
                "voice": "Низкий и спокойный голос.",
                "speech_patterns": "Говорит коротко и по делу.",
                "biography": "Вырос на улицах Редмонда.",
                "capabilities": ["Взлом", "Скрытность"],
                "limitations": ["Тяжёлые ожоги", "Недоверчивость"],
                "first_goal": "Расплатиться за лечение.",
            },
        }
    )
    draft = SessionZeroInterviewService._apply_patch(
        SessionZeroInterviewDraft(),
        patch,
    )

    assert SessionZeroInterviewService.missing_fields(draft) == []
    assert draft.character.appearance == "Обожжённое лицо скрыто шлемом-маской."
    assert draft.world.starting_location_name == "Подпольная клиника Редмонда"


def test_rate_limit_delay_is_parsed_and_capped():
    assert SessionZeroInterviewService._rate_limit_retry_seconds(
        "LLM returned HTTP 429: Please try again in 2.985s"
    ) == pytest.approx(2.985)
    assert SessionZeroInterviewService._rate_limit_retry_seconds(
        "rate_limit_exceeded; Please try again in 99s"
    ) == SessionZeroInterviewService.RATE_LIMIT_RETRY_CAP_SECONDS
    assert SessionZeroInterviewService._rate_limit_retry_seconds("HTTP 500") is None
