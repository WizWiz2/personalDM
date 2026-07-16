from datetime import datetime
from enum import Enum
from uuid import UUID
from pydantic import BaseModel, ConfigDict

class ThesisType(str, Enum):
    CANON = "canon"
    INTENTION = "intention"
    RELATIONSHIP_DYNAMIC = "relationship_dynamic"
    SECRET = "secret"
    TENSION = "tension"
    UNRESOLVED_BEAT = "unresolved_beat"
    VISUAL_STATE = "visual_state"
    MUSIC_MOOD = "music_mood"

class SceneThesisCreate(BaseModel):
    thesis_type: ThesisType
    text: str
    priority: int = 0
    visibility: str = "dm"  # 'dm', 'public', 'character_only'
    pinned: bool = False
    related_entity_ids: list[UUID] = []

class SceneThesisRead(BaseModel):
    id: UUID
    scene_id: UUID
    thesis_type: str
    text: str
    priority: int
    status: str  # 'active', 'resolved', 'superseded'
    visibility: str
    source_turn_id: int | None
    pinned: bool
    related_entity_ids: list[UUID] = []
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

class SceneThesisUpdate(BaseModel):
    text: str | None = None
    priority: int | None = None
    status: str | None = None
    visibility: str | None = None
    pinned: bool | None = None
    related_entity_ids: list[UUID] | None = None
