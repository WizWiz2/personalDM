"""Perf lever 2: a strict response schema travels once, as the response format, not also as prompt text."""
import json

import httpx
import pytest
from pydantic import BaseModel

from app.models.turn import ChatMessage
from app.providers import llm_provider as provider_module
from app.providers.llm_provider import LLMProvider
from app.services.provider_request_timing import model_schema_json
from tests.test_chatgpt_plan_provider import _chatgpt_config


class Wire(BaseModel):
    kind: str


@pytest.mark.asyncio
async def test_strict_schema_is_not_repeated_in_the_prompt(monkeypatch):
    seen = {}

    def handle(request):
        seen.update(json.loads(request.content))
        events = [{"type": "response.output_text.delta", "delta": json.dumps({"kind": "x"})},
                  {"type": "response.completed", "response": {"usage": {"input_tokens": 1, "output_tokens": 1}}}]
        return httpx.Response(200, text="\n\n".join("data: " + json.dumps(event) for event in events))

    client_type = httpx.AsyncClient
    monkeypatch.setattr(provider_module.httpx, "AsyncClient", lambda **kwargs: client_type(
        **kwargs, transport=httpx.MockTransport(handle)))
    prompt = "Rules.\n[OUTPUT JSON SCHEMA]\n" + model_schema_json(Wire)
    await LLMProvider().generate_json([ChatMessage(role="system", content=prompt)], _chatgpt_config(), "t",
                                      response_model=Wire)
    text = "".join(str(item["content"]) for item in seen["input"])
    assert seen["text"]["format"]["strict"] is True
    assert '"properties"' not in text and "Rules." in text
