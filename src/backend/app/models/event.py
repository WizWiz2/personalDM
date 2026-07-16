from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, ConfigDict

class EventCreate(BaseModel):
    event_type: str
    description: str
    world_time: str | None = None
    location_id: UUID | None = None
    importance: str = "normal"  # 'trivial', 'normal', 'important', 'critical'
    participant_ids: list[UUID] = []

class EventRead(BaseModel):
    id: UUID
    campaign_id: UUID
    event_type: str
    description: str
    world_time: str | None
    location_id: UUID | None
    importance: str
    source_turns: list[UUID] = []  # Decoded from JSON array
    participant_ids: list[UUID] = []  # Queried from event_participants
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
