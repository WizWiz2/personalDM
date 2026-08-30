from contextvars import ContextVar
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


_current_execution: ContextVar["ActionSequenceExecution | None"] = ContextVar(
    "current_action_sequence_execution",
    default=None,
)


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
    item_id: UUID | None = None
    item_name: str | None = None
    item_operation: str | None = None
    item_previous_owner_id: UUID | None = None
    item_previous_location_id: UUID | None = None
    item_result_owner_id: UUID | None = None
    item_result_location_id: UUID | None = None


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

    @model_validator(mode="after")
    def publish_for_narrator(self):
        _current_execution.set(self)
        return self

    @property
    def blocked(self) -> bool:
        return self.blocked_step_index is not None

    @property
    def prepared(self) -> bool:
        return self.status == "prepared"


def set_current_execution(execution: ActionSequenceExecution | None) -> None:
    _current_execution.set(execution)


def take_current_execution() -> ActionSequenceExecution | None:
    execution = _current_execution.get()
    _current_execution.set(None)
    return execution
