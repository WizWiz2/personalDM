from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.turn import ChatMessage
from app.services.turn_authority_planner import CoordinatedTurnPlan, TurnAuthorityPlanner


def step(label):
    return {'action_type': 'interaction', 'intent': label,
            'resolution': 'auto_success', 'safe_mundane': True,
            'observable_outcome': label}


@pytest.mark.asyncio
async def test_patch_recompiles_executor_payload_without_mutating_original():
    original = CoordinatedTurnPlan.model_validate({
        'player_intent': 'Execute the sequence', 'resolution': 'sequence',
        'action_sequence': {'steps': [step('A'), step('C')]},
    })
    router = SimpleNamespace(generate_json=AsyncMock(return_value={
        'patches': [{'insert_at': 1, 'step': step('B')}],
    }))
    result = await TurnAuthorityPlanner(router)._apply_compound_action_patch(
        None, [ChatMessage(role='system', content='State')], 'Execute the sequence',
        original, ['Missing an action'],
    )
    assert [s.intent for s in result.action_sequence.steps] == ['A', 'B', 'C']
    assert [s['intent'] for s in result.scene_transition.sequence_payload['steps']] == ['A', 'B', 'C']
    assert [s.intent for s in original.action_sequence.steps] == ['A', 'C']
    assert [s['intent'] for s in original.scene_transition.sequence_payload['steps']] == ['A', 'C']


@pytest.mark.asyncio
async def test_invalid_original_index_rejects_entire_patch():
    original = CoordinatedTurnPlan.model_validate({
        'player_intent': 'Execute the sequence', 'resolution': 'sequence',
        'action_sequence': {'steps': [step('A')]},
    })
    router = SimpleNamespace(generate_json=AsyncMock(return_value={
        'patches': [{'insert_at': 0, 'step': step('B')},
                    {'insert_at': 2, 'step': step('C')}],
    }))
    result = await TurnAuthorityPlanner(router)._apply_compound_action_patch(
        None, [ChatMessage(role='system', content='State')], 'Execute the sequence',
        original, ['Missing actions'],
    )
    assert result is original


@pytest.mark.asyncio
async def test_outcomeless_success_patch_is_rejected_atomically():
    original = CoordinatedTurnPlan.model_validate({
        'player_intent': 'Execute the sequence', 'resolution': 'sequence',
        'action_sequence': {'steps': [step('A')]},
    })
    before = original.model_dump()
    router = SimpleNamespace(generate_json=AsyncMock(return_value={
        'patches': [{'insert_at': 1, 'step': {**step('B'), 'observable_outcome': None}}],
    }))
    result = await TurnAuthorityPlanner(router)._apply_compound_action_patch(
        None, [ChatMessage(role='system', content='State')], 'Execute the sequence',
        original, ['Missing actions'],
    )
    assert result is original
    assert original.model_dump() == before
