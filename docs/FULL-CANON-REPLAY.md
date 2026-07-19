# Полный replay канона

Этот этап закрывает stateful-дыру rebuild, оставшуюся после Playable Campaign Debugger.

## Initial world state

Перед первым `movement` или `item_transfer` сохраняется неизменяемый baseline:

- исходная локация каждого существующего персонажа;
- исходный владелец или локация каждого существующего предмета;
- schema version и SHA-256 snapshot.

Новые персонажи и предметы, появившиеся позже, добавляются в baseline перед их первым stateful изменением, не перезаписывая уже сохранённые начальные значения.

## Rebuild

Rebuild выполняет:

1. backup SQLite;
2. восстановление initial world state;
3. удаление производного канона;
4. replay всех accepted/edited proposals по порядку ходов;
5. сравнение stateful fingerprint с состоянием до rebuild;
6. rollback при несовпадении.

`movement` и `item_transfer` больше не пропускаются.

## Export

JSON export использует формат `personal-dm-campaign` версии 2 и включает initial world state вместе с его hash.
