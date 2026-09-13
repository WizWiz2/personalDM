from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.models.fact import FactRead
from app.models.proposed_change import ChangeType, ProposedChangeCreate
from app.services.fact_slot_reconciliation_guard import (
    FactSlotDecision,
    FactSlotMatch,
    FactSlotReview,
    _candidate_indexes,
    _fact_slot_wire_model,
    _review_from_decisions,
    apply_fact_slot_matches,
)


def _fact(*, scope="scene", scene_id=None, object_value="выключен"):
    campaign_id = uuid4()
    if scope == "scene" and scene_id is None:
        scene_id = uuid4()
    now = datetime.now(timezone.utc)
    return FactRead(
        id=uuid4(),
        campaign_id=campaign_id,
        subject="Свет в комнате Кая",
        predicate="состояние",
        object_value=object_value,
        truth_status="true",
        source_turn_id=None,
        confidence=1.0,
        visibility="public",
        scope=scope,
        scene_id=scene_id if scope == "scene" else None,
        memory_kind="scene_state" if scope == "scene" else "world_canon",
        subject_entity_id=None,
        is_current=True,
        superseded_by=None,
        created_at=now,
        updated_at=now,
    )


def _proposal(scene_id, *, object_value="лампа включена", subject="Комната Кая", predicate="освещение"):
    return ProposedChangeCreate(
        change_type=ChangeType.FACT,
        payload={
            "subject": subject,
            "predicate": predicate,
            "object_value": object_value,
            "truth_status": "true",
            "scope": "scene",
            "scene_id": str(scene_id),
            "operation": "assert",
            "cardinality": "single",
        },
    )


def test_semantic_slot_match_reuses_existing_fact_key_and_revises_value():
    scene_id = uuid4()
    current = _fact(scene_id=scene_id)
    proposal = _proposal(scene_id)

    result = apply_fact_slot_matches(
        [proposal],
        [current],
        FactSlotReview(
            matches=[FactSlotMatch(proposal_index=0, current_fact_id=str(current.id))]
        ),
    )

    payload = result[0].payload
    assert payload["subject"] == "Свет в комнате Кая"
    assert payload["predicate"] == "состояние"
    assert payload["previous_object_value"] == "выключен"
    assert payload["operation"] == "revise"
    assert payload["scope"] == "scene"
    assert payload["scene_id"] == str(scene_id)
    assert payload["memory_kind"] == "scene_state"


def test_same_value_stays_assert_and_becomes_repository_noop():
    scene_id = uuid4()
    current = _fact(scene_id=scene_id, object_value="включен")
    proposal = _proposal(scene_id, object_value="включен")

    result = apply_fact_slot_matches(
        [proposal],
        [current],
        FactSlotReview(
            matches=[FactSlotMatch(proposal_index=0, current_fact_id=str(current.id))]
        ),
    )

    assert result[0].payload["operation"] == "assert"
    assert result[0].payload["subject"] == current.subject
    assert result[0].payload["predicate"] == current.predicate


def test_unknown_fact_id_cannot_rewrite_proposal():
    scene_id = uuid4()
    current = _fact(scene_id=scene_id)
    proposal = _proposal(scene_id)

    result = apply_fact_slot_matches(
        [proposal],
        [current],
        FactSlotReview(
            matches=[FactSlotMatch(proposal_index=0, current_fact_id=str(uuid4()))]
        ),
    )

    assert result[0] == proposal


def test_scope_mismatch_cannot_rewrite_proposal():
    scene_id = uuid4()
    current = _fact(scene_id=uuid4())
    proposal = _proposal(scene_id)

    result = apply_fact_slot_matches(
        [proposal],
        [current],
        FactSlotReview(
            matches=[FactSlotMatch(proposal_index=0, current_fact_id=str(current.id))]
        ),
    )

    assert result[0] == proposal


def test_semantically_matched_scene_observation_revises_campaign_slot_and_value():
    current = _fact(scope="campaign")
    proposal = _proposal(uuid4(), predicate="свет включен", object_value="true")
    result = apply_fact_slot_matches(
        [proposal], [current], FactSlotReview(matches=[FactSlotMatch(
            proposal_index=0, current_fact_id=str(current.id), object_value="включен",
        )]),
    )
    payload = result[0].payload
    assert payload["scope"] == "campaign"
    assert "scene_id" not in payload
    assert payload["subject"] == current.subject
    assert payload["predicate"] == current.predicate
    assert payload["object_value"] == "включен"
    assert payload["operation"] == "revise"


def test_exact_structural_slot_skips_extra_semantic_call():
    scene_id = uuid4()
    current = _fact(scene_id=scene_id)
    proposal = _proposal(
        scene_id,
        subject=current.subject,
        predicate=current.predicate,
    )

    assert _candidate_indexes([proposal], [current]) == []


def test_paraphrased_single_slot_is_candidate_for_semantic_reconciliation():
    scene_id = uuid4()
    current = _fact(scene_id=scene_id)
    proposal = _proposal(scene_id)

    assert _candidate_indexes([proposal], [current]) == [0]


def test_wire_review_requires_exactly_one_decision_per_candidate():
    response_model = _fact_slot_wire_model(2)
    payload = {
        "decisions": [
            {"current_fact_id": None, "object_value": None},
            {"current_fact_id": str(uuid4()), "object_value": "включен"},
        ]
    }

    review = response_model.model_validate(payload)
    assert len(review.decisions) == 2

    for wrong_count in (0, 1, 3):
        wrong_payload = {
            "decisions": [
                {"current_fact_id": None, "object_value": None}
                for _ in range(wrong_count)
            ]
        }
        with pytest.raises(ValueError):
            response_model.model_validate(wrong_payload)


def test_wire_decisions_are_bound_to_machine_candidate_order():
    decisions = [
        FactSlotDecision(current_fact_id=str(uuid4()), object_value="включен"),
        FactSlotDecision(current_fact_id=None, object_value=None),
    ]

    review = _review_from_decisions([1, 3], decisions)

    assert [match.proposal_index for match in review.matches] == [1, 3]
    assert review.matches[0].current_fact_id == decisions[0].current_fact_id
    assert review.matches[1].current_fact_id is None
