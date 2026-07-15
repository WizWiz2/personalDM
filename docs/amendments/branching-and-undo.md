# Branching & Undo: Механика ветвления и отката

**Статус:** поправка к product-foundation.md  
**Дополняет:** §10 (Turn — поле branch_id), §11 (Обработка одного хода), §19 (Roadmap)  
**Версия:** 0.1  
**Дата:** 14 июля 2026

---

## 1. Обзор

Product-foundation.md упоминает `branch_id` в структуре Turn и `текущую ветку` в Campaign, но не раскрывает механику ветвления и отката. Этот документ детализирует:

1. **Branching** — создание альтернативных линий повествования
2. **Undo / Rollback** — откат последнего хода или возврат к произвольной точке
3. **Regenerate** — перегенерация ответа DM с тем же контекстом
4. **Каскадные эффекты** — что происходит с derived данными при откате

Ветвление — это не экзотическая функция. Это повседневная потребность: «мне не понравился ответ DM, хочу попробовать другой вариант» или «что было бы, если бы я сделал иначе?»

---

## 2. Модель данных ветвления

### 2.1. Branch

```
Branch:
  id: UUID
  campaign_id: UUID
  parent_branch_id: UUID | null     # null для main branch
  fork_turn_number: int | null      # номер хода, от которого ветка отделилась
  name: str                         # "main", "Что если Лиара ушла", etc.
  description: str | null
  created_at: datetime
  is_active: bool                   # текущая ветка кампании
  status: enum                      # active | archived | deleted
```

### 2.2. Связь Branch ↔ Turn

Каждый Turn содержит `branch_id`. Ходы одной ветки образуют линейную последовательность. Ветки образуют дерево:

```
main:        T1 → T2 → T3 → T4 → T5 → T6 → T7 → T8
                         │
branch-A:                └→ T3a → T4a → T5a
                                   │
branch-B:                          └→ T4b → T5b → T6b
```

**Важно:**
- Ходы T1, T2, T3 **не дублируются** в branch-A. Branch-A **наследует** их от main.
- `fork_turn_number` для branch-A = 3 (ветка начинается после хода 3)
- `fork_turn_number` для branch-B = 4a (ветка начинается после хода 4a, что делает branch-B вложенной)

### 2.3. Виртуальная история ветки

Полная история ветки branch-A:
```
T1 (from main) → T2 (from main) → T3 (from main) → T3a → T4a → T5a
```

Это вычисляется рекурсивно:
```python
def get_branch_history(branch_id: UUID) -> list[Turn]:
    branch = get_branch(branch_id)
    if branch.parent_branch_id is None:
        # main branch — просто все ходы
        return get_turns(branch_id)
    
    # Наследуемые ходы от parent
    parent_history = get_branch_history(branch.parent_branch_id)
    inherited = [t for t in parent_history if t.turn_number <= branch.fork_turn_number]
    
    # Собственные ходы
    own_turns = get_turns(branch_id)
    
    return inherited + own_turns
```

### 2.4. Нумерация ходов в ветках

Два подхода:

| Подход | Пример | Плюсы | Минусы |
|--------|--------|-------|--------|
| **Global sequence** | main: 1,2,3,4,5; branch-A: 6,7,8 | Уникальный номер для каждого хода | Номера не отражают позицию в нарративе |
| **Branch-local + inherited** | main: 1,2,3,4,5; branch-A: 1,2,3(inherited),4,5,6 | Интуитивная нумерация «с начала» | Номера пересекаются между ветками |

**Рекомендация: global sequence для storage, branch-local для UI.**

В БД: каждый Turn имеет уникальный `global_sequence_number` (автоинкремент). В UI: показывается позиция в текущей ветке.

---

## 3. Операции с ветками

### 3.1. Создание ветки (Fork)

**Триггер:** пользователь выбирает произвольный ход и нажимает «Создать ветку отсюда».

**Процесс:**

```
1. Определить точку ветвления (fork_turn)
2. Определить текущую ветку, из которой создаётся (parent_branch)
3. Создать запись Branch:
   - parent_branch_id = parent_branch.id
   - fork_turn_number = fork_turn.turn_number
   - name = автосгенерированное или заданное пользователем
   - is_active = false (не переключаемся автоматически)
4. Snapshot state:
   - Вычислить состояние мира на момент fork_turn
   - Это state = initial_state + sum(state_deltas[1..fork_turn])
   - Сохранить snapshot как checkpoint (для быстрого восстановления)
5. Предложить пользователю переключиться на новую ветку
```

