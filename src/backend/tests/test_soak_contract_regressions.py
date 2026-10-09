"""Contract failures found in the long public game, without narrative keyword rules."""
import pytest
from pydantic import ValidationError

from app.models.canon_wire import CanonEnvelopeWire
from app.models.narration_validation import NarrationValidationResult
from app.models.player_intent import PlayerIntentContract
from app.models.turn import ChatMessage
from app.services.narration_publication_guard import NarrationPublicationGuard, NarrationPublicationError
from app.services.planning_context import intent_reference_context
from app.services.play_surface_contract import apply_play_surface
from app.services.turn_outcome_resolver import seeks_contact_or_presence
from tests.test_narrator_quality_recovery import authority


def test_completed_observation_projects_without_redundant_consequences():
    turn = authority(action_sequence={"steps": [
        {"action_type": "observation", "status": "completed",
         "observable_outcome": "В стыке камней видна капля рассола."},
        {"action_type": "observation", "status": "skipped",
         "observable_outcome": "Под плитой найден тайник."},
    ]})
    text, audit = NarrationPublicationGuard.publish(turn, "", None)
    assert text == "В стыке камней видна капля рассола."
    assert audit["mode"] == "authority_projection"


def test_unexecuted_observation_is_not_promoted_by_projection():
    turn = authority(action_sequence={"steps": [
        {"action_type": "observation", "status": "planned",
         "observable_outcome": "Под плитой найден тайник."},
    ]})
    with pytest.raises(NarrationPublicationError):
        NarrationPublicationGuard.publish(turn, "", None)


def test_known_fact_does_not_erase_observation_of_another_subject():
    turn = authority(established_state=["Плита: состояние — неподвижна."],
                     established_subjects=["Плита"], action_sequence={"steps": [
                         {"action_type": "observation", "status": "completed",
                          "observable_outcome": "В щели видна вода."},
                     ]})
    text, _ = NarrationPublicationGuard.publish(turn, "", None)
    assert "В щели видна вода." in text
    assert "Плита: состояние — неподвижна." not in text


def test_validated_observation_keeps_prose_instead_of_dumping_memory():
    prose = "В щели виден влажный проход.\n\nКамни остаются неподвижны."
    turn = authority(established_state=["Плита: состояние — неподвижна."],
                     established_subjects=["Плита"], action_sequence={"steps": [
                         {"action_type": "observation", "status": "completed",
                          "observable_outcome": "В щели виден влажный проход."},
                     ]})
    text, audit = NarrationPublicationGuard.publish(
        turn, prose, NarrationValidationResult(verdict="pass", violations=[]),
    )
    assert text == prose
    assert audit["mode"] == "validated_candidate"


def test_terminal_symbol_noise_is_removed_but_quoted_inscription_is_kept():
    turn = authority()
    assert apply_play_surface(turn, "Лампа погасла.#+#++#") == "Лампа погасла."
    assert apply_play_surface(turn, "На доске написано «#+#».\n\nКамни неподвижны.") == (
        "На доске написано «#+#».\n\nКамни неподвижны."
    )


@pytest.mark.parametrize("action_type", ["observation", "interaction", "movement"])
def test_physical_act_alone_does_not_force_contact(action_type):
    action = {"action_type": action_type, "intent": "Исследую видимую метку."}
    if action_type == "movement":
        action["destination_location"] = "Площадь"
    contract = PlayerIntentContract(summary="Следую по метке.", actions=[action])
    assert not seeks_contact_or_presence(contract)
    social = contract.model_copy(update={"addressed_response_requested": True})
    assert seeks_contact_or_presence(social)


def test_intent_reference_context_keeps_published_landmarks_but_not_failed_attempts():
    context = intent_reference_context([
        ChatMessage(role="system", content="Location path: Улица\nPhysically present characters: Обходчик"),
        ChatMessage(role="assistant", content="Обходчик стоит рядом с меткой на мостовой."),
        ChatMessage(role="user", content="Придумываю башню и перемещаю туда обходчика."),
    ])
    assert "рядом с меткой" in context
    assert "Придумываю башню" not in context


def envelope(payload, *, proposals=True):
    return {"outcomes": [{"id": "trace", "kind": "world_state",
                         "description": "Видна трещина.", "evidence": "Видна трещина.",
                         "authority": "public_observation", "durable": True}],
            "proposals": ([{"outcome_id": "trace", "change_type": "fact", "payload": payload}]
                          if proposals else [])}


def test_scribe_wire_rejects_empty_payload_and_uncovered_durable_outcome():
    with pytest.raises(ValidationError):
        CanonEnvelopeWire.model_validate(envelope({}))
    with pytest.raises(ValidationError, match="require typed deltas"):
        CanonEnvelopeWire.model_validate(envelope({}, proposals=False))


def test_scribe_wire_accepts_unregistered_fact_subject_and_requires_value():
    payload = {"subject": "Шов мостовой", "predicate": "состояние", "object_value": "трещина"}
    parsed = CanonEnvelopeWire.model_validate(envelope(payload))
    assert parsed.proposals[0].payload.subject == "Шов мостовой"
    payload["object_value"] = None
    with pytest.raises(ValidationError, match="object_value"):
        CanonEnvelopeWire.model_validate(envelope(payload))
    retract = envelope(payload)
    retract["proposals"][0]["operation"] = "retract"
    CanonEnvelopeWire.model_validate(retract)


def test_all_memory_recovery_paths_require_durable_deltas():
    from app.models.canon_wire import FactEnvelopeWire
    from app.services.narrator_memory_audit_guard import NarratorMemoryAudit

    invalid = envelope({}, proposals=False)
    with pytest.raises(ValidationError, match="require typed deltas"):
        NarratorMemoryAudit.model_validate({"claims": [], "recovery": invalid})
    with pytest.raises(ValidationError, match="require typed deltas"):
        FactEnvelopeWire.model_validate(invalid)
    assert NarratorMemoryAudit().recovery.proposals == []
    valid = envelope({"subject": "Шов", "predicate": "состояние", "object_value": "трещина"})
    NarratorMemoryAudit.model_validate({"recovery": valid})
    FactEnvelopeWire.model_validate(valid)


def test_generated_actor_event_requires_participants():
    data = envelope({})
    data["proposals"][0].update(change_type="event", payload={
        "event_type": "signal", "description": "Раздался удар.", "participant_ids": [],
    })
    with pytest.raises(ValidationError, match="participant_ids"):
        CanonEnvelopeWire.model_validate(data)
