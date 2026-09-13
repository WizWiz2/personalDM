from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.player_intent import PlayerActionIntent, PlayerIntentContract


def test_movement_intent_requires_explicit_destination() -> None:
    with pytest.raises(ValidationError):
        PlayerActionIntent(
            action_type="movement",
            intent="Выхожу из комнаты.",
        )


def test_nonmovement_cannot_smuggle_route_authority() -> None:
    with pytest.raises(ValidationError):
        PlayerActionIntent(
            action_type="interaction",
            intent="Подхожу к стойке.",
            destination_location="Контора",
        )


def test_dialogue_can_request_response_without_executable_action() -> None:
    contract = PlayerIntentContract(
        summary="Кай спрашивает Мартина о складе.",
        actions=[],
        addressed_response_requested=True,
        addressed_character_name="Мартин Вэнс",
    )

    assert contract.actions == []
    assert contract.addressed_response_requested is True
    assert contract.addressed_character_name == "Мартин Вэнс"


def test_give_intent_requires_recipient_identity() -> None:
    with pytest.raises(ValidationError):
        PlayerActionIntent(
            action_type="inventory",
            intent="Возвращаю ключ.",
            item_id="00000000-0000-4000-8000-000000000001",
            inventory_operation="give",
        )
