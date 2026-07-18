from __future__ import annotations

from app.providers.llm_provider import LLMProvider


_INSTALLED = False


def install(quality_module) -> None:
    """Make control JSON bypass temporary Scribe/Curator stream wrappers.

    During Scribe and Curator extraction the service temporarily replaces an instance's
    ``generate_stream`` with a JSON adapter. The control-plane must call the underlying
    provider transport instead of recursively invoking that adapter.

    In production the class-level method is the real OpenAI/Ollama transport, so the
    control-plane chooses its non-streaming HTTP path. In deterministic CI the class-level
    method is the test double, so it is invoked directly and still produces reproducible
    JSON without network access.
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

    quality_module._uses_mock_stream = uses_mock_stream
    quality_module._mock_stream_json = mock_stream_json
