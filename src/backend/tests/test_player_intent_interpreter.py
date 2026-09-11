from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.models.player_intent import PlayerIntentContract, PlayerIntentReview
from app.models.turn import ChatMessage
from app.services.player_intent_interpreter import PlayerIntentInterpreter


class _Router:
    def __init__(self, reviews: list[str]):
        self.reviews = list(reviews)
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
        if response_model is PlayerIntentContract:
            return {
                "summary": "Кай выходит в коридор.",
                "actions": [
                    {
                        "action_type": "movement",
                        "intent": "Выйти в коридор.",
                        "destination_location": "Коридор",
                    }
                ],
            }
        if response_model is PlayerIntentReview:
            verdict = self.reviews.pop(0)
            return {
                "verdict": verdict,
                "issues": ([] if verdict == "pass" else ["Нужна корректировка IR."]),
                "summary": "",
            }
        raise AssertionError(response_model)


@pytest.mark.asyncio
async def test_healthy_intent_path_has_exactly_two_control_calls() -> None:
    router = _Router(["pass"])
    interpreter = PlayerIntentInterpreter(router)

    result = await interpreter.interpret(
        SimpleNamespace(),
        [ChatMessage(role="system", content="AUTHORITATIVE STATE")],
        "Я выхожу в коридор.",
    )

    assert result.actions[0].destination_location == "Коридор"
    assert router.calls == ["PlayerIntentContract", "PlayerIntentReview"]


@pytest.mark.asyncio
async def test_repair_path_is_hard_bounded_to_one_repair_and_final_review() -> None:
    router = _Router(["repair_required", "pass"])
    interpreter = PlayerIntentInterpreter(router)

    await interpreter.interpret(
        SimpleNamespace(),
        [ChatMessage(role="system", content="AUTHORITATIVE STATE")],
        "Я выхожу в коридор.",
    )

    assert router.calls == [
        "PlayerIntentContract",
        "PlayerIntentReview",
        "PlayerIntentContract",
        "PlayerIntentReview",
    ]
