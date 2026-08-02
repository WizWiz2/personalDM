from uuid import UUID

from pydantic import BaseModel, Field


class ExecutedActionStep(BaseModel):
    step_index: int = Field(ge=0)
    action_type: str
    intent: str
    resolution: str
    safe_mundane: bool
    status: str
    observable_outcome: str | None = None
    blocking_reason: str | None = None
    transition_id: UUID | None = None
    source_scene_id: UUID | None = None
    target_scene_id: UUID | None = None


class ActionSequenceExecution(BaseModel):
    sequence_id: UUID
    campaign_id: UUID
    trigger_turn_id: UUID
    status: str
    source_scene_id: UUID | None = None
    final_scene_id: UUID | None = None
    summary: str | None = None
    planned_steps: int
    completed_steps: int
    blocked_step_index: int | None = None
    steps: list[ExecutedActionStep] = Field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.blocked_step_index is not None

    @property
    def prepared(self) -> bool:
        return self.status == "prepared"
