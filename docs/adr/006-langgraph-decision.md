# ADR-006: LangGraph для оркестрации workflow — использовать или нет

**Статус:** Proposed  
**Дата:** 14 июля 2026  
**Контекст:** product-foundation.md §8 («LangGraph только для workflow/checkpoints, если подтвердит пользу»), §9 (логическая архитектура: Turn Runner, Context Compiler, Memory Scribe, Continuity Checker), §11 (обработка одного хода)

---

## Проблема

Turn Runner — центральный workflow «Личного ДМ». Каждый ход пользователя проходит через цепочку:

```
Ввод пользователя
  → Context Compiler (сбор контекста из памяти, сцены, персонажей)
  → Narrative LLM (генерация ответа, tool calls)
  → Continuity Checker (валидация на конфликты)
  → State Commit (применение дельт к Campaign State)
  → Memory Scribe (извлечение событий, фактов, отношений)
  → UI Update (streaming ответа, обновление Memory Inspector)
```

Помимо Turn Runner, есть другие workflows:

- **Memory Extraction Pipeline**: анализ ответа LLM → предложение фактов/событий/отношений → review → commit.
- **Continuity Check Pipeline**: набор проверок (knowledge leaks, location conflicts, inventory, timeline) → отчёт → auto-fix или user decision.
- **Scene Transition**: закрытие сцены → summary → обновление сцены → music transition → optional image generation.
- **Reindex/Rebuild**: пересборка derived memory из сырого архива.
- **Campaign Import/Export**: сериализация/десериализация campaign bundle.

LangGraph (часть LangChain ecosystem) предлагает:

- Граф состояний (StateGraph) для определения workflow как конечного автомата.
- Встроенный checkpointing (сохранение промежуточного состояния).
- Conditional edges (ветвление по условиям).
- Human-in-the-loop (пауза для подтверждения пользователем).
- Поддержка streaming.
- Визуализацию графа.

Вопрос: **стоит ли использовать LangGraph для этих workflows, или написать собственную оркестрацию?**

---

## Рассмотренные варианты

### Вариант A: Собственный Turn Runner (async Python state machine + event log)

Написать все workflows на чистом Python с использованием `asyncio`, Pydantic-моделей для состояния и собственного event log.

**Архитектура:**

```python
class TurnRunner:
    async def execute(self, campaign_id: str, user_input: str) -> TurnResult:
        # 1. Compile context
        context = await self.context_compiler.compile(campaign_id, user_input)
        
        # 2. Call LLM (streaming)
        async for chunk in self.llm_gateway.stream(context):
            yield chunk  # stream to frontend
        
        response = chunks_to_response(chunks)
        
        # 3. Validate continuity
        issues = await self.continuity_checker.check(campaign_id, response)
        if issues.has_critical():
            response = await self.handle_issues(response, issues)
        
        # 4. Commit state
        delta = await self.state_builder.build_delta(response)
        await self.campaign_repo.apply_delta(campaign_id, delta)
        
        # 5. Extract memory
        extractions = await self.memory_scribe.extract(response, context)
        await self.campaign_repo.propose_extractions(campaign_id, extractions)
        
        # 6. Log
        await self.event_log.append(TurnEvent(...))
        
        return TurnResult(response=response, delta=delta, extractions=extractions)
```

**Плюсы:**

- **Полный контроль**: каждый шаг — явный Python-код. Дебаг через стандартные инструменты (pdb, logging, pytest).
- **Нет внешних зависимостей** для оркестрации. Только `asyncio` (stdlib).
- **Прозрачность**: новый разработчик читает `TurnRunner.execute()` и понимает весь pipeline. Нет магии фреймворка.
- **Тестируемость**: каждый шаг — это dependency-injectable сервис. Mock `llm_gateway`, mock `continuity_checker`, запускай unit-тесты.
- **Производительность**: нулевой overhead на оркестрацию. `async/await` — нативный Python.
- **Нет конфликта состояний**: Campaign State живёт в наших моделях и нашей БД. Нет второго state store (LangGraph checkpointer).
- **Streaming**: нативный `async for` — полный контроль над streaming. SSE или WebSocket — выбор за нами.
- **Vendor lock-in**: отсутствует. Зависимость только от Python stdlib.
- **Версионирование**: workflow описан в коде. Изменения — через git. Нет внешних схем графов.
- **Миграция**: если позже решим добавить LangGraph — можно обернуть существующие сервисы в LangGraph nodes без переписывания логики.

