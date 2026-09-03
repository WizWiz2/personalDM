# Playtest protocol

**Статус:** current verification contract  
**Владельцы:** deterministic CI, `PlaytestTraceService`, local `live_model_contracts`.

## Цель

Playtest должен находить первый неправильный boundary, а не просто фиксировать «проза странная». Для каждого regression различаются semantic model decision, deterministic state transition, Narrator draft, validation/repair, publication и post-turn memory.

## Три разных слоя проверки

### 1. Deterministic CI

Unit/integration/product-contract tests проверяют написанную нами deterministic логику. Внешняя недетерминированность может быть scripted/mocked. CI не доказывает, что реальная Qwen/Gemma понимает произвольный ход.

Coverage относится только к deterministic Python-коду и не является оценкой качества LLM.

### 2. Local model contracts

`test-models.bat` запускает реальные configured Ollama models через настоящий turn runtime. Каждый contract получает отдельный Python process и SQLite, фиксированный starting world/input и deterministic DB oracle.

Проверяется delta truth state: create/remove/revisit/supersede/undo для Scene, Location, NPC, presence, fact, knowledge, relationship, thesis, item, event, time и compound sequence. Точная формулировка прозы не сравнивается.

Отчёты: `src/backend/data/live-model-contracts/<timestamp>/` и stable pointer `.../latest/`.

### 3. Soak/live campaign

Длинная игра нужна для накопительной деградации, latency tails и continuity, но не заменяет короткие transition contracts.

## Обязательный causal trace

При player-visible ошибке фиксировать:

1. exact user input/channel;
2. model role и actual model;
3. Planner plan + telemetry;
4. structured execution/transition/materialization;
5. `TurnAuthority`;
6. raw Narrator draft;
7. Validator attempts/violations/repair;
8. publication mode и final text;
9. persisted Scene/Location/presence/canon;
10. post-turn jobs/memory.

`GET /api/campaigns/{id}/debugger/turns/{assistant_turn_id}` является primary per-turn evidence. Campaign trace агрегирует publication modes, RAW/PUBLISHED classes и latency.

## RAW / PUBLISHED классификация

Trace различает как минимум:

- `RAW GOOD/PUBLISHED GOOD`;
- `RAW BAD/PUBLISHED GOOD`;
- `RAW GOOD/PUBLISHED BAD`;
- `RAW BAD/PUBLISHED BAD`;
- `RAW UNKNOWN/...`, если старый turn не имеет durable validation audit.

Это диагностика boundary, а не художественная оценка. Для repaired turn также считается preservation ratio как вспомогательная метрика.

## PASS/FAIL

Hard truth invariant должен проходить 100%: dead NPC resurrection, duplicate identity, impossible movement, canon overwrite, unauthorized item transfer и подобные нарушения нельзя усреднять.

Stochastic semantic cases можно повторять; отчёт обязан показывать first-pass/repetition rate, а не rerun-until-green.

Prose quality оценивается отдельно от truth correctness. `safe_fallback` считается degraded publication, даже если структурное состояние осталось корректным.

## Failure report template

Минимум: case/input → expected structural delta → actual delta → first wrong boundary → upstream cause → downstream cascade → player-visible symptom → artifact path.

## Историческая граница

Старые 100–200-turn deterministic simulations допустимы как soak/regression fixture, но не считаются доказательством semantic correctness реальных моделей.