from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker
from app.db.repositories.llm_usage_repo import LLMUsageRepository
from app.services.llm_pricing import estimate_openai_text_cost_usd


@dataclass(frozen=True)
class LLMUsageContext:
    campaign_id: UUID
    user_turn_id: UUID
    generation_run_id: UUID | None = None
    assistant_turn_id: UUID | None = None
    bind: Any | None = None


_current_usage_context: ContextVar[LLMUsageContext | None] = ContextVar(
    "personaldm_llm_usage_context",
    default=None,
)


def set_usage_context(
    *,
    campaign_id: UUID,
    user_turn_id: UUID,
    generation_run_id: UUID | None = None,
    assistant_turn_id: UUID | None = None,
    bind: Any | None = None,
) -> Token:
    return _current_usage_context.set(
        LLMUsageContext(
            campaign_id=campaign_id,
            user_turn_id=user_turn_id,
            generation_run_id=generation_run_id,
            assistant_turn_id=assistant_turn_id,
            bind=bind,
        )
    )


def reset_usage_context(token: Token) -> None:
    _current_usage_context.reset(token)


def current_usage_context() -> LLMUsageContext | None:
    return _current_usage_context.get()


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    return 0


def _usage_parts(usage: dict[str, Any]) -> dict[str, int]:
    input_tokens = _int(
        usage.get("input_tokens")
        or usage.get("prompt_tokens")
        or usage.get("prompt_eval_count")
    )
    output_tokens = _int(
        usage.get("output_tokens")
        or usage.get("completion_tokens")
        or usage.get("eval_count")
    )

    input_details = usage.get("input_tokens_details")
    if not isinstance(input_details, dict):
        input_details = usage.get("prompt_tokens_details")
    if not isinstance(input_details, dict):
        input_details = {}

    output_details = usage.get("output_tokens_details")
    if not isinstance(output_details, dict):
        output_details = usage.get("completion_tokens_details")
    if not isinstance(output_details, dict):
        output_details = {}

    cached_input = _int(input_details.get("cached_tokens"))
    cache_write = _int(input_details.get("cache_write_tokens"))
    reasoning = _int(output_details.get("reasoning_tokens"))
    total = _int(usage.get("total_tokens")) or input_tokens + output_tokens

    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": min(cached_input, input_tokens),
        "cache_write_tokens": min(cache_write, input_tokens),
        "output_tokens": output_tokens,
        "reasoning_tokens": min(reasoning, output_tokens),
        "total_tokens": total,
    }


def _aggregate_usage(telemetry: dict[str, Any]) -> tuple[dict[str, int], int]:
    attempts = telemetry.get("attempts")
    attempt_usages: list[dict[str, Any]] = []
    if isinstance(attempts, list):
        for attempt in attempts:
            if isinstance(attempt, dict) and isinstance(attempt.get("usage"), dict):
                attempt_usages.append(attempt["usage"])

    usages = attempt_usages or (
        [telemetry["usage"]] if isinstance(telemetry.get("usage"), dict) else []
    )
    totals = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
    }
    for usage in usages:
        parts = _usage_parts(usage)
        for key in totals:
            totals[key] += parts[key]

    request_count = len(attempts) if isinstance(attempts, list) and attempts else 1
    return totals, max(1, request_count)


def _provider_kind(telemetry: dict[str, Any]) -> str:
    transport = str(telemetry.get("transport") or "")
    if transport == "chatgpt_responses":
        return "chatgpt"
    if telemetry.get("native_ollama"):
        return "local"
    url = str(telemetry.get("url") or "")
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").casefold()
        if host in {"127.0.0.1", "localhost", "::1"}:
            return "local"
        if host == "api.openai.com":
            return "openai"
    except ValueError:
        pass
    return "openai_compatible"


async def record_provider_telemetry(telemetry: dict[str, Any] | None) -> None:
    context = current_usage_context()
    if context is None or not telemetry:
        return

    usage, request_count = _aggregate_usage(telemetry)
    model_name = str(
        telemetry.get("model")
        or telemetry.get("resolved_model")
        or "unknown"
    )
    provider_kind = _provider_kind(telemetry)
    estimated_cost_usd = None
    pricing_basis = None
    if provider_kind in {"chatgpt", "openai"}:
        estimated_cost_usd, pricing_basis = estimate_openai_text_cost_usd(
            model_name=model_name,
            input_tokens=usage["input_tokens"],
            cached_input_tokens=usage["cached_input_tokens"],
            cache_write_tokens=usage["cache_write_tokens"],
            output_tokens=usage["output_tokens"],
        )

    event = {
        **usage,
        "model_role": (
            str(telemetry.get("model_role"))
            if telemetry.get("model_role") is not None
            else None
        ),
        "model_name": model_name,
        "provider_kind": provider_kind,
        "transport": (
            str(telemetry.get("transport"))
            if telemetry.get("transport") is not None
            else None
        ),
        "status": str(telemetry.get("status") or "unknown"),
        "request_count": request_count,
        "duration_ms": (
            _int(telemetry.get("duration_ms"))
            if telemetry.get("duration_ms") is not None
            else None
        ),
        "estimated_cost_usd": estimated_cost_usd,
        "pricing_basis": pricing_basis,
    }

    if context.bind is None:
        return
    factory = async_sessionmaker(
        bind=context.bind,
        expire_on_commit=False,
        autoflush=False,
    )
    async with factory() as session:
        await LLMUsageRepository(session).record(context, event)
        await session.commit()
