# Личный ДМ
## Фундамент продукта

**Статус:** канонический рабочий документ  
**Версия:** 0.2  
**Дата:** 15 июля 2026

## 1. Продукт

«Личный ДМ» — local-first приложение для продолжительных AI-кампаний в НРИ. LLM создаёт повествование, но не является источником истины. Источник истины — Campaign Truth Engine, который хранит канон, состояние сцен, знания персонажей и происхождение изменений.

Пользователь может подключить локальную или OpenAI-compatible LLM, продолжать кампанию после restart, видеть активные тезисы сцены, хранить канон и субъективные знания NPC, проверять происхождение памяти, исправлять ошибки, вручную генерировать изображения сцен, подбирать локальную музыку и экспортировать кампанию.

## 2. Основная гипотеза

Проблема длинных AI-кампаний не в качестве одного ответа, а в распаде причинности: NPC используют недоступные секреты, отношения и цели забываются, сцена теряет состояние, старые последствия исчезают, а ошибочную память трудно исправить.

> Пользователь выберет локального AI-ДМа, который ведёт проверяемый канон, понимает ограниченные знания персонажей и позволяет чинить память без переписывания всей истории.

## 3. Killer feature

### Campaign Truth Engine

Система различает объективный факт, присутствие при событии, восприятие, интерпретацию, текущее убеждение, уверенность, источник сведений и влияние целей/отношений.

### Campaign Debugger

У каждого важного утверждения есть источник, время появления, статус, область видимости, история изменений и возможность исправления. Derived memory пересобирается из сырого архива.

### Actor-scoped knowledge

Narrative LLM не получает все секреты присутствующих NPC в одном общем prompt. Для значимой реплики или решения формируется actor packet:

- общая наблюдаемая сцена;
- знания конкретного NPC;
- его убеждения;
- цели;
- отношения;
- доступные ему активные тезисы.

Недоступные сведения не маскируются инструкцией. Они не передаются в actor context.

## 4. Память

### Сырой архив

Append-only журнал пользовательского ввода, ответов ДМа, tool calls, proposed/applied changes, использованного контекста, модели, corrections, undo и regenerate. Канонический архив хранится в SQLite. JSONL и Markdown — экспорт.

### Структурированный канон

Entities, facts, beliefs, character goals, relationship assertions, events, scenes, scene theses и memory chunks.

### Оперативный контекст

Context Compiler собирает только релевантную часть канона для конкретного хода и конкретного действующего NPC.

## 5. Scene Thesis

Тезис сцены — компактное утверждение, которое ДМ активно удерживает в рабочем контексте.

Примеры:

- «Лиара подозревает Софию, но не готова обвинить её открыто».
- «Айра подслушивает за дверью; участники сцены этого не знают».
- «Разговор внешне вежливый, напряжение растёт».

Минимальные поля: scene_id, type, text, priority, status, visibility, related_entities, source_turn_id, locked_by_user.

Типы: canon, intent, relationship_dynamic, secret, tension, unresolved_beat, visual_state, music_mood.

## 6. Единый pipeline хода

```text
User input
→ save raw input
→ Context Compiler
→ Narrative LLM streaming
→ save raw response
→ Proposed Changes extraction
→ deterministic validation
→ user review or commit
→ update canon
→ async summary / embeddings / soft continuity warnings
```

Правила:

- streaming не блокируется поздними семантическими проверками;
- критические state changes происходят через typed tools или validated deltas;
- SQL проверяет структуру предложений, а не смысл произвольной прозы;
- semantic continuity checks являются мягкими предупреждениями;
- Memory Scribe создаёт proposals, но не переписывает канон напрямую.

## 7. Приоритеты

### P0. Вертикальный скелет

Одна кампания, один пользователь, провайдер LLM, streaming, stop, turn log, restart and continue, undo/regenerate, export, обработка ошибок.

### P1. Ядро памяти и тезисов

Scene, Scene Thesis, Character, Fact, Belief, Goal, Relationship Assertion, actor-scoped context, Memory Inspector и provenance.

### P2. Assisted Canon и тонкая атмосфера

После ответа система предлагает 1–5 изменений; пользователь принимает, отклоняет или правит. Добавляются ручная кнопка «Нарисовать сцену», один approved reference на персонажа, локальная музыкальная библиотека, mood tags и cooldown.

### P3. Длинная кампания

Scene summaries, FTS5, vector search, entity-aware retrieval, reindex, correction propagation и мягкие continuity warnings.

### P4. Расширение

IP-Adapter/InstantID/ControlNet, многоперсонажная визуальная консистентность, automatic visual triggers, продвинутая музыка, rulesets, TTS и multiplayer.

## 8. Формат и стек

Целевая форма — Tauri desktop app с React/Vite, локальным FastAPI backend и SQLite. Первые этапы работают как localhost web app. LangGraph не используется в MVP; Turn Runner реализуется на async Python.

## 9. Доменная модель

- `Entity` хранит базовую идентичность; типоспецифичные данные — отдельно.
- Goals — отдельные записи с жизненным циклом, не JSON-массивы.
- Secret backstory представляется facts, visibility и beliefs, когда возможно.
- Relationships — assertion records с type, optional intensity, description, reason, provenance, validity и visibility. Numeric axes — optional derived view.
- `Character.current_location_id` — источник истины; occupants вычисляются.
- Item имеет ровно одно текущее положение: owner, location или container; это обеспечивает DB constraint.

## 10. Изображения и музыка

Ранний image workflow: approved reference → manual scene generation → сохранение prompt, model/workflow, seed и результата. MVP не обещает абсолютную консистентность.

Музыка работает с локальной библиотекой. Выбор учитывает mood/energy/tension, случайность, novelty bonus и штраф за недавние повторы.

## 11. Не входит в MVP

PostgreSQL adapter, server mode, multiplayer, full branch tree, branch merge, tactical VTT, generic ruleset framework, automatic images on every turn, semantic judging of every sentence, relationship graph и TTS для каждого NPC.

## 12. Критерий доказанной идеи

Пользователь может играть несколько сотен ходов, исправлять scene theses, продолжать после restart, встречать старого NPC с корректной knowledge boundary, проверять источник знания, чинить факт, вручную генерировать изображение сцены и получать неповторяющийся локальный музыкальный фон.
