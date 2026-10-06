from app.models.action_sequence import (
    ActionSequenceExecution,
    take_current_execution,
)


def take_action_execution() -> ActionSequenceExecution | None:
    return take_current_execution()
