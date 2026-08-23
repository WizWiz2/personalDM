# Runtime Transparency

**Статус:** current implementation map  
**Дата:** 24 августа 2026

Этот документ отвечает на вопрос: **почему игрок увидел именно такой ответ и где это можно доказать?**

Он не заменяет product foundation или ADR. Его задача — показать текущую production цепочку, владельца каждого решения, persisted evidence и известные пробелы наблюдаемости.

## 1. Главный принцип

PersonalDM разделяет:

- **truth/state** — Scene, Location, participants, movement, NPC identity, structured outcome, facts/beliefs;
- **presentation** — художественная формулировка того, что уже произошло.

LLM не должна быть единственным свидетелем собственного решения. Для важных boundary система хранит typed/deterministic evidence.

## 2. Кто чем владеет

| Решение | Владелец | Может Narrator изменить? | Где искать evidence |
|---|---|---:|---|
| raw player input | пользователь | нет | `turns` user turn |
| meta vs narrative routing | `GameApplication` / meta parser | нет | turn role/channel |
| acting NPC | routing + scene presence | нет | `acting_character_id`, scene participants |
| intended outcome | `TurnAuthorityPlanner` | нет | `context_snapshot.turn_planner` |
| action sequence | Planner + deterministic executor | нет | `action_sequences`, authority snapshot |
| Scene/Location transition | deterministic transition services | нет | `scene_transitions`, active Scene, player `current_location_id` |
| allowed new NPC | Planner/normalizers + materializer | нет | `npc_introductions`, `allowed_new_npcs`, materialization snapshot |
| final authority contract | `TurnAuthority` | нет | `context_snapshot.turn_authority` |
| prose | Narrator | да, только внутри authority | validation audit / assistant turn |
| prose accept/repair | Narration Validator + deterministic guards | state менять не может | `narration_validation_runs` + telemetry |
| fallback prose | `NarrationPublicationGuard` | deterministic | publication metadata |
| durable memory proposals | Scribe / Registrar | не задним числом | post-turn jobs + proposed changes |
| final persisted memory | deterministic validation/storage | нет | facts, beliefs, relationships, events |

Если две записи расходятся, **structured state и persisted authority важнее художественной прозы**.

## 3. Полный narrative turn

```text
1. принять user input
2. сохранить user turn / generation run
3. собрать ContextCompiler snapshot
4. вызвать TurnAuthorityPlanner
5. выполнить structured boundary/action sequence
6. построить TurnAuthority
7. собрать Narrator context + compact authority
8. получить raw Narrator draft
9. проверить repetition и deterministic player agency
10. вызвать Narration Validator
11. при необходимости получить Narrator repair и проверить снова
12. при невозможности безопасной прозы построить deterministic publication fallback
13. сохранить published assistant turn
14. materialize разрешённые structured сущности/результаты
15. commit
16. enqueue background memory jobs
```

В live regression следует искать **первый неправильный этап**, а не последнюю видимую ошибку. Например, `/talk Анна` после неудачного return может быть каскадом; root cause находится раньше в destination authorization.

## 4. Model transparency

### Campaign primary

По default это `gemma4:e4b`, но provider config хранится на кампании. Поэтому имя default-модели не является доказательством модели старого turn.

Primary обслуживает:

- Narrator;
- opening scene;
- Session Zero;
- `/DM` / `/OOC` Game Master;
- Character Builder, если нет override.

### Control model

Default `qwen2.5:7b` обслуживает:

- Planner;
- Narration Validator;
- Entity Registrar;
- Scribe;
- Curator;
- Evaluator;
- simulation Player;
- Scenario Builder;
- structured repair.

Control roles strict: provider/schema failure не должен скрыто переключать их на creative Narrator.

### Что должно быть видно по конкретному turn

Минимум:

- `model_role`;
- actual `model`;
- `role_model_source`;
- provider transport/status;
- prompt/completion usage, если provider сообщил;
- duration;
- whether routing fallback occurred.

## 5. Context transparency

`ContextCompiler` возвращает messages и metadata manifest.

Для расследования важны:

- authoritative Scene ID;
- active Location/path;
- physically present participants;
- available exits/destinations;
- actor identity;
- included facts/beliefs/theses/details;
- history budget;
- current user message;
- final Narrator context budget.

Для Narrator после P0 recovery Planner reserve не вычитается повторно. Metadata содержит:

```text
planner_reserve_removed_from_narrator_budget=true
final_narrator_context_budget=<value>
```

