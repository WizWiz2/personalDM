"""The validator audits items this prose can touch (here, in hand, moved or split this turn),
not the whole campaign inventory; a remote item cannot be silently rewritten either."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.models.turn import ChatMessage
from tests.run_ci_mock_simulation_v3 import deterministic_generate_json

pytestmark = pytest.mark.interagent_contract_enforced

from app.models.item_state import ItemStateSnapshot
from app.models.turn_authority import PlannedItemIntroduction, TurnAuthority
from app.services.turn_authority_validator import TurnAuthorityValidator, inventory_validation_model


def _world():
    hero, here, far, keeper = uuid4(), uuid4(), uuid4(), uuid4()
    states = {
        "held": ItemStateSnapshot(item_id=uuid4(), name="Посох", owner_id=hero, owner_location_id=here),
        "here": ItemStateSnapshot(item_id=uuid4(), name="Кружка", location_id=here),
        "npc_here": ItemStateSnapshot(item_id=uuid4(), name="Ключ", owner_id=keeper, owner_location_id=here),
        "far": ItemStateSnapshot(item_id=uuid4(), name="Письмо", location_id=far),
        "split_source": ItemStateSnapshot(item_id=uuid4(), name="Кошель", location_id=far, quantity=3),
    }
    auth = TurnAuthority(
        campaign_id=uuid4(), trigger_turn_id=uuid4(), player_character_id=hero, player_input="Пью сидр.",
        place_frame={"location_id": str(here)}, item_states=list(states.values()),
        allowed_new_items=[PlannedItemIntroduction(canonical_name="серебряк", evidence="серебряк лежит",
                                                   taken_from_item_id=states["split_source"].item_id, quantity=1)],
    )
    return auth, states


def test_audit_covers_only_items_the_turn_can_touch():
    auth, states = _world()
    reviews = inventory_validation_model(auth).model_json_schema()["$defs"]["ItemReviewsByIdentity"]["required"]
    assert set(reviews) == {str(states[k].item_id) for k in ("held", "here", "npc_here", "split_source")}


@pytest.mark.asyncio
async def test_deterministic_simulation_honors_the_current_scoped_review_schema():
    auth, states = _world()
    wire = inventory_validation_model(auth, "В комнате тихо.")
    response = await deterministic_generate_json(SimpleNamespace(), [ChatMessage(role="user", content=(
        "[FOUR BANS NARRATION CHECK]\n[TURN FACTS]\n"
        + json.dumps(auth.validator_payload(), ensure_ascii=False)
        + "\n\n[CANDIDATE NARRATION]\nВ комнате тихо."))], None, response_model=wire)
    assert str(states["far"].item_id) not in response["item_reviews"]
    assert set(wire.model_validate(response).item_reviews.model_dump()) == {
        str(state.item_id) for state in auth.audited_item_states()}


@pytest.mark.asyncio
async def test_a_conflict_about_an_unaudited_item_still_fails_publication():
    auth, states = _world()
    audited = [s for k, s in states.items() if k != "far"]
    reviews = {str(s.item_id): {"item_id": str(s.item_id), "reference": "absent", "claims": []} for s in audited}
    conflict = {"violation_type": "canon_conflict", "severity": "error", "state_domain": "item_position",
                "item_id": str(states["far"].item_id), "evidence": "Письмо у тебя в кармане.",
                "explanation": "Письмо лежит в другом месте.", "correction": "Письмо осталось на месте."}
    router = SimpleNamespace(generate_json=AsyncMock(return_value={
        "verdict": "repair_required", "item_reviews": reviews, "violations": [conflict]}))
    result = await TurnAuthorityValidator(router).validate(SimpleNamespace(), auth, "Письмо у тебя в кармане.")
    assert result.verdict == "repair_required"
