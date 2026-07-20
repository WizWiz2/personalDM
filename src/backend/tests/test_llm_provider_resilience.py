import pytest
from pydantic import BaseModel
from typing import Literal

from app.models.turn import ChatMessage
from app.providers import llm_provider as llm_provider_module
from app.providers.llm_provider import LLMProvider


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._payload


class _FakeAsyncClient:
    requests = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, headers=None, json=None):
        self.requests.append({"url": url, "headers": headers, "json": json})
        if len(self.requests) == 1:
            return _FakeResponse(
                {
                    "message": {
                        "thinking": "reasoning consumed the first budget",
                        "content": "",
                    },
                    "done": True,
                    "done_reason": "length",
                    "prompt_eval_count": 700,
                    "eval_count": 900,
                }
            )
        return _FakeResponse(
            {
                "message": {"content": '{"desired_active":[]}'},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 760,
                "eval_count": 40,
            }
        )


class _EvaluationPayload(BaseModel):
    status: Literal["progressing", "resolved"]
    evidence: str


class _MockConfig:
    base_url = "http://localhost:11434/v1"
    model_name = "gemma4:e4b"


def test_ollama_endpoint_detection():
    assert LLMProvider._is_ollama("http://127.0.0.1:11434/v1") is True
    assert LLMProvider._is_ollama("http://localhost:11434/v1") is True
    assert LLMProvider._is_ollama("https://api.example.com/v1") is False


def test_ollama_native_chat_url_discards_v1_suffix():
    assert (
        LLMProvider._ollama_native_url("http://localhost:11434/v1")
        == "http://localhost:11434/api/chat"
    )


def test_reasoning_is_counted_but_not_returned_as_content():
    frame = {
        "choices": [
            {
                "delta": {
                    "reasoning_content": "hidden chain",
                    "content": "visible answer",
                }
            }
        ]
    }
    assert LLMProvider._reasoning_characters(frame) == len("hidden chain")
    assert LLMProvider._extract_content(frame) == "visible answer"


def test_native_ollama_thinking_is_counted():
    frame = {
        "message": {
            "thinking": "hidden native reasoning",
            "content": "",
        },
        "done": True,
        "done_reason": "length",
    }
    assert LLMProvider._reasoning_characters(frame) == len("hidden native reasoning")
    assert LLMProvider._extract_content(frame) == ""
    assert LLMProvider._extract_finish_reason(frame) == "length"


def test_reasoning_only_frame_has_no_usable_content():
    frame = {
        "choices": [
            {
                "delta": {
                    "reasoning_content": "long hidden reasoning",
                },
                "finish_reason": "length",
            }
        ]
    }
    assert LLMProvider._reasoning_characters(frame) == len("long hidden reasoning")
    assert LLMProvider._extract_content(frame) == ""
    assert LLMProvider._extract_finish_reason(frame) == "length"


def test_completion_shape_detection():
    assert LLMProvider._looks_complete(
        "Гаррик показывает на западный овраг и велит группе держаться ниже гребня."
    ) is True
    assert LLMProvider._looks_complete(
        "Гаррик показывает на западный овраг и велит группе держаться"
    ) is False
    assert LLMProvider._looks_complete("коротко") is False


def test_json_prompt_detection_is_explicit():
    assert LLMProvider._expects_json(
        [ChatMessage(role="system", content="Верни только JSON с ключом proposals.")]
    ) is True
    assert LLMProvider._expects_json(
        [ChatMessage(role="system", content="Напиши художественный ответ на русском языке.")]
    ) is False


def test_balanced_json_parser_extracts_object_without_markdown_noise():
    assert LLMProvider._parse_json_object(
        'Пояснение до ответа {"proposals":[{"value":1}]} хвост'
    ) == {"proposals": [{"value": 1}]}


def test_balanced_json_parser_accepts_fenced_json():
    assert LLMProvider._parse_json_object(
        '```json\n{"desired_active":[]}\n```'
    ) == {"desired_active": []}


