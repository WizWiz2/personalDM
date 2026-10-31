from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.models.turn import ChatMessage


async def reconcile_starter_item_custody(router, provider, selection, draft, messages):
    """Bind opening custody to session-zero agreements before materializing any item.

    Physical placement and custody are separate: a keeper can control a key on a
    board. Only the session-zero compiler calls this, never the prose renderer.
    """
    items = draft.world.starter_items
    indices = tuple(index for index, item in enumerate(items) if item.present_at_start)
    if not indices:
        return draft
    npcs = draft.world.starter_npcs
    holders = tuple(index for index, npc in enumerate(npcs) if npc.present_at_start)

    class CustodyBinding(BaseModel):
        item_index: Literal[indices]
        holder: Literal["player", "npc", "unowned"] = Field(description=(
            "player: the hero carries it (on the body, worn, in a bag or pocket, in hand); "
            "npc: a present starter NPC holds or controls it; unowned: nobody keeps it."))
        npc_index: Literal[holders] | None if holders else type(None)
        source: str = Field(max_length=800)

        @model_validator(mode="after")
        def valid_holder(self):
            if (self.holder == "npc") != (self.npc_index is not None):
                raise ValueError("Only NPC custody selects a present starter NPC")
            return self

    class CustodyBindings(BaseModel):
        items: list[CustodyBinding] = Field(min_length=len(indices), max_length=len(indices))

        @model_validator(mode="after")
        def complete(self):
            if sorted(item.item_index for item in self.items) != list(indices):
                raise ValueError("Review every opening item exactly once")
            return self

    data = await router.generate_json(provider, selection, [
        ChatMessage(role="system", content=(
            "Bind the custody of EACH physically present opening item to the session-zero "
            "agreements. Player statements override a conflicting draft. Holding, storing, "
            "guarding or controlling access is custody even when the item hangs on a board "
            "or lies on furniture. Placement is not proof of being ownerless. Select the "
            "existing present NPC's index, never invent a keeper or infer ownership merely "
            "from a profession. Choose for every item; unowned only when the agreed item is "
            "actually without a keeper. "
            "source briefly explains the agreed evidence. Do not add items or change the scene."
        )),
        ChatMessage(role="user", content=json.dumps({
            "player_agreements": [m["content"] for m in messages if m.get("role") == "user" and m.get("content")],
            "starting_situation": draft.world.starting_situation,
            "items": [{"item_index": index, **items[index].model_dump(mode="json")} for index in indices],
            "npcs": [{"npc_index": index, **npcs[index].model_dump(mode="json")} for index in holders],
        }, ensure_ascii=False)),
    ], max_tokens=400 + 180 * len(indices), temperature=0.0, response_model=CustodyBindings)
    bindings = CustodyBindings.model_validate(data)
    updated = [item.model_copy(deep=True) for item in items]
    for binding in bindings.items:
        item = updated[binding.item_index]
        item.held_by_player = binding.holder == "player"
        item.held_by_npc = (npcs[binding.npc_index].name or npcs[binding.npc_index].role
                            if binding.holder == "npc" else None)
    return draft.model_copy(update={"world": draft.world.model_copy(update={"starter_items": updated})})
