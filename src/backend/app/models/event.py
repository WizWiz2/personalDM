from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EventCreate(BaseModel):
    event_type: str
    description: str
    world_time: str | None = None
    location_id: UUID | None = None
    importance: str = "normal"
    participant_ids: list[UUID] = Field(default_factory=list)
    source_turns: list[UUID] = Field(default_factory=list)


class EventRead(BaseModel):
    id: UUID
    campaign_id: UUID
    event_type: str
    description: str
    world_time: str | None
    location_id: UUID | None
    importance: str
    source_turns: list[UUID] = Field(default_factory=list)
    participant_ids: list[UUID] = Field(default_factory=list)
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
