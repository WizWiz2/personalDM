"""verify281: the validator spent ~1.2k output tokens walking every audited item ("absent", "mention").
Its wire now carries only the position claims the prose makes and the violations; the verdict is
derived in code. Completeness of conflict detection is the typed comparison, not a per-item essay."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

pytestmark = pytest.mark.interagent_contract_enforced

from app.models.item_state import ItemStateSnapshot
from app.models.turn_authority import TurnAuthority
from app.services.turn_authority_validator import TurnAuthorityValidator, inventory_validation_model


def _authority(hero, items):
    return TurnAuthority(campaign_id=uuid4(), trigger_turn_id=uuid4(), player_input="Пью сидр.",
                         item_states=[ItemStateSnapshot(item_id=item, name=f"Вещь {n}", owner_id=hero)
                                      for n, item in enumerate(items)])


def test_wire_has_no_per_item_walkthrough_verdict_or_summary():
    auth = _authority(uuid4(), [uuid4() for _ in range(7)])
    props = inventory_validation_model(auth, "Тарин пьёт сидр.").model_json_schema()["properties"]
    assert set(props) == {"position_claims", "violations"}


@pytest.mark.asyncio
async def test_silent_prose_passes_with_an_empty_claim_list():
    auth = _authority(uuid4(), [uuid4() for _ in range(7)])
    router = SimpleNamespace(generate_json=AsyncMock(return_value={"position_claims": [], "violations": []}))
    assert (await TurnAuthorityValidator(router).validate(SimpleNamespace(), auth, "Тарин пьёт сидр.")).verdict == "pass"


@pytest.mark.asyncio
async def test_a_single_reported_claim_against_custody_is_still_caught():
    hero, npc, coin = uuid4(), uuid4(), uuid4()
    text = "Серебряк теперь у купца."
    router = SimpleNamespace(generate_json=AsyncMock(return_value={"violations": [], "position_claims": [{
        "item_id": str(coin), "evidence": text, "change": "moved", "scope": "current", "mode": "owned", "owner_id": str(npc)}]}))
    result = await TurnAuthorityValidator(router).validate(SimpleNamespace(), _authority(hero, [coin, uuid4()]), text)
    assert result.verdict == "repair_required" and result.violations[0].violation_type == "canon_conflict"
