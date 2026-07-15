# Дополнительные открытые вопросы

**Статус:** поправка к product-foundation.md  
**Дополняет:** §23 (Открытые продуктовые вопросы)  
**Версия:** 0.1  
**Дата:** 14 июля 2026

---

## Обзор

В §23 product-foundation.md перечислены 12 открытых вопросов (1-12). Этот документ добавляет вопросы 13-24, обнаруженные при анализе архитектуры, roadmap и сценариев использования.

Для каждого вопроса указано:
- **Суть проблемы** — почему это важно решить
- **Критический этап** — когда решение становится обязательным (откладывание дальше — технический долг)
- **Варианты решения** — предварительные мысли (не окончательные)

---

## Вопрос 13. Версионирование API: как frontend и backend обновляются независимо?

### Суть проблемы

Product-foundation.md описывает архитектуру «React frontend + FastAPI backend». В процессе разработки frontend и backend будут эволюционировать с разной скоростью. Без версионирования API:

- Обновление backend может сломать frontend
- Пользователь может запустить новый frontend со старым backend (или наоборот, если обновление Tauri произошло, а backend ещё нет)
- В будущем: LAN-доступ означает, что несколько клиентов могут быть разных версий

### Критический этап

**Этап 1 (Manual Canon).** Как только появится structured data (персонажи, факты, отношения), API endpoints станут контрактом. Изменение формата ответа сломает всех клиентов.

На Этапе 0 (Vertical Skeleton) допустимо жить без версионирования, потому что API минимален и нестабилен.

### Варианты решения

| Вариант | Описание | Сложность |
|---------|----------|-----------|
| **URL versioning** (`/api/v1/...`) | Классический подход. Новый major → новый prefix | Низкая |
| **Header versioning** (`Accept: application/vnd.pdm.v1+json`) | Более чистый REST, но сложнее дебажить | Средняя |
| **Compatibility layer** | Backend поддерживает несколько форматов ответа | Высокая |
| **Monorepo + lockstep releases** | Frontend и backend всегда релизятся вместе | Низкая, но ограничивает гибкость |

**Рекомендация для MVP:** monorepo + lockstep releases. Это desktop-приложение — frontend и backend обновляются вместе в Tauri bundle. URL versioning добавить, когда появится LAN-доступ или server mode.

---

## Вопрос 14. Миграции данных кампании: как мигрировать 500-ходовую кампанию при изменении схемы?

### Суть проблемы

Product-foundation.md предполагает, что модель данных будет эволюционировать (§22: «добавлять типы после реальных проблем»). Но у пользователя уже может быть кампания с 500+ ходами. При изменении схемы БД нужно:

1. Не потерять данные пользователя
2. Не заставлять пользователя начинать кампанию заново
3. Обеспечить обратную совместимость или прозрачную миграцию

Это особенно критично для local-first приложения: нельзя мигрировать данные на сервере — миграция должна произойти на машине пользователя.

### Критический этап

**Этап 1 (Manual Canon).** После Этапа 0 будет первое серьёзное расширение схемы (добавление Entity, Fact, Relationship, Scene). Это первая реальная миграция.

### Варианты решения

| Вариант | Описание | Применимость |
|---------|----------|-------------|
| **Alembic migrations** | Стандартные SQL-миграции, выполняемые при запуске | ✅ Основной метод |
| **Schema version в campaign.db** | Таблица с текущей версией схемы, проверка при открытии | ✅ Обязательно |
| **Backup перед миграцией** | Автоматическое копирование .db файла перед ALTER TABLE | ✅ Обязательно |
| **Rollback миграции** | Alembic downgrade, если миграция сломалась | 🟡 Желательно |
| **Export + reimport** | Экспорт в JSON → создание новой БД → импорт | 🛑 Крайний случай |

**Рекомендация:**

1. Alembic как основной инструмент миграций (уже в стеке, §8)
2. При каждом запуске: проверить `schema_version` → если устарела → показать пользователю: «Обновление базы данных кампании. Создаю резервную копию...»
3. Автоматический backup `.db` файла перед каждой миграцией
4. Тестировать миграции на сгенерированных данных (см. Вопрос 15)

