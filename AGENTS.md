# AGENTS.md — правила для любого агента в personalDM

Короткий дайджест. Детали: [docs/agents/engineering-rules.md](docs/agents/engineering-rules.md),
[docs/TESTING-STRATEGY.md](docs/TESTING-STRATEGY.md), [docs/playtest-protocol.md](docs/playtest-protocol.md),
[docs/cr-eliminate-lexical-semantic-hardcoding.md](docs/cr-eliminate-lexical-semantic-hardcoding.md), `docs/architecture/`, `docs/adr/`.
Баги в этом репо чинят несколько агентов параллельно: перед работой `git pull`, смотри открытые PR.

## Архитектура: жёсткие правила
- Никаких regex/словарей/списков слов, фраз, лемм как игровой логики. Смысл решает модель в типизированном поле.
- Чини структурно: схема, enum, `Literal`-выбор из кандидатов, ID, инвариант в БД, typed authority.
- Каждое изменение делает код тоньше. Новый список или обходной guard — это не фикс.
- Тождество только по ID, не по тексту/падежу/имени. Модель выбирает ID из каталога хода.
- Факты рождаются только из типизированных исходов. Проза — декорация, из неё ничего не пишется в канон.
- Что не запрещено — разрешено. Запретов ровно четыре:
  1. не выдумывать людей вне каста/интродукции;
  2. не переписывать то, что уже случилось;
  3. не писать действия и реплики игрока;
  4. не перемещать людей без реального перехода.
- Не урезай креативность мастеров. Мастера должны различаться (политика инициативы, приёмы, темп).

## Карта ядра (`src/backend/app/`)
- `services/turn_saga.py` — единственный владелец хода: plan → prepare → narrate → validate → publish/compensate.
- `services/turn_planner.py` — схемы плана (`ActionSequencePlan`, `SceneTransitionPlan`, `NarrationPolicy`).
- `services/turn_authority_planner.py` — LLM-планировщик + семантический ревью плана.
- `services/action_plan_compiler.py` — компилирует план в шаги движка, владеет топологией маршрута.
- `services/turn_authority_service.py` (+ `_resolvers.py`) — собирает `TurnAuthority`: актёры, интродукции NPC.
- `services/turn_authority_validator.py` — проверяет прозу против authority (`four_bans_only`), ремонт.
- `services/scene_transition_executor.py` — prepare/finalize/compensate переходов между сценами.
- `services/scene_lifecycle.py` — атомарный указатель активной сцены кампании.
- `db/repositories/fact_repo.py` — текущие факты и их замена (supersede).
- `services/name_identity_contract.py` — общий контракт имён для интродукций и реестра.
- `services/world_initiative_ledger.py` — запись принятого хода мира в канон с provenance.

## Git
- Один фикс = маленький коммит = PR → сразу merge в `main` merge-коммитом.
- Без force-push, `reset --hard` по общим веткам и переписывания истории — только с согласия владельца.
- Лучше откат (revert), чем долгая ветка.

## Тесты
- Запуск: `cd src/backend && PDM_TEXT_PROVIDER=local python -m pytest`.
- Сначала красный тест: без фикса он обязан падать.
- Assert на типизированный исход и состояние БД. Запрещено: подстроки промпта, текст исходников,
  только вызовы моков, тесты без assert.
- Тест не закрепляет известный баг. Нашёл такой — переписываешь его в PR фикса.
- Тесты не зависят от cwd и порядка импорта; никаких `PROMPT +=` при импорте модуля.
- Если важен commit — проверяй второй сессией. SQLite-конкуренцию — реальными двумя движками, не моком.
- Ядро держит mutation score; правки не должны его опускать. Как гонять — в engineering-rules.

## Плейтест
- Только вручную, ход за ходом: прочитал ответ → написал следующий. Без скриптов, батчей, параллели.
- Отдельный data dir и своя копия ChatGPT-профиля. Основной сервер и профиль не трогать.
- Soak: сервер отвязан от сессии агента, watchdog `/health`, снимок БД каждые 50 ходов,
  `scripts/soak_metrics.py`.
- Отчёт: s/ход, $/ход, тренд input-токенов, падения/ремонты, первая неправильная граница.
