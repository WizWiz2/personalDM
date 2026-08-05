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
        "Шестой мир: мегакорпорации, раннеры, Матрица, магия и метачеловечество."
    ),
}


FULL_PATCH = {
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


async def _campaign(db_session: AsyncSession, name: str):
    campaign = await CampaignService(db_session).create_campaign(
        CampaignCreate(name=name)
    )
    await db_session.commit()
    return campaign


def _agent_decision(
    assistant_message: str,
    *,
    patch_data: dict | None = None,
    finalize: bool = False,
    question_topics: list[str] | None = None,
):
    tool_calls = []
    if patch_data is not None:
        tool_calls.append(
            {"name": "update_session_zero", "patch": patch_data}
        )
    if finalize:
        tool_calls.append({"name": "finalize_session_zero"})
    return {
        "assistant_message": assistant_message,
        "tool_calls": tool_calls,
        "question_topics": question_topics or [],
    }


@pytest.mark.asyncio
async def test_known_setting_is_understood_without_player_retelling_lore(
    db_session: AsyncSession,
):
    campaign = await _campaign(db_session, "Native Shadowrun")
    interview = SessionZeroInterviewService(db_session)
    reply = (
        "Да, знаю Shadowrun: магия, киберпанк, мегакорпорации и работа в тенях. "
        "Берём узнаваемый канон или тебе важен какой-то особый акцент?"
    )
    model_decision = _agent_decision(
        reply,
        patch_data={
            "world": {
                **SHADOWRUN_WORLD,
                "premise": "История раннера, который берётся за теневые контракты.",
                "tone": "Напряжённое городское приключение.",
                "play_style": "Контракты, переговоры, расследования и последствия.",
            }
        },
    )

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new_callable=AsyncMock,
        return_value=model_decision,
    ):
        decision = await interview.answer(campaign.id, "В Shadowrun")

    assert decision.assistant_message == reply
    assert decision.draft.world.setting_name == "Shadowrun"
    assert "мегакорпорации" in decision.draft.world.world_summary
    assert "Какие черты выбранного мира" not in decision.assistant_message


@pytest.mark.asyncio
async def test_player_uncertainty_lets_agent_choose_and_move_on(
    db_session: AsyncSession,
):
    campaign = await _campaign(db_session, "Agent chooses defaults")
    interview = SessionZeroInterviewService(db_session)
    first = _agent_decision(
        "Берём канонический Shadowrun. Есть ли особый акцент или сразу перейдём к герою?",
        patch_data={"world": {"setting_name": "Shadowrun"}},
    )
    second_reply = (
        "Хорошо, оставляю узнаваемый канон без специальных отклонений. "
        "Расскажи теперь о герое — кто такой Кабуто?"
    )
    second = _agent_decision(
        second_reply,
        patch_data={
            "world": {
                **SHADOWRUN_WORLD,
                "premise": "Камерная история начинающего теневого оперативника.",
                "tone": "Мрачное приключение без постоянной безнадёжности.",
                "play_style": "Контракты, отношения и последствия решений.",
            }
        },
    )

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new_callable=AsyncMock,
        side_effect=[first, second],
    ):
        await interview.answer(campaign.id, "В Shadowrun")
        decision = await interview.answer(campaign.id, "Не могу сказать")

    assert decision.assistant_message == second_reply
    assert "кто такой кабуто" in decision.assistant_message.casefold()
    assert decision.draft.world.world_summary.startswith("Шестой мир")


@pytest.mark.asyncio
async def test_exact_repeat_is_returned_to_agent_for_natural_repair(
    db_session: AsyncSession,
):
    campaign = await _campaign(db_session, "No scripted repeat")
    interview = SessionZeroInterviewService(db_session)
    repeated = "Какие черты выбранного мира особенно важны для этой кампании?"
    first = _agent_decision(
        repeated,
        patch_data={"world": {"setting_name": "Shadowrun"}},
    )
    repeated_again = _agent_decision(repeated)
    repaired_reply = (
        "Понял, не будем отдельно разбирать устройство мира. "
        "Оставляю канонический Shadowrun и перейдём к Кабуто: чем он занимается?"
    )
    repaired = _agent_decision(
        repaired_reply,
        patch_data={"world": SHADOWRUN_WORLD},
    )
    model = AsyncMock(side_effect=[first, repeated_again, repaired])

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new=model,
    ):
        await interview.answer(campaign.id, "В Shadowrun")
        decision = await interview.answer(campaign.id, "Не могу сказать")

    assert model.await_count == 3
    assert decision.assistant_message == repaired_reply
    repair_messages = model.await_args_list[-1].args[2]
    assert "repeated_reply" in repair_messages[-1].content
    assert "Какие черты выбранного мира" not in decision.assistant_message


