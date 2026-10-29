"""Typed ActionStepPlan contracts: invalid combinations are refused, the valid twin is accepted."""
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.services.turn_planner import ActionStepPlan, NarrationPolicy, SceneTransitionPlan

ITEM = uuid4()
NPC = uuid4()


def _step(**fields) -> dict:
    return {"action_type": "interaction", "intent": "действие", "resolution": "auto_success", **fields}


INVALID = [
    pytest.param(_step(resolution="blocked"), id="blocked-needs-reason"),
    pytest.param(_step(blocking_reason="заперто"), id="auto-success-has-no-blocking-reason"),
    pytest.param(_step(action_type="inventory", item_id=ITEM), id="completed-inventory-needs-operation"),
    pytest.param(_step(action_type="inventory", inventory_operation="take"), id="completed-inventory-needs-item"),
    pytest.param(_step(item_id=ITEM, inventory_operation="reposition"), id="reposition-needs-exact-place"),
    pytest.param(_step(item_id=ITEM, inventory_operation="give"), id="give-needs-recipient"),
    pytest.param(_step(item_id=ITEM, inventory_operation="receive"), id="receive-needs-owner"),
]

VALID = [
    pytest.param(_step(resolution="blocked", blocking_reason="заперто"), id="blocked-with-reason"),
    pytest.param(_step(item_id=ITEM, inventory_operation="take"), id="take"),
    pytest.param(_step(item_id=ITEM, inventory_operation="reposition", item_placement_detail="на полке"), id="reposition"),
    pytest.param(_step(item_id=ITEM, inventory_operation="reposition", resolution="requires_choice"), id="reposition-choice"),
    pytest.param(_step(item_id=ITEM, inventory_operation="give", inventory_target_id=NPC), id="give"),
    pytest.param(_step(item_id=ITEM, inventory_operation="receive", inventory_target_id=NPC), id="receive"),
    pytest.param(_step(action_type="inventory", resolution="requires_choice"), id="inventory-choice-without-item"),
]


@pytest.mark.parametrize("data", INVALID)
def test_invalid_step_combination_is_refused(data):
    with pytest.raises(ValidationError):
        ActionStepPlan.model_validate(data)


@pytest.mark.parametrize("data", VALID)
def test_valid_step_combination_is_accepted(data):
    step = ActionStepPlan.model_validate(data)
    assert step.resolution == data["resolution"]


def test_typed_inventory_fields_make_the_step_an_inventory_step():
    step = ActionStepPlan.model_validate(_step(item_id=ITEM, inventory_operation="take"))
    assert step.action_type == "inventory"
    assert ActionStepPlan.model_validate(_step()).action_type == "interaction"


@pytest.mark.parametrize("resolution", ["blocked", "requires_choice"])
def test_safe_mundane_never_survives_a_non_success_resolution(resolution):
    step = ActionStepPlan.model_validate(_step(resolution=resolution, safe_mundane=True, blocking_reason="нет"))
    assert step.safe_mundane is False


def test_safe_mundane_is_kept_on_auto_success():
    assert ActionStepPlan.model_validate(_step(safe_mundane=True)).safe_mundane is True


def test_placement_detail_without_operation_is_refused():
    with pytest.raises(ValidationError):
        ActionStepPlan.model_validate(_step(item_placement_detail="на полке", action_type="interaction"))


def test_scene_transition_contract():
    assert SceneTransitionPlan(required=False, transition_type="location_transition").transition_type == "none"
    with pytest.raises(ValidationError):
        SceneTransitionPlan(required=True, transition_type="none")
    with pytest.raises(ValidationError):
        SceneTransitionPlan(required=True, transition_type="location_transition")
    ok = SceneTransitionPlan(required=True, transition_type="location_transition", destination_location="Пристань")
    assert ok.destination_location == "Пристань"


def test_complication_requires_an_established_source():
    with pytest.raises(ValidationError):
        NarrationPolicy(allow_new_complication=True)
    assert NarrationPolicy(allow_new_complication=False, complication_source="x").complication_source is None
    assert NarrationPolicy(allow_new_complication=True, complication_source="x").complication_source == "x"
