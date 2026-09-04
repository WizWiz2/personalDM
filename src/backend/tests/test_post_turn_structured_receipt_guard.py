import json
from types import SimpleNamespace
from uuid import uuid4

from app.services.post_turn_structured_receipt_guard import (
    RelationshipReceiptDecision,
    _executed_steps,
    _player_id,
)


def _assistant_with_authority(*, player_id, steps):
    return SimpleNamespace(
        context_snapshot=json.dumps(
            {
                "turn_authority": {
                    "player_character_id": str(player_id),
                    "action_sequence": {
                        "status": "applied",
                        "steps": steps,
                    },
                }
            }
        )
    )


def test_completed_typed_receipts_are_read_from_authority_snapshot():
    player_id = uuid4()
    target_scene_id = uuid4()
    assistant = _assistant_with_authority(
        player_id=player_id,
        steps=[
            {
                "step_index": 0,
                "action_type": "movement",
                "status": "completed",
                "target_scene_id": str(target_scene_id),
                "observable_outcome": "Кай вышел в коридор.",
            }
        ],
    )
    assert _player_id(assistant) == player_id
    steps = _executed_steps(assistant)
    assert len(steps) == 1
    assert steps[0]["target_scene_id"] == str(target_scene_id)


def test_relationship_reconciler_defaults_to_no_change():
    decision = RelationshipReceiptDecision()
    assert decision.verdict == "no_change"
    assert decision.retract_ids == []


def test_relationship_reconciler_accepts_only_typed_verdicts():
    relationship_id = uuid4()
    decision = RelationshipReceiptDecision(
        verdict="retract",
        retract_ids=[relationship_id],
        reason="Структурированная передача выполнила явное условие долга.",
    )
    assert decision.retract_ids == [relationship_id]
