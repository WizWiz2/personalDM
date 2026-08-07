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
    "premise": "Уличные раннеры выживают между мегакорпорациями, магией и криминалом.",
    "tone": "неоновый криминальный триллер",
    "world_summary": "Канонический Шестой мир Shadowrun.",
    "play_style": "миссии, расследования и последствия решений",
    "starting_location_name": "Сиэтл",
    "starting_situation": "Новый раннер ищет первый серьёзный контракт.",
}


async def _campaign(db_session: AsyncSession, name: str):
    campaign = await CampaignService(db_session).create_campaign(
        CampaignCreate(name=name)
    )
    await db_session.commit()
    return campaign


def _decision(
    message: str,
    *,
    patch_data: dict | None = None,
    question_topics: list[str] | None = None,
    finalize: bool = False,
):
    calls = []
    if patch_data is not None:
        calls.append({"name": "update_session_zero", "patch": patch_data})
    if finalize:
        calls.append({"name": "finalize_session_zero"})
    return {
        "assistant_message": message,
        "tool_calls": calls,
        "question_topics": question_topics or [],
    }


@pytest.mark.asyncio
async def test_known_setting_can_fill_world_without_lore_interrogation(
    db_session: AsyncSession,
):
    campaign = await _campaign(db_session, "Known setting")
    interview = SessionZeroInterviewService(db_session)
    response = _decision(
        "Да, Shadowrun знаю: магия, киберпанк и мегакорпорации. Каким будет твой герой?",
        patch_data={"world": SHADOWRUN_WORLD},
        question_topics=["character.description"],
    )
    model = AsyncMock(return_value=response)

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new=model,
    ):
        decision = await interview.answer(campaign.id, "В Shadowrun")

    assert decision.draft.world.setting_name == "Shadowrun"
    assert decision.draft.world.world_summary == "Канонический Шестой мир Shadowrun."
    assert decision.question_topics == ["character.description"]
    assert "Какие черты" not in decision.assistant_message


@pytest.mark.asyncio
async def test_player_refusal_to_explain_setting_moves_to_character(
    db_session: AsyncSession,
):
    campaign = await _campaign(db_session, "No lore lecture")
    interview = SessionZeroInterviewService(db_session)
    first = _decision(
        "Shadowrun понял. Есть ли особый акцент кампании, или берём узнаваемый канон?",
        patch_data={"world": {"setting_name": "Shadowrun"}},
        question_topics=["world.premise"],
    )
    second = _decision(
        "Берём узнаваемый канон. Расскажи, кем будет твой герой.",
        patch_data={"world": SHADOWRUN_WORLD},
        question_topics=["character.description"],
    )
    model = AsyncMock(side_effect=[first, second])

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new=model,
    ):
        await interview.answer(campaign.id, "Shadowrun")
        decision = await interview.answer(
            campaign.id,
            "Я написал. Ты знаешь что такое Shadowrun?",
        )

    assert decision.draft.world.world_summary
    assert decision.question_topics == ["character.description"]
    assert "Какие черты" not in decision.assistant_message


@pytest.mark.asyncio
async def test_no_preference_does_not_loop_same_world_question(
    db_session: AsyncSession,
):
    campaign = await _campaign(db_session, "No preference loop")
    interview = SessionZeroInterviewService(db_session)
    first = _decision(
        "Есть ли особый акцент Shadowrun, или берём канон?",
        patch_data={"world": {"setting_name": "Shadowrun"}},
        question_topics=["world.premise"],
    )
    second = _decision(
        "Тогда беру классический уличный Shadowrun. Как зовут героя?",
        patch_data={"world": SHADOWRUN_WORLD},
        question_topics=["character.name"],
    )
    model = AsyncMock(side_effect=[first, second])

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new=model,
    ):
        await interview.answer(campaign.id, "Shadowrun")
        decision = await interview.answer(campaign.id, "не могу сказать")

    assert decision.draft.world.premise == SHADOWRUN_WORLD["premise"]
    assert decision.question_topics == ["character.name"]
    assert "особый акцент" not in decision.assistant_message.casefold()


