# Playable Campaign Debugger milestone

Этот milestone превращает внутренние механизмы PersonalDM в наблюдаемый и восстанавливаемый игровой цикл. Текущее состояние существенно шире первоначального debugger snapshot.

## Что уже наблюдаемо

- явный `player_character_id` у кампании;
- authoritative current Scene и player Location;
- persistent generation runs и cancellation state;
- durable post-turn jobs;
- Campaign Debugger snapshot;
- causal trace одного assistant turn;
- whole-playtest flight recorder;
- scene transition/action-sequence evidence;
- entity/location/presence health;
- Session Zero state/invariants;
- facts, beliefs, relationships, events, theses и proposals с provenance;
- memory operations и retry post-turn jobs;
- backup/archive/rebuild tooling;
- latency и diagnostic flags для live playtest.

## Основные endpoints

```text
GET /api/campaigns/{campaign_id}/debugger
GET /api/campaigns/{campaign_id}/debugger/turns/{assistant_turn_id}
GET /api/campaigns/{campaign_id}/debugger/trace
GET /api/campaigns/{campaign_id}/memory-ops
GET /api/debugger
GET /api/memory-ops
```

## Per-turn causal trace

Trace собирает одну цепочку вокруг опубликованного assistant turn:

```text
input
→ routing
→ planner
→ TurnAuthority
→ transition/action sequence
→ materialization
→ narrator telemetry / published text
→ validator summary
→ memory job/proposals/persisted memory
→ generation/timing
→ diagnostics
```

Смысл trace — найти **first wrong boundary**, а не просто перечислить симптомы.

## Diagnostic flags

Автоматически детектируются, среди прочего:

- `PLANNER_BYPASSED_WITH_ACTION_LANGUAGE`;
- `ACTOR_MEMORY_DROPOUT`;
- `OBJECTIVE_CANON_FROM_ACTOR_SPEECH`;
- `PROSE_STATE_DIVERGENCE`;
- `TECHNICAL_LEAK`;
- `SLOW_TURN`.

Flags — эвристический слой. Они не подменяют проверку raw persisted evidence.

## Archive / rebuild

Экспорт и rebuild сохраняют идею Campaign Truth Engine: производное состояние должно быть восстановимо и проверяемо против baseline/current semantic projection. Provider secrets не должны попадать в portable archive.

## Что ещё не закрыто

Debugger пока не полностью показывает Narrator presentation pipeline, хотя данные уже сохраняются в БД.

Главный gap: `NarrationValidationRun` хранит original draft, attempts, violations, repair candidates и final text, но обычный `DebuggerService.snapshot()` эти строки не включает. Поэтому current playtest trace показывает validation status/telemetry, но не даёт полноценный raw draft → repair → published diff одним запросом.

Следующие observability задачи:

1. добавить narration validation attempts в per-turn trace;
2. показывать publication mode (`passed/repaired/safe_fallback`) в debug timeline;
3. добавить runtime manifest endpoint;
4. добавить per-turn prompt/token budget breakdown;
5. разделить debug transparency и player-facing `/DM`, чтобы внутренние snapshots никогда не утекали в обычный ответ.

Полный анализ: [`runtime-transparency.md`](runtime-transparency.md).

## Проверка

CI должен проверять не только debugger API shape, но и то, что causal evidence остаётся привязан к реальным persisted IDs/turns после изменений pipeline. Live playtest дополнительно проверяет пригодность trace для локализации первого неправильного boundary.
