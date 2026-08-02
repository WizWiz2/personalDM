from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class MemoryKind(str, Enum):
    WORLD_CANON = "world_canon"
    ENTITY_STATE = "entity_state"
    SCENE_STATE = "scene_state"
    NARRATIVE_DETAIL = "narrative_detail"


class NarrativeDetailType(str, Enum):
    AMBIENT = "ambient"
    SENSORY = "sensory"
    GAZE = "gaze"
    EXPRESSION = "expression"
    GESTURE = "gesture"
    POSE = "pose"
    SPATIAL = "spatial"
    OTHER = "other"


class NarrativeDetailCreate(BaseModel):
    scene_id: UUID
    text: str = Field(min_length=1, max_length=2000)
    detail_type: NarrativeDetailType = NarrativeDetailType.OTHER
    subject_entity_id: UUID | None = None
    visibility: str = "public"
    source_turn_id: UUID | None = None
    turn_window: int = Field(default=3, ge=1, le=12)


class NarrativeDetailRead(BaseModel):
    id: UUID
    campaign_id: UUID
    scene_id: UUID
    text: str
    detail_type: NarrativeDetailType
    subject_entity_id: UUID | None
    visibility: str
    source_turn_id: UUID | None
    turn_window: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