**Snapshot state** — это оптимизация. Без него при переключении ветки пришлось бы replay всех state deltas с начала кампании. С checkpoints достаточно найти ближайший checkpoint и replay оттуда.

**Рекомендация по checkpoints:**
- Автоматический checkpoint при создании ветки
- Автоматический checkpoint каждые N ходов (например, 50)
- Checkpoint при смене сцены
- Checkpoint при явном сохранении пользователя

### 3.2. Переключение между ветками

**Триггер:** пользователь выбирает другую ветку в UI.

**Процесс:**

```
1. Сохранить текущее состояние (если не сохранено)
2. Определить целевую ветку
3. Загрузить checkpoint ближайший к HEAD целевой ветки
4. Replay state deltas от checkpoint до HEAD
5. Обновить Campaign.current_branch = target_branch
6. Обновить Campaign.current_scene = scene at HEAD of target branch
7. Обновить UI:
   - Chat history → история целевой ветки
   - Scene View → сцена целевой ветки
   - Character View → состояние персонажей в целевой ветке
   - Memory Inspector → facts/relationships целевой ветки
```

**Критично: Context Compiler и Memory Retrieval должны учитывать branch_id.** Факты из ветки A не должны попадать в контекст ветки B (кроме наследованных из общего предка).

### 3.3. Удаление ветки

**Триггер:** пользователь хочет удалить альтернативную ветку.

**Каскадные удаления:**

```
Удаление branch-A:
  1. Удалить все дочерние ветки (branch-B, если parent = branch-A) — рекурсивно
  2. Удалить все Turn записи, принадлежащие branch-A (НО НЕ наследованные)
  3. Удалить state checkpoints для branch-A
  4. Удалить/пометить как deleted:
     - Facts с branch_id = branch-A (не наследованные)
     - Relationships с branch_id = branch-A
     - Beliefs с branch_id = branch-A
     - Events с branch_id = branch-A
     - Memory Chunks с branch_id = branch-A
     - Scene snapshots с branch_id = branch-A
  5. Обновить FTS и vector индексы
  6. RAW ARCHIVE НЕ ЗАТРАГИВАЕТСЯ (ходы остаются в raw log с пометкой deleted branch)
```

**Soft delete vs hard delete:**

| Подход | Плюсы | Минусы |
|--------|-------|--------|
| Soft delete (status = deleted) | Можно восстановить; raw archive остаётся консистентным | Занимает место; сложнее запросы (WHERE status != deleted) |
| Hard delete | Меньше данных; проще запросы | Невозможно восстановить; orphaned ссылки |

**Рекомендация:** soft delete с возможностью purge (полного удаления) по запросу пользователя. Purge — это осознанное необратимое действие.

### 3.4. Merge веток

**Вопрос из задания:** нужно ли в MVP?

**Ответ: нет, и вероятно никогда в полноценном виде.**

Причины:
- Narrative merge — это не code merge. Нельзя «слить» два альтернативных диалога в один.
- State conflicts неразрешимы автоматически: если в ветке A персонаж жив, а в ветке B — мёртв, merge невозможен.
- Пользователь может достичь «merge-подобного» эффекта вручную:
  1. Прочитать обе ветки
  2. Выбрать одну как основную
  3. В основной ветке вручную добавить факты из другой, если нужно

**Единственный допустимый «merge»:** cherry-pick отдельных фактов или событий из одной ветки в другую. Это не merge, а ручной перенос.

### 3.5. Визуализация дерева веток

```
Пример UI (дерево):

📖 Кампания "Тёмный Шпиль"
│
├─ 🟢 main (active) — 247 ходов
│  ├─ Сцена: Тронный зал
│  └─ Последний ход: "Лиара входит..."
│
├─ 📌 "Что если отказаться от миссии" — 12 ходов
│  ├─ Ответвление от хода #45
│  ├─ Сцена: Таверна "Медный Грифон"
│  └─ Последний ход: "Бармен кивает..."
│
└─ 🗄️ "Попытка сразиться с драконом" (archived) — 3 хода
   ├─ Ответвление от хода #198
   └─ Статус: архивирована
```

Визуализация может быть:
1. **Tree view** (как выше) — для понимания структуры
2. **Timeline view** — горизонтальная шкала с точками ветвления
3. **Graph view** — для сложных случаев с вложенными ветками

В MVP достаточно tree view.

---

## 4. Undo / Rollback

### 4.1. Простой Undo (откат последнего хода)

**Самый частый use case:** «DM ответил глупость, хочу переиграть».

**Процесс:**

