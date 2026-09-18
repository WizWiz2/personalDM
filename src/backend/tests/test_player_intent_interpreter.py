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
    def __init__(self, payload: dict, bindings: dict | None = None):
        self.payload = payload
        self.bindings = bindings
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
        if response_model.__name__ == "DestinationIdentityBindings":
            return self.bindings
        assert issubclass(response_model, PlayerIntentContractDraft)
        return self.payload


@pytest.mark.asyncio
async def test_known_destination_identity_overrides_inflected_model_label():
    location_id = str(uuid4())
    router = _Router(
        {
            "summary": "Возвращаюсь домой.",
            "actions": [
                {
                    "action_type": "movement",
                    "intent": "Вернуться в свою комнату.",
                    "destination_location": "комната Кай",
                }
            ],
        },
        bindings={"action_0": location_id},
    )
    result = await PlayerIntentInterpreter(router).interpret(
        SimpleNamespace(),
        [],
        "Возвращаюсь в свою комнату.",
        location_references={location_id: "Комната Кая"},
    )
    assert result.actions[0].destination_location == "Комната Кая"
    assert router.calls == ["PlayerIntentContractDraft", "DestinationIdentityBindings"]


@pytest.mark.asyncio
async def test_unknown_location_identity_cannot_authorize_a_destination():
    router = _Router(
        {
            "summary": "Иду в комнату.",
            "actions": [
                {
                    "action_type": "movement",
                    "intent": "Иду в комнату.",
                    "destination_location": "Моя комната",
                }
            ],
        },
        bindings={"action_0": str(uuid4())},
    )
    with pytest.raises(TurnPlanningError, match="player intent interpretation failed"):
        await PlayerIntentInterpreter(router).interpret(
            SimpleNamespace(),
            [],
            "Иду в комнату.",
            location_references={str(uuid4()): "Комната"},
        )


@pytest.mark.asyncio
async def test_new_destination_binding_preserves_selected_endpoint_and_action():
    router = _Router(
        {
            "summary": "Иду в прачечную.",
            "actions": [
                {
                    "action_type": "movement",
                    "intent": "Иду в прачечную.",
                    "destination_location": "Прачечная соседнего дома",
                }
            ],
        },
        bindings={"action_0": "new"},
    )
    result = await PlayerIntentInterpreter(router).interpret(
        SimpleNamespace(),
        [],
        "Иду в прачечную соседнего дома.",
        location_references={str(uuid4()): "Контора"},
    )
    assert len(result.actions) == 1
    assert result.actions[0].destination_location == "Прачечная соседнего дома"
    assert result.actions[0].intent == "Иду в прачечную."
    assert router.calls == ["PlayerIntentContractDraft"]


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
        location_references={str(uuid4()): "Коридор"},
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


def test_plural_imperative_is_not_the_player_undressing() -> None:
    draft = PlayerIntentContractDraft.model_validate(
        {
            "summary": "Кай понимает, что ответ — да, и начинает раздеваться.",
            "actions": [
                {
                    "action_type": "other",
                    "intent": "начинаю раздеваться",
                }
            ],
        }
    )

    result = normalize_intent_draft(
        draft,
        "- Я так понимаю, ответ - да. Тогда раздевайтесь",
    )

    assert result.actions == []
    assert result.addressed_response_requested is True
    assert "раздевайтесь" in result.summary


def test_plural_remove_command_is_not_a_player_inventory_take() -> None:
    draft = PlayerIntentContractDraft.model_validate(
        {
            "summary": "Кай просит снять заметную деталь.",
            "actions": [
                {
                    "action_type": "inventory",
                    "intent": "снять заметную деталь",
                    "item_id": str(uuid4()),
                    "inventory_operation": "take",
                }
            ],
        }
    )

    result = normalize_intent_draft(draft, "-Хорошо, это тоже снимайте")

    assert result.actions == []
    assert result.addressed_response_requested is True


def test_player_own_undress_still_survives() -> None:
    draft = PlayerIntentContractDraft.model_validate(
        {
            "summary": "Кай раздевается.",
            "actions": [
                {
                    "action_type": "other",
                    "intent": "Я раздеваюсь.",
                }
            ],
        }
    )

    result = normalize_intent_draft(draft, "Я раздеваюсь.")

    assert len(result.actions) == 1


def test_description_request_does_not_spawn_an_npc() -> None:
    from types import SimpleNamespace
    from app.services.addressee_guard import scrub_uninvited_spawn

    plan = SimpleNamespace(
        npc_introductions=["Молодой, но влиятельный маг или аристократ"],
        observable_consequences=["Кай продолжает раздеваться."],
    )
    scrub_uninvited_spawn(
        plan,
        "Ты описываешь только лицо. Я хочу подробности касательно их тел.",
    )
    assert plan.npc_introductions == []
    assert plan.observable_consequences == []
