# Уточнение стратегии хранения данных

> Дополнение к [product-foundation.md](../product-foundation.md), секции §8, §12, §16, §27.
> Разрешение противоречия SQLite vs PostgreSQL.

**Статус:** рабочий документ
**Дата:** 14 июля 2026

---

## 1. Суть проблемы

В исходном product-foundation.md обнаружены противоречия в упоминании СУБД:

| Секция | Цитата | Подразумеваемая СУБД |
|---|---|---|
| §8 «Хранилище» (стр. 267–281) | «В MVP следует реализовать только один полноценный storage adapter — SQLite» | SQLite |
| §12 «Поиск памяти» (стр. 627) | «PostgreSQL full-text search» | PostgreSQL |
| §16 «Экспорт и Obsidian» (стр. 748) | «PostgreSQL остаётся источником истины» | PostgreSQL |
| §27 (стр. 1167) | «Для desktop MVP рекомендуется SQLite» | SQLite |

Это не фатально, но создаёт когнитивную нагрузку при чтении и риск неверных архитектурных решений при переходе к реализации.

---

## 2. Два режима работы — явное разделение

Продукт должен поддерживать два режима. Каждый имеет свою конфигурацию хранилища:

### Desktop Mode (MVP, приоритет)

```text
Пользователь
  └── Tauri / Browser → localhost
        └── FastAPI backend
              ├── SQLite (campaign.db)
              │     ├── relational data (entities, facts, beliefs, turns...)
              │     ├── FTS5 (full-text search)
              │     └── campaign state
              ├── SQLite vector extension (sqlite-vec)
              │     └── embeddings для semantic search
              └── Filesystem
                    ├── media assets (images, audio)
                    └── raw archive (append-only log)
```

**Характеристики:**
- Один файл `campaign.db` — переносимый, легко резервировать
- Не требует отдельного серверного процесса СУБД
- FTS5 — встроенный модуль SQLite, не требует установки
- sqlite-vec — расширение для векторного поиска (альтернатива: sqlite-vss, но sqlite-vec активнее поддерживается)
- Всё локально, работает офлайн

### Server Mode (будущее, self-hosted / multiplayer)

```text
Пользователи (N)
  └── Browser → LAN / Internet
        └── FastAPI backend
              ├── PostgreSQL
              │     ├── relational data
              │     ├── tsvector (full-text search)
              │     ├── pgvector (vector search)
              │     └── campaign state
              └── Filesystem / MinIO
                    ├── media assets
                    └── raw archive
```

**Характеристики:**
- Несколько пользователей, конкурентный доступ
- PostgreSQL лучше держит нагрузку и конкурентность
- pgvector — зрелое расширение, хорошо интегрировано
- Требует отдельный процесс СУБД (docker-compose)
- Может хостить несколько кампаний

---

## 3. Таблица компонентов по режимам

| Компонент | Desktop (SQLite) | Server (PostgreSQL) |
|---|---|---|
| **Реляционные данные** | SQLite 3.x | PostgreSQL 15+ |
| **Полнотекстовый поиск** | FTS5 | tsvector + GIN index |
| **Векторный поиск** | sqlite-vec | pgvector |
| **Миграции** | Alembic (SQLite dialect) | Alembic (PostgreSQL dialect) |
| **Media Assets** | Filesystem (relative paths) | Filesystem / MinIO |
| **Сырой архив** | Append-only file (JSONL) | Append-only таблица / файл |
| **Бэкап** | Копирование файла | pg_dump / campaign export |
| **Конкурентность** | Single-writer (WAL mode) | Full MVCC |
| **Campaign Bundle** | .zip с campaign.db + media/ | Export → .zip |

---

## 4. Repository Interface — единая абстракция

Доменная модель и сервисы **не должны знать** о конкретной СУБД. Архитектурный подход:

```text
Domain Layer
  ├── Campaign Service
  ├── Turn Runner
  ├── Memory Scribe
  └── Context Compiler
        │
        ▼
Repository Interfaces (abc)
  ├── CampaignRepository
  ├── TurnRepository
  ├── EntityRepository
  ├── FactRepository
  ├── BeliefRepository
  ├── RelationshipRepository
  ├── SceneRepository
  ├── MemoryChunkRepository
  ├── SearchIndex (FTS + vector)
  └── MediaAssetRepository
        │
        ├── SQLiteAdapter (MVP)
        │     ├── SQLAlchemy + sqlite3
        │     ├── FTS5 queries
        │     └── sqlite-vec queries
        │
        └── PostgresAdapter (future)
              ├── SQLAlchemy + asyncpg
              ├── tsvector queries
              └── pgvector queries
```

### Правила:

1. **Репозитории возвращают доменные объекты** (Pydantic models), не ORM-модели
2. **SQL-специфичный код** изолирован внутри adapter'а
3. **Тесты** используют SQLite adapter (быстро, без зависимостей)
4. **Переключение** через конфигурацию: `STORAGE_BACKEND=sqlite|postgres`
5. **Оба adapter'а проходят один и тот же набор интеграционных тестов**

