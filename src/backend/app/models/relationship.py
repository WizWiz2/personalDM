from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RelationshipCreate(BaseModel):
    subject_id: UUID
    object_id: UUID
    relation_type: str = Field(
        ...,
        description="e.g. trust, fear, rivalry, debt, etc.",
    )
    description: str
    reason: str | None = None
    intensity: float | None = Field(default=None, ge=-1.0, le=1.0)
    source_turn_id: UUID | None = None
    provenance: str = "manual"
    visibility: str = "dm"


class RelationshipRead(BaseModel):
    id: UUID
    campaign_id: UUID
    subject_id: UUID
    object_id: UUID
    relation_type: str
    description: str
    reason: str | None
    intensity: float | None
    source_turn_id: UUID | None
    provenance: str
    confidence: float
    valid_from: datetime
    valid_until: datetime | None
    is_current: bool
    visibility: str
    superseded_by: UUID | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class RelationshipUpdate(BaseModel):
    description: str | None = None
    reason: str | None = None
    intensity: float | None = Field(default=None, ge=-1.0, le=1.0)
    visibility: str | None = None
    is_current: bool | None = None
    superseded_by: UUID | None = None
