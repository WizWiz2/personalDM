from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from pydantic import ValidationError

from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.context_compiler import ContextCompiler, count_tokens
from app.services.memory_scribe import MemoryScribe
from app.services.thesis_curator import ThesisCurator


class BenchmarkControlError(RuntimeError):
    """Raised when a quality benchmark loses one of its control agents."""


@dataclass(frozen=True)
class ControlJSONResult:
    data: dict[str, Any]
    attempt: int
    transport: str


CONTROL_STATS: Counter[str] = Counter()
CONTROL_FAILURES: list[str] = []
_SCRIBE_EMPTY_STREAK = 0
_INSTALLED = False


def benchmark_mode() -> str:
    value = os.getenv("PDM_SIM_MODE", "quality").strip().casefold()
    return value if value in {"quality", "smoke"} else "quality"


def quality_mode() -> bool:
    return benchmark_mode() == "quality"


def _data_dir() -> Path:
    return Path(os.getenv("PDM_SIM_DATA_DIR", "./data"))


def _write_health() -> None:
    path = _data_dir() / "realistic_simulation_health.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "mode": benchmark_mode(),
        "control_stats": dict(CONTROL_STATS),
        "control_failures": list(CONTROL_FAILURES),
        "valid": not CONTROL_FAILURES,
    }
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def health_snapshot() -> dict[str, Any]:
    return {
        "mode": benchmark_mode(),
        "control_stats": dict(CONTROL_STATS),
        "control_failures": list(CONTROL_FAILURES),
        "valid": not CONTROL_FAILURES,
    }


def record_control_failure(label: str, error: Exception | str) -> None:
    message = f"{label}: {error}"
    CONTROL_STATS[f"{label}_failures"] += 1
    if message not in CONTROL_FAILURES:
        CONTROL_FAILURES.append(message)
    _write_health()


def assert_control_health() -> None:
    if quality_mode() and CONTROL_FAILURES:
        raise BenchmarkControlError(CONTROL_FAILURES[-1])


def _balanced_json_object(text: str) -> dict[str, Any]:
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
        raise ValueError("response does not contain a JSON object")
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
                value = json.loads(clean[start : index + 1])
                if not isinstance(value, dict):
                    raise ValueError("response JSON is not an object")
                return value
    raise ValueError("response contains an incomplete JSON object")


def _uses_mock_stream(provider: LLMProvider) -> bool:
    method = getattr(provider.generate_stream, "__func__", provider.generate_stream)
    module = getattr(method, "__module__", "")
    return bool(getattr(provider, "_simulation_mock", False)) or module != "app.providers.llm_provider"


async def _mock_stream_json(
    provider: LLMProvider,
    messages: list[ChatMessage],
    config,
    api_key: str | None,
    max_tokens: int,
    temperature: float,
) -> tuple[dict[str, Any], str]:
    raw = ""
    async for token in provider.generate_stream(
        messages,
        config,
        api_key,
        max_tokens=max_tokens,
        temperature=temperature,
    ):
        raw += token
    return _balanced_json_object(raw), "mock_stream"