Если маленькая модель начинает писать обобщённо или теряет continuity, budget/manifest необходимо проверять до смены модели.

## 6. TurnAuthority transparency

Narrator получает compact render contract, но persisted `TurnAuthority` остаётся audit source.

Ключевые поля:

- exact `player_input`;
- player/acting character;
- source/target location;
- present/known-absent characters;
- allowed new NPCs;
- resolution;
- observable consequences;
- action sequence;
- narration guidance;
- pending player choice;
- allow/disallow new complication.

### Agency

Human player владеет protagonist actions/dialogue. Deterministic guard ловит как минимум:

- повтор прямой реплики игрока как реплики мира/NPC;
- добавленное добровольное действие/мысль/эмоцию героя.

Semantic Validator дополняет эти проверки более широкими authority constraints.

## 7. Narration transparency

В БД `NarrationValidationRun` уже сохраняет:

- original `draft_text`;
- validator model;
- attempts;
- candidate text каждого attempt;
- verdict/summary;
- violations;
- telemetry;
- final text;
- repair count;
- failure reason.

Это позволяет различать:

- `RAW GOOD / PUBLISHED BAD`;
- `RAW BAD / PUBLISHED BAD`;
- `RAW BAD / PUBLISHED GOOD`;
- `RAW GOOD / PUBLISHED GOOD`.

Без этого сравнения нельзя честно ответить, сломалась ли Gemma или post-generation pipeline.

## 8. Publication modes

Нормальные и degraded режимы необходимо различать явно:

- `passed` — первый draft принят;
- `repaired` — потребовался repair или presentation recovery;
- `failed_open` — Validator path недоступен/сломался, publication guard применил policy;
- `safe_fallback` — опубликована deterministic authority projection;
- `not_invoked` — validator не относится к этому turn.

`safe_fallback` не должен считаться художественным успехом. В quality report это отдельный failure/degraded metric.

## 9. Session Zero transparency

Session Zero хранит structured draft отдельно от текста разговора.

При завершении:

1. `ready_to_finalize=true`;
2. final interview message детерминированно terminal и не задаёт новый вопрос;
3. герой/Location/Scene materialize;
4. создаётся system-owned opening assistant turn;
5. opening использует campaign Narrator;
6. повторный finalize не создаёт второй opening.

Opening context snapshot содержит marker `session_zero_opening=true`, `system_owned=true`, source и provider telemetry.

## 10. Debugger endpoints

### Campaign snapshot

```text
GET /api/campaigns/{campaign_id}/debugger
```

Показывает:

- campaign/current Scene/player Location;
- scene/location invariant issues;
- entities/locations/scenes/participants;
- turns и их `context_snapshot`;
- facts/beliefs/relationships/events;
- theses/proposals;
- post-turn jobs;
- generation runs;
- health counters.

### Causal trace одного turn

```text
GET /api/campaigns/{campaign_id}/debugger/turns/{assistant_turn_id}
```

Собирает удобную цепочку:

```text
input
→ routing
→ planner
→ authority
→ transition
→ materialization
→ narrator
→ validator summary
→ memory
→ generation/timing
→ diagnostics
```

### Whole-playtest flight recorder

```text
GET /api/campaigns/{campaign_id}/debugger/trace
```

Содержит per-turn trace и summary:

- diagnostic flags;
- validator statuses;
- interactive latency min/max/average.

### HTML debugger

```text
GET /api/debugger
```

## 11. Автоматические diagnostic flags

`PlaytestTraceService` уже умеет отмечать, среди прочего:

- `PLANNER_BYPASSED_WITH_ACTION_LANGUAGE`;
- `ACTOR_MEMORY_DROPOUT`;
- `OBJECTIVE_CANON_FROM_ACTOR_SPEECH`;
- `PROSE_STATE_DIVERGENCE`;
- `TECHNICAL_LEAK`;
- `SLOW_TURN`.

Эти flags являются подсказкой, а не заменой causal analysis.

## 12. Что сейчас всё ещё недостаточно прозрачно

Ниже — реальные observability gaps текущего `main`, а не пожелания «когда-нибудь».

### GAP 1 — raw Narrator/repair audit есть в БД, но не выведен в обычный debugger snapshot/trace полностью

`NarrationValidationRun` хранит draft и все attempts, однако `DebuggerService.snapshot()` их сейчас не запрашивает. `PlaytestTraceService` показывает validator status/telemetry, но не гарантирует полный raw draft → repair → final diff.

**Почему важно:** при плохой прозе нельзя одним trace доказать, испортила ли текст Gemma, Validator repair или publication guard.

