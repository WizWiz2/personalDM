"""verify281 T8 class: prose hands the coin to the merchant while the typed state keeps it with Tarin.
A holder named in a claim is custody, whatever 'aspect' the model labels it: an owned claim with a
different owner ID is a structural conflict, never a placement detail."""
from uuid import uuid4

from app.models.item_state import ItemPositionClaim, ItemStateSnapshot
from app.services.item_state_authority import item_position_violations

TEXT = "Серебряк оказывается у купца, и тот прячет его в сумку."


def test_prose_only_handoff_is_a_conflict_even_when_labelled_placement():
    tarin, merchant, coin = uuid4(), uuid4(), uuid4()
    state = ItemStateSnapshot(item_id=coin, name="Серебряк", owner_id=tarin, placement_detail="на ладони")
    claim = ItemPositionClaim.model_validate({"item_id": str(coin), "evidence": TEXT, "scope": "current",
                                              "aspect": "placement", "mode": "owned", "owner_id": str(merchant)})
    assert [v.violation_type for v in item_position_violations([state], [claim], TEXT)] == ["canon_conflict"]


def test_the_holders_surface_is_still_compatible():
    tarin, coin = uuid4(), uuid4()
    text = "Серебряк лежит на ладони."
    state = ItemStateSnapshot(item_id=coin, name="Серебряк", owner_id=tarin, placement_detail="на ладони")
    claim = ItemPositionClaim.model_validate({"item_id": str(coin), "evidence": text, "scope": "current",
                                              "mode": "located", "placement_matches": True})
    assert item_position_violations([state], [claim], text) == []
