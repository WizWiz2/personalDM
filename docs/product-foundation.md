# Личный ДМ
## Фундамент продукта

**Статус:** канонический рабочий документ  
**Версия:** 0.4  
**Дата:** 24 августа 2026

## 1. Продукт

«Личный ДМ» — локальное приложение для продолжительных AI-кампаний в **systemless narrative RPG**.

LLM создаёт повествование, персонажей и интерпретации, но не является источником истины. Источник истины — **Campaign Truth Engine**, который хранит канон, состояние сцен и локаций, знания персонажей, structured consequences и происхождение памяти отдельно от художественной прозы.

Продукт должен ощущаться как разговор с живым личным мастером, а не как админка RPG-движка. Структура существует ради устойчивости и прозрачности, но пользовательский основной экран остаётся текстовой игрой.

## 2. Основная гипотеза

Проблема длинных AI-кампаний двойная:

1. распадается причинность и память — NPC знают лишнее, забываются отношения, положение персонажей и последствия;
2. попытка исправить это чрезмерными guard/prompt правилами может уничтожить саму игру — Narrator становится деревянным, шаблонным и боится двигать сцену.

> Пользователь выберет локального AI-ДМа, если он одновременно удерживает проверяемую причинность **и** пишет связную, атмосферную, живую прозу без управления героем игрока.

Authority correctness и художественное качество — две равноправные оси продукта.

## 3. Ключевые принципы

### Campaign Truth Engine

Система различает:

- объективное состояние мира;
- физическое присутствие;
- восприятие;
- субъективное убеждение;
- источник знания;
- текущее намерение;
- transient narrative detail;
- художественную формулировку.

### Human owns the protagonist

Игрок владеет добровольными действиями, репликами, решениями, мыслями и эмоциональными выводами своего героя. Narrator описывает последствия и реакцию мира, но не дописывает следующий выбор за игрока.

### Truth before prose

Для значимого narrative turn сначала определяется и исполняется structured authority, затем Narrator рендерит результат.

Если prose расходится с authority, исправляется prose. Уже выполненный structured outcome не переписывается ради удобства Narrator.

### Fail closed for truth, degrade visibly for presentation

При неоднозначном placement/movement/NPC identity система предпочитает не выдумывать состояние.

Если художественный pipeline не смог безопасно отрендерить уже известный исход, допускается deterministic presentation fallback. Такой fallback должен быть диагностически видим как degraded mode и не считаться успешной художественной генерацией.

### Transparency over mystique

Для каждого плохого хода должна существовать причинная цепочка: input → plan → execution → authority → raw draft → validation/repair → publication → memory.

Внутренняя прозрачность не означает prompt leakage игроку: технические snapshots принадлежат debugger’у, а не обычному `/DM` ответу.

## 4. Нулевая сессия

Session Zero — свободный разговор, а не анкета.

Агент скрыто поддерживает structured draft мира и героя. Когда информации достаточно, он сам может безопасно достроить технический минимум и завершить setup.

Handoff в игру должен быть чистым:

1. финальная реплика Session Zero не задаёт нового вопроса;
2. материализуются герой, стартовая Location и первая Scene;
3. Narrator автоматически создаёт крупный opening post;
4. opening сохраняется как первый system-owned assistant turn Scene;
5. пользователь сразу играет — отдельная команда «Начинаем» не нужна.

## 5. TurnAuthority

Обычный narrative turn имеет один typed `TurnAuthority`, который соединяет Planner, deterministic executors, Narrator, Validator и materializer.

Упрощённая цепочка:

```text
player input
→ ContextCompiler
→ TurnAuthorityPlanner
→ deterministic action/transition execution
→ TurnAuthority
→ Narrator render
→ Validator / deterministic agency checks
→ optional repair
→ accepted prose or deterministic presentation fallback
→ materialization + commit
→ background memory jobs
```

### Владение решениями

- player input и voluntary protagonist action — человек;
- turn resolution — Planner в рамках systemless policy;
- movement/Scene/Location — deterministic execution;
- allowed NPC introduction — typed plan + deterministic materialization;
- prose/style — Narrator;
- continuity/agency verdict — Validator + deterministic guards;
- long-term memory proposals — post-turn memory pipeline.

