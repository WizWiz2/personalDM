from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.campaign import CampaignCreate
from app.models.session_zero_interview import SessionZeroInterviewModelDecision
from app.services.campaign_service import CampaignService
from app.services.session_zero_interview import SessionZeroInterviewService


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
):
    calls = []
    if patch_data is not None:
        calls.append({"name": "update_session_zero", "patch": patch_data})
    return {
        "assistant_message": message,
        "tool_calls": calls,
        "question_topics": question_topics or [],
    }


def test_whitespace_agent_message_is_normalized_before_validation():
    decision = SessionZeroInterviewModelDecision.model_validate(
        {
            "assistant_message": "   \n\t ",
            "tool_calls": [],
        }
    )

    assert decision.assistant_message.strip()
    assert decision.assistant_message != "   \n\t "


@pytest.mark.asyncio
async def test_player_answer_is_recorded_before_agent_moves_to_next_topic(
    db_session: AsyncSession,
):
    campaign = await _campaign(db_session, "Record previous answer")
    interview = SessionZeroInterviewService(db_session)
    first = _decision(
        "Чего Кабуто хочет добиться в самом начале кампании?",
        patch_data={
            "world": {
                "setting_name": "Shadowrun",
                "genre": "киберпанк с магией",
                "world_summary": "Шестой мир Shadowrun.",
            },
            "character": {"name": "Кабуто"},
        },
        question_topics=["character.first_goal"],
    )
    forgot_answer = _decision(
        "Что Кабуто уже умеет делать хорошо?",
        question_topics=["character.capabilities"],
    )
    repaired = _decision(
        "Принято: сначала ему нужна кибердека получше. В чём он особенно силён?",
        patch_data={
            "character": {"first_goal": "Найти кибердеку получше."}
        },
        question_topics=["character.capabilities"],
    )
    model = AsyncMock(side_effect=[first, forgot_answer, repaired])

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new=model,
    ):
        await interview.answer(campaign.id, "Героя зовут Кабуто")
        decision = await interview.answer(campaign.id, "Найти кибердеку получше")

    assert model.await_count == 3
    assert decision.draft.character.first_goal == "Найти кибердеку получше."
    assert decision.question_topics == ["character.capabilities"]
    feedback = model.await_args_list[-1].args[2][-1].content
    assert "unrecorded_player_answer" in feedback
    assert "Найти кибердеку получше" in feedback


@pytest.mark.asyncio
async def test_rephrased_question_for_filled_topic_is_rejected(
    db_session: AsyncSession,
):
    campaign = await _campaign(db_session, "No semantic goal repeat")
    interview = SessionZeroInterviewService(db_session)
    first = _decision(
        "Чего Кабуто хочет добиться в самом начале кампании?",
        patch_data={
            "world": {"setting_name": "Shadowrun"},
            "character": {"name": "Кабуто"},
        },
        question_topics=["character.first_goal"],
    )
    repeated = _decision(
        "Какова первая цель Кабуто?",
        patch_data={
            "character": {"first_goal": "Найти кибердеку получше."}
        },
        question_topics=["character.first_goal"],
    )
    repaired = _decision(
        "С кибердекой понятно. Что он уже умеет делать хорошо?",
        question_topics=["character.capabilities"],
    )
    model = AsyncMock(side_effect=[first, repeated, repaired])

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new=model,
    ):
        await interview.answer(campaign.id, "Героя зовут Кабуто")
        decision = await interview.answer(campaign.id, "Найти кибердеку получше")

    assert decision.assistant_message == repaired["assistant_message"]
    assert decision.draft.character.first_goal == "Найти кибердеку получше."
    feedback = model.await_args_list[-1].args[2][-1].content
    assert "question_already_answered" in feedback


@pytest.mark.asyncio
async def test_third_narrow_character_question_is_repaired_into_broader_progress(
    db_session: AsyncSession,
):
    campaign = await _campaign(db_session, "Break questionnaire streak")
    interview = SessionZeroInterviewService(db_session)
    ask_values = _decision(
        "Что для Кабуто важнее всего?",
        patch_data={
            "world": {"setting_name": "Shadowrun"},
            "character": {"name": "Кабуто"},
        },
        question_topics=["character.values"],
    )
    ask_fears = _decision(
        "Чего Кабуто боится больше всего?",
        patch_data={
            "character": {"values": ["Вырваться с улиц"]}
        },
        question_topics=["character.fears"],
    )
    ask_desires = _decision(
        "О чём Кабуто мечтает?",
        patch_data={
            "character": {"fears": ["Потерять дорогих ему людей"]}
        },
        question_topics=["character.desires"],
    )
    repaired = _decision(
        "Этого уже достаточно, чтобы понять его внутренний стержень. "
        "Представь первую сцену: где Кабуто оказывается перед новым делом?",
        patch_data={
            "character": {"desires": ["Выбраться из нищеты"]}
        },
        question_topics=["world.starting_location_name"],
    )
    model = AsyncMock(side_effect=[ask_values, ask_fears, ask_desires, repaired])

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new=model,
    ):
        await interview.answer(campaign.id, "Героя зовут Кабуто")
        await interview.answer(campaign.id, "Он хочет вырваться с улиц")
        decision = await interview.answer(
            campaign.id,
            "Он боится потерять немногих дорогих ему людей",
        )

    assert decision.assistant_message == repaired["assistant_message"]
    assert decision.question_topics == ["world.starting_location_name"]
    assert decision.draft.character.desires == ["Выбраться из нищеты"]
    feedback = model.await_args_list[-1].args[2][-1].content
    assert "questionnaire_pattern" in feedback
