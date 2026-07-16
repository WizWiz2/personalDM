from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict

class ChatMessage(BaseModel):
    role: str  # 'user', 'assistant', 'system'
    content: str

class TurnCreate(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str
    scene_id: UUID | None = None
    acting_character_id: UUID | None = None
    parent_turn_id: UUID | None = None
    model_name: str | None = None
    context_snapshot: dict | None = None  # Will be stored as JSON string in DB
    token_count: int | None = None

class TurnRead(BaseModel):
    id: UUID
    campaign_id: UUID
    scene_id: UUID | None
    role: str
    content: str
    parent_turn_id: UUID | None
    status: str  # 'active', 'alternative', 'undone'
    model_name: str | None
    token_count: int | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
