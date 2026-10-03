from uuid import uuid4


from app.models.narration_validation import NarrationValidationResult
from app.models.turn_authority import TurnAuthority
from app.services.context_compiler import ContextCompiler


def authority(**updates) -> TurnAuthority:
    payload = {
        "campaign_id": uuid4(),
        "trigger_turn_id": uuid4(),
        "player_character_id": uuid4(),
        "player_character_name": "Рэт Уайтмоур",
        "player_input": "Я осматриваю дверь.",
        "scene_disposition": "stay",
        "observable_consequences": [],
        "ending_hook": "",
    }
    payload.update(updates)
    return TurnAuthority(**payload)


def passed() -> NarrationValidationResult:
    return NarrationValidationResult(verdict="pass", summary="ok", violations=[])


def test_general_narrator_contract_matches_player_facing_validator_rules():
    contract = ContextCompiler.NARRATOR_SURFACE_CONTRACT
    assert "second person" in contract
    assert "Russian" in contract
    assert "UUIDs" in contract
    assert "BLOCKED/SKIPPED" in contract
    assert "waiting for the player's next input" in contract


def test_blocked_sequence_does_not_put_engine_status_into_observable_consequence():
    turn = authority(
        scene_disposition="sequence",
        action_sequence={
            "steps": [
                {
                    "status": "blocked",
                    "intent": "Иду в закрытый подвал",
                    "public_blocking_reason": "Неясно, куда именно ведёт этот шаг; путь остаётся прежним.",
                    "blocking_reason": (
                        "Player destination is not authorized: ambiguous destination"
                    ),
                },
                {
                    "status": "skipped",
                    "intent": "Ложусь спать",
                },
            ]
        },
        observable_consequences=["Действие не выполнено: Иду в закрытый подвал."],
    )

    assert turn.observable_consequences == [
        "Неясно, куда именно ведёт этот шаг; путь остаётся прежним."
    ]
    payload = turn.narrator_payload()
    assert "Действие не выполнено" not in " ".join(payload["observable_consequences"])
    assert "Player destination" not in " ".join(payload["observable_consequences"])
    assert "Продвинуться дальше" not in " ".join(payload["observable_consequences"])


