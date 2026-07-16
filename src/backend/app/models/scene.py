from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, ConfigDict

class SceneCreate(BaseModel):
    title: str
    location_description: str | None = None
    mood: str | None = None
    tension: str | None = None

class SceneRead(BaseModel):
    id: UUID
    campaign_id: UUID
    title: str
    location_description: str | None
    mood: str | None
    tension: str | None
    status: str  # 'active', 'completed', 'abandoned'
    participants: list[UUID] = []
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

class SceneUpdate(BaseModel):
    title: str | None = None
    location_description: str | None = None
    mood: str | None = None
    tension: str | None = None
    status: str | None = None