```
1. Определить последний Turn в текущей ветке
2. Revert state delta:
   - Для каждого изменения в state_delta — применить обратное
   - Fact created → delete fact
   - Relationship updated → restore previous value
   - Character moved → move back
   - Scene updated → restore previous scene state
3. Удалить Turn (soft delete, помечается как undone)
4. Удалить/revert все Memory Scribe записи, созданные этим ходом:
   - proposed → delete
   - active (если были auto-approved) → delete
5. Обновить Memory Chunks, если ход уже был включён в chunk (пересоздать chunk)
6. RAW ARCHIVE: добавить запись "undo of turn N" (НЕ удалять оригинал)
7. Обновить UI: последний ход исчезает из чата
```

**State delta как обратимая операция:**

Для поддержки undo, state delta должен хранить не только новое значение, но и старое:

```json
{
  "type": "relationship_update",
  "entity": "relationship_lara_safira_trust",
  "field": "intensity",
  "old_value": 0.7,
  "new_value": 0.3,
  "turn_id": "turn_247"
}
```

Это позволяет undo без полного replay: просто восстановить `old_value`.

### 4.2. Deep Rollback (откат до произвольного хода)

**Use case:** «Три хода назад я принял плохое решение, хочу откатиться».

**Процесс:**

```
1. Пользователь выбирает целевой ход N
2. Система предупреждает: "Будет создана новая ветка. Ходы N+1..HEAD останутся в текущей ветке."
3. Создать новую ветку (fork от хода N):
   - parent_branch = current_branch
   - fork_turn_number = N
4. Переключиться на новую ветку
5. Пользователь может продолжить игру с хода N
```

**Почему ветка, а не удаление:**
- Ходы N+1..HEAD могут содержать ценный контент
- Пользователь может передумать и вернуться
- Raw archive не нарушается
- Это соответствует принципу «append-only, никогда не модифицируется»

**Альтернативный вариант (destructive rollback):**
Для пользователей, которые точно не хотят сохранять отменённые ходы:

```
1. Пользователь подтверждает: "Удалить ходы N+1..HEAD безвозвратно?"
2. Soft-delete ходов N+1..HEAD в текущей ветке
3. Revert state до checkpoint <= N, replay deltas до N
4. Пользователь продолжает в той же ветке
5. Raw archive: добавить запись "destructive rollback to turn N"
```

**Рекомендация:** по умолчанию — ветка (non-destructive). Destructive rollback — как опция для продвинутых.

### 4.3. Каскадные эффекты при откате

При откате до хода N нужно обработать все derived данные, созданные ходами N+1..HEAD:

| Данные | Действие | Обоснование |
|--------|----------|-------------|
| **Facts** (created after N) | Переносятся в ветку / soft-delete | Эти факты не произошли в новой ветке |
| **Relationships** (changed after N) | Revert к значениям на ход N | Отношения откатываются |
| **Beliefs** (created/changed after N) | Revert / soft-delete | Персонажи «забывают» узнанное после N |
| **Events** (after N) | Переносятся в ветку / soft-delete | Эти события не случились |
| **Scene updates** (after N) | Revert сцены к состоянию на ход N | Сцена откатывается |
| **Memory Chunks** (covering turns after N) | Пересоздать / soft-delete | Chunks содержат не-произошедшие данные |
| **Memory Scribe proposals** (from turns after N) | Delete (если proposed), soft-delete (если active) | Предложения больше не актуальны |
| **Embeddings** (for deleted chunks) | Удалить из vector index | Поисковый индекс должен быть консистентен |
| **Story Threads** (updated after N) | Revert к состоянию на ход N | Сюжетные линии откатываются |
| **Raw Archive** | **НЕ ЗАТРАГИВАЕТСЯ** | Append-only |

**Checkpoint-based rollback:**

Если есть checkpoint на ходе N (или ранее), откат сводится к:
1. Загрузить state из checkpoint
2. Replay deltas от checkpoint до N (если checkpoint < N)
3. Soft-delete все derived данные с branch_id и turn_number > N

Без checkpoints нужен replay с самого начала, что дорого при кампании в 1000+ ходов.

---

## 5. Regenerate

### 5.1. Что это

**Use case:** «Ответ DM скучный/неудачный. Хочу перегенерировать с тем же контекстом, но другим результатом.»

Regenerate — это не undo + redo. Это:
1. Взять тот же контекст (system prompt, scene, characters, memory, user input)
2. Изменить параметры генерации (seed, temperature, или просто рандом)
3. Получить новый ответ DM

### 5.2. Варианты реализации

