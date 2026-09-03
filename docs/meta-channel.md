# Meta channel: `/DM` и `/OOC`

**Статус:** current implementation contract  
**Владелец:** `MetaCommandRunner`; deterministic routing — `parse_meta_command()`.

## Нормативный контракт

Meta channel — read-only разговор игрока с мастером о кампании и работе игры. Это не художественная сцена и не скрытая команда на изменение мира.

Инварианты:

- только явная leading-команда `/DM` или `/OOC` входит в meta channel;
- упоминание `/DM` внутри обычной фразы остаётся narrative input;
- meta path не вызывает Planner, transition/materialization, Scribe, Curator или post-turn jobs;
- meta turns хранятся отдельно как `meta_user` / `meta_assistant` и не входят в Narrator history;
- meta ответ может читать structured snapshot и объяснять ошибки причинности, но не меняет facts, time, relationships, placement или scene;
- если structured state расходится с prose, meta мастер должен назвать рассинхронизацию, а не придумывать скрытое сюжетное объяснение;
- внутренние prompt/control blocks нельзя публиковать игроку.

## Текущая реализация

`MetaCommandRunner._messages()` компилирует read-only snapshot текущей кампании и добавляет отдельный системный контракт. История meta-диалога загружается только из meta turns. Для генерации используется роль `GAME_MASTER`.

После ответа действует отдельная deterministic publication boundary `sanitize_meta_output()`. Если provider эхом выводит `<campaign_snapshot>`, `TYPED TURN AUTHORITY`, Character Card, Scene State или другой внутренний marker, ответ целиком заменяется безопасным публичным сообщением. В context snapshot сохраняется `output_sanitization` с причиной срабатывания.

Это намеренно fail-closed: частичная строковая редактура скрытого prompt неизвестной формы хуже полной блокировки утечки.

## Persisted evidence

- `meta_user` / `meta_assistant` turns;
- `parent_turn_id` пары;
- `context_snapshot.channel = meta`;
- `read_only = true`, `side_effect_pipeline = disabled`;
- provider telemetry и `output_sanitization` на meta assistant turn.

## Failure semantics

- нет provider → meta user turn помечается failed, world state неизменен;
- transport/provider error → world state неизменен;
- пустой answer → failed meta turn;
- internal prompt leakage → safe public replacement, world state неизменен;
- `/undo` narrative pair не должен поглощать более новую meta-пару.

## Проверка

Endpoints используют общий turns API; channel виден через `/api/campaigns/{id}/turns?channel=all`. Debugger даёт persisted evidence.

`tests/test_meta_commands.py` и runtime-observability tests проверяют routing, read-only semantics, context isolation, undo isolation и sanitization.

## Историческая граница

Старое поведение, где `/DM` попадал в Narrator и продолжал художественную сцену, не является допустимым fallback.