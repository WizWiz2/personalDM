import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    DATABASE_URL: str = "sqlite+aiosqlite:///./data/campaign.db"
    DATA_DIR: str = "./data"

    # LLM settings (OpenAI-compatible / native Ollama)
    LLM_BASE_URL: str = "http://localhost:11434/v1"
    LLM_MODEL: str = "gemma4:e4b"  # Gemma 4 (4B effective parameters)
    LLM_API_KEY: str | None = None
    LLM_CONTEXT_WINDOW: int = 4096

    # Role-based local model routing. Narration keeps the campaign provider;
    # structured control defaults to the smaller, stricter Qwen model.
    CONTROL_LLM_BASE_URL: str | None = None
    CONTROL_LLM_MODEL: str = "qwen2.5:7b"
    CONTROL_LLM_API_KEY: str | None = None
    CONTROL_LLM_CONTEXT_WINDOW: int | None = None
    SCRIBE_LLM_MODEL: str | None = None
    CURATOR_LLM_MODEL: str | None = None
    EVALUATOR_LLM_MODEL: str | None = None
    CHARACTER_BUILDER_LLM_MODEL: str | None = None

    # Expensive maintenance agents do not need to run after every narrative turn.
    CURATOR_INTERVAL_TURNS: int = 3
    SIM_EVALUATOR_INTERVAL_TURNS: int = 2
    SIM_PLAYER_MODE: str = "deterministic"

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
