# Session Zero

**Статус:** current implementation contract  
**Владелец:** `SessionZeroService` + `SessionZeroInterviewService`; API — `app/api/session_zero.py`.

## Нормативный контракт

Нулевая сессия — разговор до начала обычного narrative runtime. Она должна получить достаточно данных, чтобы создать не placeholder, а пригодный к игре мир: сеттинг/жанр/завязку/тон, границы, героя, стартовую ситуацию и устойчивую стартовую Location/Scene.

Инварианты:

- interview не является анкетой: ответы можно уточнять и продолжать после ошибки провайдера;
- незавершённая Session Zero не должна незаметно превращаться в обычный игровой ход;
- `complete/finalize` допустим только после readiness-проверки обязательных полей и карточки героя;
- placeholder-названия вроде «Стартовая локация» не становятся каноном;
- материализация героя, Location и opening state должна быть идемпотентной относительно retry;
- opening не получает право говорить/решать за героя и не может создавать неразрешённую физическую сущность;
- визуальная генерация запускается только после commit playable state и является производным best-effort артефактом.

## Текущая реализация

`GET /api/campaigns/{id}/session-zero/interview` возвращает persisted interview state. `POST .../answer` сохраняет пользовательский ответ и просит модель обновить draft. Если модель падает или получает rate limit, ответ игрока уже сохранён; `POST .../retry` повторно обрабатывает pending answer.

Когда interview сообщает `ready_to_finalize`, `SessionZeroInterviewService.finalize()` передаёт данные в общий materialization path. Прямой CRUD-контракт доступен через `PUT /session-zero` и `POST /session-zero/complete`.

После успешного завершения создаются/назначаются player character, стартовая Location и Scene. Только после commit `VisualGenerationDispatcher.schedule_session_zero()` может запланировать портрет/обложку.

## Persisted evidence

Главные доказательства:

- persisted Session Zero setup/interview state;
- `Campaign.player_character_id` и `Campaign.current_scene_id`;
- durable Character card;
- structured starting Location/Scene;
- debugger `session_zero`, `missing_fields` и `character_card_missing_fields`.

Проза interview не является единственным источником истины: finalized structured state имеет приоритет.

## Failure semantics

- incomplete readiness → HTTP 409 с `missing_fields`, без частичной публикации playable campaign;
- locked/finalized setup → изменение отвергается;
- provider failure во время interview → HTTP 502, но ответ игрока остаётся сохранённым и retryable;
- visual provider failure не откатывает завершённую Session Zero;
- placeholder/неполный starting state должен fail closed до materialization.

## Проверка

Основные endpoints: `/api/campaigns/{id}/session-zero*`, `/api/characters/{id}/card`, `/api/campaigns/{id}/debugger`.

Основные тестовые слои: Session Zero tests, CLI/API resilience, placeholder regression, product contracts. Реальная способность configured LLM вести разговор проверяется только live-model acceptance, а не deterministic CI.

## Историческая граница

Старые сценарии, где Session Zero была набором свободных prompt-ответов без durable readiness/materialization contract, считаются superseded текущей схемой.