| Вариант | Описание | Плюсы | Минусы |
|---------|----------|-------|--------|
| **Overwrite** | Заменить последний ответ DM новым | Просто; нет мусора | Потеря оригинала; нарушает append-only |
| **Branch** | Создать ветку, генерировать в ней | Сохраняет оригинал; чистая модель | «Мусорные» ветки при частом regenerate |
| **In-place with history** | Заменить видимый ответ, но сохранить оригинал как previous_version | Баланс: чистый UI + сохранение истории | Нестандартная семантика Turn |

**Рекомендация: вариант 3 (in-place with history) для MVP.**

Реализация:

```
Turn:
  ...
  response_text: str            # текущий (возможно, перегенерированный) ответ
  response_history: list[dict]  # предыдущие версии ответа
    - text: str
    - model: str
    - seed: int
    - temperature: float
    - generated_at: datetime
    - state_delta: dict         # delta от этой версии
  regenerate_count: int          # сколько раз перегенерировали
```

**Процесс:**

```
1. Пользователь нажимает "Regenerate"
2. Сохранить текущий ответ в response_history
3. Revert state delta текущего ответа
4. Собрать тот же контекст (из кэша или пересобрать)
5. Опционально: изменить temperature / seed
6. Отправить запрос к LLM
7. Получить новый ответ
8. Вычислить новый state delta
9. Apply новый state delta
10. Обновить turn.response_text и turn.state_delta
11. Перезапустить Memory Scribe для нового ответа
12. Обновить UI
```

### 5.3. Ограничения regenerate

- Regenerate доступен **только для последнего хода** (в простом варианте). Regenerate хода посередине — это deep rollback + новый ход.
- После regenerate все предложения Memory Scribe для старого ответа удаляются, создаются новые.
- Если пользователь уже сделал ход после — regenerate невозможен (предложить deep rollback).
- Лимит regenerate: рекомендуется мягкий лимит (5-10 попыток) с предупреждением, но без жёсткого ограничения.

### 5.4. UI для regenerate

```
┌─────────────────────────────────────────────────────┐
│ DM (попытка 3/3):                                   │
│                                                      │
│ "Лиара осторожно открывает дверь. За ней — тёмный   │
│  коридор, в конце которого мерцает тусклый свет..."  │
│                                                      │
│ [🔄 Regenerate]  [◀ Prev version]  [▶ Next version]  │
│ [⚙ Temperature: 0.7 ▾]                              │
└─────────────────────────────────────────────────────┘
```

---

## 6. Branch-aware запросы

### 6.1. Принцип branch isolation

Все запросы к данным кампании должны быть branch-aware:

```sql
-- ❌ НЕПРАВИЛЬНО: может вернуть факты из другой ветки
SELECT * FROM facts WHERE campaign_id = ?

-- ✅ ПРАВИЛЬНО: только факты текущей ветки и предков
SELECT * FROM facts 
WHERE campaign_id = ?
  AND branch_id IN (SELECT id FROM branch_ancestry(?current_branch_id))
  AND status = 'active'
```

### 6.2. Branch ancestry

```sql
-- Рекурсивный CTE для получения цепочки предков
WITH RECURSIVE branch_ancestry AS (
    SELECT id, parent_branch_id, fork_turn_number
    FROM branches
    WHERE id = ?current_branch_id
    
    UNION ALL
    
    SELECT b.id, b.parent_branch_id, b.fork_turn_number
    FROM branches b
    JOIN branch_ancestry ba ON b.id = ba.parent_branch_id
)
SELECT id FROM branch_ancestry;
```

### 6.3. Конфликты при ветвлении фактов

Когда ветка создана от хода N:
- Факты, созданные до хода N → наследуются (видны в обеих ветках)
- Факт, изменённый в ветке A после N → изменение видно только в ветке A
- Факт, изменённый в main после N → изменение видно только в main

Для этого факты и отношения должны иметь `branch_id` и `created_at_turn`:

```sql
-- Получить текущее значение факта с учётом branch override
SELECT * FROM facts
WHERE subject = ? AND predicate = ?
  AND branch_id IN (SELECT id FROM branch_ancestry(?current_branch))
ORDER BY 
  CASE WHEN branch_id = ?current_branch THEN 0 ELSE 1 END,  -- своя ветка приоритетнее
  created_at_turn DESC  -- последняя версия
LIMIT 1;
```

---

## 7. Взаимодействие с Memory Pipeline

### 7.1. Memory Chunks и ветвление

Memory Chunks (§2.4 memory-pipeline.md) привязаны к branch_id:

