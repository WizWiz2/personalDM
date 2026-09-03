# Visual generation

**Статус:** current implementation contract  
**Владельцы:** `VisualGenerationService`, provider factory/dispatcher; API — `app/api/visuals.py`.

## Нормативный контракт

Изображения — derived presentation artifacts. Они могут визуализировать уже сохранённый Character/Scene/Campaign, но не создают и не изменяют truth state.

Поддерживаются provider modes `local`, `cloud`, `off`. Local использует ComfyUI; cloud — OpenAI-compatible images provider. Ошибка визуального backend не должна ломать игровой ход, Session Zero или durable NPC.

Инварианты:

- portrait строится из сохранённого Character profile/appearance;
- campaign cover строится из finalized campaign state;
- scene image строится из structured Scene/campaign context;
- generated image/seed/prompt не является доказательством факта мира;
- portrait generation нового NPC запускается после durable materialization, а не вместо неё;
- изображения могут быть повторно сгенерированы (`force`) без изменения canonical entity;
- user library остаётся playable при `IMAGE_PROVIDER=off` или недоступном ComfyUI;
- local pipeline должен учитывать ограниченный VRAM и может освобождать Ollama VRAM перед diffusion generation.

## Текущая topology

Local defaults ориентированы на ComfyUI `http://127.0.0.1:8188`. Config задаёт diffusion model, text encoder, VAE, pixel-art LoRA, steps, reference limit и отдельные LoRA strength для portrait/scene/cover.

Артефакты сохраняются под `PDM_DATA_DIR/<generated subdir>` и отдаются через public generated URL. При force-generation успешный результат архивируется в `gallery`; `MediaAsset` хранит metadata/prompt/seed и относительный путь.

Основные endpoints:

- `GET /api/visuals/status`;
- `GET/POST /api/characters/{character_id}/visuals/portrait`;
- `GET/POST /api/campaigns/{campaign_id}/visuals/cover`;
- `GET /api/campaigns/{campaign_id}/visuals/gallery`;
- `GET /api/campaigns/{campaign_id}/scenes/{scene_id}/visuals/latest`;
- `POST /api/campaigns/{campaign_id}/scenes/{scene_id}/visuals`.

## Persisted evidence

Canonical evidence остаётся в Character/Location/Scene/Campaign. Visual evidence — `MediaAsset`, file path, prompt, seed, asset type, optional scene id и metadata. Удаление generated file не должно менять world truth.

## Failure semantics

- ComfyUI/provider unavailable → API 503 для явного generation request;
- unknown Character/Scene/Campaign → 404;
- background visual generation failure не откатывает committed game state;
- missing file в gallery пропускается как отсутствующий derived asset;
- `off` означает intentional no-generation, не corruption.

## Local hardware boundary

Текущие defaults выбраны для локального режима порядка 8 GB VRAM: короткий low-step workflow и опциональное освобождение Ollama VRAM. Это implementation constraint, а не продуктовый invariant: замена visual model не должна затрагивать truth engine.

## Проверка

Visual tests проверяют provider routing, prompt/reference construction, file/archive semantics и отсутствие влияния на canonical state. Live model truth-contract suite намеренно запускается с image generation выключенной: он тестирует LLM/truth transitions, а не GPU diffusion throughput.