"""The validator grounds the model's item reviews against registered identities and the candidate."""
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


def _review(item, reference="mention", claims=()):
    return {"item_id": str(item), "reference": reference, "claims": list(claims)}


def _data(*reviews):
    return {"verdict": "pass", "violations": [], "item_reviews": list(reviews)}


def _interpret(data, text=TEXT):
    return TurnAuthorityValidator._interpret(_authority(), text, data)


def test_grounded_owned_claim_passes():
    result = _interpret(_data(_review(LETTER, "position", [_claim(LETTER, "Ирма держит письмо")]), _review(KEY)))
    assert result.verdict == "pass"


def test_quoted_evidence_is_unwrapped_before_grounding():
    result = _interpret(_data(_review(LETTER, "position", [_claim(LETTER, "«Ирма держит письмо»")]), _review(KEY)))
    assert result.verdict == "pass"


def test_claim_contradicting_canon_owner_requires_repair():
    result = _interpret(_data(_review(LETTER, "position", [_claim(LETTER, "Ирма держит письмо", owner=NPC)]),
                              _review(KEY)))
    assert result.verdict == "repair_required"


def test_keyed_reviews_are_accepted_when_keys_match_identities():
    keyed = {str(LETTER): _review(LETTER), str(KEY): _review(KEY)}
    assert _interpret({"verdict": "pass", "violations": [], "item_reviews": keyed}).verdict == "pass"


@pytest.mark.parametrize("data", [
    pytest.param({"verdict": "pass", "item_reviews": {str(LETTER): _review(LETTER)}}, id="keyed-missing-item"),
    pytest.param({"verdict": "pass", "item_reviews": {str(LETTER): _review(KEY), str(KEY): _review(KEY)}},
                 id="keyed-identity-mismatch"),
    pytest.param(_data(_review(LETTER)), id="review-missing"),
    pytest.param(_data(_review(LETTER), _review(LETTER), _review(KEY)), id="review-duplicated"),
    pytest.param(_data(_review(LETTER), _review(uuid4())), id="unknown-item"),
    pytest.param(_data(_review(LETTER, "position"), _review(KEY)), id="position-without-claims"),
    pytest.param(_data(_review(LETTER, "mention", [_claim(LETTER, "Ирма держит письмо")]), _review(KEY)),
                 id="claims-without-position"),
    pytest.param(_data(_review(LETTER, "absent"), _review(KEY)), id="mentioned-item-marked-absent"),
    pytest.param(_data(_review(LETTER, "position", [_claim(KEY, "Ключ остался на столе")]), _review(KEY)),
                 id="claim-about-another-item"),
    pytest.param(_data(_review(LETTER, "position", [_claim(LETTER, "Ирма прячет письмо")]), _review(KEY)),
                 id="evidence-not-in-candidate"),
])
def test_ungrounded_review_is_refused(data):
    with pytest.raises(NarrationValidationError):
        _interpret(data)


def test_absent_is_accepted_for_an_unmentioned_item():
    result = _interpret(_data(_review(LETTER, "absent"), _review(KEY)), text="Ключ остался на столе.")
    assert result.verdict == "pass"
