import json
import time
from collections import Counter
from collections.abc import AsyncIterator
from copy import deepcopy
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.models.provider_config import ProviderConfigRead
from app.models.turn import ChatMessage


class LLMProviderError(RuntimeError):
    """Raised when a provider request cannot produce a usable model response."""


class LLMProviderTruncatedError(LLMProviderError):
    """Raised when a provider exhausts its output budget before finishing."""

    def __init__(self, message: str, partial_text: str = ""):
        super().__init__(message)
        self.partial_text = partial_text


class LLMProvider:
    """OpenAI-compatible client with a native Ollama fast path.

    Narrative requests stream. Explicit JSON requests use a non-streaming structured
    transport with adaptive output budgets. Ollama is called through ``/api/chat`` so
    ``think=false`` and ``format=json`` are applied by the native API instead of being
    silently ignored by an OpenAI compatibility shim.
    """

    COMPLETE_ENDINGS = (".", "!", "?", "…", ":", ";", "»", '"', "'", ")", "]", "}", "*")
    JSON_MARKERS = (
        "верни только json",
        "верни один json",
        "верни ровно один json",
        "return exactly one json",
        "return only json",
        "return one json",
    )
    TRUNCATION_REASONS = {"length", "max_tokens", "max_token", "limit"}

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
        """Count hidden reasoning for diagnostics without exposing it."""
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
        message = data.get("message")
        if isinstance(message, dict):
            candidates.extend(
                [
                    message.get("thinking"),
                    message.get("reasoning"),
                    message.get("reasoning_content"),
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
        hostname = (parsed.hostname or "").casefold()
        return parsed.port == 11434 or "ollama" in hostname

    @staticmethod
    def _ollama_native_url(base_url: str) -> str:
        parsed = urlparse(base_url)
        scheme = parsed.scheme or "http"
        netloc = parsed.netloc or parsed.path.split("/", 1)[0]
        return f"{scheme}://{netloc}/api/chat"

    @classmethod
    def _looks_complete(cls, text: str) -> bool:
        clean = text.rstrip()
        if len(clean) < 20:
            return False
        return clean.endswith(cls.COMPLETE_ENDINGS)

    @classmethod
    def _expects_json(cls, messages: list[ChatMessage]) -> bool:
        text = "\n".join(message.content for message in messages).casefold()
        return any(marker in text for marker in cls.JSON_MARKERS)

    @staticmethod
    def _parse_json_object(text: str) -> dict[str, Any]:
        clean = text.strip()
        if clean.startswith("```"):
            lines = clean.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            clean = "\n".join(lines).strip()
        try:
            value = json.loads(clean)
            if isinstance(value, dict):
                return value
        except Exception:
            pass

        start = clean.find("{")
        if start < 0:
            raise LLMProviderError("structured response does not contain a JSON object")
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(clean)):
            char = clean[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(clean[start : index + 1])
                    except json.JSONDecodeError as exc:
                        raise LLMProviderError(
                            f"structured response contains invalid JSON: {exc}"
                        ) from exc
                    if not isinstance(value, dict):
                        raise LLMProviderError("structured response JSON is not an object")
                    return value
        raise LLMProviderError("structured response contains incomplete JSON")

    @staticmethod
    def _messages_payload(messages: list[ChatMessage]) -> list[dict[str, str]]:
        return [{"role": message.role, "content": message.content} for message in messages]

    @staticmethod
    def _repair_instruction() -> dict[str, str]:
        return {
            "role": "user",
            "content": (
                "Не рассуждай вслух. Верни только один короткий валидный JSON-объект "
                "строго по заданной схеме. Не используй markdown и пояснения."
            ),
        }

    @staticmethod
    def _adaptive_budget(base: int, attempt: int) -> int:
        if attempt <= 1:
            return base
        ceiling = max(base, int(settings.LLM_CONTEXT_WINDOW * 0.5))
        return min(ceiling, max(base + 512, base * 2))

    @staticmethod
    def _completion_tokens(usage: dict[str, int]) -> int:
        return int(
            usage.get("completion_tokens")
            or usage.get("output_tokens")
            or usage.get("eval_count")
            or 0
        )

    @classmethod
    def _budget_exhausted(
        cls,
        finish_reason: str | None,
        usage: dict[str, int],
        budget: int,
    ) -> bool:
        reason = (finish_reason or "").casefold()
        if reason in cls.TRUNCATION_REASONS:
            return True
        completion_tokens = cls._completion_tokens(usage)
        return bool(completion_tokens and completion_tokens >= max(1, int(budget * 0.97)))

    @staticmethod
    def _openai_no_reasoning_payload(payload: dict[str, Any]) -> dict[str, Any]:
        result = deepcopy(payload)
        result["reasoning_effort"] = "none"
        result["chat_template_kwargs"] = {"enable_thinking": False}
        return result

    @staticmethod
    def _openai_compat_variants(payload: dict[str, Any]) -> list[dict[str, Any]]:
        variants = [deepcopy(payload)]
        for key in ("reasoning_effort", "chat_template_kwargs", "response_format"):
            previous = deepcopy(variants[-1])
            previous.pop(key, None)
            if previous not in variants:
                variants.append(previous)
        return variants

    async def _post_openai_json(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> tuple[httpx.Response, dict[str, Any]]:
        last_response: httpx.Response | None = None
        candidate = payload
        for candidate in self._openai_compat_variants(payload):
            response = await client.post(url, headers=headers, json=candidate)
            last_response = response
            if response.status_code != 400:
                return response, candidate
        assert last_response is not None
        return last_response, candidate

    async def generate_json(
        self,
        messages: list[ChatMessage],
        config: ProviderConfigRead,
        api_key: str | None = None,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        """Return one validated JSON object with adaptive budget and repair."""
        is_ollama = self._is_ollama(config.base_url)
        url = (
            self._ollama_native_url(config.base_url)
            if is_ollama
            else f"{config.base_url.rstrip('/')}/chat/completions"
        )
        headers = {"Content-Type": "application/json"}
        if api_key and not is_ollama:
            headers["Authorization"] = f"Bearer {api_key}"

        base_budget = max_tokens or settings.CONTROL_RESPONSE_RESERVE_TOKENS
        base_messages = self._messages_payload(messages)
        started = time.monotonic()
        last_error: Exception | None = None
        attempt_telemetry: list[dict[str, Any]] = []

        async with httpx.AsyncClient(
            trust_env=False,
            timeout=httpx.Timeout(240.0, connect=10.0),
        ) as client:
            for attempt in range(1, 4):
                budget = self._adaptive_budget(base_budget, attempt)
                request_messages = list(base_messages)
                if attempt > 1:
                    request_messages.append(self._repair_instruction())

                if is_ollama:
                    payload: dict[str, Any] = {
                        "model": config.model_name,
                        "messages": request_messages,
                        "stream": False,
                        "format": "json",
                        "think": False,
                        "options": {"num_predict": budget},
                    }
                    if temperature is not None:
                        payload["options"]["temperature"] = (
                            temperature if attempt == 1 else 0.0
                        )
                else:
                    payload = self._openai_no_reasoning_payload(
                        {
                            "model": config.model_name,
                            "messages": request_messages,
                            "stream": False,
                            "max_tokens": budget,
                            "response_format": {"type": "json_object"},
                        }
                    )
                    if temperature is not None:
                        payload["temperature"] = temperature if attempt == 1 else 0.0

                try:
                    if is_ollama:
                        response = await client.post(url, headers=headers, json=payload)
                        used_payload = payload
                    else:
                        response, used_payload = await self._post_openai_json(
                            client, url, headers, payload
                        )
                    if response.status_code != 200:
                        raise LLMProviderError(
                            f"LLM returned HTTP {response.status_code}: {response.text[:2000]}"
                        )
                    data = response.json()
                    if not isinstance(data, dict):
                        raise LLMProviderError("LLM returned a non-object response")
                    provider_error = data.get("error")
                    if provider_error:
                        raise LLMProviderError(f"LLM provider error: {provider_error}")

                    finish_reason = self._extract_finish_reason(data)
                    usage = self._extract_usage(data)
                    reasoning_chars = self._reasoning_characters(data)
                    raw_text = self._extract_content(data)
                    if isinstance(data.get("response"), dict):
                        parsed = data["response"]
                        raw_text = json.dumps(parsed, ensure_ascii=False)
                    else:
                        if not raw_text:
                            raise LLMProviderError(
                                "LLM completed without structured content "
                                f"(reasoning_chars={reasoning_chars}, "
                                f"finish_reason={finish_reason!r}, budget={budget})"
                            )
                        parsed = self._parse_json_object(raw_text)

                    telemetry = {
                        "attempt": attempt,
                        "requested_max_tokens": budget,
                        "finish_reason": finish_reason,
                        "usage": usage,
                        "reasoning_characters": reasoning_chars,
                        "response_characters": len(raw_text),
                        "http_status": response.status_code,
                        "native_ollama": is_ollama,
                        "thinking_disabled": bool(
                            used_payload.get("think") is False
                            or used_payload.get("reasoning_effort") == "none"
                            or (
                                used_payload.get("chat_template_kwargs") or {}
                            ).get("enable_thinking") is False
                        ),
                    }
                    attempt_telemetry.append(telemetry)
                    self.last_telemetry = {
                        "model": config.model_name,
                        "url": url,
                        "status": "completed",
                        "control_plane": True,
                        "transport": "ollama_native_json" if is_ollama else "http_json",
                        "attempt": attempt,
                        "attempts": attempt_telemetry,
                        **telemetry,
                        "duration_ms": round((time.monotonic() - started) * 1000),
                    }
                    return parsed
                except (httpx.RequestError, LLMProviderError, json.JSONDecodeError) as exc:
                    last_error = exc
                    attempt_telemetry.append(
                        {
                            "attempt": attempt,
                            "requested_max_tokens": budget,
                            "status": "error",
                            "error": str(exc),
                        }
                    )

        self.last_telemetry = {
            "model": config.model_name,
            "url": url,
            "status": "structured_error",
            "error": str(last_error or "unknown structured response error"),
            "attempts": attempt_telemetry,
            "duration_ms": round((time.monotonic() - started) * 1000),
        }
        raise LLMProviderError(f"Failed to obtain valid JSON: {last_error}")

    async def _stream_once(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        async with client.stream("POST", url, headers=headers, json=payload) as response:
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
                    yield {"done": True, "done_reason": "stop"}
                    break
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    yield {"_malformed": True}
                    continue
                if isinstance(data, dict):
                    yield data

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
        if self._expects_json(messages):
            payload = await self.generate_json(
                messages,
                config,
                api_key,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            yield json.dumps(payload, ensure_ascii=False)
            return

        is_ollama = self._is_ollama(config.base_url)
        url = (
            self._ollama_native_url(config.base_url)
            if is_ollama
            else f"{config.base_url.rstrip('/')}/chat/completions"
        )
        headers = {"Content-Type": "application/json"}
        if api_key and not is_ollama:
            headers["Authorization"] = f"Bearer {api_key}"

        completion_budget = max_tokens or settings.RESPONSE_RESERVE_TOKENS
        if is_ollama:
            payload: dict[str, Any] = {
                "model": config.model_name,
                "messages": self._messages_payload(messages),
                "stream": True,
                "think": False if disable_thinking else True,
                "options": {"num_predict": completion_budget},
            }
            if temperature is not None:
                payload["options"]["temperature"] = temperature
            payload_variants = [payload]
        else:
            payload = {
                "model": config.model_name,
                "messages": self._messages_payload(messages),
                "stream": True,
                "max_tokens": completion_budget,
            }
            if temperature is not None:
                payload["temperature"] = temperature
            if disable_thinking:
                payload = self._openai_no_reasoning_payload(payload)
            payload_variants = self._openai_compat_variants(payload)

        started = time.monotonic()
        emitted_parts: list[str] = []
        parsed_frames = 0
        malformed_frames = 0
        reasoning_characters = 0
        finish_reason = None
        usage: dict[str, int] = {}
        frame_keys: Counter[str] = Counter()
        used_payload = payload_variants[-1]
        last_error: Exception | None = None

        try:
            async with httpx.AsyncClient(
                trust_env=False,
                timeout=httpx.Timeout(240.0, connect=10.0),
            ) as client:
                for candidate_index, candidate in enumerate(payload_variants):
                    used_payload = candidate
                    try:
                        async for data in self._stream_once(
                            client, url, headers, candidate
                        ):
                            if data.get("_malformed"):
                                malformed_frames += 1
                                continue
                            parsed_frames += 1
                            frame_keys.update(str(key) for key in data)
                            provider_error = data.get("error")
                            if provider_error:
                                raise LLMProviderError(
                                    f"LLM provider error: {provider_error}"
                                )
                            finish_reason = (
                                self._extract_finish_reason(data) or finish_reason
                            )
                            frame_usage = self._extract_usage(data)
                            if frame_usage:
                                usage = frame_usage
                            reasoning_characters += self._reasoning_characters(data)
                            content = self._extract_content(data)
                            if content:
                                emitted_parts.append(content)
                                yield content
                        last_error = None
                        break
                    except LLMProviderError as exc:
                        last_error = exc
                        if (
                            "HTTP 400" in str(exc)
                            and candidate_index + 1 < len(payload_variants)
                            and not emitted_parts
                        ):
                            continue
                        raise
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
                "error": str(exc),
                "parsed_frames": parsed_frames,
                "malformed_frames": malformed_frames,
                "reasoning_characters": reasoning_characters,
                "response_characters": len(partial_text),
                "frame_keys": dict(frame_keys),
                "duration_ms": round((time.monotonic() - started) * 1000),
                "requested_max_tokens": completion_budget,
                "native_ollama": is_ollama,
            }
            raise

        if last_error is not None:
            raise LLMProviderError(str(last_error))

        output = "".join(emitted_parts)
        budget_exhausted = self._budget_exhausted(
            finish_reason, usage, completion_budget
        )
        incomplete = bool(output.strip()) and not self._looks_complete(output)
        reasoning_only = bool(reasoning_characters and not output.strip())
        truncated = budget_exhausted or incomplete or reasoning_only
        status = "truncated" if truncated else ("completed" if output.strip() else "empty")
        thinking_disabled = bool(
            used_payload.get("think") is False
            or used_payload.get("reasoning_effort") == "none"
            or (
                used_payload.get("chat_template_kwargs") or {}
            ).get("enable_thinking") is False
        )
        self.last_telemetry = {
            "model": config.model_name,
            "url": url,
            "status": status,
            "finish_reason": finish_reason,
            "usage": usage,
            "parsed_frames": parsed_frames,
            "malformed_frames": malformed_frames,
            "reasoning_characters": reasoning_characters,
            "response_characters": len(output),
            "frame_keys": dict(frame_keys),
            "thinking_disabled": thinking_disabled,
            "requested_max_tokens": completion_budget,
            "native_ollama": is_ollama,
            "duration_ms": round((time.monotonic() - started) * 1000),
        }

        if truncated:
            reason = "reasoning-only output" if reasoning_only else (
                "completion budget exhausted" if budget_exhausted else "unfinished response"
            )
            raise LLMProviderTruncatedError(
                f"LLM produced {reason} "
                f"(content_chars={len(output)}, reasoning_chars={reasoning_characters}, "
                f"finish_reason={finish_reason!r})",
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
        is_ollama = self._is_ollama(base_url)
        url = (
            self._ollama_native_url(base_url)
            if is_ollama
            else f"{base_url.rstrip('/')}/chat/completions"
        )
        headers = {"Content-Type": "application/json"}
        if api_key and not is_ollama:
            headers["Authorization"] = f"Bearer {api_key}"
        if is_ollama:
            payload: dict[str, Any] = {
                "model": model_name,
                "messages": [{"role": "user", "content": "ping"}],
                "stream": False,
                "think": False,
                "options": {"num_predict": 8},
            }
        else:
            payload = {
                "model": model_name,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 8,
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
                return isinstance(data, dict) and (
                    bool(self._extract_content(data))
                    or bool(data.get("choices"))
                    or data.get("done") is True
                )
        except Exception:
            return False
