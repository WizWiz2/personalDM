# Runtime provider management

## Goal

PersonalDM must let the user choose, inspect, repair and switch text and image model providers without manually editing `.env` or reinstalling the whole application.

Text and image providers are independent.

Supported combinations include:

- local text + local images;
- local text + cloud images;
- cloud text + local images;
- cloud text + cloud images;
- local/cloud text + images disabled.

## Provider modes

### Text

- `local`: Ollama + a configured local model (default `gemma4:e4b`).
- `cloud`: OpenAI-compatible chat/completions provider with independent base URL, model, API key and context window.

Text generation cannot be disabled because it is required for gameplay.

### Images

- `local`: ComfyUI + FLUX.2 Klein 4B + the PersonalDM pixel-art assets.
- `cloud`: OpenAI-compatible Images API; first supported implementation uses `gpt-image-2` by default.
- `off`: no new image generation. Existing generated files and gallery entries remain readable.

The image API key is independent from the text API key.

## Configuration ownership

Durable machine-level provider selection is stored in `src/backend/.env` using `PDM_` settings. Secrets remain outside Git.

The active campaign text provider is synchronized when the user changes text provider from campaign settings, preserving the existing per-campaign provider contract.

Important settings:

- `PDM_TEXT_PROVIDER=local|cloud`
- `PDM_LLM_BASE_URL`
- `PDM_LLM_MODEL`
- `PDM_LLM_API_KEY`
- `PDM_LLM_CONTEXT_WINDOW`
- `PDM_IMAGE_PROVIDER=local|cloud|off`
- `PDM_IMAGE_ENABLED=true|false` (backward-compatible runtime gate)
- `PDM_IMAGE_BASE_URL` for local ComfyUI
- `PDM_IMAGE_CLOUD_BASE_URL`
- `PDM_IMAGE_CLOUD_MODEL`
- `PDM_IMAGE_API_KEY`

## Shared runtime service

`RuntimeProviderService` is the single implementation used by:

- first-run / `play.bat` bootstrap;
- GUI settings;
- CLI settings;
- health checks;
- local installation/repair.

No second Ollama or ComfyUI installer implementation should live in `play.bat`.

## Checker contract

Each provider reports:

- `ready`: whether it can be used now;
- `code`: stable state code;
- `message`: user-facing reason/status;
- `installable`: whether PersonalDM can repair/install it locally.

Expected local text states include:

- `not_installed` — Ollama missing;
- `service_offline` — Ollama present but not reachable;
- `model_missing` — configured model missing;
- `ready`.

Expected local image states include:

- `not_installed` — ComfyUI source missing;
- `service_offline` — source/models present but server not reachable;
- `model_missing` — one or more required model files missing;
- `ready`.

Cloud providers report missing configuration, connectivity/auth failures or ready status. Image `off` reports a successful `disabled` state.

## Installer / repair contract

Selecting a local provider triggers a checker first. Installation/repair runs only when the provider is not ready.

Local text repair may:

- install Ollama on supported Windows systems;
- start the Ollama service;
- pull the configured model.

Local image repair may:

- clone/download ComfyUI;
- create the isolated ComfyUI Python environment;
- install the CUDA PyTorch runtime and ComfyUI requirements;
- download required FLUX/text-encoder/VAE/LoRA files;
- start ComfyUI in low-VRAM mode.

GUI installation uses an asynchronous backend job and polling so the HTTP request itself is not held open for multi-minute model downloads.

## First-run behavior

If `PDM_TEXT_PROVIDER` is absent, first run asks for text mode and required cloud settings when applicable.

If `PDM_IMAGE_PROVIDER` is absent, first run asks for local/cloud/off and required cloud settings when applicable.

On later launches `play.bat` does not ask again. It runs a cheap checker. Selected local providers are started/repaired only when not ready.

Cloud/off modes must not install ComfyUI or image model files.

## GUI behavior

Campaign settings expose a single provider section instead of a separate legacy LLM form.

For text and images the UI must show:

- selected mode;
- base URL/model fields when relevant;
- masked API-key state;
- current checker message;
- save action;
- install/repair action for an unready local provider;
- global `Check all` action.

Changing text provider also updates the active campaign provider configuration.

Changing image provider affects subsequent cover/portrait/scene generation immediately. Existing gallery assets remain available in every mode.

## CLI behavior

Campaign menu contains `Models and providers` / `Модели и провайдеры`.

The CLI supports the same text modes, image modes, status checks and local repair operations as GUI.

## Visual provider abstraction

The narrative/visual orchestration layer must not choose ComfyUI directly. It asks a provider factory for the configured visual generation service.

Local mode uses ComfyUI. Cloud mode uses the Images API adapter. Off mode is gated before generation.

Campaign cover, portrait, scene and gallery behavior remain provider-independent.

## Acceptance criteria

1. Fresh install asks for text provider and image provider independently.
2. Selecting image `off` downloads/starts no image infrastructure.
3. Existing `.env` installations remain usable; legacy `PDM_IMAGE_ENABLED=true` is interpreted as local images until an explicit mode is saved.
4. `play.bat` contains no duplicate ComfyUI/Ollama installation logic.
5. GUI can switch/check/install both provider families.
6. CLI can switch/check/install both provider families.
7. Local checker differentiates missing installation, missing model and offline service.
8. Cloud text and cloud image keys are separate.
9. Cloud image generation reuses the existing cover/portrait/scene/gallery pipeline.
10. Image `off` disables new generation without deleting existing gallery assets.
11. Provider changes are persisted and survive restart.
12. Backend/frontend tests and existing campaign integrity suites remain green.
