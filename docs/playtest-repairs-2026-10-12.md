# Починки по итогам плейтеста 12.10.2026

Ветка `fix/playtest-2026-10-12`. Источник — агентский плейтест (74 опубликованных
хода кампании «Playtest-агент 12.10», методология causal trace из
`docs/playtest-protocol.md`). Первоначальные хотфиксы ниже позднее прошли проверку на общность правил.
Часть ограничений пересмотрена после негативных тестов; актуальные решения описаны ниже.

> **Обновление после архитектурного review.** Исходный хотфикс
> `scene_state → cardinality=single` откачен: наблюдения с одинаковой
> текстовой подписью могут относиться к разным объектам. Теперь решение
> `single/multi` остаётся за семантическим слоем до появления типизированных
> слотов. Замена фактов одной сущности ограничена тем же предикатом.
> Слепое объединение персонажей только по `slot_id` также убрано.
> API `/regenerate` изменён на undo+replay последнего активного хода.
> Подробности и оставшиеся ограничения — в
> `docs/playtest-fixes-review-2026-10-12.md` и PR #207.
>
## Исправлено

### 1. Слоты scene_state: одно значение на (scene, subject, predicate)

В плейтесте четыре противоречивых факта «ритм стука под настилом» сосуществовали
как активные: кардинальность выбирал LLM-скриба и голосовала `multi`.
`FactRepository.apply_change` теперь принудительно `single` для
`memory_kind="scene_state"` независимо от голоса скрибы; история сохраняется
через `superseded_by`, другие предикаты того же субъекта не затрагиваются.

- `src/backend/app/db/repositories/fact_repo.py`
- тест: `test_canon_versioning.py::test_scene_state_single_slot_survives_a_multi_cardinality_vote`

### 2. Гонка публикации: «Prepared physical world changed before turn publication»

Fingerprint мира (`TurnWorldFrame`) захватывался после подготовки хода, а
post-turn джобы (MOVEMENT-presence у скрибы, commit регистрарера) могли
автокоммитить те же строки в окне capture→publish — сага компенсировала ход,
который шёл по правилам. Решение — кампейн-скоупный write-barrier: сага
держит барьер от захвата кадра мира до commit публикации; post-turn джобы
берут его только вокруг короткой durable-секции.

- Новый модуль `src/backend/app/services/campaign_write_barrier.py`
  (per-campaign `asyncio.Lock`, FIFO).
- `turn_saga.py`: барьер от `TurnWorldFrame.capture` до commit публикации
  (освобождение в `finally`, включая compensate-путь).
- `post_turn_processor.py`: барьер только вокруг durable-записи (commit
  регистрарера; persist+auto-commit предложений). LLM-извлечение осталось
  вне барьера — память ждёт хода, а не ход памяти.
- тесты: `tests/test_campaign_write_barrier.py`.

### 3. Двойники персон

Три детерминированных винта, без лексических синонимов
(см. `docs/cr-eliminate-lexical-semantic-hardcoding.md`):

- `entity_identity.resolve_character_candidates`: рольная ветка теперь
  двухступенчатая — сначала временные идентичности, затем точное хранимое
  роль promoted-персоны (нарратор продолжает звать «конторщиком» уже
  названного человека — прежний человек, а не двойник). Временные матчатся
  первыми, чтобы промоушен не возвращал неоднозначность.
- Голый ролевой designator («смотритель причала») привязывается к уникальному
  владельцу `slot_id` цели; нет владельца или несколько — fail-closed.
- Анти-твин-гейт в `entity_registrar`: однословная падежная форма живого
  имени («Юнда» при живом «Юнд») больше не создаёт сущность — fail-soft skip.

- тесты: `tests/test_entity_identity_resolution.py`,
  `test_entity_registrar.py::test_inflected_surface_of_a_live_person_never_mints_a_twin`

### 4. Потеря годного текста на safe_fallback (beat tolerance)

Кандидат, у которого нарушена только инициативная реплика (beat grant не
«съеден» грамматически), раньше выбрасывался до валидатора. Теперь четырёх
запретов вердикт решает публикацию: прошедший валидатор кандидат публикуется
с `reason=beat_unhonored` и `beat=None`; проекция — только когда и валидатор
против. Это основной источник 9/47 safe_fallback в плейтесте.

- `authority_narration_pipeline.py`, `turn_authority_validator.py`
- обновлённый pinned-контракт + новый тест в
  `test_stabilization_narration_failure_containment.py`

### 5. Многoшаговые ходы: наблюдаемость вместо молчания

Компилятор плана строго 1:1 с замороженным контрактом, так что «потеря
второго действия» рождается выше (речевой фильтр намерений, extraction cap).
Добавлено: `action_coverage` (requested_actions vs planned_steps) в audit-
словаре плана, аудит-запись `speech_filtered_actions`, и правило цепочек в
промпт интерпретатора. Молчаливое усечение теперь локализуется по trace.

- `turn_intent_pipeline.py`, `player_intent_interpreter.py`

### 6. CJK-мусор в опубликованной прозе

`consistent_script` чинит только смешанные слова; цельные CJK/Hangul/Thai
мусорные слова доходили до поверхности. Новый детектор
`foreign_script_intrusions` дисквалифицирует такой кандидат (как дословный
повтор) и называет ремонт-модели слова-виновники; повторный гейт на
отремонтированном тексте.

- `script_consistency.py`, `turn_authority_validator.py`, `authority_narration_pipeline.py`
- тесты: `test_script_consistency.py`,
  `test_stabilization_narration_failure_containment.py::test_foreign_script_garbage_goes_to_repair_not_the_surface`

## Осознанно не в этой ветке (роадмап)

- **Физические объекты как сущности первого класса** (лодка, безымянный
  сошедший): телепорты/дубли предметов — следствие модели мира, где объекты
  не имеют presence. Это ADR-уровень (рядом с `adr/008`), не хотфикс.
- **Семантическая дедупликация сверх точной строки** («Смотритель причала» ≠
  «ночной причальный конторщик» лексически): требует LLM-арбитража в
  registrar с доказательной связкой; детерминированная часть (slot-holder
  bind) сделана, лексический словарь не вводится по контракту CR.
- **`POST /turns/{id}/regenerate`** — дописывает мусорный ход вместо замены;
  отдельная правка API-семантики.
- **`/talk` как HTTP-слой** — реализован только в CLI.
- **Escort/NPC-сопровождение** и **talk-affordance** в UI — продуктовые
  фичи из предложений, не баги.
- **Разделение ролевых моделей по качеству** (planner ≠ narrator ≠ scribe
  по умолчанию) — конфиг-политика, требует live-оценки, не детерминизм.

## Проверка

- Целевые наборы: identity (70), facts/memory (46), saga/post-turn/intent
  (62), публикация/four-bans/script (39) — зелёные.
- Полный backend-сьют на HEAD ветки: **1001 passed** (6:45).
