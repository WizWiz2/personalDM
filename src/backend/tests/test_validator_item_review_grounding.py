"""The validator grounds the model's position claims against registered identities and the candidate."""
from uuid import uuid4

import pytest

from app.models.item_state import ItemStateSnapshot
from app.models.turn_authority import TurnAuthority
from app.services.narration_validator import NarrationValidationError
from app.services.turn_authority_validator import TurnAuthorityValidator

LETTER, KEY, HERO, NPC = uuid4(), uuid4(), uuid4(), uuid4()
TEXT = "Ирма держит письмо. Ключ остался на столе."


def _authority() -> TurnAuthority:
    return TurnAuthority(
        campaign_id=uuid4(), trigger_turn_id=uuid4(), player_input="Смотрю",
        item_states=[
            ItemStateSnapshot(item_id=LETTER, name="Письмо", owner_id=HERO, owner_name="Ирма"),
            ItemStateSnapshot(item_id=KEY, name="Ключ", owner_id=HERO, owner_name="Ирма"),
        ],
    )


def _claim(item, evidence, owner=HERO):
    return {"item_id": str(item), "evidence": evidence, "scope": "current", "mode": "owned",
            "owner_id": str(owner), "location_id": None}


def _data(*claims):
    return {"violations": [], "position_claims": list(claims)}


def _interpret(data, text=TEXT):
    return TurnAuthorityValidator._interpret(_authority(), text, data)


def test_grounded_owned_claim_passes():
    assert _interpret(_data(_claim(LETTER, "Ирма держит письмо"))).verdict == "pass"


def test_quoted_evidence_is_unwrapped_before_grounding():
    assert _interpret(_data(_claim(LETTER, "«Ирма держит письмо»"))).verdict == "pass"


def test_claim_contradicting_canon_owner_requires_repair():
    assert _interpret(_data(_claim(LETTER, "Ирма держит письмо", owner=NPC))).verdict == "repair_required"


def test_prose_without_position_claims_passes():
    assert _interpret(_data()).verdict == "pass"


@pytest.mark.parametrize("data", [
    pytest.param(_data(_claim(uuid4(), "Ирма держит письмо")), id="unknown-item"),
    pytest.param(_data(_claim(LETTER, "Ирма прячет письмо")), id="evidence-not-in-candidate"),
])
def test_ungrounded_claim_is_refused(data):
    with pytest.raises(NarrationValidationError):
        _interpret(data)
