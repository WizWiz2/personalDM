"""A parsed first-person request must not replace the model-selected executor ID."""
from types import SimpleNamespace

import pytest

from app.db.tables import Item
from app.models.player_intent import ActionOutcomeDecision, TurnOutcomeDecision
from app.services.linguistic_intent_analyzer import LinguisticIntentAnalysis
from app.services.player_intent_interpreter import (
    PlayerIntentContractDraft,
    PlayerIntentInterpreter,
    normalize_intent_draft,
)
from tests.test_qa219_regressions import _world
from tests.test_qa223_regressions import _execute


@pytest.mark.asyncio
@pytest.mark.parametrize("syntax", [LinguisticIntentAnalysis(action_roles=("speaker",)),
                                    LinguisticIntentAnalysis(uniform_action_role="speaker")])
async def test_npc_selected_by_id_places_its_document_even_inside_first_person_request(db_session, syntax):
    cid, room, _, hero, npc, letter, scene = await _world(db_session)
    item = await db_session.get(Item, str(letter))
    item.current_owner_id, item.current_location_id = str(npc), None
    text = "Я вежливо прошу Алису положить оригинал письма на стол."
    draft = PlayerIntentContractDraft.model_validate({"summary": text, "actions": [{
        "evidence_quote": text, "actor_id": str(npc), "contribution_kind": "world_action",
        "action_type": "inventory", "item_id": str(letter), "inventory_operation": "place",
        "item_placement_detail": "на столе", "intent": "Положить оригинал на стол."}]})
    interpreter = PlayerIntentInterpreter(SimpleNamespace(), SimpleNamespace(analyze=lambda *_: syntax))
    interpreter._apply_ownership(draft, text, str(hero))
    contract = normalize_intent_draft(draft, text)
    assert contract.actions[0].requested_inventory.actor_id == npc
    execution = await _execute(db_session, cid, scene, contract, TurnOutcomeDecision(action_outcomes=[
        ActionOutcomeDecision(action_index=0, resolution="auto_success", npc_response="accepted",
                              observable_outcome="Алиса кладёт оригинал на стол.")]))
    assert execution.steps[0].status == "completed"
    assert (item.current_owner_id, item.current_location_id, item.placement_detail) == (None, str(room), "на столе")
