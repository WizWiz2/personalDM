from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class GoalCreate(BaseModel):
    description: str
    priority: int = 0
    is_secret: bool = False
    valid_until: datetime | None = None


class GoalRead(BaseModel):
    id: UUID
    character_id: UUID
    description: str
    priority: int
    status: str
    is_secret: bool
    source_turn_id: UUID | None
    valid_until: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class GoalUpdate(BaseModel):
    description: str | None = None
    priority: int | None = None
    status: str | None = None
    is_secret: bool | None = None
    valid_until: datetime | None = None
