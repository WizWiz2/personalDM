from uuid import uuid4

from app.models.narration_validation import NarrationValidationResult, NarrationViolation
from app.models.turn_authority import TurnAuthority
from app.services.narration_publication_guard import NarrationPublicationGuard


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


def test_round26_actor_fallback_discards_entire_rejected_candidate():
    authority = _authority(actor_turn=True)
    rejected = (
        "Ирина отвечает: «Его зовут Иван Петров». "
        "Виктор Соколов кивает и говорит: «Хорошо, это только начало»."
    )

    published, audit = NarrationPublicationGuard.publish(
        authority,
        rejected,
        _error("player_agency", "Виктор Соколов кивает и говорит"),
    )

    assert "Иван Петров" not in published
    assert "Хорошо" not in published
    assert "Виктор" not in published
    assert audit["mode"] == "authority_projection"
    assert audit["candidate_discarded"] is True


def test_round26_unauthorized_npc_cannot_survive_safe_fallback():
    authority = _authority(actor_turn=False)
    rejected = "Незнакомец в тёмном плаще входит в офис и закрывает дверь."

    published, audit = NarrationPublicationGuard.publish(
        authority,
        rejected,
        _error("other", "Незнакомец в тёмном плаще входит в офис"),
    )

    assert "Незнакомец" not in published
    assert "плащ" not in published
    assert audit["mode"] == "authority_projection"
    assert audit["validated_surface"] is False


def test_unvalidated_candidate_is_not_a_canonical_surface():
    authority = _authority(actor_turn=False)
    rejected = "Незнакомец входит в офис."

    published, audit = NarrationPublicationGuard.publish(authority, rejected, None)

    assert rejected not in published
    assert "Незнакомец" not in published
    assert audit["candidate_discarded"] is True


def test_validated_candidate_remains_publishable():
    authority = _authority(actor_turn=True)
    candidate = "Ирина отвечает: «Его зовут Иван Петров»."
    passed = NarrationValidationResult(verdict="pass", summary="ok", violations=[])

    published, audit = NarrationPublicationGuard.publish(authority, candidate, passed)

    assert published == candidate
    assert audit["mode"] == "validated_candidate"
    assert audit["validated_surface"] is True


def test_texture_violation_publishes_surgical_remainder_instead_of_planner_skeleton():
    authority = _authority(actor_turn=False)
    extra = "В углу сидел один из редких посетителей и ждал чего-то конкретного."
    draft = (
        "Сырой воздух просачивался сквозь щели в досках «Якоря». "
        "За стойкой стоял хозяин трактира и протирал дерево тряпкой. "
        f"{extra} "
        "На стене висело объявление о работе."
    )

    published, audit = NarrationPublicationGuard.publish(
        authority,
        draft,
        _error("ungrounded_complication", extra),
    )

    assert extra not in published
    assert "хозяин трактира" in published
    assert "объявление о работе" in published
    assert audit["mode"] == "surgical_surface"
    assert audit["candidate_discarded"] is False


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
