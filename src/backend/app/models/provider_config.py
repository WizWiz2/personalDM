from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field

class ProviderConfigCreate(BaseModel):
    base_url: str = Field(..., examples=["http://localhost:11434/v1"])
    model_name: str = Field(..., examples=["gemma2:27b"])
    api_key: str | None = None
    context_window: int = 8192

class ProviderConfigRead(BaseModel):
    id: UUID
    campaign_id: UUID
    base_url: str
    model_name: str
    has_api_key: bool
    context_window: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

class ProviderConfigUpdate(BaseModel):
    base_url: str | None = None
    model_name: str | None = None
    api_key: str | None = None
    context_window: int | None = None
