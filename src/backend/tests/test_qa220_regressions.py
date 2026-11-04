from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.db.repositories.turn_repo import TurnRepository
from app.db.tables import Item
from app.models.item_state import ItemStateSnapshot
from app.models.narration_validation import NarrationValidationResult
from app.models.player_intent import PlayerActionIntent, PlayerIntentContract
from app.models.turn import TurnCreate
from app.models.turn_authority import TurnAuthority
from app.services.action_plan_compiler import ActionPlanCompiler
from app.services.action_sequence_executor import ActionSequenceExecutor
from app.services.authority_narration_pipeline import AuthorityNarrationPipeline
from app.services.item_state_authority import load_item_states
from app.services.narration_publication_guard import NarrationPublicationGuard
from app.services.narration_validator import NarrationValidationError
from app.services.turn_authority_planner import CoordinatedTurnPlan
from app.services.turn_authority_service import TurnAuthorityService
from app.services.turn_authority_validator import TurnAuthorityValidator
from app.services.turn_outcome_resolver import _outcome_wire_model, normalize_outcome_draft
from app.services.turn_planner import ActionSequencePlan, ActionStepPlan
from tests.test_interagent_turn_authority import _world as _empty_world
from tests.test_qa219_regressions import _world

pytestmark = pytest.mark.interagent_contract_enforced


def _auth():
    item, owner, room = uuid4(), uuid4(), uuid4()
    return TurnAuthority(
        campaign_id=uuid4(), trigger_turn_id=uuid4(), player_input="Осматриваю ключ, не трогая его.",
        object_names=["Ключ от чердака"], item_states=[ItemStateSnapshot(
            item_id=item, name="Ключ от чердака", owner_id=owner, owner_name="Осип Ким",
            placement_detail="Висит на ключевой доске под контролем Осипа.",
            owner_location_id=room,
        )],
    )
























@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["Алиса, возьмите письмо на хранение.", "Передаю Алисе письмо на хранение."])
async def test_player_owned_letter_cannot_decode_as_reverse_receive(db_session, intent):
    cid, _, _, hero, npc, item, scene = await _world(db_session)
    row = await db_session.get(Item, str(item))
    row.current_owner_id = str(hero)
    await db_session.flush()
    wire = _outcome_wire_model(1, handoff_states=await load_item_states(db_session, cid),
                               controlled_id=str(hero), present_ids={str(hero), str(npc)})
    common = {"action_outcomes": [{"action_index": 0, "resolution": "auto_success",
                                   "observable_outcome": "Алиса принимает письмо на хранение."}],
              "npc_introductions": []}
    with pytest.raises(ValidationError):
        wire.model_validate({**common, "npc_item_handoffs": [{"item_id": str(item),
            "giver_id": str(npc), "evidence": "Алиса принимает письмо на хранение."}]})
    draft = wire.model_validate({**common, "npc_item_handoffs": [], "player_item_handoffs": [{
        "item_id": str(item), "recipient_id": str(npc), "offer_evidence": intent,
        "evidence": "Алиса принимает письмо на хранение.", "after_action_index": 0,
    }]})
    contract = PlayerIntentContract(summary=intent, actions=[PlayerActionIntent(action_type="service", intent=intent)])
    plan = await ActionPlanCompiler(db_session).compile(cid, contract, normalize_outcome_draft(draft, contract), player_input=intent)
    user = await TurnRepository(db_session).create(cid, TurnCreate(role="user", content=intent, scene_id=scene))
    report = await ActionSequenceExecutor(db_session).execute(cid, scene, user.id, plan.action_sequence)
    assert report.steps[-1].item_operation == "give"
    assert row.current_owner_id == str(npc)


@pytest.mark.asyncio
async def test_resolver_block_keeps_item_attempt_and_placement_without_mutation(db_session):
    cid, _, elsewhere, _, _, letter, scene = await _world(db_session)
    item = await db_session.get(Item, str(letter))
    item.current_owner_id = None
    item.current_location_id = str(elsewhere)
    item.placement_detail = "На перилах"
    await db_session.flush()
    user = await TurnRepository(db_session).create(cid, TurnCreate(role="user", content="Беру письмо.", scene_id=scene))
    report = await ActionSequenceExecutor(db_session).execute(cid, scene, user.id, ActionSequencePlan(steps=[
        ActionStepPlan(action_type="inventory", intent=user.content, item_id=letter,
                       inventory_operation="take", resolution="blocked", blocking_reason="Письмо снаружи."),
    ]))
    step = report.steps[0]
    assert step.status == "blocked" and step.item_id == letter and step.item_operation == "take"
    assert step.item_previous_location_id == elsewhere
    assert step.item_previous_placement_detail == "На перилах"
    assert step.item_result_owner_id is None
    assert item.current_location_id == str(elsewhere)
