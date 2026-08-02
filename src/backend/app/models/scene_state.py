from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class LocationExitCreate(BaseModel):
    to_location_id: UUID
    label: str = Field(min_length=1, max_length=255)
    direction: str | None = Field(default=None, max_length=100)
    travel_time: str | None = Field(default=None, max_length=100)
    access_rule: str | None = None
    discovered: bool = True
    active: bool = True
    bidirectional: bool = False
    reverse_label: str | None = Field(default=None, max_length=255)


class LocationExitRead(BaseModel):
    id: UUID
    campaign_id: UUID
    from_location_id: UUID
    to_location_id: UUID
    from_location_name: str
    to_location_name: str
    label: str
    direction: str | None
    travel_time: str | None
    access_rule: str | None
    discovered: bool
    active: bool
    created_at: datetime
    updated_at: datetime


class SceneStateUpdate(BaseModel):
    world_time_label: str | None = Field(default=None, max_length=255)
    world_time_order: int | None = Field(default=None, ge=0)
    scene_goal: str | None = None
    active_conflict: str | None = None


class SceneStateRead(BaseModel):
    campaign_id: UUID
    scene_id: UUID
    scene_status: str
    scene_title: str
    location_id: UUID | None
    location_path: list[str] = Field(default_factory=list)
    world_time_label: str | None
    world_time_order: int
    scene_goal: str | None
    active_conflict: str | None
    participant_ids: list[UUID] = Field(default_factory=list)
    participant_names: list[str] = Field(default_factory=list)
    object_ids: list[UUID] = Field(default_factory=list)
    object_names: list[str] = Field(default_factory=list)
    available_exits: list[LocationExitRead] = Field(default_factory=list)
    invariant_errors: list[str] = Field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.invariant_errors


class SceneStateValidation(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    state: SceneStateRead


class TransitionDestinationCheck(BaseModel):
    source_location_id: UUID
    target_location_id: UUID
    allow_discovery: bool = False

    @model_validator(mode="after")
    def different_locations(self):
        if self.source_location_id == self.target_location_id:
            raise ValueError("source and target locations must differ")
        return self
