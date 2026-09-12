from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.turn import ChatMessage
from app.services.player_intent_interpreter import (
    PlayerIntentContractDraft,
    PlayerIntentInterpreter,
    normalize_intent_draft,
)
from app.services.turn_planner import TurnPlanningError


class _Router:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[str] = []

    async def generate_json(
        self,
        provider,
        selection,
        messages,
        *,
        response_model,
        **kwargs,
    ):
        del provider, selection, messages, kwargs
        self.calls.append(response_model.__name__)
        assert response_model is PlayerIntentContractDraft
        return self.payload


@pytest.mark.asyncio
async def test_healthy_intent_path_uses_one_semantic_control_call() -> None:
    router = _Router(
        {
            "summary": "Кай выходит в коридор.",
            "actions": [
                {
                    "action_type": "movement",
                    "intent": "Выйти в коридор.",
                    "destination_location": "Коридор",
                }
            ],
        }
    )
    interpreter = PlayerIntentInterpreter(router)

    result = await interpreter.interpret(
        SimpleNamespace(),
        [ChatMessage(role="system", content="AUTHORITATIVE STATE")],
        "Я выхожу в коридор.",
    )

    assert result.actions[0].destination_location == "Коридор"
    assert router.calls == ["PlayerIntentContractDraft"]
    assert interpreter.audit[0]["phase"] == "single_pass"
    assert interpreter.audit[0]["normalization"] == "deterministic"


def test_inventory_verb_can_recover_mislabelled_model_action() -> None:
    item_id = uuid4()
    draft = PlayerIntentContractDraft.model_validate(
        {
            "summary": "Кай кладёт ключ на пол.",
            "actions": [
                {
                    "action_type": "movement",
                    "intent": "Кладу латунный ключ на пол.",
                    "item_id": str(item_id),
                }
            ],
        }
    )

    result = normalize_intent_draft(draft, "Кладу латунный ключ на пол.")

    action = result.actions[0]
    assert action.action_type == "inventory"
    assert action.item_id == item_id
    assert action.inventory_operation == "drop"
    assert action.destination_location is None


def test_irrelevant_inventory_fields_are_removed_from_non_inventory_action() -> None:
    draft = PlayerIntentContractDraft.model_validate(
        {
            "summary": "Кай нажимает кнопку.",
            "actions": [
                {
                    "action_type": "interaction",
                    "intent": "Нажать кнопку.",
                    "item_id": str(uuid4()),
                }
            ],
        }
    )

    action = normalize_intent_draft(draft, "Нажимаю кнопку.").actions[0]

    assert action.action_type == "interaction"
    assert action.item_id is None
    assert action.inventory_operation is None
    assert action.inventory_target_id is None


def test_give_operation_is_recovered_from_explicit_player_language() -> None:
    item_id = uuid4()
    target_id = uuid4()
    draft = PlayerIntentContractDraft.model_validate(
        {
            "summary": "Кай отдаёт ключ Мартину.",
            "actions": [
                {
                    "action_type": "inventory",
                    "intent": "Отдаю латунный ключ Мартину.",
                    "item_id": str(item_id),
                    "inventory_target_id": str(target_id),
                }
            ],
        }
    )

    action = normalize_intent_draft(draft, "Отдаю латунный ключ Мартину.").actions[0]

    assert action.inventory_operation == "give"
    assert action.item_id == item_id
    assert action.inventory_target_id == target_id


def test_true_movement_without_destination_still_fails_closed() -> None:
    draft = PlayerIntentContractDraft.model_validate(
        {
            "summary": "Кай куда-то идёт.",
            "actions": [{"action_type": "movement", "intent": "Иду дальше."}],
        }
    )

    with pytest.raises(TurnPlanningError, match="missing the player-selected destination"):
        normalize_intent_draft(draft, "Иду дальше.")
