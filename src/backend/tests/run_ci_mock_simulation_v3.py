"""Run the v3 campaign pipeline with a deterministic in-process provider."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections import Counter

from app.providers.llm_provider import LLMProvider

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
    return json.dumps(
        {"target": target, "mode": mode, "intent": intent},
        ensure_ascii=False,
    )


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


async def deterministic_generate_stream(
    self,
    messages,
    config,
    api_key=None,
    **kwargs,
):
    prompt = _all_text(messages)
    if "Создай различимую карточку NPC" in prompt:
        CALLS["builder"] += 1
        output = _json_character(prompt)
    elif "Ты имитируешь живого игрока" in prompt:
        output = _player_decision(prompt)
    elif "Ты проверяешь фактическое состояние цели сцены" in prompt:
        CALLS["evaluator"] += 1
        output = _objective(prompt)
    elif "Ты куратор живых тезисов сцены" in prompt:
        output = _curator(prompt)
    elif "Ты Memory Scribe" in prompt:
        CALLS["scribe"] += 1
        output = json.dumps({"proposals": []}, ensure_ascii=False)
    else:
        CALLS["dm"] += 1
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
    try:
        from .run_realistic_simulation_v3 import run_realistic_simulation_v3
    except ImportError:
        from run_realistic_simulation_v3 import run_realistic_simulation_v3
    await run_realistic_simulation_v3()
    print("MOCK_CALLS", json.dumps(CALLS, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
