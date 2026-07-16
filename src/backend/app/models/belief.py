from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, ConfigDict

class BeliefCreate(BaseModel):
    character_id: UUID
    fact_id: UUID | None = None
    proposition: str
    status: str = "believed"  # 'known', 'believed', 'suspected', 'doubted', 'rejected', 'fabricated'
    confidence: float = 1.0
    source_turn_id: int | None = None
    source_character_id: UUID | None = None
    visibility: str = "dm"  # 'dm', 'public', 'character_only'

class BeliefRead(BaseModel):
    id: UUID
    character_id: UUID
    fact_id: UUID | None
    proposition: str
    status: str
    confidence: float
    source_turn_id: int | None
    source_character_id: UUID | None
    visibility: str
    is_current: bool
    superseded_by: UUID | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

class BeliefUpdate(BaseModel):
    proposition: str | None = None
    status: str | None = None
    confidence: float | None = None
    visibility: str | None = None
    is_current: bool | None = None
    superseded_by: UUID | None = None
