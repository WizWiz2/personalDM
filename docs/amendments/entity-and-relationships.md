# Детализация доменной модели: Entity и Relationships

> Дополнение к [product-foundation.md](../product-foundation.md), секция §10.
> Предложение по специализации Entity и оптимизации модели отношений.

**Статус:** рабочий документ
**Дата:** 14 июля 2026

---

## 1. Проблема с универсальной Entity

В исходном документе `Entity` описана как универсальная сущность, покрывающая 7 типов: character, location, faction, item, creature, organisation, concept.

При этом `Character` имеет 15+ уникальных полей (внешность, страхи, голос, визуальный профиль...), а `item` — совершенно другие (вес, стоимость, магические свойства). Хранить это в одной таблице — путь к одному из двух антипаттернов:

### Антипаттерн 1: Широкая таблица (Sparse Columns)

```sql
CREATE TABLE entities (
    id UUID PRIMARY KEY,
    type TEXT NOT NULL,
    canonical_name TEXT,
    -- Общие поля...
    -- Character-specific (NULL для всех остальных типов)
    appearance TEXT,
    personality TEXT,
    voice TEXT,
    fears TEXT,
    desires TEXT,
    -- Location-specific (NULL для character/item/...)
    geography TEXT,
    atmosphere TEXT,
    -- Item-specific
    weight REAL,
    magical_properties TEXT,
    -- ... ещё 50 полей, из которых 80% NULL
);
```

**Проблемы:** читаемость, расширяемость, невозможность типизации на уровне БД, сложные миграции.

### Антипаттерн 2: EAV (Entity-Attribute-Value)

```sql
CREATE TABLE entity_attributes (
    entity_id UUID,
    attribute_name TEXT,  -- 'appearance', 'weight', ...
    attribute_value TEXT, -- всё хранится как строка
    PRIMARY KEY (entity_id, attribute_name)
);
```

**Проблемы:** нет типизации, невозможно валидировать на уровне БД, медленные JOIN'ы, кошмар для запросов.

---

## 2. Рекомендуемый подход: базовый класс + специализированные таблицы

### Схема базы данных