**Минусы:**

- **Checkpointing**: нужно реализовать самостоятельно. Если Turn Runner крашится на шаге 4, как возобновить? Нужен собственный механизм идемпотентности или checkpoint. Для нашего случая это может быть просто: каждый ход атомарен (либо полностью применён, либо нет — database transaction).
- **Conditional branching**: ветвление логики (retry LLM при ошибке, human-in-the-loop для review) нужно кодить вручную. Это не сложно, но код растёт.
- **Нет визуализации графа**: нет красивой диаграммы workflow «из коробки». Нужно рисовать Mermaid вручную.
- **Нет стандартного human-in-the-loop**: механизм «пауза → ждём подтверждения пользователя → продолжение» нужно реализовать самостоятельно (через WebSocket события и async Event / queue).
- **Паттерны оркестрации**: retry, timeout, parallel execution, fan-out/fan-in — нужно реализовать или использовать `tenacity`, `asyncio.gather()` и т.д.
- **Масштабирование workflows**: если количество workflows вырастет до 10–15 сложных pipeline'ов, ручное управление состоянием может стать хрупким.

**Сложность реализации:**

Для MVP (Этапы 0–2): **низкая**. Turn Runner — линейный pipeline с 5–6 шагами. `async/await` + dependency injection — достаточно.

Для полной системы (Этапы 3–4): **средняя**. Continuity Engine и Memory Scribe добавляют ветвление и human-in-the-loop. Всё ещё реализуемо вручную, но код разрастается.

---

### Вариант B: LangGraph для всех workflows

Использовать LangGraph `StateGraph` для определения Turn Runner, Memory Extraction, Continuity Check и других workflow как графов состояний.

**Архитектура:**

```python
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.sqlite import SqliteSaver

class TurnState(TypedDict):
    campaign_id: str
    user_input: str
    context: dict | None
    llm_response: str | None
    continuity_issues: list | None
    delta: dict | None
    extractions: list | None

workflow = StateGraph(TurnState)

workflow.add_node("compile_context", compile_context_node)
workflow.add_node("call_llm", call_llm_node)
workflow.add_node("check_continuity", check_continuity_node)
workflow.add_node("handle_issues", handle_issues_node)
workflow.add_node("commit_state", commit_state_node)
workflow.add_node("extract_memory", extract_memory_node)

workflow.add_edge("compile_context", "call_llm")
workflow.add_edge("call_llm", "check_continuity")
workflow.add_conditional_edges(
    "check_continuity",
    has_critical_issues,
    {"yes": "handle_issues", "no": "commit_state"}
)
workflow.add_edge("handle_issues", "call_llm")  # retry
workflow.add_edge("commit_state", "extract_memory")
workflow.add_edge("extract_memory", END)

workflow.set_entry_point("compile_context")

checkpointer = SqliteSaver.from_conn_string("checkpoints.db")
app = workflow.compile(checkpointer=checkpointer)
```

**Плюсы:**

- **Декларативный граф**: workflow определён как граф с узлами и рёбрами. Визуально понятная структура, генерация диаграммы из кода.
- **Checkpointing из коробки**: LangGraph сохраняет состояние после каждого узла. При crash — возобновление с последнего checkpoint. Для длинных workflow (rebuild memory из 1000+ ходов) — полезно.
- **Human-in-the-loop**: встроенная поддержка `interrupt_before` / `interrupt_after` — граф останавливается, ждёт input от пользователя, продолжает.
- **Conditional edges**: ветвление по условиям — декларативно, не в коде.
- **Streaming**: LangGraph поддерживает streaming через `stream()` / `astream()`. Можно стримить промежуточные состояния.
- **LangSmith интеграция**: если подключить LangSmith, можно трассировать каждый вызов, каждый шаг. Полезно для дебага LLM-пайплайнов.
- **Subgraphs**: можно вложить один граф в другой (например, Memory Extraction как подграф Turn Runner).
- **Time travel**: LangGraph checkpointing позволяет «откатить» workflow к предыдущему шагу. Концептуально близко к Campaign Debugger (§5).

**Минусы:**

