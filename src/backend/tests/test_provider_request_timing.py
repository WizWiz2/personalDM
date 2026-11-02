import json
from uuid import uuid4

import httpx
import pytest

from app.providers.llm_provider import LLMProvider
from app.services.llm_usage_tracker import (
    close_usage_context,
    current_usage_context,
    set_usage_context,
)
from app.services.provider_request_timing import timed_client


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


@pytest.mark.asyncio
async def test_headers_service_event_and_first_text_are_distinct(tmp_path, monkeypatch):
    from app.services import provider_request_timing as timing

    clock = Clock()
    monkeypatch.setattr(timing, "perf_counter", clock)
    path = tmp_path / "trace.jsonl"
    monkeypatch.setenv("PDM_PERFORMANCE_LOG", str(path))

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for at, data in [
                (0.03, ": keepalive"),
                (0.04, "data: " + json.dumps({"type": "response.created", "text": "service metadata"})),
                (
                    0.07,
                    "data: " + json.dumps({"type": "response.output_text.delta", "delta": "hello"}),
                ),
                (0.09, "data: " + json.dumps({"type": "response.completed"})),
            ]:
                clock.now = at
                yield (data + "\n\n").encode()
            clock.now = 0.1

    async def handle(request):
        clock.now = 0.02
        return httpx.Response(200, headers={"x-request-id": "safe-request-id"}, stream=Stream())

    token = set_usage_context(campaign_id=uuid4(), user_turn_id=uuid4(), performance_started=0)
    try:
        async with timed_client(httpx.AsyncClient(transport=httpx.MockTransport(handle))) as client:
            result = [
                item
                async for item in LLMProvider()._stream_once(
                    client,
                    "https://example.test/responses",
                    {"Authorization": "Bearer SECRET"},
                    {
                        "model": "test",
                        "input": [{"role": "user", "content": "PRIVATE"}],
                        "stream": True,
                    },
                )
            ]
        assert result[-1]["type"] == "response.completed"
        op = next(
            o for o in current_usage_context().performance.intervals if o["category"] == "provider"
        )
        assert op["headers_ms"] == 20
        assert op["first_event_ms"] == 40
        assert op["first_text_ms"] == 70
        assert op["body_ms"] == 80
        assert op["after_first_text_ms"] == 30
        assert op["provider_request_id"] == "safe-request-id"
        assert op["parent_id"] == current_usage_context().performance.trace_id
    finally:
        await close_usage_context(token)
    raw = path.read_text()
    assert "SECRET" not in raw and "PRIVATE" not in raw
    records = [json.loads(line) for line in raw.splitlines()]
    assert [x["kind"] for x in records][:2] == ["provider_request_start", "provider_request"]


@pytest.mark.asyncio
async def test_buffered_response_has_header_and_body_times_but_no_fake_first_text(
    tmp_path, monkeypatch
):
    from app.services import provider_request_timing as timing

    clock = Clock()
    monkeypatch.setattr(timing, "perf_counter", clock)
    monkeypatch.setenv("PDM_PERFORMANCE_LOG", str(tmp_path / "trace.jsonl"))

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            clock.now = 0.08
            yield b'{"ok":true}'

    async def handle(request):
        clock.now = 0.03
        return httpx.Response(200, stream=Stream())

    token = set_usage_context(campaign_id=uuid4(), user_turn_id=uuid4(), performance_started=0)
    try:
        async with timed_client(httpx.AsyncClient(transport=httpx.MockTransport(handle))) as client:
            assert (await client.post("https://example.test", json={"model": "test"})).json() == {
                "ok": True
            }
        op = next(
            o for o in current_usage_context().performance.intervals if o["category"] == "provider"
        )
        assert op["headers_ms"] == 30 and op["body_ms"] == 50
        assert op["first_text_ms"] is None
        assert op["mode"] == "buffered"
    finally:
        await close_usage_context(token)


