from contextvars import ContextVar

from app.models.action_sequence import ActionSequenceExecution


_current_execution: ContextVar[ActionSequenceExecution | None] = ContextVar(
    "current_action_sequence_execution",
    default=None,
)


def set_action_execution(execution: ActionSequenceExecution | None) -> None:
    _current_execution.set(execution)


def take_action_execution() -> ActionSequenceExecution | None:
    execution = _current_execution.get()
    _current_execution.set(None)
    return execution
