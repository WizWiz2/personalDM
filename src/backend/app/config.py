import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_data_dir() -> str:
    """Use the normal per-user data location, while keeping dev/test overrides."""
    explicit = os.getenv("PDM_DATA_DIR")
    if explicit:
        return explicit
    if os.name == "nt":
        root = os.getenv("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return str(Path(root) / "PersonalDM" / "library")
    root = os.getenv("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return str(Path(root) / "PersonalDM" / "library")


def _default_database_url() -> str:
    return f"sqlite+aiosqlite:///{Path(_default_data_dir()) / 'campaign.db'}"


class Settings(BaseSettings):
    DATABASE_URL: str = Field(default_factory=_default_database_url)
    DATA_DIR: str = Field(default_factory=_default_data_dir)

    # LLM settings (OpenAI-compatible / native Ollama)
    TEXT_PROVIDER: str = "local"
    LLM_BASE_URL: str = "http://localhost:11434/v1"
    LLM_MODEL: str = "gemma4:e4b"  # Gemma 4 (4B effective parameters)
    LLM_API_KEY: str | None = None
    LLM_CONTEXT_WINDOW: int = 4096

    # Visual generation can be local (ComfyUI), cloud (OpenAI-compatible images API)
    # or off. IMAGE_ENABLED is retained as a backward-compatible runtime gate.
    IMAGE_PROVIDER: str = "local"
    IMAGE_ENABLED: bool = False
    IMAGE_BASE_URL: str = "http://127.0.0.1:8188"
    IMAGE_CLOUD_BASE_URL: str = "https://api.openai.com/v1"
    IMAGE_CLOUD_MODEL: str = "gpt-image-2"
    IMAGE_API_KEY: str | None = None
    IMAGE_GENERATED_SUBDIR: str = "generated"
    IMAGE_DIFFUSION_MODEL: str = "flux-2-klein-4b-fp8.safetensors"
    IMAGE_TEXT_ENCODER: str = "qwen_3_4b_fp4_flux2.safetensors"
    IMAGE_VAE_MODEL: str = "flux2-vae.safetensors"
    IMAGE_LORA_MODEL: str = "pixel-art-lora.safetensors"
    IMAGE_STEPS: int = 4
    IMAGE_MAX_REFERENCES: int = 6
    IMAGE_SCENE_HISTORY_TURNS: int = 8
    IMAGE_TIMEOUT_SECONDS: float = 240.0
    IMAGE_RELEASE_OLLAMA_VRAM: bool = True
    IMAGE_PORTRAIT_LORA_STRENGTH: float = 1.05
    IMAGE_SCENE_LORA_STRENGTH: float = 0.9
    IMAGE_COVER_LORA_STRENGTH: float = 0.9

    # Role-based local model routing. Narration keeps the campaign provider;
    # structured control defaults to the smaller, stricter Qwen model.
    CONTROL_LLM_BASE_URL: str | None = None
    CONTROL_LLM_MODEL: str = "qwen2.5:7b"
    CONTROL_LLM_API_KEY: str | None = None
    CONTROL_LLM_CONTEXT_WINDOW: int | None = None
    CONTROL_REQUEST_DEADLINE_SECONDS: float = 120.0
    PLANNER_LLM_MODEL: str | None = None
    SCRIBE_LLM_MODEL: str | None = None
    CURATOR_LLM_MODEL: str | None = None
    EVALUATOR_LLM_MODEL: str | None = None
    PLAYER_LLM_MODEL: str | None = None
    SCENARIO_BUILDER_LLM_MODEL: str | None = None
    CHARACTER_BUILDER_LLM_MODEL: str | None = None
    NARRATION_VALIDATOR_LLM_MODEL: str | None = None

    # Expensive maintenance agents do not need to run after every narrative turn.
    CURATOR_INTERVAL_TURNS: int = 3
    SIM_EVALUATOR_INTERVAL_TURNS: int = 2
    # Real benchmarks use an actor-scoped LLM player. Deterministic mode remains an
    # explicit fixture option for fast CI and focused tests only.
    SIM_PLAYER_MODE: str = "llm"

    # Keep small narrators focused on current scene state instead of long prose tails.
    NARRATOR_HISTORY_LIMIT: int = 12
    NARRATOR_STAGNATION_TURNS: int = 2
    NARRATOR_RECEIPT_MAX_ITEMS: int = 6
    NARRATOR_TEMPERATURE: float = 0.55
    NARRATOR_RETRY_TEMPERATURE: float = 0.3

    # Transient narrative texture is remembered briefly inside the current scene only.
    NARRATIVE_DETAIL_TURN_WINDOW: int = 3
    NARRATIVE_DETAIL_MAX_ITEMS: int = 8

    # The planner is a compact structured control call before prose generation.
    PLANNER_TEMPERATURE: float = 0.15
    PLANNER_MAX_TOKENS: int = 900
    PLANNER_CONTEXT_RESERVE_TOKENS: int = 700

    # Narration is buffered until a control-model validation pass accepts it.
    NARRATION_VALIDATOR_TEMPERATURE: float = 0.0
    NARRATION_VALIDATOR_MAX_TOKENS: int = 1100
    NARRATION_REPAIR_TEMPERATURE: float = 0.25
    NARRATION_REPAIR_ATTEMPTS: int = 2
    NARRATION_VALIDATOR_FAIL_OPEN: bool = True

    # Narrative and structured control calls have different completion needs.
    RESPONSE_RESERVE_TOKENS: int = 1536
    CONTROL_RESPONSE_RESERVE_TOKENS: int = 1600
    SAFETY_MARGIN_PERCENT: float = 0.05

    # Secrets Encryption Key (32-byte url-safe base64 key for cryptography.fernet).
    # If not provided, a machine-specific key will be derived.
    SECRET_ENCRYPTION_KEY: str | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="PDM_",
        extra="ignore",
    )


settings = Settings()
