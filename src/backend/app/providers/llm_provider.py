import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.config import settings
from app.models.provider_config import ProviderConfigRead
from app.models.turn import ChatMessage


class LLMProviderError(RuntimeError):
    """Raised when a provider request cannot produce a usable model response."""


class LLMProvider:
    """Client for OpenAI-compatible and common Ollama-compatible chat APIs."""

    @staticmethod
    def _content_to_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                    if isinstance(text, str):
                        parts.append(text)
            return "".join(parts)
        return ""

    @classmethod
    def _extract_content(cls, data: dict) -> str:
        """Extract text from OpenAI SSE, non-streaming and Ollama JSON shapes."""
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            choice = choices[0] or {}
            delta = choice.get("delta") or {}
            text = cls._content_to_text(delta.get("content"))
            if text:
                return text
            message = choice.get("message") or {}
            text = cls._content_to_text(message.get("content"))
            if text:
                return text
            text = cls._content_to_text(choice.get("text"))
            if text:
                return text

        message = data.get("message")
        if isinstance(message, dict):
            text = cls._content_to_text(message.get("content"))
            if text:
                return text

        for key in ("response", "content", "text"):
            text = cls._content_to_text(data.get(key))
            if text:
                return text
        return ""

    async def generate_stream(
        self,
        messages: list[ChatMessage],
        config: ProviderConfigRead,
        api_key: str | None = None,
    ) -> AsyncIterator[str]:
        """Stream model text or raise LLMProviderError.

        Provider failures are never yielded as narrative text. This prevents an
        HTTP error string or an empty generation from entering campaign history
        and being processed by Memory Scribe as canon.
        """
        url = f"{config.base_url.rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload = {
            "model": config.model_name,
            "messages": [{"role": message.role, "content": message.content} for message in messages],
            "stream": True,
            "max_tokens": settings.RESPONSE_RESERVE_TOKENS,
        }

        emitted_content = False
        parsed_frames = 0
        malformed_frames = 0
        timeout = httpx.Timeout(120.0, connect=10.0)

        try:
            async with httpx.AsyncClient(
                trust_env=False,
                timeout=timeout,
            ) as client:
                async with client.stream(
                    "POST",
                    url,
                    headers=headers,
                    json=payload,
                ) as response:
                    if response.status_code != 200:
                        error_body = await response.aread()
                        detail = error_body.decode(errors="replace")[:2000]
                        raise LLMProviderError(
                            f"LLM returned HTTP {response.status_code}: {detail}"
                        )

                    async for raw_line in response.aiter_lines():
                        if not raw_line:
                            continue

                        line = raw_line.strip()
                        if line.startswith("data:"):
                            line = line[5:].strip()
                        if line == "[DONE]":
                            break
                        if not line:
                            continue

                        try:
                            data = json.loads(line)
                            parsed_frames += 1
                        except json.JSONDecodeError:
                            malformed_frames += 1
                            continue

                        provider_error = data.get("error")
                        if provider_error:
                            raise LLMProviderError(
                                f"LLM provider error: {provider_error}"
                            )

                        content = self._extract_content(data)
                        if content:
                            emitted_content = True
                            yield content

        except httpx.RequestError as exc:
            raise LLMProviderError(f"Failed to reach LLM provider: {exc}") from exc

        if not emitted_content:
            frame_note = (
                f"parsed_frames={parsed_frames}, malformed_frames={malformed_frames}"
            )
            raise LLMProviderError(
                f"LLM completed without usable text ({frame_note})"
            )

    async def check_connection(
        self,
        base_url: str,
        model_name: str,
        api_key: str | None = None,
    ) -> bool:
        url = f"{base_url.rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 5,
            "stream": False,
        }

        try:
            async with httpx.AsyncClient(
                trust_env=False,
                timeout=httpx.Timeout(15.0, connect=5.0),
            ) as client:
                response = await client.post(url, headers=headers, json=payload)
                if response.status_code != 200:
                    return False
                data = response.json()
                return bool(self._extract_content(data)) or bool(data.get("choices"))
        except Exception:
            return False