@pytest.mark.asyncio
async def test_agent_repair_handles_exact_repeat_without_scripted_fallback(
    db_session: AsyncSession,
):
    campaign = await _campaign(db_session, "Repeat repair")
    interview = SessionZeroInterviewService(db_session)
    repeated = _decision(
        "Как зовут героя?",
        patch_data={"world": SHADOWRUN_WORLD},
        question_topics=["character.name"],
    )
    repaired = _decision(
        "Мир уже понятен. Расскажи одним предложением, кто твой герой и чем он занимается?",
        patch_data={"world": SHADOWRUN_WORLD},
        question_topics=["character.description"],
    )
    model = AsyncMock(side_effect=[repeated, repaired])

    state = await interview.get_state(campaign.id)
    state.messages.append({"role": "assistant", "content": "Как зовут героя?"})
    state.pending_user_message = "Shadowrun"
    await interview._save_state(campaign.id, state, commit=True)

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new=model,
    ):
        decision = await interview.retry_pending(campaign.id)

    assert model.await_count == 2
    assert decision.assistant_message == repaired["assistant_message"]
    assert decision.question_topics == ["character.description"]
    repair_messages = model.await_args_list[-1].args[2]
    assert "повтор" in repair_messages[-1].content.casefold()


@pytest.mark.asyncio
async def test_agent_repair_handles_english_without_code_picking_question(
    db_session: AsyncSession,
):
    campaign = await _campaign(db_session, "Language repair")
    interview = SessionZeroInterviewService(db_session)
    english = _decision(
        "Great. Tell me about your character.",
        patch_data={"world": SHADOWRUN_WORLD},
        question_topics=["character.description"],
    )
    repaired = _decision(
        "Отлично. Расскажи немного о герое — кто он в тенях Сиэтла?",
        question_topics=["character.description"],
    )
    model = AsyncMock(side_effect=[english, repaired])

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new=model,
    ):
        decision = await interview.answer(campaign.id, "Shadowrun")

    assert model.await_count == 2
    assert decision.assistant_message == repaired["assistant_message"]
    assert decision.question_topics == ["character.description"]


@pytest.mark.asyncio
async def test_incomplete_finalize_returns_tool_feedback_to_agent(
    db_session: AsyncSession,
):
    campaign = await _campaign(db_session, "Finalize feedback")
    interview = SessionZeroInterviewService(db_session)
    incomplete = _decision(
        "Основа готова, можно начинать.",
        patch_data={"world": {"setting_name": "Shadowrun"}},
        finalize=True,
    )
    repaired = _decision(
        "Перед стартом уточню только героя: как его зовут и чем он занимается?",
        patch_data={"world": SHADOWRUN_WORLD},
        question_topics=["character.name", "character.description"],
    )
    model = AsyncMock(side_effect=[incomplete, repaired])

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new=model,
    ):
        decision = await interview.answer(campaign.id, "В Shadowrun")

    assert model.await_count == 2
    assert decision.ready_to_finalize is False
    feedback_message = model.await_args_list[-1].args[2][-1].content
    assert "finalize_session_zero" in feedback_message
    assert "character.name" in feedback_message
    assert decision.assistant_message == repaired["assistant_message"]


@pytest.mark.asyncio
async def test_confirmation_can_complete_detailed_card(
    db_session: AsyncSession,
):
    campaign = await _campaign(db_session, "Complete card")
    interview = SessionZeroInterviewService(db_session)
    draft = {
        "world": {
            **SHADOWRUN_WORLD,
            "boundaries": [],
            "boundaries_confirmed": True,
        },
        "character": {
            "name": "Кабуто",
            "description": "Молодой уличный эльф-раннер, совмещающий хакинг и магию.",
            "appearance": "Обожжённое лицо скрыто шлемом-маской; свободная одежда не мешает движениям.",
            "personality": "Закрытый, одинокий и скрытный.",
            "values": ["свобода", "верность немногим близким"],
            "fears": ["потерять дорогих людей"],
            "desires": ["вырваться с улиц"],
            "voice": "Тихий и сдержанный.",
            "speech_patterns": "Немногословен; чаще общается с духами и программами.",
            "biography": "Вырос на улицах; после пробуждения магии недруги облили его лицо кислотой.",
            "capabilities": ["хакинг", "колдовство", "паркур"],
            "limitations": ["скрывает обезображенное лицо", "с трудом доверяет людям"],
            "first_goal": "Найти кибердеку получше.",
        },
    }
    response = _decision(
        "Отлично. Карточка собрана; если всё верно, можем переходить к первой сцене.",
        patch_data=draft,
        finalize=True,
    )
    model = AsyncMock(return_value=response)

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new=model,
    ):
        decision = await interview.answer(
            campaign.id,
            "Да, всё так. Начинаем.",
        )

    assert decision.ready_to_finalize is True
    assert decision.missing_topics == []
    assert decision.draft.character.name == "Кабуто"
    assert "хакинг" in decision.draft.character.capabilities
    assert decision.draft.world.boundaries_confirmed is True


