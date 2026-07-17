import json
import time
from collections import Counter
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.models.provider_config import ProviderConfigRead
from app.models.turn import ChatMessage


class LLMProviderError(RuntimeError):
    """Raised when a provider request cannot produce a usable model response."""


class LLMProviderTruncatedError(LLMProviderError):
    """Raised when the provider exhausts its output budget before finishing."""

    def __init__(self, message: str, partial_text: str = ""):
        super().__init__(message)
        self.partial_text = partial_text


class LLMProvider:
    """Client for OpenAI-compatible and common Ollama-compatible chat APIs.

    Reasoning/thinking fields are deliberately never exposed as answer text. For local
    Ollama endpoints thinking is disabled by default because otherwise small models can
    spend the whole completion budget on hidden reasoning and return no usable content.
    """

    COMPLETE_ENDINGS = (".", "!", "?", "…", ":", ";", "»", '"', "'", ")", "]", "}", "*")

    def __init__(self):
        self.last_telemetry: dict[str, Any] = {}

    @staticmethod
    def _content_to_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
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

    @classmethod
    def _reasoning_characters(cls, data: dict) -> int:
        """Count hidden reasoning for diagnostics without surfacing its contents."""
        total = 0
        candidates: list[Any] = [data.get("thinking"), data.get("reasoning")]
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            choice = choices[0] or {}
            delta = choice.get("delta") or {}
            message = choice.get("message") or {}
            candidates.extend(
                [
                    delta.get("reasoning_content"),
                    delta.get("thinking"),
                    message.get("reasoning_content"),
                    message.get("thinking"),
                ]
            )
        for value in candidates:
            total += len(cls._content_to_text(value))
        return total

    @staticmethod
    def _extract_finish_reason(data: dict) -> str | None:
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            reason = (choices[0] or {}).get("finish_reason")
            if reason:
                return str(reason)
        reason = data.get("done_reason")
        if reason:
            return str(reason)
        if data.get("done") is True:
            return "stop"
        return None

    @staticmethod
    def _extract_usage(data: dict) -> dict[str, int]:
        usage = data.get("usage")
        if isinstance(usage, dict):
            return {
                key: int(value)
                for key, value in usage.items()
                if isinstance(value, (int, float))
            }
        result: dict[str, int] = {}
        if isinstance(data.get("prompt_eval_count"), int):
            result["prompt_tokens"] = data["prompt_eval_count"]
        if isinstance(data.get("eval_count"), int):
            result["completion_tokens"] = data["eval_count"]
        if result:
            result["total_tokens"] = sum(result.values())
        return result

    @staticmethod
    def _is_ollama(base_url: str) -> bool:
        parsed = urlparse(base_url)
        return parsed.port == 11434 or "ollama" in parsed.hostname.lower() if parsed.hostname else False

    @classmethod
    def _looks_complete(cls, text: str) -> bool:
        clean = text.rstrip()
        if len(clean) < 40:
            return False
        return clean.endswith(cls.COMPLETE_ENDINGS)

    async def generate_stream(
        self,
        messages: list[ChatMessage],
        config: ProviderConfigRead,
        api_key: str | None = None,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        disable_thinking: bool = True,
    ) -> AsyncIterator[str]:
        url = f"{config.base_url.rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        completion_budget = max_tokens or settings.RESPONSE_RESERVE_TOKENS
        payload: dict[str, Any] = {
            "model": config.model_name,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in messages
            ],
            "stream": True,
            "max_tokens": completion_budget,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if disable_thinking and self._is_ollama(config.base_url):
            payload["think"] = False

        started = time.monotonic()
        emitted_parts: list[str] = []
        parsed_frames = 0
        malformed_frames = 0
        reasoning_characters = 0
        finish_reason = None
        usage: dict[str, int] = {}
        http_status = None
        frame_keys: Counter[str] = Counter()
        self.last_telemetry = {
            "model": config.model_name,
            "url": url,
            "status": "started",
            "thinking_disabled": bool(payload.get("think") is False),
            "requested_max_tokens": completion_budget,
        }

        try:
            async with httpx.AsyncClient(
                trust_env=False,
                timeout=httpx.Timeout(180.0, connect=10.0),
            ) as client:
                async with client.stream(
                    "POST",
                    url,
                    headers=headers,
                    json=payload,
                ) as response:
                    http_status = response.status_code
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
                            finish_reason = finish_reason or "stop"
                            break
                        if not line:
                            continue

                        try:
                            data = json.loads(line)
                            parsed_frames += 1
                            frame_keys.update(str(key) for key in data)
                        except json.JSONDecodeError:
                            malformed_frames += 1
                            continue

                        provider_error = data.get("error")
                        if provider_error:
                            raise LLMProviderError(
                                f"LLM provider error: {provider_error}"
                            )

                        finish_reason = self._extract_finish_reason(data) or finish_reason
                        frame_usage = self._extract_usage(data)
                        if frame_usage:
                            usage = frame_usage
                        reasoning_characters += self._reasoning_characters(data)
                        content = self._extract_content(data)
                        if content:
                            emitted_parts.append(content)
                            yield content

        except httpx.RequestError as exc:
            self.last_telemetry = {
                "model": config.model_name,
                "url": url,
                "status": "transport_error",
                "error": str(exc),
                "duration_ms": round((time.monotonic() - started) * 1000),
                "requested_max_tokens": completion_budget,
            }
            raise LLMProviderError(f"Failed to reach LLM provider: {exc}") from exc
        except LLMProviderError as exc:
            partial_text = "".join(emitted_parts)
            self.last_telemetry = {
                "model": config.model_name,
                "url": url,
                "status": "provider_error",
                "http_status": http_status,
                "error": str(exc),
                "parsed_frames": parsed_frames,
                "malformed_frames": malformed_frames,
                "reasoning_characters": reasoning_characters,
                "response_characters": len(partial_text),
                "frame_keys": dict(frame_keys),
                "duration_ms": round((time.monotonic() - started) * 1000),
                "requested_max_tokens": completion_budget,
            }
            raise

        output = "".join(emitted_parts)
        truncated = finish_reason in {"length", "max_tokens"}
        status = "truncated" if truncated else ("completed" if output.strip() else "empty")
        self.last_telemetry = {
            "model": config.model_name,
            "url": url,
            "status": status,
            "http_status": http_status,
            "finish_reason": finish_reason,
            "usage": usage,
            "parsed_frames": parsed_frames,
            "malformed_frames": malformed_frames,
            "reasoning_characters": reasoning_characters,
            "response_characters": len(output),
            "frame_keys": dict(frame_keys),
            "thinking_disabled": bool(payload.get("think") is False),
            "requested_max_tokens": completion_budget,
            "duration_ms": round((time.monotonic() - started) * 1000),
        }

        if truncated and (not output.strip() or not self._looks_complete(output)):
            raise LLMProviderTruncatedError(
                "LLM exhausted the completion budget before producing a complete answer "
                f"(content_chars={len(output)}, reasoning_chars={reasoning_characters})",
                partial_text=output,
            )
        if not output.strip():
            raise LLMProviderError(
                "LLM completed without usable text "
                f"(parsed_frames={parsed_frames}, malformed_frames={malformed_frames}, "
                f"reasoning_chars={reasoning_characters}, finish_reason={finish_reason!r})"
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
        payload: dict[str, Any] = {
            "model": model_name,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 8,
            "stream": False,
        }
        if self._is_ollama(base_url):
            payload["think"] = False
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