- **Тяжёлая зависимость**: LangGraph тянет `langchain-core`, `langgraph`, `langgraph-checkpoint`, `langgraph-sdk` и потенциально десятки transitive dependencies. `langchain-core` — активно развивающийся пакет с частыми breaking changes. Обновление одной зависимости может сломать workflow.
  
  Текущий размер зависимостей (примерный):
  ```
  langchain-core: ~50 MB installed
  langgraph: ~10 MB installed
  langgraph-checkpoint-sqlite: ~5 MB installed
  + transitive: pydantic, jsonpatch, tenacity, etc.
  ```

- **Конфликт state stores**: LangGraph имеет собственный checkpointer (SQLite/Postgres), который хранит промежуточные состояния workflow. Наш Campaign State хранится в своей SQLite/Postgres. Два state store — потенциальный источник рассинхронизации. Какой из них source of truth для «текущего состояния хода»?

- **Абстракция leak**: LangGraph оборачивает каждый шаг в свои абстракции (`Runnable`, `RunnableConfig`). Это удобно для LangChain ecosystem, но создаёт friction для нашего кода, который не использует LangChain для LLM вызовов (у нас свой OpenAI-compatible adapter).

- **Дебагабельность**:
  - Плюс: LangSmith + граф-визуализация.
  - Минус: stack traces через LangGraph internals — длинные и неочевидные. `Runnable.__call__` → `Channel.update` → ... → наш код. Для Python-разработчика, незнакомого с LangGraph, — это «руны, написанные на забытом языке».

- **Тестируемость**: LangGraph nodes — это функции `(state: TurnState) -> dict`. Тестировать их отдельно — просто. Тестировать весь граф — нужно настроить checkpointer, mock nodes, ассертить промежуточные состояния. Больше boilerplate, чем у чистого Python.

- **Производительность**: каждый node invocation имеет overhead: сериализация состояния, checkpointing, channel management. Для Turn Runner с 5–6 шагами — несколько миллисекунд. Незначительно по сравнению с LLM call (секунды), но складывается при массовых операциях (rebuild, reindex).

- **Breaking changes**: LangGraph активно развивается (v0.x → v1.x). LangChain ecosystem известен частыми изменениями API. Миграция между мажорными версиями может потребовать переписывания workflow определений. Для продукта с расчётом на годы — это риск.

- **Vendor lock-in**: workflow описаны в терминах LangGraph API. Переход на другой оркестратор = переписывание всех графов. Бизнес-логика в nodes остаётся, но клей между ними — LangGraph-специфичный.

- **Overhead для простых workflows**: Scene Transition — это 3–4 шага без ветвления. Campaign Export — линейный pipeline. Использовать StateGraph для таких — overhead без пользы.

- **Community perception**: в AI/ML community LangChain/LangGraph имеет поляризующую репутацию. Часть разработчиков считает его unnecessary abstraction layer. Для open-source продукта это может отпугнуть контрибьюторов.

**Сложность реализации:**

Начальная: **средняя**. Нужно изучить LangGraph API, настроить checkpointer, адаптировать наши сервисы в nodes.

Поддержка: **высокая**. Следить за обновлениями LangGraph, мигрировать при breaking changes, дебажить через LangGraph internals.

---

### Вариант C: Гибрид — LangGraph для сложных multi-step workflows, собственный код для простых

Разделить workflows по сложности:

- **Собственный код** (async Python): Turn Runner (основной pipeline), Scene Transition, Campaign Export/Import, простые CRUD-операции.
- **LangGraph**: Memory Extraction Pipeline (complex, multi-step, human-in-the-loop), Continuity Check Pipeline (complex, conditional branching, partial auto-fix), Rebuild Pipeline (длинный, нужен checkpointing для resume).

**Архитектура:**

```
Turn Runner (собственный async Python)
  ├── Context Compiler → наш код
  ├── LLM Call → наш OpenAI-compatible adapter
  ├── Continuity Check → LangGraph subgraph (если сложный)
  ├── State Commit → наш код
  └── Memory Extraction → LangGraph subgraph (human-in-the-loop)

Scene Transition → наш async Python
Campaign Export → наш async Python
Rebuild Memory → LangGraph (checkpointing для длинных операций)
```

**Плюсы:**

- **Лучшее из двух миров**: простые workflow — чистый Python (нет overhead), сложные — LangGraph (checkpointing, human-in-the-loop).
- **Гибкость**: можно начать без LangGraph, добавить его для конкретных workflow, когда сложность оправдает зависимость.
- **Постепенная миграция**: если LangGraph окажется проблематичным — убираем его только из конкретных workflows, не из всей системы.

**Минусы:**

