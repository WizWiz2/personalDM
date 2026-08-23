# Спецификация MVP
## Текущий проверяемый вертикальный срез

**Статус:** канонический MVP contract  
**Версия:** 0.3  
**Дата:** 24 августа 2026

## Цель

Проверить, что Campaign Truth Engine делает длинную локальную AI-кампанию одновременно:

1. устойчивой к распаду причинности;
2. прозрачной для диагностики;
3. достаточно качественной по тексту, чтобы в неё хотелось играть.

MVP больше не считается успешным, если он только «правильно хранит state», но Narrator систематически публикует деревянные fallback-заглушки.

## Основной пользовательский сценарий

Пользователь:

1. создаёт кампанию;
2. проходит разговорную Session Zero;
3. сразу получает автоматический opening post;
4. играет естественными действиями и репликами без обязательной командной грамматики;
5. перемещается между Location/Scene;
6. разговаривает с присутствующими NPC;
7. продолжает игру после перезапуска;
8. использует `/DM` для read-only вопроса мастеру;
9. при проблеме открывает debugger/trace и видит причинную цепочку;
10. при желании генерирует локальные портреты/обложку/scene art.

## Product boundary

MVP — **systemless narrative RPG**.

Не требуются:

- HP;
- levels;
- характеристики;
- dice checks;
- combat engine;
- инвентарь с rules mechanics;
- реализация конкретной настольной системы.

Эти вещи допустимы только как отдельный будущий rules layer.

## Session Zero

Session Zero должна быть обычным разговором, а не анкетой по полям.

Минимальный materialization contract:

- узнаваемый setting/genre/world anchor;
- герой и его концепт;
- практическая первая цель;
- starting Location;
- конкретная starting situation.

Агент может безопасно достроить недостающие технические детали сам, если пользователь делегирует выбор или явно просит начинать.

### Handoff acceptance

При завершении:

- final Session Zero message не содержит нового вопроса;
- `ready_to_finalize=true`;
- создаются герой, Location и первая active Scene;
- создаётся ровно один system-owned opening assistant turn;
- opening появляется в Play UI без пользовательского «Начинаем»;
- retry finalize не дублирует opening.

## Narrative turn contract

```text
persist user input
→ compile context
→ structured TurnAuthorityPlanner
→ deterministic action/transition execution
→ build TurnAuthority
→ generate Narrator draft
→ repetition + deterministic agency checks
→ Narration Validator
→ optional targeted repair
→ accepted prose OR deterministic presentation fallback
→ materialize allowed structured outcome
→ persist/commit
→ enqueue post-turn memory jobs
```

### Authority ownership

- protagonist voluntary actions/dialogue — только human input;
- resolution — Planner;
- placement/movement — deterministic engine;
- new NPC introduction — typed plan + deterministic materialization;
- prose — Narrator;
- prose correctness against authority — Validator/guards;
- background long-term memory extraction — Scribe/Registrar/Curator pipeline.

Narrator не может менять уже выполненный structured outcome.

## Scene и Location

Scene и Location — разные сущности.

MVP обязан поддерживать:

- одну authoritative current Scene у кампании;
- Scene → Location binding;
- `player.current_location_id` синхронно active Scene Location;
- structured forward movement;
- return/revisit по уже известной Location;
- fail-closed при реальной неоднозначности destination;
- отсутствие silent NPC teleport;
- SceneTransition evidence для movement.

Bootstrap/служебная Location не должна случайно считаться тем же visited destination только из-за похожего имени.

## NPC identity и presence

MVP обязан различать:

- уже существующего физически присутствующего NPC;
- known absent NPC;
- явно planned new NPC;
- generic temporary contact;
- common-noun premise, который не является новым персонажем.

Narrator не может самостоятельно создать speaker, которого нет в authority.

Explicit generic contact имеет бинарный contract:

- affirmative responder → typed temporary NPC + materialization;
- explicit no-contact → никакой сущности не создаётся.

Состояние «в prose кто-то ответил, но typed responder отсутствует» недопустимо.

## Player agency

Запрещено приписывать герою игрока без explicit input:

- новое действие;
- следующую реплику;
- решение;
- план;
- belief;
- consent/refusal;
- страх/радость/влечение и другие эмоциональные conclusions.

Особый deterministic regression guard должен ловить direct-speech inversion: реплика игрока не может стать репликой мира/NPC в том же turn без явно разрешённого echo.

## Narrator quality

Narrator acceptance состоит из двух независимых частей.

### Functional

- authority preserved;
- player agency preserved;
- movement/result отражён;
- нет technical leakage;
- ordinary valid turns не падают в `safe_fallback`.

### Prose

Live smoke оценивает минимум:

- local coherence;
- global coherence;
- continuity;
- scene-specific detail;
- atmosphere without generic filler;
- language variation;
- natural Russian prose;
- dramatic utility.

Opening должен быть крупнее обычного turn и реально погружать в стартовую ситуацию.

Raw draft и published text оцениваются отдельно.

## Narration failure semantics

Semantic prose violation не должен откатывать уже корректный truth state.

Pipeline пытается repair; если безопасной prose нет, публикуется deterministic authority projection.

Такой режим маркируется degraded (`safe_fallback`/related validation status) и не считается художественным PASS.

Successful movement fallback не имеет права утверждать «Пока ничего заметно не меняется».

## Actor-scoped knowledge

Для выбранного acting NPC context строится из доступных ему сведений.

NPC не должен получать private facts/beliefs другого персонажа только потому, что они существуют в общей кампании.

Objective canon не должен автоматически создаваться из субъективной NPC speech.

## Memory

После принятого assistant turn фоновые durable jobs обрабатывают память.

MVP хранит минимум:

- facts;
- beliefs;
- relationships;
- events;
- goals;
- scene theses;
- transient narrative details;
- provenance/source turn.

Failure post-turn job не отменяет опубликованный turn. Job можно диагностировать/retry отдельно.

## `/DM` / meta channel

Meta dialogue:

- read-only;
- не вызывает normal narrative Planner/execution/memory pipeline;
- может объяснять state и ошибки;
- не должен продолжать fiction как выполненное действие;
- не должен публиковать внутренние system/control blocks пользователю.

## Debugger и trace

MVP обязан иметь causal evidence, а не только raw chat history.

Текущие endpoints:

```text
GET /api/campaigns/{campaign_id}/debugger
GET /api/campaigns/{campaign_id}/debugger/turns/{assistant_turn_id}
GET /api/campaigns/{campaign_id}/debugger/trace
GET /api/debugger
```

Per-turn investigation должна позволять пройти:

```text
input
→ routing
→ planner
→ execution
→ authority
→ narrator
→ validator/repair
→ publication
→ materialization
→ memory
```

Known transparency gaps перечислены в `runtime-transparency.md` и сами являются частью MVP hardening backlog.

## Модели

Локальный default:

```text
Narrator / Session Zero / Game Master: gemma4:e4b
Control roles: qwen2.5:7b
```

Фактическая модель конкретного turn определяется persisted campaign config/telemetry, а не только current default.

Control roles strict и не должны молча fallback’иться на creative primary model при structured failure.

## Изображения

При `IMAGE_ENABLED=true` ComfyUI используется как best-effort локальный atmosphere layer.

MVP поддерживает:

- portrait character;
- campaign cover;
- scene generation;
- фоновые visual tasks, не блокирующие truth state.

Image generation failure не отменяет Session Zero или игровой turn.

## Persistence

SQLite — production storage локального MVP.

Хранятся, среди прочего:

- campaigns/provider config;
- turns;
- scenes/participants/locations;
- generation runs;
- action sequences/scene transitions;
- narration validation audit;
- entities/characters;
- facts/beliefs/relationships/events;
- theses/narrative detail;
- proposed changes;
- post-turn jobs;
- visual assets/metadata.

Undo должен отменять связанную narrative pair и структурные последствия, а не только скрывать текст.

## Критерии приёмки вертикального среза

Обязательные:

- свежая campaign проходит Session Zero без questionnaire loop;
- terminal handoff не оставляет вопроса;
- automatic opening появляется без user turn;
- герой не получает invented agency;
- forward movement и return/revisit сохраняют correct Location/Scene;
- существующий NPC не дублируется;
- explicit new contact materialize только при разрешённом outcome;
- NPC actor context не содержит недоступную память;
- ordinary narration не систематически падает в safe fallback;
- raw-vs-published evidence позволяет локализовать prose regression;
- campaign переживает restart;
- post-turn memory failure не ломает accepted turn;
- debugger обнаруживает state invariant errors;
- visual generation не влияет на truth correctness.

## Не является целью текущего MVP

- rules engine конкретной системы;
- tactical combat;
- multiplayer/server mode;
- PostgreSQL;
- полноценное branching/merge;
- perfect semantic verification любого предложения;
- абсолютная visual consistency;
- автоматическая генерация scene art на каждом ходу;
- скрытая магия, которую невозможно объяснить через persisted evidence.

## Definition of playable

MVP считается playable, когда несколько последовательных живых сессий можно провести без ручного «подталкивания» движка, при этом:

- causality остаётся структурно верной;
- Narrator пишет связно и атмосферно;
- игрок сохраняет agency;
- debugger позволяет объяснить любой серьёзный FAIL по первому неправильному boundary.
