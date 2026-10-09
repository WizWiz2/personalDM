"""A wall-clock allowance shared by interactive model calls, including queues/retries.

Background jobs have no interactive deadline. Database publication and compensation are
outside the model allowance so a timeout cannot interrupt their transactions.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from time import monotonic

from app.providers.llm_provider import LLMProviderError


class InteractiveBudgetExceeded(LLMProviderError):
    pass


_DEADLINE: ContextVar[float | None] = ContextVar("interactive_deadline", default=None)


def detach() -> None:
    """Child background tasks inherit ContextVars; explicitly drop the parent's deadline."""
    _DEADLINE.set(None)


@contextmanager
def interactive_budget(seconds: float):
    deadline = monotonic() + seconds
    inherited = _DEADLINE.get()
    token = _DEADLINE.set(min(inherited, deadline) if inherited is not None else deadline)
    try:
        yield
    finally:
        _DEADLINE.reset(token)


def remaining() -> float | None:
    deadline = _DEADLINE.get()
    return max(0.0, deadline - monotonic()) if deadline is not None else None


def request_timeout(default: float, cap: float) -> float:
    left = remaining()
    if left is None:
        return default
    if left <= 0:
        raise InteractiveBudgetExceeded("Interactive model time budget exhausted")
    return min(default, cap, left)
