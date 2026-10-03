from __future__ import annotations

import json
from datetime import datetime
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
from pydantic import BaseModel

from app.config import settings
from app.models.provider_config import ProviderConfigRead
from app.models.turn import ChatMessage
from app.providers import llm_provider as llm_provider_module
from app.providers.llm_provider import LLMProvider
from app.services.chatgpt_auth_service import ChatGPTAuthError, ChatGPTAuthService


class _StructuredPayload(BaseModel):
    ok: bool


def _chatgpt_config() -> ProviderConfigRead:
    return ProviderConfigRead(
        id=uuid4(),
        campaign_id=uuid4(),
        base_url="https://api.openai.com/v1",
        model_name="gpt-test",
        has_api_key=True,
        context_window=128000,
        provider_kind="chatgpt",
        created_at=datetime.utcnow(),
    )


class _FakeStreamResponse:
    status_code = 200

    def __init__(self, lines: list[str]):
        self.lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_lines(self):
        for line in self.lines:
            yield line

    async def aread(self):
        return b""


class _ResponsesClient:
    requests: list[dict] = []
    response_text = "Hello from ChatGPT."

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, method, url, headers=None, json=None):
        self.requests.append(
            {"method": method, "url": url, "headers": headers, "json": json}
        )
        lines = [
            "data: " + __import__("json").dumps(
                {"type": "response.output_text.delta", "delta": self.response_text}
            ),
            "data: " + __import__("json").dumps(
                {
                    "type": "response.completed",
                    "response": {
                        "usage": {"input_tokens": 12, "output_tokens": 4}
                    },
                }
            ),
        ]
        return _FakeStreamResponse(lines)


@pytest.mark.asyncio
async def test_chatgpt_plan_stream_uses_stateless_responses_contract(monkeypatch):
    _ResponsesClient.requests = []
    _ResponsesClient.response_text = "Hello from ChatGPT."
    monkeypatch.setattr(llm_provider_module.httpx, "AsyncClient", _ResponsesClient)

    provider = LLMProvider()
    chunks = [
        chunk
        async for chunk in provider.generate_stream(
            [
                ChatMessage(role="system", content="System contract"),
                ChatMessage(role="user", content="Say hello"),
            ],
            _chatgpt_config(),
            "oauth-token",
        )
    ]

    assert "".join(chunks) == "Hello from ChatGPT."
    request = _ResponsesClient.requests[0]
    assert request["url"] == "https://api.openai.com/v1/responses"
    assert request["headers"]["Authorization"] == "Bearer oauth-token"
    assert request["json"]["stream"] is True
    assert request["json"]["store"] is False
    assert request["json"]["input"][0]["role"] == "developer"
    assert request["json"]["input"][1]["role"] == "user"
    assert "temperature" not in request["json"]
    assert "max_output_tokens" not in request["json"]
    assert provider.last_telemetry["transport"] == "chatgpt_responses"


@pytest.mark.asyncio
async def test_chatgpt_plan_structured_calls_parse_streamed_json(monkeypatch):
    _ResponsesClient.requests = []
    _ResponsesClient.response_text = json.dumps({"ok": True})
    monkeypatch.setattr(llm_provider_module.httpx, "AsyncClient", _ResponsesClient)

    provider = LLMProvider()
    result = await provider.generate_json(
        [ChatMessage(role="system", content="Верни только JSON.")],
        _chatgpt_config(),
        "oauth-token",
        response_model=_StructuredPayload,
    )

    assert result == {"ok": True}
    assert _ResponsesClient.requests[0]["json"]["stream"] is True
    assert _ResponsesClient.requests[0]["json"]["store"] is False
    assert provider.last_telemetry["transport"] == "chatgpt_responses"


def test_chatgpt_sign_in_uses_dynamic_client_pkce_and_loopback(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    ChatGPTAuthService._attempts.clear()
    service = ChatGPTAuthService()

    authorization_url = service.start_sign_in(
        redirect_uri="http://127.0.0.1:8000/api/runtime/providers/chatgpt/callback",
        return_url="http://localhost:5173/#/settings",
    )
    query = parse_qs(urlparse(authorization_url).query)

    assert query["client_id"] == ["dynamic_agent_client"]
    assert query["agent_name_hint"] == ["PersonalDM"]
    assert query["redirect_uri"] == [
        "http://127.0.0.1:8000/api/runtime/providers/chatgpt/callback"
    ]
    assert query["code_challenge_method"] == ["S256"]
    assert query["resource"] == ["https://api.openai.com/v1"]
    assert "chatgpt.tokens.use.direct" in query["scope"][0]
    assert query["ext_agent_host_id"][0].startswith("urn:uuid:")
    assert "code_verifier" not in query


def test_chatgpt_sign_in_rejects_nonlocal_return_url(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DATA_DIR", str(tmp_path))
    service = ChatGPTAuthService()

    with pytest.raises(ChatGPTAuthError, match="локальный PersonalDM"):
        service.start_sign_in(
            redirect_uri="http://127.0.0.1:8000/api/runtime/providers/chatgpt/callback",
            return_url="https://example.com/steal",
        )
