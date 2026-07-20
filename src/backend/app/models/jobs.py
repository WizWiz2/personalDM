from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class GenerationRunRead(BaseModel):
    id: UUID
    campaign_id: UUID
    user_turn_id: UUID
    assistant_turn_id: UUID | None
    status: str
    cancel_requested: bool
    error: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PostTurnJobRead(BaseModel):
    id: UUID
    campaign_id: UUID
    assistant_turn_id: UUID
    job_type: str
    status: str
    attempts: int
    error: str | None
    locked_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
