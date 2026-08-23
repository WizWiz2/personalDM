# Карта документации

В репозитории есть документы трёх разных типов: продуктовые принципы, архитектурные решения и описание текущей реализации. Их нельзя смешивать в одну шкалу приоритета.

## Как читать документы

### 1. Нормативные продуктовые документы

- `product-foundation.md` — зачем существует продукт, какие свойства важны и чего он сознательно не делает.
- `MVP-SPEC.md` — исходный контракт первого вертикального среза.
- принятые ADR — конкретные архитектурные решения, которые могут уточнять или supersede старые части MVP/product docs.

### 2. Текущая реализация

- `runtime-transparency.md` — актуальная карта production runtime: этапы хода, владельцы authority, модели, persisted evidence и известные пробелы наблюдаемости.
- `model-role-routing.md` — фактическая маршрутизация model roles.
- `gemma-narrator-stability.md` — текущие ограничения и recovery-механизмы Narrator.
- `architecture/interagent-turn-authority.md` — typed `TurnAuthority` contract.
- `architecture/narration-pipeline.md` — фактический publication pipeline.

Документы `architecture/context-pipeline.md` и `architecture/personal-dm-runtime.md` полезны как история стабилизации архитектуры, но отдельные списки guards, приоритеты клиентов и future-work пункты в них могут устаревать. Для текущего состояния сверяйтесь с `runtime-transparency.md` и `app.runtime.runtime_manifest()`.

### 3. Исследования

- `docs/amendments/` — исследовательские дополнения; сами по себе не становятся нормативными.
- предлагаемые ADR — обсуждаемые решения, пока не приняты.

## Порядок разрешения противоречий

Для **продуктового смысла**:

1. принятый ADR;
2. `product-foundation.md`;
3. `MVP-SPEC.md`;
4. proposed ADR / amendments.

Для вопроса **«что реально делает текущая сборка?»**:

1. persisted evidence и код текущего `main`;
2. `runtime_manifest()`;
3. `runtime-transparency.md`;
4. специализированные current-state docs (`model-role-routing.md`, `narration-pipeline.md`);
5. исторические architecture notes.

Это различие намеренное: документация не должна притворяться более истинной, чем наблюдаемая система.

## Текущий продуктовый фокус

PersonalDM сейчас — **systemless narrative RPG engine**, а не CRPG/rules engine.

Приоритеты текущей стабилизации:

- **P0 — playable narration:** качественный связный Narrator без присвоения действий/реплик героя, без систематических safe fallback и с хорошим opening;
- **P0 — authority correctness:** Scene/Location, movement, presence, NPC identity, direct contact и player agency должны быть структурно истинны до публикации прозы;
- **P1 — observability:** любой плохой ответ должен раскладываться на routing → plan → authority → execution → draft → validation/repair → publication → memory;
- **P1 — long-session continuity:** facts, beliefs, relationships, theses и transient narrative detail должны переживать длинную игру без утечек знаний;
- **P2 — visual atmosphere:** локальные портреты/обложки/сцены через ComfyUI без влияния на truth engine;
- **P3 — rules layer:** только как отдельный осознанный слой, если появится потребность в конкретной игровой системе.

## Зафиксированные границы

- SQLite остаётся единственным production-хранилищем локального режима.
- LLM не является источником истины для placement, movement, participant set или других структурных последствий.
- Недоступные NPC сведения должны отсутствовать из actor-scoped context, а не только сопровождаться инструкцией «не используй».
- Narrative prose публикуется только после authority validation/repair или deterministic presentation fallback.
- Post-turn memory jobs не должны отменять уже опубликованный и закоммиченный игровой ход.
- Meta `/DM` — read-only канал и не должен сам чинить канон или продолжать сцену.
- Визуальная генерация best-effort и не является authority.

## Обязательная прозрачность

При расследовании live-regression недостаточно посмотреть только опубликованный текст. Минимальный пакет доказательств должен позволять ответить:

1. какой input был принят и в какой channel он попал;
2. какая модель/роль реально вызывалась;
3. что решил Planner;
4. какие structured действия действительно были выполнены;
5. какой `TurnAuthority` получил Narrator;
6. какой draft выдал Narrator;
7. что сказал Validator и был ли repair;
8. почему был опубликован именно final text;
9. какие NPC/Location/Scene были материализованы;
10. что затем попало в память.

Текущее состояние и оставшиеся пробелы описаны в [`runtime-transparency.md`](runtime-transparency.md).
