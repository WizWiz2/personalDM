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
- backup базы, archive v2, import и полный replay принятых canon deltas.

## Replay канона

Initial world snapshot фиксирует исходные локации персонажей и положение предметов. Rebuild восстанавливает baseline и воспроизводит facts, beliefs, relationships, events, movement и item transfer. Подробный контракт описан в `CANON-REPLAY-ROUNDTRIP.md`.

## Проверка

Новый workflow выполняет compile, полный Alembic cycle, milestone tests, весь backend pytest и targeted Ruff для новых файлов.
