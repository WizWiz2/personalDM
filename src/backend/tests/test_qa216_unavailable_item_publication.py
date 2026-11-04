"""F-47: fluent prose cannot pretend the protagonist handles an unavailable item."""
from uuid import uuid4

from app.models.turn_authority import TurnAuthority
from app.models.narration_validation import NarrationValidationResult
from app.services.narration_publication_guard import NarrationPublicationGuard


def authority(**kwargs):
    return TurnAuthority(
        campaign_id=uuid4(), trigger_turn_id=uuid4(),
        player_input="Достаю пластину и показываю архивариусу.",
        unavailable_item_names=["медная пластина Оста"],
        observable_consequences=["Пластина недоступна: она у Алисы."],
        **kwargs,
    )




def test_valid_completed_receipt_for_same_item_is_allowed():
    item = "медная пластина Оста"
    auth = authority(action_sequence={"steps": [{
        "action_type": "inventory", "status": "completed",
        "item_name": item, "observable_outcome": "Пластина получена."
    }]})
    validation = NarrationValidationResult(verdict="pass")
    validation._item_states_verified = True
    result, meta = NarrationPublicationGuard.publish(
        auth, "Ты получаешь медную пластину Оста.",
        validation,
    )
    assert meta["mode"] == "validated_candidate"
    assert "пластину" in result




def test_ordinary_mention_without_claiming_possession_is_allowed():
    validation = NarrationValidationResult(verdict="pass")
    validation._item_states_verified = True
    _, meta = NarrationPublicationGuard.publish(
        authority(),
        "Алиса всё ещё хранит медную пластину Оста в конторе.",
        validation,
    )
    assert meta["mode"] == "validated_candidate"
