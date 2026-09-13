from __future__ import annotations

import asyncio
import atexit
import json
import os
import time
from collections import defaultdict
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


def _performance_report_path() -> Path:
    log = _performance_log_path()
    return log.with_name("llm-performance.md")


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


def _attempt_summaries(telemetry: dict[str, Any]) -> list[dict[str, Any]]:
    attempts = telemetry.get("attempts")
    if not isinstance(attempts, list):
        return []
    result: list[dict[str, Any]] = []
    for item in attempts:
        if not isinstance(item, dict):
            continue
        usage = item.get("usage") if isinstance(item.get("usage"), dict) else {}
        summary = {
            "attempt": item.get("attempt"),
            "status": item.get("status") or ("completed" if usage else None),
            "requested_max_tokens": item.get("requested_max_tokens"),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "ollama_total_ms": usage.get("ollama_total_ms"),
            "ollama_load_ms": usage.get("ollama_load_ms"),
            "ollama_prompt_eval_ms": usage.get("ollama_prompt_eval_ms"),
            "ollama_eval_ms": usage.get("ollama_eval_ms"),
            "error": item.get("error"),
        }
        result.append({key: value for key, value in summary.items() if value is not None})
    return result


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
        "attempts": _attempt_summaries(telemetry) or None,
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


def _number(record: dict[str, Any], key: str) -> float:
    value = record.get(key)
    return float(value) if isinstance(value, (int, float)) else 0.0


def _load_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
    except OSError:
        return []
    return records


def _render_report(records: list[dict[str, Any]]) -> str:
    groups: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for record in records:
        key = (str(record.get("stage") or "unknown"), str(record.get("model") or "unknown"))
        bucket = groups[key]
        bucket["calls"] += 1
        for field in (
            "wall_ms",
            "ollama_total_ms",
            "ollama_load_ms",
            "ollama_prompt_eval_ms",
            "ollama_eval_ms",
            "prompt_tokens",
            "completion_tokens",
        ):
            bucket[field] += _number(record, field)
        bucket["retries"] += max(0.0, _number(record, "attempt_count") - 1.0)
        if record.get("error") or str(record.get("status") or "").endswith("error"):
            bucket["errors"] += 1

    total_wall = sum(_number(record, "wall_ms") for record in records)
    total_load = sum(_number(record, "ollama_load_ms") for record in records)
    total_prompt = sum(_number(record, "ollama_prompt_eval_ms") for record in records)
    total_eval = sum(_number(record, "ollama_eval_ms") for record in records)
    total_provider = sum(_number(record, "ollama_total_ms") for record in records)
    slow_loads = sum(1 for record in records if _number(record, "ollama_load_ms") >= 500.0)
    retries = sum(max(0, int(_number(record, "attempt_count")) - 1) for record in records)

    model_switches = 0
    previous_model: str | None = None
    for record in records:
        model = str(record.get("model") or "")
        if model and previous_model and model != previous_model:
            model_switches += 1
        if model:
            previous_model = model

    lines = [
        "# PersonalDM LLM performance",
        "",
        f"Calls: **{len(records)}**  ",
        f"Summed call wall time: **{total_wall / 1000.0:.1f}s**  ",
        f"Ollama reported total: **{total_provider / 1000.0:.1f}s**  ",
        f"Model load: **{total_load / 1000.0:.1f}s**  ",
        f"Prompt evaluation: **{total_prompt / 1000.0:.1f}s**  ",
        f"Token generation: **{total_eval / 1000.0:.1f}s**  ",
        f"Structured retries: **{retries}**  ",
        f"Model changes between consecutive calls: **{model_switches}**  ",
        f"Calls with load >= 500ms: **{slow_loads}**",
        "",
        "Times are summed per LLM call; background/concurrent work can make this larger than user-visible elapsed time.",
        "",
        "## By stage and model",
        "",
        "| Stage | Model | Calls | Wall | Load | Prompt | Eval | Prompt tok/s | Gen tok/s | Retries | Errors |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]

    ordered = sorted(groups.items(), key=lambda item: item[1]["wall_ms"], reverse=True)
    for (stage, model), bucket in ordered:
        prompt_tps = (
            bucket["prompt_tokens"] / (bucket["ollama_prompt_eval_ms"] / 1000.0)
            if bucket["ollama_prompt_eval_ms"] > 0
            else 0.0
        )
        generation_tps = (
            bucket["completion_tokens"] / (bucket["ollama_eval_ms"] / 1000.0)
            if bucket["ollama_eval_ms"] > 0
            else 0.0
        )
        lines.append(
            f"| {stage} | {model} | {int(bucket['calls'])} | "
            f"{bucket['wall_ms'] / 1000.0:.1f}s | {bucket['ollama_load_ms'] / 1000.0:.1f}s | "
            f"{bucket['ollama_prompt_eval_ms'] / 1000.0:.1f}s | {bucket['ollama_eval_ms'] / 1000.0:.1f}s | "
            f"{prompt_tps:.1f} | {generation_tps:.1f} | {int(bucket['retries'])} | {int(bucket['errors'])} |"
        )

    lines.extend(
        [
            "",
            "## Slowest calls",
            "",
            "| # | Stage | Model | Wall | Load | Prompt | Eval | Attempts | Status |",
            "| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    slowest = sorted(records, key=lambda item: _number(item, "wall_ms"), reverse=True)[:10]
    for index, record in enumerate(slowest, start=1):
        lines.append(
            f"| {index} | {record.get('stage') or 'unknown'} | {record.get('model') or 'unknown'} | "
            f"{_number(record, 'wall_ms') / 1000.0:.1f}s | "
            f"{_number(record, 'ollama_load_ms') / 1000.0:.1f}s | "
            f"{_number(record, 'ollama_prompt_eval_ms') / 1000.0:.1f}s | "
            f"{_number(record, 'ollama_eval_ms') / 1000.0:.1f}s | "
            f"{int(_number(record, 'attempt_count'))} | {record.get('status') or ''} |"
        )
    lines.append("")
    return "\n".join(lines)


def _write_report() -> None:
    """Write one human-readable summary beside the raw trace at clean process exit."""
    try:
        records = _load_records(_performance_log_path())
        if not records:
            return
        report = _performance_report_path()
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(_render_report(records), encoding="utf-8")
    except Exception:
        return


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
    atexit.register(_write_report)
    _INSTALLED = True


__all__ = [
    "_duration_ms",
    "_ollama_timing_usage",
    "_render_report",
    "install",
]
