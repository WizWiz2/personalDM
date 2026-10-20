"""QA-213 F-33: a false model-validated item hand-off must not be published."""
from uuid import uuid4

from app.models.narration_validation import NarrationValidationResult
from app.models.turn_authority import TurnAuthority
from app.services.narration_publication_guard import NarrationPublicationGuard


def test_validator_pass_cannot_override_blocked_inventory_receipt():
    authority = TurnAuthority(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_input="Передаю письмо дозорному.",
        action_sequence={
            "steps": [
                {"action_type": "inventory", "status": "blocked",
                 "intent": "Передать письмо дозорному",
                 "public_blocking_reason": "Передача не состоялась: дозорного нет рядом."}
            ],
        },
    )
    candidate = "Дозорный берёт письмо и убирает его в карман."
    published, metadata = NarrationPublicationGuard.publish(
        authority, candidate, NarrationValidationResult(verdict="pass"),
    )
    assert published == "Передача не состоялась: дозорного нет рядом."
    assert "берёт письмо" not in published
    assert metadata["candidate_discarded"] is True
    assert metadata["mode"] == "authority_projection"


def test_noninventory_validated_scene_remains_prose():
    authority = TurnAuthority(
        campaign_id=uuid4(), trigger_turn_id=uuid4(),
        player_input="Смотрю на дождь.",
        observable_consequences=["За окном идёт дождь."],
    )
    candidate = "Дождь мерно стучит по черепице."
    published, metadata = NarrationPublicationGuard.publish(
        authority, candidate, NarrationValidationResult(verdict="pass"),
    )
    assert published == candidate
    assert metadata["mode"] == "validated_candidate"


def test_false_unreceipted_npc_coin_transfer_is_not_published():
    authority = TurnAuthority(
        campaign_id=uuid4(), trigger_turn_id=uuid4(),
        player_input="Спрашиваю Зеню о монете.",
        object_names=["Монета"],
        observable_consequences=["Зеня отказывается отвечать о монете."],
        action_sequence={"steps": []},
    )
    candidate = "Зеня убрал монету в карман."
    published, metadata = NarrationPublicationGuard.publish(
        authority, candidate, NarrationValidationResult(verdict="pass"),
    )
    assert published == "Зеня отказывается отвечать о монете."
    assert metadata["candidate_discarded"] is True


def test_static_description_of_item_without_transfer_is_allowed():
    authority = TurnAuthority(
        campaign_id=uuid4(), trigger_turn_id=uuid4(),
        player_input="Смотрю на монету.", object_names=["Монета"],
        observable_consequences=["Монета лежит на столе."],
    )
    text = "Монета лежит на столе, рядом с пыльной лампой."
    published, metadata = NarrationPublicationGuard.publish(
        authority, text, NarrationValidationResult(verdict="pass"),
    )
    assert published == text
    assert metadata["mode"] == "validated_candidate"