@pytest.mark.asyncio
async def test_confirmed_fields_are_not_overwritten_without_correction(
    db_session: AsyncSession,
):
    campaign = await _campaign(db_session, "Protected facts")
    interview = SessionZeroInterviewService(db_session)
    first = _decision(
        "Shadowrun принят. Как зовут героя?",
        patch_data={"world": SHADOWRUN_WORLD},
        question_topics=["character.name"],
    )
    second = _decision(
        "Кабуто. Расскажи коротко, кто он.",
        patch_data={
            "world": {"setting_name": "Cyberpunk RED"},
            "character": {"name": "Кабуто"},
        },
        question_topics=["character.description"],
    )
    model = AsyncMock(side_effect=[first, second])

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new=model,
    ):
        await interview.answer(campaign.id, "Shadowrun")
        decision = await interview.answer(campaign.id, "Кабуто")

    assert decision.draft.world.setting_name == "Shadowrun"
    assert decision.draft.character.name == "Кабуто"


@pytest.mark.asyncio
async def test_agent_request_uses_compact_draft_and_conversation_history(
    db_session: AsyncSession,
):
    campaign = await _campaign(db_session, "Prompt budget")
    interview = SessionZeroInterviewService(db_session)
    state = await interview.get_state(campaign.id)
    state.draft.world.setting_name = "Shadowrun"
    state.messages = [
        {"role": "assistant" if i % 2 == 0 else "user", "content": f"m{i}"}
        for i in range(20)
    ]
    state.pending_user_message = "Последний ответ"
    await interview._save_state(campaign.id, state, commit=True)

    response = _decision(
        "Понял. Расскажи немного о герое.",
        patch_data={"world": SHADOWRUN_WORLD},
    )
    model = AsyncMock(return_value=response)

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new=model,
    ):
        await interview.retry_pending(campaign.id)

    kwargs = model.await_args.kwargs
    messages = model.await_args.args[2]
    assert kwargs["max_tokens"] == 1200
    assert kwargs["response_model"].__name__ == "SessionZeroInterviewModelDecision"
    assert len(messages) == SessionZeroInterviewService.MAX_HISTORY_MESSAGES + 1
    assert [item.content for item in messages[1:]] == [
        "m9",
        "m10",
        "m11",
        "m12",
        "m13",
        "m14",
        "m15",
        "m16",
        "m17",
        "m18",
        "m19",
        "Последний ответ",
    ]
    prompt = messages[0].content
    assert "Это разговор, а не анкета" in prompt
    assert "Не проси игрока пересказывать базовый канон" in prompt
    assert "update_session_zero" in prompt
    assert "finalize_session_zero" in prompt
    assert "ТЕХНИЧЕСКИЙ МИНИМУМ" in prompt
    assert "Это не список вопросов игроку" in prompt
    assert '\n  "world"' not in prompt


def test_tool_patch_accumulates_complete_card_without_losing_earlier_fields():
    draft = SessionZeroInterviewDraft()
    draft = SessionZeroInterviewService._apply_patch(
        draft,
        SessionZeroInterviewPatch.model_validate({"world": SHADOWRUN_WORLD}),
    )
    draft = SessionZeroInterviewService._apply_patch(
        draft,
        SessionZeroInterviewPatch.model_validate(
            {
                "character": {
                    "name": "Кабуто",
                    "description": "Уличный эльф-раннер и хакер.",
                    "appearance": "Обожжённое лицо скрыто шлемом-маской.",
                }
            }
        ),
    )

    assert draft.world.setting_name == "Shadowrun"
    assert draft.character.name == "Кабуто"
    assert "Обожжённое лицо" in draft.character.appearance
