from app.models.proposed_change import ChangeType
from app.services.memory_scribe import MemoryScribe


def test_supported_outcome_with_unknown_entity_becomes_canon_gap():
    scribe = MemoryScribe(None)
    proposals = scribe._parse_data(
        {
            "outcomes": [
                {
                    "id": "move1",
                    "kind": "movement",
                    "description": "Незнакомец вошёл в неизвестную башню.",
                    "evidence": "Незнакомец вошёл в неизвестную башню",
                    "authority": "dm_confirmed",
                    "durable": True,
                }
            ],
            "proposals": [
                {
                    "outcome_id": "move1",
                    "change_type": "movement",
                    "operation": "assert",
                    "cardinality": "single",
                    "payload": {
                        "character_id": "Незнакомец",
                        "location_id": "Неизвестная башня",
                        "description": "Незнакомец вошёл в неизвестную башню.",
                    },
                }
            ],
        },
        authoritative_text="Незнакомец вошёл в неизвестную башню.",
        known_entities={},
        known_ids=set(),
        acting_character_id=None,
        player_character_id=None,
        scene_participant_ids=[],
    )

    assert len(proposals) == 1
    assert proposals[0].change_type == ChangeType.CANON_GAP
    assert "failed backend" in proposals[0].payload["_validation_error"]
    assert scribe.last_audit["envelope_valid"] is False
    assert scribe.last_audit["gap_count"] == 1
    assert scribe.last_audit["coverage_ratio"] == 0.0
