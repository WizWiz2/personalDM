# Маршрутизация моделей по ролям

Personal DM использует основную модель кампании для художественного ответа и отдельную
локальную control-модель для коротких структурированных задач.

Рекомендуемая конфигурация для текущих локальных моделей:

```env
PDM_LLM_MODEL=gemma4:e4b
PDM_CONTROL_LLM_MODEL=qwen2.5:7b
PDM_CURATOR_INTERVAL_TURNS=3
PDM_SIM_PLAYER_MODE=deterministic
PDM_SIM_EVALUATOR_INTERVAL_TURNS=2
```

Основная модель кампании остаётся Narrator и Character Builder. `Memory Scribe`,
`Thesis Curator`, benchmark Evaluator и structured repair используют control-модель.
Для отдельных ролей модель можно переопределить:

```env
PDM_SCRIBE_LLM_MODEL=qwen2.5:7b
PDM_CURATOR_LLM_MODEL=qwen2.5:7b
PDM_EVALUATOR_LLM_MODEL=qwen2.5:7b
PDM_CHARACTER_BUILDER_LLM_MODEL=gemma4:e4b
```

По умолчанию обе локальные модели используют endpoint кампании. Для отдельного control
endpoint задаются `PDM_CONTROL_LLM_BASE_URL`, `PDM_CONTROL_LLM_API_KEY` и
`PDM_CONTROL_LLM_CONTEXT_WINDOW`. Секрет основной кампании никогда автоматически не
передаётся на другой endpoint.

Если control-модель недоступна, production Scribe и Curator один раз переходят на
основную модель кампании. Телеметрия помечает такой вызов `role_router_fallback=true`.

В автономном benchmark игрок по умолчанию детерминированный и не расходует LLM-запрос.
Evaluator запускается раз в два хода после минимальной длины сцены. Curator в обычной
игре запускается на первом ходе сцены и затем раз в три хода.