- **Два паттерна оркестрации** в одной кодовой базе. Разработчик должен знать оба. Нет единого подхода — документация усложняется.
- **Граница** между «простым» и «сложным» workflow — субъективна и со временем сдвигается. Turn Runner «простой» сегодня, но через полгода с conditional retry, parallel memory extraction и human-in-the-loop — уже сложный.
- **Интеграция**: LangGraph subgraph внутри собственного pipeline — нужен glue code. Async context, error propagation, streaming — нетривиально.
- **Зависимость всё равно есть**: даже если LangGraph используется для 2 из 6 workflows — `langchain-core` всё равно в `requirements.txt`.
- **Тестирование**: два типа тестов для двух типов workflows.

**Сложность реализации:**

Начальная: **средняя**. По сути — Вариант A + интеграция LangGraph для отдельных pipeline.

Поддержка: **средняя**. Два паттерна, но изолированные.

---

### Вариант D: Temporal.io / другой workflow engine

Использовать полноценный workflow engine (Temporal, Prefect, Airflow, Dagster) для оркестрации.

**Temporal.io:**

```python
from temporalio import workflow, activity

@activity.defn
async def compile_context(campaign_id: str, user_input: str) -> dict:
    ...

@activity.defn
async def call_llm(context: dict) -> str:
    ...

@workflow.defn
class TurnWorkflow:
    @workflow.run
    async def run(self, campaign_id: str, user_input: str) -> TurnResult:
        context = await workflow.execute_activity(
            compile_context, args=[campaign_id, user_input],
            start_to_close_timeout=timedelta(seconds=30)
        )
        response = await workflow.execute_activity(
            call_llm, args=[context],
            start_to_close_timeout=timedelta(seconds=120)
        )
        ...
```

**Плюсы:**

- **Production-grade**: Temporal — battle-tested workflow engine, используемый Netflix, Uber, Stripe. Надёжный checkpointing, retry, timeout.
- **Deterministic replay**: workflow можно воспроизвести для дебага.
- **Дурабельные workflows**: workflow выживает crash server. Идеален для rebuild/reindex pipeline.
- **Язык-агностичный**: Temporal SDK есть для Python, Go, Java, TypeScript. Можно в будущем вынести activities на другой язык.
- **Visibility**: Temporal Web UI показывает все запущенные workflow, их состояние, историю. Может пригодиться для Campaign Debugger.
- **Versioning**: поддержка версионирования workflow — можно мигрировать running workflows между версиями кода.

**Минусы:**

- **Отдельный сервер**: Temporal требует запуска `temporal-server` (Go binary + PostgreSQL/Cassandra). Для local-first desktop приложения — это архитектурный абсурд. Docker compose с Temporal server — это 3–4 контейнера (server, frontend, PostgreSQL, worker).
- **RAM**: Temporal server потребляет ~500 MB+ RAM. Для пользователя, который уже запускает LLM (4–16 GB VRAM), Python backend, Tauri — это неприемлемо.
- **Latency**: каждый activity call — это сетевой round-trip к Temporal server + persistence. Для Turn Runner, где latency = UX (пользователь ждёт ответ) — недопустимый overhead.
- **Operational complexity**: Temporal — это production-grade infrastructure. Для single-user desktop — как «вызвать легион, чтобы открыть дверь».
- **Зависимость**: temporal-sdk + temporal-server + PostgreSQL. Огромный deployment footprint.
- **Community**: для нашего use case (single-user game engine) Temporal community не имеет релевантных примеров. Все примеры — для microservices и enterprise workflows.
- **Overkill**: 100% overkill для нашего случая.

**Другие workflow engines:**

| Engine | Проблема для нас |
|--------|-----------------|
| **Prefect** | Ориентирован на data engineering. Требует Prefect server. Overkill. |
| **Airflow** | Batch-ориентирован (scheduled pipelines). Тяжёлый. Не для real-time. |
| **Dagster** | Data engineering. Тяжёлый. |
| **Celery** | Task queue, не workflow engine. Требует broker (Redis/RabbitMQ). |
| **Dramatiq** | Аналогично Celery. Task queue. |

**Вердикт по всем внешним workflow engines:** не подходят для local-first single-user desktop application. Они проектировались для distributed server environments.

**Сложность реализации:**

Начальная: **высокая** (deployment Temporal + integration).

Поддержка: **высокая** (Temporal server management).

---

## Сравнительная таблица

