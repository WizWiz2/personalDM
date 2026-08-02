from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class SceneBridgeRead(BaseModel):
    id: UUID
    campaign_id: UUID
    transition_id: UUID
    source_scene_id: UUID | None
    target_scene_id: UUID
    status: str
    previous_scene_summary: str
    carried_goals: list[str] = Field(default_factory=list)
    unresolved_threads: list[str] = Field(default_factory=list)
    departed_participant_ids: list[UUID] = Field(default_factory=list)
    departed_participant_names: list[str] = Field(default_factory=list)
    carried_participant_ids: list[UUID] = Field(default_factory=list)
    carried_participant_names: list[str] = Field(default_factory=list)
    negative_placement_facts: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    applied_at: datetime | None
    undone_at: datetime | None
