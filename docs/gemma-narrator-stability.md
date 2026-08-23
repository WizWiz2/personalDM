# Стабильность локального Narrator на Gemma

Текущий default Narrator — campaign primary model, локально `gemma4:e4b`. Этот документ описывает не «качество Gemma вообще», а условия, в которых PersonalDM даёт ей художественную работу.

## Базовый принцип

Gemma не решает truth state. До Narrator уже существуют:

- player input;
- structured Planner result;
- выполненные deterministic boundaries/action sequence;
- typed `TurnAuthority`.

Narrator получает задачу **отрендерить уже утверждённый непосредственный результат как естественную русскую прозу**.

## Context budget

Default context window остаётся 4096:

```env
PDM_LLM_CONTEXT_WINDOW=4096
PDM_RESPONSE_RESERVE_TOKENS=1536
PDM_SAFETY_MARGIN_PERCENT=0.05
```

Planner вызывается раньше отдельным structured request, поэтому его reserve больше не вычитается из финального Narrator context. `narrator_quality_recovery_guard` рассчитывает бюджет как:

```text
context_window
- response reserve
- safety margin
```

При 4096 это примерно 2356 токенов context budget до provider tokenization overhead.

В `context_snapshot` для narrator-turn сохраняются:

- `planner_reserve_removed_from_narrator_budget=true`;
- `final_narrator_context_budget`.

Это важно для live диагностики: если художественный ответ деградирует, сначала проверяется реальный budget, а не только имя модели.

## История и transient detail

По умолчанию compiler держит ограниченную историю:

```env
PDM_NARRATOR_HISTORY_LIMIT=12
PDM_NARRATOR_STAGNATION_TURNS=2
PDM_NARRATOR_RECEIPT_MAX_ITEMS=6
```

Старые устойчивые сведения должны приходить не через длинный prose tail, а через facts, beliefs, character cards, active theses, scene state и компактные structured последствия.

`narrative_detail` — краткоживущая фактура текущей сцены. Она не становится каноном только потому, что Narrator однажды её написал.

## Compact typed authority

После построения `TurnAuthority` Narrator получает не полный audit JSON, а compact render payload. В него входят только поля, нужные для художественного ответа, например:

- exact `player_input`;
- player/acting character;
- source/target location;
- present и known-absent characters;
- allowed new NPCs;
- resolution;
- observable consequences;
- narration guidance;
- compact completed/blocked action steps.

Цель — не заставлять маленькую модель одновременно читать художественный контекст и огромный юридический протокол.

## Player agency guard

До публикации deterministic guard дополнительно проверяет два класса живых регрессий:

1. **speech inversion** — прямая реплика игрока не может внезапно стать репликой NPC/мира;
2. **added protagonist agency** — Narrator не может дописать герою новое добровольное действие, решение, мысль или эмоцию, которых нет в player input.

Пример запрещённого поведения:

```text
Игрок: Я оглядываюсь.
- Кто здесь?

Narrator: Кто-то отвечает:
- Кто здесь?
Александр быстро оборачивается и осторожно идёт к голосу.
```

Такой draft должен получить `player_agency` violation и уйти на repair/fallback.

## Repetition guard

Перед Validator кандидат сравнивается с недавними опубликованными ответами. При near-verbatim повторе Narrator получает ровно одну regeneration попытку. Если повтор сохраняется, pipeline не публикует дубликат как нормальную прозу и переходит к deterministic presentation fallback.

## Validator и repair

Обычный happy path:

```text
Gemma draft
→ Qwen Narration Validator
→ pass
→ publish draft
```

При semantic violation:

```text
Gemma draft
→ Qwen: repair_required
→ Gemma targeted repair
→ Qwen validate repair
→ publish repair OR deterministic fallback
```

Validator не имеет права менять truth state. Он проверяет только соответствие prose уже существующему authority.

## Presentation fallback

Если repair не смог дать безопасную прозу, `NarrationPublicationGuard` строит deterministic текст из authority.

Это **не считается хорошим Narrator output**. Для quality metrics `safe_fallback` должен отслеживаться отдельно от model prose.

После live-регрессии #116 fallback дополнительно не имеет права лгать, что «ничего не меняется», если structured outcome уже произошёл:

- успешный location transition должен хотя бы сообщить, что герой оказался в target location;
- выполненный observation может сообщить, что новых подтверждённых деталей нет.

## Opening scene

После завершения Session Zero создаётся отдельный system-owned `assistant` turn стартовой Scene. Opening использует campaign Narrator и отдельный prompt на более крупный вводный пост: 4–7 абзацев, место, атмосфера, concrete hook.

Opening не должен:

- требовать пользовательского «Начинаем»;
- придумывать добровольное действие/мысль/эмоцию героя;
- добавлять физических NPC сверх `starter_npcs`;
- показывать внутренние инструкции.

Provider failure или слишком короткий opening заменяется grounded fallback из Session Zero state; повторный finalize opening не дублирует.

## Как оценивать качество

Нельзя судить о Gemma только по published text. Для каждого плохого turn нужно различать:

- `RAW GOOD / PUBLISHED BAD` — проблема validation/repair/publication pipeline;
- `RAW BAD / PUBLISHED BAD` — проблема Narrator prompt/context/model;
- `RAW BAD / PUBLISHED GOOD` — repair полезен;
- `RAW GOOD / PUBLISHED GOOD` — здоровый путь.

Помимо authority correctness live smoke должен оценивать связность, continuity, конкретность, естественность русского текста, вариативность языка и драматургическую функцию. Deterministic fallback в художественный score не включается.
