from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

FactMemoryKind = Literal["world_canon", "entity_state", "scene_state"]


class FactCreate(BaseModel):
    subject: str
    predicate: str
    object_value: str | None = None
    truth_status: str = "true"
    confidence: float = 1.0
    visibility: str = "dm"
    source_turn_id: UUID | None = None
    scope: Literal["campaign", "scene"] = "campaign"
    scene_id: UUID | None = None
    memory_kind: FactMemoryKind | None = None
    subject_entity_id: UUID | None = None

    @model_validator(mode="after")
    def validate_scope(self):
        if self.memory_kind is None:
            self.memory_kind = (
                "scene_state" if self.scope == "scene" else "world_canon"
            )
        if self.memory_kind == "scene_state":
            self.scope = "scene"
            if self.scene_id is None:
                raise ValueError("scene_state requires scene_id")
        else:
            self.scope = "campaign"
            self.scene_id = None
        return self


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
    scope: Literal["campaign", "scene"]
    scene_id: UUID | None
    memory_kind: FactMemoryKind = "world_canon"
    subject_entity_id: UUID | None = None
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
    scope: Literal["campaign", "scene"] | None = None
    scene_id: UUID | None = None
    memory_kind: FactMemoryKind | None = None
    subject_entity_id: UUID | None = None
    is_current: bool | None = None
    superseded_by: UUID | None = None
