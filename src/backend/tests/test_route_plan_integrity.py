from uuid import uuid4
from unittest.mock import AsyncMock

import pytest

from app.services.scene_transition_executor import SceneTransitionExecutor
from app.services.turn_planner import ActionSequencePlan, ActionStepPlan, SceneTransitionPlan


@pytest.mark.asyncio
@pytest.mark.parametrize('resolution', ['auto_success', 'blocked'])
async def test_single_movement_is_not_reinterpreted_by_execution(resolution):
    """No lexical recovery may rewrite a semantically planned atomic commitment."""
    step = ActionStepPlan(
        action_type='movement', intent='return to the previously visited place',
        resolution=resolution, safe_mundane=resolution == 'auto_success',
        blocking_reason='route is closed' if resolution == 'blocked' else None,
        observable_outcome='the result for this specific destination',
        transition=SceneTransitionPlan(
            required=True, transition_type='location_transition',
            destination_location='Previously visited place',
            bridge_summary='Destination-specific profile',
        ),
    )
    plan = ActionSequencePlan(steps=[step])
    executor = object.__new__(SceneTransitionExecutor)
    executor.authorize_destination = AsyncMock(side_effect=AssertionError('not route-media filtering'))
    result = await executor._collapse_unauthorized_route_media(plan, uuid4())
    assert result is plan
    assert result.model_dump() == plan.model_dump()
    executor.authorize_destination.assert_not_called()
