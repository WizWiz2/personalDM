from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import cli
from app.models.campaign import CampaignCreate
from app.models.provider_config import ProviderConfigRead
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.campaign_service import CampaignService
from app.services.session_zero_interview import SessionZeroInterviewService

RATE_LIMIT_ERROR = LLMProviderError(
    "Failed to obtain valid JSON: LLM returned HTTP 429: "
    '{"error":{"message":"Rate limit reached: Limit 12000, Used 5078, '
    'Requested 7519. Please try again in 2.985s.",'
    '"code":"rate_limit_exceeded"}}'
)

DECISION = {
    "assistant_message": "Какой характер у Кабуто?",
    "ready_to_finalize": False,
    "question_topics": ["character.personality"],
    "draft": {
        "world": {
            "setting_name": "Shadowrun",
            "genre": "киберпанк и городское фэнтези",
            "world_summary": "Шестой мир Shadowrun.",
        },
        "character": {
            "name": "Кабуто",
            "appearance": "Обожжённое лицо скрыто маской-шлемом; одежда практичная.",
        },
    },
}


@pytest.mark.asyncio
async def test_session_zero_waits_for_groq_window_and_retries_once(
    db_session: AsyncSession,
):
    campaign = await CampaignService(db_session).create_campaign(
        CampaignCreate(name="Shadowrun budget")
    )
    await db_session.commit()
    interview = SessionZeroInterviewService(db_session)
    calls = []

    async def fake_generate(self, provider, selection, messages, **kwargs):
        calls.append((messages, kwargs))
        if len(calls) == 1:
            raise RATE_LIMIT_ERROR
        return DECISION

    with (
        patch(
            "app.services.session_zero_interview.RoleModelRouter.generate_json",
            new=fake_generate,
        ),
        patch(
            "app.services.session_zero_interview.asyncio.sleep",
            new_callable=AsyncMock,
        ) as sleep,
    ):
        decision = await interview.answer(campaign.id, "Хочу сыграть в Shadowrun")

    assert decision.assistant_message == "Какой характер у Кабуто?"
    assert len(calls) == 2
    assert calls[0][1]["max_tokens"] == 1200
    sleep.assert_awaited_once_with(3.235)
    assert (await interview.get_state(campaign.id)).pending_user_message is None


@pytest.mark.asyncio
async def test_session_zero_prompt_uses_compact_draft_and_six_message_history(
    db_session: AsyncSession,
):
    campaign = await CampaignService(db_session).create_campaign(
        CampaignCreate(name="Compact interview")
    )
    await db_session.commit()
    interview = SessionZeroInterviewService(db_session)
    state = await interview.get_state(campaign.id)
    state.messages = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"m{index}"}
        for index in range(20)
    ]
    state.messages.append({"role": "user", "content": "Последний ответ"})
    state.pending_user_message = "Последний ответ"
    await interview._save_state(campaign.id, state, commit=True)
    captured = {}

    async def fake_generate(self, provider, selection, messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return DECISION

    with patch(
        "app.services.session_zero_interview.RoleModelRouter.generate_json",
        new=fake_generate,
    ):
        await interview.retry_pending(campaign.id)

    messages = captured["messages"]
    assert len(messages) == 7
    assert [item.content for item in messages[1:]] == [
        "m15",
        "m16",
        "m17",
        "m18",
        "m19",
        "Последний ответ",
    ]
    assert captured["kwargs"]["max_tokens"] == 1200
    assert '\n  "world"' not in messages[0].content
    assert '[CURRENT DRAFT]\n{"world":' in messages[0].content


@pytest.mark.asyncio
async def test_cli_hides_raw_rate_limit_payload(
    db_session: AsyncSession,
    capsys,
):
    campaign = await CampaignService(db_session).create_campaign(
        CampaignCreate(name="Quiet 429")
    )
    await db_session.commit()

    with (
        patch(
            "app.services.session_zero_interview.RoleModelRouter.generate_json",
            new_callable=AsyncMock,
            side_effect=[RATE_LIMIT_ERROR, RATE_LIMIT_ERROR],
        ),
        patch(
            "app.services.session_zero_interview.asyncio.sleep",
            new_callable=AsyncMock,
        ),
        patch("builtins.input", side_effect=["Хочу сыграть в Shadowrun"]),
    ):
        completed = await cli.run_session_zero_interview(campaign.id, db_session)

    output = capsys.readouterr().out
    assert completed is False
    assert "Техническая причина" not in output
    assert "console.groq.com" not in output
    assert "Failed to obtain valid JSON" not in output
    assert "Твой ответ сохранён" in output


class _RateLimitedResponse:
    status_code = 429
    text = (
        '{"error":{"message":"Please try again in 2.985s",'
        '"code":"rate_limit_exceeded"}}'
    )


@pytest.mark.asyncio
async def test_structured_provider_does_not_retry_429_with_larger_budget():
    provider = LLMProvider()
    config = ProviderConfigRead(
        id=uuid4(),
        campaign_id=uuid4(),
        base_url="https://api.groq.com/openai/v1",
        model_name="llama-3.3-70b-versatile",
        has_api_key=True,
        context_window=32768,
        created_at=datetime.now(UTC),
    )

    with patch.object(
        provider,
        "_post_openai_json",
        new_callable=AsyncMock,
        return_value=(_RateLimitedResponse(), {}),
    ) as request, pytest.raises(LLMProviderError, match="429"):
        await provider.generate_json(
            [ChatMessage(role="user", content="Верни только JSON")],
            config,
            "secret",
            max_tokens=1200,
        )

    assert request.await_count == 1
    assert provider.last_telemetry["attempts"] == [
        {
            "attempt": 1,
            "requested_max_tokens": 1200,
            "status": "error",
            "error": "LLM returned HTTP 429: " + _RateLimitedResponse.text,
        }
    ]
