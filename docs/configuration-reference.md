# Configuration reference

**Статус:** current implementation reference  
**Источник истины:** `src/backend/app/config.py` + runtime provider management.

Все `Settings` читаются с prefix `PDM_`; `.env` является convenience storage, но campaign-specific provider config может переопределять runtime selection.

## Storage

- `PDM_DATA_DIR` — library root. Windows default: `%APPDATA%/PersonalDM/library`; Linux default: `$XDG_DATA_HOME/PersonalDM` или `~/.local/share/PersonalDM`.
- `PDM_DATABASE_URL` — SQLAlchemy URL, default SQLite `campaign.db` внутри data dir.
- `PDM_SECRET_ENCRYPTION_KEY` — optional Fernet key; при отсутствии используется machine-specific derivation.

## Primary text provider

- `PDM_TEXT_PROVIDER`: `local` / cloud mode selected by provider management.
- `PDM_LLM_BASE_URL`, default `http://localhost:11434/v1`.
- `PDM_LLM_MODEL`, default `gemma4:e4b`.
- `PDM_LLM_API_KEY`.
- `PDM_LLM_CONTEXT_WINDOW`, default `4096`.

Для cloud OpenAI-compatible endpoint campaign model name остаётся model id для control roles, если отдельный supported role override не задан. Локальный runtime может использовать narrator/control split.

## Control/model roles

- `PDM_CONTROL_LLM_BASE_URL`;
- `PDM_CONTROL_LLM_MODEL`, default `qwen2.5:7b`;
- `PDM_CONTROL_LLM_API_KEY`;
- `PDM_CONTROL_LLM_CONTEXT_WINDOW`;
- `PDM_CONTROL_LLM_TIMEOUT_SECONDS`, default `60`;
- role model overrides: `PDM_PLANNER_LLM_MODEL`, `PDM_SCRIBE_LLM_MODEL`, `PDM_CURATOR_LLM_MODEL`, `PDM_EVALUATOR_LLM_MODEL`, `PDM_PLAYER_LLM_MODEL`, `PDM_SCENARIO_BUILDER_LLM_MODEL`, `PDM_CHARACTER_BUILDER_LLM_MODEL`, `PDM_NARRATION_VALIDATOR_LLM_MODEL`.

Role routing source/actual model должен проверяться по persisted provider telemetry, а не только по текущему `.env`.

## Prompt/generation budgets

Narrator:

- `PDM_NARRATOR_HISTORY_LIMIT=12`;
- `PDM_NARRATOR_STAGNATION_TURNS=2`;
- `PDM_NARRATOR_RECEIPT_MAX_ITEMS=6`;
- `PDM_NARRATOR_TEMPERATURE=0.55`;
- `PDM_NARRATOR_RETRY_TEMPERATURE=0.3`.

Planner/validation:

- `PDM_PLANNER_TEMPERATURE=0.15`;
- `PDM_PLANNER_MAX_TOKENS=900`;
- `PDM_PLANNER_CONTEXT_RESERVE_TOKENS=700`;
- `PDM_NARRATION_VALIDATOR_TEMPERATURE=0.0`;
- `PDM_NARRATION_VALIDATOR_MAX_TOKENS=1100`;
- `PDM_NARRATION_REPAIR_TEMPERATURE=0.25`;
- `PDM_NARRATION_REPAIR_ATTEMPTS=2`;
- `PDM_NARRATION_VALIDATOR_FAIL_OPEN=true`;
- `PDM_RESPONSE_RESERVE_TOKENS=1536`;
- `PDM_CONTROL_RESPONSE_RESERVE_TOKENS=1600`;
- `PDM_SAFETY_MARGIN_PERCENT=0.05`.

Transient scene texture:

- `PDM_NARRATIVE_DETAIL_TURN_WINDOW=3`;
- `PDM_NARRATIVE_DETAIL_MAX_ITEMS=8`.

Maintenance/simulation:

- `PDM_CURATOR_INTERVAL_TURNS=3`;
- `PDM_SIM_EVALUATOR_INTERVAL_TURNS=2`;
- `PDM_SIM_PLAYER_MODE=llm`.

## Images

- `PDM_IMAGE_PROVIDER`: `local`, `cloud`, `off`;
- `PDM_IMAGE_ENABLED` — backward-compatible gate;
- `PDM_IMAGE_BASE_URL`, default ComfyUI `http://127.0.0.1:8188`;
- `PDM_IMAGE_CLOUD_BASE_URL`, `PDM_IMAGE_CLOUD_MODEL`, `PDM_IMAGE_API_KEY`;
- `PDM_IMAGE_GENERATED_SUBDIR`;
- `PDM_IMAGE_DIFFUSION_MODEL`, `PDM_IMAGE_TEXT_ENCODER`, `PDM_IMAGE_VAE_MODEL`, `PDM_IMAGE_LORA_MODEL`;
- `PDM_IMAGE_STEPS`, `PDM_IMAGE_MAX_REFERENCES`, `PDM_IMAGE_SCENE_HISTORY_TURNS`, `PDM_IMAGE_TIMEOUT_SECONDS`;
- `PDM_IMAGE_RELEASE_OLLAMA_VRAM`;
- portrait/scene/cover LoRA strength variables.

## Diagnostics/build

- `PDM_CRASH_LOG` — path for faulthandler/unhandled exception log.
- `PDM_BUILD_COMMIT` — optional build SHA exposed by `/api/debugger/runtime`; GitHub Actions may supply `GITHUB_SHA`.

## Failure semantics

Missing/invalid provider config must fail at provider/role boundary, never silently change world truth. API keys are never exposed by profile/debugger responses. Visual settings do not affect canonical state.

## Verification

`GET /api/debugger/runtime` shows actual installed runtime fingerprint. Provider settings/status APIs show effective provider configuration without secrets. Config defaults are regression-tested; model behavior itself is covered by local live-model contracts.