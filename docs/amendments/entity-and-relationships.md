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
    backstory_secret TEXT,       -- fallback-проза; по ADR-008 предпочтительно
                                --   выражать через Facts + visibility + Beliefs

    -- Текущее состояние
    emotional_state TEXT,
    current_location_id UUID REFERENCES entities(id),
    current_intentions TEXT,     -- JSON array текущих намерений

    -- Цели хранятся в отдельной таблице character_goals (ADR-008).
    -- Каждая цель — отдельная запись с описанием, приоритетом,
    -- статусом, секретностью, источником и сроком действия.

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
    danger_level TEXT            -- safe, moderate, dangerous, lethal
    -- current_occupants убран: по ADR-008 список находящихся
    -- в локации персонажей вычисляется запросом по
    -- characters.current_location_id, а не хранится вторым списком.
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
    backstory_secret: str | None = None  # fallback; предпочтительно Facts + visibility (ADR-008)

    emotional_state: str | None = None
    current_location_id: UUID | None = None
    current_intentions: list[str] = []

    # Цели хранятся в отдельной таблице character_goals (ADR-008).
    # goals: list[CharacterGoal] загружаются через repository.

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

## 5. Модель отношений (Relationships) — приведение к ADR-008

> **Обновлено 15 июля 2026.** Раздел переработан для соответствия принятому
> [ADR-008](../adr/008-domain-model-and-storage-boundaries.md).
> Первичная модель — **повествовательные утверждения**. Числовые шкалы —
> необязательное производное представление.

### Принцип: утверждение, а не шкала

Ранняя версия этого документа предлагала 4–10 числовых осей как первичную
структуру хранения. ADR-008 зафиксировал другой подход:

> *«Отношения хранятся как утверждения с типом, необязательной интенсивностью,
> повествовательным описанием, причиной, происхождением, сроком действия
> и видимостью. Числовые панели могут быть построены позднее как производное
> представление.»*

**Почему утверждения лучше осей для нарратива:**

| Аспект | Числовые оси | Утверждения |
|---|---|---|
| Что видит LLM | `trust: 0.6, fear: 0.3` | «Доверяет, но побаивается из-за случая в Акте II» |
| Точность | Ложная — 0.6 vs 0.7 не значит ничего | Содержательная — причина и контекст |
| Извлечение | LLM плохо даёт точные числа | LLM хорошо даёт тип + описание |
| Отладка | «Почему 0.6?» — непонятно | «Почему доверяет?» — есть причина |
| Масштабирование | N×N×K записей | Только значимые отношения |

### Схема БД для утверждений об отношениях

```sql
CREATE TABLE relationship_assertions (
    id UUID PRIMARY KEY DEFAULT (uuid()),
    campaign_id UUID NOT NULL REFERENCES campaigns(id),

    -- Кто → к кому
    subject_id UUID NOT NULL REFERENCES entities(id),
    object_id UUID NOT NULL REFERENCES entities(id),

    -- Содержание утверждения
    relation_type TEXT NOT NULL,     -- 'trust', 'fear', 'rivalry', 'debt',
                                    -- 'romantic_interest', 'mentor', 'grudge', ...
    description TEXT NOT NULL,       -- "Лиара доверяет Сафире с тех пор,
                                    --  как та помогла ей скрыться от стражи"
    reason TEXT,                     -- краткая причина: "помогла скрыться"

    -- Необязательная интенсивность
    intensity REAL,                  -- NULL или значение [-1.0, 1.0]
                                    -- Числовое значение — вспомогательное;
                                    -- description первичен.

    -- Происхождение
    source_event_id UUID REFERENCES events(id),
    source_turn_id INTEGER,
    provenance TEXT NOT NULL DEFAULT 'manual',  -- manual, extracted, system
    confidence REAL DEFAULT 1.0,

    -- Temporal
    valid_from TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    valid_until TIMESTAMP,           -- NULL = текущее
    is_current BOOLEAN NOT NULL DEFAULT TRUE,

    -- Visibility
    visibility TEXT NOT NULL DEFAULT 'dm',  -- 'dm', 'public', 'character_only'

    -- Версионирование
    superseded_by UUID REFERENCES relationship_assertions(id),

    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- Constraints
    CHECK (subject_id != object_id),
    CHECK (intensity IS NULL OR (intensity >= -1.0 AND intensity <= 1.0))
);

CREATE INDEX idx_ra_subject ON relationship_assertions(subject_id, is_current);
CREATE INDEX idx_ra_object ON relationship_assertions(object_id, is_current);
CREATE INDEX idx_ra_campaign ON relationship_assertions(campaign_id, is_current);
CREATE INDEX idx_ra_pair ON relationship_assertions(subject_id, object_id, is_current);
```

### Типы отношений

Тип (`relation_type`) — **свободная строка**, не enum. Это позволяет кампании
использовать любые типы без миграций. Рекомендуемые значения:

| Тип | Описание | Пример |
|---|---|---|
| `trust` | Доверие | «Доверяет после спасения в Акте II» |
| `distrust` | Недоверие | «Подозревает в подделке письма» |
| `fear` | Страх | «Боится после того, как видела расправу» |
| `loyalty` | Лояльность | «Верна из чувства долга перед семьёй» |
| `affection` | Симпатия | «Чувствует тёплое расположение» |
| `hostility` | Враждебность | «Ненавидит за предательство гильдии» |
| `debt` | Долг | «Должна жизнь после битвы у реки» |
| `rivalry` | Соперничество | «Соперничает за внимание короля» |
| `romantic` | Романтический интерес | «Влюблена, но скрывает» |
| `mentor` | Наставничество | «Обучает тайным искусствам» |
| `grudge` | Обида | «Затаила обиду за публичное унижение» |
| `alliance` | Союз | «Временный союз против общего врага» |

