# NPC Identity и Materialization

**Статус:** current implementation contract  
**Владельцы:** `TurnAuthorityPlanner`, identity reconciliation/resolver, `TurnOutcomeMaterializer`, `PresenceService`.

## Нормативный контракт

NPC — durable identity, а не имя, случайно появившееся в прозе. До первого физического появления Planner должен либо связать упомянутого человека с существующим Character, либо разрешить создание новой identity.

Инварианты:

- одна реальная identity не должна раскалываться на несколько Character из-за alias/позднего раскрытия имени;
- temporary role identity допустима только как читаемая роль (`Диспетчер`, `охранник`), а не synthetic placeholder вроде «Безымянный собеседник»;
- temporary identity может быть promoted в stable canonical name без создания второго NPC;
- новый NPC должен иметь содержательное public description и portrait-ready appearance; voice опционален;
- существующий отсутствующий NPC может прибыть только через `allowed_existing_npc_arrivals`/typed authority;
- новый NPC может материализоваться только через `allowed_new_npcs`/structured outcome;
- `dead` и `destroyed` не могут быть физически материализованы обычным упоминанием;
- Narrator prose само по себе не регистрирует новую identity.

## Текущая реализация

Planner возвращает structured introductions/arrivals. Identity resolver очищает имена и роли, reconciler ищет уже известную identity/alias и решает create/promote/reuse. `TurnOutcomeMaterializer` создаёт разрешённые Character до Narrator, а `PresenceService` применяет placement. После PREPARED Narrator видит уже материализованный мир.

Портрет является derived artifact: он строится из durable Character description/appearance, но никогда не является источником канона.

## Persisted evidence

- Character entity + canonical name/status;
- aliases/temporary identity metadata;
- description, appearance и дополнительные profile fields;
- `current_location_id` и scene participation;
- source turn/materialization metadata;
- `TurnAuthority.allowed_new_npcs` / existing arrivals;
- generated portrait path/MediaAsset как производный артефакт.

## Failure semantics

- unreadable/placeholder-only identity → fail closed до materialization;
- конфликт с существующей identity → reconcile/promote, а не тихое дублирование;
- ошибка portrait generation не откатывает Character;
- попытка вернуть dead/destroyed через обычный turn отвергается presence/materialization invariant;
- failure после PREPARED до publication компенсирует новые materialized entities.

## Проверка

Debugger показывает auto-registered NPC, location/presence, source turn и identity flags. Local model contracts включают `new_npc_direct_contact`, `npc_temporary_to_stable_identity`, `dead_character_mention`, `undo_npc_creation`.

Deterministic tests проверяют resolver/materializer/presence независимо от качества модели.

## Историческая граница

Синтетические canonical names, постфактум-регистрация из Narrator prose и автоматическое следование NPC за активной Scene считаются superseded.