def test_control_retry_increases_budget_without_consuming_whole_context():
    assert LLMProvider._adaptive_budget(900, 1) == 900
    assert LLMProvider._adaptive_budget(900, 2) == 1800
    assert LLMProvider._adaptive_budget(1600, 2) == 2048


def test_usage_can_reveal_silent_budget_exhaustion():
    assert LLMProvider._budget_exhausted(
        None,
        {"completion_tokens": 1015},
        1024,
    ) is True
    assert LLMProvider._budget_exhausted(
        "stop",
        {"completion_tokens": 400},
        1024,
    ) is False


def test_openai_compatibility_variants_remove_optional_flags():
    payload = LLMProvider._openai_no_reasoning_payload(
        {
            "model": "model",
            "messages": [],
            "response_format": {"type": "json_object"},
        }
    )
    variants = LLMProvider._openai_compat_variants(payload)
    assert variants[0]["reasoning_effort"] == "none"
    assert "reasoning_effort" not in variants[1]
    assert "chat_template_kwargs" not in variants[2]
    assert "response_format" not in variants[-1]


@pytest.mark.asyncio
async def test_native_ollama_json_retries_reasoning_only_length(monkeypatch):
    _FakeAsyncClient.requests = []
    monkeypatch.setattr(llm_provider_module.httpx, "AsyncClient", _FakeAsyncClient)

    provider = LLMProvider()
    result = await provider.generate_json(
        [ChatMessage(role="system", content="Верни только JSON.")],
        _MockConfig(),
        max_tokens=900,
        temperature=0.1,
    )

    assert result == {"desired_active": []}
    assert len(_FakeAsyncClient.requests) == 2
    first = _FakeAsyncClient.requests[0]
    second = _FakeAsyncClient.requests[1]
    assert first["url"] == "http://localhost:11434/api/chat"
    assert first["json"]["think"] is False
    assert first["json"]["format"] == "json"
    assert first["json"]["options"]["num_predict"] == 900
    assert second["json"]["options"]["num_predict"] == 1800
    assert len(second["json"]["messages"]) == 2
    assert provider.last_telemetry["attempt"] == 2
    assert provider.last_telemetry["transport"] == "ollama_native_json"


class _SchemaRepairClient:
    requests = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, headers=None, json=None):
        self.requests.append({"url": url, "headers": headers, "json": json})
        if len(self.requests) == 1:
            return _FakeResponse(
                {
                    "message": {
                        "content": '{"status":"awaiting_schema","evidence":"x"}'
                    },
                    "done": True,
                    "done_reason": "stop",
                }
            )
        return _FakeResponse(
            {
                "message": {
                    "content": '{"status":"progressing","evidence":"исправлено"}'
                },
                "done": True,
                "done_reason": "stop",
            }
        )


@pytest.mark.asyncio
async def test_native_ollama_uses_schema_and_repairs_validation_error(monkeypatch):
    _SchemaRepairClient.requests = []
    monkeypatch.setattr(llm_provider_module.httpx, "AsyncClient", _SchemaRepairClient)

    provider = LLMProvider()
    result = await provider.generate_json(
        [ChatMessage(role="system", content="Верни только JSON.")],
        _MockConfig(),
        max_tokens=400,
        temperature=0.0,
        response_model=_EvaluationPayload,
    )

    assert result == {"status": "progressing", "evidence": "исправлено"}
    assert len(_SchemaRepairClient.requests) == 2
    first_payload = _SchemaRepairClient.requests[0]["json"]
    second_payload = _SchemaRepairClient.requests[1]["json"]
    assert first_payload["format"]["properties"]["status"]["enum"] == [
        "progressing",
        "resolved",
    ]
    repair_text = second_payload["messages"][-1]["content"]
    assert "awaiting_schema" in repair_text
    assert "validation" in repair_text
    assert provider.last_telemetry["schema_enforced"] is True
    assert provider.last_telemetry["response_model"] == "_EvaluationPayload"
