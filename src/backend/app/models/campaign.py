from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class CampaignCreate(BaseModel):
    name: str
    description: str | None = None
    system_instructions: str | None = None
    narrative_style: str | None = None
    player_character_id: UUID | None = None


class CampaignRead(BaseModel):
    id: UUID
    name: str
    description: str | None
    system_instructions: str | None
    narrative_style: str | None
    current_scene_id: UUID | None
    player_character_id: UUID | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CampaignUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    system_instructions: str | None = None
    narrative_style: str | None = None
    current_scene_id: UUID | None = None
    player_character_id: UUID | None = None
