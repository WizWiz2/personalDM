from unittest.mock import AsyncMock

import pytest

from app.models.session_zero_interview import (
    SessionZeroInterviewDraft,
    SessionZeroInterviewModelDecision,
    SessionZeroInterviewState,
)
from app.services.session_zero_interview import SessionZeroInterviewService


def _minimal_draft() -> SessionZeroInterviewDraft:
    return SessionZeroInterviewDraft.model_validate(
        {
            "world": {
                "setting_name": "Shadowrun",
            },
            "character": {
                "name": "Кабуто",
                "description": "Эльф-хакер и маг в маске.",
                "first_goal": "Найти кибердеку получше.",
            },
        }
    )


def test_technical_start_defaults_fill_only_missing_fields():
    draft = _minimal_draft()

    materialized = SessionZeroInterviewService._technical_start_defaults(draft)

    assert materialized.world.setting_name == "Shadowrun"
    assert materialized.character.name == "Кабуто"
    assert materialized.character.description == "Эльф-хакер и маг в маске."
    assert materialized.character.first_goal == "Найти кибердеку получше."
    assert materialized.world.starting_location_name
    assert materialized.world.starting_situation
    assert SessionZeroInterviewService.missing_fields(materialized) == []


def test_start_claim_detection_covers_natural_russian_phrase():
    assert SessionZeroInterviewService._assistant_claims_start(
        "Отлично. Начнём расследование с портовых складов."
    )


@pytest.mark.asyncio
async def test_start_claim_without_finalize_becomes_ready(monkeypatch):
    service = object.__new__(SessionZeroInterviewService)
    service._router = AsyncMock()
    service._provider = object()
    service._agent = AsyncMock()

    selection = object()
    service._router.resolve.return_value = selection
    service._agent.respond.return_value = SessionZeroInterviewModelDecision(
        assistant_message="Отлично. Начнём расследование.",
        tool_calls=[],
        question_topics=[],
    )

    state = SessionZeroInterviewState(
        draft=_minimal_draft(),
        messages=[
            {"role": "user", "content": "Хочу расследование в Shadowrun."},
        ],
        pending_user_message="Погнали",
    )

    materialized = SessionZeroInterviewService._technical_start_defaults(state.draft)
    service._materialize_start = AsyncMock(return_value=materialized)
    service._save_state = AsyncMock()

    decision = await service._continue_pending("00000000-0000-0000-0000-000000000001", state)

    assert decision.ready_to_finalize is True
    assert decision.missing_topics == []
    service._materialize_start.assert_awaited_once()
    service._save_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_explicit_start_request_does_not_depend_on_finalize_tool(monkeypatch):
    service = object.__new__(SessionZeroInterviewService)
    service._router = AsyncMock()
    service._provider = object()
    service._agent = AsyncMock()

    selection = object()
    service._router.resolve.return_value = selection
    service._agent.respond.return_value = SessionZeroInterviewModelDecision(
        assistant_message="Хорошо, подготовлю первую сцену.",
        tool_calls=[],
        question_topics=[],
    )

    state = SessionZeroInterviewState(
        draft=_minimal_draft(),
        messages=[],
        pending_user_message="Давай играть",
    )
    materialized = SessionZeroInterviewService._technical_start_defaults(state.draft)
    service._materialize_start = AsyncMock(return_value=materialized)
    service._save_state = AsyncMock()

    decision = await service._continue_pending("00000000-0000-0000-0000-000000000001", state)

    assert decision.ready_to_finalize is True
    assert decision.question_topics == []