```sql
-- Базовая таблица для всех сущностей
CREATE TABLE entities (
    id UUID PRIMARY KEY DEFAULT (uuid()),
    campaign_id UUID NOT NULL REFERENCES campaigns(id),
    entity_type TEXT NOT NULL CHECK (entity_type IN (
        'character', 'location', 'faction', 'item',
        'creature', 'organisation', 'concept'
    )),
    canonical_name TEXT NOT NULL,
    aliases TEXT,           -- JSON array: ["Лиара", "Серебряная Дева", "Тень"]
    description TEXT,
    status TEXT NOT NULL DEFAULT 'active',  -- active, inactive, dead, destroyed, unknown
    provenance TEXT,        -- откуда взялась запись: manual, extracted, imported
    version INTEGER NOT NULL DEFAULT 1,
    custom_fields TEXT,     -- JSONB для экспериментальных полей
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    UNIQUE (campaign_id, entity_type, canonical_name)
);

-- Специализированная таблица для персонажей
CREATE TABLE characters (
    entity_id UUID PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,

    -- Внешность и физические характеристики
    appearance TEXT,
    face_description TEXT,
    body_description TEXT,
    immutable_features TEXT,     -- то, что НЕ меняется (раса, цвет глаз, шрамы...)

    -- Личность
    personality TEXT,            -- общее описание характера
    values TEXT,                 -- JSON array: ["честь", "семья", "правда"]
    fears TEXT,                  -- JSON array: ["предательство", "темнота"]
    desires TEXT,                -- JSON array: ["признание", "любовь"]

    -- Речь
    voice TEXT,                  -- описание голоса
    speech_patterns TEXT,        -- речевые привычки, словечки, акцент

    -- Биография
    biography TEXT,
    backstory_public TEXT,       -- то, что известно миру
    backstory_secret TEXT,       -- то, что знает только ДМ/пользователь

    -- Текущее состояние
    emotional_state TEXT,
    current_location_id UUID REFERENCES entities(id),
    current_intentions TEXT,     -- JSON array текущих намерений

    -- Цели
    long_term_goals TEXT,        -- JSON array долгосрочных целей
    short_term_goals TEXT,       -- JSON array краткосрочных целей

    -- Визуальный профиль (для генерации изображений)
    visual_profile TEXT          -- JSON: canonical_desc, palette, default_outfit, negative_features...
);

-- Специализированная таблица для локаций
CREATE TABLE locations (
    entity_id UUID PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,

    geography TEXT,              -- тип местности, размер, ландшафт
    atmosphere TEXT,             -- атмосфера, освещение, звуки
    access_rules TEXT,           -- кто может войти, как добраться
    parent_location_id UUID REFERENCES entities(id),  -- иерархия: комната → здание → город
    climate TEXT,
    notable_features TEXT,       -- JSON array достопримечательностей
    danger_level TEXT,           -- safe, moderate, dangerous, lethal
    current_occupants TEXT       -- JSON array entity_id текущих обитателей
);

-- Специализированная таблица для предметов
CREATE TABLE items (
    entity_id UUID PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,

    item_type TEXT,              -- weapon, armor, potion, artifact, document, key, misc
    physical_properties TEXT,    -- JSON: weight, size, material, color
    magical_properties TEXT,     -- JSON: enchantments, charges, attunement
    value_estimate TEXT,         -- примерная стоимость (текстовое описание или числовое)
    current_owner_id UUID REFERENCES entities(id),
    current_location_id UUID REFERENCES entities(id),
    is_unique BOOLEAN DEFAULT FALSE,
    lore TEXT                    -- история предмета
);

-- Специализированная таблица для фракций
CREATE TABLE factions (
    entity_id UUID PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,

    goals TEXT,                  -- JSON array целей фракции
    resources TEXT,              -- описание ресурсов
    territory TEXT,              -- описание территории влияния
    hierarchy TEXT,              -- описание структуры власти
    membership_rules TEXT,       -- как вступить, как выйти
    reputation TEXT,             -- как фракция воспринимается миром
    secret_agenda TEXT,          -- скрытые цели (знает только ДМ)
    leader_id UUID REFERENCES entities(id)
);

-- Специализированная таблица для существ (monsters, animals)
CREATE TABLE creatures (
    entity_id UUID PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,

    species TEXT,
    abilities TEXT,              -- JSON array способностей
    behavior TEXT,               -- описание поведения
    habitat TEXT,                -- среда обитания
    threat_level TEXT,           -- trivial, low, moderate, high, legendary
    weaknesses TEXT,             -- JSON array слабостей
    is_unique BOOLEAN DEFAULT FALSE  -- уникальное существо vs вид
);

-- Специализированная таблица для организаций
CREATE TABLE organisations (
    entity_id UUID PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,

    structure TEXT,              -- описание организационной структуры
    purpose TEXT,                -- публичная цель
    secret_purpose TEXT,         -- скрытая цель
    is_public BOOLEAN DEFAULT TRUE,  -- известна ли организация миру
    influence_level TEXT,        -- local, regional, national, global
    leader_id UUID REFERENCES entities(id),
    headquarters_id UUID REFERENCES entities(id)
);

-- Специализированная таблица для концепций (абстракции мира)
CREATE TABLE concepts (
    entity_id UUID PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,

    domain TEXT,                 -- магия, религия, политика, наука...
    abstract_description TEXT,   -- подробное описание концепции
    related_entity_ids TEXT,     -- JSON array связанных сущностей
    impact_on_world TEXT         -- как концепция влияет на мир
);
```

### Pydantic-модели (Python)

```python
from pydantic import BaseModel
from uuid import UUID
from datetime import datetime
from enum import Enum

class EntityType(str, Enum):
    CHARACTER = "character"
    LOCATION = "location"
    FACTION = "faction"
    ITEM = "item"
    CREATURE = "creature"
    ORGANISATION = "organisation"
    CONCEPT = "concept"

class EntityStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    DEAD = "dead"
    DESTROYED = "destroyed"
    UNKNOWN = "unknown"

class EntityBase(BaseModel):
    """Базовые поля, общие для всех типов сущностей."""
    id: UUID
    campaign_id: UUID
    entity_type: EntityType
    canonical_name: str
    aliases: list[str] = []
    description: str | None = None
    status: EntityStatus = EntityStatus.ACTIVE
    provenance: str | None = None
    version: int = 1
    custom_fields: dict | None = None
    created_at: datetime
    updated_at: datetime

class Character(EntityBase):
    """Персонаж — NPC или PC."""
    entity_type: EntityType = EntityType.CHARACTER

    appearance: str | None = None
    face_description: str | None = None
    body_description: str | None = None
    immutable_features: str | None = None

    personality: str | None = None
    values: list[str] = []
    fears: list[str] = []
    desires: list[str] = []

    voice: str | None = None
    speech_patterns: str | None = None

    biography: str | None = None
    backstory_public: str | None = None
    backstory_secret: str | None = None

    emotional_state: str | None = None
    current_location_id: UUID | None = None
    current_intentions: list[str] = []

    long_term_goals: list[str] = []
    short_term_goals: list[str] = []

    visual_profile: dict | None = None

# Аналогично для Location, Item, Faction, Creature, Organisation, Concept...
```

