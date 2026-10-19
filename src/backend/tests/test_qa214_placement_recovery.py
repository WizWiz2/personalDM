"""QA-213 F-32: missing inventory intent is recovered only with exact evidence and catalog IDs."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.models.turn import ChatMessage
from app.services.player_intent_interpreter import (
    PlayerIntentContractDraft, PlayerIntentInterpreter, normalize_intent_draft,
)


@pytest.mark.asyncio
async def test_missing_reposition_recovers_catalogued_item_and_exact_surface():
    coin = uuid4()
    human = "Ставлю монету под каблук."
    decision = {
        "recovered": True, "item_id": str(coin), "actor_role": "speaker",
        "evidence_quote": "Ставлю монету под каблук",
        "destination_quote": "под каблук",
    }
    router = SimpleNamespace(generate_json=AsyncMock(return_value=decision))
    interpreter = PlayerIntentInterpreter(router)
    draft = PlayerIntentContractDraft(summary=human, actions=[])
    context = [ChatMessage(
        role="system",
        content=f"Objects physically here: Серебряная монета [id={coin}]",
    )]
    await interpreter._recover_missing_placement(None, draft, human, context)
    result = normalize_intent_draft(draft, human)
    assert len(result.actions) == 1
    assert result.actions[0].action_type == "inventory"
    assert result.actions[0].inventory_operation == "reposition"
    assert result.actions[0].item_id == coin
    assert result.actions[0].item_placement_detail == "под каблук"


@pytest.mark.asyncio
async def test_missing_placement_cannot_invent_destination_evidence():
    coin = uuid4()
    router = SimpleNamespace(generate_json=AsyncMock(return_value={
        "recovered": True, "item_id": str(coin), "actor_role": "speaker",
        "evidence_quote": "Кладу монету на стойку",
        "destination_quote": "в карман Зени",
    }))
    interpreter = PlayerIntentInterpreter(router)
    draft = PlayerIntentContractDraft(summary="Кладу монету на стойку", actions=[])
    context = [ChatMessage(
        role="system", content=f"Objects physically here: Монета [id={coin}]",
    )]
    await interpreter._recover_missing_placement(
        None, draft, "Кладу монету на стойку", context,
    )
    assert not draft.actions
