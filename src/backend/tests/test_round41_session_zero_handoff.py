from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.campaign import CampaignCreate
from app.models.session_zero_interview import SessionZeroInterviewModelDecision
from app.services.campaign_service import CampaignService
from app.services.session_zero_interview import SessionZeroInterviewService


PLAYABLE_PATCH = {
    "world": {
        "setting_name": "Неоновый мегаполис",
        "genre": "классический киберпанк с магией и фэнтезийными расами",
        "starting_location_name": "Ядро",
        "starting_situation": (
            "Кабуто пережидает ночь на подпольном технорынке, когда рядом появляется "
            "возможность заработать на странной маготехнической находке."
        ),
    },
    "character": {
        "name": "Кабуто",
        "description": (
            "Эльф, уличный маг и хакер в закрытом шлеме, скрывающем обезображенное лицо."
        ),
        "first_goal": "Выжить, заработать и постепенно выбраться с улиц.",
    },
}


async def _campaign(db_session: AsyncSession, name: str):
    campaign = await CampaignService(db_session).create_campaign(CampaignCreate(name=name))
    await db_session.commit()
    return campaign


def test_start_game_disposition_is_authoritative_even_without_finalize_tool() -> None:
    decision = SessionZeroInterviewModelDecision.model_validate(
        {
            "assistant_message": "Основа готова. Передаю управление первой сцене.",
            "conversation_disposition": "start_game",
            "tool_calls": [],
            "question_topics": [],
        }
    )

    assert decision.conversation_disposition == "start_game"
    assert decision.ready_to_finalize is True
    assert [call.name for call in decision.tool_calls] == ["finalize_session_zero"]


def test_continue_response_is_not_rewritten_with_stock_question() -> None:
    original = "Понял. Магия здесь давно встроена в повседневную жизнь города."
    decision = SessionZeroInterviewModelDecision.model_validate(
        {
            "assistant_message": original,
            "conversation_disposition": "continue",
            "tool_calls": [],
            "question_topics": [],
        }
    )

    assert decision.assistant_message == original
    assert "Что тебе хочется добавить" not in decision.assistant_message
    assert decision.ready_to_finalize is False


@pytest.mark.asyncio
async def test_bare_nachinaem_hands_off_in_same_turn_without_keyword_guard(
    db_session: AsyncSession,
) -> None:
    campaign = await _campaign(db_session, "Kabuto handoff")
    interview = SessionZeroInterviewService(db_session)
    response = {
        "assistant_message": "Основа готова. Начинаем с первой сцены.",
        "conversation_disposition": "start_game",
        "tool_calls": [
            {"name": "update_session_zero", "patch": PLAYABLE_PATCH},
        ],
        "question_topics": [],
    }

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new_callable=AsyncMock,
        return_value=response,
    ):
        decision = await interview.answer(campaign.id, "начинаем")

    assert decision.ready_to_finalize is True
    assert decision.missing_topics == []
    assert decision.draft.character.name == "Кабуто"
    assert decision.draft.world.starting_location_name == "Ядро"
    assert "?" not in decision.assistant_message


@pytest.mark.asyncio
async def test_semantic_start_does_not_need_phrase_from_legacy_marker_list(
    db_session: AsyncSession,
) -> None:
    campaign = await _campaign(db_session, "Semantic start wording")
    interview = SessionZeroInterviewService(db_session)
    response = {
        "assistant_message": "Хорошо. Основа собрана, переходим к приключению.",
        "conversation_disposition": "start_game",
        "tool_calls": [
            {"name": "update_session_zero", "patch": PLAYABLE_PATCH},
        ],
        "question_topics": [],
    }

    # This wording is intentionally absent from START_REQUEST_MARKERS. The typed semantic
    # disposition, not a growing phrase list, must own the handoff decision.
    assert "ну всё, пора" not in interview.START_REQUEST_MARKERS

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new_callable=AsyncMock,
        return_value=response,
    ):
        decision = await interview.answer(campaign.id, "ну всё, пора")

    assert decision.ready_to_finalize is True
