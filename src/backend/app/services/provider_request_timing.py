"""Observe HTTP boundaries without changing request bodies, retries or stream contents.

First text is a protocol-visible output delta, not server reasoning or a keepalive.
All measurements are client-observed; server queue/prefill/reasoning times stay unknown.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from time import perf_counter
from uuid import uuid4

from app.services.turn_performance import (
    call_reason,
    current_trace,
    operation_parent,
    record_interval,
)

_current_request: ContextVar[RequestTiming | None] = ContextVar(
    "provider_request_timing", default=None
)


@contextmanager
def local_span(name):
    started = perf_counter()
    try:
        yield
    finally:
        record_interval("local", name, started, perf_counter())


def measured_local(name):
    def decorate(function):
        @wraps(function)
        def measured(*args, **kwargs):
            with local_span(name):
                return function(*args, **kwargs)

        return measured

    return decorate


def model_schema_json(model):
    with local_span("schema.prompt"):
        return json.dumps(model.model_json_schema(), ensure_ascii=False)


def validate_structured(model, parsed):
    with local_span("response.model_validate"):
        return model.model_validate(parsed).model_dump(mode="json")


def _write(record):
    from app.services.performance_telemetry_guard import _append_record

    _append_record(record)


@dataclass
class RequestTiming:
    payload: dict
    mode: str
    started: float = field(default_factory=lambda: perf_counter())
    request_id: str = field(default_factory=lambda: str(uuid4()))
    sent: float | None = None
    headers: float | None = None
    first_line: float | None = None
    first_event: float | None = None
    first_text: float | None = None
    body_end: float | None = None
    http_status: int | None = None
    provider_request_id: str | None = None
    request_bytes: int | None = None
    observed_events: int = 0

    def metadata(self):
        format_spec = (
            self.payload.get("text", {}).get("format")
            or self.payload.get("response_format")
            or self.payload.get("format")
        )
        schema = None
        if isinstance(format_spec, dict):
            schema = format_spec.get("schema") or (format_spec.get("json_schema") or {}).get(
                "schema"
            )
            if not schema and (format_spec.get("type") == "object" or "properties" in format_spec):
                schema = format_spec
        schema_text = (
            json.dumps(schema, ensure_ascii=False, separators=(",", ":")) if schema else ""
        )
        messages = self.payload.get("input") or self.payload.get("messages") or []
        return {
            "http_request_id": self.request_id,
            "operation_id": self.request_id,
            "parent_id": operation_parent.get()
            or (current_trace().trace_id if current_trace() else None),
            "reason": call_reason.get() or "normal_call",
            "model": self.payload.get("model"),
            "mode": self.mode,
            "request_bytes": self.request_bytes,
            "schema_characters": len(schema_text),
            "schema_sha256": hashlib.sha256(schema_text.encode()).hexdigest()
            if schema_text
            else None,
            "request_prompt_characters": sum(
                len(str(m.get("content", ""))) for m in messages if isinstance(m, dict)
            ),
            "effective_reasoning": self.payload.get(
                "reasoning", self.payload.get("reasoning_effort")
            ),
            "effective_max_tokens": self.payload.get(
                "max_output_tokens",
                self.payload.get(
                    "max_tokens", (self.payload.get("options") or {}).get("num_predict")
                ),
            ),
            "effective_think": self.payload.get("think"),
        }

    def observe(self, data, visible):
        now = perf_counter()
        if self.first_event is None:
            self.first_event = now
        self.observed_events += 1
        if visible and self.first_text is None:
            self.first_text = now

    def finish(self, error=None):
        ended = perf_counter()

        def offset(value):
            return round((value - self.started) * 1000, 3) if value is not None else None

        def duration(start, end):
            return (
                round(max(0, end - start) * 1000, 3)
                if start is not None and end is not None
                else None
            )

        status = (
            "cancelled"
            if isinstance(error, asyncio.CancelledError)
            else "error"
            if error
            else "http_error"
            if self.http_status and self.http_status >= 400
            else "completed"
        )
        record = {
            **self.metadata(),
            "http_status": self.http_status,
            "provider_request_id": self.provider_request_id,
            "status": status,
            "error_type": type(error).__name__ if error else None,
            "wall_ms": offset(ended),
            "request_build_ms": offset(self.sent),
            "headers_ms": offset(self.headers),
            "first_line_ms": offset(self.first_line),
            "first_event_ms": offset(self.first_event),
            "first_text_ms": offset(self.first_text),
            "headers_wait_ms": duration(self.sent, self.headers),
            "body_ms": duration(self.headers, self.body_end),
            "after_first_text_ms": duration(self.first_text, self.body_end),
            "observed_events": self.observed_events,
            "server_queue_ms": None,
            "server_reasoning_ms": None,
        }
        record_interval("provider", "HTTP request", self.started, ended, **record)
        _write({"kind": "provider_request", **record})


async def _request_hook(request):
    probe = _current_request.get()
    if probe:
        probe.sent = perf_counter()
        probe.request_bytes = len(request.content)


async def _response_hook(response):
    probe = _current_request.get()
    if probe:
        probe.headers = perf_counter()
        probe.http_status = response.status_code
        probe.provider_request_id = response.headers.get("x-request-id") or response.headers.get(
            "request-id"
        )


def observe_event(data, visible=None):
    probe = _current_request.get()
    if probe:
        probe.observe(data, visible)


class _TimedResponse:
    def __init__(self, response, probe):
        self._response, self._probe = response, probe

    def __getattr__(self, name):
        return getattr(self._response, name)

    async def aiter_lines(self):
        try:
            async for line in self._response.aiter_lines():
                if self._probe.first_line is None:
                    self._probe.first_line = perf_counter()
                yield line
        finally:
            self._probe.body_end = perf_counter()

    async def aread(self):
        try:
            return await self._response.aread()
        finally:
            self._probe.body_end = perf_counter()


class TimedClient:
    def __init__(self, client):
        self._client = client
        hooks = getattr(client, "event_hooks", None)
        if isinstance(hooks, dict):
            hooks.setdefault("request", []).append(_request_hook)
            hooks.setdefault("response", []).append(_response_hook)

    @contextmanager
    def request(self, payload, mode):
        probe = RequestTiming(payload, mode)
        token = _current_request.set(probe)
        _write({"kind": "provider_request_start", **probe.metadata()})
        error = None
        try:
            yield probe
        except BaseException as exc:
            error = exc
            raise
        finally:
            try:
                probe.finish(error)
            finally:
                _current_request.reset(token)

    async def post(self, url, **kwargs):
        with self.request(kwargs.get("json") or {}, "buffered") as probe:
            response = await self._client.post(url, **kwargs)
            probe.body_end = perf_counter()
            probe.http_status = response.status_code
            return response

    @asynccontextmanager
    async def stream(self, method, url, **kwargs):
        with self.request(kwargs.get("json") or {}, "stream") as probe:
            async with self._client.stream(method, url, **kwargs) as response:
                probe.http_status = response.status_code
                try:
                    yield _TimedResponse(response, probe)
                finally:
                    if probe.body_end is None:
                        probe.body_end = perf_counter()


@asynccontextmanager
async def timed_client(client):
    async with client as entered:
        yield TimedClient(entered)