async def _http_json(
    provider: LLMProvider,
    messages: list[ChatMessage],
    config,
    api_key: str | None,
    max_tokens: int,
    temperature: float,
) -> tuple[dict[str, Any], str]:
    url = f"{config.base_url.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload: dict[str, Any] = {
        "model": config.model_name,
        "messages": [
            {"role": message.role, "content": message.content}
            for message in messages
        ],
        "stream": False,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }
    if provider._is_ollama(config.base_url):
        payload["think"] = False

    started = time.monotonic()
    async with httpx.AsyncClient(
        trust_env=False,
        timeout=httpx.Timeout(180.0, connect=10.0),
    ) as client:
        response = await client.post(url, headers=headers, json=payload)
        if response.status_code == 400:
            payload.pop("response_format", None)
            response = await client.post(url, headers=headers, json=payload)
        if response.status_code != 200:
            raise LLMProviderError(
                f"control request returned HTTP {response.status_code}: {response.text[:1500]}"
            )
        data = response.json()

    content = provider._extract_content(data)
    if not content and isinstance(data, dict) and isinstance(data.get("response"), dict):
        return data["response"], "http_json"
    if not content:
        raise LLMProviderError(
            "control request completed without JSON content "
            f"(finish_reason={provider._extract_finish_reason(data)!r})"
        )
    provider.last_telemetry = {
        "status": "completed",
        "control_plane": True,
        "transport": "http_json",
        "finish_reason": provider._extract_finish_reason(data),
        "usage": provider._extract_usage(data),
        "response_characters": len(content),
        "duration_ms": round((time.monotonic() - started) * 1000),
        "thinking_disabled": bool(payload.get("think") is False),
    }
    return _balanced_json_object(content), "http_json"


async def generate_control_json(
    provider: LLMProvider,
    messages: list[ChatMessage],
    config,
    api_key: str | None,
    *,
    label: str,
    max_tokens: int,
    temperature: float,
) -> ControlJSONResult | None:
    CONTROL_STATS[f"{label}_calls"] += 1
    last_error: Exception | None = None
    base_messages = list(messages)
    for attempt in range(1, 3):
        request_messages = list(base_messages)
        if attempt == 2:
            request_messages.append(
                ChatMessage(
                    role="user",
                    content=(
                        "Предыдущий ответ не был корректным JSON. Верни только один валидный JSON-объект "
                        "строго по указанной схеме, без markdown и пояснений."
                    ),
                )
            )
        try:
            if _uses_mock_stream(provider):
                data, transport = await _mock_stream_json(
                    provider,
                    request_messages,
                    config,
                    api_key,
                    max_tokens,
                    temperature if attempt == 1 else 0.0,
                )
            else:
                data, transport = await _http_json(
                    provider,
                    request_messages,
                    config,
                    api_key,
                    max_tokens,
                    temperature if attempt == 1 else 0.0,
                )
            CONTROL_STATS[f"{label}_success"] += 1
            if attempt > 1:
                CONTROL_STATS[f"{label}_repair_success"] += 1
            _write_health()
            return ControlJSONResult(data=data, attempt=attempt, transport=transport)
        except Exception as exc:
            last_error = exc

    record_control_failure(label, last_error or "unknown JSON control error")
    if quality_mode():
        raise BenchmarkControlError(f"{label} unavailable: {last_error}")
    return None


def ensure_current_user_message(
    messages: list[ChatMessage],
    metadata: dict,
    current_user_content: str,
) -> tuple[list[ChatMessage], dict]:
    if any(
        message.role == "user" and message.content == current_user_content
        for message in messages
    ):
        metadata["current_user_reserved"] = True
        return messages, metadata

    maximum = int(metadata.get("token_budget_max") or 0)
    user_tokens = count_tokens(current_user_content)
    result = list(messages)
    used = sum(count_tokens(message.content) for message in result)
    removed = 0
    while len(result) > 1 and maximum and used + user_tokens >= maximum:
        candidate = result[1]
        used -= count_tokens(candidate.content)
        result.pop(1)
        removed += 1
    result.append(ChatMessage(role="user", content=current_user_content))
    metadata = dict(metadata)
    metadata["current_user_reserved"] = True
    metadata["history_messages_removed_for_current_user"] = removed
    metadata["token_budget_used"] = used + user_tokens
    layers = list(metadata.get("included_layers") or [])
    if "layer_6_current_user" not in layers:
        layers.append("layer_6_current_user")
    metadata["included_layers"] = layers
    return result, metadata


