# Playable Campaign Debugger milestone

Этот этап превращает внутренние механизмы памяти в наблюдаемый и восстанавливаемый игровой цикл.

## Включено

- явный `player_character_id` у кампании;
- persistent generation runs и отмена через SQLite;
- durable post-turn jobs для Thesis Curator и Memory Scribe;
- фоновое восстановление зависших jobs после перезапуска;
- Campaign Debugger API и локальная HTML-панель;
- просмотр provenance, evidence, версий канона и ошибок обработки;
- ручной retry post-turn jobs;
- backup базы и JSON archive v2 без provider secret;
- checkpoint начального состояния персонажей и предметов;
- полный replay facts, beliefs, relationships, events, movement и item transfer;
- сравнение семантической проекции до и после rebuild;
- автоматический rollback при любом расхождении.

## Старые кампании

Для кампании, созданной до появления checkpoint, первое сохранение состояния становится безопасной baseline-точкой. В snapshot записывается список уже учтённых accepted proposals. Они повторяются при rebuild для восстановления provenance events, но итоговое состояние обязано совпасть с baseline и последующими дельтами.

## Archive v2

Экспорт содержит debugger snapshot, initial world state, текущую каноническую проекцию и SHA-256 хеши. Это позволяет проверить целостность архива и доказать, что rebuild вернул тот же наблюдаемый канон.

## Проверка

Workflow выполняет compile, полный Alembic cycle, отдельные round-trip tests, весь backend pytest и targeted Ruff для milestone-файлов.
