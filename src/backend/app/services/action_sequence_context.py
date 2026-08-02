from app.models.action_sequence import (
    ActionSequenceExecution,
    set_current_execution,
    take_current_execution,
)


def set_action_execution(execution: ActionSequenceExecution | None) -> None:
    set_current_execution(execution)


def take_action_execution() -> ActionSequenceExecution | None:
    return take_current_execution()
