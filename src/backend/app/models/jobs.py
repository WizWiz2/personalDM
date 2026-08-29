from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class GenerationPhase(StrEnum):
    RECEIVED = "received"
    PLANNED = "planned"
    PREPARED = "prepared"
    NARRATED = "narrated"
    PUBLISHED = "published"
    POST_TURN_DONE = "post_turn_done"
    COMPENSATED = "compensated"


class GenerationRunRead(BaseModel):
    id: UUID
    campaign_id: UUID
    user_turn_id: UUID
    assistant_turn_id: UUID | None
    status: str
    cancel_requested: bool
    error: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class GenerationLifecycleRead(BaseModel):
    generation_run_id: UUID
    phase: GenerationPhase
    attempt: int
    received_at: datetime | None
    planned_at: datetime | None
    prepared_at: datetime | None
    narrated_at: datetime | None
    published_at: datetime | None
    post_turn_done_at: datetime | None
    compensated_at: datetime | None
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PostTurnJobRead(BaseModel):
    id: UUID
    campaign_id: UUID
    assistant_turn_id: UUID
    job_type: str
    status: str
    attempts: int
    error: str | None
    locked_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
