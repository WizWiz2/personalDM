# Карта документации

Документы PersonalDM делятся на normative product contracts, primary current-state contracts и historical/research material. Для вопроса «что реально делает текущий `main`?» код + persisted evidence имеют приоритет над текстом документа.

## Нормативные продуктовые документы

- [`product-foundation.md`](product-foundation.md) — зачем существует продукт и какие свойства обязательны.
- [`MVP-SPEC.md`](MVP-SPEC.md) — текущий проверяемый vertical slice / Definition of Playable.
- принятые ADR — конкретные решения, которые могут уточнять общие правила.

## Primary current-state map

У каждой production subsystem есть один основной current-state документ. Остальные документы могут давать детали, но не конкурируют за ownership.

| Production subsystem | Primary current-state document |
| --- | --- |
| production turn/runtime causal order, observability | [`runtime-transparency.md`](runtime-transparency.md) |
| Session Zero + character handoff/opening | [`session-zero.md`](session-zero.md) |
| Scene / Location / physical presence / movement | [`scene-location-presence.md`](scene-location-presence.md) |
| NPC identity / reconciliation / materialization | [`npc-identity-and-materialization.md`](npc-identity-and-materialization.md) |
| `/DM` / `/OOC` meta channel | [`meta-channel.md`](meta-channel.md) |
| model role/provider routing | [`model-role-routing.md`](model-role-routing.md) |
| context selection/enrichment/token budget | [`architecture/context-pipeline.md`](architecture/context-pipeline.md) |
| typed inter-agent outcome authority | [`architecture/interagent-turn-authority.md`](architecture/interagent-turn-authority.md) |
| Narrator validation/repair/publication | [`architecture/narration-pipeline.md`](architecture/narration-pipeline.md) |
| facts/beliefs/relationships/theses and memory lifecycle | [`architecture/personal-dm-runtime.md`](architecture/personal-dm-runtime.md) |
| persistence / migrations / undo / saga recovery | [`persistence-recovery.md`](persistence-recovery.md) |
| runtime/provider/environment configuration | [`configuration-reference.md`](configuration-reference.md) |
| visual portrait/cover/scene generation | [`visual-generation.md`](visual-generation.md) |
| deterministic CI / local real-model tests / soak | [`playtest-protocol.md`](playtest-protocol.md) |

`TESTING-STRATEGY.md` и `TRUTH-TRANSITION-MATRIX.md` являются detailed verification references для `playtest-protocol.md`; `runtime-provider-management.md` — operational detail для configuration/model routing; `gemma-narrator-stability.md` — model-specific note для narration pipeline.

## Что обязан содержать primary current-state contract

Каждый такой документ отвечает как минимум на пять вопросов:

1. кто владеет решением;
2. какие invariants обязательны;
3. что является persisted evidence;
4. как выглядит failure semantics;
5. какими tests/endpoints это проверяется.

Если эти ответы расходятся с текущим кодом/runtime trace, документ считается drifted и должен быть исправлен.

## Разрешение противоречий

Для **продуктового смысла**:

1. принятый ADR;
2. `product-foundation.md`;
3. `MVP-SPEC.md`;
4. proposed ADR / amendments.

Для **текущего поведения сборки**:

1. persisted evidence и код `main`;
2. `GET /api/debugger/runtime` / `runtime_manifest()`;
3. primary current-state документ из таблицы выше;
4. detailed implementation notes.

## Текущий продуктовый фокус

PersonalDM — **systemless narrative RPG engine**, не CRPG/rules engine.

Главные границы:

- SQLite — canonical production storage локального режима;
- LLM не является единственным источником истины для placement, movement, NPC identity или structured consequences;
- Narrative prose публикуется поверх `TurnAuthority`, а не создаёт truth постфактум;
- недоступные NPC сведения исключаются из actor-scoped context;
- post-turn memory failure не откатывает опубликованный ход;
- `/DM` — read-only meta channel с отдельной publication sanitization boundary;
- visual generation — best-effort derived artifact;
- deterministic CI проверяет deterministic code, а реальные semantic model transitions проверяет отдельный local model-contract suite.

## Обязательная прозрачность live regression

Для плохого turn должно быть возможно установить:

input/channel → actual model role → Planner → structured execution → `TurnAuthority` → raw Narrator → Validator/repair → publication mode/final text → persisted world → post-turn memory.

Primary evidence: `/api/campaigns/{id}/debugger/turns/{assistant_turn_id}`. Runtime fingerprint: `/api/debugger/runtime`. Канонический порядок playtest — [`playtest-protocol.md`](playtest-protocol.md).

## История и исследования

- `docs/amendments/` — исследовательские дополнения, не normative сами по себе;
- proposed ADR — обсуждаемые решения;
- старые Round-specific contracts, superseded текущими primary docs/tests, остаются в git history, а не в current-state backlog.
