from uuid import uuid4

import pytest

from app.models.narration_validation import NarrationValidationResult
from app.models.turn_authority import TurnAuthority
from app.services.context_compiler import ContextCompiler
from app.services.narration_publication_guard import (
    NarrationPublicationError,
    NarrationPublicationGuard,
)
from app.services.turn_authority_validator import TurnAuthorityValidator


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
    assert payload["execution_section"] == "[EXECUTED ACTION SEQUENCE]"
    assert "Действие не выполнено" not in " ".join(payload["observable_consequences"])
    assert "Player destination" not in " ".join(payload["observable_consequences"])
    assert "Продвинуться дальше" not in " ".join(payload["observable_consequences"])


def test_safe_projection_never_exposes_meta_actor_waiting_message():
    turn = authority(
        scene_disposition="actor_turn",
        acting_character_id=uuid4(),
        acting_character_name="Лиза",
        observable_consequences=[],
    )

    text = NarrationPublicationGuard.render_authority(turn)

    assert text == "Лиза умолкает."
    assert "игрок" not in text.casefold()
    assert "ответ" not in text.casefold()


def test_safe_projection_drops_technical_consequences_and_fails_if_nothing_real_remains():
    internal_id = str(uuid4())
    turn = authority(
        observable_consequences=[
            f"target_scene_id={internal_id}",
            "Действие не выполнено: открыть дверь.",
        ],
        ending_hook="",
    )

    with pytest.raises(NarrationPublicationError):
        NarrationPublicationGuard.render_authority(turn)


def test_deterministic_surface_gate_handles_only_machine_provable_leaks():
    turn_id = str(uuid4())
    technical = TurnAuthorityValidator.apply_deterministic_surface_quality(
        passed(),
        f"Маршрут готов: target_scene_id={turn_id}.",
    )
    semantic_meta = TurnAuthorityValidator.apply_deterministic_surface_quality(
        passed(),
        "Лиза заканчивает ответ и ждёт дальнейших слов игрока.",
    )

    assert technical.verdict == "repair_required"
    assert any(item.severity == "error" for item in technical.violations)
    assert semantic_meta.verdict == "pass"
    assert "META LANGUAGE" in TurnAuthorityValidator.SYSTEM_PROMPT
    assert "waiting for" in TurnAuthorityValidator.SYSTEM_PROMPT
