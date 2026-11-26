# Роадмап: перформанс хода и единый канон (тезисы/факты)

Статус: **в работе** (старт 2026-11-26, база `f30cd879`).
Документ — источник истины по этой программе работ. Каждый PR, закрывающий пункт,
обновляет таблицу «Журнал выполнения» (статус, PR/коммит, замер или тест).

## Журнал выполнения

Статусы: `todo` · `wip` · `done` · `dropped` (с причиной).

| ID | Пункт | Статус | PR / коммит | Подтверждение |
|---|---|---|---|---|
| D0 | Роадмап в репозитории | done | docs/perf-canon-roadmap | — |
| P0.1 | Бенч-сценарий и базовая линия (TTFB, s/ход, cached-токены) | wip | — | Пробы транспорта 2026-11-26, см. «Замеры». Полный бенч QA-410 ещё не прогнан |
| P0.2 | Reasoning effort по ролям | blocked | | Эксперимент r1–r3 (`c598356b` → `330ed866` → `3628a956`, 2026-11-04) откатили, причина не записана. Проба: на прозе `none` и `low` не различимы (≈4 с на 130 токенов); в живых вызовах reasoning — 100–350 токенов на вызов (≈2–7 с при ~50 ток/с). Нужен ответ владельца |
| P0.3 | Один `httpx.AsyncClient` на провайдер (keep-alive) | todo | | |
| P0.4 | Промпты, дружественные к кэшу: стабильный префикс, ключ кэша, лог cached_tokens | dropped | — | Транспорт не кэширует: 62/62 живых вызова и 14 проб с одинаковым префиксом 10k токенов дают `cached_tokens=0`, `cache_write_tokens=0`. `prompt_cache_retention`, `cache_control`, `instructions` не поддерживаются (400) или не влияют. Prefill дешёвый: 10k входа ≈ 1–2.5 с. Лог cached_tokens уже есть |
| P0.5 | Одна компиляция контекста за ход, без N+1 | todo | | |
| P1 | Стриминг с валидацией по предложениям | todo | | |
| P2 | Объединённый вызов intent+outcome | todo | | |
| K1.0 | Срез: позиции персонажей только из типизированных исполнителей, без перемещений из прозы (QA-410 A1) | done (ждёт PR) | `fix/k1-scribe-movement`, `da6cf514` | Красный→зелёный `test_scribe_prose_movement_cannot_relocate_an_npc`; полный suite 1399 passed, 1 падение платформенное (`test_qa304_process_ownership`, `
` на Windows, не связано). Отдельное поле `npc_movements` не понадобилось: автономное перемещение NPC уже типизировано в `PlannedWorldMove(actor_id, destination_location_id)`, перенос спутников — в `carry_participants` перехода |
| K1.0a | Убрать `movement` и `item_transfer` из схемы и промпта Scribe (их всегда отклоняют, это пустые выходные токены фонового вызова) | todo | | |
| K1 | TE2 как единственный писатель (FactClaim, нити, миграция, shadow) | todo | | |
| K2 | CanonView и типизированный валидатор (`fact_ref`) | todo | | |
| K3 | Снос писателей из прозы (Scribe-цепочка, guard'ы, curator), undo через события | todo | | |

### Замеры (2026-11-26, gpt-5.6-luna, `api.openai.com/v1/responses` с OAuth-токеном плана)

- **Кэш промптов недоступен.** Три одинаковых запроса подряд (префикс 10 418 токенов, один `prompt_cache_key`): `cached_tokens=0`, `cache_write_tokens=0` (поля `ttl_30m_tokens`/`ttl_12h_tokens` в ответе есть, но всегда 0). Так же во всех 62 живых вызовах профиля с токенами за 10–14.11.
- **Вход дешёвый, выход дорогой.** 10k входных токенов обрабатываются за 1.1–2.5 с; TTFT ≈ 1.5–2.5 с; генерация ≈ 40–50 ток/с. Нарратор 550–700 токенов выхода = 12–16 с; Scribe 400–1000 токенов = 20–40 с.
- **Вывод для приоритетов:** сокращение промптов и кэш-префиксы почти ничего не дают. Время хода определяется (1) числом последовательных вызовов, (2) объёмом выхода каждого, включая reasoning-токены, (3) тем, что игрок ждёт конца. Поэтому главные рычаги — P1 (стрим), P2 (меньше вызовов), компактные схемы ответа и reasoning по ролям (P0.2).
- В плане ниже утверждение «в outcome-промпте блок evidence повторяется дважды» неверно: второй `_indexed_evidence` индексирует только строки ввода игрока (`start=len(...)`).

---

## Context

Есть две хронические проблемы:
1. **Ход длится 30–60+ с** на быстрой облачной модели (gpt-5.6-luna через ChatGPT-транспорт).
2. **Фактчек по тезисам истории постоянно ломается.** Агенты закрывают каждый случай узкой заплаткой.

Разбор кода (HEAD `f30cd879`) показал, что обе проблемы архитектурные, а не набор багов.

### Диагноз 1: перформанс

На LLM уходит 90–95% времени хода. В QA-410 из 31–57 с на LLM пришлось 29–55 с.

Критический путь (`services/turn_saga.py:303-820`). Все вызовы идут строго последовательно, и игрок не видит ни байта до самого конца:

| # | Вызов | Где | Типичное время |
|---|---|---|---|
| 1 | intent (PLANNER) | `player_intent_interpreter.py:780` | 8–15 с |
| 2 | outcome (PLANNER) | `turn_outcome_resolver.py:1505` | 8–10 с |
| 3 | narrator: `generate_json` при наличии beat owner, т.е. **без стрима** | `authority_narration_pipeline.py:268` / `:160` | 6–13 с |
| 4 | validator: ждёт полный текст | `turn_authority_validator.py:248` | 5–14 с |
| 5 | repair-цикл: до 3 нарраторов и 3 валидаторов | `authority_narration_pipeline.py:454-572` | +10–20 с |

Что этому способствует:
- **Текст отдаётся игроку только после publish.** Вызов `yield narration.text` стоит в `turn_saga.py:758`, уже после `PostTurnDispatcher.schedule`. В отчёте QA-408 TTFB равен полному времени хода.
- **Reasoning `effort: "low"` уходит в каждом вызове** ChatGPT-транспорта, включая нарратора и валидатора (`config.py:40`, `llm_provider.py:522`).
- **Контекст компилируется дважды за ход** (`turn_saga.py:378` и `:530`). Per-turn блоки (SESSION AGREEMENTS, NPC-HELD ITEMS, THREADS) вставляются в первый system-message. Из-за этого `prompt_cache_key` (sha первых 2000 символов, `llm_provider.py:499`) и серверный prefix-cache почти не попадают.
- **Новый `httpx.AsyncClient` на каждый вызов** (`llm_provider.py:630,734,825,1198,1376`), без keep-alive.
- **N+1 запросы в `base_context_compiler.py:616`.**
- **Пост-ход гоняет 4–6 фоновых LLM-проходов** (Scribe и три guard'а поверх него, curator). Они держат barrier/SQLite и тормозят следующий ход.

### Диагноз 2: тезисы и факты

Факт об истории живёт примерно в 12 представлениях: SceneThesis, Fact, FactMemoryProfile, Belief, RelationshipAssertion, Event, NarrativeDetail, ProposedChange, TurnAuthority.established_state, SceneRuntimeState, физическое состояние и TE2. Почти все они идентифицируются **по тексту**.

Пять корневых причин. Каждая заплатка закрывает одно проявление одной из них, поэтому классы ошибок возвращаются.

- **RC1. Канон рождается из текста.**
  - ThesisCurator читает прозу и пишет даже тезисы типа `canon` (`thesis_curator.py:636,652`).
  - Scribe-цепочка устроена как петля: typed outcome расплющивается в текст (`post_turn_processor._typed_record` 158-184), затем LLM снова извлекает из него факты. Сверху навешано ещё три LLM-guard'а через monkeypatch (`runtime.py:90-160`).
  - `_auto_commit_proposals` (`post_turn_processor.py:264-279`) принимает NPC MOVEMENT, извлечённый из прозы. Это открытая регрессия QA-410 A1.
- **RC2. Идентичность по тексту.** Около дюжины механизмов сопоставляют по строке:
  - `fact_repo.normalize` / `find_current_by_key`;
  - SequenceMatcher 0.86 и пересечение токенов в curator;
  - 75% пересечения слов в `canon_semantics.evidence_supported`;
  - casefold-сопоставления в guard'ах;
  - матчер долга.
- **RC3. Нарратор и валидатор видят разный канон.**
  - Нарратор получает все тезисы и факты (`base_context_compiler.py:399-419, 523-544`).
  - Валидатор получает `established_state_lines` (`outcome_fact_authority.py:61-93`): последние 12 фактов по `updated_at`, только `public` (при дефолте `dm`), только с `source_turn_id`, только из ходов с завершённым world-шагом. Всё это рендерится свободным текстом.
  - **Тезисы в валидатор не попадают вообще.** Противоречие всему, что вне этого среза, пропускается по построению.
- **RC4. Несколько источников истины без сверки.**
  - Тезис `canon` против Fact.
  - `tension` против `SceneRuntimeState.active_conflict` против `Scene.tension`.
  - Позиция NPC и предмета хранится в трёх местах.
  - Undo откатывает факты через replay, а тезисы просто удаляет (`turn_undo_service.py:297`), не восстанавливая те, что были superseded.
- **RC5/RC6. Правила и инфраструктура противоречат друг другу.**
  - Семантические решения записываются в свободный текст и потом перепроверяются лексически.
  - `docs/cr-eliminate-lexical-semantic-hardcoding.md` разрешает пороги и пересечение токенов, ADR 009 их запрещает.
  - В проекте 22 `install()`-monkeypatch'а.
  - TE2 (ID-based фактовая модель: `FluentAssertion(subject_entity_id, semantic_type_id, value_json)`) так и стоит в режиме `legacy`.

## Принятые решения

- **Стриминг: валидация по предложениям.** Игрок видит только проверенные предложения, ремонтируется только сломанное предложение.
- **Канон: TE2 становится единственным источником истины.** Fact и canon-тезисы превращаются в проекции.

---

## Целевая архитектура тезисов (общее алгоритмическое решение)

### Принцип: разделить два разных понятия, которые сейчас слиты в «тезис»

1. **Факт (state).** Утверждение о мире, которое может быть истинным или ложным и может противоречить прозе.
   - Хранится как TE2 fluent или relation.
   - Ключ: `(subject_entity_id, semantic_type_id)`. Для relation ключ `(subject_id, type_id, object_entity_id)`.
   - Значение типизировано по `SemanticType.value_schema_json`: entity_id, enum или короткий литерал для открытых слотов.
   - Только факты подлежат фактчеку.
2. **Нить (thread / directorial note).** Сюда относятся intention, tension, secret, unresolved_beat, relationship_dynamic, visual_state и music_mood.
   - Это режиссёрская заметка, а не утверждение о мире. Противоречить ей проза не может, она может только её продвигать или закрывать.
   - У нити есть `thread_id`, привязка к entity_ids и жизненный цикл `open → advanced → resolved / abandoned`.
   - Жизненный цикл тоже пишется событиями TE2, тогда undo работает автоматически.
   - Тип `canon` у тезиса упраздняется: всё, что раньше было canon-тезисом, становится фактом.

### Алгоритм 1: единственный писатель канона — typed outcome хода

- Outcome-резолвер (вызов №2, позже объединённый планировщик, см. P2) возвращает два типизированных поля:
  - `established: list[FactClaim]`, где `FactClaim = {subject_ref, slot, value}`:
    - `subject_ref` берётся как `Literal` из каталога ID хода (каст, предметы, локации);
    - `slot` берётся как `Literal` из каталога `SemanticType`;
    - `value` проверяется по схеме слота.
  - `thread_updates: list[{thread_ref ∈ Literal[open thread ids] | new, op ∈ {open, advance, resolve, abandon}, related_refs}]`.
- Где эти поля материализуются:
  - на prepare они становятся provisional TE2-эффектами, по образцу журнала `ProvisionalTurnEffect` в `turn_outcome_materializer.py`;
  - на publish они коммитятся, на compensate откатываются.
- **Проза больше ничего не пишет в канон.** Удаляются:
  - MemoryScribe-цепочка для managed-ходов;
  - `narrator_memory_audit_guard`, LLM-части `quality_stabilization_guard`, `player_quote_provenance_guard`;
  - `_typed_record`;
  - запись тезисов в ThesisCurator.
- Curator, если он вообще остаётся, становится советником планировщика: раз в N ходов предлагает кандидатов в нити, а принимает их следующий outcome через `thread_updates`.

### Алгоритм 2: супersession и противоречие решаются ключом, а не текстом

- `set_fluent(subject_id, type_id, value)` детерминированно закрывает предыдущее значение того же ключа. Поля `valid_to_seq` и `superseded_by_event` уже есть в TE2-проекции.
- Кардинальность задаётся в `SemanticType`:
  - single: новое значение заменяет старое;
  - multi/relation: `add_relation` / `remove_relation` по `object_entity_id`.
- Текстовое сравнение не нужно нигде. Удаляются:
  - `fact_repo.normalize` / `find_current_by_key` как механизм идентичности;
  - `_semantic_key` в curator и `memory_operations`;
  - SequenceMatcher;
  - `evidence_supported`.
- «Не переписывать случившееся» (бан №2) формулируется по ID. Новый `FactClaim` на ключ с завершённым (completed-step) значением в этом ходу допустим только как явный переход, то есть шаг движка, а не тихая перезапись. Это проверяется детерминированно на prepare.

### Алгоритм 3: один CanonView на ход, общий для нарратора и валидатора

- Новый модуль `services/canon_view.py`. Он строит **один раз за ход**, после prepare, снимок фактов из TE2-проекции.
- Отбор по графу, а не по `updated_at[-12:]`:
  1. факты о сущностях, присутствующих в сцене, о локации сцены и о предметах при присутствующих;
  2. факты, затронутые эффектами этого хода;
  3. pinned-факты и факты, на которые ссылаются открытые нити;
  4. бюджет по приоритету слота с детерминированным tie-break.
- Каждому факту присваивается короткий ref (`F1…Fn`), каждой нити свой ref (`T1…`).
- Нарратор и валидатор получают **один и тот же** CanonView. `established_state: list[str]` в `TurnAuthority` заменяется ссылкой на CanonView, `outcome_fact_authority.established_state_lines` удаляется.
- Схема ответа валидатора:
  - `findings: list[{ban ∈ Literal[4 бана], sentence_ref ∈ Literal[anchors], fact_ref ∈ Literal[F-refs] | null, step_ref | null}]`;
  - finding типа `canon_conflict` без `fact_ref` или `step_ref` недопустим по схеме.
  - Так валидатор не может «догадываться», а всё, что он видит, он может и проверить (RC3 закрыт).
- Visibility (`dm` / `public`) влияет только на то, что NPC может знать. Для фактчека прозы нарратора проверяются все факты, которые видит нарратор.

### Алгоритм 4: undo и replay через события

- Undo хода переводит его `TruthEventRecord` в статус `reverted` и пересобирает проекцию.
- Нити и факты откатываются одинаково, включая восстановление superseded (RC4 закрыт).
- `ActiveCanonReplay` по ProposedChange для этих доменов удаляется.

### Единственное хранилище физики

- Позиции NPC и предметов берутся только из TE2 (`core.entity.location`, `core.item.position`), которые пишут executors и materializer.
- Строки Character/Item остаются кешем-проекцией, обновляемой в той же транзакции. Отдельного писателя у них нет.

---

## Роадмап (маленькие PR, каждый мержится, по AGENTS.md)

### Фаза P0. Измерить и быстрые выигрыши по перформансу (независимо от канона)

1. **Бенч-сценарий.**
   - Зафиксировать базовую линию: 6 ходов QA-410 вручную, `llm-performance.jsonl`, `stage_timings`, `scripts/soak_metrics.py`.
   - Метрики: TTFB (время до первого проверенного предложения), полное время хода, число вызовов, input-токены и cached-токены.
2. **Reasoning effort по ролям.**
   - В `config.py` вместо одного `LLM_REASONING_EFFORT` завести per-role настройку (NARRATOR и NARRATION_VALIDATOR: `none` или `minimal`; PLANNER: `low`).
   - Применять её в `llm_provider.py:522` через роль из `role_model_router`.
   - Проверить качество на бенче.
3. **Один `httpx.AsyncClient` на провайдер.**
   - Lifespan-клиент с keep-alive вместо `async with httpx.AsyncClient(...)` в каждом вызове (`llm_provider.py:630,734,825,1198,1376`).
   - `timed_client` сохранить для телеметрии.
4. **Cache-friendly промпты.**
   - Стабильный префикс (правила, мастер-блок, каталог) ставить первым. Per-turn блоки (agreements, NPC items, threads, evidence) переносить в конец.
   - `prompt_cache_key` считать по хешу стабильного префикса.
   - Логировать `cached_tokens` из ответа.
5. **Одна компиляция контекста за ход.**
   - `_recompile_narrator_context` (`turn_saga.py:530`) заменить на дельту поверх результата `turn_saga.py:378`.
   - Убрать N+1 в `base_context_compiler.py:616`: батч `get_by_ids`.

Ожидание: −8–15 с на ход, без изменения семантики.

### Фаза P1. Стриминг с валидацией по предложениям

1. Нарратор всегда стримит.
   - Для beat-owner режима, где сейчас `generate_json` с GrantedNarration, перейти на стрим прозы. Типизированные поля beat выдаются отдельно: либо их владелец — outcome, либо они идут в короткий хвостовой JSON.
2. Сегментация потока на предложения.
   - Это техническая сегментация, а не игровая логика. Переиспользовать существующий механизм sentence anchors (`turn_authority_validator`).
3. Валидатор переходит на инкрементальный режим.
   - Батчи по 1–3 предложения валидируются **параллельно** с продолжением генерации.
   - На вход идут CanonView, проверенный префикс и новые предложения.
4. Проверенные предложения отдаются клиенту.
   - Протокол SSE: `{type: sentence}`, `{type: final}`, `{type: fallback}`.
   - При провале предложение ремонтируется точечно (существующий sentence-repair, `authority_narration_pipeline.py:543`). Генерация продолжается от исправленного префикса.
5. Publish (commit) и пост-ход остаются в конце. Сначала уходит `final` клиенту, потом commit. Для фронта нужно обработать новый протокол (`src/frontend`).

Цель: первое предложение ≤ 12 с, медиана полного хода ≤ 25 с.

### Фаза P2. Объединённый планировщик

- intent (вызов №1) и outcome (вызов №2) объединяются в один PLANNER-вызов с объединённой схемой. Поля intent являются подмножеством входа outcome.
- Существующие шорткаты (`combined_dialogue`, `resolve_known_travel`) становятся частными случаями.
- Ожидание: −8–10 с.
- Делать после P0, чтобы мерить на чистой базе. Подтвердить, что качество intent на бенче не падает.

### Фаза K1. TE2 в main как единственный писатель

1. Довести ADR 009 до статуса Accepted.
   - Убрать противоречие с `docs/cr-eliminate-lexical-semantic-hardcoding.md`: запретить пороги и пересечение токенов как идентичность.
   - Добавить правило: новые `install()`-monkeypatch'и запрещены.
2. Расширить TE2.
   - `SemanticType` получает кардинальность и `value_schema`.
   - Добавить эффекты нитей `open_thread`, `advance_thread`, `resolve_thread`, `abandon_thread`, проекция `ThreadProjection`.
3. Outcome-схема получает `established: list[FactClaim]` и `thread_updates` (`turn_outcome_resolver.py`, модели в `models/turn_authority.py`). Materializer пишет их provisional TE2-эффектами.
4. Миграция legacy.
   - Fact с `FactMemoryProfile.subject_entity_id` переходят в fluent.
   - Fact без якоря становятся «unanchored note»: только контекст нарратора, без фактчека.
   - `canon`-тезисы переходят в fluent, если сущность и слот резолвятся, иначе в note.
   - Остальные тезисы становятся нитями.
5. Shadow-режим на 1–2 плейтеста: старый и новый пути параллельно, расхождения в лог. Затем переключение.

### Фаза K2. CanonView и типизированный валидатор

1. `services/canon_view.py`: отбор по графу и refs, используется и в `base_context_compiler`, и в `validator_payload`.
2. Схема валидатора `turn_authority_validator.py` со ссылками `fact_ref` и `sentence_ref` в виде `Literal`.
3. Удалить:
   - `outcome_fact_authority.established_state_lines`;
   - секции «Active Scene Notes» и «Campaign Facts» в контексте (их заменяет CanonView);
   - `NarrationValidator.protect_confirmed_speaker`.

### Фаза K3. Снос старых писателей (код становится тоньше)

1. Удалить для managed-ходов:
   - MemoryScribe-цепочку и `_auto_commit_proposals`;
   - `narrator_memory_audit_guard`, LLM-части `quality_stabilization_guard`, `player_quote_provenance_guard`;
   - `post_turn_structured_receipt_guard`, `truth_engine_relationship_receipts` (лексический долг), `_deterministic_debt_fallback`;
   - тексто-матчеры curator.
   - Это же убирает 4–6 фоновых LLM-вызовов и contention на barrier.
2. Undo переходит на revert событий, `turn_undo_service.py:297` и `ActiveCanonReplay` для этих доменов удаляются.
3. Свести `SceneRuntimeState.active_conflict` и `Scene.tension` к нитям (или к их проекции). Выбор plot source в `turn_authority_service.py:105-130` делать по `thread_id`.
4. Тесты, которые закрепляли поведение удалённых guard'ов, переписать на типизированный исход и состояние TE2.

Порядок: **P0 → P1 ∥ K1 → K2 → P2 → K3.**
- P0 и K1 независимы и могут идти параллельно разными агентами.
- K3 даёт и качество, и перформанс фона.

## Критические файлы

- **Перформанс:**
  - `services/turn_saga.py`
  - `services/authority_narration_pipeline.py`
  - `services/turn_authority_validator.py`
  - `providers/llm_provider.py`
  - `services/role_model_router.py`
  - `config.py`
  - `services/base_context_compiler.py`
  - `services/context_compiler.py`
  - `services/turn_intent_pipeline.py`
  - `services/player_intent_interpreter.py`
  - `services/turn_outcome_resolver.py`
  - `api/turns.py`
  - фронтенд-клиент стрима
- **Канон:**
  - `db/truth_engine_table.py`, `services/truth_engine*.py`
  - `services/turn_outcome_materializer.py`
  - `models/turn_authority.py`
  - `services/outcome_fact_authority.py`
  - `services/thesis_curator.py`
  - `services/post_turn_processor.py`
  - `services/memory_scribe.py` и guard'ы из `runtime.py:90-160`
  - `services/turn_undo_service.py`
  - `services/active_canon_replay.py`
  - `db/repositories/fact_repo.py`
  - `docs/adr/009-*.md`
  - `docs/cr-eliminate-lexical-semantic-hardcoding.md`
  - `docs/TRUTH-TRANSITION-MATRIX.md`
- **Переиспользовать:**
  - `ProvisionalTurnEffect`-журнал (materializer);
  - `StructuredReceiptEventCompiler` (`truth_engine_receipts.py`);
  - sentence anchors и sentence-repair;
  - `performance_telemetry_guard` и `turn_performance.mark_phase`;
  - `name_identity_contract` для каталога ID.

## Verification

- **Тесты:** `cd src/backend && PDM_TEXT_PROVIDER=local python -m pytest`. Сначала красный тест на каждый PR.
  - TE2 supersession по ключу: два `set_fluent` на один ключ дают одно current-значение.
  - Undo восстанавливает superseded факт и нить.
  - NPC MOVEMENT из прозы не меняет позицию (регрессия QA-410 A1).
  - Валидатор ловит противоречие факту, который раньше выпадал из среза: факт с `dm`, 13-й по давности, из session zero.
  - Схема валидатора отвергает `canon_conflict` без `fact_ref`.
  - Стрим отдаёт только проверенные предложения. Сломанное предложение ремонтируется, остальные не трогаются.
- **Мутационный score ядра не падает** (engineering-rules).
- **Перформанс:** ручной плейтест сценария QA-410 до и после каждой P-фазы. Сравнение `llm-performance.jsonl`, `stage_timings`, TTFB, cached-токенов. Отчёт: s/ход, $/ход, тренд input-токенов, число ремонтов.
- **Канон:** shadow-прогон K1 с логом расхождений. Ручной плейтест по `docs/playtest-protocol.md` с проверкой того, что каждая противоречащая канону фраза ловится с `fact_ref`.