**Рекомендуемое улучшение:** добавить в per-turn trace блок:

```json
"narration_audit": {
  "draft_text": "...",
  "attempts": [...],
  "final_text": "...",
  "status": "passed|repaired|..."
}
```

### GAP 2 — publication mode не виден игроку/тестировщику рядом с сообщением

GUI показывает одинаково normal prose и `safe_fallback`.

**Почему важно:** деревянный deterministic receipt визуально выглядит как «Gemma написала плохо».

**Рекомендуемое улучшение:** только в debug/developer mode показывать badge `passed / repaired / safe_fallback`, actual model и latency. В обычной игре технический шум не нужен.

### GAP 3 — нет удобного prompt-budget breakdown на один turn

Manifest хранит часть budget metadata, provider telemetry — usage, но нет одной таблицы:

```text
context window
system contract
scene state
cards
memory
history
compact authority
input
response reserve
actual provider prompt tokens
```

**Почему важно:** маленькая локальная модель чувствительна к нескольким сотням токенов control noise.

### GAP 4 — `runtime_manifest()` не является пользовательски доступным debug endpoint

Manifest есть в Python и тестирует parity, но его неудобно получить из работающей GUI-сборки.

**Рекомендуемое улучшение:** read-only `/api/debugger/runtime` с guards, context pipeline, narration pipeline, model defaults и build commit.

### GAP 5 — documentation drift не проверяется автоматически

Runtime guards/model roles менялись, а architecture docs продолжали описывать старые списки.

**Рекомендуемое улучшение:** генерируемый `docs/runtime-manifest.json` или docs-test, сравнивающий перечисленные model roles/guards/pipeline со `runtime_manifest()`.

### GAP 6 — meta `/DM` должен быть прозрачен как read-only, но внутренний snapshot не должен становиться user-facing текстом

Debug transparency и prompt leakage — противоположные вещи. Внутренние `[AUTHORITATIVE SCENE STATE]`, watchdog и system instructions должны быть доступны debugger’у, а не печататься в обычном ответе `/DM`.

**Рекомендуемое улучшение:** отдельный meta output publication/sanitization boundary + regression на отсутствие internal control blocks.

### GAP 7 — художественное качество пока не является persisted metric

Live tests уже оценивают coherence/prose/utility, но runtime не сохраняет такой score.

Это нормально для production engine, однако benchmark/playtest tooling должен хранить:

- coherence score;
- prose score;
- dramatic utility;
- repetition/stock phrase flags;
- raw-vs-published classification.

Иначе можно улучшать authority correctness и незаметно ухудшать саму игру.

## 13. Рекомендуемый минимальный transparency backlog

Порядок по пользе:

1. **P0:** включить `NarrationValidationRun` и attempts в playtest per-turn trace;
2. **P0:** debug-only publication badge/model/latency в GUI или debugger timeline;
3. **P1:** `/api/debugger/runtime` из `runtime_manifest()`;
4. **P1:** per-turn token-budget breakdown;
5. **P1:** meta `/DM` output sanitization/validation boundary;
6. **P2:** persist playtest prose-quality evaluation отдельно от production truth state;
7. **P2:** docs/runtime manifest drift test.

## 14. Правило live-playtest расследования

При FAIL фиксировать в таком порядке:

```text
exact input
→ route/channel/actor
→ raw Planner plan
→ deterministic normalization/repair
→ executed sequence / transition
→ TurnAuthority
→ raw Narrator draft
→ validation attempt 0
→ repair draft (если есть)
→ validation attempt 1
→ publication mode/final text
→ materialization
→ active Scene/Location/participants
→ post-turn memory
```

В отчёте отдельно указывать:

- **first wrong boundary**;
- **cascade**;
- **player-visible symptom**.

Это не бюрократия: такой формат не даёт чинить последний симптом вместо первой причины.

## 15. Определение «прозрачного» PersonalDM

Система достаточно прозрачна, когда для любого опубликованного хода можно без догадок ответить:

- что хотел игрок;
- что понял Planner;
- что реально выполнил engine;
- какую authority получил Narrator;
- что первоначально написал Narrator;
- что именно отклонил Validator;
- был ли repair/fallback;
- какая модель реально использовалась;
- что было опубликовано;
- что было записано в мир и память;
- сколько занял каждый критический этап.

Сейчас большая часть evidence уже durable, но пункты про полный narration audit, runtime manifest endpoint и удобный token breakdown ещё требуют реализации.
