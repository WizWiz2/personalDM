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

## Контракт имени (identity slot)

`name_identity_contract` — единый источник предикатов для intro sanitize, registrar и
ремонта уже сохранённых сущностей:

- `canonical_name` — короткое личное имя **или** короткое ролевое обозначение;
- длинное description/blurb, comma-heavy designation и duty-clause shape не занимают
  identity slot;
- при ремонте blurb остаётся в `description`/`role`; short role token допустим;
- без короткого designation — статус `needs_name` в `custom_fields` + fail-soft human display,
  без выдумывания личного имени и без сырого токена `needs_name` в identity slot;
- **уникальность canonical_name:** short designation, выставляемый repair/intro/registrar,
  не должен совпадать с `canonical_name` другой live-сущности в кампании; при коллизии —
  статус `needs_name` и человекочитаемый уникальный provisional label (роль → leading clause →
  `Role N`), а не twin `Служанка`/`Служанка`, не выдуманное имя и не machine token в
  `canonical_name` / `participant_names`.

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