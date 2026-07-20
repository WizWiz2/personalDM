"""Run the campaign pipeline with a deterministic in-process provider."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections import Counter
from pathlib import Path

from app.providers.llm_provider import LLMProvider, LLMProviderError

CALLS: Counter[str] = Counter()


def _all_text(messages) -> str:
    return "\n\n".join(str(message.content) for message in messages)


def _json_character(prompt: str) -> str:
    name_match = re.search(r"Имя:\s*([^\n]+)", prompt)
    role_match = re.search(r"Роль:\s*([^\n]+)", prompt)
    tone_match = re.search(r"Тон:\s*([^\n]+)", prompt)
    name = name_match.group(1).strip() if name_match else "Неизвестный"
    role = role_match.group(1).strip() if role_match else "специалист"
    tone = tone_match.group(1).strip() if tone_match else "наблюдательный"
    return json.dumps(
        {
            "canonical_name": name,
            "description": f"{name} выполняет роль: {role}.",
            "appearance": f"У {name} практичная одежда и уникальный знак профессии.",
            "face_description": f"Лицо {name} выражает черту: {tone}.",
            "body_description": "Телосложение соответствует привычной работе.",
            "immutable_features": f"Узнаваемая деталь {name}.",
            "personality": tone,
            "values": ["компетентность", "личная свобода"],
            "fears": ["потерять доверие группы"],
            "desires": ["довести экспедицию до цели"],
            "voice": f"Отличимый голос {name}, связанный с ролью {role}.",
            "speech_patterns": f"{name} использует профессиональную лексику и отвечает конкретно.",
            "biography": f"{name} пришёл в экспедицию как {role}.",
            "backstory_public": f"Группа знает {name} как {role}.",
            "secrets": [f"{name} скрывает личную причину участия."],
            "emotional_state": "собранность",
            "current_intentions": ["проявить компетентность", "не раскрывать секрет без причины"],
            "goals": ["помочь текущей сцене", "разрешить личный конфликт"],
            "capabilities": [f"применять знания по роли {role}"],
            "limitations": ["не использует способности вне своей роли"],
            "equipment": [f"личный набор {name}", f"инструмент профессии {name}"],
            "initial_beliefs": ["Цитадель опаснее публичных описаний."],
            "visual_profile": {"mock": True, "name": name},
        },
        ensure_ascii=False,
    )


def _player_decision(prompt: str) -> str:
    CALLS["player"] += 1
    active_match = re.search(r"АКТИВНЫЕ NPC:\s*([^\n]+)", prompt)
    active = [
        item.strip()
        for item in (active_match.group(1).split(",") if active_match else [])
        if item.strip()
    ]
    number = CALLS["player"]
    if active and number % 2:
        target = active[(number - 1) % len(active)]
        mode = "question"
        intent = (
            f"Я спрашиваю {target}, какой конкретный признак номер {number} поможет "
            "проверить текущую гипотезу, не объявляя результат заранее."
        )
    else:
        target = "narrator"
        mode = "action"
        intent = (
            f"Я использую фонарь и обычные навыки руиниста, чтобы на ходе {number} "
            "осмотреть конкретные следы и крепления, не касаясь магических элементов."
        )
    return json.dumps({"target": target, "mode": mode, "intent": intent}, ensure_ascii=False)


def _objective(prompt: str) -> str:
    turn_match = re.search(r"ХОДОВ В СЦЕНЕ:\s*(\d+)", prompt)
    phase_turn = int(turn_match.group(1)) if turn_match else 0
    status = "resolved" if phase_turn >= 4 else "progressing"
    return json.dumps(
        {
            "status": status,
            "evidence": (
                "Группа совершила подтверждённое действие, закрывающее цель сцены."
                if status == "resolved"
                else "Цель продвигается, но итог ещё не подтверждён."
            ),
            "outcome_summary": (
                "Цель сцены достигнута с явным последствием."
                if status == "resolved"
                else None
            ),
            "confirmed_pulses": [0] if phase_turn >= 2 else [],
        },
        ensure_ascii=False,
    )


def _curator(prompt: str) -> str:
    CALLS["curator"] += 1
    revision = CALLS["curator"]
    return json.dumps(
        {
            "desired_active": [
                {
                    "thesis_type": "tension",
                    "text": f"Текущее осложнение сцены подтверждено на ревизии {revision}.",
                    "priority": 7,
                    "visibility": "dm",
                    "related_entity_ids": [],
                    "existing_thesis_id": None,
                    "semantic_key": "current-complication",
                },
                {
                    "thesis_type": "unresolved_beat",
                    "text": "Группа должна получить конкретный результат текущей цели.",
                    "priority": 8,
                    "visibility": "public",
                    "related_entity_ids": [],
                    "existing_thesis_id": None,
                    "semantic_key": "objective-result",
                },
            ]
        },
        ensure_ascii=False,
    )


def _scribe(prompt: str) -> str:
    CALLS["scribe"] += 1
    number = CALLS["scribe"]
    actor_match = re.search(r"КТО ГОВОРИЛ В ОТВЕТЕ:\s*([^\n]+)", prompt)
    player_match = re.search(r"ПЕРСОНАЖ ИГРОКА:\s*([^\n]+)", prompt)
    actor = actor_match.group(1).strip() if actor_match else "narrator"
    player = player_match.group(1).strip() if player_match else "Eldon"
    authoritative = prompt.split("АВТОРИТЕТНЫЙ РЕЗУЛЬТАТ ДМА:\n")[-1].strip()
    evidence = authoritative[:600]
    if actor.casefold() != "narrator":
        outcome = {
            "id": "o1",
            "kind": "knowledge_transfer",
            "description": f"{player} услышал подтверждённое заявление {actor}.",
            "evidence": evidence,
            "authority": "character_claim",
            "durable": True,
        }
        proposal = {
            "outcome_id": "o1",
            "change_type": "knowledge",
            "operation": "assert",
            "cardinality": "multi",
            "payload": {
                "recipient_id": player,
                "fact_id": None,
                "proposition": f"{actor} сообщил проверяемое наблюдение номер {number}.",
                "source_character_id": actor,
                "confidence": 0.9,
            },
        }
    else:
        outcome = {
            "id": "o1",
            "kind": "world_state",
            "description": "Экспедиция получила новое наблюдаемое свидетельство.",
            "evidence": evidence,
            "authority": "dm_confirmed",
            "durable": True,
        }
        proposal = {
            "outcome_id": "o1",
            "change_type": "fact",
            "operation": "assert",
            "cardinality": "multi",
            "payload": {
                "subject": "экспедиция",
                "predicate": "получила_свидетельство",
                "object_value": f"наблюдение номер {number}",
            },
        }
    return json.dumps({"outcomes": [outcome], "proposals": [proposal]}, ensure_ascii=False)


def _dispatch_json(prompt: str) -> str:
    if "Создай различимую карточку NPC" in prompt:
        CALLS["builder"] += 1
        return _json_character(prompt)
    if "Ты имитируешь живого игрока" in prompt:
        return _player_decision(prompt)
    if "Ты проверяешь фактическое состояние цели сцены" in prompt:
        CALLS["evaluator"] += 1
        return _objective(prompt)
    if "Ты куратор живых тезисов сцены" in prompt:
        return _curator(prompt)
    if "Ты Memory Scribe" in prompt:
        return _scribe(prompt)
    return "{}"


def _consume_dm_failure_budget() -> bool:
    requested = max(0, int(os.getenv("PDM_MOCK_FAIL_DM_ATTEMPTS", "0")))
    if requested <= 0:
        return False
    data_dir = Path(os.getenv("PDM_SIM_DATA_DIR", "./data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    budget_path = data_dir / "mock_dm_failure_budget.txt"
    if not budget_path.exists():
        budget_path.write_text(str(requested), encoding="utf-8")
    remaining = int(budget_path.read_text(encoding="utf-8").strip() or "0")
    if remaining <= 0:
        return False
    budget_path.write_text(str(remaining - 1), encoding="utf-8")
    return True


async def deterministic_generate_json(self, messages, config, api_key=None, **kwargs):
    output = _dispatch_json(_all_text(messages))
    self.last_telemetry = {
        "status": "completed",
        "finish_reason": "stop",
        "response_characters": len(output),
        "reasoning_characters": 0,
        "usage": {"completion_tokens": max(1, len(output) // 4)},
        "mock": True,
        "control_plane": True,
    }
    return json.loads(output)


async def deterministic_generate_stream(self, messages, config, api_key=None, **kwargs):
    prompt = _all_text(messages)
    if any(
        marker in prompt
        for marker in (
            "Создай различимую карточку NPC",
            "Ты имитируешь живого игрока",
            "Ты проверяешь фактическое состояние цели сцены",
            "Ты куратор живых тезисов сцены",
            "Ты Memory Scribe",
        )
    ):
        output = _dispatch_json(prompt)
    else:
        CALLS["dm"] += 1
        if _consume_dm_failure_budget():
            self.last_telemetry = {
                "status": "transport_error",
                "error": "planned mock provider failure",
                "mock": True,
            }
            raise LLMProviderError("planned mock provider failure")
        output = (
            f"ДМ подтверждает конкретное последствие хода {CALLS['dm']}: группа получает "
            "новое наблюдаемое свидетельство, меняет подход и на шаг приближается к цели сцены."
        )
    self.last_telemetry = {
        "status": "completed",
        "finish_reason": "stop",
        "response_characters": len(output),
        "reasoning_characters": 0,
        "usage": {"completion_tokens": max(1, len(output) // 4)},
        "mock": True,
    }
    yield output


async def main() -> None:
    LLMProvider.generate_stream = deterministic_generate_stream
    LLMProvider.generate_json = deterministic_generate_json
    try:
        from .run_realistic_simulation import run_realistic_simulation
    except ImportError:
        from run_realistic_simulation import run_realistic_simulation
    await run_realistic_simulation()
    print("MOCK_CALLS", json.dumps(CALLS, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
