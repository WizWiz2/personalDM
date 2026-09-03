import pytest
from pydantic import ValidationError

from app.services.turn_planner import ActionStepPlan


def test_completed_inventory_step_requires_typed_target_and_operation():
    with pytest.raises(ValidationError):
        ActionStepPlan(
            action_type="inventory",
            intent="Забрать жетон",
            resolution="auto_success",
            observable_outcome="Жетон у персонажа",
        )


def test_inventory_step_keeps_exact_entity_reference():
    item_id = "11111111-1111-4111-8111-111111111111"
    step = ActionStepPlan(
        action_type="inventory",
        intent="Убрать жетон",
        resolution="auto_success",
        observable_outcome="Жетон оказывается у персонажа",
        item_id=item_id,
        inventory_operation="take",
    )
    assert str(step.item_id) == item_id
    assert step.inventory_operation == "take"


def test_typed_inventory_fields_normalize_interaction_to_inventory():
    item_id = "11111111-1111-4111-8111-111111111111"
    target_id = "22222222-2222-4222-8222-222222222222"
    step = ActionStepPlan(
        action_type="interaction",
        intent="Передать жетон Мартину",
        resolution="auto_success",
        safe_mundane=True,
        observable_outcome="Мартин получил жетон",
        item_id=item_id,
        inventory_operation="give",
        inventory_target_id=target_id,
    )

    assert step.action_type == "inventory"
    assert str(step.item_id) == item_id
    assert step.inventory_operation == "give"
    assert str(step.inventory_target_id) == target_id


def test_partial_typed_inventory_payload_cannot_hide_as_interaction():
    with pytest.raises(ValidationError):
        ActionStepPlan(
            action_type="interaction",
            intent="Передать жетон",
            resolution="auto_success",
            item_id="11111111-1111-4111-8111-111111111111",
        )
