# Runtime Transparency

**Статус:** current implementation map  
**Дата:** 29 августа 2026

Этот документ отвечает на вопрос: **почему игрок увидел именно такой ответ и где это можно доказать?**

## 1. Главный принцип

PersonalDM разделяет truth/state и presentation. Scene, Location, physical presence, movement, NPC identity и structured outcome не выводятся из красивой прозы задним числом. LLM не должна быть единственным свидетелем собственного решения.

Если prose расходится с persisted structured state/authority, structured state имеет приоритет.

## 2. Кто чем владеет

| Решение | Владелец | Persisted evidence |
|---|---|---|
| raw player input | пользователь | user `turns` row |
| meta vs narrative routing | `GameApplication` | route/channel + turn snapshot |
| intended outcome | `TurnAuthorityPlanner` | `context_snapshot.turn_planner` |
| action sequence | Planner + deterministic executor | `action_sequences` + authority |
| Scene/Location transition | deterministic transition services | `scene_transitions` |
| physical presence mutation | `PresenceService` | `scene_participants` + `Character.current_location_id` |
| allowed new NPC | Planner + materializer | authority + materialization snapshot |
| final render contract | `TurnAuthority` | `context_snapshot.turn_authority` |
| prose | Narrator | narration audit + assistant turn |
| accept/repair/containment | Validator + deterministic guards | `narration_validation_runs` |
| saga progress | `TurnSaga` | `generation_lifecycles` |
| durable memory work | post-turn pipeline | jobs + proposals/canon |

## 3. Фактический narrative turn

```text
1. persist user turn / generation run
2. lifecycle = RECEIVED
3. compile Planner context
4. Planner structured decision
5. lifecycle = PLANNED
6. deterministic scene/action execution
7. build TurnAuthority
8. materialize allowed structured outcomes
9. commit prepared structured truth
10. lifecycle = PREPARED
11. recompile Narrator context from prepared world
12. Narrator draft
13. deterministic checks + Validator + optional repair/containment
14. lifecycle = NARRATED
15. persist assistant turn / finalize prepared transition/materialization
16. lifecycle = PUBLISHED; generation status = completed
17. enqueue post-turn jobs
18. after durable first processing pass: lifecycle = POST_TURN_DONE
```

Это порядок production code и `runtime_manifest()`. Materialization **не** происходит после Narrator: world truth intentionally prepared before prose.

## 4. Saga recovery transparency

`GenerationRun.status` отвечает на вопрос «чем закончился run», а `generation_lifecycles.phase` — «до какой causal boundary дошла текущая попытка».

Phases:

```text
received → planned → prepared → narrated → published → post_turn_done
                              ↘ compensated
```

В lifecycle row также сохраняются `attempt` и timestamp каждой фазы.

`GenerationLifecycleRepository.list_incomplete()` специально ищет `prepared/narrated` runs без безопасного terminal phase. Это crash-recovery evidence: такие attempts нельзя просто считать обычным failed turn, потому что world mutation могла уже быть durable.

На abort после `PREPARED`, но до `PUBLISHED`, `TurnSaga` компенсирует materialization/transition и фиксирует `COMPENSATED`.

## 5. Model transparency

Campaign primary по default `gemma4:e4b`; control default `qwen2.5:7b`. Фактическое имя модели старого turn определяется persisted routing/provider telemetry, а не текущим default.

Для конкретного model call полезны:

- `model_role`;
- actual model;
- role model source;
- provider status;
- prompt/completion usage;
- duration;
- routing fallback.

## 6. Context transparency

Planner context и Narrator context — разные causal snapshots.

Planner получает world до execution. Если structured execution/materialization меняет scene/presence/NPC state, Narrator context компилируется повторно после `PREPARED`. Поэтому debug analysis должен различать:

```text
planner_context_scene_id
narrator_context_scene_id
```

и смотреть token-budget metadata отдельно для final Narrator context.

## 7. TurnAuthority transparency

