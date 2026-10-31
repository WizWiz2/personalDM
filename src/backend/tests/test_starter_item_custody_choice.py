"""verify-r3 session zero: purse, bag and staff 'при Тарине' were bootstrapped as located in the tavern.
Custody of every opening item is an explicit typed choice; the reviewer cannot punt with 'unchanged'."""
from types import SimpleNamespace

import pytest

from app.models.session_zero_interview import SessionZeroStarterItem
from app.services.starter_item_custody import reconcile_starter_item_custody


@pytest.mark.asyncio
async def test_custody_is_chosen_for_every_opening_item_and_carried_means_player():
    seen = {}

    async def generate_json(provider, selection, messages, **kw):
        model = kw["response_model"]
        seen["holders"] = model.model_json_schema()["$defs"]["CustodyBinding"]["properties"]["holder"]["enum"]
        return {"items": [{"item_index": 0, "holder": "player", "npc_index": None, "source": "при Тарине"}]}

    purse = SessionZeroStarterItem(canonical_name="Кошель", initial_placement="При Тарине", quantity=5)
    draft = SimpleNamespace(world=SimpleNamespace(
        starter_items=[purse], starter_npcs=[], starting_situation="Тарин у стойки.",
        model_copy=lambda update: SimpleNamespace(**update)))
    draft.model_copy = lambda update: SimpleNamespace(**update)
    result = await reconcile_starter_item_custody(SimpleNamespace(generate_json=generate_json), None, None, draft, [])
    assert "unchanged" not in seen["holders"]
    assert result.world.starter_items[0].held_by_player is True
