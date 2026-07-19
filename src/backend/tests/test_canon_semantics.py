from app.models.proposed_change import ChangeType
from app.services.canon_semantics import proposals_from_envelope


DM_TEXT = (
    "Гаррик указывает на западный проход. Каменная дверь открылась, "
    "и группа вошла в архив."
)


def test_outcome_first_fact_is_covered_and_evidence_backed():
    proposals, audit = proposals_from_envelope(
        {
            "outcomes": [
                {
                    "id": "o1",
                    "kind": "world_state",
                    "description": "Дверь архива открыта.",
                    "evidence": "Каменная дверь открылась",
                    "authority": "dm_confirmed",
                    "durable": True,
                }
            ],
            "proposals": [
                {
                    "outcome_id": "o1",
                    "change_type": "fact",
                    "operation": "revise",
                    "cardinality": "single",
                    "payload": {
                        "subject": "дверь архива",
                        "predicate": "состояние",
                        "object_value": "открыта",
                    },
                }
            ],
        },
        DM_TEXT,
    )
    assert audit.envelope_valid is True
    assert audit.coverage_ratio == 1.0
    assert audit.gap_count == 0
    assert len(proposals) == 1
    assert proposals[0].change_type == ChangeType.FACT
    assert proposals[0].payload["operation"] == "revise"
    assert proposals[0].payload["_canon"]["evidence"] == "Каменная дверь открылась"


def test_character_claim_cannot_become_objective_fact():
    proposals, audit = proposals_from_envelope(
        {
            "outcomes": [
                {
                    "id": "claim",
                    "kind": "knowledge_transfer",
                    "description": "Гаррик заявил, что проход безопасен.",
                    "evidence": "Гаррик указывает на западный проход",
                    "authority": "character_claim",
                    "durable": True,
                }
            ],
            "proposals": [
                {
                    "outcome_id": "claim",
                    "change_type": "fact",
                    "operation": "assert",
                    "cardinality": "single",
                    "payload": {
                        "subject": "западный проход",
                        "predicate": "безопасность",
                        "object_value": "безопасен",
                    },
                }
            ],
        },
        DM_TEXT,
    )
    assert audit.rejected_authority_count == 1
    assert audit.gap_count == 1
    assert proposals[0].change_type == ChangeType.CANON_GAP
    assert proposals[0].payload["_validation_error"]


def test_uncovered_durable_outcome_becomes_invalid_gap():
    proposals, audit = proposals_from_envelope(
        {
            "outcomes": [
                {
                    "id": "o1",
                    "kind": "event",
                    "description": "Группа вошла в архив.",
                    "evidence": "группа вошла в архив",
                    "authority": "public_observation",
                    "durable": True,
                }
            ],
            "proposals": [],
        },
        DM_TEXT,
    )
    assert audit.gap_count == 1
    assert audit.coverage_ratio == 0.0
    assert len(proposals) == 1
    assert proposals[0].change_type == ChangeType.CANON_GAP


def test_unsupported_evidence_is_rejected_not_silently_accepted():
    proposals, audit = proposals_from_envelope(
        {
            "outcomes": [
                {
                    "id": "o1",
                    "kind": "world_state",
                    "description": "Дракон уничтожен.",
                    "evidence": "Дракон рассыпался в золотой пепел",
                    "authority": "dm_confirmed",
                    "durable": True,
                }
            ],
            "proposals": [
                {
                    "outcome_id": "o1",
                    "change_type": "fact",
                    "payload": {
                        "subject": "дракон",
                        "predicate": "состояние",
                        "object_value": "уничтожен",
                    },
                }
            ],
        },
        DM_TEXT,
    )
    assert proposals == []
    assert audit.envelope_valid is False
    assert audit.rejected_evidence_count >= 1


def test_legacy_proposals_are_marked_as_unverified():
    proposals, audit = proposals_from_envelope(
        {
            "proposals": [
                {
                    "change_type": "fact",
                    "payload": {
                        "subject": "дверь",
                        "predicate": "состояние",
                        "object_value": "открыта",
                    },
                }
            ]
        },
        DM_TEXT,
    )
    assert len(proposals) == 1
    assert audit.legacy_envelope is True
    assert audit.envelope_valid is False
