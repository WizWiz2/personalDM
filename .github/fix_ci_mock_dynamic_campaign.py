from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected one match, found {count}: {old[:120]!r}")
    target.write_text(text.replace(old, new), encoding="utf-8")


path = "src/backend/tests/run_ci_mock_simulation_v3.py"

replace_once(
    path,
    '''def _player_decision(prompt: str) -> str:
''',
    '''def _scenario_arc(prompt: str) -> str:
    CALLS["scenario"] += 1
    match = re.search(r"НОМЕР АКТА:\\s*(\\d+)", prompt)
    arc_number = int(match.group(1)) if match else CALLS["scenario"]
    npc_names = [f"Проводник {arc_number}-{index}" for index in range(1, 5)]
    phases = []
    for index, npc_name in enumerate(npc_names, start=1):
        slug = f"mock_arc_{arc_number}_phase_{index}"
        phases.append(
            {
                "slug": slug,
                "title": f"Испытание {arc_number}.{index}",
                "location_description": (
                    f"Участок {arc_number}.{index} содержит заметный механизм, "
                    "два доступных пути и следы недавнего события."
                ),
                "mood": "сосредоточенность",
                "tension": "ограниченное время",
                "objective": (
                    f"Проверить свидетельство {arc_number}.{index} и зафиксировать "
                    "наблюдаемый результат перед переходом дальше."
                ),
                "introduced_npcs": [npc_name],
                "active_npcs": [npc_name],
                "opening_theses": [
                    {
                        "thesis_type": "intention",
                        "text": f"Нужно проверить свидетельство {arc_number}.{index}.",
                        "priority": 9,
                        "visibility": "public",
                        "related_names": [npc_name],
                    },
                    {
                        "thesis_type": "tension",
                        "text": "Промедление усложнит доступный путь.",
                        "priority": 7,
                        "visibility": "public",
                        "related_names": [],
                    },
                ],
                "pulses": [
                    {
                        "at_fraction": 0.3,
                        "event": f"Механизм {arc_number}.{index} начинает закрываться.",
                        "thesis": {
                            "thesis_type": "tension",
                            "text": "Безопасное окно быстро сокращается.",
                            "priority": 8,
                            "visibility": "public",
                            "related_names": [],
                        },
                    },
                    {
                        "at_fraction": 0.7,
                        "event": f"Появляется второе свидетельство {arc_number}.{index}.",
                        "thesis": {
                            "thesis_type": "unresolved_beat",
                            "text": "Нужно сопоставить два наблюдаемых свидетельства.",
                            "priority": 8,
                            "visibility": "public",
                            "related_names": [npc_name],
                        },
                    },
                ],
                "director_note": "Закрыть сцену только после двух подтверждённых фактов.",
                "completion_criteria": [
                    {
                        "key": "evidence_recorded",
                        "description": "Первое наблюдаемое свидетельство сохранено в каноне.",
                        "allowed_change_types": ["fact"],
                    },
                    {
                        "key": "result_recorded",
                        "description": "Итог проверки сохранён как устойчивый факт.",
                        "allowed_change_types": ["fact"],
                    },
                ],
                "min_turns": 4,
                "max_turns": 10,
            }
        )
    return json.dumps(
        {
            "arc_title": f"Mock-акт {arc_number}",
            "premise": (
                f"Акт {arc_number} проверяет продолжение кампании через четыре "
                "различимых сцены с новыми персонажами."
            ),
            "terminal": False,
            "npcs": [
                {
                    "name": name,
                    "concept": (
                        f"{name} хранит проверяемое свидетельство и скрывает личную "
                        "причину довести экспедицию до следующего участка."
                    ),
                    "campaign_role": "локальный проводник и источник свидетельства",
                    "tone": "конкретный, осторожный, профессиональный",
                }
                for name in npc_names
            ],
            "phases": phases,
        },
        ensure_ascii=False,
    )


def _player_decision(prompt: str) -> str:
''',
)

replace_once(
    path,
    '''def _objective(prompt: str) -> str:
    turn_match = re.search(r"ХОДОВ В СЦЕНЕ:\\s*(\\d+)", prompt)
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
''',
    '''def _objective(prompt: str) -> str:
    turn_match = re.search(r"ХОДОВ В СЦЕНЕ:\\s*(\\d+)", prompt)
    phase_turn = int(turn_match.group(1)) if turn_match else 0
    criteria_block = prompt.split("КРИТЕРИИ ЗАВЕРШЕНИЯ:\\n", 1)[-1].split(
        "УЖЕ ПОДТВЕРЖДЕНЫ:", 1
    )[0]
    criterion_keys = re.findall(r"^- ([a-z][a-z0-9_]*):", criteria_block, re.MULTILINE)
    durable_block = prompt.split(
        "ПРИНЯТЫЕ DURABLE CHANGES ЭТОЙ СЦЕНЫ:\\n", 1
    )[-1].split("АКТИВНЫЕ ТЕЗИСЫ:", 1)[0]
    evidence = next(
        (line.strip() for line in durable_block.splitlines() if line.strip().startswith("fact:")),
        "",
    )
    resolved = phase_turn >= 4 and bool(evidence) and bool(criterion_keys)
    criteria_met = criterion_keys if resolved else []
    return json.dumps(
        {
            "status": "resolved" if resolved else "progressing",
            "evidence": (
                evidence
                if resolved
                else "Цель продвигается, но формальные критерии ещё не подтверждены."
            ),
            "outcome_summary": (
                "Все формальные критерии сцены подтверждены каноном."
                if resolved
                else None
            ),
            "confirmed_pulses": [],
            "criteria_met": criteria_met,
            "criterion_evidence": {key: evidence for key in criteria_met},
        },
        ensure_ascii=False,
    )
''',
)

replace_once(
    path,
    '''def _dispatch_json(prompt: str) -> str:
    if "Создай различимую карточку NPC" in prompt:
''',
    '''def _dispatch_json(prompt: str) -> str:
    if "Ты проектируешь новый акт автономной кампании" in prompt:
        return _scenario_arc(prompt)
    if "Создай различимую карточку NPC" in prompt:
''',
)

replace_once(
    path,
    '''        for marker in (
            "Создай различимую карточку NPC",
''',
    '''        for marker in (
            "Ты проектируешь новый акт автономной кампании",
            "Создай различимую карточку NPC",
''',
)
