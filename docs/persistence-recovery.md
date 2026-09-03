# Persistence, undo и recovery

**Статус:** current implementation contract  
**Владельцы:** SQLAlchemy/Alembic repositories, `TurnSaga`, `GenerationLifecycleRepository`, `ActiveCanonReplay`, campaign archive/recovery services.

## Authority и storage

Локальный production mode использует SQLite как единственное canonical storage. Файлы изображений и derived reports не заменяют rows truth engine.

Alembic migrations являются единственным supported schema evolution path. Runtime и debugger должны читать одну и ту же schema; live-model oracle имеет отдельный schema-contract test против drift.

## Turn saga durability

Narrative turn не держит одну SQL transaction во время всех LLM calls. Ключевая граница — `PREPARED`:

1. user/generation attempt persisted;
2. Planner выполняется;
3. structured transition/materialization подготавливаются;
4. world mutations и lifecycle `PREPARED` commit-ятся вместе;
5. Narrator/Validator работают по уже durable prepared world;
6. assistant turn + finalization commit-ятся как `PUBLISHED`;
7. post-turn jobs выполняются независимо.

Lifecycle phases: `received → planned → prepared → narrated → published → post_turn_done`, с terminal compensation path для prepared failure.

## Failure semantics

До PREPARED ошибка не требует world compensation: незакоммиченные structured writes откатываются.

После PREPARED, но до PUBLISHED, `TurnSaga` компенсирует materialized entities/transition и ставит lifecycle `COMPENSATED`. Нельзя оставлять durable world mutation без published turn.

После PUBLISHED post-turn memory failure не откатывает игровой ход. Job остаётся retryable и виден debugger.

Startup/debugger умеет обнаруживать dangerous incomplete `PREPARED/NARRATED` attempts по persisted lifecycle. Это **не следует путать с полноценным автоматическим startup resolver**: текущая архитектура гарантирует compensation во время живого failure path, а оставшиеся после hard process death attempts должны быть явно обнаружены/разобраны, пока отдельная startup policy не реализована.

## Undo

Undo — не удаление последних строк вслепую. `ActiveCanonReplay` восстанавливает projection из active canonical history/initial checkpoint и исключает undone turn pair. Structured movement, created NPC, items/facts и другие turn-owned последствия должны исчезать или возвращаться к предыдущему состоянию согласно их lifecycle.

Meta turns `/DM`/`/OOC` не входят в narrative undo pair.

## Archive/backup

Campaign archive/export должен переносить canonical campaign data и необходимые durable relations. Generated visual files считаются derived assets; отсутствие картинки не делает campaign invalid.

Перед destructive/rebuild операциями authoritative SQLite/archive важнее prose export. Migration downgrade/upgrade cycles регулярно гоняются в CI.

## Persisted evidence

- `turns` + status/parent/source links;
- `generation_runs`;
- `generation_lifecycles` + attempt/timestamps/phases;
- `scene_transitions`, action sequences и materialization provenance;
- initial world state/checkpoint;
- facts/beliefs/relationships/theses/events/items;
- `post_turn_jobs` с attempts/error;
- archive metadata/files.

## Debugging

`/api/campaigns/{id}/debugger` показывает generation runs/jobs/current truth. Per-turn trace связывает generation → plan → transition → publication → memory. `dangerous_incomplete_generations` — сигнал ручного расследования, а не художественное состояние игры.

## Проверка

Deterministic suites включают migration cycles, resume/canon replay/undo, post-turn retry, compensation и campaign integrity. Local live-model contracts дополнительно проверяют реальные model-driven transitions и undo в отдельной SQLite на каждый case run.

## Историческая граница

Старые модели «просто сохранить prose и потом восстановить состояние из текста» и «одна гигантская DB transaction вокруг LLM» не являются current design.