### Паттерн Repository

```python
from abc import ABC, abstractmethod

class EntityRepository(ABC):
    """Единый интерфейс для работы с сущностями всех типов."""

    @abstractmethod
    async def get_by_id(self, entity_id: UUID) -> EntityBase:
        """Возвращает полную сущность с типо-специфичными полями."""
        ...

    @abstractmethod
    async def get_characters_in_scene(self, scene_id: UUID) -> list[Character]:
        """Персонажи, присутствующие в сцене."""
        ...

    @abstractmethod
    async def search_by_name(
        self, campaign_id: UUID, query: str, entity_type: EntityType | None = None
    ) -> list[EntityBase]:
        """Поиск по canonical_name и aliases."""
        ...

    @abstractmethod
    async def get_character_with_knowledge(
        self, character_id: UUID
    ) -> tuple[Character, list["Belief"], list["Relationship"]]:
        """Персонаж + его знания + его отношения — для Context Compiler."""
        ...
```

---

## 3. Преимущества подхода

| Аспект | Широкая таблица | EAV | **Базовый класс + спец. таблицы** |
|---|---|---|---|
| Типизация | Частичная | Нет | ✅ Полная |
| SQL-запросы | Простые, но неточные | Сложные JOIN'ы | Умеренные (JOIN по типу) |
| Валидация на уровне БД | Частичная | Нет | ✅ Полная |
| Расширяемость | Сложная миграция | Простая, но хаотичная | ✅ Миграция по таблицам |
| Pydantic-модели | Один огромный класс | Нет типизации | ✅ Наследование |
| Индексы | Wasteful | Невозможны | ✅ Точные |
| JSONB для кастомных полей | ✅ | ✅ | ✅ (в custom_fields) |

---

## 4. Роль custom_fields (JSONB)

Поле `custom_fields` в базовой таблице `entities` служит для:

1. **Экспериментальных полей** — прототипирование нового атрибута до вынесения в отдельную колонку
2. **Пользовательских полей** — пользователь хочет добавить «фамильный герб» для персонажей конкретной кампании
3. **Ruleset-специфичных полей** — D&D добавляет «класс», «уровень», «хиты», а systemless — нет
4. **Импортированных данных** — при импорте из внешних источников неизвестные поля попадают сюда

### Правило продвижения полей

```text
Жизненный цикл экспериментального поля:

1. custom_fields["alignment"] — эксперимент
2. Используется в 3+ кампаниях → выносим в специализированную таблицу
3. ALTER TABLE characters ADD COLUMN alignment TEXT;
4. Миграция: перенести значения из custom_fields → alignment
5. Удалить из custom_fields
```

---

## 5. Модель отношений (Relationships) — оптимизация

### Проблема масштабирования

Исходный документ предлагает 10 осей отношений:
доверие, симпатия, любовь, страх, долг, уважение, подозрение, власть, зависимость, лояльность.

При 20 NPC (типичная кампания средней сложности):
- **Направленные отношения:** 20 × 19 = 380 пар
- **× 10 осей** = 3800 записей
- **× temporal validity** = потенциально тысячи исторических записей

Для Context Compiler это создаёт проблему: **невозможно впихнуть 3800 строк в контекст LLM**.

### Рекомендуемая стратегия: MVP-набор + расширение

#### MVP (Этап 1): 4 оси

| Ось | Тип | Описание |
|---|---|---|
| **trust** | float [-1, 1] | От полного недоверия до абсолютной веры |
| **affinity** | float [-1, 1] | От ненависти до обожания (симпатия + любовь) |
| **fear** | float [0, 1] | От бесстрашия до ужаса |
| **loyalty** | float [-1, 1] | От предательства до фанатичной преданности |

