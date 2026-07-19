# Полный replay канона и переносимый архив

Этот слой делает принятые proposals воспроизводимым журналом изменений, а JSON-архив кампании проверяемым переносимым снимком.

## Initial world snapshot

Перед первой принятой дельтой `movement` или `item_transfer` backend атомарно сохраняет исходное изменяемое состояние мира:

- текущие локации персонажей;
- владельцев и локации предметов;
- версию схемы и SHA-256 digest.

Персонаж или предмет, созданный позднее, добавляется в baseline непосредственно перед своей первой stateful-дельтой. Заменить baseline после принятия stateful canon нельзя. Ручное изменение локации или владельца после появления baseline требует `source_turn_id` и сохраняется как принятый proposal.

## Rebuild

`POST /api/campaigns/{campaign_id}/canon/rebuild?apply=false` выполняет dry-run. С `apply=true` система:

1. создаёт SQLite backup;
2. восстанавливает initial world snapshot;
3. удаляет только производные записи и события;
4. сохраняет ручные записи и события без source turns;
5. воспроизводит accepted/edited proposals в хронологическом порядке;
6. переназначает ссылки beliefs на заново созданные facts;
7. сравнивает semantic digest до и после replay.

`semantic_match_before=true` означает, что актуальное состояние мира и канона воспроизвелось без смысловых расхождений.

## Archive v2

`GET /api/campaigns/{campaign_id}/export` создаёт JSON archive v2. Архив:

- сохраняет UUID, timestamps, raw turns, proposals и derived state;
- включает initial world snapshot;
- не содержит зашифрованный API key;
- имеет digest таблиц и semantic state digest.

`POST /api/campaigns/import` импортирует архив в пустое место. `replace=true` разрешает явную замену существующей кампании и предварительно создаёт backup. Импорт выполняется транзакционно и откатывается целиком при неверном digest или несовпадении semantic state.

## Инварианты

- `movement` и `item_transfer` больше не пропускаются при rebuild;
- архив с изменёнными таблицами, state digest или provider secret отклоняется;
- ручные события без source turns не удаляются replay-процессом;
- export → delete → import сохраняет semantic и archive digest;
- исходный API key после import необходимо настроить заново.