@pytest.mark.asyncio
async def test_wrong_language_is_repaired_by_agent_not_replaced_by_script(
    db_session: AsyncSession,
):
    campaign = await _campaign(db_session, "Russian agent repair")
    interview = SessionZeroInterviewService(db_session)
    english = _agent_decision(
        "Great, I know Shadowrun. Who is your character?",
        patch_data={"world": SHADOWRUN_WORLD},
    )
    russian_reply = (
        "Да, Shadowrun знаю. Берём канон; теперь расскажи, кто твой герой?"
    )
    russian = _agent_decision(russian_reply)
    model = AsyncMock(side_effect=[english, russian])

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new=model,
    ):
        decision = await interview.answer(campaign.id, "Хочу Shadowrun")

    assert decision.assistant_message == russian_reply
    assert model.await_count == 2
    assert "wrong_language" in model.await_args_list[-1].args[2][-1].content
    assert decision.draft.world.setting_name == "Shadowrun"


@pytest.mark.asyncio
async def test_incomplete_finalize_returns_tool_feedback_to_agent(
    db_session: AsyncSession,
):
    campaign = await _campaign(db_session, "Finalize feedback")
    interview = SessionZeroInterviewService(db_session)
    premature = _agent_decision(
        "Кажется, можно начинать.",
        patch_data={"world": {"setting_name": "Shadowrun"}},
        finalize=True,
    )
    natural_followup = _agent_decision(
        "С миром определились. Теперь расскажи о герое так, как удобно: кто он и чего хочет?"
    )
    model = AsyncMock(side_effect=[premature, natural_followup])

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new=model,
    ):
        decision = await interview.answer(campaign.id, "В Shadowrun")

    assert decision.ready_to_finalize is False
    assert decision.assistant_message == natural_followup["assistant_message"]
    feedback = model.await_args_list[-1].args[2][-1].content
    assert "finalize_session_zero" in feedback
    assert "missing_fields" in feedback
    assert "Сам реши, как естественно продолжить" in feedback


@pytest.mark.asyncio
async def test_agent_can_update_full_card_and_request_finalize(
    db_session: AsyncSession,
):
    campaign = await _campaign(db_session, "Complete native session")
    interview = SessionZeroInterviewService(db_session)
    response = _agent_decision(
        "Отлично, основа сложилась. Проверь итоговую сводку перед стартом.",
        patch_data=FULL_PATCH,
        finalize=True,
    )

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new_callable=AsyncMock,
        return_value=response,
    ):
        decision = await interview.answer(
            campaign.id,
            "Да, дополнительных границ нет. Старт и остальные детали выбери сам.",
        )

    assert decision.ready_to_finalize is True
    assert decision.missing_topics == []
    assert decision.draft.character.name == "Кабуто"
    assert decision.draft.world.starting_location_name == "Подпольная клиника Редмонда"


@pytest.mark.asyncio
async def test_confirmed_scalar_fact_is_not_silently_rewritten(
    db_session: AsyncSession,
):
    campaign = await _campaign(db_session, "Stable agent tools")
    interview = SessionZeroInterviewService(db_session)
    first = _agent_decision(
        "Shadowrun принят. Кто такой Кабуто?",
        patch_data={
            "world": SHADOWRUN_WORLD,
            "character": {"name": "Кабуто"},
        },
    )
    second = _agent_decision(
        "Расскажи о его первой цели.",
        patch_data={
            "world": {"setting_name": "Cyberpunk 2077"},
            "character": {
                "name": "Другой герой",
                "description": "Уличный эльф-раннер.",
            },
        },
    )

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new_callable=AsyncMock,
        side_effect=[first, second],
    ):
        await interview.answer(campaign.id, "В Shadowrun, героя зовут Кабуто")
        decision = await interview.answer(campaign.id, "Он уличный эльф-раннер")

    assert decision.draft.world.setting_name == "Shadowrun"
    assert decision.draft.character.name == "Кабуто"
    assert decision.draft.character.description == "Уличный эльф-раннер."


@pytest.mark.asyncio
async def test_agent_request_uses_compact_draft_and_conversation_history(
    db_session: AsyncSession,
):
    campaign = await _campaign(db_session, "Native agent request")
    interview = SessionZeroInterviewService(db_session)
    state = await interview.get_state(campaign.id)
    state.messages = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"m{index}"}
        for index in range(20)
    ]
    state.messages.append({"role": "user", "content": "Последний ответ"})
    state.pending_user_message = "Последний ответ"
    await interview._save_state(campaign.id, state, commit=True)
    response = _agent_decision(
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
    assert "ЭТО НЕ СПИСОК ВОПРОСОВ" in prompt
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


def test_full_tool_patch_remains_finalize_ready():
    draft = SessionZeroInterviewService._apply_patch(
        SessionZeroInterviewDraft(),
        SessionZeroInterviewPatch.model_validate(FULL_PATCH),
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
