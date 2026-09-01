from __future__ import annotations

import asyncio
import time
from functools import wraps

from app.config import settings
from app.providers.llm_provider import LLMProvider, LLMProviderError

_INSTALLED = False
_ORIGINAL_GENERATE_JSON = None


def install() -> None:
    """Bound one structured control call across all of its internal repair attempts.

    ``LLMProvider.generate_json`` historically allowed three HTTP attempts with a 240 second
    timeout each. A contradictory turn could therefore block one control stage for roughly twelve
    minutes before the normal turn failure/recovery path got a chance to run. Keep the provider's
    adaptive repair logic, but give the whole operation one gameplay-sized deadline.
    """

    global _INSTALLED, _ORIGINAL_GENERATE_JSON
    if _INSTALLED:
        return

    original = LLMProvider.generate_json
    _ORIGINAL_GENERATE_JSON = original

    @wraps(original)
    async def bounded_generate_json(self, *args, **kwargs):
        deadline = float(settings.CONTROL_REQUEST_DEADLINE_SECONDS)
        if deadline <= 0:
            return await original(self, *args, **kwargs)

        started = time.monotonic()
        try:
            return await asyncio.wait_for(
                original(self, *args, **kwargs),
                timeout=deadline,
            )
        except TimeoutError as exc:
            previous = dict(getattr(self, "last_telemetry", {}) or {})
            self.last_telemetry = {
                **previous,
                "status": "control_deadline_exceeded",
                "deadline_seconds": deadline,
                "duration_ms": round((time.monotonic() - started) * 1000),
            }
            raise LLMProviderError(
                f"Structured control call exceeded {deadline:g}s deadline"
            ) from exc

    LLMProvider.generate_json = bounded_generate_json
    _INSTALLED = True


__all__ = ["install"]
