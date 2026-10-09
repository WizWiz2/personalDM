"""One shared budget across narrator retries, validation and re-adjudication."""
from contextlib import contextmanager
from contextvars import ContextVar

from app.providers.llm_provider import LLMProviderError

_STATE: ContextVar[dict | None] = ContextVar('narration_call_budget', default=None)


@contextmanager
def narration_budget(control_limit: int, render_limit: int):
    state = {'control_used': 0, 'control_limit': control_limit,
             'render_used': 0, 'render_limit': render_limit}
    token = _STATE.set(state)
    try:
        yield state
    finally:
        _STATE.reset(token)


def has_capacity(kind: str) -> bool:
    state = _STATE.get()
    return state is None or state[kind + '_used'] < state[kind + '_limit']


def consume(kind: str) -> None:
    state = _STATE.get()
    if state is None:
        return
    if not has_capacity(kind):
        raise LLMProviderError(f'narration {kind} call budget exhausted')
    state[kind + '_used'] += 1


def consume_control(role: str) -> None:
    if role in {'narration_validator', 'evaluator'}:
        consume('control')
