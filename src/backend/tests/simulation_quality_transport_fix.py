from __future__ import annotations

from app.providers.llm_provider import LLMProvider


_INSTALLED = False


def install(quality_module) -> None:
    """Route every real control request through production ``generate_json``.

    Temporary Scribe and Curator wrappers still need the underlying class-level mock
    transport in deterministic CI. Real providers use the same native Ollama or
    OpenAI-compatible path as the application, including adaptive budgets and telemetry.
    """

    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    original_uses_mock_stream = quality_module._uses_mock_stream
    original_mock_stream_json = quality_module._mock_stream_json

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

    quality_module._uses_mock_stream = uses_mock_stream
    quality_module._mock_stream_json = mock_stream_json
    quality_module.generate_control_json = generate_control_json
