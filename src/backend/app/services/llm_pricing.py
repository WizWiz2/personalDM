from __future__ import annotations

from dataclasses import dataclass


PRICING_VERSION = "openai-standard-2026-10-03"
LONG_CONTEXT_THRESHOLD = 272_000


@dataclass(frozen=True)
class TextTokenRates:
    input: float
    cached_input: float
    cache_write: float
    output: float
    long_input: float
    long_cached_input: float
    long_cache_write: float
    long_output: float


# USD per 1M tokens. These are deliberately explicit instead of fetched at runtime:
# PersonalDM is a local/offline-first app and usage recording must never depend on the web.
_OPENAI_STANDARD_RATES: dict[str, TextTokenRates] = {
    "gpt-6-astra": TextTokenRates(10.0, 1.0, 12.5, 50.0, 20.0, 2.0, 25.0, 75.0),
    "gpt-6.1-sol": TextTokenRates(2.0, 0.1, 2.5, 10.0, 4.0, 0.2, 5.0, 15.0),
    "gpt-6-sol": TextTokenRates(2.0, 0.2, 2.5, 10.0, 4.0, 0.4, 5.0, 15.0),
    "gpt-6-luna": TextTokenRates(0.1, 0.01, 0.125, 0.5, 0.2, 0.02, 0.25, 0.75),
    "gpt-5.6-sol": TextTokenRates(4.0, 0.4, 5.0, 20.0, 8.0, 0.8, 10.0, 30.0),
    "gpt-5.6": TextTokenRates(4.0, 0.4, 5.0, 20.0, 8.0, 0.8, 10.0, 30.0),
    "gpt-5.6-terra": TextTokenRates(2.0, 0.2, 2.5, 12.0, 4.0, 0.4, 5.0, 18.0),
    "gpt-5.6-luna": TextTokenRates(0.2, 0.02, 0.25, 1.2, 0.4, 0.04, 0.5, 1.8),
}


def _rates_for_model(model_name: str) -> tuple[str, TextTokenRates] | None:
    folded = model_name.casefold().strip()
    # Match the most specific aliases first so snapshots inherit the right rate.
    for key in sorted(_OPENAI_STANDARD_RATES, key=len, reverse=True):
        if folded == key or folded.startswith(key + "-"):
            return key, _OPENAI_STANDARD_RATES[key]
    return None


def estimate_openai_text_cost_usd(
    *,
    model_name: str,
    input_tokens: int,
    cached_input_tokens: int,
    cache_write_tokens: int,
    output_tokens: int,
) -> tuple[float | None, str | None]:
    match = _rates_for_model(model_name)
    if match is None:
        return None, None
    pricing_key, rates = match

    input_tokens = max(0, int(input_tokens))
    cached_input_tokens = max(0, min(int(cached_input_tokens), input_tokens))
    cache_write_tokens = max(
        0,
        min(int(cache_write_tokens), input_tokens - cached_input_tokens),
    )
    uncached_input = max(0, input_tokens - cached_input_tokens - cache_write_tokens)
    output_tokens = max(0, int(output_tokens))

    long_context = input_tokens > LONG_CONTEXT_THRESHOLD
    if long_context:
        input_rate = rates.long_input
        cached_rate = rates.long_cached_input
        write_rate = rates.long_cache_write
        output_rate = rates.long_output
        context_label = "long"
    else:
        input_rate = rates.input
        cached_rate = rates.cached_input
        write_rate = rates.cache_write
        output_rate = rates.output
        context_label = "short"

    cost = (
        uncached_input * input_rate
        + cached_input_tokens * cached_rate
        + cache_write_tokens * write_rate
        + output_tokens * output_rate
    ) / 1_000_000
    basis = f"{PRICING_VERSION}:{pricing_key}:{context_label}"
    return round(cost, 8), basis