### Где абстракция неполна:

Есть операции, которые **объективно отличаются** между SQLite и PostgreSQL:

| Операция | SQLite | PostgreSQL |
|---|---|---|
| Full-text query | `MATCH` syntax | `@@` + `to_tsquery` |
| Vector similarity | `vec_distance_L2` | `<->` operator (pgvector) |
| JSON queries | `json_extract` | `jsonb` operators |
| Concurrent writes | WAL mode, single writer | MVCC, multiple writers |
| Array fields | JSON array | `ARRAY` type |

Эти различия должны быть **инкапсулированы внутри adapter'а**. Repository interface оперирует высокоуровневыми методами:

```python
# Не так:
def search_fts(self, query: str, table: str) -> list[Row]

# А так:
def search_memory(
    self,
    query: str,
    campaign_id: UUID,
    filters: MemorySearchFilters,
    limit: int = 20,
) -> list[MemoryChunk]
```

---

## 5. Полнотекстовый поиск: FTS5 vs tsvector

### FTS5 (SQLite)

```sql
-- Создание виртуальной таблицы
CREATE VIRTUAL TABLE memory_fts USING fts5(
    text,
    keywords,
    entity_names,
    content='memory_chunks',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

-- Поиск
SELECT * FROM memory_fts WHERE memory_fts MATCH 'король AND монастырь';
```

**Плюсы:** встроен в SQLite, нулевые зависимости, быстрый для небольших объёмов.
**Минусы:** ограниченная поддержка языков (можно подключить ICU tokenizer), нет built-in ranking по релевантности (нужен bm25()).

### tsvector (PostgreSQL)

```sql
-- Колонка + индекс
ALTER TABLE memory_chunks ADD COLUMN search_vector tsvector;
CREATE INDEX idx_memory_search ON memory_chunks USING GIN(search_vector);

-- Поиск
SELECT * FROM memory_chunks
WHERE search_vector @@ to_tsquery('russian', 'король & монастырь')
ORDER BY ts_rank(search_vector, to_tsquery('russian', 'король & монастырь')) DESC;
```

**Плюсы:** отличная поддержка русского языка (стемминг, стоп-слова), встроенный ranking, зрелые GIN-индексы.
**Минусы:** требует PostgreSQL.

### Рекомендация для MVP

FTS5 с unicode61 tokenizer. Для русского языка потребуется:
- Либо ICU tokenizer (SQLite extension, поддерживает русский стемминг)
- Либо внешняя нормализация при индексации (Python-side stemming через `nltk` или `pymorphy3`)
- Либо принять, что поиск будет по точным формам слов (для первого MVP допустимо)

---

## 6. Векторный поиск: sqlite-vec vs pgvector

### sqlite-vec (Desktop)

```sql
-- Создание виртуальной таблицы
CREATE VIRTUAL TABLE memory_vec USING vec0(
    embedding float[384],  -- размерность зависит от embedding model
    +chunk_id integer
);

-- Поиск K ближайших
SELECT chunk_id, distance
FROM memory_vec
WHERE embedding MATCH ?  -- передаём вектор запроса
ORDER BY distance
LIMIT 20;
```

**Характеристики:**
- Автор: Alex Garcia (активная разработка, 2024+)
- Поддержка: float32, int8 quantization
- Скорость: brute-force scan, но для десятков тысяч chunks — достаточно
- Ограничение: нет HNSW-индекса (в отличие от pgvector), линейная сложность поиска
- Для MVP (тысячи chunks) — более чем достаточно

### pgvector (Server)

```sql
-- Колонка + HNSW индекс
ALTER TABLE memory_chunks ADD COLUMN embedding vector(384);
CREATE INDEX idx_memory_embedding ON memory_chunks
    USING hnsw (embedding vector_cosine_ops);

-- Поиск
SELECT id, 1 - (embedding <=> query_embedding) AS similarity
FROM memory_chunks
WHERE campaign_id = $1
ORDER BY embedding <=> query_embedding
LIMIT 20;
```

**Характеристики:**
- HNSW-индекс — логарифмическая сложность
- Зрелое расширение, широко используется
- Оптимизации для больших объёмов

### Переиндексация при смене embedding модели

Смена модели (например, с `all-MiniLM-L6-v2` на `multilingual-e5-large`) требует **полной переиндексации**:

1. Удалить все embedding записи
2. Загрузить все memory chunks из основной таблицы
3. Прогнать через новую embedding модель (batch processing)
4. Записать новые embeddings

**Оценка времени:**
- 1000 chunks × ~50 tokens/chunk × local embedding model ≈ 5–30 секунд
- 10000 chunks ≈ 50–300 секунд
- Background task с прогресс-баром

---

## 7. Миграции данных кампании (Alembic)

### Проблема

При развитии продукта схема данных **будет меняться**. Пользователь с кампанией на v1.3 должен обновиться до v1.4 без потери данных.

### Стратегия

```text
Alembic migration chain:
  v001_initial.py
  v002_add_beliefs.py
  v003_add_story_threads.py
  v004_add_visual_profiles.py
  ...
```

