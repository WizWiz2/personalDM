# Исправления после ручного прогона на CPU

## Поведение

- Рассказчик получает утверждённые ответы NPC на каждый вопрос. Ранее сокращённый
  контекст терял `addressed_response`, хотя валидатор требовал покрыть его ответы.
- Только выполненные шаги дают результаты. Пропущенный осмотр больше не хранит
  запланированный результат; валидатор отдельно запрещает описывать даже отрицательный
  результат такого осмотра.
- Зависимость шагов сохраняется от извлечения намерения до исполнения. После препятствия
  можно выполнить только явно независимое, безопасное наблюдение без перехода. Осмотр
  по дороге или в пункте назначения зависит от успешного перехода.
- Утверждённые указания NPC сохраняются в снимке ответа: место, промежуточные ориентиры
  и дословное свидетельство. Для известного места без ребра они разрешают попытку
  исследования пути после успешного внешнего разрешения действия. Закрытые и
  необнаруженные существующие проходы сохраняют ограничения. Слова NPC не становятся
  доказанным путешествием через быстрый путь планировщика.
- Для старых ответов распознаются цельные названия с русским склонением; отменённые
  ответы и ответы из другого исходного места исключены. Совпадение по части слова запрещено.
- Запасная реплика объединяет ответы в одну речь NPC и убирает повторные подписи
  его текущего имени и прежнего временного обозначения.
- API возвращает текущий этап, время его начала, последний ответ модели и число
  полученных фрагментов. Интерфейс показывает длительность всего хода и этапа.
  Эти диагностики живут в процессе; долговечные статусы и восстановление после
  потери воркера остаются отдельным механизмом.
- Локальная очередь резервируется на весь активный ход. Фоновая память продолжает
  работу после публикации или завершения неудачного хода. Уже начатый запрос памяти
  не прерывается. Намеренная отсрочка памяти не считается тайм-аутом очереди.

## Скорость и ограничения измерения

Четвёртый ход старого прогона завершился за 66 минут 27 секунд. Раннее сообщение
о зависании было ошибочным. В журнале девять вызовов этого хода суммарно заняли
66,42 минуты, из них 18,26 минуты — ожидание модели. Две наиболее дорогие стадии:
проверка семантического владельца действий (16,37 минуты, включая 8,44 в очереди)
и извлечение намерения (10,21 минуты, включая 6,14 в очереди).

Исправление очереди устраняет вклинивание новых запросов памяти между стадиями.
Дополнительные проверки владельца действий запускаются при противоречивом разборе,
поэтому удалять их как дубликаты небезопасно. Фактическое ускорение полного хода
после исправлений ещё не измерено; вычисление на CPU остаётся медленным.

## Проверки

184 проверки выбранных подсистем прошли, включая новые регрессии ручного прогона,
составные действия, откат, направления NPC, покрытие вопросов, очередь и жизненный
цикл генерации. TypeScript и сборка Vite прошли. Сервер перезапущен; публичный API
возвращает обновлённую схему с `progress` и сохранённую завершённую генерацию.

Проверки логики используют существующие подмены модельного транспорта. Для побочных
запросов памяти в тестах применён секундный бюджет контрольного вызова, чтобы не
превращать проверку исполнения и отката в часовой прогон реальных моделей. Это не
подтверждает литературное качество новых ответов. Полный повторный прогон с LLM
и двадцать завершённых дополнительных ходов пока не выполнены.

## Contract repairs after the second CPU session (2026-10-02)

- New directions select `speech_index` in the approved spoken response. The engine binds the evidence to that utterance, avoiding a separately generated quotation. Historical directions remain in active source turns at the route origin; movement outcomes cannot reissue them as a new response. Old records with literal evidence still validate.
- Unresolved destination identity produces `clarification_required`. The intent authorizes no executable actions. The pipeline skips outcome resolution and director selection; the authority skips NPC initiative and model narration. Publication returns the clarification directly, without post-turn memory extraction or rhythm advancement.
- Frozen actions preserve `actor_role`. The compiler binds `actor_id` and `actor_name` from campaign identity, separately from the response speaker. Both fields survive durable execution and narrator compaction. Migration `b9d0e1f2a3b4` adds nullable columns for historical actions and accepts columns precreated by the ORM. Historical actors are not guessed.
- Indexed responses declare `delivery=spoken|nonverbal`. Spoken words exclude labels and stage directions; nonverbal responses are rendered as observable acts without quotation marks. The native output schema requests an explicit delivery type, while old stored responses retain their spoken default.
- Literal inclusion of an approved indexed answer is deterministic question-coverage evidence. Paraphrases still require reviewer-selected evidence; other semantic violations remain errors.

These changes use typed ownership, references and lifecycle state. They do not classify episodes using character names, campaign keywords or lists of player phrases. Semantic accuracy of model-authored words, direction extraction and delivery classification still requires live play validation; typed fields cannot prove the model understood the scene.

The existing full-turn/undo test now isolates post-turn processing explicitly: its stream mock does not intercept native Ollama JSON memory requests. The engine execution and undo assertions remain active. Separate tests cover background scheduling.

Validation at the time: 155 selected backend tests passed, including the complete API clarification lifecycle, ordered execution/undo and migration compatibility. `ruff --select E9,F` and `git diff --check` passed. The cloud campaign database was backed up with SQLite's backup API, upgraded to `b9d0e1f2a3b4`, and the backend restarted. The campaign debugger reported zero scene/location errors, canon gaps, failed/pending jobs and unfinished generations.

## Live endpoint check (2026-10-02)

One manually submitted CPU turn took 39m 08s. It exposed a remaining disagreement: the initial extractor described the direction as undecided, while the semantic reviewer marked `наружу с рынка` as a committed endpoint. Destination identity then bound the origin market, leaving Lada physically there while narration said she had left. The director also created a duplicate craftsperson. The validator accepted this mismatch. The published test turn was undone afterward.

The extractor now supplies a typed `destination_committed` field, and the semantic reviewer cannot upgrade an open endpoint to committed. When they disagree, the interpreter requests clarification before destination binding or execution. Regression coverage checks that disagreement. The scene's pre-existing Ivan participation/location mismatch was repaired by restoring his current location to the market.

A second manual API turn on the updated code completed in 453.6 seconds. It published `Куда именно ты хочешь направиться? Назови место или ориентир.`, with no action steps, transition or NPC introductions. Lada and Ivan remained at the market; the debugger reported zero scene/location errors, canon gaps, failed/pending jobs or unfinished generations. The faster result returned before world resolution and narration, compared with 39 minutes for the earlier broken movement turn.

Validation after the live check: 102 selected backend tests passed, including intent extraction, clarification routing, spatial authority, outcome resolution and undo. `ruff --select E9,F` and `git diff --check` passed.
