from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.character_card import CharacterCardRead
from app.models.scene import SceneRead


class SessionZeroUpdate(BaseModel):
    setting_name: str | None = None
    genre: str | None = None
    premise: str | None = None
    tone: str | None = None
    themes: list[str] | None = None
    boundaries: list[str] | None = None
    boundaries_confirmed: bool | None = None
    rules_system: str | None = None
    world_summary: str | None = None
    starting_situation: str | None = None
    starting_location_id: UUID | None = None
    starting_scene_title: str | None = None
    play_style: str | None = None
    content_rating: str | None = None
    custom_fields: dict | None = None
    player_character_id: UUID | None = None
    narrative_style: str | None = None


class SessionZeroRead(BaseModel):
    campaign_id: UUID
    status: str
    setting_name: str | None = None
    genre: str | None = None
    premise: str | None = None
    tone: str | None = None
    themes: list[str] = Field(default_factory=list)
    boundaries: list[str] = Field(default_factory=list)
    boundaries_confirmed: bool = False
    rules_system: str | None = None
    world_summary: str | None = None
    starting_situation: str | None = None
    starting_location_id: UUID | None = None
    starting_location_name: str | None = None
    starting_scene_title: str | None = None
    play_style: str | None = None
    content_rating: str | None = None
    custom_fields: dict = Field(default_factory=dict)
    player_character_id: UUID | None = None
    player_character_name: str | None = None
    current_scene_id: UUID | None = None
    character_card_missing_fields: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    ready_to_complete: bool = False
    legacy_imported: bool = False
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class SessionZeroCompleteRequest(BaseModel):
    player_character_id: UUID | None = None
    starting_scene_title: str | None = None


class SessionZeroCompletionRead(BaseModel):
    setup: SessionZeroRead
    scene: SceneRead
    character_card: CharacterCardRead
