"""A granted NPC placement must become custody state before prose is validated."""
from types import SimpleNamespace

import pytest

from app.db.tables import Item
from app.services.linguistic_intent_analyzer import LinguisticIntentAnalysis
from app.services.player_intent_interpreter import (
    PlayerIntentContractDraft,
    PlayerIntentInterpreter,
    normalize_intent_draft,
)
from app.services.turn_outcome_resolver import TurnOutcomeDecisionDraft, normalize_outcome_draft
from tests.test_qa219_regressions import _world
from tests.test_qa223_regressions import _execute


@pytest.mark.asyncio
async def test_local_npc_request_resolves_placement_as_a_typed_effect_before_narration(db_session):
    cid, room, _, hero, npc, letter, scene = await _world(db_session)
    item = await db_session.get(Item, str(letter))
    item.current_owner_id, item.current_location_id = str(npc), None
    text = "Я прошу Алису положить оригинал письма на стол."
    draft = PlayerIntentContractDraft.model_validate({"summary": text, "actions": [{
        "actor_id": str(npc), "evidence_quote": text, "contribution_kind": "world_action",
        "action_type": "interaction", "intent": "Положить оригинал на стол.", "subject_item_id": str(letter)}]})
    interpreter = PlayerIntentInterpreter(SimpleNamespace(), SimpleNamespace(analyze=lambda *_: LinguisticIntentAnalysis()))
    interpreter._apply_ownership(draft, text, str(hero))
    contract = normalize_intent_draft(draft, text)
    outcome = TurnOutcomeDecisionDraft.model_validate({"action_outcomes": [{"action_index": 0,
        "resolution": "auto_success", "npc_response": "accepted", "observable_outcome": "Алиса кладёт оригинал на стол."}],
        "npc_item_placements": [{"action_index": 0, "item_id": str(letter), "operation": "place", "placement_detail": "на столе"}]})
    decision = normalize_outcome_draft(outcome, contract)
    execution = await _execute(db_session, cid, scene, contract, decision)
    assert execution.steps[0].status == "completed"
    assert (item.current_owner_id, item.current_location_id, item.placement_detail) == (None, str(room), "на столе")
    assert execution.steps[0].item_id == letter


def test_native_placement_requires_the_frozen_subject_and_explicit_consent():
    from uuid import uuid4

    from pydantic import ValidationError

    from app.services.turn_outcome_resolver import _outcome_wire_model

    subject, unrelated = map(str, (uuid4(), uuid4()))
    wire = _outcome_wire_model(1, allow_choice=False, npc_placement_targets={0: subject})
    data = {"action_outcomes": [{"action_index": 0, "resolution": "auto_success",
        "npc_response": "accepted", "observable_outcome": "Лист положен на стол."}],
        "npc_item_placements": [{"action_index": 0, "item_id": subject,
                                 "operation": "place", "placement_detail": "на столе"}]}
    assert str(wire.model_validate(data).npc_item_placements[0].item_id) == subject
    with pytest.raises(ValidationError):
        wire.model_validate({**data, "npc_item_placements": [
            {**data["npc_item_placements"][0], "item_id": unrelated}]})
    with pytest.raises(ValidationError, match="accepted request"):
        wire.model_validate({**data, "action_outcomes": [
            {**data["action_outcomes"][0], "npc_response": "refused"}]})
    with pytest.raises(ValidationError, match="accepted request"):
        wire.model_validate({**data, "npc_item_placements": data["npc_item_placements"] * 2})
    with pytest.raises(ValidationError):
        _outcome_wire_model(1, npc_placement_targets={}).model_validate(data)
