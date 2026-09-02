from uuid import uuid4

import pytest

from app.models.narration_validation import NarrationValidationResult, NarrationViolation
from app.models.turn_authority import TurnAuthority
from app.services.narration_publication_guard import (
    NarrationPublicationError,
    NarrationPublicationGuard,
)


def _authority(*, actor_turn: bool = False) -> TurnAuthority:
    actor_id = uuid4() if actor_turn else None
    return TurnAuthority(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_character_id=uuid4(),
        player_character_name="Виктор Соколов",
        acting_character_id=actor_id,
        acting_character_name="Ирина" if actor_turn else None,
        player_input="Ирина, расскажите подробнее.",
        scene_disposition="actor_turn" if actor_turn else "stay",
        present_character_names=["Виктор Соколов", "Ирина"],
        observable_consequences=[],
        ending_hook="",
    )


def _error(kind: str, evidence: str) -> NarrationValidationResult:
    return NarrationValidationResult(
        verdict="repair_required",
        summary="candidate violates authority",
        violations=[
            NarrationViolation(
                violation_type=kind,
                severity="error",
                evidence=evidence,
                correction="remove it",
            )
        ],
    )


def test_actor_surgical_repair_keeps_npc_speech_after_player_agency_cut():
    rejected = (
        "Ирина отвечает: «Его зовут Иван Петров». "
        "Виктор Соколов кивает и говорит: «Хорошо, это только начало»."
    )

    repaired, audit = NarrationPublicationGuard.surgical_repair_candidate(
        rejected,
        _error("player_agency", "Виктор Соколов кивает и говорит: «Хорошо, это только начало»."),
    )

    assert repaired is not None
    assert "Иван Петров" in repaired
    assert "Хорошо" not in repaired
    assert "кивает" not in repaired
    assert audit["strategy"] == "deterministic_span_removal"
    assert audit["status"] == "candidate"


def test_actor_projection_does_not_publish_player_choice_hook():
    authority = _authority(actor_turn=True).model_copy(
        update={
            "player_character_name": "Вера",
            "acting_character_name": "Хозяин трактира",
            "ending_hook": "Вера решает принять вызов или отказаться.",
        }
    )

    published, audit = NarrationPublicationGuard.publish(authority, "", None)

    assert "решает" not in published
    assert "вызов" not in published
    assert audit["mode"] == "authority_projection"
    assert "Хозяин" in published


def test_round26_unauthorized_npc_cannot_be_replaced_with_empty_safe_fallback():
    authority = _authority(actor_turn=False)
    rejected = "Незнакомец в тёмном плаще входит в офис и закрывает дверь."

    with pytest.raises(NarrationPublicationError):
        NarrationPublicationGuard.publish(
            authority,
            rejected,
            _error("other", "Незнакомец в тёмном плаще входит в офис"),
        )


def test_unvalidated_candidate_is_not_replaced_with_empty_canonical_surface():
    authority = _authority(actor_turn=False)
    rejected = "Незнакомец входит в офис."

    with pytest.raises(NarrationPublicationError):
        NarrationPublicationGuard.publish(authority, rejected, None)


def test_validated_candidate_remains_publishable():
    authority = _authority(actor_turn=True)
    candidate = "Ирина отвечает: «Его зовут Иван Петров»."
    passed = NarrationValidationResult(verdict="pass", summary="ok", violations=[])

    published, audit = NarrationPublicationGuard.publish(authority, candidate, passed)

    assert published == candidate
    assert audit["mode"] == "validated_candidate"
    assert audit["validated_surface"] is True


def test_texture_violation_produces_surgical_repair_candidate():
    extra = "В углу сидел один из редких посетителей и ждал чего-то конкретного."
    draft = (
        "Сырой воздух просачивался сквозь щели в досках «Якоря». "
        "За стойкой стоял хозяин трактира и протирал дерево тряпкой. "
        f"{extra} "
        "На стене висело объявление о работе."
    )

    repaired, audit = NarrationPublicationGuard.surgical_repair_candidate(
        draft,
        _error("ungrounded_complication", extra),
    )

    assert repaired is not None
    assert extra not in repaired
    assert "хозяин трактира" in repaired
    assert "объявление о работе" in repaired
    assert audit["status"] == "candidate"


def test_engine_exception_is_not_a_player_facing_reply():
    leaked = (
        "location_transition resolved to the current physical location; "
        "use stay/focus_transition instead of claiming physical travel."
    )
    authority = _authority(actor_turn=False)
    authority = authority.model_copy(
        update={
            "player_character_name": "Вера",
            "player_input": "Возвращаюсь туда, откуда только что пришла.",
            "scene_disposition": "sequence",
            "action_sequence": {
                "steps": [
                    {
                        "action_type": "movement",
                        "status": "blocked",
                        "blocking_reason": leaked,
                        "observable_outcome": "",
                    }
                ]
            },
        }
    )

    published, audit = NarrationPublicationGuard.publish(authority, leaked, None)

    assert "location_transition" not in published
    assert "focus_transition" not in published
    assert TurnAuthority._player_facing_blocking_reason(leaked) == (
        "Ты остаёшься там, где уже стоишь."
    )
    assert audit["mode"] == "authority_projection"
    assert "остаёшься" in published