@pytest.mark.asyncio
async def test_cancelled_http_request_keeps_a_start_and_partial_measurement(tmp_path, monkeypatch):
    import asyncio

    path = tmp_path / "trace.jsonl"
    monkeypatch.setenv("PDM_PERFORMANCE_LOG", str(path))
    entered = asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"type":"response.created"}\n\n'
            entered.set()
            await asyncio.Event().wait()

    async def handle(request):
        return httpx.Response(200, stream=Stream())

    token = set_usage_context(campaign_id=uuid4(), user_turn_id=uuid4())

    async def run():
        async with timed_client(httpx.AsyncClient(transport=httpx.MockTransport(handle))) as client:
            return [
                x
                async for x in LLMProvider()._stream_once(
                    client, "https://example.test", {}, {"model": "test"}
                )
            ]

    try:
        task = asyncio.create_task(run())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        op = next(
            o for o in current_usage_context().performance.intervals if o["category"] == "provider"
        )
        assert op["status"] == "cancelled"
        assert op["first_event_ms"] is not None and op["first_text_ms"] is None
    finally:
        await close_usage_context(token)
    rows = [json.loads(l) for l in path.read_text().splitlines()]
    start = next(r for r in rows if r["kind"] == "provider_request_start")
    finish = next(r for r in rows if r["kind"] == "provider_request")
    assert start["http_request_id"] == finish["http_request_id"]


@pytest.mark.asyncio
async def test_json_retry_has_two_http_requests_under_one_logical_call(tmp_path, monkeypatch):
    from pydantic import BaseModel

    from app.models.turn import ChatMessage
    from app.providers import llm_provider as module
    from app.services.performance_telemetry_guard import install
    from app.services.role_model_router import ModelRole, RoleModelRouter, RoleModelSelection
    from tests.test_chatgpt_plan_provider import _chatgpt_config

    class Result(BaseModel):
        ok: bool

    install()
    path = tmp_path / "trace.jsonl"
    monkeypatch.setenv("PDM_PERFORMANCE_LOG", str(path))
    count = 0

    async def handle(request):
        nonlocal count
        count += 1
        output = "NOT JSON" if count == 1 else '{"ok":true}'
        events = [
            {"type": "response.output_text.delta", "delta": output},
            {
                "type": "response.completed",
                "response": {"usage": {"input_tokens": 5, "output_tokens": 5}},
            },
        ]
        return httpx.Response(200, text="\n\n".join("data: " + json.dumps(x) for x in events))

    original = httpx.AsyncClient
    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        lambda **kw: original(**kw, transport=httpx.MockTransport(handle)),
    )
    config = _chatgpt_config()
    selection = RoleModelSelection(ModelRole.PLANNER, config, "token", config, "token", "test")
    token = set_usage_context(campaign_id=uuid4(), user_turn_id=uuid4())
    try:
        result = await RoleModelRouter(None).generate_json(
            LLMProvider(),
            selection,
            [ChatMessage(role="user", content="test")],
            response_model=Result,
        )
        assert result == {"ok": True}
        trace = current_usage_context().performance.snapshot()
        assert trace["logical_calls"] == 1 and trace["http_requests"] == 2
        requests = [o for o in trace["operations"] if o["category"] == "provider"]
        call = next(o for o in trace["operations"] if o["category"] == "llm")
        assert len({r["http_request_id"] for r in requests}) == 2
        assert {r["parent_id"] for r in requests} == {call["operation_id"]}
        assert call["http_request_count"] == 2 and call["call_unaccounted_ms"] >= 0
        names = {o["name"] for o in trace["operations"] if o["category"] == "local"}
        assert {"request.prepare", "response.json_decode", "response.model_validate"} <= names
        assert all(r["effective_max_tokens"] is None for r in requests)
        assert all(r["schema_sha256"] for r in requests)
    finally:
        await close_usage_context(token)