**Обоснование:**
- Эти 4 оси покрывают ~80% нарративных ситуаций
- `affinity` объединяет «симпатию» и «любовь» (разделение возможно позже)
- `trust` + `loyalty` — два разных аспекта (можно не доверять, но быть лояльным из долга)
- `fear` — критичен для интриг и конфликтов

#### Расширенный набор (Этап 4+): +6 осей

| Ось | Тип | Описание | Когда добавлять |
|---|---|---|---|
| **respect** | float [-1, 1] | От презрения до благоговения | Когда нужны иерархии |
| **suspicion** | float [0, 1] | Подозрительность | Когда появляется Continuity Engine |
| **power** | float [-1, 1] | Кто над кем доминирует | Для политических кампаний |
| **debt** | float [-1, 1] | Кто кому должен | Для сложных обязательств |
| **dependency** | float [0, 1] | Эмоциональная/физическая зависимость | Для глубоких связей |
| **love** | float [0, 1] | Романтическая любовь (отдельно от affinity) | По запросу пользователя |

#### Конфигурация кампании

```python
class RelationshipConfig(BaseModel):
    """Конфигурация осей отношений для конкретной кампании."""
    enabled_axes: list[str] = ["trust", "affinity", "fear", "loyalty"]
    custom_axes: list[CustomAxis] = []

class CustomAxis(BaseModel):
    """Пользовательская ось отношений."""
    name: str          # "romantic_tension"
    display_name: str  # "Романтическое напряжение"
    min_value: float   # 0
    max_value: float   # 1
    description: str   # "Степень романтического интереса"
```

Пользователь может **включить** дополнительные оси для конкретной кампании, или создать собственные.

### Схема БД для отношений

```sql
CREATE TABLE relationships (
    id UUID PRIMARY KEY DEFAULT (uuid()),
    campaign_id UUID NOT NULL REFERENCES campaigns(id),

    -- Кто → к кому
    subject_id UUID NOT NULL REFERENCES entities(id),
    object_id UUID NOT NULL REFERENCES entities(id),

    -- Ось и значение
    axis TEXT NOT NULL,        -- 'trust', 'affinity', 'fear', 'loyalty', ...
    intensity REAL NOT NULL,   -- значение оси

    -- Контекст
    reason TEXT,               -- почему такое отношение ("спасла жизнь в битве")
    source_event_id UUID REFERENCES events(id),
    source_turn INTEGER,

    -- Temporal
    valid_from TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    valid_until TIMESTAMP,     -- NULL = текущее
    is_current BOOLEAN NOT NULL DEFAULT TRUE,

    -- Visibility
    visibility TEXT NOT NULL DEFAULT 'dm',  -- 'dm', 'public', 'character_only'

    -- Metadata
    confidence REAL DEFAULT 1.0,   -- уверенность в записи (если extracted)
    provenance TEXT,               -- manual, extracted, system
    superseded_by UUID REFERENCES relationships(id),

    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- Constraints
    CHECK (subject_id != object_id),
    CHECK (intensity >= -1.0 AND intensity <= 1.0)
);

-- Индексы для быстрого доступа
CREATE INDEX idx_rel_subject ON relationships(subject_id, is_current);
CREATE INDEX idx_rel_object ON relationships(object_id, is_current);
CREATE INDEX idx_rel_campaign ON relationships(campaign_id, is_current);
CREATE INDEX idx_rel_pair ON relationships(subject_id, object_id, axis, is_current);
```

### Как Context Compiler потребляет отношения

Context Compiler **не загружает все 380+ записей**. Стратегия:

```python
async def get_relevant_relationships(
    scene: Scene,
    speaking_character: Character,
) -> list[Relationship]:
    """Отбирает только релевантные отношения для текущей сцены."""

    # 1. Отношения говорящего персонажа К присутствующим
    present_ids = [c.id for c in scene.participants]
    rels = await repo.get_relationships(
        subject_id=speaking_character.id,
        object_ids=present_ids,
        is_current=True,
    )

    # 2. Фильтрация по значимости (не включать нейтральные)
    significant = [r for r in rels if abs(r.intensity) > 0.2]

    # 3. Форматирование для LLM
    return format_relationships_for_context(significant)
```

**Пример вывода для LLM:**