Persisted `TurnAuthority` содержит exact player input, player/acting character, source/target location, present/known-absent characters, allowed new NPCs/arrivals, resolution, observable consequences, action sequence, narration guidance и player-control constraints.

Human player владеет voluntary protagonist actions/dialogue. Narrator может только отрендерить authority и не может создать competing world outcome.

## 8. Narration transparency

`NarrationValidationRun` хранит raw draft, validator model, attempts, verdict/violations, telemetry, final text, repair count и failure reason. Это позволяет различать `RAW GOOD/PUBLISHED BAD`, `RAW BAD/PUBLISHED GOOD` и другие классы проблемы.

Publication modes должны анализироваться отдельно: `passed`, `repaired`, `failed_open`, `safe_fallback`, `not_invoked`. `safe_fallback` — degraded presentation, а не художественный успех.

## 9. Physical-state transparency

`PresenceService` является единственным implementation owner для `SceneParticipant`/`Character.current_location_id` mutations. `SceneRepository.add_participant/remove_participant` — compatibility facade и не содержит отдельной mutation policy.

`SceneStateService` остаётся authoritative read/invariant checker.

При movement расследовании проверять минимум:

```text
scene_transition
source/target location
character current_location_id
scene participant rows
active scene
```

Stale participation на другой physical location при structured move удаляется mutation owner'ом.

## 10. Debugger

`GET /api/campaigns/{campaign_id}/debugger` теперь показывает у generation runs:

```text
status
phase
attempt
received/planned/prepared/narrated/published/post_turn_done/compensated timestamps
```

Health включает `dangerous_incomplete_generations` для persisted `PREPARED/NARRATED` attempts.

Per-turn investigation должна идти в causal order:

```text
input
→ routing
→ Planner context/plan
→ deterministic execution
→ TurnAuthority
→ materialization
→ Narrator context
→ raw Narrator
→ validation/repair/containment
→ publication
→ active Scene/Location/presence
→ post-turn memory
```

В отчёте отдельно фиксировать first wrong boundary, cascade и player-visible symptom.

## 11. Runtime parity

`runtime_manifest()` — auditable fingerprint для CLI/FastAPI/test harness и содержит:

- guards;
- context providers;
- actual turn pipeline;
- generation phases;
- failure semantics;
- narration pipeline;
- implementation identities;
- post-turn mode.

Parity test должен падать при drift entrypoints или при возврате скрытой production зависимости от legacy `base_turn_runner`.

## 12. Что пока остаётся недостаточно прозрачно

1. Полный `NarrationValidationRun` audit всё ещё не выведен целиком в обычный debugger snapshot/flight trace.
2. GUI не показывает debug-only publication mode/model/latency рядом с сообщением.
3. Нет одной consolidated таблицы prompt-budget breakdown.
4. `runtime_manifest()` ещё не вынесен в удобный read-only GUI/debug endpoint.
5. `/DM` нужен отдельный user-facing sanitization boundary против internal prompt/control leakage.
6. Художественное качество live playtest пока не persisted metric.
7. Startup recovery пока только **обнаруживает** опасные incomplete attempts через persisted lifecycle; автоматическое decision/compensation при старте процесса ещё не выполняется.

## 13. Минимальный transparency backlog

1. P0 — добавить full NarrationValidationRun attempts в per-turn trace;
2. P0 — automatic startup handling для incomplete `PREPARED/NARRATED` attempts;
3. P1 — debug publication badge/model/latency;
4. P1 — `/api/debugger/runtime` поверх `runtime_manifest()`;
5. P1 — consolidated token-budget breakdown;
6. P1 — `/DM` sanitization/validation boundary;
7. P2 — persisted playtest prose-quality evaluation;
8. P2 — docs/runtime manifest drift test.

## 14. Определение прозрачного PersonalDM

Для любого опубликованного хода должно быть возможно без догадок ответить: что ввёл игрок, что решил Planner, что реально изменил engine, какая authority была подготовлена, что первоначально написал Narrator, что отклонил Validator, был ли repair/fallback, какая модель использовалась, что опубликовано, что записано в world/memory и на какой saga phase произошёл любой failure.