**Edge case: миграция raw archive.** Raw archive — append-only, его формат должен быть максимально стабильным. Изменения схемы raw archive допустимы только как добавление новых полей (backwards-compatible). Если нужна breaking change — создать новую таблицу `raw_archive_v2` и мигрировать.

---

## Вопрос 15. Тестовая стратегия: как тестировать продукт, зависящий от недетерминированного LLM?

### Суть проблемы

LLM — недетерминистический компонент. Один и тот же prompt может дать разные ответы. Это делает невозможным:

- Традиционный golden-file testing (ожидаемый ответ != реальный)
- Детерминированный CI (тесты могут flake)
- Assertion на точный текст

При этом нужно тестировать:
- Что Context Compiler правильно собирает контекст
- Что Memory Scribe правильно извлекает факты
- Что Continuity Checker обнаруживает нарушения
- Что Turn Runner правильно обрабатывает ход
- Что Branching/Undo корректно откатывают state

### Критический этап

**Этап 0 (Vertical Skeleton).** Без тестовой стратегии первые же рефакторинги сломают работающий код без предупреждения. Особенно критично, когда один разработчик.

### Варианты решения

#### Mock LLM для CI

Создать `MockLLMProvider`, который возвращает предопределённые ответы:

```python
class MockLLMProvider(LLMProvider):
    def __init__(self, responses: dict[str, str]):
        self.responses = responses  # prompt pattern → response
    
    async def generate(self, prompt: str) -> str:
        for pattern, response in self.responses.items():
            if pattern in prompt:
                return response
        return "Default mock response"
```

Это позволяет тестировать всю pipeline (Context Compiler → LLM → Memory Scribe → Continuity Checker) без реальной LLM.

#### Snapshot testing

Для Context Compiler: зафиксировать, что при данном state + user input Context Compiler генерирует ожидаемый prompt. Если prompt изменился — тест показывает diff, разработчик подтверждает или отклоняет.

```
tests/snapshots/
  context_compiler/
    test_simple_scene.approved.txt
    test_with_beliefs.approved.txt
    test_large_history.approved.txt
```

#### Property-based testing

Для Continuity Checker:

```python
# Свойство: если персонаж мёртв, он не может говорить
@given(state=campaign_states(), action=character_actions())
def test_dead_characters_cant_act(state, action):
    if state.character_is_dead(action.character):
        violations = continuity_checker.check(state, action)
        assert any(v.type == "dead_character_acting" for v in violations)
```

Для Branching/Undo:

```python
# Свойство: undo(apply(state, delta)) == state
@given(state=world_states(), delta=state_deltas())
def test_undo_is_inverse_of_apply(state, delta):
    new_state = apply_delta(state, delta)
    restored_state = undo_delta(new_state, delta)
    assert restored_state == state
```

#### Integration tests с реальной LLM

Отдельный test suite, запускаемый вручную (не в CI):
- Проверяет, что real LLM может следовать structured output schema
- Проверяет, что extraction выдаёт валидный JSON
- Проверяет качество на golden scenarios (субъективная оценка)

**Рекомендация:** Mock LLM для CI (автоматические тесты), snapshot testing для Context Compiler, property-based для Continuity Checker и Branching. Integration с реальной LLM — ручной прогон перед релизом.

---

## Вопрос 16. Оценка стоимости хода при облачных LLM

### Суть проблемы

При использовании облачных LLM (OpenAI, Anthropic) каждый ход стоит денег. Пользователь может не осознавать, что длинный контекст + structured extraction + continuity check = 3-5 вызовов API на один ход.

Пример для GPT-4o (примерные цены):
- Input: $5/1M tokens
- Output: $15/1M tokens
- Один ход: ~4000 input + ~1000 output = ~$0.035
- Memory Scribe: ~2000 input + ~500 output = ~$0.0175
- Итого за ход: ~$0.05
- 100 ходов: ~$5
- 1000 ходов: ~$50

Для GPT-4o-mini или Claude Haiku — в 10-20x дешевле, но всё равно non-zero.

### Критический этап

**Этап 0 (Vertical Skeleton).** Как только пользователь подключает облачный API, он должен понимать расходы.

### Варианты решения