def install_quality_controls(runtime) -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    original_compile_context = ContextCompiler.compile_context
    original_scribe = MemoryScribe.extract_proposals
    original_curate = ThesisCurator.curate_after_turn
    base_policy = runtime.PlayerPolicy

    async def reserved_compile_context(self, *args, **kwargs):
        messages, metadata = await original_compile_context(self, *args, **kwargs)
        acting_character_id = kwargs.get("acting_character_id")
        current_user_content = kwargs.get("current_user_content")
        if acting_character_id and current_user_content:
            return ensure_current_user_message(messages, metadata, current_user_content)
        return messages, metadata

    ContextCompiler.compile_context = reserved_compile_context

    class SafePlayerPolicy(base_policy):
        def fallback(
            self,
            active_npcs,
            mode,
            objective,
            latest_result,
            active_theses,
            turn_number,
        ):
            # DM-only theses are deliberately discarded. Smoke fallback may use only the
            # public objective and the latest authoritative result visible to Eldon.
            return super().fallback(
                active_npcs,
                mode,
                objective,
                latest_result,
                [],
                turn_number,
            )

    runtime.PlayerPolicy = SafePlayerPolicy

    async def strict_player(
        provider,
        config,
        api_key,
        compiler,
        campaign_id,
        phase_runtime,
        player_id,
        history,
        policy,
        turn_number,
        active_theses,
    ):
        assert_control_health()
        active_npcs = list(phase_runtime.phase.active_npcs)
        preferred = policy.preferred_mode(turn_number)
        suggested = policy.suggested_target(active_npcs, preferred)
        context_messages, _ = await compiler.compile_context(
            campaign_id=campaign_id,
            acting_character_id=player_id,
            scene_id=phase_runtime.scene_id,
        )
        trusted_context = "\n\n".join(message.content for message in context_messages)
        recent = "\n".join(
            f"{'ДМ' if turn.role == 'assistant' else 'ИГРОК'}: {turn.content}"
            for turn in history[-10:]
        )
        system = f"""Ты имитируешь живого игрока настольной RPG, а не второго ДМа.
Верни один JSON: {{"target":"narrator|ActiveNpc","mode":"action|dialogue|question|plan|decision","intent":"1-3 предложения"}}.

ЦЕЛЬ СЦЕНЫ: {phase_runtime.phase.objective}
АКТИВНЫЕ NPC: {', '.join(active_npcs)}
ПРЕДПОЧТИТЕЛЬНЫЙ ТИП ХОДА: {preferred}
НЕДОИСПОЛЬЗОВАННЫЙ NPC: {suggested}

Правила:
- Только русский язык.
- Опиши только намерение Элдона, не результат.
- Реагируй на последний результат ДМа.
- Используй только знания, предметы и способности из доверенного контекста.
- Не используй режиссёрские или DM-only тезисы: их здесь намеренно нет.
- Не повторяй недавние действия.

ДОВЕРЕННЫЙ КОНТЕКСТ ЭЛДОНА:
{trusted_context}"""
        user = (
            f"НЕДАВНЯЯ ИГРА:\n{recent or '(начало сцены)'}\n\n"
            "НЕДАВНИЕ ДЕЙСТВИЯ, КОТОРЫЕ НЕЛЬЗЯ ПОВТОРЯТЬ:\n"
            + "\n".join(policy.recent_fingerprints)
        )
        error = None
        for semantic_attempt in range(2):
            prompt = system + (f"\nПредыдущий вариант отклонён: {error}." if error else "")
            result = await generate_control_json(
                provider,
                [
                    ChatMessage(role="system", content=prompt),
                    ChatMessage(role="user", content=user),
                ],
                config,
                api_key,
                label="player",
                max_tokens=420,
                temperature=0.7 if semantic_attempt == 0 else 0.2,
            )
            if result is None:
                break
            try:
                decision = runtime.parse_player_decision(
                    json.dumps(result.data, ensure_ascii=False),
                    active_npcs,
                )
                valid, error = policy.validate(decision, active_npcs)
                if valid:
                    policy.remember(decision)
                    return decision
            except Exception as exc:
                error = str(exc)
        if quality_mode():
            record_control_failure("player_semantics", error or "invalid player decision")
            raise BenchmarkControlError(f"player decision invalid: {error}")
        latest_result = next(
            (turn.content for turn in reversed(history) if turn.role == "assistant"),
            "",
        )
        decision = policy.fallback(
            active_npcs,
            preferred,
            phase_runtime.phase.objective,
            latest_result,
            [],
            turn_number,
        )
        policy.remember(decision)
        return decision

    async def strict_evaluator(
        provider,
        config,
        api_key,
        phase_runtime,
        recent_history,
        assistant_content,
        active_theses,
        minimum_turns,
    ):
        assert_control_health()
        if phase_runtime.phase_turn < minimum_turns:
            return runtime.ObjectiveEvaluation(
                status="progressing",
                evidence=(
                    f"Минимальная длина сцены ещё не достигнута: "
                    f"{phase_runtime.phase_turn}/{minimum_turns}"
                ),
            )
        pending_indexes = sorted(
            phase_runtime.injected_pulses - phase_runtime.confirmed_pulses
        )
        pending = [
            f"{index}: {phase_runtime.phase.pulses[index].event}"
            for index in pending_indexes
        ]
        recent = "\n".join(
            f"{'ДМ' if turn.role == 'assistant' else 'ИГРОК'}: {turn.content}"
            for turn in recent_history[-8:]
        )
        prompt = f"""Ты проверяешь фактическое состояние цели сцены RPG.
Верни только JSON:
{{"status":"progressing|resolved|failed|blocked","evidence":"короткий факт","outcome_summary":"итог или null","confirmed_pulses":[индексы]}}

ЦЕЛЬ: {phase_runtime.phase.objective}
ХОДОВ В СЦЕНЕ: {phase_runtime.phase_turn}
АКТИВНЫЕ ТЕЗИСЫ: {' | '.join(active_theses)}
ОЖИДАЮЩИЕ ОСЛОЖНЕНИЯ:
{chr(10).join(pending) or '- нет'}

НЕДАВНЯЯ ИГРА:
{recent}

ПОСЛЕДНИЙ РЕЗУЛЬТАТ ДМА:
{assistant_content}

Resolved только если цель действительно достигнута. Не считай план или тезис событием."""
        result = await generate_control_json(
            provider,
            [ChatMessage(role="system", content=prompt)],
            config,
            api_key,
            label="evaluator",
            max_tokens=360,
            temperature=0.0,
        )
        if result is None:
            return runtime.ObjectiveEvaluation(
                status="blocked",
                evidence="Smoke evaluator unavailable; phase remains open.",
            )
        try:
            evaluation = runtime.ObjectiveEvaluation.model_validate(result.data)
        except ValidationError as exc:
            record_control_failure("evaluator_schema", exc)
            if quality_mode():
                raise BenchmarkControlError(f"evaluator schema invalid: {exc}") from exc
            return runtime.ObjectiveEvaluation(status="blocked", evidence=str(exc))
        evaluation.confirmed_pulses = [
            index
            for index in evaluation.confirmed_pulses
            if index in pending_indexes
        ]
        if evaluation.status in {"resolved", "failed"} and not evaluation.evidence.strip():
            record_control_failure("evaluator_evidence", "terminal status without evidence")
            if quality_mode():
                raise BenchmarkControlError("evaluator returned terminal status without evidence")
            evaluation.status = "blocked"
        return evaluation

    async def strict_builder(provider, config, api_key, seed, location_id):
        assert_control_health()
        prompt = f"""Создай различимую карточку NPC для долгой русскоязычной кампании.
Верни только JSON с ключами CharacterDraft: canonical_name, description, appearance,
face_description, body_description, immutable_features, personality, values, fears,
desires, voice, speech_patterns, biography, backstory_public, secrets,
emotional_state, current_intentions, goals, capabilities, limitations, equipment,
initial_beliefs, visual_profile.

Имя: {seed.name}
Концепция: {seed.concept}
Роль: {seed.campaign_role}
Тон: {seed.tone}

Все поля на русском. Списки содержат 1-4 элемента. Equipment содержит уникальные экземпляры с именем владельца."""
        error = None
        for attempt in range(2):
            messages = [ChatMessage(role="system", content=prompt)]
            if error:
                messages.append(
                    ChatMessage(
                        role="user",
                        content=f"Исправь карточку по ошибке валидации: {error}",
                    )
                )
            result = await generate_control_json(
                provider,
                messages,
                config,
                api_key,
                label="builder",
                max_tokens=1600,
                temperature=0.35 if attempt == 0 else 0.0,
            )
            if result is None:
                break
            try:
                payload = dict(result.data)
                payload["current_location_id"] = location_id
                card = runtime.CharacterDraft.model_validate(payload)
                return card, "model" if attempt == 0 else "repair"
            except ValidationError as exc:
                error = str(exc)
        if quality_mode():
            record_control_failure("builder_schema", error or "invalid CharacterDraft")
            raise BenchmarkControlError(f"Character Builder invalid: {error}")
        return runtime.deterministic_fallback_card(seed, location_id), "fallback"

    async def strict_scribe(self, *args, **kwargs):
        global _SCRIBE_EMPTY_STREAK
        original_generate_stream = self._llm_provider.generate_stream

        async def json_stream(messages, config, api_key=None, **generation_kwargs):
            result = await generate_control_json(
                self._llm_provider,
                messages,
                config,
                api_key,
                label="scribe",
                max_tokens=int(generation_kwargs.get("max_tokens") or 900),
                temperature=float(generation_kwargs.get("temperature") or 0.0),
            )
            payload = result.data if result is not None else {"proposals": []}
            yield json.dumps(payload, ensure_ascii=False)

        self._llm_provider.generate_stream = json_stream
        try:
            proposals = await original_scribe(self, *args, **kwargs)
        finally:
            self._llm_provider.generate_stream = original_generate_stream

        assistant_content = str(kwargs.get("assistant_content") or "")
        if len(assistant_content.strip()) >= 180:
            if proposals:
                _SCRIBE_EMPTY_STREAK = 0
            else:
                _SCRIBE_EMPTY_STREAK += 1
            limit = max(4, int(os.getenv("PDM_SIM_MAX_EMPTY_SCRIBE_TURNS", "12")))
            if quality_mode() and _SCRIBE_EMPTY_STREAK >= limit:
                record_control_failure(
                    "scribe_health",
                    f"no canon candidates for {_SCRIBE_EMPTY_STREAK} meaningful turns",
                )
                raise BenchmarkControlError(CONTROL_FAILURES[-1])
        return proposals

    async def strict_curator(self, *args, **kwargs):
        original_generate_stream = self._llm_provider.generate_stream

        async def json_stream(messages, config, api_key=None, **generation_kwargs):
            result = await generate_control_json(
                self._llm_provider,
                messages,
                config,
                api_key,
                label="curator",
                max_tokens=int(generation_kwargs.get("max_tokens") or 900),
                temperature=float(generation_kwargs.get("temperature") or 0.0),
            )
            payload = result.data if result is not None else {"desired_active": []}
            yield json.dumps(payload, ensure_ascii=False)

        self._llm_provider.generate_stream = json_stream
        try:
            return await original_curate(self, *args, **kwargs)
        except BenchmarkControlError:
            raise
        finally:
            self._llm_provider.generate_stream = original_generate_stream

    runtime.generate_player_decision = strict_player
    runtime.evaluate_objective = strict_evaluator
    runtime.build_character_card = strict_builder
    MemoryScribe.extract_proposals = strict_scribe
    ThesisCurator.curate_after_turn = strict_curator
    _write_health()
