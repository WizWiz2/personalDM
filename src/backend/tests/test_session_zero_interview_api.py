from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.models.session_zero_interview import (
    SessionZeroInterviewDecision,
    SessionZeroInterviewDraft,
    SessionZeroInterviewState,
)
from app.services.session_zero_interview import SessionZeroInterviewService


def _campaign(client: TestClient) -> str:
    response = client.post("/api/campaigns", json={"name": "Conversation First"})
    assert response.status_code == 201
    return response.json()["id"]


def test_interview_snapshot_exposes_opening_persisted_messages_and_summary(
    client: TestClient,
):
    campaign_id = _campaign(client)
    state = SessionZeroInterviewState(
        messages=[
            {"role": "user", "content": "Хочу сыграть в Shadowrun"},
            {"role": "assistant", "content": "Отлично. Какой герой тебя сейчас цепляет?"},
        ],
        last_summary="Текущая сводка кампании",
    )

    with patch.object(
        SessionZeroInterviewService,
        "get_state",
        new=AsyncMock(return_value=state),
    ):
        response = client.get(
            f"/api/campaigns/{campaign_id}/session-zero/interview"
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["opening_message"]
    assert payload["status"] == "draft"
    assert payload["state"]["messages"] == state.messages
    assert payload["summary"] == state.last_summary


def test_interview_answer_uses_conversational_service(client: TestClient):
    campaign_id = _campaign(client)
    draft = SessionZeroInterviewDraft()
    decision = SessionZeroInterviewDecision(
        assistant_message="Понял. Давай соберём этого героя без анкеты.",
        ready_to_finalize=False,
        draft=draft,
        summary="Черновик разговора",
    )
    state = SessionZeroInterviewState(
        messages=[
            {"role": "user", "content": "Хочу быть магом-хакером"},
            {"role": "assistant", "content": decision.assistant_message},
        ],
        draft=draft,
        last_summary=decision.summary,
    )

    with (
        patch.object(
            SessionZeroInterviewService,
            "answer",
            new=AsyncMock(return_value=decision),
        ) as answer,
        patch.object(
            SessionZeroInterviewService,
            "get_state",
            new=AsyncMock(return_value=state),
        ),
    ):
        response = client.post(
            f"/api/campaigns/{campaign_id}/session-zero/interview/answer",
            json={"message": "Хочу быть магом-хакером"},
        )

    assert response.status_code == 200
    answer.assert_awaited_once()
    payload = response.json()
    assert payload["completed"] is False
    assert payload["decision"]["assistant_message"] == decision.assistant_message
    assert payload["summary"] == decision.summary
    assert payload["state"]["messages"][-1]["role"] == "assistant"


def test_interview_retry_uses_same_persisted_service_contract(client: TestClient):
    campaign_id = _campaign(client)
    draft = SessionZeroInterviewDraft()
    decision = SessionZeroInterviewDecision(
        assistant_message="Продолжаем с сохранённого ответа.",
        ready_to_finalize=False,
        draft=draft,
        summary="Сводка после повтора",
    )
    state = SessionZeroInterviewState(
        messages=[{"role": "assistant", "content": decision.assistant_message}],
        draft=draft,
        last_summary=decision.summary,
    )

    with (
        patch.object(
            SessionZeroInterviewService,
            "retry_pending",
            new=AsyncMock(return_value=decision),
        ) as retry,
        patch.object(
            SessionZeroInterviewService,
            "get_state",
            new=AsyncMock(return_value=state),
        ),
    ):
        response = client.post(
            f"/api/campaigns/{campaign_id}/session-zero/interview/retry"
        )

    assert response.status_code == 200
    retry.assert_awaited_once()
    assert response.json()["summary"] == decision.summary


def test_frontend_session_zero_preserves_cli_behavior_without_legacy_form():
    page = (
        Path(__file__).resolve().parents[2]
        / "frontend"
        / "src"
        / "pages"
        / "SessionZeroPage.tsx"
    ).read_text(encoding="utf-8")

    assert "/api/session-zero-ui" not in page
    assert "answerSessionZeroInterview" in page
    assert "retrySessionZeroInterview" in page
    assert "autoRetryAttempted" in page
    assert "Итоговые договорённости" in page
    for command in ("/summary", "/retry", "/error", "/later"):
        assert command in page