| Критерий | A: Собственный | B: LangGraph all | C: Гибрид | D: Temporal |
|----------|:-:|:-:|:-:|:-:|
| **Сложность начальная** | Низкая | Средняя | Средняя | Высокая |
| **Зависимости** | Нет (stdlib) | langchain ecosystem | langchain ecosystem | temporal-server + SDK |
| **Checkpoint конфликт** | Нет | Да (два state stores) | Частично | Нет (другой scope) |
| **Дебагабельность** | ✅✅✅ (стандартные инструменты) | ✅ (LangSmith) ⚠️ (stack traces) | ✅✅ | ✅✅ (Temporal UI) |
| **Тестируемость** | ✅✅✅ (pytest, mocks) | ✅✅ (но boilerplate) | ✅✅ | ✅✅ |
| **Vendor lock-in** | Нет | Средний (LangChain) | Низкий–средний | Высокий (Temporal) |
| **Overhead per turn** | ~0 ms | ~1–5 ms | ~0–5 ms | ~10–50 ms |
| **Streaming** | ✅✅✅ (native async) | ✅✅ (через LangGraph API) | ✅✅✅ | ⚠️ (не нативное) |
| **Human-in-the-loop** | Ручная реализация | ✅ (встроенная) | ✅ (для LG workflows) | ✅ (встроенная) |
| **Checkpointing** | Ручная реализация | ✅ (встроенный) | Частично | ✅ (встроенный) |
| **Визуализация графа** | ❌ (вручную) | ✅ (из кода) | Частично | ✅ (Temporal UI) |
| **Community** | N/A | Большое, поляризующее | N/A | Большое, enterprise |
| **Риск breaking changes** | Нет | Высокий (LangChain 0.x/1.x) | Средний | Низкий (stable API) |
| **Deployment footprint** | Нулевой | Минимальный | Минимальный | Огромный |
| **Подходит для local-first** | ✅✅✅ | ✅✅ | ✅✅ | ❌ |

---

## Дополнительный анализ: нужен ли нам checkpointing?

Checkpointing — главный аргумент в пользу LangGraph. Но нужен ли он для наших workflows?

### Turn Runner

Один ход занимает 5–30 секунд (в основном LLM inference). Если backend крашится посреди хода:
- **Без checkpointing**: ход не применяется (rollback БД-транзакции). Пользователь отправляет ввод снова. UX: «произошла ошибка, попробуйте ещё раз». Приемлемо.
- **С checkpointing**: ход возобновляется с последнего узла. Но нужно ли? Crash посреди хода — это исключение, не правило. Retry обычно дешевле, чем resume.

**Вывод**: checkpointing для Turn Runner — nice-to-have, не must-have.

### Memory Rebuild (пересборка из сырого архива)

Пересборка 1000+ ходов — это долгая операция (минуты). Если крашится на ходе 500:
- **Без checkpointing**: начинаем сначала. Потерянное время.
- **С checkpointing**: возобновляем с хода 500.

**Вывод**: checkpointing для Rebuild — полезен. Но его можно реализовать проще: записывать `last_processed_turn_id` в БД. Это 5 строк кода, не целый framework.

### Memory Extraction (human-in-the-loop)

Memory Scribe предлагает факты/отношения → пользователь подтверждает/отклоняет → commit. Если пользователь закрыл приложение между предложением и подтверждением:
- **Без checkpointing**: предложенные extractions хранятся в БД со статусом `proposed`. При следующем запуске — показать их снова.
- **С checkpointing**: LangGraph запоминает, что workflow остановлен на `human_review` node. При запуске — продолжает.

**Вывод**: human-in-the-loop можно реализовать через состояния в собственной БД (`proposed` → `approved` / `rejected`). LangGraph checkpointing — альтернативный подход, но добавляет второй state store.

---

## Рекомендация

**Рекомендуемый вариант: A (Собственный Turn Runner) с опцией миграции на C (Гибрид) в будущем.**

### Обоснование

1. **MVP (Этапы 0–2)**: Turn Runner — это линейный pipeline с 5–6 шагами. `async/await` + dependency injection — достаточно и избыточно просто. Добавлять LangGraph на этом этапе — это «призывать демона для зажигания свечи».

2. **Нет конфликта состояний**: Campaign State — единственный source of truth. LangGraph checkpointer создал бы второй state store, который нужно синхронизировать. Для продукта, чья killer feature — проверяемый канон — это опасный паттерн.