1. **Трекинг расходов:**
   - Записывать input_tokens, output_tokens, model_name для каждого вызова
   - Показывать в UI: «Этот ход: ~$0.05 | Всего за сессию: ~$1.20 | Всего за кампанию: ~$15.40»
   - Поддержка custom pricing (пользователь вводит цены своего провайдера)

2. **Budget alerts:**
   - Настраиваемый лимит: «Предупредить после $10 за кампанию»
   - Предупреждение перед ходом: «Этот ход будет стоить ~$0.08 (длинный контекст)»

3. **Cost optimization:**
   - Использовать дешёвую модель для Memory Scribe (extraction не требует creativity)
   - Кэшировать system prompt (некоторые провайдеры поддерживают prompt caching)
   - Batching Memory Scribe calls (один запрос на extraction + validation)

---

## Вопрос 17. Rate limiting / cooldown — защита от случайного спама ходов

### Суть проблемы

Пользователь может случайно отправить 10 ходов за минуту:
- Нажал Enter дважды
- Отправил пустой ход
- Скрипт/автоматизация бомбит API

Последствия:
- Облачный API: неожиданные расходы
- Локальная LLM: очередь запросов, зависание
- Memory Scribe: каскад proposals, сложно review

### Критический этап

**Этап 0 (Vertical Skeleton).** Базовая защита нужна с первого дня.

### Варианты решения

1. **Debounce на UI:** кнопка «Отправить» неактивна 1-2 секунды после отправки
2. **Server-side cooldown:** минимальный интервал между ходами (настраиваемый, по умолчанию 3 сек)
3. **Queue with confirmation:** если >3 ходов в очереди, показать «У вас 3 хода в очереди. Продолжить?»
4. **Cost guard (для облачных):** «Вы отправили 10 ходов за минуту. Расход: ~$0.50. Продолжить?»
5. **Empty input guard:** не отправлять пустой ход, показать предупреждение

**Рекомендация:** debounce + server-side cooldown + empty input guard на Этапе 0. Остальное — по мере необходимости.

---

## Вопрос 18. Логирование и observability — как дебажить, почему LLM получила неправильный контекст?

### Суть проблемы

Главная проблема LLM-приложений при дебаге: «Почему NPC сказал X, хотя не мог этого знать?»

Чтобы ответить на этот вопрос, нужно видеть:
1. Какой контекст был собран Context Compiler'ом
2. Какие факты были включены (и почему)
3. Какие факты были исключены (и почему)
4. Какой prompt был отправлен LLM
5. Какой ответ пришёл (полный, включая tool calls)
6. Какие changes предложил Memory Scribe
7. Что прошло/не прошло Continuity Checker

Без structured logging это невозможно.

### Критический этап

**Этап 0 (Vertical Skeleton).** Logging инфраструктура должна быть заложена с первого дня. Добавлять logging задним числом — мучительно.

### Варианты решения

#### Structured Turn Log

Каждый ход сохраняет полный debug context:

```json
{
  "turn_id": "...",
  "debug": {
    "context_compilation": {
      "system_prompt_tokens": 450,
      "scene_tokens": 120,
      "character_sheets_tokens": 340,
      "memory_chunks_retrieved": 5,
      "memory_chunks_tokens": 890,
      "sliding_window_turns": 15,
      "sliding_window_tokens": 3200,
      "total_tokens": 5000,
      "budget_remaining": 3096,
      "facts_included": ["fact_001", "fact_023", ...],
      "facts_excluded_reason": {
        "fact_042": "character_knowledge_scope",
        "fact_055": "superseded"
      }
    },
    "llm_call": {
      "model": "llama3.1:8b",
      "prompt_hash": "abc123",
      "temperature": 0.7,
      "response_tokens": 450,
      "latency_ms": 3200
    },
    "memory_scribe": {
      "proposals": 3,
      "auto_approved": 1,
      "pending_review": 2
    },
    "continuity_check": {
      "violations_found": 0,
      "warnings": ["character_location_uncertain"]
    }
  }
}
```

#### Memory Inspector integration

Product-foundation.md (§9) упоминает Memory Inspector в UI. Его нужно расширить:

- **Context View:** для любого хода — показать полный prompt, отправленный LLM
- **Provenance View:** для любого факта — показать цепочку: ход → extraction → approval
- **Diff View:** сравнить контекст двух ходов — что изменилось

#### Application Logs

Стандартный structured logging (JSON) для серверной стороны:
- `DEBUG`: каждое решение Context Compiler
- `INFO`: каждый ход, каждый LLM call
- `WARNING`: приближение к лимитам, ретраи
- `ERROR`: ошибки LLM, validation failures

Файл лога ротируется, доступен пользователю (для bug reports).

**Рекомендация:** Structured Turn Log + application logs на Этапе 0. Memory Inspector с Context View — Этап 1. Provenance View — Этап 4.

---

## Вопрос 19. Offline-first sync: нужна ли синхронизация?

### Суть проблемы

Product-foundation.md постулирует local-first (§6, принцип 1). Но что если пользователь:
- Играет на десктопе дома
- Хочет продолжить на планшете (через LAN, §7)
- Или в будущем: играет на ноутбуке offline, а потом подключается

Нужна ли синхронизация между устройствами?

### Критический этап

**НЕ критично до Фазы 2 (Multiplayer).** В single-player всё хранится на одном устройстве. LAN-доступ — это доступ к тому же серверу, не копия данных.

### Варианты решения

| Вариант | Когда | Описание |
|---------|-------|----------|
| **Нет sync** | Фаза 1 | Одна копия данных на одном устройстве. LAN — через тот же backend |
| **Manual sync** (export/import) | Фаза 1, если нужно | Экспорт campaign bundle → перенос на другое устройство → импорт |
| **Cloud backup** | Фаза 1+ (опционально) | Автоматический backup на облачное хранилище (Google Drive, S3). НЕ sync, а backup |
| **CRDTs / full sync** | Фаза 2+ | Полная синхронизация между устройствами. Сложно. Очень сложно |

**Рекомендация:** на Фазе 1 — нет sync. Manual export/import достаточен. Cloud backup как отдельная фича, если есть спрос.

---

## Вопрос 20. Internationalization: язык интерфейса

### Суть проблемы

Product-foundation.md написан на русском. Продукт позиционируется как local-first, без привязки к сервису. Вопросы:

- Интерфейс только на русском? Или английский тоже?
- System prompts для LLM на каком языке?
- Кампания может быть на любом языке?
- UI strings нужно ли интернационализировать?

### Критический этап

**Этап 0 (Vertical Skeleton).** Если i18n не заложен в архитектуру с начала, добавлять потом — refactoring каждого компонента.

**Однако:** если целевая аудитория MVP — русскоязычные пользователи, i18n можно отложить при условии, что strings не hardcoded в компоненты.

### Варианты решения

1. **Русский only (MVP):** быстрее, проще. Все строки захардкожены.
2. **Strings extraction (подготовка к i18n):** вынести все UI strings в файлы (JSON/YAML), но переводить только на русский. Это ~10% дополнительной работы, но спасает от большого рефакторинга позже.
3. **English + Russian (MVP):** больше работы, но открывает international audience с первого дня.

**Рекомендация:** вариант 2 (strings extraction, один язык). Это минимальная инвестиция с максимальной отдачей. Переводы добавятся потом community-driven.

**Кампания на любом языке:** не требует i18n. Пользователь пишет на любом языке, LLM отвечает на нём же (настраивается в system prompt).

---

## Вопрос 21. Accessibility: доступность интерфейса

### Суть проблемы

Нишевой продукт может позволить себе отложить accessibility, но базовые вещи легче заложить сразу, чем добавлять потом.

### Критический этап

**Этап 1 (Manual Canon).** Как только UI усложняется (формы, карточки, навигация), accessibility становится всё дороже добавлять.

### Минимальный scope

| Что | Когда | Сложность |
|-----|-------|-----------|
| **Semantic HTML** (button, nav, main, article) | Этап 0 | Нулевая |
| **ARIA labels** для интерактивных элементов | Этап 0 | Низкая |
| **Keyboard navigation** (Tab, Enter, Escape) | Этап 1 | Средняя |
| **Contrast ratio** ≥ 4.5:1 (WCAG AA) | Этап 1 | Низкая (при правильном дизайне) |
| **Focus indicators** | Этап 1 | Низкая |
| **Screen reader support** | Этап 3+ | Высокая |
| **Reduced motion** preference | Этап 5 (с анимациями) | Низкая |

