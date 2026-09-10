from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import settings

_INSTALLED = False
_SEQUENCE = 0

_OLLAMA_DURATION_FIELDS = {
    "total_duration": "ollama_total_ms",
    "load_duration": "ollama_load_ms",
    "prompt_eval_duration": "ollama_prompt_eval_ms",
    "eval_duration": "ollama_eval_ms",
}


def _duration_ms(value: object) -> float | None:
    """Convert Ollama nanosecond durations to milliseconds."""
    if not isinstance(value, (int, float)) or value < 0:
        return None
    return round(float(value) / 1_000_000.0, 3)


def _ollama_timing_usage(data: dict[str, Any]) -> dict[str, Any]:
    """Extract native Ollama timing counters into the existing telemetry usage envelope."""
    result: dict[str, Any] = {}
    for source, target in _OLLAMA_DURATION_FIELDS.items():
        converted = _duration_ms(data.get(source))
        if converted is not None:
            result[target] = converted

    prompt_count = data.get("prompt_eval_count")
    eval_count = data.get("eval_count")
    prompt_ms = result.get("ollama_prompt_eval_ms")
    eval_ms = result.get("ollama_eval_ms")
    if isinstance(prompt_count, int) and prompt_count >= 0 and prompt_ms:
        result["ollama_prompt_tps"] = round(prompt_count / (prompt_ms / 1000.0), 2)
    if isinstance(eval_count, int) and eval_count >= 0 and eval_ms:
        result["ollama_generation_tps"] = round(eval_count / (eval_ms / 1000.0), 2)
    return result


def _performance_log_path() -> Path:
    explicit = os.getenv("PDM_PERFORMANCE_LOG")
    if explicit:
        return Path(explicit)
    # Normal runtime DATA_DIR is .../PersonalDM/library, so the log lands next to the library.
    # Live contracts set DATA_DIR to <run>/runtime-data, so the log lands in that exact run folder.
    return Path(settings.DATA_DIR).resolve().parent / "llm-performance.jsonl"


def _task_id() -> str | None:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        return None
    return hex(id(task)) if task is not None else None


def _message_stats(messages: object) -> tuple[int, int]:
    if not isinstance(messages, list):
        return 0, 0
    characters = 0
    for message in messages:
        content = getattr(message, "content", None)
        if content is None and isinstance(message, dict):
            content = message.get("content")
        characters += len(str(content or ""))
    return len(messages), characters


def _append_record(record: dict[str, Any]) -> None:
    """Best-effort diagnostics: performance logging must never affect a game turn."""
    global _SEQUENCE
    try:
        _SEQUENCE += 1
        payload = {
            "sequence": _SEQUENCE,
            "timestamp": datetime.now(UTC).isoformat(),
            "task_id": _task_id(),
            **record,
        }
        path = _performance_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except Exception:
        # Instrumentation is deliberately fail-open and must not perturb production behavior.
        return


def _flatten_telemetry(telemetry: dict[str, Any]) -> dict[str, Any]:
    usage = telemetry.get("usage") if isinstance(telemetry.get("usage"), dict) else {}
    attempts = telemetry.get("attempts") if isinstance(telemetry.get("attempts"), list) else []
    result = {
        "model": telemetry.get("model"),
        "status": telemetry.get("status"),
        "transport": telemetry.get("transport"),
        "provider_duration_ms": telemetry.get("duration_ms"),
        "attempt": telemetry.get("attempt"),
        "attempt_count": len(attempts) or (1 if telemetry else 0),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "ollama_total_ms": usage.get("ollama_total_ms"),
        "ollama_load_ms": usage.get("ollama_load_ms"),
        "ollama_prompt_eval_ms": usage.get("ollama_prompt_eval_ms"),
        "ollama_eval_ms": usage.get("ollama_eval_ms"),
        "ollama_prompt_tps": usage.get("ollama_prompt_tps"),
        "ollama_generation_tps": usage.get("ollama_generation_tps"),
        "requested_num_ctx": telemetry.get("requested_num_ctx"),
        "requested_max_tokens": telemetry.get("requested_max_tokens"),
        "response_characters": telemetry.get("response_characters"),
        "finish_reason": telemetry.get("finish_reason"),
        "thinking_disabled": telemetry.get("thinking_disabled"),
    }
    return {key: value for key, value in result.items() if value is not None}


def install() -> None:
    """Record every local LLM call without changing routing, prompts, retries, or results."""
    global _INSTALLED
    if _INSTALLED:
        return

    from app.providers.llm_provider import LLMProvider
    from app.services.role_model_router import RoleModelRouter

    original_extract_usage = LLMProvider._extract_usage

    def extract_usage_with_timings(data):
        usage = dict(original_extract_usage(data))
        usage.update(_ollama_timing_usage(data))
        return usage

    LLMProvider._extract_usage = staticmethod(extract_usage_with_timings)

    original_generate_json = RoleModelRouter.generate_json

    async def measured_generate_json(self, provider, selection, messages, **kwargs):
        started = time.monotonic()
        error: Exception | None = None
        try:
            return await original_generate_json(
                self,
                provider,
                selection,
                messages,
                **kwargs,
            )
        except Exception as exc:
            error = exc
            raise
        finally:
            telemetry = dict(getattr(provider, "last_telemetry", None) or {})
            message_count, prompt_characters = _message_stats(messages)
            response_model = kwargs.get("response_model")
            response_model_name = getattr(response_model, "__name__", None)
            role = getattr(getattr(selection, "role", None), "value", None)
            _append_record(
                {
                    "kind": "structured",
                    "role": role,
                    "stage": (
                        f"{role}:{response_model_name or 'json'}"
                        if role
                        else response_model_name or "structured_json"
                    ),
                    "response_model": response_model_name,
                    "wall_ms": round((time.monotonic() - started) * 1000, 3),
                    "message_count": message_count,
                    "prompt_characters": prompt_characters,
                    "error": str(error)[:1200] if error else None,
                    **_flatten_telemetry(telemetry),
                }
            )

    RoleModelRouter.generate_json = measured_generate_json

    original_generate_stream = LLMProvider.generate_stream

    async def measured_generate_stream(self, messages, config, api_key=None, **kwargs):
        started = time.monotonic()
        error: Exception | None = None
        try:
            async for chunk in original_generate_stream(
                self,
                messages,
                config,
                api_key,
                **kwargs,
            ):
                yield chunk
        except Exception as exc:
            error = exc
            raise
        finally:
            telemetry = dict(getattr(self, "last_telemetry", None) or {})
            message_count, prompt_characters = _message_stats(messages)
            _append_record(
                {
                    "kind": "stream",
                    "role": "text",
                    "stage": "text_stream",
                    "wall_ms": round((time.monotonic() - started) * 1000, 3),
                    "message_count": message_count,
                    "prompt_characters": prompt_characters,
                    "error": str(error)[:1200] if error else None,
                    **_flatten_telemetry(telemetry),
                }
            )

    LLMProvider.generate_stream = measured_generate_stream
    _INSTALLED = True


__all__ = ["_duration_ms", "_ollama_timing_usage", "install"]
