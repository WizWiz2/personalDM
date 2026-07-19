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
- backup базы, JSON export и воспроизводимый rebuild поддерживаемых canon deltas.

## Ограничения rebuild

Rebuild безопасно воспроизводит facts, beliefs, relationships и обычные events из принятых proposals. Stateful deltas движения и владения предметами пока только диагностируются и пропускаются, потому что для их полного восстановления нужен отдельный начальный snapshot состояния мира.

## Проверка

Новый workflow выполняет compile, полный Alembic cycle, milestone tests, весь backend pytest и targeted Ruff для новых файлов.
