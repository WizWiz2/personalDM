"""verify-r4: 6/10 turns were repaired, mostly on false item-position conflicts (T3, T4, T5, T10):
an unrelated sentence cited as evidence, the merchant's own bag bound to the hero's herb bag, an open
purse 'on the counter edge'. A claim is cited by a code-numbered sentence anchor S<n> and declares
what that sentence does to the item; only a narrated change is compared with the typed state."""
from uuid import uuid4

import pytest

from app.models.item_state import ItemStateSnapshot
from app.models.turn_authority import TurnAuthority
from app.services.turn_authority_validator import TurnAuthorityValidator, candidate_sentences

pytestmark = pytest.mark.interagent_contract_enforced

TARIN, BORM, MERCHANT, TAVERN = uuid4(), uuid4(), uuid4(), uuid4()
COIN, BAG, PURSE = uuid4(), uuid4(), uuid4()
STATES = [
    ItemStateSnapshot(item_id=COIN, name="один серебряк", location_id=TAVERN, placement_detail="на стойку"),
    ItemStateSnapshot(item_id=BAG, name="сумка трав", owner_id=TARIN, owner_name="Тарин"),
    ItemStateSnapshot(item_id=PURSE, name="кошель с серебряками", owner_id=TARIN, owner_name="Тарин", quantity=2),
]
CASES = {
    "T3": ("Борм слышит вопрос из-за стойки. Он смотрит на серебряк, лежащий на стойке.", COIN,
           {"mode": "located", "location_id": str(TAVERN), "placement_conflicts": True}),
    "T4": ("Торговец подходит к свободному месту. Его дорожный плащ потемнел от дождя, на сапогах засохли "
           "брызги, небольшая сумка висит у бедра.", BAG, {"mode": "owned", "owner_id": str(MERCHANT)}),
    "T5": ("За стойкой остаётся только серебряк, лежащий за расспросы. — Сначала нужно понять, когда вообще "
           "выйдем.", COIN, {"mode": "located", "location_id": None}),
    "T10": ("Шнурок пока не затянут, кошель раскрыт на краю стойки. Внутри звякают два серебряка.", PURSE,
            {"mode": "located", "location_id": str(TAVERN)}),
}


def _auth():
    return TurnAuthority(campaign_id=uuid4(), trigger_turn_id=uuid4(), player_character_id=TARIN,
                         player_input="Смотрю.", item_states=STATES)


@pytest.mark.parametrize("case", sorted(CASES))
def test_static_descriptions_from_verify_r4_never_trigger_a_repair(case):
    text, item, position = CASES[case]
    anchor = list(candidate_sentences(text))[-1]
    data = {"violations": [], "position_claims": [{"item_id": str(item), "sentence": anchor, "change": "static",
                                                   "scope": "current", **position}]}
    assert TurnAuthorityValidator._interpret(_auth(), text, data).verdict == "pass"


def test_an_unrecorded_payment_is_still_a_conflict():
    text = "Тарин отдаёт серебряк торговцу. Тот прячет монету в сумку."
    data = {"violations": [], "position_claims": [{"item_id": str(COIN), "sentence": "S0", "change": "given",
                                                   "scope": "current", "mode": "owned", "owner_id": str(MERCHANT)}]}
    result = TurnAuthorityValidator._interpret(_auth(), text, data)
    assert result.verdict == "repair_required" and result.violations[0].evidence == "Тарин отдаёт серебряк торговцу."