- Chunk, покрывающий ходы до fork point → наследуется (виден из обеих веток)
- Chunk, покрывающий ходы после fork point → принадлежит конкретной ветке
- При создании ветки существующие chunks не копируются — они наследуются через branch ancestry

### 7.2. Sliding window и ветвление

Sliding window формируется из виртуальной истории ветки (§2.3):

```
Для branch-A (fork from main at turn 3):
  Sliding window = [...inherited turns from main up to 3...] + [branch-A own turns]
```

### 7.3. Rebuild и ветвление

Полная переиндексация (rebuild) должна обрабатывать каждую ветку независимо:
1. Для каждой ветки — вычислить полную историю
2. Суммаризировать собственные (не наследованные) ходы
3. Не дублировать chunks для наследованных ходов

---

## 8. Checkpoint Strategy

### 8.1. Зачем нужны checkpoints

Без checkpoints переключение ветки или rollback требует replay всех state deltas с начала кампании. Для кампании в 1000 ходов это ~1000 операций.

Checkpoint = snapshot полного state мира на конкретный ход:
- Все active facts
- Все текущие relationships
- Все beliefs
- Текущая сцена
- Позиции персонажей
- Инвентарь
- Story thread statuses

### 8.2. Когда создавать

| Триггер | Обоснование |
|---------|-------------|
| Создание ветки | Обязательно — это fork point |
| Каждые N ходов (50-100) | Ограничивает максимальный replay |
| Смена сцены | Естественная точка «сохранения» |
| Ручное сохранение | Пользователь явно хочет точку возврата |
| Перед dangerous action | «Вы уверены? Сохранить checkpoint?» |

### 8.3. Хранение

```
Checkpoint:
  id: UUID
  campaign_id: UUID
  branch_id: UUID
  turn_number: int
  created_at: datetime
  state_snapshot: JSON/BLOB    # полный state
  size_bytes: int
  trigger: enum                # fork | periodic | scene_change | manual
```

**Размер checkpoint:** зависит от количества entities. Грубая оценка:
- 50 персонажей × ~1 KB = ~50 KB
- 200 фактов × ~200 B = ~40 KB
- 100 отношений × ~200 B = ~20 KB
- Итого: ~100-200 KB на checkpoint

При checkpoint каждые 50 ходов и кампании в 1000 ходов: ~20 checkpoints × 200 KB = ~4 MB. Это ничтожно.

### 8.4. Garbage collection

Стратегия прореживания checkpoints:
- Последние 5 checkpoints — хранить всегда
- Checkpoints, созданные при fork — хранить всегда
- Manual checkpoints — хранить всегда
- Старые periodic checkpoints — прореживать (оставлять каждый N-й)

---

## 9. Этапы реализации

| Этап roadmap | Что реализуется |
|-------------|----------------|
| Этап 0 (Vertical Skeleton) | Одна ветка (main), нет undo, нет checkpoints. Regenerate — перезапись последнего ответа (без истории) |
| Этап 1 (Manual Canon) | Простой undo (откат последнего хода). State delta с old_value. Базовые checkpoints (при смене сцены) |
| Этап 1.5 (Assisted Canon) | Regenerate с историей версий (in-place with history). Каскадный revert Memory Scribe proposals |
| Этап 2 (Memory Scribe) | Создание веток. Переключение. Branch-aware запросы. Branch ancestry. Deep rollback через fork |
| Этап 3+ | Визуализация дерева веток. Удаление веток. Cherry-pick фактов между ветками. Checkpoint GC |

---

## 10. Открытые вопросы

1. **Naming convention для автоматических веток:** при regenerate или deep rollback — как автоматически именовать? «Regenerate от хода #247 (2)»? Или давать пользователю prompt?

2. **Максимальная глубина вложенности веток:** ограничивать? 3 уровня? Без ограничений? Глубокое вложение усложняет branch ancestry запросы.

3. **Notifications при переключении веток:** должны ли Memory Scribe proposals из другой ветки быть видны? Или только из текущей?

4. **Multiplayer и ветвление:** в multiplayer (Фаза 2) ветвление усложняется многократно. Может ли каждый игрок иметь свои ветки? Или ветвление — только для DM?

5. **Export и ветки:** при экспорте в Markdown/Obsidian — экспортировать все ветки? Только активную? Отдельные папки?

6. **State delta granularity:** слишком грубые deltas (JSON diff всего state) дороги в хранении. Слишком мелкие (каждое поле отдельно) сложны в реализации. Оптимальный уровень?

---

*Этот документ является рабочим дополнением к product-foundation.md и будет обновляться по мере уточнения архитектуры.*
