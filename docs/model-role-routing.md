# Маршрутизация моделей по ролям

PersonalDM использует две логические группы моделей: художественную primary model кампании и стабильную control-plane model для structured-задач.

Текущие локальные defaults:

```env
PDM_LLM_MODEL=gemma4:e4b
PDM_CONTROL_LLM_MODEL=qwen2.5:7b
PDM_LLM_CONTEXT_WINDOW=4096
```

## Фактические роли

| Role | Default source | Назначение |
|---|---|---|
| `NARRATOR` | campaign primary | художественный ответ и opening scene |
| `GAME_MASTER` | campaign primary | `/DM` / `/OOC` read-only meta dialogue |
| `SESSION_ZERO` | campaign primary | разговорная нулевая сессия |
| `CHARACTER_BUILDER` | campaign primary, если нет override | творческая карточка персонажа |
| `PLANNER` | control model | typed план исхода хода |
| `NARRATION_VALIDATOR` | control model | continuity/agency validation прозы |
| `ENTITY_REGISTRAR` | control model | legacy/background entity extraction |
| `SCRIBE` | control model | memory proposals |
| `CURATOR` | control model | scene thesis lifecycle |
| `EVALUATOR` | control model | benchmark/evaluation |
| `PLAYER` | control model | LLM player в simulation mode |
| `SCENARIO_BUILDER` | control model | benchmark/scenario generation |
| `STRUCTURED_REPAIR` | control model | bounded schema repair |

Точный список задан в `ModelRole` и `CONTROL_ROLES` в `app/services/role_model_router.py`.

## Почему control roles strict

Planner, Validator, Scribe и другие control roles являются частью семантики движка. Если Qwen не смог вернуть валидную структуру после собственных bounded repair attempts, runtime **не должен тихо переключить эту роль на Gemma**.

Причина проста: такой fallback меняет не только качество, но и поведение authority pipeline. Вместо этого ошибка поднимается вызывающему слою, где используется conservative deterministic fallback/containment.

Поэтому для `CONTROL_ROLES` `fallback_config` совпадает с самой control-конфигурацией и `has_distinct_fallback == false`.

## Overrides

Для отдельных ролей можно задать модель явно:

```env
PDM_PLANNER_LLM_MODEL=qwen2.5:7b
PDM_NARRATION_VALIDATOR_LLM_MODEL=qwen2.5:7b
PDM_SCRIBE_LLM_MODEL=qwen2.5:7b
PDM_CURATOR_LLM_MODEL=qwen2.5:7b
PDM_EVALUATOR_LLM_MODEL=qwen2.5:7b
PDM_CHARACTER_BUILDER_LLM_MODEL=gemma4:e4b
```

Control endpoint также можно отделить:

```env
PDM_CONTROL_LLM_BASE_URL=http://127.0.0.1:11434/v1
PDM_CONTROL_LLM_API_KEY=
PDM_CONTROL_LLM_CONTEXT_WINDOW=4096
```

Если control endpoint отличается от campaign primary endpoint, ключ основной кампании автоматически туда не передаётся.

## Telemetry

Structured calls должны оставлять telemetry минимум с:

- `model_role`;
- `role_model_source` (`campaign_primary`, `control_default`, `role_override`);
- `role_router_fallback`;
- фактическим `model`;
- transport/provider status;
- usage и duration, когда provider их возвращает.

Для narrative turn часть этой telemetry сохраняется в `assistant turn.context_snapshot.provider_telemetry` и затем попадает в playtest trace.

## Важный нюанс: «модель кампании»

Default в коде — Gemma, но конкретная существующая campaign хранит собственный provider config в SQLite. Поэтому по одному `config.py` нельзя доказать, какая модель реально обслужила конкретный старый ход.

Для расследования live playtest источник истины — persisted telemetry/model_name конкретного turn, а не текущий default.

## Character Builder

`CHARACTER_BUILDER` остаётся primary model без override. Это сделано сознательно: creative character draft не должен автоматически пересочиняться другой моделью только потому, что structured normalization дала ошибку.

Если понадобится cross-model normalization карточек, это должен быть отдельный явно наблюдаемый этап, а не скрытый fallback.
