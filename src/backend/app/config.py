import os
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    DATABASE_URL: str = "sqlite+aiosqlite:///./data/campaign.db"
    DATA_DIR: str = "./data"
    
    # LLM Settings (OpenAI-compatible / Ollama)
    LLM_BASE_URL: str = "http://localhost:11434/v1"
    LLM_MODEL: str = "gemma4:e4b"  # Gemma 4 (4B effective parameters)
    LLM_API_KEY: str | None = None
    LLM_CONTEXT_WINDOW: int = 4096
    RESPONSE_RESERVE_TOKENS: int = 1024
    SAFETY_MARGIN_PERCENT: float = 0.05
    
    # Secrets Encryption Key (32-byte url-safe base64 key for cryptography.fernet)
    # If not provided, a machine-specific key will be derived.
    SECRET_ENCRYPTION_KEY: str | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="PDM_",
        extra="ignore"
    )

settings = Settings()
