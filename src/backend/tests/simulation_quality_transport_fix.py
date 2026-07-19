from __future__ import annotations

from app.providers.llm_provider import LLMProvider
from app.services.memory_scribe import MemoryScribe


_INSTALLED = False


def install(quality_module) -> None:
    """Align quality controls with production provider and outcome-first Scribe."""
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    original_uses_mock_stream = quality_module._uses_mock_stream
    original_mock_stream_json = quality_module._mock_stream_json
    original_scribe = MemoryScribe.extract_proposals

    def underlying_class_method(provider):
        return getattr(type(provider), "generate_stream", LLMProvider.generate_stream)

    def uses_mock_stream(provider) -> bool:
        bound = provider.generate_stream
        method = getattr(bound, "__func__", bound)
        if getattr(method, "__name__", "") == "json_stream":
            underlying = underlying_class_method(provider)
            module = getattr(underlying, "__module__", "")
            return module != "app.providers.llm_provider"
        return original_uses_mock_stream(provider)

    async def mock_stream_json(
        provider,
        messages,
        config,
        api_key,
        max_tokens,
        temperature,
    ):
        bound = provider.generate_stream
        method = getattr(bound, "__func__", bound)
        if getattr(method, "__name__", "") != "json_stream":
            return await original_mock_stream_json(
                provider,
                messages,
                config,
                api_key,
                max_tokens,
                temperature,
            )
        underlying = underlying_class_method(provider)
        raw = ""
        async for token in underlying(
            provider,
            messages,
            config,
            api_key,
            max_tokens=max_tokens,
            temperature=temperature,
        ):
            raw += token
        return quality_module._balanced_json_object(raw), "mock_stream"

    def control_budget(label: str, requested: int) -> int:
        floors = {
            "builder": 1800,
            "curator": 1600,
            "scribe": 1400,
            "player": 640,
            "evaluator": 640,
        }
        return max(int(requested), floors.get(label, int(requested)))

    async def generate_control_json(
        provider,
        messages,
        config,
        api_key,
        *,
        label,
        max_tokens,
        temperature,
    ):
        quality_module.CONTROL_STATS[f"{label}_calls"] += 1
        budget = control_budget(label, max_tokens)
        quality_module.CONTROL_STATS[f"{label}_requested_tokens"] += budget
        try:
            if uses_mock_stream(provider):
                data, transport = await mock_stream_json(
                    provider,
                    messages,
                    config,
                    api_key,
                    budget,
                    temperature,
                )
                attempt = 1
            else:
                data = await provider.generate_json(
                    messages,
                    config,
                    api_key,
                    max_tokens=budget,
                    temperature=temperature,
                )
                telemetry = dict(provider.last_telemetry or {})
                transport = str(telemetry.get("transport") or "provider_json")
                attempt = int(telemetry.get("attempt") or 1)
                quality_module.CONTROL_STATS[
                    f"{label}_reasoning_characters"
                ] += int(telemetry.get("reasoning_characters") or 0)
                quality_module.CONTROL_STATS[
                    f"{label}_response_characters"
                ] += int(telemetry.get("response_characters") or 0)
            quality_module.CONTROL_STATS[f"{label}_success"] += 1
            if attempt > 1:
                quality_module.CONTROL_STATS[f"{label}_repair_success"] += 1
            quality_module._write_health()
            return quality_module.ControlJSONResult(
                data=data,
                attempt=attempt,
                transport=transport,
            )
        except Exception as exc:
            quality_module.record_control_failure(label, exc)
            if quality_module.quality_mode():
                raise quality_module.BenchmarkControlError(
                    f"{label} unavailable: {exc}"
                ) from exc
            return None

    async def audited_scribe(self, *args, **kwargs):
        quality_module.CONTROL_STATS["scribe_calls"] += 1
        proposals = await original_scribe(self, *args, **kwargs)
        audit = dict(getattr(self, "last_audit", {}) or {})
        failure = None
        if audit.get("legacy_envelope"):
            failure = "legacy Scribe envelope has no outcome evidence"
        elif not audit.get("envelope_valid", True):
            failure = audit.get("error") or "outcome envelope failed semantic validation"
        elif int(audit.get("gap_count") or 0) > 0:
            failure = f"{audit.get('gap_count')} durable outcomes have no canon delta"
        if failure:
            quality_module.record_control_failure("scribe_semantics", failure)
            if quality_module.quality_mode():
                raise quality_module.BenchmarkControlError(
                    f"Scribe semantics invalid: {failure}"
                )
        else:
            quality_module.CONTROL_STATS["scribe_success"] += 1
            quality_module.CONTROL_STATS["scribe_outcomes"] += int(
                audit.get("durable_outcome_count") or 0
            )
            quality_module.CONTROL_STATS["scribe_covered_outcomes"] += int(
                audit.get("covered_outcome_count") or 0
            )
            quality_module._write_health()
        return proposals

    quality_module._uses_mock_stream = uses_mock_stream
    quality_module._mock_stream_json = mock_stream_json
    quality_module.generate_control_json = generate_control_json
    MemoryScribe.extract_proposals = audited_scribe
