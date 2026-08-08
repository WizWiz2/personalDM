from datetime import datetime
from uuid import uuid4

import pytest

from app.models.provider_config import ProviderConfigRead
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider


def _config(context_window: int = 8192) -> ProviderConfigRead:
    return ProviderConfigRead(
        id=uuid4(),
        campaign_id=uuid4(),
        base_url="http://localhost:11434/v1",
        model_name="qwen2.5:7b",
        has_api_key=False,
        context_window=context_window,
        created_at=datetime.utcnow(),
    )


@pytest.mark.asyncio
async def test_native_ollama_json_sends_configured_num_ctx(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {
                "message": {"content": '{"ok": true}'},
                "done": True,
                "done_reason": "stop",
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["payload"] = json
            return FakeResponse()

    monkeypatch.setattr(
        "app.providers.llm_provider.httpx.AsyncClient",
        lambda **kwargs: FakeClient(),
    )

    provider = LLMProvider()
    config = _config(8192)
    result = await provider.generate_json(
        [ChatMessage(role="user", content="Верни только JSON")],
        config,
        max_tokens=321,
    )

    assert result == {"ok": True}
    assert captured["url"] == "http://localhost:11434/api/chat"
    assert captured["payload"]["options"]["num_ctx"] == 8192
    assert captured["payload"]["options"]["num_predict"] == 321
    assert provider.last_telemetry["requested_num_ctx"] == 8192


@pytest.mark.asyncio
async def test_native_ollama_stream_sends_configured_num_ctx():
    class CapturingProvider(LLMProvider):
        def __init__(self):
            super().__init__()
            self.payload = None

        async def _stream_once(self, client, url, headers, payload):
            self.payload = payload
            yield {
                "message": {
                    "content": "Это достаточно длинный завершённый ответ для теста."
                }
            }
            yield {"done": True, "done_reason": "stop"}

    provider = CapturingProvider()
    config = _config(6144)
    chunks = [
        chunk
        async for chunk in provider.generate_stream(
            [ChatMessage(role="user", content="Расскажи сцену")],
            config,
            max_tokens=456,
            temperature=0.4,
        )
    ]

    assert "".join(chunks).endswith(".")
    assert provider.payload["options"]["num_ctx"] == 6144
    assert provider.payload["options"]["num_predict"] == 456
    assert provider.payload["options"]["temperature"] == 0.4
    assert provider.last_telemetry["requested_num_ctx"] == 6144