**Для SQLite:**
- Alembic поддерживает SQLite, но с ограничениями:
  - Нет `ALTER COLUMN` (нужен `batch_alter_table` — Alembic поддерживает через `render_as_batch=True`)
  - Нет `DROP COLUMN` до SQLite 3.35.0 (2021)
  - Нет concurrent migrations (не проблема для single-user)
- При обновлении приложения: backend проверяет версию схемы → запускает pending migrations → продолжает

**Для PostgreSQL:**
- Alembic работает полноценно
- Стандартный online migration pipeline

### Критическое правило

> Миграции должны быть **обратимо-совместимы** минимум на 1 версию назад. Если v1.4 добавляет поле, v1.3 должна игнорировать его, а не падать. Это позволяет откатить обновление.

### Тестирование миграций

```text
Для каждой миграции:
1. Создать DB с предыдущей версией
2. Загрузить fixture с тестовыми данными
3. Применить миграцию
4. Проверить, что данные корректны
5. Проверить downgrade
```

---

## 8. Переносимый Campaign Bundle

### Формат

```text
campaign-export-{name}-{date}.zip
  ├── campaign.json          # метаданные кампании
  ├── campaign.db            # SQLite база (полный snapshot)
  ├── media/
  │     ├── images/
  │     │     ├── portrait-liara-001.png
  │     │     └── scene-throne-room-042.png
  │     └── audio/
  │           └── (если есть TTS recordings)
  ├── raw-archive/
  │     └── turns.jsonl       # сырой архив всех ходов
  ├── manifest.json           # версия схемы, checksums, размер
  └── README.md               # человекочитаемое описание кампании
```

### manifest.json

```json
{
  "format_version": "1.0",
  "app_version": "0.3.1",
  "schema_version": "v004",
  "campaign_id": "uuid-...",
  "campaign_name": "Хроники Серебряного Трона",
  "exported_at": "2026-07-14T23:00:00Z",
  "turn_count": 847,
  "entity_count": 134,
  "media_count": 42,
  "total_size_bytes": 157286400,
  "checksums": {
    "campaign.db": "sha256:abc...",
    "turns.jsonl": "sha256:def..."
  }
}
```

### Операции с bundle

| Операция | Описание |
|---|---|
| **Export** | Создать .zip из текущей кампании |
| **Import** | Загрузить .zip в новую установку |
| **Backup** | Автоматический export по расписанию / при закрытии |
| **Transfer SQLite → PostgreSQL** | Import bundle в server mode (миграция adapter'а) |
| **Transfer PostgreSQL → SQLite** | Export из server mode в portable bundle |

### Ограничения

- Bundle **не включает** API-ключи и пользовательские настройки (секреты)
- Bundle **не включает** embedding vectors (пересчитываются при импорте)
- Bundle **не включает** FTS-индексы (пересобираются при импорте)
- Максимальный размер bundle ограничен разумными пределами (~2 GB с медиа)

---

## 9. Рекомендации по §12 и §16

### §12 «Поиск памяти» — предлагаемая правка

Текущий текст упоминает «PostgreSQL full-text search». Предлагается заменить на:

> Рекомендуемый hybrid retrieval:
> - semantic similarity (vector search);
> - full-text search (FTS5 в desktop / tsvector в server mode);
> - entity overlap;
> - ...

### §16 «Экспорт и Obsidian» — предлагаемая правка

Текущий текст: «PostgreSQL остаётся источником истины». Предлагается:

> Основная СУБД (SQLite в desktop mode, PostgreSQL в server mode) остаётся источником истины. Obsidian/Markdown используется как экспортный формат.

---

## 10. WAL Mode и конкурентность в SQLite

Для desktop mode SQLite должен работать в WAL (Write-Ahead Logging) режиме:

```sql
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
```

**Почему WAL:**
- Позволяет читать данные во время записи (один writer, много readers)
- Backend может писать turn, пока frontend читает state
- Background indexing (memory chunks, embeddings) не блокирует основной поток

**Ограничение:** только один процесс может писать одновременно. Для desktop single-user это не проблема. Для server mode (multiplayer) — нужен PostgreSQL.

---

## 11. Итоговая рекомендация

1. **MVP = SQLite only**. Не тратить ресурсы на PostgreSQL adapter до реальной потребности в server mode.
2. **Repository interface с первого дня**. Абстракция дешёвая, а переписывать дорого.
3. **FTS5 + sqlite-vec** для поиска. Достаточно для десятков тысяч chunks.
4. **Alembic для миграций** с `render_as_batch=True` (обходит ограничения SQLite).
5. **Campaign Bundle (.zip)** как единица переноса и бэкапа.
6. **Все упоминания СУБД в документе** привести к явному формату `[desktop: FTS5]` / `[server: tsvector]`.

> *Один файл. Одна истина. Переносимый как свиток, защищённый как крепость. SQLite — идеальный хранитель для Хроник Акаши, пока кампания живёт на одном Алтаре.*