Один персонаж может иметь **несколько утверждений** к другому одновременно:
Лиара может одновременно `trust` Сафиру и `fear` её.

### Pydantic-модель

```python
class RelationshipAssertion(BaseModel):
    """Утверждение об отношении одного персонажа к другому."""
    id: UUID
    campaign_id: UUID
    subject_id: UUID
    object_id: UUID

    relation_type: str              # 'trust', 'fear', 'rivalry', ...
    description: str                # развёрнутое описание
    reason: str | None = None       # краткая причина

    intensity: float | None = None  # необязательное числовое значение
    source_turn_id: int | None = None
    provenance: str = "manual"
    confidence: float = 1.0

    valid_from: datetime
    valid_until: datetime | None = None
    is_current: bool = True
    visibility: str = "dm"
```

### Как Context Compiler потребляет утверждения

Стратегия не изменилась: загружать только отношения **действующего NPC**
к **присутствующим** в сцене. Но формат стал нарративным:

```python
async def get_relevant_relationships(
    scene: Scene,
    acting_character: Character,
) -> list[RelationshipAssertion]:
    """Отбирает утверждения об отношениях для контекста действующего персонажа."""

    present_ids = [c.id for c in scene.participants]
    assertions = await repo.get_assertions(
        subject_id=acting_character.id,
        object_ids=present_ids,
        is_current=True,
        visibility_in=["dm", "public", "character_only"],
    )
    return assertions
```

**Пример вывода для LLM:**

```text
[Отношения Лиары к присутствующим]

→ Сафира:
  - доверяет: «Помогла скрыться от стражи в Акте II»
  - боится: «Видела, как Сафира уничтожила амулет голыми руками — сила пугает»

→ Страж:
  - не доверяет: «Подозревает в тайном сговоре с Советом»
  - испытывает долг: «Страж спас её брата из темницы»
```

Это **естественный язык**, который LLM понимает лучше, чем `trust: 0.8, fear: 0.3`.
Контекст компактнее: только значимые утверждения, без нулевых осей.

---

## 6. Производное числовое представление (будущее)

Числовые панели и графы отношений **не входят в MVP** (ADR-008).
Когда они понадобятся, их можно построить как **view** поверх утверждений:

```python
def compute_numeric_summary(
    assertions: list[RelationshipAssertion],
) -> dict[str, float]:
    """Агрегирует утверждения в числовые оценки для UI-панелей."""
    summary = {}
    for a in assertions:
        # Если intensity задана — использовать её
        if a.intensity is not None:
            summary[a.relation_type] = a.intensity
        else:
            # Иначе — эвристика: положительный тип → +0.5, отрицательный → -0.5
            sign = +1 if a.relation_type in POSITIVE_TYPES else -1
            summary[a.relation_type] = sign * 0.5
    return summary

POSITIVE_TYPES = {"trust", "loyalty", "affection", "romantic", "mentor", "alliance"}
```

### Визуализация (будущий этап)

Граф отношений строится из утверждений:

```text
┌─────────┐  доверяет, боится  ┌─────────┐
│  Лиара  │ ─────────────────→ │ Сафира  │
│         │ ←───────────────── │         │
└─────────┘   соперничает      └─────────┘
     │                              │
     │ не доверяет, должна          │ лояльна
     ▼                              ▼
┌─────────┐                   ┌─────────┐
│  Страж  │                   │  Король │
└─────────┘                   └─────────┘
```

- Рёбра = утверждения (а не оси)
- Цвет = тип (зелёный = доверие, красный = враждебность, ...)
- Подпись = краткая причина
- Клик → полная история утверждений между парой

---

## 7. Эволюция отношений — отслеживание изменений

Каждое новое утверждение **не удаляет** предыдущее. Предыдущее помечается
как `is_current = FALSE`, `superseded_by = id нового`.

```text
Turn 47:  Лиара → Сафира, trust: «Первое знакомство, осторожное доверие»
Turn 102: Лиара → Сафира, trust: «Доверяет — Сафира помогла в бою» (supersedes prev)
Turn 189: Лиара → Сафира, trust: «Полностью доверяет — Сафира раскрыла секрет»
Turn 234: Лиара → Сафира, distrust: «Узнала о подделанном письме» (trust superseded)
```

Это позволяет:
- **Timeline** — как менялись отношения Лиары к Сафире по ходам
- **Provenance** — почему именно такое утверждение (source_turn, reason)
- **Rollback** — при откате хода восстанавливается предыдущее утверждение
- **Campaign Debugger** — «почему Лиара не доверяет Сафире?» → цепочка утверждений

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
3. **Цели** — отдельные записи `character_goals` с жизненным циклом (ADR-008), не JSON-массивы
4. **Секреты** — предпочтительно через Facts + visibility + Beliefs; `backstory_secret` — fallback-проза (ADR-008)
5. **Отношения** — повествовательные утверждения с типом, описанием, причиной и необязательной интенсивностью (ADR-008). Числовые панели — производное представление для будущего UI
6. **Локации** — `current_occupants` не хранится; список вычисляется запросом по `characters.current_location_id` (ADR-008)
7. **Context Compiler** — загружает только утверждения действующего NPC к присутствующим, в нарративном формате
8. **Inventory** — через связь Item → Owner/Location/Container с проверкой эксклюзивности (ADR-008)

> *Обновлено 15 июля 2026 для соответствия принятым ADR-007 и ADR-008.*
