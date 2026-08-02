from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MemoryClass(str, Enum):
    WORLD_CANON = "world_canon"
    ENTITY_STATE = "entity_state"
    SCENE_STATE = "scene_state"
    NARRATIVE_DETAIL = "narrative_detail"


class MemoryRetention(str, Enum):
    DURABLE = "durable"
    UNTIL_SUPERSEDED = "until_superseded"
    SCENE_LIFETIME = "scene_lifetime"
    RECENT_TURNS = "recent_turns"


EXPECTED_RETENTION = {
    MemoryClass.WORLD_CANON: MemoryRetention.DURABLE,
    MemoryClass.ENTITY_STATE: MemoryRetention.UNTIL_SUPERSEDED,
    MemoryClass.SCENE_STATE: MemoryRetention.SCENE_LIFETIME,
    MemoryClass.NARRATIVE_DETAIL: MemoryRetention.RECENT_TURNS,
}


class MemoryMetadata(BaseModel):
    memory_class: MemoryClass
    retention: MemoryRetention
    ttl_turns: int | None = Field(default=None, ge=1, le=8)

    @model_validator(mode="after")
    def validate_lifecycle(self):
        expected = EXPECTED_RETENTION[self.memory_class]
        if self.retention != expected:
            raise ValueError(
                f"{self.memory_class.value} requires retention {expected.value}"
            )
        if self.memory_class == MemoryClass.NARRATIVE_DETAIL:
            self.ttl_turns = self.ttl_turns or 3
        else:
            self.ttl_turns = None
        return self


class NarrativeDetailCreate(BaseModel):
    detail_type: str = "observation"
    text: str = Field(min_length=1, max_length=1000)
    participant_ids: list[UUID] = Field(default_factory=list)
    salience: float = Field(default=0.5, ge=0.0, le=1.0)
    ttl_turns: int = Field(default=3, ge=1, le=8)


class NarrativeDetailRead(BaseModel):
    id: UUID
    campaign_id: UUID
    scene_id: UUID
    source_turn_id: UUID
    detail_type: str
    text: str
    participant_ids: list[UUID] = Field(default_factory=list)
    salience: float
    ttl_turns: int
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