## 6. Контекст и знания NPC

Narrative/actor context не должен содержать все секреты кампании.

Для acting NPC передаются только сведения, которые ему доступны по visibility, beliefs, relationships, scene participation и persisted memory.

Недоступные сведения не сопровождаются просьбой «не используй». Они по возможности вообще не входят в actor-scoped prompt.

## 7. Память

### Сырой архив

SQLite хранит user/assistant/meta turns, context snapshots, generation runs, validation audit, transitions/action sequences, post-turn jobs и происхождение memory changes.

### Структурированная память

- facts;
- beliefs;
- relationships;
- events;
- goals;
- scene theses;
- entity/location state.

### Transient narrative detail

Жест, звук, поза, краткая фактура и другие художественные детали могут жить несколько ходов как `narrative_detail`, не становясь вечным каноном.

### Scene Thesis

`Scene Thesis` — рабочая режиссёрская память сцены: конфликт, намерение, напряжение, незавершённый сюжетный момент или другое утверждение, которое необходимо удерживать активным.

## 8. Narrator quality

Narrator считается успешным не только когда «не соврал».

Live acceptance оценивает:

- локальную и глобальную связность;
- continuity;
- scene-specific конкретность;
- атмосферу без generic filler;
- вариативность языка;
- естественность русского текста;
- драматургическую функцию;
- отсутствие управления героем;
- repair/safe-fallback rate.

Raw draft и published text сравниваются отдельно, чтобы отличать model quality от damage post-generation pipeline.

## 9. Campaign Debugger

Debugger должен позволять ответить не «почему модель так решила?», а «на каком boundary система впервые отклонилась?».

Нужны:

- current Scene/Location/participants;
- context manifest;
- Planner output;
- TurnAuthority;
- transitions/action sequences;
- raw narration validation attempts;
- final publication mode;
- model/latency/token telemetry;
- materialization;
- memory jobs/proposals/persisted state.

Текущая карта и observability gaps описаны в `runtime-transparency.md`.

## 10. Модели

Локальная default-конфигурация разделяет creative и control работу:

- campaign primary (сейчас Gemma) — Narrator, Session Zero, direct Game Master dialogue;
- control model (сейчас Qwen) — Planner, Validator, Scribe и другие structured roles.

Control role не должен тихо менять модель при schema failure: такой fallback меняет семантику runtime и затрудняет диагностику.

## 11. UI

Целевая форма — локальный React/Vite UI поверх FastAPI и SQLite; Tauri остаётся возможной desktop-shell стадией.

Campaign Library отвечает за создание/продолжение/настройки/архив/удаление кампаний.

Игровые `/DM`, `/facts`, `/undo`, talk mode и scene actions принадлежат активной кампании, а не библиотеке.

Product UI сознательно не использует CRPG vocabulary вроде HP/level/stats без реального rules layer.

## 12. Изображения

ComfyUI — локальный best-effort atmosphere layer.

Текущий путь поддерживает портреты персонажей, campaign cover и scene art. Визуальная ошибка не изменяет канон и не отменяет игровой ход.

Visual consistency важна, но truth engine не должен зависеть от image model.

## 13. Не входит в текущий core

- универсальная система правил;
- D&D/Shadowrun/PF mechanics engine;
- тактическая VTT;
- обязательные характеристики/HP/levels;
- PostgreSQL/server mode;
- multiplayer;
- полноценное дерево веток/merge;
- автоматическое принятие картинки за факт мира;
- отдельный voice model для каждого NPC.

Rules engine может появиться позже как изолированный слой; добавление декоративных чисел без механики не считается прогрессом продукта.

## 14. Критерий доказанной идеи

PersonalDM доказал идею, когда пользователь может провести длинную кампанию и одновременно получить:

1. устойчивую причинность и placement;
2. корректные границы знаний NPC;
3. возможность объяснить происхождение канона и плохого хода;
4. живой, связный Narrator, который не отнимает agency;
5. удобное продолжение после перезапуска;
6. локальную атмосферу — текст и изображения — без зависимости truth engine от генеративного слоя.
