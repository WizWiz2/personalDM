from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class FactCreate(BaseModel):
    subject: str
    predicate: str
    object_value: str | None = None
    truth_status: str = "true"
    confidence: float = 1.0
    visibility: str = "dm"
    source_turn_id: UUID | None = None


class FactRead(BaseModel):
    id: UUID
    campaign_id: UUID
    subject: str
    predicate: str
    object_value: str | None
    truth_status: str
    source_turn_id: UUID | None
    confidence: float
    visibility: str
    is_current: bool
    superseded_by: UUID | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class FactUpdate(BaseModel):
    object_value: str | None = None
    truth_status: str | None = None
    confidence: float | None = None
    visibility: str | None = None
    is_current: bool | None = None
    superseded_by: UUID | None = None
