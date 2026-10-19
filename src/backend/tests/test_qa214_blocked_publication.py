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