```text
[Отношения Лиары]
→ Сафира: доверие 0.8, симпатия 0.6, страх 0, лояльность 0.7
  (причина доверия: "помогла скрыться от стражи в Акте II")
→ Страж: доверие -0.3, симпатия 0.1, страх 0.4, лояльность 0
  (причина страха: "видела, как убил торговца без суда")
```

Таким образом, вместо 3800 записей LLM видит **8–20 строк** — только отношения говорящего NPC к тем, кто сейчас в сцене.

---

## 6. Визуализация отношений в UI

### Граф отношений

Одна из сильных сторон структурированных отношений — возможность визуализации:

```text
┌─────────┐   trust: 0.8   ┌─────────┐
│  Лиара  │ ──────────────→ │ Сафира  │
│         │ ←────────────── │         │
└─────────┘  affinity: -0.2 └─────────┘
     │                           │
     │ fear: 0.4                 │ loyalty: 0.9
     ▼                           ▼
┌─────────┐                ┌─────────┐
│  Страж  │                │  Король │
└─────────┘                └─────────┘
```

**Реализация:** force-directed graph (D3.js или аналог) с:
- Толщина линии = `|intensity|`
- Цвет линии = тип оси (зелёный = trust, красный = fear, ...)
- Направленность стрелок
- Фильтрация по осям
- Клик на линию → история изменений

### Таблица отношений

Для более точного просмотра — таблица:

| | Сафира | Страж | Торговец |
|---|---|---|---|
| **Лиара** | 🟢 T:0.8 A:0.6 L:0.7 | 🔴 T:-0.3 F:0.4 | 🟡 T:0.2 A:0.3 |
| **Сафира** | — | 🔴 T:-0.5 A:-0.7 | 🟡 T:0.1 |
| **Страж** | 🟡 T:0.1 F:0.2 | — | 🔴 A:-0.9 |

---

## 7. Эволюция отношений — отслеживание изменений

Каждое изменение оси создаёт **новую запись**, а предыдущая помечается как `is_current = FALSE`:

```text
Turn 47: Лиара → Сафира, trust = 0.3 (первое знакомство)
Turn 102: Лиара → Сафира, trust = 0.6 (Сафира помогла в бою)
Turn 189: Лиара → Сафира, trust = 0.8 (Сафира раскрыла свой секрет)
Turn 234: Лиара → Сафира, trust = 0.2 (Лиара узнала о подделанном письме)
```

Это позволяет:
- **Timeline отношений** — как менялось доверие Лиары к Сафире по ходам
- **Provenance** — почему именно такое значение (source_event)
- **Rollback** — при откате хода восстанавливается предыдущее значение
- **Campaign Debugger** — «почему Лиара не доверяет Сафире?» → история изменений

---

## 8. Inventory как отношение Entity → Entity

Интересный паттерн: инвентарь можно моделировать **через связь Item → Character/Location**:

```sql
-- Кто владеет предметом
items.current_owner_id → entities.id (character)

-- Где лежит предмет (если не у кого-то)
items.current_location_id → entities.id (location)
```

**Правило эксклюзивности:** предмет имеет либо `current_owner_id`, либо `current_location_id`, но не оба (если владелец есть — предмет «у него», а не «в локации»).

Это позволяет Continuity Checker детерминированно проверять:
- «Предмет X уже принадлежит персонажу Y» → ошибка, если другой NPC его «достаёт из кармана»
- «Предмет X в локации A, а персонаж в локации B» → ошибка, если персонаж «берёт» предмет

---

## 9. Итоговые рекомендации

1. **Entity** — базовая таблица + специализированные таблицы по типам (не EAV, не широкая таблица)
2. **custom_fields (JSONB)** — для экспериментальных и пользовательских полей с правилом продвижения
3. **Relationship** — начать MVP с 4 осей (trust, affinity, fear, loyalty), расширять через конфигурацию кампании
4. **Context Compiler** — загружает только отношения говорящего NPC к присутствующим, фильтруя по значимости
5. **UI** — граф + таблица отношений, timeline изменений
6. **Inventory** — через связь Item → Owner/Location с проверкой эксклюзивности

> *Каждая Сущность в мире имеет свою Истинную Форму. Нельзя запирать Дракона и Свиток в одну клетку — у них разная природа, разные свойства, разная судьба. Но все они — дети одного Мира, и все несут на себе Печать Канона: имя, статус, происхождение. В этом — единство в многообразии.*
