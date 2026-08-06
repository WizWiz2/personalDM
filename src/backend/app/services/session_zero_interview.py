"""Compatibility import for the native Session Zero agent.

The conversational implementation lives in ``session_zero_agent``. This module keeps the
historic import path used by CLI, API code and existing integrations.
"""

import asyncio

from app.services.role_model_router import RoleModelRouter
from app.services.session_zero_agent import (
    SessionZeroAgent,
    SessionZeroInterviewIncompleteError,
    SessionZeroInterviewService,
)

__all__ = [
    "RoleModelRouter",
    "SessionZeroAgent",
    "SessionZeroInterviewIncompleteError",
    "SessionZeroInterviewService",
    "asyncio",
]
