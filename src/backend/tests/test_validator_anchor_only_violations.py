"""Perf lever 3: a violation is a typed ban plus a sentence anchor; no prose explanation on the wire."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

pytestmark = pytest.mark.interagent_contract_enforced

from app.models.item_state import ItemStateSnapshot
from app.models.turn_authority import TurnAuthority
from app.services.turn_authority_validator import TurnAuthorityValidator, candidate_sentences

TEXT = "Борм ставит кружку. Ты говоришь ему: «Я ухожу»."


def _wire_violation(router):
    model = router.generate_json.await_args.kwargs["response_model"]
    schema = model.model_json_schema()
    return schema["$defs"][schema["properties"]["violations"]["items"]["$ref"].split("/")[-1]]


@pytest.mark.asyncio
@pytest.mark.parametrize("items", [False, True])
async def test_violation_wire_is_a_ban_and_an_anchor_and_code_rebuilds_the_evidence(items):
    hero = uuid4()
    auth = TurnAuthority(campaign_id=uuid4(), trigger_turn_id=uuid4(), player_input="Молчу.",
                         item_states=[ItemStateSnapshot(item_id=uuid4(), name="Кошель", owner_id=hero)] if items else [])
    found = {"violation_type": "player_agency", "sentence": "S1", "known_absent_name": None,
             **({"state_domain": "other", "item_id": None} if items else {})}
    router = SimpleNamespace(generate_json=AsyncMock(return_value={
        "violations": [found], "entrances": [], "names_given": [], **({"position_claims": []} if items else {})}))
    result = await TurnAuthorityValidator(router).validate(SimpleNamespace(), auth, TEXT)
    wire = _wire_violation(router)
    assert "correction" not in wire["properties"] and "evidence" not in wire["properties"]
    assert "severity" not in wire["properties"] and set(wire["properties"]["sentence"]["enum"]) == {"S0", "S1"}
    assert "verdict" not in router.generate_json.await_args.kwargs["response_model"].model_json_schema()["properties"]
    assert result.verdict == "repair_required"
    assert result.violations[0].evidence == candidate_sentences(TEXT)["S1"]
