"""QA-213 F-33: a successful prose validator cannot authorize a blocked item transfer."""
from uuid import uuid4

from app.models.narration_validation import NarrationValidationResult
from app.models.turn_authority import TurnAuthority
from app.services.narration_publication_guard import NarrationPublicationGuard


def test_blocked_inventory_never_publishes_false_success():
    authority = TurnAuthority(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_input="Передаю письмо отсутствующему дозорному.",
        observable_consequences=["Передача письма не состоялась."],
        action_sequence={"steps": [{
            "action_type": "inventory",
            "status": "blocked",
            "observable_outcome": "Письмо передано дозорному.",
            "public_blocking_reason": "Дозорного нет рядом. Передача не состоялась.",
        }]},
    )
    published, audit = NarrationPublicationGuard.publish(
        authority,
        "Дозорный берёт письмо и отмечает передачу в книге.",
        NarrationValidationResult(verdict="pass"),
    )
    assert "Дозорный берёт" not in published
    assert "Передача не состоялась" in published
    assert audit["mode"] == "authority_projection"
    assert audit["blocked_inventory_override"] is True
