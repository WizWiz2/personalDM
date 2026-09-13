from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services import turn_intent_pipeline
from app.services.turn_planner import TurnPlanningError
from app.services.turn_saga import TurnSaga


@pytest.mark.asyncio
async def test_failed_planning_preserves_cause_instead_of_returning_empty_fallback(monkeypatch):
    cause = TurnPlanningError("invalid frozen action coverage")
    pipeline = SimpleNamespace(
        plan=AsyncMock(side_effect=cause),
    )
    monkeypatch.setattr(turn_intent_pipeline, "TurnIntentPlanningPipeline", lambda *_: pipeline)
    router = SimpleNamespace(resolve=AsyncMock(return_value=SimpleNamespace()))

    with pytest.raises(TurnPlanningError, match="invalid frozen action coverage") as error:
        await TurnSaga._plan(
            SimpleNamespace(_session=AsyncMock()),
            campaign_id=uuid4(),
            user_input="Я выхожу в коридор.",
            messages=[],
            role_router=router,
            primary_config=None,
        )

    assert error.value.__cause__ is cause
    pipeline.plan.assert_awaited_once()
