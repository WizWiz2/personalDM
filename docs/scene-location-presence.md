# Scene, Location и Physical Presence

**Статус:** current implementation contract  
**Владельцы:** `SceneTransitionExecutor`, `SceneStateService`, `PresenceService`, location/profile guards.

## Нормативные различия

`Scene` и `Location` — разные сущности. Location отвечает на вопрос «где физически находится объект», Scene — «какой текущий драматический/временной контекст активен». Одна Location может посещаться снова в другой Scene; возвращение в известное место не создаёт дубликат Location.

Инварианты:

- у кампании не должно быть двух одновременно authoritative current scenes;
- физическое перемещение игрока/персонажа существует только как structured transition/action, а не потому что Narrator написал «ты вышел»;
- `Character.current_location_id` и scene participants должны согласовываться;
- NPC не следует за игроком автоматически; прибытие требует typed authority;
- `dead`/`destroyed` персонаж не может получить физическое присутствие без отдельного авторитетного механизма восстановления;
- новая Location требует устойчивого profile/description до создания; известную Location можно посетить повторно без повторного полного описания;
- compound movement выполняется в заданном порядке; blocker останавливает хвост sequence;
- ambiguity в destination/presence должна fail closed, а не материализовать догадку модели.

## Текущий переход

Planner выражает намерение через `scene_transition` или ordered action sequence. `SceneTransitionExecutor` проверяет структурные предпосылки и подготавливает transition. `PresenceService` является policy owner физического placement. После PREPARED world state Narrator получает заново скомпилированный context и только описывает уже разрешённый результат.

Переход может создать новую Scene, переиспользовать существующую Location, создать новую Location с profile или вернуть героя в старое место. Старое prose history не может физически вернуть отсутствующего NPC.

`PlayerIntentInterpreter` получает текущие имена, идентификаторы предметов и местоположение, чтобы сохранить действия игрока без оценки их успеха. Попытка пройти через отсутствующий проход остаётся `movement`, а не превращается в `interaction`.

После извлечения действий точные названия мест связываются с БД без модели. Для остальных названий отдельная узкая проверка идентичности допускает только ID локаций текущей кампании или `new`; она не может переписать список действий или предложить другой адрес. Каталог мест не подаётся в извлечение намерения, чтобы знакомое место не подменяло явно выбранное новое. Перед фиксацией намерения найденный ID преобразуется в точное каноническое название из БД. Инструкции рассказчику о запрете ещё не утверждённых переходов исключаются из контекста разрешения исхода, но реальные препятствия и правила мира сохраняются.

Для последовательности обычных перемещений по известным местам `ActionPlanCompiler.resolve_known_travel` разрешает каждый переход по графу из достигнутой точки. Такой ход не создаёт NPC и не переносит спутников автоматически. Явное обращение к персонажу, незавершённый выбор, `movement_method=special`, активный конфликт, условия доступа или новый пункт назначения сохраняют модельное разрешение внешнего результата. В диагностике источник решения записывается как `outcome_owner`.

## Persisted evidence

- `locations` и их hierarchy/profile;
- `scenes` + current scene;
- `scene_location_links`/structured location linkage;
- `scene_transitions` с source/target scene/location и status;
- `scene_participants`;
- `Character.current_location_id`;
- `action_sequences` и ordered steps;
- `TurnAuthority` в assistant context snapshot.

При расследовании movement нужно смотреть все эти слои вместе, а не только финальный текст.

## Failure semantics

- невалидный новый destination/profile → transition отвергается до публикации;
- failure после PREPARED и до PUBLISHED → saga компенсирует prepared mutations;
- narrator prose, противоречащая structured placement, не меняет truth;
- невозможный step compound sequence блокирует последующие steps;
- stale presence удаляется policy owner при подтверждённом structured move.

## Проверка

Debugger: `/api/campaigns/{id}/debugger` и `/debugger/turns/{assistant_turn_id}`.

Deterministic tests проверяют executor/presence/transition invariants. Local `test-models.bat` проверяет реальные semantic transitions: known/new location, round trip A→B→A, NPC non-follow, compound movement, blocker, dead presence, undo movement и другие переходы из `TRUTH-TRANSITION-MATRIX.md`.

## Историческая граница

Любая старая логика, где физическое состояние восстанавливалось из Narrator prose, не является current architecture.
