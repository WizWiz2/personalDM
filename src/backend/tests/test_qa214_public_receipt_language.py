"""F-40: internal machine exceptions do not become human-readable canon."""
from app.db.action_sequence_table import ActionStep
from app.services.truth_engine_receipts import StructuredReceiptEventCompiler


def test_blocked_item_receipt_uses_localized_public_surface():
    step = ActionStep(
        sequence_id="test-sequence", step_index=0, action_type="inventory",
        intent="Ставлю монету под каблук.", resolution="blocked",
        status="blocked", blocking_reason="Cannot reposition item outside current location",
    )
    assert StructuredReceiptEventCompiler._observed_step_outcome(step) == "Действие заблокировано."
    step.public_blocking_reason = "Монета находится в другой локации."
    assert StructuredReceiptEventCompiler._observed_step_outcome(step) == "Монета находится в другой локации."
