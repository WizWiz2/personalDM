"""Placement is selected by semantic review, never by a production verb list."""
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.models.turn import ChatMessage
from tests.test_player_intent_interpreter import _Router, _interpreter


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,placement", [
    ("place", "между лампой и стеной"),
    ("reposition", "под каблуком"),
    ("reposition", "в подкладке пальто"),
])
async def test_semantic_placement_keeps_identity_operation_and_surface(operation, placement):
    item_id = str(uuid4())
    text = "Размещаю находку так, как задумала."
    router = _Router({"summary": text, "actions": [{"action_type": "interaction", "intent": text}]}, review={
        "action_ownership": [{"action_index": 0, "actor_role": "speaker", "contribution_kind": "world_action",
            "action_type": "inventory", "spatial_effect": "local", "item_id": item_id,
            "inventory_operation": operation, "item_placement_detail": placement, "evidence_quote": text}],
        "information_request_only": False, "information_recipient": "none",
    })
    context = [ChatMessage(role="system", content=f"Player-owned items: жетон [id={item_id}]")]
    action = (await _interpreter(router).interpret(SimpleNamespace(), context, text)).actions[0]
    assert (action.item_id, action.inventory_operation, action.item_placement_detail) == (
        UUID(item_id), operation, placement)


@pytest.mark.asyncio
async def test_unresolved_reference_does_not_turn_a_noun_into_inventory_authority():
    text = "Письмо пусть пока останется там."
    router = _Router({"summary": text, "actions": []})
    result = await _interpreter(router).interpret(SimpleNamespace(), [], text)
    assert result.actions == []
