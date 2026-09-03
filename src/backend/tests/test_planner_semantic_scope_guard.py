from uuid import uuid4

from app.models.turn import ChatMessage
from app.services.planner_semantic_scope_guard import (
    GuardedMaterializedTurnOutcome,
    IdentityPromotionSnapshot,
    _SEMANTIC_SCOPE_CONTRACT,
    _unique_presence_keys,
)


def test_semantic_reviewer_is_not_a_literary_critic():
    assert "NOT a literary critic" in _SEMANTIC_SCOPE_CONTRACT
    assert "ostayus" not in _SEMANTIC_SCOPE_CONTRACT.casefold()
    assert "остаюсь на месте" in _SEMANTIC_SCOPE_CONTRACT
    assert "blocking_reason" in _SEMANTIC_SCOPE_CONTRACT
    assert "does NOT require observable_outcome" in _SEMANTIC_SCOPE_CONTRACT


def test_presence_keys_deduplicate_structured_reference_ids():
    entity_id = uuid4()
    messages = [
        ChatMessage(
            role="system",
            content=(
                "Physically present characters: Кай\n"
                "[STRUCTURED ACTION REFERENCES]\n"
                f"Physically present characters: Кай [id={entity_id}]\n"
            ),
        )
    ]
    assert _unique_presence_keys(messages) == {"kay"}


def test_identity_promotion_counts_as_materialized_change():
    snapshot = IdentityPromotionSnapshot(
        entity_id=uuid4(),
        canonical_name="Дежурный",
        aliases=(),
        custom_fields={"temporary_name": True, "role": "дежурный"},
    )
    outcome = GuardedMaterializedTurnOutcome(identity_promotions=(snapshot,))
    assert outcome.has_changes
    assert outcome.arrived_existing_character_ids == ()
