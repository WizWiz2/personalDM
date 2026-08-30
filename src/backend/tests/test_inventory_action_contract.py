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
