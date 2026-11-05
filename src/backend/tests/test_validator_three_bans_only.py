"""Round 2: prose cannot change the world, so the validator judges prose only for invented people,
written player actions and an overwritten past. Item positions and movement are typed upstream."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

pytestmark = pytest.mark.interagent_contract_enforced

from app.models.item_state import ItemStateSnapshot
from app.models.turn_authority import TurnAuthority
from app.services.narration_publication_guard import NarrationPublicationGuard
from app.services.turn_authority_validator import TurnAuthorityValidator

TEXT = "Две монеты лежат на стойке. Борм кивает."


def _authority():
    hero = uuid4()
    return TurnAuthority(campaign_id=uuid4(), trigger_turn_id=uuid4(), player_input="Плачу.",
                         item_states=[ItemStateSnapshot(item_id=uuid4(), name="Две монеты", owner_id=hero)])


@pytest.mark.asyncio
async def test_validator_wire_has_only_violations_over_three_prose_bans():
    router = SimpleNamespace(generate_json=AsyncMock(return_value={"violations": [], "names_given": []}))
    auth = _authority()
    result = await TurnAuthorityValidator(router).validate(SimpleNamespace(), auth, TEXT)
    kwargs = router.generate_json.await_args.kwargs
    schema = kwargs["response_model"].model_json_schema()
    assert set(schema["properties"]) <= {"violations", "names_given"}
    found = schema["$defs"][schema["properties"]["violations"]["items"]["$ref"].split("/")[-1]]
    assert set(found["properties"]["violation_type"]["enum"]) == {"absent_character", "canon_conflict", "player_agency"}
    system = router.generate_json.await_args.args[2][0].content
    assert "invalid_movement" not in system and "position_claims" not in system
    assert result.verdict == "pass"
    # A passing check publishes the prose verbatim: item state is owned by the executor, not certified by prose.
    text, publication = NarrationPublicationGuard.publish(auth, TEXT, result)
    assert text == TEXT and publication["mode"] == "validated_candidate"