3. **DX**: чистый Python-код читается проще, дебажится стандартными инструментами, тестируется pytest без boilerplate. Новый разработчик продуктивен за час, а не за день изучения LangGraph.

4. **Нет vendor lock-in**: LangChain ecosystem активно меняется. Для продукта с горизонтом в годы — зависимость от быстро меняющегося фреймворка создаёт технический долг.

5. **Checkpointing**: для Turn Runner не нужен (ход атомарен). Для Rebuild — реализуется за 5 строк (`last_processed_turn_id`). Для human-in-the-loop — реализуется через статусы в БД.

6. **Streaming**: нативный `async for` даёт полный контроль. LangGraph streaming API — дополнительный слой абстракции, который мы не контролируем.

### Когда пересмотреть

Пересмотреть решение в пользу **Варианта C (Гибрид)**, если:

- Количество workflows превысит 8–10 с нетривиальным ветвлением.
- Human-in-the-loop станет сложнее, чем `proposed → approved/rejected` (например, multi-step review с partial approve).
- Появится потребность в **time travel** (откат workflow к предыдущему шагу, не просто данных) — это пересечение с Campaign Debugger.
- LangGraph стабилизируется (v1.0+ с backwards-compatible API).

### Шаблон собственного Turn Runner

Для стандартизации workflows без фреймворка рекомендуется создать минимальный внутренний паттерн:

```python
class Pipeline(Generic[TState]):
    """Минимальный pipeline runner для стандартизации workflows."""
    
    def __init__(self, steps: list[Step[TState]]):
        self.steps = steps
    
    async def run(self, state: TState) -> TState:
        for step in self.steps:
            try:
                state = await step.execute(state)
                await self.log_step(step, state)
            except StepError as e:
                state = await self.handle_error(step, state, e)
        return state

class Step(Generic[TState], ABC):
    """Один шаг pipeline."""
    
    @abstractmethod
    async def execute(self, state: TState) -> TState: ...
    
    def should_skip(self, state: TState) -> bool:
        return False
```

Это даёт:
- Единый паттерн для всех pipeline.
- Логирование каждого шага.
- Error handling.
- Skip logic.
- Без внешних зависимостей.

---

## Последствия

1. **Turn Runner**: реализуется как набор async Python-сервисов с dependency injection. Pipeline pattern для стандартизации.

2. **Checkpointing для Rebuild**: `last_processed_turn_id` + `batch_size` + database transaction per batch. Простой, надёжный, прозрачный.

3. **Human-in-the-loop для Memory Scribe**: proposed extractions хранятся в Campaign State со статусом `proposed`. UI показывает pending extractions. Пользователь approve/reject. Это состояние переживает restart — потому что оно в БД, не в workflow engine.

4. **Streaming**: WebSocket или SSE от FastAPI. `async for chunk in llm.stream(...)` → `await websocket.send(chunk)`. Полный контроль.

5. **Тестирование**: каждый шаг pipeline — unit-тестируемый сервис. Integration test — mock LLM gateway, реальная SQLite, полный pipeline.

6. **Визуализация**: Mermaid-диаграммы workflow в документации (ручное обновление при изменении pipeline). Или автогенерация из Pipeline.steps.

7. **LangGraph не исключён навсегда**: если через 6–12 месяцев ветвление и human-in-the-loop станут сложными, Pipeline pattern позволяет обернуть шаги в LangGraph nodes без переписывания бизнес-логики. Шаги останутся теми же — изменится только оркестратор.

8. **LangChain для LLM**: отказ от LangGraph **не означает** отказ от `langchain-core` или `litellm` для LLM-вызовов. Adapter для LLM-провайдеров — отдельное решение, не связанное с оркестрацией workflows.

---

## Связанные решения

- **§8 product-foundation**: «LangGraph только для workflow/checkpoints, если подтвердит пользу» — пока не подтвердил. Для MVP польза не оправдывает зависимость.
- **§9 product-foundation**: Логическая архитектура — Turn Runner, Context Compiler, Memory Scribe, Continuity Checker.
- **§11 product-foundation**: Обработка одного хода — описывает pipeline, который реализует Turn Runner.
- **§5 product-foundation**: Campaign Debugger — если потребуется time travel по workflow (не только по данным), LangGraph может быть пересмотрен.
- **ADR-004**: Desktop shell. Turn Runner работает внутри Python backend (sidecar или web-only). Выбор оркестратора не зависит от shell.