**Рекомендация:** semantic HTML + ARIA labels + keyboard navigation + contrast. Это ~5% дополнительной работы на каждом этапе, но избавляет от болезненного refactoring.

---

## Вопрос 22. Performance budgets: допустимые задержки

### Суть проблемы

Без явных performance budgets оптимизация будет ad-hoc. Нужно определить, что считается «быстро» и что — «неприемлемо медленно».

### Критический этап

**Этап 0 (Vertical Skeleton).** Определить бюджеты до начала разработки, чтобы архитектурные решения учитывали производительность.

### Предлагаемые бюджеты

| Операция | Target | Acceptable | Unacceptable |
|----------|--------|-----------|--------------|
| **Запуск приложения** (до готовности UI) | <3 сек | <5 сек | >10 сек |
| **Открытие кампании** (загрузка state) | <1 сек | <3 сек | >5 сек |
| **Отправка хода** (до начала streaming) | <500 мс | <1 сек | >3 сек |
| **Context compilation** | <200 мс | <500 мс | >1 сек |
| **Memory retrieval** (semantic search) | <300 мс | <1 сек | >2 сек |
| **Streaming first token** (LLM) | Зависит от LLM | — | — |
| **Save turn** (persist to DB) | <100 мс | <300 мс | >1 сек |
| **Switch branch** | <1 сек | <3 сек | >5 сек |
| **Undo last turn** | <500 мс | <1 сек | >2 сек |
| **Full rebuild** (1000 ходов) | <10 мин | <30 мин | >1 час |
| **Export campaign** | <5 сек | <15 сек | >30 сек |

**Примечание:** «Streaming first token» зависит от LLM и не контролируется приложением. Остальные операции — ответственность системы.

**Рекомендация:** зафиксировать budgets в architecture.md. Добавить performance tests для критических операций (context compilation, memory retrieval, save turn).

---

## Вопрос 23. Backup strategy: резервные копии кампании

### Суть проблемы

Для local-first приложения потеря данных — катастрофа. Нет облачного backup. Пользователь может потерять сотни часов игры из-за:
- Сбоя диска
- Случайного удаления файла
- Corrupted SQLite (power loss during write)
- Ошибки миграции

### Критический этап

**Этап 0 (Vertical Skeleton).** Базовый backup нужен до того, как пользователь создаст ценную кампанию.

### Варианты решения

#### Автоматические локальные бэкапы

```
campaign_data/
  my_campaign.db           ← текущая БД
  backups/
    my_campaign_2026-07-14_18-30.db   ← auto backup
    my_campaign_2026-07-13_20-15.db
    my_campaign_2026-07-12_19-00.db
```

**Стратегия ротации:**
- Сохранять backup при каждом закрытии приложения
- Сохранять backup каждые N ходов (например, 50)
- Хранить: последние 5 daily backups + последние 4 weekly backups
- Автоматически удалять старые по расписанию
- Максимальный объём backups: настраиваемый (по умолчанию 1 GB)

#### Campaign Bundle export

Product-foundation.md (§16) упоминает экспорт. Bundle должен включать:
- SQLite database (полная копия)
- Media assets
- Campaign metadata (version, created_at, last_played)
- Manifest файл (для валидации целостности)

Формат: `.pdm` (zip-архив с определённой структурой).

#### WAL mode для SQLite

SQLite в WAL (Write-Ahead Logging) mode снижает риск corruption при неожиданном выключении:
```sql
PRAGMA journal_mode=WAL;
```

Это не backup, но снижает вероятность потери данных.

**Рекомендация:** WAL mode + auto backup при закрытии + backup каждые 50 ходов + rotation. Campaign bundle export — Этап 1.

---

## Вопрос 24. Plugin system: пользовательские расширения или только конфигурация?

### Суть проблемы

Product-foundation.md упоминает «pluggable rulesets» (§19, Этап 7) и «pluggable embedding interface» (§8). Но общая стратегия расширяемости не определена:

- Могут ли пользователи создавать свои rulesets?
- Могут ли пользователи добавлять свои LLM providers?
- Могут ли пользователи модифицировать system prompt templates?
- Есть ли API для third-party расширений?
- Или всё настраивается через конфигурацию (JSON/YAML)?

### Критический этап

**Этап 7 (Rules Engine).** До этого система может работать с fixed набором конфигураций. Pluggable rulesets — первое место, где нужно решить: плагин или конфигурация.

**Но архитектурное решение нужно раньше:** если система монолитная и жёстко связанная, добавить plugin system на Этапе 7 потребует rewrite.

### Варианты решения

| Уровень | Описание | Сложность | Пример |
|---------|----------|-----------|--------|
| **L0: Hardcoded** | Всё зашито в код | Нулевая | «Только systemless mode» |
| **L1: Configuration** | JSON/YAML файлы, интерпретируемые системой | Низкая | Rulesets как JSON schema |
| **L2: Templates** | Пользователь может редактировать prompt templates, display templates | Средняя | «Кастомный system prompt для DM» |
| **L3: Plugin API** | Определённые extension points с API | Высокая | Python плагины для custom rules |
| **L4: Full SDK** | SDK для разработки расширений | Очень высокая | Marketplace модулей |

**Рекомендация:**

- **Этапы 0-2:** L1 (Configuration) + L2 (Templates). Пользователь настраивает через JSON/YAML и может редактировать prompt templates.
- **Этап 7:** L3 (Plugin API) для rulesets. Минимальный набор extension points:
  - `on_turn_start`, `on_turn_end` — хуки жизненного цикла хода
  - `roll_dice(formula)` — custom dice logic
  - `validate_action(action, state)` — custom validation
  - `compute_damage(attacker, defender, weapon)` — custom combat
- **Post-MVP (если есть community):** L4 (Full SDK).

**Архитектурное ограничение:** даже на Этапе 0 — проектировать core services как interfaces (abstractions), а не конкретные реализации. Это не plugin system, но это фундамент для неё.

---

## Сводная таблица

| # | Вопрос | Критический этап | Влияние на архитектуру |
|---|--------|-----------------|----------------------|
| 13 | Версионирование API | Этап 1 | 🟡 Среднее |
| 14 | Миграции данных | Этап 1 | 🔴 Высокое |
| 15 | Тестовая стратегия | Этап 0 | 🔴 Высокое |
| 16 | Стоимость хода | Этап 0 | 🟢 Низкое |
| 17 | Rate limiting | Этап 0 | 🟢 Низкое |
| 18 | Логирование | Этап 0 | 🔴 Высокое |
| 19 | Offline sync | Фаза 2 | 🟡 Среднее |
| 20 | Internationalization | Этап 0 (подготовка) | 🟡 Среднее |
| 21 | Accessibility | Этап 0-1 | 🟡 Среднее |
| 22 | Performance budgets | Этап 0 | 🟡 Среднее |
| 23 | Backup strategy | Этап 0 | 🔴 Высокое |
| 24 | Plugin system | Этап 7 (решение раньше) | 🔴 Высокое |

### Приоритет решения (что решать первым)

**Решить до начала разработки (Этап 0):**
- #15 Тестовая стратегия — определяет workflow разработки
- #18 Логирование — закладывается в архитектуру
- #22 Performance budgets — влияет на архитектурные решения
- #23 Backup strategy — потеря данных пользователя непростительна

**Решить при начале Этапа 1:**
- #13 Версионирование API — до появления stable endpoints
- #14 Миграции данных — до первого расширения схемы
- #20 Internationalization — strings extraction, не перевод
- #21 Accessibility — semantic HTML с первого компонента

**Решить позднее (когда станет актуально):**
- #16 Стоимость хода — при добавлении cloud providers
- #17 Rate limiting — при реальных пользователях
- #19 Offline sync — при появлении спроса на multi-device
- #24 Plugin system — при проектировании Rules Engine

---

*Этот документ является рабочим дополнением к product-foundation.md и будет обновляться по мере уточнения архитектуры и принятия решений.*
