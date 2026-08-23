# personalDM

Локальный AI-мастер для продолжительных systemless narrative RPG-кампаний.

Ядро продукта — **Campaign Truth Engine**. LLM пишет прозу, но не является источником истины: структурный runtime хранит сцену, положение персонажей, канон, знания NPC, причинность хода и происхождение памяти отдельно от художественного текста.

## Что уже работает

- разговорная Session Zero вместо анкеты;
- автоматический handoff Session Zero → первая активная сцена;
- большой system-owned opening post без пользовательского «Начинаем»;
- typed `TurnAuthority` между Planner, deterministic executors, Narrator и Validator;
- fail-closed spatial/agency boundaries: Narrator не может сам перемещать героя, телепортировать NPC или материализовать неразрешённого персонажа;
- actor-scoped контекст для NPC, чтобы не передавать им недоступные знания;
- durable журнал ходов, undo, generation runs и фоновые post-turn jobs;
- Campaign Debugger, causal playtest trace и memory operations;
- локальная генерация портретов, обложки кампании и сцен через ComfyUI;
- React/Vite GUI и CLI поверх одного `GameApplication` и одного runtime pipeline.

Текущий продукт сознательно **не является CRPG rules engine**: характеристики, HP, уровни, броски и конкретные настольные системы не считаются частью core, пока отдельный rules layer не появится осознанно.

## Документация

1. [`docs/product-foundation.md`](docs/product-foundation.md) — продуктовые принципы и границы.
2. [`docs/MVP-SPEC.md`](docs/MVP-SPEC.md) — исходный MVP-контракт; исторические детали могут быть superseded принятыми ADR и текущим runtime.
3. [`docs/runtime-transparency.md`](docs/runtime-transparency.md) — **как реально проходит ход сейчас, кто владеет каждым решением и где лежит доказательство**.
4. [`docs/model-role-routing.md`](docs/model-role-routing.md) — какие модели обслуживают Narrator/control-plane роли.
5. [`docs/architecture/interagent-turn-authority.md`](docs/architecture/interagent-turn-authority.md) — typed authority contract.
6. [`docs/architecture/narration-pipeline.md`](docs/architecture/narration-pipeline.md) — текущий Narrator → Validator → repair/fallback pipeline.
7. [`docs/README.md`](docs/README.md) — карта документов и их приоритет.
8. [`docs/adr/`](docs/adr/) — принятые и предлагаемые архитектурные решения.

Если документация и runtime расходятся, для диагностики текущей сборки приоритет имеют `runtime_manifest()`, persisted turn evidence и код `main`. Смысл этого правила — не скрывать дрейф документации за красивой схемой.

## Модели по умолчанию

Локальная конфигурация разделяет художественную и control-plane работу:

- **Gemma `gemma4:e4b`** — Narrator, Session Zero, `/DM` / game-master dialogue и Character Builder без отдельного override;
- **Qwen `qwen2.5:7b`** — Planner, Narration Validator, Entity Registrar, Memory Scribe, Curator, Evaluator, Scenario Builder и structured repair.

Control-plane роли не должны молча переключаться на Narrator при schema/provider failure: лучше получить диагностируемый conservative fallback, чем изменить семантику хода другой моделью.

Подробности: [`docs/model-role-routing.md`](docs/model-role-routing.md).

## Запуск

На Windows рекомендуемый вход — один файл:

```bat
play.bat
```

Launcher готовит Python-окружение, локальные LLM и при включённой графике ComfyUI assets, затем предлагает режим запуска:

- **GUI** — FastAPI + React/Vite, пользовательский основной путь;
- **CLI** — терминальный клиент поверх того же application/runtime слоя;
- **Выход**.

Для GUI нужен Node.js с npm. При первом запуске launcher выполняет установку frontend dependencies.

Ручной запуск:

```bash
# backend
cd src/backend
python -m uvicorn app.main:app --port 8000

# frontend
cd src/frontend
npm install
npm run dev
```

Vite работает на `http://localhost:5173` и проксирует `/api` и `/health` на `http://127.0.0.1:8000`.

## Session Zero → игра

GUI и CLI используют один `SessionZeroInterviewService`. Агент скрыто поддерживает структурированный draft мира и героя, но разговор остаётся свободным.

Когда информации достаточно:

1. Session Zero выдаёт terminal message **без нового вопроса**;
2. backend материализует героя, стартовую Location и Scene;
3. Narrator автоматически пишет первый большой opening post;
4. opening сохраняется как обычный `assistant` turn стартовой Scene;
5. игровой экран открывается уже с начавшейся сценой — писать «Начинаем» не требуется.

Повторный finalize idempotent и не должен создавать второй opening.

## Narrator и authority

Обычный narrative turn проходит через:

```text
user input
→ ContextCompiler
→ TurnAuthorityPlanner
→ deterministic structured execution
→ TurnAuthority
→ Narrator draft
→ repetition/agency guards
→ Narration Validator
→ optional Narrator repair
→ publication guard / deterministic fallback
→ materialization + commit
→ background memory jobs
```

Главный принцип: **Planner/engine владеют исходом, Narrator владеет только формой**. Если проза противоречит уже выполненному structured outcome, исправляется проза, а не состояние мира.

## Debugging и прозрачность

Полный debugger snapshot:

```text
GET /api/campaigns/{campaign_id}/debugger
```

Causal trace одного опубликованного ответа:

```text
GET /api/campaigns/{campaign_id}/debugger/turns/{assistant_turn_id}
```

Flight recorder кампании:

```text
GET /api/campaigns/{campaign_id}/debugger/trace
```

Локальная HTML-панель:

```text
/api/debugger
```

Trace помогает отделить первый неправильный boundary от последующего каскада: routing → Planner → authority → transition/action sequence → Narrator/Validator → materialization → memory.

Подробный список того, что уже прозрачно, а чего **ещё не хватает**, находится в [`docs/runtime-transparency.md`](docs/runtime-transparency.md).

## Изображения

ComfyUI integration уже подключена. При `IMAGE_ENABLED=true` после Session Zero best-effort/background генерация может создать портрет героя и обложку кампании; портреты новых NPC также могут планироваться фоново. Сцену пользователь может перегенерировать вручную из Play UI.

Визуальная генерация не является authority: её ошибка не должна отменять или изменять игровой ход.
