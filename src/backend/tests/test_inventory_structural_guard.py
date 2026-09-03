from uuid import uuid4

from app.models.turn import ChatMessage
from app.services.systemless_authority_guard import structured_inventory_contract_issues
from app.services.turn_authority_planner import CoordinatedTurnPlan
from app.services.turn_planner import ActionSequencePlan, ActionStepPlan


def _context(*, owned_id=None, object_id=None, character_id=None):
    owned = f"латунный ключ [id={owned_id}]" if owned_id else "none recorded"
    objects = f"латунный ключ [id={object_id}]" if object_id else "none recorded"
    characters = f"Кай [id={character_id}]" if character_id else "none recorded"
    return [
        ChatMessage(
            role="system",
            content=(
                "[AUTHORITATIVE SCENE STATE]\n"
                f"Objects physically here: {objects}\n"
                "[STRUCTURED ACTION REFERENCES]\n"
                f"Player-owned items: {owned}\n"
                f"Physically present characters: {characters}\n"
                "Planner inventory contract:\n"
                "- take uses an object physically here; drop/place/give uses a player-owned item.\n"
            ),
        )
    ]


def _inventory_plan(item_id, operation, *, target_id=None):
    return CoordinatedTurnPlan(
        player_intent="Переместить ключ.",
        resolution="sequence",
        action_sequence=ActionSequencePlan(
            steps=[
                ActionStepPlan(
                    action_type="inventory",
                    intent="Переместить латунный ключ",
                    resolution="auto_success",
                    observable_outcome="Ключ перемещён.",
                    item_id=item_id,
                    inventory_operation=operation,
                    inventory_target_id=target_id,
                )
            ]
        ),
    )


def test_owned_item_cannot_be_planned_as_take():
    item_id = uuid4()
    player_id = uuid4()
    plan = _inventory_plan(item_id, "take")

    issues = structured_inventory_contract_issues(
        plan,
        _context(owned_id=item_id, character_id=player_id),
    )

    assert len(issues) == 1
    assert "already player-owned" in issues[0]
    assert str(item_id) in issues[0]


def test_owned_item_can_be_planned_as_place():
    item_id = uuid4()
    player_id = uuid4()
    plan = _inventory_plan(item_id, "place")

    assert (
        structured_inventory_contract_issues(
            plan,
            _context(owned_id=item_id, character_id=player_id),
        )
        == []
    )


def test_take_requires_item_to_be_physically_present():
    item_id = uuid4()
    plan = _inventory_plan(item_id, "take")

    assert (
        structured_inventory_contract_issues(
            plan,
            _context(object_id=item_id),
        )
        == []
    )


def test_give_requires_physically_present_target():
    item_id = uuid4()
    absent_target_id = uuid4()
    plan = _inventory_plan(item_id, "give", target_id=absent_target_id)

    issues = structured_inventory_contract_issues(
        plan,
        _context(owned_id=item_id),
    )

    assert len(issues) == 1
    assert "not a physically present character" in issues[0]
