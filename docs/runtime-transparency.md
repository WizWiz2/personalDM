# Runtime Transparency

**Статус:** current implementation contract  
**Дата:** 3 сентября 2026

Этот документ отвечает на вопрос: **почему игрок увидел именно такой ответ и где это доказать?**

## 1. Главный принцип

PersonalDM разделяет truth/state и presentation. Scene, Location, physical presence, movement, NPC identity и structured outcome не выводятся из прозы задним числом. Если prose расходится с persisted structured state/authority, structured state имеет приоритет.

## 2. Владельцы и persisted evidence

| Решение | Владелец | Persisted evidence |
| --- | --- | --- |
| raw input/channel | пользователь + `GameApplication` | turn row/channel |
| intended outcome | `TurnAuthorityPlanner` | `context_snapshot.turn_planner` |
| action sequence | Planner + deterministic executor | `action_sequences` + authority |
| Scene/Location transition | transition services | `scene_transitions` |
| physical presence | `PresenceService` | participants + `current_location_id` |
| NPC materialization | Planner + materializer | authority + materialization snapshot |
| render contract | `TurnAuthority` | `context_snapshot.turn_authority` |
| raw/final prose | Narrator/publication pipeline | `NarrationValidationRun` + assistant turn |
| validation/repair | Validator + deterministic publication guards | `narration_validation_runs` |
| saga progress | `TurnSaga` | `generation_lifecycles` |
| durable memory | post-turn pipeline | jobs + proposals/canon |

## 3. Narrative turn causal order

```text
persist user/generation
→ RECEIVED
→ compile Planner context
→ Planner structured decision
→ PLANNED
→ deterministic execution
→ build TurnAuthority
→ materialize allowed outcomes
→ PREPARED + structured commit
→ recompile Narrator context
→ Narrator draft
→ validation / repair / deterministic publication veto
→ NARRATED
→ persist assistant + finalize transition/materialization
→ PUBLISHED
→ post-turn jobs
→ POST_TURN_DONE
```

Materialization намеренно происходит **до** Narrator: prose описывает подготовленную truth, а не создаёт её.

## 4. Saga recovery transparency

`GenerationRun.status` показывает outcome run, `generation_lifecycles.phase` — последнюю durable causal boundary. Phases:

```text
received → planned → prepared → narrated → published → post_turn_done
                              ↘ compensated
```

На живом failure после PREPARED, но до PUBLISHED, `TurnSaga` компенсирует transition/materialization. Persisted `PREPARED/NARRATED` без terminal phase после hard process death обнаруживаются debugger как dangerous incomplete attempts.

Automatic startup resolver для hard-crash incomplete attempts пока не является частью transparency contract; это отдельная recovery capability, описанная в `persistence-recovery.md`.

## 5. Model и context transparency

Фактическая модель определяется persisted routing/provider telemetry, а не текущим default. Для каждого model call trace может показать actual model, source, duration, status, requested context/output budget и provider usage.

Planner context и Narrator context различаются: после PREPARED Narrator получает свежий world snapshot. Persisted metadata содержит `planner_context_scene_id` и `narrator_context_scene_id`.

ContextCompiler теперь дополнительно сохраняет deterministic `token_budget_breakdown` по диагностическим buckets:

```text
system
scene
memory
history
input
```

Per-turn trace добавляет estimated serialized `TurnAuthority`, requested reserves и actual provider prompt/completion usage. Это observability accounting: оно не меняет prompt selection.

## 6. Narration/publication transparency

`NarrationValidationRun` хранит raw draft, final text, attempts, violations, validator model, repair count и failure reason. `/api/campaigns/{id}/debugger/turns/{assistant_turn_id}` выводит полный matching audit и все runs для turn.

Trace отдельно показывает:

- durable validator status;
- runtime validator/publication mode;
- raw draft и final published text;
- repair count и preservation ratio;
- provider/model telemetry;
- deterministic diagnostics;
- RAW/PUBLISHED classification.

Классы: `RAW GOOD/PUBLISHED GOOD`, `RAW BAD/PUBLISHED GOOD`, `RAW GOOD/PUBLISHED BAD`, `RAW BAD/PUBLISHED BAD`; старые turns без audit маркируются `RAW UNKNOWN/...`.

`safe_fallback`/presentation fallback — degraded publication, а не художественный PASS.

## 7. Debugger surfaces

`GET /api/campaigns/{campaign_id}/debugger` — current persisted state/health.  
`GET /api/campaigns/{campaign_id}/debugger/turns/{assistant_turn_id}` — causal trace одного turn.  
`GET /api/campaigns/{campaign_id}/debugger/trace` — campaign-level playtest trace/summary.  
`GET /api/debugger/runtime` — read-only `runtime_manifest()` + build commit/source.

Standalone Campaign Debugger показывает debug-only runtime fingerprint и publication timeline: model, publication mode, RAW/PUBLISHED class, validator/repair count, latency и token usage. Normal player UI этим control-plane шумом не загрязняется.

## 8. `/DM` transparency boundary

`/DM`/`/OOC` может читать structured snapshot и объяснять причинность, но остаётся read-only. После model generation действует deterministic `sanitize_meta_output()`: если provider пытается вывести внутренние prompt/control markers, публикация fail-closed заменяется безопасным публичным объяснением. Факт sanitization сохраняется в meta assistant context snapshot.

## 9. Runtime parity/drift

`runtime_manifest()` является auditable fingerprint CLI/API/test harness и содержит guards, context pipeline, turn/narration pipelines, generation phases, failure semantics и implementation identities. Read-only endpoint делает этот fingerprint доступным без импорта кода вручную.

Current-state documentation map находится в `docs/README.md`; tests проверяют существование обязательных primary docs и runtime observability contracts.

## 10. Что не входит в закрытый observability backlog

Transparency теперь достаточна, чтобы определить first wrong boundary для опубликованного turn. Отдельными будущими улучшениями, а не пробелами #120, остаются:

- automatic startup decision/compensation для hard-crash incomplete attempts;
- полноценная persisted художественная оценка prose (coherence/dramatic utility) как benchmark metric.

Они не мешают увидеть raw draft, repair/publication и structured truth текущего turn.

## 11. Определение прозрачного PersonalDM

Для опубликованного хода без ручного SQL должно быть возможно ответить: что ввёл игрок, какой channel/model был выбран, что решил Planner, что изменил deterministic engine, какой `TurnAuthority` получил Narrator, какой был raw draft, что отклонил Validator, применялся ли repair/fallback/publication guard, какой final text опубликован, какой world state сохранён и что затем сделала память.